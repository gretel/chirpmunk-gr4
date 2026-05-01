#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Decode-mode hardware test (Heltec TX → SDR RX, CRC/SNR matrix).

Carved out of ``lora_test.py`` in Phase 5B. Public entry
point :func:`run` is invoked by the ``lora hwtest decode`` CLI; the
companion connect path goes through ``lora.bridges.meshcore.driver``
exactly as the legacy harness did.
"""

from __future__ import annotations

import os
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from lora.bridges.meshcore.driver import CompanionDriver
from lora.core.udp import create_udp_subscriber, parse_host_port
from lora.hwtests.harness import (
    BRIDGE_PORT,
    FLUSH_DECODE_S,
    FREQ_TOL_HZ,
    TRX_PORT,
    EventCollector,
    assert_all_alive,
    companion_apply_and_advert,
    spawn_sdr_binary,
    spawn_serial_bridge,
    stop_process,
)
from lora.hwtests.matrix import (
    MATRICES,
    ConfigPoint,
    lora_airtime_s,
    point_label,
)
from lora.hwtests.report import (
    PointResult,
    err,
    info,
    write_results,
)


def _collect_decode(
    result: PointResult,
    events: list[dict[str, Any]],
    tx_freq_hz: float,
) -> None:
    """Extract decode results from ``lora_frame`` events into ``result``.

    Filters off-frequency frames (``|channel_freq - tx_freq_hz| > FREQ_TOL_HZ``,
    when ``channel_freq > 0``) and aggregates CRC, best SNR, and per-SF
    counts. Mirrors the legacy harness byte-for-byte.
    """
    for ev in events:
        if ev.get("type") != "lora_frame":
            continue
        phy = ev.get("phy", {})
        carrier = ev.get("carrier", {})
        ch_freq = phy.get("channel_freq", 0.0)
        if ch_freq > 0 and abs(ch_freq - tx_freq_hz) > FREQ_TOL_HZ:
            continue

        crc_ok = ev.get("crc_valid", False)
        snr = phy.get("snr_db")
        sf = carrier.get("sf", phy.get("sf", 0))
        bw = carrier.get("bw", phy.get("bw", 0))

        if crc_ok:
            result.crc_ok += 1
        else:
            result.crc_fail += 1
        if snr is not None and (result.best_snr is None or snr > result.best_snr):
            result.best_snr = round(snr, 1)
        if sf > 0:
            result.detected_sfs[sf] = result.detected_sfs.get(sf, 0) + 1

        result.frames.append(
            {
                "sf": sf,
                "bw": bw,
                "crc_ok": crc_ok,
                "snr_db": round(snr, 1) if snr is not None else None,
                "channel_freq": ch_freq,
                "cfo_int": phy.get("cfo_int"),
            }
        )


def _run_decode_point(
    point: ConfigPoint,
    companion: CompanionDriver,
    collector: EventCollector,
) -> PointResult:
    """Configure companion, send ADVERTs, collect lora_frame events."""
    # SF12/BW62.5k airtime is ~4.5 s; use airtime + 3 s margin so the
    # decode pipeline has room to finish.
    flush_s = max(FLUSH_DECODE_S, lora_airtime_s(point.sf, point.bw) + 3.0)
    result = PointResult(config=asdict(point))

    set_ok, ok_count = companion_apply_and_advert(
        point, companion, pre_send=collector.drain
    )
    if not set_ok:
        err(f"set_radio failed for {point_label(point)}")
        return result
    result.tx_ok = ok_count > 0
    info(f"  TX {ok_count}/3 ok")

    time.sleep(flush_s)
    events = collector.drain()
    tx_freq_hz = point.freq_mhz * 1e6
    _collect_decode(result, events, tx_freq_hz)
    return result


def run(
    *,
    serial_port: str | None,
    tcp_host: str | None,
    tcp_port: int | None,
    matrix_name: str,
    config_file: str = "apps/config.toml",
    binary: str = "",
    label: str = "",
    hypothesis: str = "",
    output_dir: str = "data/testing",
    attach: bool = False,
    host: str = "127.0.0.1",
) -> int:
    """Execute the decode hardware test. Returns process exit code."""
    matrix = list(MATRICES[matrix_name])
    binary_path = binary or "./build/apps/lora_trx"
    label = label or f"decode_{matrix_name}"
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_path = os.path.join(output_dir, f"lora_test_{label}_{ts}.json")

    # ---- companion (serial_bridge for USB, direct TCP for WiFi) ----
    bridge_proc = None
    if tcp_host is not None and tcp_port is not None:
        info(f"Connecting to companion via TCP {tcp_host}:{tcp_port}")
        companion = CompanionDriver(bridge_host=tcp_host, bridge_port=tcp_port)
    else:
        if serial_port is None:
            err("decode: --serial or --tcp required")
            return 2
        # ---- past this point everything talks to real hardware ----
        bridge_proc = spawn_serial_bridge(
            serial_port, attach=attach
        )  # pragma: no cover
        companion = CompanionDriver(  # pragma: no cover
            bridge_host="127.0.0.1", bridge_port=BRIDGE_PORT
        )
    radio_info = companion.get_radio()  # pragma: no cover
    if not radio_info:  # pragma: no cover
        err("companion not responding")
        stop_process(bridge_proc)
        return 1
    info(f"companion: {radio_info}")  # pragma: no cover

    # ---- SDR binary ----
    binary_proc = spawn_sdr_binary(  # pragma: no cover
        binary_path,
        config_file=config_file,
        udp_port=TRX_PORT,
        log_path="tmp/decode.log",
        attach=attach,
        udp_timeout=30.0,
    )

    # ---- subscribe + collect ----
    sock, _, _ = create_udp_subscriber(host, TRX_PORT)
    collector = EventCollector(sock, {"lora_frame"})
    collector.start()
    if not attach:
        time.sleep(1.0)

    info(f"\n--- decode: {len(matrix)} points, matrix={matrix_name} ---")
    if hypothesis:
        info(f"H: {hypothesis}")
    info("")

    results: list[PointResult] = []
    try:
        for i, point in enumerate(matrix):
            info(f"[{i + 1}/{len(matrix)}] {point_label(point)}")
            assert_all_alive()
            result = _run_decode_point(point, companion, collector)
            results.append(result)
            snr = f" SNR={result.best_snr}" if result.best_snr is not None else ""
            info(f"  => {len(result.frames)} frames, {result.crc_ok} CRC OK{snr}")
    except KeyboardInterrupt:
        info("\nInterrupted")
    finally:
        collector.stop()
        sock.close()
        companion.set_radio(869.618, 62.5, 8, cr=8)
        stop_process(binary_proc)
        stop_process(bridge_proc)

    write_results(
        output_path=output_path,
        label=label,
        hypothesis=hypothesis,
        mode="decode",
        binary=binary_path,
        config_file=config_file,
        matrix_name=matrix_name,
        results=results,
    )
    return 0


def parse_tcp(value: str | None) -> tuple[str | None, int | None]:
    """Parse ``HOST:PORT`` or return ``(None, None)`` for None / empty."""
    if not value:
        return None, None
    return parse_host_port(value)


__all__ = ["_collect_decode", "_run_decode_point", "parse_tcp", "run"]
