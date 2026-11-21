"""Builds the patched iscsi_target_mod for the running kernel and writes the manifest.

It never loads a module; the target does that (see iscsicontainer.module).
"""
import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import urllib.request

NAME = "iscsi_target_mod"
MODULES_DIR = "/modules"
HOST_MODULES = "/host/lib/modules"
SHARE = "/usr/share/iscsicontainer/patches"
PATCH = SHARE + "/0001-iscsit-stop-session-rescan.patch"
FIXED = SHARE + "/fixed-since"
BOOT_ID = "/proc/sys/kernel/random/boot_id"
PROC_VERSION = "/proc/version"
OS_RELEASE = "/etc/os-release"
SOURCES = "/etc/apt/sources.list.d/iscsimodule.list"
WORK_ROOT = "/var/tmp"
SNAPSHOT = "https://snapshot.debian.org"
ALIVE_INTERVAL = 10
SUBDIR = "drivers/target/iscsi"
GOOD = ("patched", "original-has-fix")
REUSABLE = GOOD + ("original-unpatched",)


class BuildError(Exception):
    pass


def log(message):
    print(message, file=sys.stderr, flush=True)


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def sh(run, args, cwd=None):
    result = run(args, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-5:]
        raise BuildError(f"{' '.join(args[:3])}: " + " | ".join(tail))
    return result.stdout


def boot_id():
    return _read(BOOT_ID).strip()


def debian_version(text):
    """The Debian package version in /proc/version, None on any other kernel."""
    match = re.search(r"\bDebian (\d[0-9A-Za-z.+~:-]*)", text)
    return match.group(1) if match else None


def patch_state():
    digest = hashlib.sha256()
    for path in (PATCH, FIXED):
        with open(path, "rb") as handle:
            digest.update(handle.read())
    return digest.hexdigest()[:16]


def fixed_since(version, run=subprocess.run):
    for line in _read(FIXED).splitlines():
        line = line.split("#")[0].strip()
        if line and run(["dpkg", "--compare-versions", version, "ge", line]).returncode == 0:
            return True
    return False


def original(release):
    found = sorted(glob.glob(os.path.join(HOST_MODULES, release, "kernel", SUBDIR, NAME + ".ko*")))
    return found[0] if found else None


def srcversion(path, run=subprocess.run):
    return sh(run, ["modinfo", "-F", "srcversion", path]).strip().upper()


def _write_json(path, data):
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    os.replace(temporary, path)


def read_manifest(directory):
    try:
        data = json.loads(_read(os.path.join(directory, "manifest.json")))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class Alive:
    """Rewrites the alive file while the service works."""

    def __init__(self, directory, interval=ALIVE_INTERVAL):
        self.path = os.path.join(directory, "alive")
        self.interval = interval
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def beat(self):
        _write_json(self.path, {"boot_id": boot_id()})

    def _loop(self):
        while not self.done.wait(self.interval):
            self.beat()

    def start(self):
        self.beat()
        self.thread.start()

    def stop(self):
        self.done.set()
        self.thread.join()


def _codename():
    match = re.search(r"^VERSION_CODENAME=(\S+)", _read(OS_RELEASE), re.M)
    return match.group(1) if match else "trixie"


def _sources(lines):
    with open(SOURCES, "w", encoding="ascii") as handle:
        handle.write("\n".join(lines) + "\n")


def _download(release, version, work, run, options=()):
    sh(run, ["apt-get", *options, "update"])
    sh(run, ["apt-get", *options, "install", "-y", "--no-install-recommends",
             f"linux-headers-{release}={version}"])
    sh(run, ["apt-get", *options, "source", "--download-only", f"linux={version}"], cwd=work)


