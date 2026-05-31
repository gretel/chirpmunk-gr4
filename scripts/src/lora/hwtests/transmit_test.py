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

Uses :class:`lora.hwtests.harness.MeshCoreCompanion` for a persistent
TCP/serial connection (one connect, many commands — no per-command
``uvx meshcore-cli`` subprocess, unlike the old ``CompanionDriver``).
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import asdict
from datetime import datetime, timezone

from lora.hwtests.harness import (
    MeshCoreCompanion,
    companion_apply_and_advert_async,
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


async def _run_transmit_point(
    point: ConfigPoint,
    companion: MeshCoreCompanion,
) -> PointResult:
    """Configure companion, burst ADVERTs, no receiver-side validation."""
    result = PointResult(config=asdict(point))
    set_ok, ok_count = await companion_apply_and_advert_async(
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


async def _run_async(
    *,
    serial_port: str | None,
    tcp_host: str | None,
    tcp_port: int | None,
    matrix_name: str,
    label: str = "",
    hypothesis: str = "",
    output_dir: str = "data/testing",
) -> int:
    """Async core of the transmit test. Call via ``asyncio.run()``."""
    matrix = list(MATRICES[matrix_name])
    label = label or f"transmit_{matrix_name}"
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_path = os.path.join(output_dir, f"lora_test_{label}_{ts}.json")

    # ---- companion (direct TCP or serial via meshcore_py) ----
    companion = MeshCoreCompanion()
    if tcp_host is not None and tcp_port is not None:
        info(f"Connecting to companion via TCP {tcp_host}:{tcp_port}")
        connected = await companion.connect(tcp_host=tcp_host, tcp_port=tcp_port)
    elif serial_port is not None:
        info(f"Connecting to companion on {serial_port}")
        connected = await companion.connect(serial_port)
    else:
        err("transmit: --serial or --tcp required")
        return 2

    if not connected:
        err("companion not responding")
        await companion.close()
        return 1

    radio = await companion.get_radio()
    info(f"companion: {radio}")

    info(f"\n--- transmit: {len(matrix)} points, matrix={matrix_name} ---")
    if hypothesis:
        info(f"H: {hypothesis}")
    info("")

    results: list[PointResult] = []
    try:
        for i, point in enumerate(matrix):
            info(f"[{i + 1}/{len(matrix)}] {point_label(point)}")
            results.append(await _run_transmit_point(point, companion))
    except KeyboardInterrupt:
        info("\nInterrupted")
    finally:
        await companion.set_radio(869.618, 62.5, 8, cr=8)
        await companion.close()

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
    from lora.core.udp import parse_host_port

    return parse_host_port(value)


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
    """Execute the companion-only transmit test. Returns process exit code.

    ``attach`` is accepted for API compatibility with other hwtest modes
    but ignored — ``MeshCoreCompanion`` always opens its own persistent
    connection.
    """
    return asyncio.run(
        _run_async(
            serial_port=serial_port,
            tcp_host=tcp_host,
            tcp_port=tcp_port,
            matrix_name=matrix_name,
            label=label,
            hypothesis=hypothesis,
            output_dir=output_dir,
        )
    )


__all__ = ["TX_REPEATS", "_run_transmit_point", "run"]
