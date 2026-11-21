import os

CONFIGFS = "/sys/kernel/config/target/iscsi"


def portal_name(address, port):
    return f"[{address}]:{port}" if ":" in address else f"{address}:{port}"


def _enabled(directory):
    try:
        with open(os.path.join(directory, "enable"), encoding="ascii") as handle:
            return handle.read().strip() == "1"
    except OSError:
        return False


def problems(state):
    """Why the container is not healthy; empty if it is."""
    if state is None:
        return ["the target has not finished starting"]
    out = []
    if state["mcs_requested"] and not state["mcs_active"]:
        out.append("MC/S is configured but ignored, module status " + state["module_status"])
    for target in state["targets"]:
        iqn = target["iqn"]
        if not target["ok"]:
            out.append(f"{iqn} is not exported: {target['error']}")
            continue
        tpg = os.path.join(CONFIGFS, iqn, "tpgt_1")
        if not _enabled(tpg):
            out.append(f"{iqn} is not enabled in the kernel")
        for lun in target["luns"]:
            if not lun["ok"]:
                out.append(f"{iqn} LUN {lun['lun']} ({lun['name']}): {lun['error']}")
        for interface in target["interfaces"]:
            if not interface["ok"]:
                out.append(f"{iqn}: interface {interface['name']} yields no portal")
        if not target["portals"]:
            out.append(f"{iqn} has no portal")
        for portal in target["portals"]:
            name = portal_name(portal["address"], portal["port"])
            if not portal["ok"]:
                out.append(f"{iqn}: no portal on {name}")
            elif not os.path.isdir(os.path.join(tpg, "np", name)):
                out.append(f"{iqn}: portal {name} is not in the kernel")
    return out
