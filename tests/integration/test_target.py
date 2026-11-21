"""Runs iscsictl against the real kernel target and logs in with open-iscsi.

Needs a privileged container in the host network with /lib/modules, /sys/kernel/config and /dev
of the host; started only with ISCSI_INTEGRATION=1.
"""
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import time
import urllib.request

import pytest
import yaml

from iscsicontainer import apply

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("ISCSI_INTEGRATION") != "1",
                       reason="set ISCSI_INTEGRATION=1 in a privileged container"),
]

TARGET = "iqn.2026-11.test.integration:target"
INITIATOR = "iqn.2026-11.test.integration:init"
PORTAL = "127.0.0.1:3260"
CONFIGFS = "/sys/kernel/config/target"
TPG = f"{CONFIGFS}/iscsi/{TARGET}/tpgt_1"
STATE = "/run/iscsicontainer/state.json"
PASSWORD = "integration-1234"
MUTUAL = "mutual-integ-5678"


def sh(*args, check=True, stdin=None):
    result = subprocess.run(args, capture_output=True, text=True, input=stdin)
    if check and result.returncode != 0:
        raise AssertionError(f"{args}: {result.stdout}{result.stderr}")
    return result


# The initiator of the Docker host (ISCSI_HOST_INITIATOR=1, container with --pid host) or one
# started in this container. Inside a container iscsid does not find the kernel's transport
# on every host.
HOST = []
if os.environ.get("ISCSI_HOST_INITIATOR") == "1":
    HOST = ["nsenter", "-t", "1", "-m", "-n", "-i", "-u", "--"]


def adm(*args, check=True):
    return sh(*HOST, "iscsiadm", *args, check=check)


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read().strip()


