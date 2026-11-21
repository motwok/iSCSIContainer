import socket
import urllib.error
import urllib.request

import pytest

from iscsicontainer import health, metrics

from .conftest import I1, T1, T2

INFO = """InitiatorName: x
LIO Session ID: 13   ISID: 0x40
----------------------[iSCSI Connections]-------------------------
CID: 1  Connection State: TARG_CONN_STATE_LOGGED_IN
CID: 2  Connection State: TARG_CONN_STATE_LOGGED_IN
"""


def state():
    return {
        "mcs_requested": True, "mcs_active": True, "module_status": "patched",
        "targets": [
            {"iqn": T1, "ok": True, "error": None, "initiators": [I1, "iqn.2026-11.test:off"],
             "interfaces": [{"name": "eth7", "ok": True}],
             "portals": [{"address": "192.0.2.10", "port": 3260, "ok": True},
                         {"address": "fe80::7%eth7", "port": 3260, "ok": True}],
             "luns": [{"lun": 0, "name": "data", "ok": True, "error": None}]},
        ],
    }


@pytest.fixture
def configfs(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "CONFIGFS", str(tmp_path))
    tpg = tmp_path / T1 / "tpgt_1"
    for name in ("192.0.2.10:3260", "[fe80::7%eth7]:3260"):
        (tpg / "np" / name).mkdir(parents=True)
    (tpg / "enable").write_text("1\n")
    stats = tpg / "acls" / I1 / "lun_0" / "statistics" / "scsi_auth_intr"
    stats.mkdir(parents=True)
    (tpg / "acls" / I1 / "info").write_text(INFO)
    (stats / "num_cmds").write_text("42\n")
    (stats / "read_mbytes").write_text("3\n")
    (stats / "write_mbytes").write_text("broken\n")
    logins = tmp_path / T1 / "fabric_statistics" / "iscsi_login_stats"
    logins.mkdir(parents=True)
    (logins / "authenticate_fails").write_text("5\n")
    return tmp_path


def test_portal_name():
    assert health.portal_name("192.0.2.1", 3260) == "192.0.2.1:3260"
    assert health.portal_name("fd00::1", 3261) == "[fd00::1]:3261"


def test_healthy(configfs):
    assert health.problems(state()) == []


def test_not_started():
    assert health.problems(None) == ["the target has not finished starting"]


def test_problems(configfs):
    bad = state()
    bad["mcs_active"] = False
    bad["module_status"] = "build-failed"
    target = bad["targets"][0]
    target["luns"][0].update(ok=False, error="missing")
    target["interfaces"][0]["ok"] = False
    target["portals"][0]["ok"] = False
    (configfs / T1 / "tpgt_1" / "np" / "[fe80::7%eth7]:3260").rmdir()
    (configfs / T1 / "tpgt_1" / "enable").write_text("0\n")
    bad["targets"].append({"iqn": T2, "ok": False, "error": "rejected"})
    bad["targets"].append({"iqn": "iqn.2026-11.test:three", "ok": True, "error": None,
                           "luns": [], "interfaces": [], "portals": [], "initiators": []})
    assert health.problems(bad) == [
        "MC/S is configured but ignored, module status build-failed",
        f"{T1} is not enabled in the kernel",
        f"{T1} LUN 0 (data): missing",
        f"{T1}: interface eth7 yields no portal",
        f"{T1}: no portal on 192.0.2.10:3260",
        f"{T1}: portal [fe80::7%eth7]:3260 is not in the kernel",
        f"{T2} is not exported: rejected",
        "iqn.2026-11.test:three is not enabled in the kernel",
        "iqn.2026-11.test:three has no portal",
    ]


def test_render(configfs):
    current = state()
    current["targets"].append({"iqn": T2, "ok": False, "error": "x", "initiators": [I1],
                               "interfaces": [], "portals": [
                                   {"address": "192.0.2.7", "port": 3261, "ok": True}],
                               "luns": [{"lun": 0, "name": 'b"k', "ok": True, "error": None}]})
    text = metrics.render(current)
    for line in (
            "iscsi_mcs_active 1",
            'iscsi_module_info{status="patched"} 1',
            f'iscsi_target_up{{target="{T1}"}} 1',
            f'iscsi_target_up{{target="{T2}"}} 0',
            f'iscsi_lun_up{{target="{T1}",lun="0",name="data"}} 1',
            f'iscsi_lun_up{{target="{T2}",lun="0",name="b\\"k"}} 0',
            f'iscsi_portal_up{{target="{T1}",portal="[fe80::7%eth7]:3260"}} 1',
            f'iscsi_portal_up{{target="{T2}",portal="192.0.2.7:3261"}} 0',
            f'iscsi_sessions{{target="{T1}",initiator="{I1}"}} 1',
            f'iscsi_connections{{target="{T1}",initiator="{I1}"}} 2',
            f'iscsi_lun_commands_total{{target="{T1}",initiator="{I1}",lun="0",name="data"}} 42',
            f'iscsi_lun_read_bytes_total{{target="{T1}",initiator="{I1}",lun="0",name="data"}} '
            "3145728",
            f'iscsi_login_failures_total{{target="{T1}",reason="authenticate"}} 5',
            "# TYPE iscsi_lun_commands_total counter",
            "# TYPE iscsi_target_up gauge"):
        assert line in text.splitlines()
    assert "written_bytes" not in text and "iqn.2026-11.test:off" not in text


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _get(port, path="/metrics"):
    return urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5)


@pytest.mark.parametrize("address", [None, "127.0.0.1"])
def test_serve(configfs, address):
    port = _free_port()
    server = metrics.serve(state(), address, port)
    try:
        response = _get(port, "/metrics?x=1")
        assert response.status == 200 and b"iscsi_target_up" in response.read()
        assert response.headers["Content-Type"].startswith("text/plain; version=0.0.4")
        with pytest.raises(urllib.error.HTTPError) as error:
            _get(port, "/other")
        assert error.value.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_serve_ipv6_address(configfs):
    port = _free_port()
    try:
        server = metrics.serve(state(), "::1", port)
    except OSError:
        pytest.skip("no IPv6 loopback")
    try:
        assert urllib.request.urlopen(f"http://[::1]:{port}/metrics", timeout=5).status == 200
    finally:
        server.shutdown()
        server.server_close()


def test_serve_falls_back_to_ipv4(configfs, monkeypatch):
    real = metrics._server
    calls = []

    def flaky(host, port, family, dual):
        calls.append(host)
        if host == "::":
            raise OSError("no IPv6")
        return real(host, port, family, dual)
    monkeypatch.setattr(metrics, "_server", flaky)
    port = _free_port()
    server = metrics.serve(state(), None, port)
    try:
        assert calls == ["::", "0.0.0.0"] and _get(port).status == 200
    finally:
        server.shutdown()
        server.server_close()
