import os
import types

import pytest

from iscsicontainer import apply, config
from iscsicontainer.chap import Cred, Store
from iscsicontainer.config import ConfigError
from iscsicontainer.module import Result

from .conftest import I1, I2, T1, T2, parse

PW = "secret-12345"
BLOCK = 0o060660
FILE = 0o100644


class Files:
    """Fake os.stat: path -> (mode, device number or (st_dev, st_ino))."""

    def __init__(self, monkeypatch):
        self.known = {"/luns/data.img": (FILE, (1, 10)), "/dev/disk": (BLOCK, 0x801),
                      "/luns/backup.img": (FILE, (1, 11))}
        self.real = os.stat
        monkeypatch.setattr(os, "stat", self.stat)
        monkeypatch.setattr(apply, "hidden_cache", lambda path: False)

    def stat(self, path, *args, **kwargs):
        if path not in self.known:
            if str(path).startswith(("/luns/", "/dev/")):
                raise FileNotFoundError(path)
            return self.real(path, *args, **kwargs)
        mode, ident = self.known[path]
        if mode == BLOCK:
            return types.SimpleNamespace(st_mode=mode, st_rdev=ident, st_dev=0, st_ino=0)
        return types.SimpleNamespace(st_mode=mode, st_rdev=0, st_dev=ident[0], st_ino=ident[1])


@pytest.fixture
def files(monkeypatch):
    return Files(monkeypatch)


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "chap"))


def addresses(name):
    return {"eth7": ["192.0.2.7", "fe80::7%eth7"]}.get(name)


def build(lio, store, cfg=None, mcs=True, status="patched"):
    return apply.build(cfg or parse(), lio, store, Result(mcs, status), addresses)


def test_serial_is_stable():
    assert apply.serial("data") == apply.serial("data") != apply.serial("other")
    assert len(apply.serial("data")) == 36


def test_identity(files):
    data, disk = parse().targets[0].luns
    assert apply.identity(data) == ("file", 1, 10) and apply.identity(disk) == ("device", 0x801)
    files.known["/luns/data.img"] = (BLOCK, 0x801)
    files.known["/dev/disk"] = (FILE, (1, 12))
    assert apply.identity(data) is None and apply.identity(disk) is None
    del files.known["/dev/disk"]
    assert apply.identity(disk) is None


def test_check_identities(files):
    apply.check_identities(parse())
    files.known["/luns/backup.img"] = (FILE, (1, 10))
    with pytest.raises(ConfigError, match="are the same file"):
        apply.check_identities(parse())
    del files.known["/luns/backup.img"]
    apply.check_identities(parse())


def test_build_everything(files, lio, store, logs):
    store.write(T1, I1, Cred("user", PW))
    store.write(T2, I2, Cred("orphan", PW))
    state = build(lio, store)
    assert state["mcs_requested"] and state["mcs_active"] and state["module_status"] == "patched"
    one, two = state["targets"]
    assert one["ok"] and two["ok"] and one["initiators"] == [I1, I2]
    tpg = lio.targets[T1]
    assert tpg.enabled and tpg.portals == [("192.0.2.10", 3260), ("2001:db8::10", 3260)]
    assert tpg.attributes == {"generate_node_acls": "0", "authentication": "1",
                              "default_cmdsn_depth": "128", "demo_mode_discovery": "1"}
    assert tpg.parameters == {"MaxConnections": "4", "InitialR2T": "No",
                              "MaxBurstLength": "1048576"}
    data, disk = lio.storages["data"], lio.storages["disk"]
    assert (data.kind, data.write_back, data.serial) == ("fileio", True, apply.serial("data"))
    assert data.attributes == {"emulate_write_cache": "1"} and disk.kind == "block"
    assert sorted(tpg.luns) == [0, 1] and tpg.acls[I1].mapped == [0, 1]
    assert tpg.acls[I1].chap[:2] == ("user", PW) and tpg.acls[I2].chap[0] is None
    other = lio.targets[T2]
    assert other.portals == [("192.0.2.7", 3261), ("fe80::7%eth7", 3261)]
    assert other.parameters == {"MaxConnections": "1"}
    assert two["interfaces"] == [{"name": "eth7", "ok": True}]
    assert [p["ok"] for p in one["portals"]] == [True, True]
    out = logs()
    assert "several initiators" in out and f"{T2} {I2} are unused" in out
    assert f"{I2} has no CHAP password" in out


def test_open_target(files, lio, store, logs):
    store.write(T1, I1, Cred("user", PW))
    cfg = parse(lambda raw: raw["targets"][0].update(acl="any"))
    one = build(lio, store, cfg)["targets"][0]
    assert one["ok"] and one["open"] and one["initiators"] == []
    tpg = lio.targets[T1]
    assert tpg.acls == {}
    assert {k: tpg.attributes[k] for k in ("generate_node_acls", "authentication",
                                           "cache_dynamic_acls", "demo_mode_write_protect")}         == {"generate_node_acls": "1", "authentication": "0", "cache_dynamic_acls": "1",
            "demo_mode_write_protect": "0"}
    out = logs()
    assert "acl any, every initiator may log in" in out and f"{T1} {I1} are unused" in out
    assert "only the initiator name" not in out.split(T2)[0]


def test_mcs_ignored(files, lio, store):
    state = build(lio, store, mcs=False, status="build-failed")
    assert not state["mcs_active"] and lio.targets[T1].parameters["MaxConnections"] == "1"


