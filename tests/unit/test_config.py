import pytest
import yaml

from iscsicontainer import config
from iscsicontainer.config import ConfigError

from .conftest import I1, T1, T2, make, parse


def test_full_example():
    cfg = parse()
    one, two = cfg.targets
    assert one.iqn == T1 and one.addresses == ("192.0.2.10", "2001:db8::10")
    assert one.interfaces == () and one.port == 3260 and one.max_connections == 4
    assert one.parameters == (("InitialR2T", "No"), ("MaxBurstLength", "1048576"))
    assert one.attributes == (("default_cmdsn_depth", "128"), ("demo_mode_discovery", "1"))
    assert one.chap is None and one.acl[0] == I1
    data, disk = one.luns
    assert (data.kind, data.path, data.write_back) == ("file", "/luns/data.img", True)
    assert data.attributes == (("emulate_write_cache", "1"),)
    assert (disk.kind, disk.path, disk.write_back) == ("device", "/dev/disk", False)
    assert two.interfaces == ("eth7",) and two.port == 3261 and two.max_connections == 1
    assert cfg.metrics_port == 9482 and cfg.metrics_address is None
    assert cfg.module_wait == 900 and cfg.trusted_srcversions == ()
    assert cfg.wants_mcs and cfg.target(T2) is two and cfg.target("iqn.2026-11.x:y") is None


def test_optional_sections():
    def change(raw):
        raw["metrics"] = {"port": 9000, "address": "192.0.2.1"}
        raw["module"] = {"wait": "2m", "trusted_srcversions": ["abcdef0123456789"]}
        raw["targets"][0]["chap"] = True
        raw["targets"][0]["max_connections"] = 1
    cfg = parse(change)
    assert (cfg.metrics_port, cfg.metrics_address, cfg.module_wait) == (9000, "192.0.2.1", 120)
    assert cfg.trusted_srcversions == ("ABCDEF0123456789",)
    assert cfg.targets[0].chap is True and not cfg.wants_mcs


@pytest.mark.parametrize("value,seconds", [(30, 30), ("45s", 45), ("1h", 3600)])
def test_wait(value, seconds):
    assert parse(lambda raw: raw.update(module={"wait": value})).module_wait == seconds


def _t(key, value):
    return lambda raw: raw["targets"][0].__setitem__(key, value)


def _l(key, value):
    return lambda raw: raw["targets"][0]["luns"][0].__setitem__(key, value)


def _drop(*path):
    def change(raw):
        node = raw
        for step in path[:-1]:
            node = node[step]
        del node[path[-1]]
    return change


@pytest.mark.parametrize("change", [
    lambda raw: raw.update(extra=1),
    _drop("targets"),
    lambda raw: raw.update(targets=[]),
    lambda raw: raw.update(targets="x"),
    lambda raw: raw.update(metrics=[]),
    lambda raw: raw.update(metrics={"port": 0}),
    lambda raw: raw.update(metrics={"port": True}),
    lambda raw: raw.update(metrics={"address": "nowhere"}),
    lambda raw: raw.update(module={"wait": "soon"}),
    lambda raw: raw.update(module={"wait": True}),
    lambda raw: raw.update(module={"trusted_srcversions": "abc"}),
    lambda raw: raw.update(module={"trusted_srcversions": ["xyz"]}),
    lambda raw: raw.update(module={"trusted_srcversions": [1]}),
    lambda raw: raw["targets"].append(raw["targets"][0]),
    _t("iqn", "IQN.2026-11.test:one"),
    _t("iqn", 5),
    _t("unknown", 1),
    _drop("targets", 0, "portal"),
    _t("portal", {}),
    _t("portal", {"interfaces": ["a"], "addresses": ["192.0.2.1"]}),
    _t("portal", {"interfaces": []}),
    _t("portal", {"interfaces": ["a/b"]}),
    _t("portal", {"interfaces": [7]}),
    _t("portal", {"addresses": ["192.0.2.999"]}),
    _t("portal", {"addresses": ["192.0.2.1"], "port": 70000}),
    _t("max_connections", 0),
    _t("max_connections", "4"),
    _t("chap", "yes"),
    _t("parameters", []),
    _t("parameters", {"MaxConnections": 2}),
    _t("parameters", {"X": [1]}),
    _t("parameters", {1: "a"}),
    _t("attributes", {"authentication": 0}),
    _t("attributes", {"generate_node_acls": 1}),
    _t("luns", []),
    _t("acl", []),
    _t("acl", [{"initiator": "nobody"}]),
    _t("acl", [{"name": I1}]),
    _t("acl", [{"initiator": I1}, {"initiator": I1}]),
    _l("lun", 256),
    _l("lun", "0"),
    _l("name", "bad name"),
    _l("name", 3),
    _l("file", "relative.img"),
    _l("file", 3),
    _l("device", "/dev/both"),
    _l("write_back", "yes"),
    _l("attributes", "x"),
    _drop("targets", 0, "luns", 0, "file"),
    lambda raw: raw["targets"][0]["luns"][1].update(write_back=True),
    lambda raw: raw["targets"][0]["luns"][1].update(lun=0),
    lambda raw: raw["targets"][1]["luns"][0].update(name="data"),
    lambda raw: raw["targets"][1]["luns"][0].update(file="/luns/./data.img"),
])
def test_errors(change):
    with pytest.raises(ConfigError):
        parse(change)


def test_not_yaml_and_not_a_mapping():
    with pytest.raises(ConfigError, match="cannot be read"):
        config.parse("a: [")
    with pytest.raises(ConfigError, match="must be a mapping"):
        config.parse("- 1")


def test_load(tmp_path):
    text = yaml.safe_dump(make())
    assert config.load({"ISCSI_CONFIG": text}).targets[0].iqn == T1
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    assert config.load({"ISCSI_CONFIG_FILE": str(path)}).targets[1].iqn == T2
    for environ in ({}, {"ISCSI_CONFIG": text, "ISCSI_CONFIG_FILE": str(path)}):
        with pytest.raises(ConfigError, match="exactly one"):
            config.load(environ)
    with pytest.raises(ConfigError, match="cannot be read"):
        config.load({"ISCSI_CONFIG_FILE": str(tmp_path / "missing.yaml")})


def test_load_uses_the_environment(monkeypatch):
    monkeypatch.delenv("ISCSI_CONFIG_FILE", raising=False)
    monkeypatch.setenv("ISCSI_CONFIG", yaml.safe_dump(make()))
    assert len(config.load().targets) == 2