@pytest.fixture(scope="module", autouse=True)
def initiator():
    if HOST:
        yield
        adm("-m", "node", "--logoutall=all", check=False)
        return
    sh("modprobe", "iscsi_tcp")
    os.makedirs("/etc/iscsi", exist_ok=True)
    with open("/etc/iscsi/initiatorname.iscsi", "w", encoding="ascii") as handle:
        handle.write(f"InitiatorName={INITIATOR}\n")
    daemon = subprocess.Popen(["iscsid", "-f"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
    time.sleep(1)
    yield
    adm("-m", "node", "--logoutall=all", check=False)
    daemon.terminate()
    daemon.wait(10)


@pytest.fixture(autouse=True)
def clean():
    shutil.rmtree("/config/chap", ignore_errors=True)
    adm("-m", "node", "-o", "delete", check=False)
    adm("-m", "discoverydb", "-t", "sendtargets", "-p", PORTAL, "-o", "delete", check=False)
    yield


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "lun.img"
    with open(path, "wb") as handle:
        handle.truncate(64 * 1024 * 1024)
    return str(path)


def one_target(luns, **extra):
    target = {"iqn": TARGET, "portal": {"addresses": ["127.0.0.1"]}, "luns": luns,
              "acl": [{"initiator": INITIATOR}]}
    target.update(extra)
    return {"targets": [target]}


class Running:
    def __init__(self, cfg, log):
        if os.path.exists(STATE):
            os.remove(STATE)
        self.log = log
        # As in the container: every later iscsictl call sees the same configuration.
        os.environ["ISCSI_CONFIG"] = yaml.safe_dump(cfg)
        os.environ.pop("ISCSI_CONFIG_FILE", None)
        with open(log, "w", encoding="utf-8") as handle:
            self.process = subprocess.Popen(["iscsictl", "run"], stderr=handle)
        deadline = time.time() + 120
        while not os.path.exists(STATE) and self.process.poll() is None:
            assert time.time() < deadline, self.output()
            time.sleep(0.2)

    def output(self):
        return read(self.log)

    def stop(self):
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
        return self.process.wait(60)


@pytest.fixture
def start(tmp_path):
    started = []

    def run(cfg):
        started.append(Running(cfg, str(tmp_path / f"target{len(started)}.log")))
        return started[-1]
    yield run
    adm("-m", "node", "--logoutall=all", check=False)
    for target in started:
        target.stop()


def health():
    return sh("iscsictl", "health", check=False)


def metrics():
    with urllib.request.urlopen("http://127.0.0.1:9482/metrics", timeout=10) as response:
        return response.read().decode()


def node(*args, check=True):
    return adm("-m", "node", "-T", TARGET, "-p", PORTAL, *args, check=check)


def discover():
    result = adm("-m", "discovery", "-t", "sendtargets", "-p", PORTAL, check=False)
    if "connect to iscsid" in result.stderr:
        pytest.skip("the initiator cannot be used in this network namespace")
    assert result.returncode == 0 and TARGET in result.stdout, result.stdout + result.stderr
    return result.stdout


def disks():
    out = adm("-m", "session", "-P", "3").stdout
    return sorted("/dev/" + name for name in re.findall(r"Attached scsi disk (\S+)", out))


def diagnostics():
    found = None
    if os.path.isdir("/sys/class/iscsi_transport"):
        found = os.listdir("/sys/class/iscsi_transport")
    return "\n".join([f"transports {found}", sh("lsmod", check=False).stdout,
                      sh("sh", "-c", "dmesg | tail -15", check=False).stdout])


def usable(disk):
    # The node can exist before the disk answers (or be left over from the session before).
    try:
        os.close(os.open(disk, os.O_RDONLY))
    except OSError:
        return False
    return True


def login(count=1):
    result = node("--login", check=False)
    if "could not communicate to iscsid" in result.stderr:
        # The iSCSI netlink family only exists in the initial network namespace.
        pytest.skip("the initiator cannot be used in this network namespace")
    assert result.returncode == 0, result.stdout + result.stderr + diagnostics()
    deadline = time.time() + 30
    while True:
        found = [d for d in disks() if usable(d)]
        if len(found) == count:
            return found
        assert time.time() < deadline, f"disks {found}"
        time.sleep(0.5)


def write_and_read_back(disk, tmp_path):
    data = os.urandom(1024 * 1024)
    source = tmp_path / "random"
    source.write_bytes(data)
    sh("dd", f"if={source}", f"of={disk}", "bs=1M", "count=1", "oflag=direct")
    back = tmp_path / "back"
    sh("dd", f"if={disk}", f"of={back}", "bs=1M", "count=1", "iflag=direct")
    assert back.read_bytes() == data
    return data


def test_file_lun_login_io_metrics_and_teardown(start, image, tmp_path):
    cfg = one_target([{"lun": 0, "name": "it-file", "file": image}])
    cfg["targets"][0]["portal"]["addresses"].append("::1")
    target = start(cfg)
    assert health().returncode == 0, health().stdout + target.output()
    assert sorted(os.listdir(f"{TPG}/np")) == ["127.0.0.1:3260", "[::1]:3260"]
    assert read(f"{TPG}/param/MaxConnections") == "1"
    assert read(f"{TPG}/attrib/authentication") == "0"
    assert read(f"{TPG}/attrib/generate_node_acls") == "0"
    serial = read(f"{CONFIGFS}/core/fileio_0/it-file/wwn/vpd_unit_serial")
    assert serial.endswith(apply.serial("it-file"))
    discover()
    (disk,) = login()
    data = write_and_read_back(disk, tmp_path)
    with open(image, "rb") as handle:
        assert handle.read(len(data)) == data
    text = metrics()
    assert f'iscsi_target_up{{target="{TARGET}"}} 1' in text
    assert f'iscsi_sessions{{target="{TARGET}",initiator="{INITIATOR}"}} 1' in text
    assert f'iscsi_connections{{target="{TARGET}",initiator="{INITIATOR}"}} 1' in text
    assert re.search(r"iscsi_lun_commands_total\{[^}]*\} [1-9]", text)
    assert re.search(r"iscsi_lun_written_bytes_total\{[^}]*\} [1-9]", text)
    assert "iscsi_login_failures_total" in text
    node("--logout")
    assert target.stop() == 0
    assert not os.path.exists(f"{CONFIGFS}/iscsi/{TARGET}")
    assert not os.path.exists(STATE) and health().returncode == 1


def test_block_device_lun(start, image, tmp_path):
    loop = sh("losetup", "--find", "--show", image).stdout.strip()
    try:
        start(one_target([{"lun": 3, "name": "it-block", "device": loop}]))
        assert health().returncode == 0, health().stdout
        discover()
        (disk,) = login()
        data = write_and_read_back(disk, tmp_path)
        node("--logout")
        sh("sync")
        with open(image, "rb") as handle:
            assert handle.read(len(data)) == data
    finally:
        adm("-m", "node", "--logoutall=all", check=False)
        time.sleep(1)
        sh("losetup", "-d", loop, check=False)


def test_chap_and_mutual_chap(start, image):
    target = start(one_target([{"lun": 0, "name": "it-chap", "file": image}]))
    discover()
    sh("iscsictl", "chap", "set", TARGET, INITIATOR, "user", stdin=PASSWORD + "\n")
    assert read(f"{TPG}/attrib/authentication") == "1"
    refused = node("--login", check=False)
    if "could not communicate to iscsid" in refused.stderr:
        pytest.skip("the initiator cannot be used in this network namespace")
    assert refused.returncode != 0
    node("-o", "update", "-n", "node.session.auth.authmethod", "-v", "CHAP")
    node("-o", "update", "-n", "node.session.auth.username", "-v", "user")
    node("-o", "update", "-n", "node.session.auth.password", "-v", "wrong-password-1")
    assert node("--login", check=False).returncode != 0
    node("-o", "update", "-n", "node.session.auth.password", "-v", PASSWORD)
    login()
    node("--logout")
    sh("iscsictl", "chap", "set-mutual", TARGET, INITIATOR, "target", stdin=MUTUAL + "\n")
    node("-o", "update", "-n", "node.session.auth.username_in", "-v", "target")
    node("-o", "update", "-n", "node.session.auth.password_in", "-v", "wrong-mutual-123")
    assert node("--login", check=False).returncode != 0
    node("-o", "update", "-n", "node.session.auth.password_in", "-v", MUTUAL)
    login()
    node("--logout")
    listing = sh("iscsictl", "chap", "list").stdout
    assert "user=user mutual_user=target" in listing and PASSWORD not in listing
    assert re.search(r'iscsi_login_failures_total\{[^}]*reason="authenticate"\} [1-9]', metrics())
    sh("iscsictl", "chap", "remove", TARGET, INITIATOR)
    assert read(f"{TPG}/attrib/authentication") == "0"
    assert PASSWORD not in target.output()


def test_chap_survives_a_restart(start, image):
    cfg = one_target([{"lun": 0, "name": "it-chap2", "file": image}])
    first = start(cfg)
    sh("iscsictl", "chap", "set", TARGET, INITIATOR, "user", stdin=PASSWORD + "\n")
    assert first.stop() == 0
    start(cfg)
    assert read(f"{TPG}/attrib/authentication") == "1"
    acl = f"{TPG}/acls/{INITIATOR}/auth"
    assert read(f"{acl}/userid") == "user" and read(f"{acl}/password") == PASSWORD


def test_mcs_only_with_a_trusted_module(start, image):
    cfg = one_target([{"lun": 0, "name": "it-mcs", "file": image}], max_connections=4)
    cfg["module"] = {"trusted_srcversions": ["0123456789ABCDEF"]}
    target = start(cfg)
    assert read(f"{TPG}/param/MaxConnections") == "1"
    result = health()
    assert result.returncode == 1 and "MC/S is configured but ignored" in result.stdout
    assert "MC/S IS IGNORED" in target.output() and "iscsi_mcs_active 0" in metrics()
    assert target.stop() == 0
    cfg["module"] = {"trusted_srcversions": [read("/sys/module/iscsi_target_mod/srcversion")]}
    start(cfg)
    assert read(f"{TPG}/param/MaxConnections") == "4"
    assert health().returncode == 0 and "iscsi_mcs_active 1" in metrics()


def test_parameters_attributes_and_failures(start, image, tmp_path):
    luns = [{"lun": 0, "name": "it-ok", "file": image, "attributes": {"emulate_tpu": 1}},
            {"lun": 1, "name": "it-missing", "file": str(tmp_path / "missing.img")}]
    cfg = one_target(luns, parameters={"InitialR2T": False, "MaxBurstLength": 1048576},
                     attributes={"default_cmdsn_depth": 128})
    bad = {"iqn": TARGET + "-bad", "portal": {"addresses": ["127.0.0.1"], "port": 3261},
           "luns": [{"lun": 0, "name": "it-bad", "file": image + ".none"}],
           "acl": [{"initiator": INITIATOR}], "parameters": {"NoSuchParameter": "1"}}
    cfg["targets"].append(bad)
    target = start(cfg)
    assert read(f"{TPG}/param/InitialR2T") == "No"
    assert read(f"{TPG}/param/MaxBurstLength") == "1048576"
    assert read(f"{TPG}/attrib/default_cmdsn_depth") == "128"
    assert read(f"{CONFIGFS}/core/fileio_0/it-ok/attrib/emulate_tpu") == "1"
    assert os.listdir(f"{TPG}/lun") == ["lun_0"]
    assert not os.path.exists(f"{CONFIGFS}/iscsi/{TARGET}-bad")
    result = health()
    assert result.returncode == 1
    assert "LUN 1 (it-missing)" in result.stdout and f"{TARGET}-bad is not exported" in result.stdout
    assert "parameter NoSuchParameter=1" in target.output()


def test_yaml_error_stops_the_container(start):
    target = start({"targets": [{"iqn": TARGET}]})
    assert target.process.wait(30) == 2
    assert "configuration error" in target.output()


def test_same_file_twice_is_refused(start, image, tmp_path):
    link = tmp_path / "link.img"
    os.symlink(image, link)
    luns = [{"lun": 0, "name": "it-a", "file": image}, {"lun": 1, "name": "it-b", "file": str(link)}]
    target = start(one_target(luns))
    assert target.process.wait(30) == 2 and "are the same file" in target.output()


def _link_local():
    data = json.loads(sh("ip", "-json", "addr", "show").stdout)
    for link in data:
        for info in link.get("addr_info", []):
            if info.get("family") == "inet6" and info.get("scope") == "link" \
                    and not info.get("tentative"):
                return link["ifname"], info["local"]
    return None


def test_interface_binding_with_link_local(start, image):
    found = _link_local()
    if found is None:
        pytest.skip("no interface with a link-local address")
    name, address = found
    cfg = one_target([{"lun": 0, "name": "it-ll", "file": image}])
    cfg["targets"][0]["portal"] = {"interfaces": [name], "port": 3262}
    target = start(cfg)
    state = json.loads(read(STATE))
    portals = [p["address"] for p in state["targets"][0]["portals"]]
    assert state["targets"][0]["interfaces"] == [{"name": name, "ok": True}], target.output()
    if f"{address}%{name}" not in portals:
        assert "link-local address" in target.output()
        pytest.skip("this kernel does not accept a portal with a zone")
    assert f"[{address}%{name}]:3262" in os.listdir(f"{TPG}/np")
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
        probe.settimeout(5)
        probe.connect((address, 3262, 0, socket.if_nametoindex(name)))
    assert health().returncode == 0, health().stdout
