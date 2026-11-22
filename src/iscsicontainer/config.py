import ipaddress
import os
import re
from dataclasses import dataclass

import yaml


class ConfigError(Exception):
    pass


NAME_RE = re.compile(
    r"^(iqn\.\d{4}-\d{2}\.[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[!-.0-~]+)?"
    r"|eui\.[0-9a-f]{16}|naa\.[0-9a-f]{16,32})$")
LUN_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
SRCVERSION_RE = re.compile(r"^[0-9A-Fa-f]{8,64}$")
DURATION_RE = re.compile(r"^(\d+)([smh]?)$")
MANAGED_PARAMETERS = ("MaxConnections",)
MANAGED_ATTRIBUTES = ("authentication", "generate_node_acls")
DEFAULT_PORT = 3260
DEFAULT_METRICS_PORT = 9482
DEFAULT_WAIT = 900


@dataclass(frozen=True)
class Lun:
    lun: int
    name: str
    kind: str
    path: str
    write_back: bool
    attributes: tuple


@dataclass(frozen=True)
class Target:
    iqn: str
    interfaces: tuple
    addresses: tuple
    port: int
    max_connections: int
    chap: object
    parameters: tuple
    attributes: tuple
    luns: tuple
    acl: tuple
    open: bool = False


@dataclass(frozen=True)
class Config:
    targets: tuple
    metrics_port: int
    metrics_address: object
    module_wait: int
    trusted_srcversions: tuple

    @property
    def wants_mcs(self):
        return any(t.max_connections > 1 for t in self.targets)

    def target(self, iqn):
        for t in self.targets:
            if t.iqn == iqn:
                return t
        return None


def _mapping(value, where, allowed, required=()):
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: must be a mapping")
    for key in value:
        if key not in allowed:
            raise ConfigError(f"{where}: unknown key {key!r}")
    for key in required:
        if key not in value:
            raise ConfigError(f"{where}: missing key {key!r}")
    return value


def _list(value, where):
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{where}: must be a non-empty list")
    return value


