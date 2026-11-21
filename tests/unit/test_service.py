import io
import json
import os
import signal
import threading

import pytest

from iscsimodule import service
from iscsimodule.service import BuildError

from .conftest import Runner, completed

REL = os.uname().release
DEBIAN = ("Linux version 6.12.117+deb13-amd64 (debian-kernel@lists.debian.org) "
          "(x86_64-linux-gnu-gcc-14) #1 SMP PREEMPT_DYNAMIC Debian 6.12.117-1 (2026-11-12)\n")
OTHER = "Linux version 6.18.58.1-microsoft-standard-WSL2 (root@host) #1 SMP\n"
SRC = "AAAA1111BBBB2222CCCC333"
SNAPSHOT = {"fileinfo": {
    "h1": [{"archive_name": "debian", "first_seen": "20261113T030000Z", "name": "a.dsc"}],
    "h2": [{"archive_name": "debian", "first_seen": "20261112T210000Z", "name": "b.tar.xz"}]}}


@pytest.fixture
def world(tmp_path, monkeypatch):
    """The paths the service uses, below a temporary directory."""
    for name in ("modules", "host", "work", "share"):
        (tmp_path / name).mkdir()
    (tmp_path / "share" / "patch").write_text("patch 1\n")
    (tmp_path / "share" / "fixed").write_text("# none yet\n")
    (tmp_path / "boot").write_text("boot-1\n")
    (tmp_path / "version").write_text(DEBIAN)
    (tmp_path / "os-release").write_text('NAME="Debian"\nVERSION_CODENAME=trixie\n')
    for attribute, value in (("MODULES_DIR", "modules"), ("HOST_MODULES", "host"),
                             ("WORK_ROOT", "work"), ("PATCH", "share/patch"),
                             ("FIXED", "share/fixed"), ("BOOT_ID", "boot"),
                             ("PROC_VERSION", "version"), ("OS_RELEASE", "os-release"),
                             ("SOURCES", "sources.list")):
        monkeypatch.setattr(service, attribute, str(tmp_path / value))
    return tmp_path


def host_module(world, release=REL, name="iscsi_target_mod.ko.xz"):
    directory = world / "host" / release / "kernel" / "drivers" / "target" / "iscsi"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_bytes(b"original")
    return directory / name


def builder(world, check=("bug",), fail=()):
    """A runner that behaves like a working build: download, unpack, patch, make."""
    answers = {"modinfo -F srcversion": completed(stdout=SRC.lower() + "\n"),
               "dpkg --print-architecture": completed(stdout="amd64\n"),
               "apt-cache show": completed(
                   stdout="Package: linux-headers-amd64\nDepends: linux-headers-6.12.120+deb13-"
                          "amd64 (= 6.12.120-1)\n")}
    run = Runner(answers)
    checks = list(check)

    def call(args, cwd=None, **kwargs):
        run.commands.append(list(args))
        text = " ".join(args)
        for needle in fail:
            if needle in text:
                return completed(1, stderr="line1\nboom " + needle)
        if args[:2] == ["apt-get", "source"] or "source" in args and args[0] == "apt-get":
            with open(os.path.join(cwd, "linux_6.12.117-1.dsc"), "w", encoding="ascii"):
                pass
        elif args[0] == "dpkg-source":
            os.makedirs(os.path.join(cwd, "src", service.SUBDIR))
        elif args[0] == "patch" and "--dry-run" in args:
            wanted = "--forward" if "--forward" in args else "--reverse"
            state = checks[0]
            good = (state == "bug" and wanted == "--forward") or \
                   (state == "fixed" and wanted == "--reverse")
            return completed(0 if good else 1)
        elif args[0] == "make":
            directory = [a for a in args if a.startswith("M=")][0][2:]
            with open(os.path.join(directory, "iscsi_target_mod.ko"), "wb") as handle:
                handle.write(b"patched")
        for size in range(len(args), 0, -1):
            key = " ".join(args[:size])
            if key in answers:
                return answers[key]
        return completed()
    return run, call


def test_small_helpers(world, capsys):
    service.log("hello")
    assert capsys.readouterr().err == "hello\n"
    assert service.boot_id() == "boot-1"
    assert service.debian_version(DEBIAN) == "6.12.117-1"
    assert service.debian_version(OTHER) is None
    first = service.patch_state()
    (world / "share" / "fixed").write_text("6.12.130-1\n")
    assert len(first) == 16 and service.patch_state() != first
    assert service._codename() == "trixie"
    (world / "os-release").write_text("NAME=x\n")
    assert service._codename() == "trixie"


