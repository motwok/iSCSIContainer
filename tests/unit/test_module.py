import json
import os

import pytest

from iscsicontainer import module
from iscsicontainer.module import ModuleError

from .conftest import Runner, completed, parse

BOOT = "boot-1"
SRC = "AAAA1111BBBB2222CCCC333"
ORIGINAL = "/lib/modules/x/iscsi_target_mod.ko"


class World:
    """A fake /modules, /sys/module and boot id below a temporary directory."""

    def __init__(self, tmp_path, monkeypatch):
        self.sys = tmp_path / "sys"
        self.boot = tmp_path / "boot_id"
        self.boot.write_text(BOOT + "\n")
        monkeypatch.setattr(module, "MODULES_DIR", str(tmp_path / "modules"))
        monkeypatch.setattr(module, "SYS_MODULE", str(self.sys))
        monkeypatch.setattr(module, "BOOT_ID", str(self.boot))
        self.dir = tmp_path / "modules" / os.uname().release
        self.dir.mkdir(parents=True)
        self.now = 1000.0

    def manifest(self, status="patched", **extra):
        data = {"kernel": os.uname().release, "boot_id": BOOT, "status": status,
                "module": "iscsi_target_mod.ko", "srcversion": SRC, "valid_srcversions": [SRC]}
        data.update(extra)
        (self.dir / "manifest.json").write_text(json.dumps(data))

    def alive(self, age=0.0, boot=BOOT):
        path = self.dir / "alive"
        path.write_text(json.dumps({"boot_id": boot}))
        os.utime(path, (self.now - age, self.now - age))

    def load(self, srcversion=SRC):
        self.sys.mkdir(exist_ok=True)
        if srcversion is not None:
            (self.sys / "srcversion").write_text(srcversion.lower() + "\n")

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def test_basics(world):
    assert module.boot_id() == BOOT and not module.loaded() and module.srcversion() is None
    world.load()
    assert module.loaded() and module.srcversion() == SRC
    assert module.kernel_dir() == str(world.dir)


def test_read_manifest(world):
    assert module.read_manifest() is None
    (world.dir / "manifest.json").write_text("[1]")
    assert module.read_manifest() is None
    (world.dir / "manifest.json").write_text("not json")
    assert module.read_manifest() is None
    world.manifest(kernel="other")
    assert module.read_manifest() is None
    for status in module.FINAL:
        world.manifest(status, boot_id="older")
        assert module.read_manifest()["status"] == status
    world.manifest("build-failed", boot_id="older")
    assert module.read_manifest() is None
    world.manifest("build-failed")
    assert module.read_manifest()["status"] == "build-failed"
    world.manifest("building")
    assert module.read_manifest() is None


def test_alive_age(world, monkeypatch):
    assert module.alive_age(world.now) is None
    world.alive(age=5, boot="older")
    assert module.alive_age(world.now) is None
    world.alive(age=5)
    assert module.alive_age(world.now) == pytest.approx(5)
    monkeypatch.setattr(os.path, "getmtime", lambda path: (_ for _ in ()).throw(OSError()))
    assert module.alive_age(world.now) is None


def test_wait_returns_the_manifest_when_it_appears(world):
    world.alive()

    def sleep(seconds):
        world.sleep(seconds)
        world.alive()
        if world.now >= 1100:
            world.manifest()
    assert module.wait_for_manifest(900, world.clock, sleep)["status"] == "patched"
    assert world.now == pytest.approx(1100)


def test_wait_gives_up(world, logs):
    assert module.wait_for_manifest(900, world.clock, world.sleep) is None
    assert world.now == pytest.approx(1060) and "is not running" in logs()
    world.alive(age=0)
    start = world.now
    assert module.wait_for_manifest(900, world.clock, world.sleep) is None
    assert world.now - start == pytest.approx(62) and "stopped without" in logs()
    start = world.now

    def sleep(seconds):
        world.sleep(seconds)
        world.alive()
    assert module.wait_for_manifest(100, world.clock, sleep) is None
    assert world.now - start == pytest.approx(100) and "within 100 s" in logs()


def test_insmod(logs):
    run = Runner({"modinfo -F depends": completed(stdout="target_core_mod,libcrc32c\n")})
    assert module.insmod("/m/x.ko", run)
    assert run.commands[1:] == [["modprobe", "target_core_mod"], ["modprobe", "libcrc32c"],
                                ["insmod", "/m/x.ko"]]
    run = Runner({"modinfo -F depends": completed(stdout="\n")})
    assert module.insmod("/m/x.ko", run) and run.commands[1:] == [["insmod", "/m/x.ko"]]
    assert not module.insmod("/m/x.ko", Runner({"modinfo": completed(1, stderr="no file")}))
    assert not module.insmod("/m/x.ko", Runner({
        "modinfo": completed(stdout="dep"), "modprobe dep": completed(1, stderr="missing")}))
    assert not module.insmod("/m/x.ko", Runner({"insmod": completed(1, stderr="Key rejected")}))
    out = logs()
    assert "no file" in out and "missing" in out and "Key rejected" in out


