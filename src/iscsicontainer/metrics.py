import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import health

LOGIN_FAILURES = ("authenticate", "authorize", "negotiate", "other")
LUN_STATS = (("iscsi_lun_commands_total", "num_cmds", 1),
             ("iscsi_lun_read_bytes_total", "read_mbytes", 1048576),
             ("iscsi_lun_written_bytes_total", "write_mbytes", 1048576))
HELP = {
    "iscsi_target_up": "1 if the target is exported",
    "iscsi_lun_up": "1 if the LUN is exported",
    "iscsi_portal_up": "1 if the portal is set up",
    "iscsi_mcs_active": "1 if MC/S may be used, 0 if it is ignored",
    "iscsi_module_info": "status of the iscsi_target_mod the target runs with",
    "iscsi_sessions": "sessions per target and initiator",
    "iscsi_connections": "TCP connections per target and initiator",
    "iscsi_lun_commands_total": "SCSI commands per LUN and initiator",
    "iscsi_lun_read_bytes_total": "bytes read per LUN and initiator",
    "iscsi_lun_written_bytes_total": "bytes written per LUN and initiator",
    "iscsi_login_failures_total": "refused logins per target",
}


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def _number(path):
    text = _read(path)
    try:
        return int(text.strip())
    except (AttributeError, ValueError):
        return None


def _label(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class _Lines:
    def __init__(self):
        self.series = {}

    def add(self, name, value, /, **labels):
        text = ",".join(f'{k}="{_label(v)}"' for k, v in labels.items())
        self.series.setdefault(name, []).append(f"{name}{{{text}}} {value}" if text
                                                else f"{name} {value}")

    def render(self):
        out = []
        for name, lines in self.series.items():
            kind = "counter" if name.endswith("_total") else "gauge"
            out += [f"# HELP {name} {HELP[name]}", f"# TYPE {name} {kind}", *lines]
        return "\n".join(out) + "\n"


def _kernel(lines, target):
    iqn = target["iqn"]
    base = os.path.join(health.CONFIGFS, iqn)
    for reason in LOGIN_FAILURES:
        value = _number(os.path.join(base, "fabric_statistics", "iscsi_login_stats",
                                     reason + "_fails"))
        if value is not None:
            lines.add("iscsi_login_failures_total", value, target=iqn, reason=reason)
    for initiator in target["initiators"]:
        acl = os.path.join(base, "tpgt_1", "acls", initiator)
        info = _read(os.path.join(acl, "info"))
        if info is None:
            continue
        lines.add("iscsi_sessions", info.count("LIO Session ID"), target=iqn,
                  initiator=initiator)
        lines.add("iscsi_connections", info.count("CID:"), target=iqn, initiator=initiator)
        for lun in target["luns"]:
            stats = os.path.join(acl, f"lun_{lun['lun']}", "statistics", "scsi_auth_intr")
            for name, attribute, factor in LUN_STATS:
                value = _number(os.path.join(stats, attribute))
                if value is not None:
                    lines.add(name, value * factor, target=iqn, initiator=initiator,
                              lun=lun["lun"], name=lun["name"])


def render(state):
    lines = _Lines()
    lines.add("iscsi_mcs_active", int(state["mcs_active"]))
    lines.add("iscsi_module_info", 1, status=state["module_status"])
    for target in state["targets"]:
        iqn = target["iqn"]
        lines.add("iscsi_target_up", int(target["ok"]), target=iqn)
        for lun in target["luns"]:
            lines.add("iscsi_lun_up", int(lun["ok"] and target["ok"]), target=iqn,
                      lun=lun["lun"], name=lun["name"])
        for portal in target["portals"]:
            lines.add("iscsi_portal_up", int(portal["ok"] and target["ok"]), target=iqn,
                      portal=health.portal_name(portal["address"], portal["port"]))
        if target["ok"]:
            _kernel(lines, target)
    return lines.render()


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] != "/metrics":
            self.send_error(404)
            return
        body = render(self.server.state).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _server(host, port, family, dual):
    class Server(ThreadingHTTPServer):
        address_family = family
        daemon_threads = True

        def server_bind(self):
            if family == socket.AF_INET6:
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0 if dual else 1)
            super().server_bind()

    return Server((host, port), _Handler)


def serve(state, address, port):
    """Serve /metrics in a background thread; all addresses if none is given."""
    if address is None:
        try:
            server = _server("::", port, socket.AF_INET6, True)
        except OSError:
            server = _server("0.0.0.0", port, socket.AF_INET, False)
    elif ":" in address:
        server = _server(address, port, socket.AF_INET6, False)
    else:
        server = _server(address, port, socket.AF_INET, False)
    server.state = state
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