def test_missing_and_wrong_backing(files, lio, store, logs):
    del files.known["/luns/data.img"]
    files.known["/dev/disk"] = (FILE, (1, 12))
    state = build(lio, store)
    luns = state["targets"][0]["luns"]
    assert [lun["ok"] for lun in luns] == [False, False]
    assert "regular file" in luns[0]["error"] and "block device" in luns[1]["error"]
    assert state["targets"][0]["ok"] and lio.targets[T1].luns == {}
    assert "is not exported" in logs()


def test_backstore_and_attribute_errors(files, lio, store):
    lio.fail[("block", "disk")] = "no such device"
    lio.fail[("set_attribute", "emulate_write_cache")] = "invalid"
    state = build(lio, store)
    luns = state["targets"][0]["luns"]
    assert luns[0]["error"] == "attribute emulate_write_cache=1: invalid"
    assert luns[1]["error"] == "no such device"
    assert "data" not in lio.storages and state["targets"][0]["ok"]


def test_cleanup_error_is_only_logged(files, lio, store, logs):
    lio.fail[("set_attribute", "emulate_write_cache")] = "invalid"
    lio.fail[("delete", "data")] = "busy"
    build(lio, store)
    assert "cleanup: busy" in logs()


def test_hidden_cache_warning(files, lio, store, logs, monkeypatch):
    monkeypatch.setattr(apply, "hidden_cache", lambda path: True)
    build(lio, store)
    assert "/dev/disk reports no write cache" in logs()


@pytest.mark.parametrize("fail,message", [
    (("set_parameter", "InitialR2T"), "parameter InitialR2T=No: nope"),
    (("set_attribute", "default_cmdsn_depth"), "attribute default_cmdsn_depth=128: nope"),
    (("target", T1), "nope"),
    (("enable", T1), "nope"),
])
def test_target_fails_alone(files, lio, store, fail, message, logs):
    lio.fail[fail] = "nope"
    one, two = build(lio, store)["targets"]
    assert not one["ok"] and one["error"] == message and two["ok"]
    assert T1 not in lio.targets and "data" not in lio.storages and "backup" in lio.storages
    assert f"{T1} is not exported" in logs()


def test_portal_errors(files, lio, store, logs):
    lio.fail[("portal", "2001:db8::10")] = "cannot bind"
    lio.fail[("portal", "fe80::7%eth7")] = "no zone"
    one, two = build(lio, store)["targets"]
    assert [p["ok"] for p in one["portals"]] == [True, False] and one["ok"]
    assert two["portals"] == [{"address": "192.0.2.7", "port": 3261, "ok": True}]
    assert two["interfaces"][0]["ok"]
    out = logs()
    assert "no portal on 2001:db8::10" in out and "link-local address fe80::7%eth7 skipped" in out


def test_interface_without_portal(files, lio, store, logs):
    cfg = parse(lambda raw: raw["targets"][1]["portal"].update(interfaces=["eth7", "eth8"]))
    lio.fail[("portal", "192.0.2.7")] = "cannot bind"
    lio.fail[("portal", "fe80::7%eth7")] = "no zone"
    two = build(lio, store, cfg)["targets"][1]
    assert two["interfaces"] == [{"name": "eth7", "ok": False}, {"name": "eth8", "ok": False}]
    out = logs()
    assert "interface eth7 is missing or yields no portal" in out and "interface eth8" in out


def test_state_file(tmp_path, monkeypatch):
    monkeypatch.setattr(apply, "STATE", str(tmp_path / "run" / "state.json"))
    assert apply.load_state() is None
    apply.remove_state()
    apply.save_state({"a": 1})
    assert apply.load_state() == {"a": 1}
    apply.remove_state()
    assert apply.load_state() is None


def _sysfs(tmp_path, top, below):
    """/sys/dev/block with one dm device over the given disks."""
    block = tmp_path / "dev" / "block"
    block.mkdir(parents=True)
    devices = tmp_path / "devices"
    dm = devices / "dm-0"
    (dm / "queue").mkdir(parents=True)
    (dm / "queue" / "write_cache").write_text(top + "\n")
    (dm / "slaves").mkdir()
    for index, mode in enumerate(below):
        disk = devices / f"sd{index}"
        (disk / "queue").mkdir(parents=True)
        (disk / "queue" / "write_cache").write_text(mode + "\n")
        part = disk / f"sd{index}1"
        part.mkdir()
        os.symlink(part, dm / "slaves" / f"sd{index}1")
    os.symlink(dm, dm / "slaves" / "loop")
    os.symlink(dm, block / "8:1")
    return str(block)


@pytest.mark.parametrize("top,below,expected", [
    ("write through", ["write through", "write back"], True),
    ("write through", ["write through"], False),
    ("write back", ["write back"], False),
])
def test_hidden_cache(tmp_path, monkeypatch, top, below, expected):
    monkeypatch.setattr(apply, "SYS_BLOCK", _sysfs(tmp_path, top, below))
    monkeypatch.setattr(os, "stat", lambda path: types.SimpleNamespace(st_rdev=0x801))
    assert apply.hidden_cache("/dev/disk") is expected


def test_hidden_cache_unknown_device(tmp_path, monkeypatch):
    monkeypatch.setattr(apply, "SYS_BLOCK", str(tmp_path))
    monkeypatch.setattr(os, "stat", lambda path: types.SimpleNamespace(st_rdev=0x801))
    assert apply.hidden_cache("/dev/disk") is False


def test_config_error_is_shared():
    assert apply.ConfigError is config.ConfigError
