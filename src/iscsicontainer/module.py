import json
import os
import subprocess
import time
from dataclasses import dataclass

from . import log

NAME = "iscsi_target_mod"
MODULES_DIR = "/modules"
SYS_MODULE = "/sys/module/" + NAME
BOOT_ID = "/proc/sys/kernel/random/boot_id"
ALIVE_MAX_AGE = 60
GRACE = 60
POLL = 2
GOOD = ("patched", "original-has-fix")
FINAL = GOOD + ("original-unpatched",)


class ModuleError(Exception):
    pass


@dataclass
class Result:
    mcs: bool
    status: str


def _read(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return None


def _json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def boot_id():
    return _read(BOOT_ID)


def loaded():
    return os.path.isdir(SYS_MODULE)


def srcversion():
    value = _read(os.path.join(SYS_MODULE, "srcversion"))
    return value.upper() if value else None


def kernel_dir():
    return os.path.join(MODULES_DIR, os.uname().release)


def read_manifest():
    """The manifest of the running kernel if it may be used now, else None."""
    manifest = _json(os.path.join(kernel_dir(), "manifest.json"))
    if manifest is None or manifest.get("kernel") != os.uname().release:
        return None
    status = manifest.get("status")
    if status in FINAL:
        return manifest
    if status == "build-failed" and manifest.get("boot_id") == boot_id():
        return manifest
    return None


def alive_age(now):
    """Seconds since the module service of this boot last reported, None if it has not."""
    path = os.path.join(kernel_dir(), "alive")
    data = _json(path)
    if data is None or data.get("boot_id") != boot_id():
        return None
    try:
        return now - os.path.getmtime(path)
    except OSError:
        return None


def wait_for_manifest(limit, clock=time.time, sleep=time.sleep):
    start = clock()
    while True:
        manifest = read_manifest()
        if manifest is not None:
            return manifest
        now = clock()
        elapsed = now - start
        if elapsed >= limit:
            log.warning(f"no manifest from the module service within {limit} s")
            return None
        if elapsed >= GRACE:
            age = alive_age(now)
            if age is None:
                log.warning("the module service is not running")
                return None
            if age > ALIVE_MAX_AGE:
                log.warning("the module service stopped without writing a manifest")
                return None
        sleep(POLL)


def _call(run, args):
    return run(args, capture_output=True, text=True)


def insmod(path, run=subprocess.run):
    """Load the dependencies with modprobe, then the module file with insmod."""
    depends = _call(run, ["modinfo", "-F", "depends", path])
    if depends.returncode != 0:
        log.error(f"{path}: {depends.stderr.strip()}")
        return False
    for name in filter(None, depends.stdout.strip().split(",")):
        result = _call(run, ["modprobe", name])
        if result.returncode != 0:
            log.error(f"modprobe {name}: {result.stderr.strip()}")
            return False
    result = _call(run, ["insmod", path])
    if result.returncode != 0:
        log.error(f"insmod {path}: {result.stderr.strip()}")
        return False
    return True


def original_path(run=subprocess.run):
    result = _call(run, ["modinfo", "-n", NAME])
    path = result.stdout.strip()
    return path if result.returncode == 0 and path.startswith("/") else None


def prepare(cfg, run=subprocess.run, clock=time.time, sleep=time.sleep):
    """Make sure iscsi_target_mod is loaded and say whether MC/S may be used with it."""
    trusted = set(cfg.trusted_srcversions)
    manifest = None
    if not trusted:
        manifest = read_manifest()
        if manifest is None and cfg.wants_mcs:
            log.info("waiting for the module service")
            manifest = wait_for_manifest(cfg.module_wait, clock, sleep)
    if manifest is not None:
        status = manifest["status"]
    else:
        status = "trusted" if trusted else "no-manifest"
    if not loaded():
        done = False
        if manifest is not None:
            done = insmod(os.path.join(kernel_dir(), str(manifest.get("module"))), run)
            if not done:
                status = "load-failed"
                log.warning("the module from the cache cannot be loaded, using the original")
        if not done:
            path = original_path(run)
            if path is None or not insmod(path, run):
                raise ModuleError(f"{NAME} cannot be loaded")
    valid = set(trusted)
    if manifest is not None and status in GOOD:
        valid.update(str(v).upper() for v in manifest.get("valid_srcversions", []))
    current = srcversion()
    mcs = current is not None and current in valid
    if not mcs:
        if status in GOOD:
            status = "mismatch"
        elif status == "trusted":
            status = "untrusted"
    log.info(f"{NAME} srcversion {current}, module status {status}")
    return Result(mcs, status)