def test_sh():
    assert service.sh(Runner({"echo": completed(stdout="out")}), ["echo", "a"]) == "out"
    with pytest.raises(BuildError, match="make -C x: l2 | l3"):
        service.sh(Runner({"make": completed(1, stderr="l1\nl2\nl3")}), ["make", "-C", "x", "y"])
    with pytest.raises(BuildError):
        service.sh(Runner({"make": completed(2)}), ["make"])


def test_fixed_since(world):
    (world / "share" / "fixed").write_text("# comment\n\n6.12.130-1  # first fixed\n")
    newer = Runner({"dpkg --compare-versions 6.12.131-1": completed(0),
                    "dpkg --compare-versions": completed(1)})
    assert service.fixed_since("6.12.131-1", newer) and not service.fixed_since("6.12.117-1",
                                                                                 newer)
    assert newer.commands[0] == ["dpkg", "--compare-versions", "6.12.131-1", "ge", "6.12.130-1"]


def test_original_and_srcversion(world):
    assert service.original(REL) is None
    path = host_module(world)
    assert service.original(REL) == str(path)
    assert service.srcversion("x.ko", Runner({"modinfo": completed(stdout="abc\n")})) == "ABC"


def test_manifest_file(world):
    directory = str(world / "modules")
    assert service.read_manifest(directory) is None
    (world / "modules" / "manifest.json").write_text("[1]")
    assert service.read_manifest(directory) is None
    (world / "modules" / "manifest.json").write_text("{broken")
    assert service.read_manifest(directory) is None
    service._write_json(os.path.join(directory, "manifest.json"), {"a": 1})
    assert service.read_manifest(directory) == {"a": 1}


def test_alive(world):
    alive = service.Alive(str(world / "modules"), interval=0.01)
    alive.start()
    path = world / "modules" / "alive"
    first = path.stat().st_mtime_ns
    deadline = threading.Event()
    while path.stat().st_mtime_ns == first and not deadline.wait(0.01):
        pass
    alive.stop()
    assert json.loads(path.read_text()) == {"boot_id": "boot-1"}
    assert not alive.thread.is_alive()


def test_fetch_source_from_the_archive(world):
    run, call = builder(world)
    service.fetch_source("6.12.117+deb13-amd64", "6.12.117-1", str(world / "work"), call)
    assert run.commands == [
        ["apt-get", "update"],
        ["apt-get", "install", "-y", "--no-install-recommends",
         "linux-headers-6.12.117+deb13-amd64=6.12.117-1"],
        ["apt-get", "source", "--download-only", "linux=6.12.117-1"]]
    lines = (world / "sources.list").read_text().splitlines()
    assert lines == ["deb-src http://deb.debian.org/debian trixie main",
                     "deb-src http://deb.debian.org/debian trixie-updates main",
                     "deb-src http://deb.debian.org/debian-security trixie-security main"]


def _snapshot(data=None, error=None):
    def fetch(url):
        fetch.urls.append(url)
        if error:
            raise error
        return io.StringIO(json.dumps(SNAPSHOT if data is None else data))
    fetch.urls = []
    return fetch


def test_fetch_source_from_the_snapshot_archive(world, capsys):
    run = Runner({"apt-get install": [completed(1, stderr="version not found"), completed()]})
    fetch = _snapshot()
    service.fetch_source("r", "6.12.48-1", str(world / "work"), run, fetch)
    assert fetch.urls == [
        "https://snapshot.debian.org/mr/package/linux/6.12.48-1/srcfiles?fileinfo=1"]
    lines = (world / "sources.list").read_text().splitlines()
    base = "[check-valid-until=no] https://snapshot.debian.org/archive/debian/20261112T210000Z/"
    assert lines == [f"deb {base} trixie main", f"deb-src {base} trixie main",
                     f"deb {base} trixie-updates main", f"deb-src {base} trixie-updates main"]
    assert run.commands[-1][:3] == ["apt-get", "-o", "Acquire::Check-Valid-Until=false"]
    assert "trying the snapshot archive" in capsys.readouterr().err


