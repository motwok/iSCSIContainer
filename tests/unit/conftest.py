import copy
import types

import pytest
import yaml

from iscsicontainer import config

T1 = "iqn.2026-11.test:one"
T2 = "iqn.2026-11.test:two"
I1 = "iqn.2026-11.test:init1"
I2 = "iqn.2026-11.test:init2"

BASE = {
    "targets": [
        {
            "iqn": T1,
            "portal": {"addresses": ["192.0.2.10", "2001:db8::10"]},
            "max_connections": 4,
            "parameters": {"InitialR2T": False, "MaxBurstLength": 1048576},
            "attributes": {"default_cmdsn_depth": "128", "demo_mode_discovery": True},
            "luns": [
                {"lun": 0, "name": "data", "file": "/luns/data.img", "write_back": True,
                 "attributes": {"emulate_write_cache": 1}},
                {"lun": 1, "name": "disk", "device": "/dev/disk"},
            ],
            "acl": [{"initiator": I1}, {"initiator": I2}],
        },
        {
            "iqn": T2,
            "portal": {"interfaces": ["eth7"], "port": 3261},
            "luns": [{"lun": 0, "name": "backup", "file": "/luns/backup.img"}],
            "acl": [{"initiator": I1}],
        },
    ],
}


def make(change=None):
    raw = copy.deepcopy(BASE)
    if change:
        change(raw)
    return raw


def parse(change=None):
    return config.parse(yaml.safe_dump(make(change)))


class FakeError(Exception):
    pass


class Thing:
    def __init__(self, kind, **values):
        self.kind = kind
        self.attributes = {}
        self.parameters = {}
        self.deleted = False
        self.__dict__.update(values)


class FakeLio:
    Error = FakeError

    def __init__(self):
        self.fail = {}
        self.calls = []
        self.targets = {}
        self.storages = {}
        self.cleared = 0

    def _do(self, method, key=None):
        self.calls.append((method, key))
        for probe in ((method, key), (method, None)):
            if probe in self.fail:
                raise FakeError(self.fail[probe])

    def clear(self):
        self._do("clear")
        self.cleared += 1
        self.targets.clear()
        self.storages.clear()

    def fileio(self, name, path, write_back, serial):
        self._do("fileio", name)
        self.storages[name] = Thing("fileio", name=name, path=path, write_back=write_back,
                                    serial=serial)
        return self.storages[name]

    def block(self, name, path, serial):
        self._do("block", name)
        self.storages[name] = Thing("block", name=name, path=path, serial=serial)
        return self.storages[name]

    def delete(self, obj):
        self._do("delete", obj.name)
        obj.deleted = True
        self.storages.pop(obj.name, None)

    def set_attribute(self, obj, key, value):
        self._do("set_attribute", key)
        obj.attributes[key] = value

    def set_parameter(self, tpg, key, value):
        self._do("set_parameter", key)
        tpg.parameters[key] = value

    def target(self, iqn):
        self._do("target", iqn)
        self.targets[iqn] = Thing("tpg", iqn=iqn, portals=[], luns={}, acls={}, enabled=False)
        return self.targets[iqn]

    def find_target(self, iqn):
        return self.targets.get(iqn)

    def delete_target(self, tpg):
        self._do("delete_target", tpg.iqn)
        self.targets.pop(tpg.iqn, None)

    def enable(self, tpg):
        self._do("enable", tpg.iqn)
        tpg.enabled = True

    def portal(self, tpg, address, port):
        self._do("portal", address)
        tpg.portals.append((address, port))

    def lun(self, tpg, number, storage):
        self._do("lun", number)
        tpg.luns[number] = storage
        return (number, storage)

    def acl(self, tpg, initiator):
        self._do("acl", initiator)
        tpg.acls[initiator] = Thing("acl", initiator=initiator, mapped=[], chap=None)
        return tpg.acls[initiator]

    def find_acl(self, tpg, initiator):
        return tpg.acls.get(initiator)

    def map(self, acl, number, lun):
        self._do("map", number)
        acl.mapped.append(number)

    def set_chap(self, acl, user, password, mutual_user, mutual_password):
        self._do("set_chap", acl.initiator)
        acl.chap = (user, password, mutual_user, mutual_password)


@pytest.fixture
def lio():
    return FakeLio()


@pytest.fixture
def logs(capsys):
    def read():
        return capsys.readouterr().err
    return read


def completed(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class Runner:
    """Stands in for subprocess.run; answers by the first words of the command."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.commands = []

    def __call__(self, args, **kwargs):
        self.commands.append(list(args))
        for size in range(len(args), 0, -1):
            key = " ".join(args[:size])
            if key in self.answers:
                answer = self.answers[key]
                return answer if not isinstance(answer, list) else answer.pop(0)
        return completed()
