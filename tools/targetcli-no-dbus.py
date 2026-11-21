"""Lets targetcli start without a system D-Bus, as in a container.

targetcli-fb 2.1.53 looks for tcmu-runner on the system bus, but connects to the bus outside its
error handling, so it aborts with "g-io-error-quark: Could not connect" when there is no bus.
This moves the connection into the error handling; user backstores are then just not listed.
Fails if the code is not found, so a changed package is noticed at build time.
"""
import sys

PATH = "/usr/lib/python3/dist-packages/targetcli/ui_backstore.py"
OLD = """        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        try:
"""
NEW = """        try:
            bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
"""

with open(PATH, encoding="utf-8") as handle:
    text = handle.read()
if text.count(OLD) != 1:
    sys.exit(f"{PATH}: the expected code is not there, check the targetcli package")
with open(PATH, "w", encoding="utf-8") as handle:
    handle.write(text.replace(OLD, NEW))