def _snapshot_sources(version, fetch):
    """Sources of the snapshot archive at the time that version was first seen."""
    with fetch(f"{SNAPSHOT}/mr/package/linux/{version}/srcfiles?fileinfo=1") as response:
        data = json.load(response)
    seen = [entry for entries in data.get("fileinfo", {}).values() for entry in entries]
    if not seen:
        raise BuildError(f"linux {version} is not in the snapshot archive")
    first = min(seen, key=lambda entry: entry["first_seen"])
    archive, stamp = first["archive_name"], first["first_seen"]
    name = _codename()
    suites = [name + "-security"] if archive == "debian-security" else [name, name + "-updates"]
    lines = []
    for suite in suites:
        for kind in ("deb", "deb-src"):
            lines.append(f"{kind} [check-valid-until=no] {SNAPSHOT}/archive/{archive}/{stamp}/ "
                         f"{suite} main")
    return lines


def fetch_source(release, version, work, run=subprocess.run, fetch=urllib.request.urlopen):
    """Headers of the kernel into the container, its exact source package into work."""
    name = _codename()
    _sources([f"deb-src http://deb.debian.org/debian {suite} main"
              for suite in (name, name + "-updates")]
             + [f"deb-src http://deb.debian.org/debian-security {name}-security main"])
    try:
        _download(release, version, work, run)
        return
    except BuildError as exc:
        log(f"not in the current archive ({exc}), trying the snapshot archive")
    try:
        lines = _snapshot_sources(version, fetch)
    except (OSError, ValueError, KeyError) as exc:
        raise BuildError(f"snapshot archive: {exc}") from None
    _sources(lines)
    _download(release, version, work, run, ("-o", "Acquire::Check-Valid-Until=false"))


def unpack(work, run=subprocess.run):
    found = glob.glob(os.path.join(work, "linux_*.dsc"))
    if not found:
        raise BuildError("no source package was downloaded")
    sh(run, ["dpkg-source", "--no-check", "-x", found[0], "src"], cwd=work)
    return os.path.join(work, "src")


def check(source, run=subprocess.run):
    """'bug' if the patch applies, 'fixed' if it applies in reverse, else 'unknown'."""
    base = ["patch", "-p1", "--dry-run", "--batch", "-i", PATCH]
    if run(base + ["--forward"], cwd=source, capture_output=True, text=True).returncode == 0:
        return "bug"
    if run(base + ["--reverse"], cwd=source, capture_output=True, text=True).returncode == 0:
        return "fixed"
    return "unknown"


def compile_module(source, release, run=subprocess.run):
    sh(run, ["patch", "-p1", "--batch", "--forward", "-i", PATCH], cwd=source)
    directory = os.path.join(source, SUBDIR)
    sh(run, ["make", "-C", f"/lib/modules/{release}/build", f"M={directory}",
             "CONFIG_ISCSI_TARGET_CXGB4=", "modules"])
    built = os.path.join(directory, NAME + ".ko")
    sh(run, ["strip", "--strip-debug", built])
    return built


def build(release, version, directory, run=subprocess.run, fetch=urllib.request.urlopen):
    """Try to put a patched module into the directory; returns (status, file name)."""
    work = tempfile.mkdtemp(prefix="iscsimodule-", dir=WORK_ROOT)
    try:
        fetch_source(release, version, work, run, fetch)
        source = unpack(work, run)
        found = check(source, run)
        if found == "fixed":
            log("the kernel source already contains the fix")
            return "original-has-fix", None
        if found == "unknown":
            raise BuildError("the patch does not apply to this kernel source")
        built = compile_module(source, release, run)
        shutil.copyfile(built, os.path.join(directory, NAME + ".ko"))
        return "patched", NAME + ".ko"
    except BuildError as exc:
        log(f"build failed: {exc}")
        return "build-failed", None
    finally:
        shutil.rmtree(work, ignore_errors=True)


