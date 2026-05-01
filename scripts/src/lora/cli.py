#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""lora -- single CLI dispatcher for the gr4-lora Python tools.

Subcommands are registered here and route to handlers in subpackages.
Long-running subcommand handlers are expected to call setproctitle()
to identify themselves clearly in ps/top output.

Calibration / RF measurement tools (formerly ``lora cal``) live in
the standalone ``gretel/gr4-rf-tools`` repo as the ``rftools`` CLI.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, NoReturn


def _stub(name: str):
    """Return a stub handler that exits 2 with a 'phase pending' message."""

    def _run(_args: argparse.Namespace) -> NoReturn:
        print(
            f"lora {name}: phase pending — not yet implemented",
            file=sys.stderr,
        )
        raise SystemExit(2)

    return _run


def _run_core(args: argparse.Namespace) -> NoReturn:
    """Run the lora-core daemon (Phase 2E)."""
    from lora.daemon.main import main as core_main

    argv: list[str] = []
    if args.config is not None:
        argv += ["--config", args.config]
    if args.log_level is not None:
        argv += ["--log-level", args.log_level]
    if args.listen is not None:
        argv += ["--listen", args.listen]
    raise SystemExit(core_main(argv))


#: Passthrough subcommands — their flags forward verbatim to the
#: underlying tool's own argparse parser. Pre-dispatched before the
#: main argparse runs because ``argparse.REMAINDER`` on a subparser
#: conflicts with the parent's ``--help`` handling on Python 3.14.
#:
#: Each value is a zero-arg importer that returns the tool's
#: ``main(argv) -> int`` callable, so the import only happens when the
#: subcommand is actually invoked (keeps ``lora --help`` cheap).


def _import_mon():
    from lora.viz.mon import main

    return main


def _import_waterfall():
    from lora.viz.waterfall import main

    return main


def _import_spectrum():
    from lora.viz.spectrum import main

    return main


def _import_wav():
    from lora.tools.wav import main

    return main


def _import_tx():
    from lora.tools.meshcore_tx import main

    return main


def _import_bridge_meshcore():
    from lora.bridges.meshcore.cli import main

    return main


def _import_bridge_serial():
    from lora.bridges.serial import main

    return main


def _import_migrate_meshcore_json():
    from lora.tools.migrate_meshcore_json import main

    return main


def _import_hwtest():
    from lora.hwtests.cli import main

    return main


def _import_daemon():
    from lora.daemon.supervisor import main

    return main


_PASSTHROUGH_IMPORTS: dict[str, Any] = {
    "mon": _import_mon,
    "waterfall": _import_waterfall,
    "spectrum": _import_spectrum,
    "wav": _import_wav,
    "tx": _import_tx,
    "hwtest": _import_hwtest,
    "daemon": _import_daemon,
}

#: Two-level passthrough — ``lora bridge <kind>``.
_BRIDGE_PASSTHROUGH_IMPORTS: dict[str, Any] = {
    "meshcore": _import_bridge_meshcore,
    "serial": _import_bridge_serial,
}

#: Three-level passthrough — ``lora bridge meshcore <subcmd>``.
_BRIDGE_MESHCORE_SUBCMD_IMPORTS: dict[str, Any] = {
    "migrate-json": _import_migrate_meshcore_json,
}


def main(argv: list[str] | None = None) -> None:
    if argv is None:
        argv = sys.argv[1:]
    argv = list(argv)

    # ---- pre-dispatch passthrough subcommands ----------------------------
    # ``lora mon --help`` must reach lora.viz.mon's argparse, not be
    # intercepted by the dispatcher. Argparse's REMAINDER handling is too
    # fragile for this — we shortcut by string matching on argv[0].
    if argv and argv[0] in _PASSTHROUGH_IMPORTS:
        cmd = argv[0]
        rest = argv[1:]
        raise SystemExit(_PASSTHROUGH_IMPORTS[cmd]()(rest))

    # ---- pre-dispatch ``lora bridge <kind> [...]`` ----------------------
    if (
        len(argv) >= 2
        and argv[0] == "bridge"
        and argv[1] in _BRIDGE_PASSTHROUGH_IMPORTS
    ):
        kind = argv[1]
        rest = argv[2:]
        # ``lora bridge meshcore migrate-json --from PATH``
        if kind == "meshcore" and rest and rest[0] in _BRIDGE_MESHCORE_SUBCMD_IMPORTS:
            sub_cmd = rest[0]
            sub_rest = rest[1:]
            raise SystemExit(_BRIDGE_MESHCORE_SUBCMD_IMPORTS[sub_cmd]()(sub_rest))
        raise SystemExit(_BRIDGE_PASSTHROUGH_IMPORTS[kind]()(rest))

    # ---- normal argparse for the structured subcommands -----------------
    parser = argparse.ArgumentParser(
        prog="lora",
        description="gr4-lora receiver software — single CLI dispatcher.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    core_parser = sub.add_parser("core", help="data-plane daemon")
    core_parser.add_argument("--config", default=None, help="path to apps/config.toml")
    core_parser.add_argument("--log-level", default=None)
    core_parser.add_argument(
        "--listen", default=None, metavar="HOST:PORT", help="override listen address"
    )
    core_parser.set_defaults(func=_run_core)

    # The passthrough subcommands appear in ``--help`` output but their
    # parsers stay empty — we never actually reach them because the
    # pre-dispatch above shortcut their argv.
    sub.add_parser("mon", help="live frame viewer", add_help=False)
    sub.add_parser("waterfall", help="RX waterfall", add_help=False)
    sub.add_parser("spectrum", help="scan spectrum", add_help=False)
    sub.add_parser("wav", help="IQ recorder", add_help=False)
    sub.add_parser("tx", help="one-shot TX builder", add_help=False)

    bridge_parser = sub.add_parser("bridge", help="protocol bridges")
    bridge_sub = bridge_parser.add_subparsers(dest="kind", required=True)
    meshcore_parser = bridge_sub.add_parser(
        "meshcore", help="MeshCore companion bridge", add_help=False
    )
    # The migrate-json subcommand surfaces in --help but is dispatched
    # by the pre-dispatcher above so its argparse owns ``--from``.
    meshcore_sub = meshcore_parser.add_subparsers(dest="meshcore_cmd")
    meshcore_sub.add_parser(
        "migrate-json",
        help="migrate legacy config.json to CBOR EEPROM",
        add_help=False,
    )
    bridge_sub.add_parser("serial", help="TCP/serial adapter", add_help=False)

    # The hwtest subcommand appears in --help output but its parser
    # stays empty — the pre-dispatch above shortcut its argv.
    sub.add_parser("hwtest", help="hardware A/B test harness", add_help=False)

    # The daemon subcommand appears in --help output but its parser
    # stays empty — the pre-dispatch above shortcut its argv.
    sub.add_parser("daemon", help="foreground supervisor", add_help=False)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