def _int(value, where, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ConfigError(f"{where}: must be an integer from {low} to {high}")
    return value


def _name(value, where):
    if not isinstance(value, str) or not NAME_RE.match(value):
        raise ConfigError(f"{where}: {value!r} is not a valid iSCSI name")
    return value


def _free_map(value, where, managed, yes, no):
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: must be a mapping")
    out = []
    for key, val in value.items():
        if not isinstance(key, str) or not key:
            raise ConfigError(f"{where}: key {key!r} must be a string")
        if key in managed:
            raise ConfigError(f"{where}: {key!r} is managed by the container")
        if isinstance(val, bool):
            val = yes if val else no
        elif isinstance(val, (int, str)):
            val = str(val)
        else:
            raise ConfigError(f"{where}.{key}: must be a string, number or boolean")
        out.append((key, val))
    return tuple(out)


def _duration(value, where):
    match = None if isinstance(value, bool) else DURATION_RE.match(str(value))
    if not match:
        raise ConfigError(f"{where}: must be seconds or a number with s, m or h")
    return int(match.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[match.group(2)]


def _address(value, where):
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        raise ConfigError(f"{where}: {value!r} is not an IP address") from None


def _lun(raw, where):
    _mapping(raw, where, ("lun", "name", "file", "device", "write_back", "attributes"),
             ("lun", "name"))
    if ("file" in raw) == ("device" in raw):
        raise ConfigError(f"{where}: exactly one of 'file' and 'device' is required")
    kind = "file" if "file" in raw else "device"
    path = raw[kind]
    if not isinstance(path, str) or not path.startswith("/"):
        raise ConfigError(f"{where}.{kind}: must be an absolute path")
    name = raw["name"]
    if not isinstance(name, str) or not LUN_NAME_RE.match(name):
        raise ConfigError(f"{where}.name: {name!r} is not a valid name")
    write_back = raw.get("write_back", False)
    if not isinstance(write_back, bool):
        raise ConfigError(f"{where}.write_back: must be true or false")
    if "write_back" in raw and kind != "file":
        raise ConfigError(f"{where}.write_back: only for file LUNs")
    return Lun(
        lun=_int(raw["lun"], f"{where}.lun", 0, 255),
        name=name, kind=kind, path=os.path.normpath(path), write_back=write_back,
        attributes=_free_map(raw.get("attributes", {}), f"{where}.attributes", (), "1", "0"))


def _portal(raw, where):
    portal = _mapping(raw, where, ("interfaces", "addresses", "port"))
    if ("interfaces" in portal) == ("addresses" in portal):
        raise ConfigError(f"{where}: exactly one of 'interfaces' and 'addresses' is required")
    interfaces = addresses = ()
    if "interfaces" in portal:
        interfaces = tuple(_list(portal["interfaces"], f"{where}.interfaces"))
        for item in interfaces:
            if not isinstance(item, str) or not item or "/" in item:
                raise ConfigError(f"{where}.interfaces: {item!r} is not an interface name")
    else:
        addresses = tuple(_address(a, f"{where}.addresses")
                          for a in _list(portal["addresses"], f"{where}.addresses"))
    port = _int(portal.get("port", DEFAULT_PORT), f"{where}.port", 1, 65535)
    return interfaces, addresses, port


def _target(raw, where):
    _mapping(raw, where, ("iqn", "portal", "max_connections", "chap", "parameters",
                          "attributes", "luns", "acl"), ("iqn", "portal", "luns", "acl"))
    interfaces, addresses, port = _portal(raw["portal"], f"{where}.portal")
    chap = raw.get("chap")
    if chap is not None and not isinstance(chap, bool):
        raise ConfigError(f"{where}.chap: must be true, false or not set")
    luns = tuple(_lun(item, f"{where}.luns[{i}]")
                 for i, item in enumerate(_list(raw["luns"], f"{where}.luns")))
    numbers = [lun.lun for lun in luns]
    if len(set(numbers)) != len(numbers):
        raise ConfigError(f"{where}.luns: duplicate LUN number")
    acl = []
    is_open = raw["acl"] == "any"
    if is_open and chap is True:
        raise ConfigError(f"{where}: acl any cannot be combined with chap true")
    for i, item in enumerate([] if is_open else _list(raw["acl"], f"{where}.acl")):
        _mapping(item, f"{where}.acl[{i}]", ("initiator",), ("initiator",))
        acl.append(_name(item["initiator"], f"{where}.acl[{i}].initiator"))
    if len(set(acl)) != len(acl):
        raise ConfigError(f"{where}.acl: duplicate initiator")
    return Target(
        iqn=_name(raw["iqn"], f"{where}.iqn"),
        interfaces=interfaces, addresses=addresses, port=port,
        max_connections=_int(raw.get("max_connections", 1), f"{where}.max_connections",
                             1, 65535),
        chap=chap,
        parameters=_free_map(raw.get("parameters", {}), f"{where}.parameters",
                             MANAGED_PARAMETERS, "Yes", "No"),
        attributes=_free_map(raw.get("attributes", {}), f"{where}.attributes",
                             MANAGED_ATTRIBUTES, "1", "0"),
        luns=luns, acl=tuple(acl), open=is_open)


def parse(text):
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML cannot be read: {exc}") from None
    _mapping(raw, "configuration", ("metrics", "module", "targets"), ("targets",))
    metrics = _mapping(raw.get("metrics", {}), "metrics", ("port", "address"))
    module = _mapping(raw.get("module", {}), "module", ("wait", "trusted_srcversions"))
    trusted = module.get("trusted_srcversions", [])
    if not isinstance(trusted, list):
        raise ConfigError("module.trusted_srcversions: must be a list")
    for item in trusted:
        if not isinstance(item, str) or not SRCVERSION_RE.match(item):
            raise ConfigError(f"module.trusted_srcversions: {item!r} is not a srcversion")
    targets = tuple(_target(item, f"targets[{i}]")
                    for i, item in enumerate(_list(raw["targets"], "targets")))
    iqns = [t.iqn for t in targets]
    if len(set(iqns)) != len(iqns):
        raise ConfigError("targets: duplicate target name")
    names, paths = set(), set()
    for target in targets:
        for lun in target.luns:
            if lun.name in names:
                raise ConfigError(f"LUN name {lun.name!r} is used twice")
            if lun.path in paths:
                raise ConfigError(f"{lun.path} is used for two LUNs")
            names.add(lun.name)
            paths.add(lun.path)
    address = metrics.get("address")
    return Config(
        targets=targets,
        metrics_port=_int(metrics.get("port", DEFAULT_METRICS_PORT), "metrics.port", 1, 65535),
        metrics_address=None if address is None else _address(address, "metrics.address"),
        module_wait=_duration(module.get("wait", DEFAULT_WAIT), "module.wait"),
        trusted_srcversions=tuple(s.upper() for s in trusted))


def load(environ=None):
    environ = os.environ if environ is None else environ
    text = environ.get("ISCSI_CONFIG")
    path = environ.get("ISCSI_CONFIG_FILE")
    if bool(text) == bool(path):
        raise ConfigError("set exactly one of ISCSI_CONFIG and ISCSI_CONFIG_FILE")
    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            raise ConfigError(f"{path} cannot be read: {exc.strerror}") from None
    return parse(text)
