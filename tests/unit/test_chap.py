import io
import os
import stat

import pytest

from iscsicontainer import chap
from iscsicontainer.chap import ChapError, Cred, Store

from .conftest import I1, I2, T1, T2, parse

PW = "secret-12345"
PW2 = "other-123456"


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "chap"))


def test_default_root():
    assert Store().root == chap.ROOT


def test_store_round_trip(store):
    assert store.read(T1, I1) is None and store.status(T1, I1) == "none"
    store.write(T1, I1, Cred("user", PW))
    assert store.read(T1, I1) == Cred("user", PW) and store.status(T1, I1) == "ok"
    store.write(T1, I1, Cred("user", PW, "target", PW2))
    assert store.read(T1, I1).mutual_password == PW2
    mode = stat.S_IMODE(os.stat(store.path(T1, I1)).st_mode)
    assert mode == 0o600 and stat.S_IMODE(os.stat(os.path.dirname(store.path(T1, I1))).st_mode) \
        == 0o700
    assert store.entries() == [(T1, I1)]
    store.write(T1, I2, Cred("b", PW))
    assert store.remove(T1, I1) and not store.remove(T1, I1)
    assert store.remove(T1, I2) and not os.path.exists(os.path.join(store.root, T1))


@pytest.mark.parametrize("text", [
    "- 1", "user: a", "user: a\npassword: ''", "user: a\npassword: b\nmutual_user: c", "a: ["])
def test_store_unreadable(store, text):
    os.makedirs(os.path.dirname(store.path(T1, I1)))
    with open(store.path(T1, I1), "w", encoding="utf-8") as handle:
        handle.write(text)
    with pytest.raises(ChapError):
        store.read(T1, I1)
    assert store.status(T1, I1) == "unreadable"


def test_entries_without_root_and_with_a_stray_file(store):
    assert store.entries() == []
    os.makedirs(store.root)
    with open(os.path.join(store.root, "stray"), "w", encoding="utf-8"):
        pass
    store.write(T1, I1, Cred("user", PW))
    with open(os.path.join(store.root, T1, "note.txt"), "w", encoding="utf-8"):
        pass
    assert store.entries() == [(T1, I1)]


def _target(chap_value=None):
    return parse(lambda raw: raw["targets"][0].update(chap=chap_value)).targets[0]


def test_required(store):
    assert not chap.required(_target(), store)
    assert chap.required(_target(True), store)
    store.write(T1, I2, Cred("user", PW))
    assert chap.required(_target(), store) and not chap.required(_target(False), store)


def test_open_target_needs_no_chap(store, logs):
    target = parse(lambda raw: raw["targets"][0].update(acl="any")).targets[0]
    store.write(T1, I1, Cred("user", PW))
    assert not chap.required(target, store)
    chap.report(target, store)
    assert logs() == ""


def test_apply_acl(store, lio, logs):
    tpg = lio.target(T1)
    acl = lio.acl(tpg, I1)
    chap.apply_acl(lio, acl, store, T1, I1)
    assert acl.chap == (None, None, None, None)
    store.write(T1, I1, Cred("user", PW, "target", PW2))
    chap.apply_acl(lio, acl, store, T1, I1)
    assert acl.chap == ("user", PW, "target", PW2)
    with open(store.path(T1, I1), "w", encoding="utf-8") as handle:
        handle.write("broken: [")
    chap.apply_acl(lio, acl, store, T1, I1)
    assert acl.chap == (None, None, None, None) and "refused" in logs()


def test_report(store, logs):
    chap.report(_target(), store)
    assert "only the initiator name is checked" in logs()
    chap.report(_target(False), store)
    assert logs() == ""
    chap.report(_target(True), store)
    assert "every initiator is refused" in logs()
    store.write(T1, I1, Cred("user", PW))
    chap.report(_target(False), store)
    assert "are ignored" in logs()
    chap.report(_target(), store)
    out = logs()
    assert f"{I2} has no CHAP password" in out and I1 + " has" not in out


class Tty(io.StringIO):
    def isatty(self):
        return True