def test_original_path():
    assert module.original_path(Runner({"modinfo -n": completed(stdout=ORIGINAL + "\n")})) \
        == ORIGINAL
    assert module.original_path(Runner({"modinfo -n": completed(stdout="(builtin)\n")})) is None
    assert module.original_path(Runner({"modinfo -n": completed(1)})) is None


def _single():
    return parse(lambda raw: raw["targets"][0].update(max_connections=1))


def _trusted(*values):
    return parse(lambda raw: raw.update(module={"trusted_srcversions": list(values)}))


def _loader(world, srcversion=SRC, fail=()):
    """A runner whose insmod loads the fake module unless the path is in fail."""
    answers = {"modinfo -n": completed(stdout=ORIGINAL + "\n")}
    run = Runner(answers)

    def call(args, **kwargs):
        if args[0] == "insmod":
            run.commands.append(list(args))
            if args[1] in fail:
                return completed(1, stderr="rejected")
            world.load(srcversion)
            return completed()
        return Runner.__call__(run, args, **kwargs)
    return run, call


def test_prepare_with_a_patched_module(world):
    world.manifest()
    run, call = _loader(world)
    result = module.prepare(parse(), call, world.clock, world.sleep)
    assert (result.mcs, result.status) == (True, "patched")
    assert run.commands[-1] == ["insmod", str(world.dir / "iscsi_target_mod.ko")]


def test_prepare_single_connection_does_not_wait(world):
    run, call = _loader(world, "FFFF")
    result = module.prepare(_single(), call, world.clock, world.sleep)
    assert (result.mcs, result.status) == (False, "no-manifest") and world.now == 1000.0
    assert run.commands[-1] == ["insmod", ORIGINAL]


def test_prepare_waits_then_falls_back(world, logs):
    run, call = _loader(world, "FFFF")
    result = module.prepare(parse(), call, world.clock, world.sleep)
    assert (result.mcs, result.status) == (False, "no-manifest") and world.now >= 1060
    assert "waiting for the module service" in logs()


@pytest.mark.parametrize("status", ["original-unpatched", "build-failed"])
def test_prepare_without_the_patch(world, status):
    world.manifest(status)
    run, call = _loader(world)
    result = module.prepare(parse(), call, world.clock, world.sleep)
    assert (result.mcs, result.status) == (False, status)


def test_prepare_load_from_cache_fails(world, logs):
    world.manifest()
    run, call = _loader(world, fail=(str(world.dir / "iscsi_target_mod.ko"),))
    result = module.prepare(parse(), call, world.clock, world.sleep)
    assert (result.mcs, result.status) == (False, "load-failed")
    assert run.commands[-1] == ["insmod", ORIGINAL] and "using the original" in logs()


def test_prepare_nothing_can_be_loaded(world):
    run, call = _loader(world, fail=(ORIGINAL,))
    with pytest.raises(ModuleError):
        module.prepare(_single(), call, world.clock, world.sleep)
    with pytest.raises(ModuleError):
        module.prepare(_single(), Runner({"modinfo -n": completed(1)}), world.clock, world.sleep)


def test_prepare_other_module_already_loaded(world):
    world.manifest()
    world.load("FFFF")
    result = module.prepare(parse(), Runner(), world.clock, world.sleep)
    assert (result.mcs, result.status) == (False, "mismatch")
    world.manifest(valid_srcversions=[SRC, "ffff"])
    assert module.prepare(parse(), Runner(), world.clock, world.sleep).mcs


def test_prepare_no_srcversion(world):
    world.manifest()
    world.load(None)
    assert module.prepare(parse(), Runner(), world.clock, world.sleep).status == "mismatch"


def test_prepare_trusted(world):
    world.manifest("build-failed")
    run, call = _loader(world)
    result = module.prepare(_trusted(SRC.lower()), call, world.clock, world.sleep)
    assert (result.mcs, result.status) == (True, "trusted") and world.now == 1000.0
    assert run.commands[-1] == ["insmod", ORIGINAL]
    result = module.prepare(_trusted("0123456789"), Runner(), world.clock, world.sleep)
    assert (result.mcs, result.status) == (False, "untrusted")
