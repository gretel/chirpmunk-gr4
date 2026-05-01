#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""``lora hwtest`` CLI dispatcher (Phase 5D).

Routes ``lora hwtest <subcmd>`` to the appropriate module:

* ``decode``      → :func:`lora.hwtests.decode_test.run`
* ``scan``        → :func:`lora.hwtests.scan_test.run`
* ``tx``          → :func:`lora.hwtests.tx_test.run`
* ``bridge``      → :func:`lora.hwtests.bridge_test.run`
* ``transmit``    → :func:`lora.hwtests.transmit_test.run`
* ``bridge-live`` → :func:`lora.hwtests.bridge_live.main`
* ``scan-perf``   → :func:`lora.hwtests.scan_perf.main`
* ``trx-perf``    → :func:`lora.hwtests.trx_perf.main`

The matrix-driven modes share companion-connection flags via
:func:`_add_companion_args`. ``decode/scan/tx/bridge`` additionally
layer SDR-binary flags via :func:`_add_sdr_args`; ``transmit`` is
companion-only (no SDR, no UDP). The three standalone diagnostics
get a thin passthrough — each owns its own argparse.
"""

from __future__ import annotations

import argparse
import sys

from lora.core.udp import parse_host_port
from lora.hwtests.matrix import MATRICES


def _add_companion_args(p: argparse.ArgumentParser) -> None:
    """Attach companion-connection + matrix + label flags. No SDR.

    Used by every matrix-driven mode. Modes that also drive an SDR
    binary layer :func:`_add_sdr_args` on top.
    """
    conn = p.add_mutually_exclusive_group(required=True)
    conn.add_argument(
        "--serial",
        help="Companion serial port (USB, e.g. /dev/cu.usbserial-0001)",
    )
    conn.add_argument(
        "--tcp",
        metavar="HOST:PORT",
        help="Companion TCP address (WiFi bridge, e.g. 192.168.1.42:4000)",
    )
    p.add_argument(
        "--matrix",
        choices=list(MATRICES.keys()),
        default="basic",
        help="Config matrix (default: basic)",
    )
    p.add_argument("--hypothesis", default="")
    p.add_argument("--label", default="")
    p.add_argument(
        "--attach",
        action="store_true",
        default=False,
        help="Attach to already-running serial_bridge / SDR binary",
    )
    p.add_argument(
        "--tx-power",
        type=int,
        default=None,
        metavar="DBM",
        help=(
            "Override every matrix point's tx_power (dBm). Default: matrix value "
            "(2 dBm for basic/sf_sweep/full, 10 dBm for baseline/bridge_full)."
        ),
    )


def _add_sdr_args(p: argparse.ArgumentParser) -> None:
    """Attach SDR-binary flags layered on top of :func:`_add_companion_args`."""
    p.add_argument(
        "--config",
        default="apps/config.toml",
        help="TOML config for lora_trx/lora_scan",
    )
    p.add_argument(
        "--binary",
        default="",
        help="Override SDR binary path",
    )
    p.add_argument(
        "--host",
        default="127.0.0.1",
        help="SDR host for UDP subscription (default: 127.0.0.1)",
    )


def _add_matrix_args(p: argparse.ArgumentParser) -> None:
    """Attach the shared ``decode/scan/tx/bridge`` flag set (companion + SDR)."""
    _add_companion_args(p)
    _add_sdr_args(p)


def _resolve_tcp(args: argparse.Namespace) -> tuple[str | None, int | None]:
    if not args.tcp:
        return None, None
    return parse_host_port(args.tcp)


def _dispatch_matrix_mode(mode: str, args: argparse.Namespace) -> int:
    tcp_host, tcp_port = _resolve_tcp(args)
    if args.tx_power is not None:
        # Override every point in the chosen matrix with the CLI tx_power.
        # Mutates the in-process MATRICES table — fine for a one-shot CLI.
        import dataclasses

        from lora.hwtests.matrix import MATRICES as _M

        _M[args.matrix] = [
            dataclasses.replace(p, tx_power=args.tx_power) for p in _M[args.matrix]
        ]
    if mode == "transmit":
        # Companion-only TX. Skips every SDR-specific kwarg.
        from lora.hwtests.transmit_test import run as transmit_runner

        return transmit_runner(
            serial_port=args.serial,
            tcp_host=tcp_host,
            tcp_port=tcp_port,
            matrix_name=args.matrix,
            label=args.label,
            hypothesis=args.hypothesis,
            attach=args.attach,
        )
    if mode == "decode":
        from lora.hwtests.decode_test import run as runner
    elif mode == "scan":
        from lora.hwtests.scan_test import run as runner
    elif mode == "tx":
        from lora.hwtests.tx_test import run as runner
    else:  # bridge
        from lora.hwtests.bridge_test import run as runner
    return runner(
        serial_port=args.serial,
        tcp_host=tcp_host,
        tcp_port=tcp_port,
        matrix_name=args.matrix,
        config_file=args.config,
        binary=args.binary,
        label=args.label,
        hypothesis=args.hypothesis,
        attach=args.attach,
        host=args.host,
    )


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    argv = list(argv)

    # ---- standalone diagnostics: pass argv straight through ----
    if argv and argv[0] in {"bridge-live", "scan-perf", "trx-perf"}:
        sub = argv[0]
        rest = argv[1:]
        if sub == "bridge-live":
            from lora.hwtests.bridge_live import main as bl_main

            return bl_main(rest)
        if sub == "scan-perf":
            from lora.hwtests.scan_perf import main as sp_main

            return sp_main(rest)
        from lora.hwtests.trx_perf import main as tp_main

        return tp_main(rest)

    # ---- matrix-driven modes ----
    parser = argparse.ArgumentParser(
        prog="lora hwtest",
        description="Hardware test harness for gr4-lora (Phase 5).",
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    for name, help_text in (
        ("decode", "companion TX -> SDR RX, CRC/SNR matrix"),
        ("scan", "companion TX -> SDR scan, detection ratio"),
        ("tx", "SDR TX -> companion RX (ADVERT + TXT_MSG)"),
        ("bridge", "4-phase MeshCore companion round-trip"),
    ):
        sp = sub.add_parser(name, help=help_text)
        _add_matrix_args(sp)

    # Transmit is companion-only — no SDR binary, no UDP collector.
    sp_transmit = sub.add_parser(
        "transmit",
        help="companion TX only, no SDR (matrix-driven ADVERT burst)",
    )
    _add_companion_args(sp_transmit)

    # Dummies so --help lists them — actual handlers are in the
    # passthrough block above. add_help=False keeps argparse from
    # eating ``lora hwtest scan-perf --help``.
    sub.add_parser(
        "bridge-live", help="live meshcore_bridge round-trip", add_help=False
    )
    sub.add_parser("scan-perf", help="lora_scan sweep performance", add_help=False)
    sub.add_parser("trx-perf", help="lora_trx frame monitor", add_help=False)

    args = parser.parse_args(argv)
    return _dispatch_matrix_mode(args.mode, args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
