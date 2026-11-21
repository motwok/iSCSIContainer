import io
import json
import os
import signal
import threading
import types

import pytest
import yaml

from iscsicontainer import apply, chap, cli, health, lio as lio_module, log, metrics, module, net
from iscsicontainer.module import ModuleError, Result

from .conftest import I1, T1, FakeLio, Runner, completed, make

PW = "secret-12345"


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Everything cli touches, replaced by fakes below a temporary directory."""
    fake = FakeLio()
    run = Runner()
    box = types.SimpleNamespace(lio=fake, run=run, tmp=tmp_path, result=Result(True, "patched"),
                                served=[], mounted=True)
    monkeypatch.setenv("ISCSI_CONFIG", yaml.safe_dump(make()))
    monkeypatch.delenv("ISCSI_CONFIG_FILE", raising=False)
    monkeypatch.setattr(cli, "Lio", lambda: fake)
    monkeypatch.setattr(cli.subprocess, "run", run)
    monkeypatch.setattr(cli, "CONFIGFS", str(tmp_path / "config"))
    monkeypatch.setattr(os.path, "ismount", lambda path: box.mounted)
    monkeypatch.setattr(apply, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(apply, "check_identities", lambda cfg: None)
    monkeypatch.setattr(apply, "identity", lambda lun: ("x", lun.path))
    monkeypatch.setattr(apply, "hidden_cache", lambda path: False)
    monkeypatch.setattr(net, "interface_addresses", lambda name: ["192.0.2.7"])
    monkeypatch.setattr(apply.net, "interface_addresses", lambda name: ["192.0.2.7"])
    monkeypatch.setattr(chap, "ROOT", str(tmp_path / "chap"))
    monkeypatch.setattr(health, "CONFIGFS", str(tmp_path / "iscsi"))
    monkeypatch.setattr(module, "prepare", lambda cfg: box.result)
    monkeypatch.setattr(metrics, "serve", lambda *args: box.served.append(args))
    monkeypatch.setattr(signal, "signal", lambda number, handler: None)
    return box


def _stopped():
    stop = threading.Event()
    stop.set()
    return stop


def test_run(world, logs):
    assert cli.run(_stopped()) == 0
    assert world.lio.cleared == 2 and world.served[0][1:] == (None, 9482)
    assert [c for c in world.run.commands if c[0] == "modprobe"] == [
        ["modprobe", name] for name in cli.CORE_MODULES]
    assert not os.path.exists(apply.STATE)
    out = logs()
    assert "running, 2 target(s)" in out and "stopping" in out and "MC/S IS IGNORED" not in out
    assert "is not enabled in the kernel" in out


def test_run_waits_for_the_signal(world, monkeypatch):
    handlers = {}
    monkeypatch.setattr(signal, "signal", lambda number, handler: handlers.update({number: handler}))
    stop = threading.Event()
    seen = []
    real_wait = stop.wait

    def wait(timeout=None):
        seen.append(json.load(open(apply.STATE))["mcs_active"])
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        return real_wait(0)
    stop.wait = wait
    assert cli.run(stop) == 0 and seen == [True] and set(handlers) == {signal.SIGTERM,
                                                                       signal.SIGINT}


def test_run_without_the_patch(world, logs):
    world.result = Result(False, "build-failed")
    assert cli.run(_stopped()) == 0
    out = logs()
    assert "MC/S IS IGNORED" in out and "module status build-failed" in out


def test_run_mounts_configfs(world):
    world.mounted = False
    assert cli.run(_stopped()) == 0
    assert ["mount", "-t", "configfs", "configfs", cli.CONFIGFS] in world.run.commands


@pytest.mark.parametrize("answers,text", [
    ({"modprobe target_core_file": completed(1, stderr="not found")},
     "modprobe target_core_file: not found"),
    ({"mount": completed(1, stderr="denied")}, "configfs cannot be mounted: denied"),
])
def test_run_kernel_errors(world, logs, answers, text):
    world.mounted = False
    world.run.answers = answers
    assert cli.run(_stopped()) == 1 and text in logs() and world.lio.cleared == 0


def test_run_module_error(world, monkeypatch, logs):
    def fail(cfg):
        raise ModuleError("iscsi_target_mod cannot be loaded")
    monkeypatch.setattr(module, "prepare", fail)
    assert cli.run(_stopped()) == 1 and "cannot be loaded" in logs()


def test_run_configuration_error(world, monkeypatch, logs):
    monkeypatch.setenv("ISCSI_CONFIG", "targets: []")
    assert cli.run(_stopped()) == 2 and world.lio.cleared == 0
    os.makedirs(os.path.join(cli.CONFIGFS, "target"))
    assert cli.run(_stopped()) == 2 and world.lio.cleared == 1
    world.lio.fail[("clear", None)] = "busy"
    assert cli.run(_stopped()) == 2
    out = logs()
    assert "configuration error" in out and "could not be cleared: busy" in out


def test_health(world, capsys):
    assert cli.main(["health"]) == 1
    assert "has not finished starting" in capsys.readouterr().out
    apply.save_state({"mcs_requested": False, "mcs_active": False, "module_status": "x",
                      "targets": []})
    assert cli.main(["health"]) == 0 and capsys.readouterr().out == ""


def test_version(capsys):
    assert cli.main(["version"]) == 0 and capsys.readouterr().out.strip() == cli.__version__


def test_main_run(world, monkeypatch):
    monkeypatch.setattr(cli, "run", lambda: 7)
    assert cli.main(["run"]) == 7


def test_entry(monkeypatch):
    monkeypatch.setattr(cli, "main", lambda: 3)
    with pytest.raises(SystemExit) as stop:
        cli.entry()
    assert stop.value.code == 3


def test_chap_commands(world, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(PW + "\n"))
    assert cli.main(["chap", "set", T1, I1, "user"]) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO("target-123456\n"))
    assert cli.main(["chap", "set-mutual", T1, I1, "target"]) == 0
    capsys.readouterr()
    assert cli.main(["chap", "list"]) == 0
    assert capsys.readouterr().out.strip() == f"{T1} {I1} user=user mutual_user=target"
    assert cli.main(["chap", "remove-mutual", T1, I1]) == 0
    assert cli.main(["chap", "remove", T1, I1]) == 0
    assert cli.main(["chap", "remove", T1, I1]) == 1
    assert "no CHAP is set" in capsys.readouterr().err


def test_chap_with_a_broken_configuration(world, monkeypatch, capsys):
    monkeypatch.setenv("ISCSI_CONFIG", "targets: []")
    monkeypatch.setattr("sys.stdin", io.StringIO(PW + "\n"))
    assert cli.main(["chap", "set", T1, I1, "user"]) == 0
    err = capsys.readouterr().err
    assert "the configuration cannot be used" in err and "takes effect once it is" in err


def test_small_modules(capsys):
    log.info("a")
    log.warning("b")
    log.error("c")
    log.banner(["d"])
    lines = capsys.readouterr().err.splitlines()
    assert lines[:3] == ["INFO a", "WARNING b", "ERROR c"]
    assert lines[4] == "WARNING d" and lines[3] == lines[5] == "WARNING " + "!" * 72


def test_interface_addresses():
    data = [{"addr_info": [
        {"family": "inet", "local": "192.0.2.7", "scope": "global"},
        {"family": "inet6", "local": "fd00::7", "scope": "global"},
        {"family": "inet6", "local": "fd00::8", "scope": "global", "temporary": True},
        {"family": "inet6", "local": "fd00::9", "scope": "global", "tentative": True},
        {"family": "inet6", "local": "fd00::a", "scope": "global", "deprecated": True},
        {"family": "inet6", "local": "fe80::7", "scope": "link"},
        {"family": "mpls", "local": "x"}]}]
    run = Runner({"ip": completed(stdout=json.dumps(data))})
    assert net.interface_addresses("eth7", run) == ["192.0.2.7", "fd00::7", "fe80::7%eth7"]
    assert run.commands == [["ip", "-json", "addr", "show", "dev", "eth7"]]
    assert net.interface_addresses("eth7", Runner({"ip": completed(stdout="")})) == []
    assert net.interface_addresses("none", Runner({"ip": completed(1)})) is None


class Rts:
    """Records what Lio asks rtslib to do."""

    class RTSLibError(Exception):
        pass

    def __init__(self):
        self.calls = []
        self.missing = False
        for name in ("RTSRoot", "FileIOStorageObject", "BlockStorageObject", "FabricModule",
                     "Target", "TPG", "NetworkPortal", "LUN", "NodeACL", "MappedLUN"):
            setattr(self, name, self._make(name))

    def _make(self, name):
        def create(*args, **kwargs):
            if self.missing and kwargs.get("mode") == "lookup":
                raise self.RTSLibError("not found")
            self.calls.append((name, args, kwargs))
            return types.SimpleNamespace(
                name=name, args=args,
                clear_existing=lambda confirm: self.calls.append(("clear", confirm)),
                delete=lambda: self.calls.append(("delete", name)),
                set_attribute=lambda k, v: self.calls.append(("attribute", k, v)),
                set_parameter=lambda k, v: self.calls.append(("parameter", k, v)),
                parent_target=types.SimpleNamespace(
                    delete=lambda: self.calls.append(("delete", "target"))))
        return create


def test_lio(tmp_path):
    rts = Rts()
    lio = lio_module.Lio(rts)
    image = tmp_path / "a.img"
    image.write_bytes(b"x" * 4096)
    lio.clear()
    storage = lio.fileio("data", str(image), True, "serial")
    lio.block("disk", "/dev/disk", "serial2")
    lio.set_attribute(storage, "a", "1")
    tpg = lio.target(T1)
    lio.set_parameter(tpg, "p", "2")
    lio.portal(tpg, "192.0.2.1", 3260)
    lio.portal(tpg, "fd00::1", 3260)
    lun = lio.lun(tpg, 0, storage)
    acl = lio.acl(tpg, I1)
    lio.map(acl, 0, lun)
    lio.set_chap(acl, "user", PW, None, None)
    lio.enable(tpg)
    lio.delete(storage)
    lio.delete_target(tpg)
    assert lio.find_target(T1) is not None and lio.find_acl(tpg, I1) is not None
    rts.missing = True
    assert lio.find_target(T1) is None and lio.find_acl(tpg, I1) is None
    assert lio.Error is Rts.RTSLibError
    assert ("clear", True) in rts.calls and ("delete", "target") in rts.calls
    assert ("attribute", "a", "1") in rts.calls and ("parameter", "p", "2") in rts.calls
    created = {c[0]: c for c in rts.calls if len(c) == 3 and isinstance(c[2], dict)}
    assert created["FileIOStorageObject"][2] == {"dev": str(image), "size": 4096,
                                                "write_back": True, "wwn": "serial"}
    assert created["BlockStorageObject"][2] == {"dev": "/dev/disk", "wwn": "serial2"}
    portals = [c[1][1] for c in rts.calls if c[0] == "NetworkPortal"]
    assert portals == ["192.0.2.1", "[fd00::1]"]
    assert tpg.enable is True
    assert (acl.chap_userid, acl.chap_password, acl.chap_mutual_userid) == ("user", PW, "")


def test_lio_imports_rtslib(monkeypatch):
    fake = types.ModuleType("rtslib_fb")
    fake.RTSLibError = Rts.RTSLibError
    monkeypatch.setitem(__import__("sys").modules, "rtslib_fb", fake)
    assert lio_module.Lio().r is fake
