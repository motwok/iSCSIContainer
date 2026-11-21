import argparse
import os
import signal
import subprocess
import sys
import threading

from . import __version__, apply, chap, config, health, log, metrics, module
from .lio import Lio

CONFIGFS = "/sys/kernel/config"
CORE_MODULES = ("target_core_mod", "target_core_file", "target_core_iblock")


def _clear_if_possible():
    if not os.path.isdir(os.path.join(CONFIGFS, "target")):
        return
    try:
        Lio().clear()
    except Exception as exc:
        log.error(f"the kernel configuration could not be cleared: {exc}")


def _prepare_kernel(cfg):
    for name in CORE_MODULES:
        result = subprocess.run(["modprobe", name], capture_output=True, text=True)
        if result.returncode != 0:
            raise module.ModuleError(f"modprobe {name}: {result.stderr.strip()}")
    found = module.prepare(cfg)
    if not os.path.ismount(CONFIGFS):
        result = subprocess.run(["mount", "-t", "configfs", "configfs", CONFIGFS],
                                capture_output=True, text=True)
        if result.returncode != 0:
            raise module.ModuleError(f"configfs cannot be mounted: {result.stderr.strip()}")
    return found


def run(stop=None):
    try:
        cfg = config.load()
        apply.check_identities(cfg)
    except config.ConfigError as exc:
        log.error(f"configuration error: {exc}")
        _clear_if_possible()
        return 2
    try:
        found = _prepare_kernel(cfg)
    except module.ModuleError as exc:
        log.error(str(exc))
        return 1
    if cfg.wants_mcs and not found.mcs:
        log.banner([
            "MC/S IS IGNORED: every session gets a single connection.",
            f"The loaded iscsi_target_mod is not a patched one (module status {found.status}).",
            "Tearing down an MC/S session with the unpatched module can crash the kernel.",
            "Remedy: let the module service build the patched module and reboot the host,",
            "or name the srcversion of a module you patched yourself in the YAML."])
    lio = Lio()
    lio.clear()
    state = apply.build(cfg, lio, chap.Store(), found)
    apply.save_state(state)
    metrics.serve(state, cfg.metrics_address, cfg.metrics_port)
    for problem in health.problems(state):
        log.error(problem)
    log.info(f"iSCSIContainer {__version__} running, {len(cfg.targets)} target(s)")
    stop = threading.Event() if stop is None else stop
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda *_: stop.set())
    stop.wait()
    log.info("stopping, clearing the kernel configuration")
    lio.clear()
    apply.remove_state()
    return 0


def check_health():
    found = health.problems(apply.load_state())
    for problem in found:
        print(problem)
    return 1 if found else 0


def _config_or_none():
    try:
        return config.load()
    except config.ConfigError as exc:
        log.warning(f"the configuration cannot be used: {exc}")
        return None


def run_chap(args):
    store = chap.Store()
    cfg = _config_or_none()
    try:
        if args.action == "list":
            for line in chap.listing(cfg, store):
                print(line)
        elif args.action in ("set", "set-mutual"):
            chap.set_password(cfg, Lio(), store, args.target, args.initiator, args.user,
                              chap.read_password(), mutual=args.action == "set-mutual")
        else:
            chap.remove(cfg, Lio(), store, args.target, args.initiator,
                        mutual=args.action == "remove-mutual")
    except chap.ChapError as exc:
        log.error(str(exc))
        return 1
    return 0


def _parser():
    parser = argparse.ArgumentParser(prog="iscsictl")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="build the target from the YAML and serve until stopped")
    commands.add_parser("health", help="exit 0 if everything from the YAML is exported")
    commands.add_parser("version")
    actions = commands.add_parser("chap", help="CHAP users and passwords").add_subparsers(
        dest="action", required=True)
    for name in ("set", "set-mutual", "remove", "remove-mutual"):
        action = actions.add_parser(name)
        action.add_argument("target")
        action.add_argument("initiator")
        if name.startswith("set"):
            action.add_argument("user")
    actions.add_parser("list")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    if args.command == "run":
        return run()
    if args.command == "health":
        return check_health()
    if args.command == "chap":
        return run_chap(args)
    print(__version__)
    return 0


def entry():
    sys.exit(main())