def provide(release, directory, run=subprocess.run, fetch=urllib.request.urlopen):
    """Make sure the cache holds a module for the kernel and return its manifest."""
    state = patch_state()
    previous = read_manifest(directory) or {}
    reusable = (previous.get("kernel") == release and previous.get("patch") == state
                and previous.get("status") in REUSABLE
                and os.path.isfile(os.path.join(directory, str(previous.get("module")))))
    if reusable:
        log(f"module for {release} is in the cache, status {previous['status']}")
        return dict(previous, boot_id=boot_id())
    version = debian_version(_read(PROC_VERSION))
    if version is None:
        log("not a Debian kernel, the patch is not maintained for it")
        status, module = "original-unpatched", None
    elif fixed_since(version, run):
        log(f"Debian kernel {version} contains the fix")
        status, module = "original-has-fix", None
    else:
        log(f"building {NAME} for Debian kernel {release} ({version})")
        status, module = build(release, version, directory, run, fetch)
    if module is None:
        source = original(release)
        if source is not None:
            module = os.path.basename(source)
            shutil.copyfile(source, os.path.join(directory, module))
    manifest = {"kernel": release, "boot_id": boot_id(), "patch": state, "status": status,
                "module": module, "srcversion": None, "valid_srcversions": []}
    if module is not None:
        manifest["srcversion"] = srcversion(os.path.join(directory, module), run)
        manifest["valid_srcversions"] = [manifest["srcversion"]]
    if status in GOOD and previous.get("kernel") == release and previous.get("status") in GOOD:
        for value in previous.get("valid_srcversions", []):
            if value not in manifest["valid_srcversions"]:
                manifest["valid_srcversions"].append(value)
    return manifest


def cleanup(release):
    """Remove the cache of kernels that are no longer installed on the host."""
    if not os.path.isdir(HOST_MODULES):
        return
    for name in os.listdir(MODULES_DIR):
        if name != release and not os.path.isdir(os.path.join(HOST_MODULES, name)):
            log(f"removing the cache of {name}")
            shutil.rmtree(os.path.join(MODULES_DIR, name), ignore_errors=True)


def serve(stop=None, run=subprocess.run, fetch=urllib.request.urlopen):
    release = os.uname().release
    directory = os.path.join(MODULES_DIR, release)
    os.makedirs(directory, exist_ok=True)
    alive = Alive(directory)
    alive.start()
    try:
        cleanup(release)
        manifest = provide(release, directory, run, fetch)
        _write_json(os.path.join(directory, "manifest.json"), manifest)
    finally:
        alive.stop()
    log(f"manifest written: status {manifest['status']}, srcversion {manifest['srcversion']}")
    stop = threading.Event() if stop is None else stop
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda *_: stop.set())
    stop.wait()
    return 0


def health():
    manifest = read_manifest(os.path.join(MODULES_DIR, os.uname().release))
    return 0 if manifest and manifest.get("kernel") == os.uname().release else 1


def newest_kernel(run=subprocess.run):
    """Release and version of the newest Debian kernel for this architecture."""
    architecture = sh(run, ["dpkg", "--print-architecture"]).strip()
    sh(run, ["apt-get", "update"])
    shown = sh(run, ["apt-cache", "show", f"linux-headers-{architecture}"])
    match = re.search(r"^Depends:.*?linux-headers-(\S+) \(= ([^)]+)\)", shown, re.M)
    if not match:
        raise BuildError("the newest kernel cannot be determined")
    return match.group(1), match.group(2)


def build_test(release=None, version=None, run=subprocess.run, fetch=urllib.request.urlopen):
    """Build for a kernel that is not running; fails unless the patch or the fix is in."""
    try:
        if release is None:
            release, version = newest_kernel(run)
    except BuildError as exc:
        log(str(exc))
        return 1
    directory = tempfile.mkdtemp(prefix="iscsimodule-test-", dir=WORK_ROOT)
    try:
        status, module = build(release, version, directory, run, fetch)
        found = srcversion(os.path.join(directory, module), run) if module else None
        print(f"{release} {version} {status} {found}")
        return 0 if status in GOOD else 1
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="iscsimodule")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="build or reuse the module, write the manifest, stay up")
    commands.add_parser("health")
    test = commands.add_parser("build-test", help="build for the newest or a given kernel")
    test.add_argument("--kernel")
    test.add_argument("--version")
    args = parser.parse_args(argv)
    if args.command == "run":
        return serve()
    if args.command == "health":
        return health()
    return build_test(args.kernel, args.version)


def entry():
    sys.exit(main())