def test_snapshot_security_archive_and_errors(world):
    security = {"fileinfo": {"h": [{"archive_name": "debian-security",
                                    "first_seen": "20261101T000000Z", "name": "a.dsc"}]}}
    lines = service._snapshot_sources("6.12.50-1", _snapshot(security))
    assert all("archive/debian-security/20261101T000000Z/ trixie-security main" in line
               for line in lines) and len(lines) == 2
    with pytest.raises(BuildError, match="not in the snapshot archive"):
        service._snapshot_sources("1.0-1", _snapshot({}))
    failing = Runner({"apt-get update": completed(1, stderr="offline")})
    for error in (OSError("offline"), ValueError("bad json")):
        with pytest.raises(BuildError, match="snapshot archive"):
            service.fetch_source("r", "v", str(world / "work"), failing, _snapshot(error=error))
    with pytest.raises(BuildError, match="snapshot archive"):
        service.fetch_source("r", "v", str(world / "work"), failing,
                             _snapshot({"fileinfo": {"h": [{"name": "x"}]}}))


def test_unpack(world):
    work = str(world / "work")
    with pytest.raises(BuildError, match="no source package"):
        service.unpack(work, Runner())
    (world / "work" / "linux_6.12.117-1.dsc").write_text("")
    run = Runner()
    assert service.unpack(work, run) == os.path.join(work, "src")
    assert run.commands[0][:3] == ["dpkg-source", "--no-check", "-x"]


@pytest.mark.parametrize("state", ["bug", "fixed", "unknown"])
def test_check(world, state):
    run, call = builder(world, check=(state,))
    assert service.check(str(world), call) == state


def test_build(world, capsys):
    directory = str(world / "modules")
    run, call = builder(world)
    assert service.build("6.12.117+deb13-amd64", "6.12.117-1", directory, call) == (
        "patched", "iscsi_target_mod.ko")
    assert (world / "modules" / "iscsi_target_mod.ko").read_bytes() == b"patched"
    make = [c for c in run.commands if c[0] == "make"][0]
    assert make[1:3] == ["-C", "/lib/modules/6.12.117+deb13-amd64/build"]
    assert "CONFIG_ISCSI_TARGET_CXGB4=" in make and make[-1] == "modules"
    assert os.listdir(world / "work") == []
    run, call = builder(world, check=("fixed",))
    assert service.build("r", "v", directory, call) == ("original-has-fix", None)
    run, call = builder(world, check=("unknown",))
    assert service.build("r", "v", directory, call) == ("build-failed", None)
    run, call = builder(world, fail=("make",))
    assert service.build("r", "v", directory, call) == ("build-failed", None)
    err = capsys.readouterr().err
    assert "does not apply" in err and "boom make" in err and "already contains the fix" in err
    assert os.listdir(world / "work") == []


def test_provide_builds_and_reuses(world, capsys):
    directory = world / "modules" / REL
    directory.mkdir()
    run, call = builder(world)
    manifest = service.provide(REL, str(directory), call)
    assert manifest == {"kernel": REL, "boot_id": "boot-1", "patch": service.patch_state(),
                        "status": "patched", "module": "iscsi_target_mod.ko",
                        "srcversion": SRC, "valid_srcversions": [SRC]}
    service._write_json(str(directory / "manifest.json"), manifest)
    (world / "boot").write_text("boot-2\n")
    again = service.provide(REL, str(directory), Runner())
    assert again == dict(manifest, boot_id="boot-2")
    assert "is in the cache" in capsys.readouterr().err


def test_provide_rebuilds_for_a_changed_patch(world):
    directory = world / "modules" / REL
    directory.mkdir()
    run, call = builder(world)
    service._write_json(str(directory / "manifest.json"), service.provide(REL, str(directory),
                                                                          call))
    (world / "share" / "patch").write_text("patch 2\n")
    run, call = builder(world)
    run.answers["modinfo -F srcversion"] = completed(stdout="dddd4444\n")
    manifest = service.provide(REL, str(directory), call)
    assert manifest["srcversion"] == "DDDD4444"
    assert manifest["valid_srcversions"] == ["DDDD4444", SRC]
    service._write_json(str(directory / "manifest.json"), manifest)
    (world / "share" / "patch").write_text("patch 3\n")
    again = service.provide(REL, str(directory), call)
    assert again["valid_srcversions"] == ["DDDD4444", SRC]


