import getpass
import os
import sys
from dataclasses import dataclass

import yaml

from . import log
from .config import NAME_RE

ROOT = "/config/chap"
MIN_LENGTH = 12
WINDOWS_MAX = 16


class ChapError(Exception):
    pass


@dataclass
class Cred:
    user: str
    password: str
    mutual_user: object = None
    mutual_password: object = None


def _text(value):
    return isinstance(value, str) and value != ""


class Store:
    def __init__(self, root=None):
        self.root = root or ROOT

    def path(self, target, initiator):
        return os.path.join(self.root, target, initiator + ".yaml")

    def read(self, target, initiator):
        """None if nothing is set; ChapError if something is set but cannot be used."""
        path = self.path(target, initiator)
        try:
            with open(path, encoding="utf-8") as handle:
                raw = yaml.safe_load(handle)
        except FileNotFoundError:
            return None
        except (OSError, yaml.YAMLError):
            raise ChapError(f"{path} cannot be read") from None
        if not isinstance(raw, dict) or not _text(raw.get("user")) \
                or not _text(raw.get("password")):
            raise ChapError(f"{path} is not valid")
        mutual = (raw.get("mutual_user"), raw.get("mutual_password"))
        if mutual != (None, None) and not all(_text(v) for v in mutual):
            raise ChapError(f"{path} is not valid")
        return Cred(raw["user"], raw["password"], *mutual)

    def status(self, target, initiator):
        try:
            return "none" if self.read(target, initiator) is None else "ok"
        except ChapError:
            return "unreadable"

    def write(self, target, initiator, cred):
        directory = os.path.join(self.root, target)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        for item in (self.root, directory):
            os.chmod(item, 0o700)
        data = {"user": cred.user, "password": cred.password}
        if cred.mutual_user:
            data["mutual_user"] = cred.mutual_user
            data["mutual_password"] = cred.mutual_password
        path = self.path(target, initiator)
        temporary = path + ".tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            yaml.safe_dump(data, handle, default_flow_style=False)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)

    def remove(self, target, initiator):
        try:
            os.remove(self.path(target, initiator))
        except FileNotFoundError:
            return False
        try:
            os.rmdir(os.path.join(self.root, target))
        except OSError:
            pass
        return True

    def entries(self):
        out = []
        try:
            targets = sorted(os.listdir(self.root))
        except OSError:
            return out
        for target in targets:
            try:
                names = sorted(os.listdir(os.path.join(self.root, target)))
            except OSError:
                continue
            out.extend((target, n[:-5]) for n in names if n.endswith(".yaml"))
        return out


def required(target, store):
    if target.chap is not None:
        return target.chap
    return any(store.status(target.iqn, i) != "none" for i in target.acl)


def apply_acl(lio, acl, store, target, initiator):
    try:
        cred = store.read(target, initiator)
    except ChapError as exc:
        log.error(f"{target} {initiator}: {exc}; the initiator is refused")
        cred = None
    if cred is None:
        lio.set_chap(acl, None, None, None, None)
    else:
        lio.set_chap(acl, cred.user, cred.password, cred.mutual_user, cred.mutual_password)


def report(target, store):
    states = {i: store.status(target.iqn, i) for i in target.acl}
    with_password = [i for i, s in states.items() if s != "none"]
    if target.chap is False:
        if with_password:
            log.warning(f"{target.iqn}: chap is false, the passwords that are set are ignored")
        return
    if not required(target, store):
        log.warning(f"{target.iqn}: no CHAP password set, only the initiator name is checked")
        return
    if not with_password:
        log.error(f"{target.iqn}: chap is true but no password is set, "
                  "every initiator is refused")
        return
    for initiator, state in states.items():
        if state == "none":
            log.warning(f"{target.iqn}: {initiator} has no CHAP password and is refused")


def read_password(stdin=None, ask=getpass.getpass):
    stdin = sys.stdin if stdin is None else stdin
    if stdin.isatty():
        first = ask("Password: ")
        if first != ask("Repeat: "):
            raise ChapError("the passwords differ")
        return first
    return stdin.readline().rstrip("\r\n")


def _check(target, initiator, user=None, password=None):
    for value in (target, initiator):
        if not NAME_RE.match(value):
            raise ChapError(f"{value!r} is not a valid iSCSI name")
    if user is not None and (not user or any(c.isspace() for c in user)):
        raise ChapError("the user must not be empty or contain white space")
    if password is not None:
        if len(password) < MIN_LENGTH:
            raise ChapError(f"the password must have at least {MIN_LENGTH} characters")
        if len(password) > WINDOWS_MAX:
            log.warning(f"more than {WINDOWS_MAX} characters: "
                        f"the Windows initiator accepts {MIN_LENGTH} to {WINDOWS_MAX}")


def sync(cfg, lio, store, target_iqn, initiator):
    """Apply the stored credentials of one initiator to the running kernel."""
    target = cfg.target(target_iqn) if cfg is not None else None
    if target is None or initiator not in target.acl:
        log.warning("not in the configuration; this takes effect once it is")
        return
    tpg = lio.find_target(target_iqn)
    if tpg is None:
        log.warning(f"{target_iqn} is not active in the kernel; applied at the next start")
        return
    needed = required(target, store)
    lio.set_attribute(tpg, "authentication", "1" if needed else "0")
    acl = lio.find_acl(tpg, initiator)
    if acl is not None:
        apply_acl(lio, acl, store, target_iqn, initiator)
    report(target, store)


def set_password(cfg, lio, store, target, initiator, user, password, mutual=False):
    _check(target, initiator, user, password)
    cred = store.read(target, initiator)
    if mutual:
        if cred is None:
            raise ChapError("set the initiator's user and password first")
        cred.mutual_user, cred.mutual_password = user, password
    elif cred is None:
        cred = Cred(user, password)
    else:
        cred.user, cred.password = user, password
    if cred.mutual_password is not None and cred.mutual_password == cred.password:
        log.warning("the mutual password equals the initiator's; Windows refuses that")
    store.write(target, initiator, cred)
    sync(cfg, lio, store, target, initiator)


def remove(cfg, lio, store, target, initiator, mutual=False):
    _check(target, initiator)
    if mutual:
        cred = store.read(target, initiator)
        if cred is None or cred.mutual_user is None:
            raise ChapError("no mutual CHAP is set")
        cred.mutual_user = cred.mutual_password = None
        store.write(target, initiator, cred)
    elif not store.remove(target, initiator):
        raise ChapError("no CHAP is set")
    sync(cfg, lio, store, target, initiator)


def listing(cfg, store):
    lines = []
    for target_iqn, initiator in store.entries():
        try:
            cred = store.read(target_iqn, initiator)
            text = f"user={cred.user}"
            if cred.mutual_user:
                text += f" mutual_user={cred.mutual_user}"
        except ChapError:
            text = "unreadable"
        target = cfg.target(target_iqn) if cfg is not None else None
        if target is None or initiator not in target.acl:
            text += " (unused: not in the configuration)"
        lines.append(f"{target_iqn} {initiator} {text}")
    return lines
