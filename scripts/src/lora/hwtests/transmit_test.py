#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Transmit-only hardware test (companion TX, no SDR receiver).

Drives any MeshCore-compatible companion device (Heltec V3, RAK4631,
etc.) through the standard config matrix and bursts ADVERTs at each
point. No ``lora_trx``/``lora_scan`` involvement — useful for:

* Spectrum-analyser bench tests where the receiver is a third-party tool.
* Antenna-pattern / link-budget probes against a remote MeshCore node.
* Smoke-testing the companion link itself (does the device respond,
  retune, and key-up on demand?).

Carved as a sibling of :mod:`lora.hwtests.decode_test` — the per-point
``set_radio + set_tx_power + ADVERT burst`` is shared via
:func:`lora.hwtests.harness.companion_apply_and_advert`.
"""

from __future__ import annotations

import os
from dataclasses import asdict
from datetime import datetime, timezone

from lora.bridges.meshcore.driver import CompanionDriver
from lora.core.udp import parse_host_port
from lora.hwtests.harness import (
    BRIDGE_PORT,
    companion_apply_and_advert,
    spawn_serial_bridge,
    stop_process,
)
from lora.hwtests.matrix import (
    MATRICES,
    ConfigPoint,
    point_label,
)
from lora.hwtests.report import (
    PointResult,
    err,
    info,
    write_results,
)

#: Number of ADVERTs per point. Matches the legacy decode harness so
#: ``transmit`` and ``decode`` produce comparable A/B data.
TX_REPEATS = 3


def _run_transmit_point(
    point: ConfigPoint,
    companion: CompanionDriver,
) -> PointResult:
    """Configure companion, burst ADVERTs, no receiver-side validation."""
    result = PointResult(config=asdict(point))
    set_ok, ok_count = companion_apply_and_advert(
        point, companion, tx_repeats=TX_REPEATS
    )
    if not set_ok:
        err(f"set_radio failed for {point_label(point)}")
        return result
    result.tx_ok = ok_count > 0
    result.crc_ok = ok_count
    result.crc_fail = TX_REPEATS - ok_count
    info(f"  ADVERT TX {ok_count}/{TX_REPEATS} ok")
    return result


def run(
    *,
    serial_port: str | None,
    tcp_host: str | None,
    tcp_port: int | None,
    matrix_name: str,
    label: str = "",
    hypothesis: str = "",
    output_dir: str = "data/testing",
    attach: bool = False,
) -> int:
    """Execute the companion-only transmit test. Returns process exit code."""
    matrix = list(MATRICES[matrix_name])
    label = label or f"transmit_{matrix_name}"
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_path = os.path.join(output_dir, f"lora_test_{label}_{ts}.json")

    # ---- companion (serial_bridge for USB, direct TCP for WiFi) ----
    bridge_proc = None
    if tcp_host is not None and tcp_port is not None:
        info(f"Connecting to companion via TCP {tcp_host}:{tcp_port}")
        companion = CompanionDriver(bridge_host=tcp_host, bridge_port=tcp_port)
    else:
        if serial_port is None:
            err("transmit: --serial or --tcp required")
            return 2
        bridge_proc = spawn_serial_bridge(  # pragma: no cover  # hw-only
            serial_port, attach=attach
        )
        companion = CompanionDriver(  # pragma: no cover  # hw-only
            bridge_host="127.0.0.1", bridge_port=BRIDGE_PORT
        )

    radio_info = companion.get_radio()  # pragma: no cover  # hw-only
    if not radio_info:  # pragma: no cover  # hw-only
        err("companion not responding")
        stop_process(bridge_proc)
        return 1
    info(f"companion: {radio_info}")  # pragma: no cover  # hw-only

    info(f"\n--- transmit: {len(matrix)} points, matrix={matrix_name} ---")
    if hypothesis:
        info(f"H: {hypothesis}")
    info("")

    results: list[PointResult] = []
    try:
        for i, point in enumerate(matrix):  # pragma: no cover  # hw-only
            info(f"[{i + 1}/{len(matrix)}] {point_label(point)}")
            results.append(_run_transmit_point(point, companion))
    except KeyboardInterrupt:  # pragma: no cover  # hw-only
        info("\nInterrupted")
    finally:  # pragma: no cover  # hw-only
        companion.set_radio(869.618, 62.5, 8, cr=8)
        stop_process(bridge_proc)

    write_results(
        output_path=output_path,
        label=label,
        hypothesis=hypothesis,
        mode="transmit",
        binary="",
        config_file="",
        matrix_name=matrix_name,
        results=results,
    )
    return 0


def parse_tcp(value: str | None) -> tuple[str | None, int | None]:
    """Parse ``HOST:PORT`` or return ``(None, None)`` for None / empty."""
    if not value:
        return None, None
    return parse_host_port(value)


__all__ = ["TX_REPEATS", "_run_transmit_point", "parse_tcp", "run"]