def test_provide_other_kernels(world, capsys):
    directory = world / "modules" / REL
    directory.mkdir()
    (world / "version").write_text(OTHER)
    manifest = service.provide(REL, str(directory), Runner())
    assert (manifest["status"], manifest["module"], manifest["srcversion"]) == (
        "original-unpatched", None, None) and manifest["valid_srcversions"] == []
    host_module(world)
    run = Runner({"modinfo": completed(stdout="eeee\n")})
    manifest = service.provide(REL, str(directory), run)
    assert (manifest["status"], manifest["module"], manifest["srcversion"]) == (
        "original-unpatched", "iscsi_target_mod.ko.xz", "EEEE")
    assert (directory / "iscsi_target_mod.ko.xz").read_bytes() == b"original"
    (world / "version").write_text(DEBIAN)
    (world / "share" / "fixed").write_text("6.12.100-1\n")
    manifest = service.provide(REL, str(directory), run)
    assert manifest["status"] == "original-has-fix"
    out = capsys.readouterr().err
    assert "not a Debian kernel" in out and "contains the fix" in out


def test_provide_build_failed_is_retried(world):
    directory = world / "modules" / REL
    directory.mkdir()
    host_module(world)
    run, call = builder(world, fail=("make",))
    manifest = service.provide(REL, str(directory), call)
    assert (manifest["status"], manifest["module"]) == ("build-failed", "iscsi_target_mod.ko.xz")
    service._write_json(str(directory / "manifest.json"), manifest)
    run, call = builder(world)
    assert service.provide(REL, str(directory), call)["status"] == "patched"


def test_cleanup(world, monkeypatch, capsys):
    for name in (REL, "old-kernel", "installed-kernel"):
        (world / "modules" / name).mkdir()
    (world / "host" / "installed-kernel").mkdir()
    service.cleanup(REL)
    assert sorted(os.listdir(world / "modules")) == sorted([REL, "installed-kernel"])
    assert "removing the cache of old-kernel" in capsys.readouterr().err
    monkeypatch.setattr(service, "HOST_MODULES", str(world / "missing"))
    (world / "modules" / "another").mkdir()
    service.cleanup(REL)
    assert "another" in os.listdir(world / "modules")


def test_serve_and_health(world, monkeypatch, capsys):
    handlers = {}
    monkeypatch.setattr(signal, "signal", lambda number, handler: handlers.update({number: handler}))
    (world / "version").write_text(OTHER)
    assert service.health() == 1
    stop = threading.Event()
    real_wait = stop.wait

    def wait(timeout=None):
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        return real_wait(0)
    stop.wait = wait
    assert service.serve(stop, Runner()) == 0 and service.health() == 0
    directory = world / "modules" / REL
    assert json.loads((directory / "manifest.json").read_text())["status"] == "original-unpatched"
    assert json.loads((directory / "alive").read_text()) == {"boot_id": "boot-1"}
    assert "manifest written: status original-unpatched" in capsys.readouterr().err


def test_newest_kernel(world):
    run, call = builder(world)
    assert service.newest_kernel(call) == ("6.12.120+deb13-amd64", "6.12.120-1")
    with pytest.raises(BuildError, match="cannot be determined"):
        service.newest_kernel(Runner({"dpkg": completed(stdout="amd64\n")}))


def test_build_test(world, capsys):
    run, call = builder(world)
    assert service.build_test(None, None, call) == 0
    assert capsys.readouterr().out == f"6.12.120+deb13-amd64 6.12.120-1 patched {SRC}\n"
    run, call = builder(world, check=("unknown",))
    assert service.build_test("r", "v", call) == 1
    assert capsys.readouterr().out == "r v build-failed None\n"
    assert service.build_test(None, None, Runner({"dpkg": completed(1, stderr="broken")})) == 1
    assert os.listdir(world / "work") == []


def test_main(monkeypatch):
    monkeypatch.setattr(service, "serve", lambda: 10)
    monkeypatch.setattr(service, "health", lambda: 11)
    monkeypatch.setattr(service, "build_test", lambda kernel, version: (kernel, version))
    assert service.main(["run"]) == 10 and service.main(["health"]) == 11
    assert service.main(["build-test", "--kernel", "r", "--version", "v"]) == ("r", "v")
    monkeypatch.setattr(service, "main", lambda: 4)
    with pytest.raises(SystemExit) as stop:
        service.entry()
    assert stop.value.code == 4
