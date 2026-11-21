import json
import os
import stat
import uuid

from . import chap, log, net
from .config import ConfigError

SERIAL_NAMESPACE = uuid.UUID("6f0a8d0e-5c1b-5d43-9b4e-2a1f6f3c7e10")
SYS_BLOCK = "/sys/dev/block"
STATE = "/run/iscsicontainer/state.json"


def serial(name):
    return str(uuid.uuid5(SERIAL_NAMESPACE, name))


def identity(lun):
    """What the LUN's path really is, None if it is missing or of the wrong kind."""
    try:
        status = os.stat(lun.path)
    except OSError:
        return None
    if lun.kind == "device":
        return ("device", status.st_rdev) if stat.S_ISBLK(status.st_mode) else None
    return ("file", status.st_dev, status.st_ino) if stat.S_ISREG(status.st_mode) else None


def check_identities(cfg):
    seen = {}
    for target in cfg.targets:
        for lun in target.luns:
            found = identity(lun)
            if found is None:
                continue
            if found in seen:
                raise ConfigError(f"{lun.path} and {seen[found]} are the same {found[0]}")
            seen[found] = lun.path


def _cache_mode(directory):
    # A partition has no queue of its own, its disk is the parent directory.
    for candidate in (directory, os.path.dirname(directory)):
        try:
            with open(os.path.join(candidate, "queue", "write_cache"), encoding="ascii") as handle:
                return handle.read().strip()
        except OSError:
            continue
    return None


def _below(directory, seen):
    try:
        names = os.listdir(os.path.join(directory, "slaves"))
    except OSError:
        return
    for name in names:
        real = os.path.realpath(os.path.join(directory, "slaves", name))
        if real not in seen:
            seen.add(real)
            yield real
            yield from _below(real, seen)


def hidden_cache(path):
    """True if the device reports no write cache while a disk below it has one."""
    number = os.stat(path).st_rdev
    top = os.path.realpath(os.path.join(SYS_BLOCK, f"{os.major(number)}:{os.minor(number)}"))
    if _cache_mode(top) != "write through":
        return False
    return any(_cache_mode(d) == "write back" for d in _below(top, set()))


def _quiet(action, *args):
    try:
        action(*args)
    except Exception as exc:
        log.warning(f"cleanup: {exc}")


def _set(lio, setter, obj, kind, key, value):
    try:
        setter(obj, key, value)
    except lio.Error as exc:
        raise lio.Error(f"{kind} {key}={value}: {exc}") from None


def _luns(target, lio, entry):
    made = {}
    for lun in target.luns:
        item = {"lun": lun.lun, "name": lun.name, "ok": False, "error": None}
        entry["luns"].append(item)
        if identity(lun) is None:
            wanted = "block device" if lun.kind == "device" else "regular file"
            item["error"] = f"{lun.path} is missing or not a {wanted}"
        else:
            storage = None
            try:
                if lun.kind == "device":
                    storage = lio.block(lun.name, lun.path, serial(lun.name))
                else:
                    storage = lio.fileio(lun.name, lun.path, lun.write_back, serial(lun.name))
                for key, value in lun.attributes:
                    _set(lio, lio.set_attribute, storage, "attribute", key, value)
            except lio.Error as exc:
                item["error"] = str(exc)
                if storage is not None:
                    _quiet(lio.delete, storage)
        if item["error"]:
            log.error(f"{target.iqn} LUN {lun.lun} ({lun.name}) is not exported: {item['error']}")
            continue
        if lun.kind == "device" and hidden_cache(lun.path):
            log.warning(f"{lun.path} reports no write cache but a disk below it has one: "
                        "the initiator sends no flushes, data can be lost on a power failure")
        item["ok"] = True
        made[lun.lun] = storage
    return made


def _portal(lio, tpg, entry, address, port, optional):
    try:
        lio.portal(tpg, address, port)
    except lio.Error as exc:
        if optional:
            log.warning(f"{entry['iqn']}: link-local address {address} skipped: {exc}")
            return False
        entry["portals"].append({"address": address, "port": port, "ok": False})
        log.error(f"{entry['iqn']}: no portal on {address} port {port}: {exc}")
        return False
    entry["portals"].append({"address": address, "port": port, "ok": True})
    return True


def _portals(target, lio, tpg, entry, addresses):
    for name in target.interfaces:
        item = {"name": name, "ok": False}
        entry["interfaces"].append(item)
        for address in addresses(name) or ():
            if _portal(lio, tpg, entry, address, target.port, "%" in address):
                item["ok"] = True
        if not item["ok"]:
            log.error(f"{target.iqn}: interface {name} is missing or yields no portal")
    for address in target.addresses:
        _portal(lio, tpg, entry, address, target.port, False)


def _target(target, lio, store, mcs, addresses):
    entry = {"iqn": target.iqn, "ok": True, "error": None, "portals": [], "interfaces": [],
             "luns": [], "initiators": list(target.acl)}
    storages = _luns(target, lio, entry)
    tpg = None
    try:
        tpg = lio.target(target.iqn)
        needed = chap.required(target, store)
        _set(lio, lio.set_attribute, tpg, "attribute", "generate_node_acls", "0")
        _set(lio, lio.set_attribute, tpg, "attribute", "authentication", "1" if needed else "0")
        connections = target.max_connections if mcs else 1
        _set(lio, lio.set_parameter, tpg, "parameter", "MaxConnections", str(connections))
        for key, value in target.parameters:
            _set(lio, lio.set_parameter, tpg, "parameter", key, value)
        for key, value in target.attributes:
            _set(lio, lio.set_attribute, tpg, "attribute", key, value)
        luns = {number: lio.lun(tpg, number, storage) for number, storage in storages.items()}
        for initiator in target.acl:
            acl = lio.acl(tpg, initiator)
            for number, lun in luns.items():
                lio.map(acl, number, lun)
            chap.apply_acl(lio, acl, store, target.iqn, initiator)
        _portals(target, lio, tpg, entry, addresses)
        lio.enable(tpg)
    except lio.Error as exc:
        entry["ok"] = False
        entry["error"] = str(exc)
        log.error(f"{target.iqn} is not exported: {exc}")
        if tpg is not None:
            _quiet(lio.delete_target, tpg)
        for storage in storages.values():
            _quiet(lio.delete, storage)
        return entry
    chap.report(target, store)
    return entry


def build(cfg, lio, store, mod, addresses=net.interface_addresses):
    state = {"mcs_requested": cfg.wants_mcs, "mcs_active": mod.mcs, "module_status": mod.status,
             "targets": []}
    for target in cfg.targets:
        if len(target.acl) > 1:
            log.warning(f"{target.iqn}: several initiators see the same LUNs; "
                        "without a cluster file system that destroys data")
        state["targets"].append(_target(target, lio, store, mod.mcs, addresses))
    for target_iqn, initiator in store.entries():
        target = cfg.target(target_iqn)
        if target is None or initiator not in target.acl:
            log.warning(f"CHAP credentials for {target_iqn} {initiator} are unused: "
                        "not in the configuration")
    return state


def save_state(state):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    temporary = STATE + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    os.replace(temporary, STATE)


def load_state():
    try:
        with open(STATE, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def remove_state():
    try:
        os.remove(STATE)
    except OSError:
        pass
