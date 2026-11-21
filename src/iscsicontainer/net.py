import json
import subprocess


def interface_addresses(name, run=subprocess.run):
    """Addresses of an interface, link-local with zone; None if it does not exist."""
    result = run(["ip", "-json", "addr", "show", "dev", name], capture_output=True, text=True)
    if result.returncode != 0:
        return None
    out = []
    for link in json.loads(result.stdout or "[]"):
        for info in link.get("addr_info", []):
            if info.get("family") not in ("inet", "inet6"):
                continue
            if info.get("temporary") or info.get("tentative") or info.get("deprecated"):
                continue
            address = info["local"]
            if info["family"] == "inet6" and info.get("scope") == "link":
                address = f"{address}%{name}"
            out.append(address)
    return out