def test_read_password():
    assert chap.read_password(io.StringIO(PW + "\r\n")) == PW
    answers = iter([PW, PW, PW, "different"])
    assert chap.read_password(Tty(), lambda prompt: next(answers)) == PW
    with pytest.raises(ChapError, match="differ"):
        chap.read_password(Tty(), lambda prompt: next(answers))


def test_read_password_default_stream(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(PW + "\n"))
    assert chap.read_password() == PW


def _running(lio, cfg):
    for target in cfg.targets:
        tpg = lio.target(target.iqn)
        for initiator in target.acl:
            lio.acl(tpg, initiator)


def test_set_and_remove(store, lio, logs):
    cfg = parse()
    _running(lio, cfg)
    chap.set_password(cfg, lio, store, T1, I1, "user", PW)
    tpg = lio.targets[T1]
    assert tpg.attributes["authentication"] == "1" and tpg.acls[I1].chap[:2] == ("user", PW)
    assert f"{I2} has no CHAP password" in logs()
    chap.set_password(cfg, lio, store, T1, I1, "user2", PW)
    assert store.read(T1, I1).user == "user2"
    chap.set_password(cfg, lio, store, T1, I1, "target", PW, mutual=True)
    assert "Windows refuses" in logs()
    assert tpg.acls[I1].chap == ("user2", PW, "target", PW)
    chap.remove(cfg, lio, store, T1, I1, mutual=True)
    assert tpg.acls[I1].chap == ("user2", PW, None, None)
    with pytest.raises(ChapError, match="no mutual"):
        chap.remove(cfg, lio, store, T1, I1, mutual=True)
    chap.remove(cfg, lio, store, T1, I1)
    assert tpg.attributes["authentication"] == "0" and tpg.acls[I1].chap[0] is None
    assert "only the initiator name is checked" in logs()
    with pytest.raises(ChapError, match="no CHAP is set"):
        chap.remove(cfg, lio, store, T1, I1)
    with pytest.raises(ChapError, match="no mutual"):
        chap.remove(cfg, lio, store, T1, I1, mutual=True)


def test_set_errors(store, lio, logs):
    cfg = parse()
    with pytest.raises(ChapError, match="first"):
        chap.set_password(cfg, lio, store, T1, I1, "target", PW, mutual=True)
    with pytest.raises(ChapError, match="at least 12"):
        chap.set_password(cfg, lio, store, T1, I1, "user", "short")
    with pytest.raises(ChapError, match="white space"):
        chap.set_password(cfg, lio, store, T1, I1, "a b", PW)
    with pytest.raises(ChapError, match="not a valid"):
        chap.set_password(cfg, lio, store, "nothing", I1, "user", PW)
    chap.set_password(cfg, lio, store, T1, I1, "user", PW + "-longer-than-16")
    out = logs()
    assert "more than 16" in out and "not active in the kernel" in out


def test_sync_outside_the_configuration(store, lio, logs):
    cfg = parse()
    chap.set_password(cfg, lio, store, T2, I2, "user", PW)
    assert "takes effect once it is" in logs() and store.status(T2, I2) == "ok"
    chap.set_password(None, lio, store, T1, I1, "user", PW)
    assert "takes effect once it is" in logs()
    tpg = lio.target(T2)
    chap.sync(cfg, lio, store, T2, I1)
    assert tpg.attributes["authentication"] == "0"


def test_listing(store):
    cfg = parse()
    store.write(T1, I1, Cred("user", PW, "target", PW2))
    store.write(T2, I2, Cred("other", PW))
    os.makedirs(os.path.join(store.root, "iqn.2026-11.test:zzz"))
    with open(store.path("iqn.2026-11.test:zzz", I1), "w", encoding="utf-8") as handle:
        handle.write("broken: [")
    lines = chap.listing(cfg, store)
    assert lines[0] == f"{T1} {I1} user=user mutual_user=target"
    assert lines[1] == f"{T2} {I2} user=other (unused: not in the configuration)"
    assert lines[2].endswith("unreadable (unused: not in the configuration)")
    assert all(PW not in line and PW2 not in line for line in lines)
    assert chap.listing(None, store)[0].endswith("(unused: not in the configuration)")
