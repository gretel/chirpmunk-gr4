#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Scan-mode hardware test (Heltec TX → SDR scan, detection ratio).

Carved out of ``lora_test.py`` in Phase 5B. Public entry
point :func:`run` is invoked by the ``lora hwtest scan`` CLI. Cluster
logic mirrors the legacy harness verbatim so that detection counts match
existing JSON outputs.
"""

from __future__ import annotations

import os
import time
import tomllib
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from lora.bridges.meshcore.driver import CompanionDriver
from lora.core.udp import create_udp_subscriber
from lora.hwtests.harness import (
    BRIDGE_PORT,
    FLUSH_SCAN_S,
    FLUSH_SCAN_TUNING_S,
    FREQ_TOL_HZ,
    SCAN_PORT,
    SETTLE_S,
    EventCollector,
    assert_all_alive,
    spawn_sdr_binary,
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


def _collect_scan(
    result: PointResult,
    events: list[dict[str, Any]],
    tx_freq_hz: float,
) -> None:
    """Extract scan results into ``result`` with burst-level clustering.

    Per-sweep detections (``scan_spectrum`` events with non-zero
    ``ratio`` and within ``±FREQ_TOL_HZ`` of ``tx_freq_hz``) are
    bucketed by 62.5 kHz frequency bin. Hits within ``kMaxGap`` (=2)
    consecutive sweeps merge into one cluster; the highest-ratio entry
    is the cluster representative. ``det_count`` counts clusters,
    ``raw_det_count`` the pre-clustering hits — same definitions as the
    legacy harness.
    """
    n_spectrum = sum(1 for e in events if e.get("type") == "scan_spectrum")
    n_with_det = sum(
        1 for e in events if e.get("type") == "scan_spectrum" and e.get("detections")
    )
    if n_spectrum > 0:
        info(f"  cbor: {n_spectrum} spectrum, {n_with_det} with det")

    sweep_durs: list[int] = []
    hits: list[tuple[int, dict[str, Any]]] = []
    sweep_idx = 0
    for ev in events:
        t = ev.get("type", "")
        if t == "scan_sweep_end":
            dur = ev.get("duration_ms", 0)
            if dur > 0:
                sweep_durs.append(dur)
            result.overflows += ev.get("overflows", 0)
            sweep_idx += 1
        elif t == "scan_spectrum":
            for d in ev.get("detections", []):
                if isinstance(d, dict) and d.get("ratio", 0) > 0:
                    if abs(d.get("freq", 0) - tx_freq_hz) < FREQ_TOL_HZ:
                        hits.append((sweep_idx, d))
                        result.detections.append(d)

    result.sweeps = len(sweep_durs)
    result.avg_sweep_ms = int(sum(sweep_durs) / len(sweep_durs)) if sweep_durs else 0
    result.raw_det_count = len(result.detections)

    # Cluster on (freq_bin, sweep_gap). 62.5 kHz grid matches the scan
    # channelizer. Two consecutive-sweep hits on the same bin merge into
    # one cluster; a gap > kMaxGap sweeps starts a new cluster.
    k_max_gap = 2
    k_freq_bin_hz = 62500.0
    bins: dict[int, list[tuple[int, dict[str, Any]]]] = {}
    for sweep, d in hits:
        fb = int(round(d.get("freq", 0.0) / k_freq_bin_hz))
        bins.setdefault(fb, []).append((sweep, d))

    clusters: list[list[dict[str, Any]]] = []
    for entries in bins.values():
        entries.sort(key=lambda x: x[0])
        cur: list[dict[str, Any]] = [entries[0][1]]
        last_sweep = entries[0][0]
        for sweep, d in entries[1:]:
            if sweep - last_sweep <= k_max_gap:
                cur.append(d)
            else:
                clusters.append(cur)
                cur = [d]
            last_sweep = sweep
        clusters.append(cur)

    result.detection_clusters = [
        max(cl, key=lambda d: d.get("ratio", 0.0)) for cl in clusters
    ]
    result.det_count = len(result.detection_clusters)

    if result.detections:
        best = max(result.detections, key=lambda d: d.get("ratio", 0))
        result.best_ratio = round(best.get("ratio", 0.0), 1)
        slopes = [d.get("chirp_slope", 0) for d in result.detections]
        result.best_chirp_slope = max(slopes) if slopes else 0
        sfs = [d.get("sf", 0) for d in result.detections if d.get("sf", 0) > 0]
        result.best_sf = max(sfs, key=sfs.count) if sfs else None
        if best.get("sync_word") is not None:
            result.best_sync_word = best.get("sync_word")
            result.best_snr = round(best.get("snr_db", -999), 1)


def _is_tuning_scan_config(config_file: str) -> bool:
    """True when ``[scan].streaming = false`` in the TOML config."""
    try:
        with open(config_file, "rb") as f:
            toml = tomllib.load(f)
        return not toml.get("scan", {}).get("streaming", True)
    except Exception:
        return False


def _run_scan_point(
    point: ConfigPoint,
    companion: CompanionDriver,
    collector: EventCollector,
    *,
    tuning_scan: bool,
) -> PointResult:
    flush_s = FLUSH_SCAN_TUNING_S if tuning_scan else FLUSH_SCAN_S
    result = PointResult(config=asdict(point))

    bw_khz = point.bw / 1000.0
    if not companion.set_radio(point.freq_mhz, bw_khz, point.sf, cr=point.cr):
        err(f"set_radio failed for {point_label(point)}")
        return result
    if not companion.set_tx_power(point.tx_power):
        err(f"set_tx_power({point.tx_power}) failed")

    time.sleep(SETTLE_S)
    collector.drain()

    ok_count = 0
    tx_repeats = 3
    for i in range(tx_repeats):
        if companion.send_advert():
            ok_count += 1
        if i < tx_repeats - 1:
            time.sleep(2.0)
    result.tx_ok = ok_count > 0
    info(f"  TX {ok_count}/{tx_repeats} ok")

    time.sleep(flush_s)
    events = collector.drain()
    tx_freq_hz = point.freq_mhz * 1e6
    _collect_scan(result, events, tx_freq_hz)
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
    matrix = list(MATRICES[matrix_name])
    binary_path = binary or "./build/apps/lora_scan"
    label = label or f"scan_{matrix_name}"
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_path = os.path.join(output_dir, f"lora_test_{label}_{ts}.json")

    bridge_proc = None
    if tcp_host is not None and tcp_port is not None:
        info(f"Connecting to companion via TCP {tcp_host}:{tcp_port}")
        companion = CompanionDriver(bridge_host=tcp_host, bridge_port=tcp_port)
    else:
        if serial_port is None:
            err("scan: --serial or --tcp required")
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

    # Tuning scanner needs longer startup (first sweep ~30-60 s).
    udp_timeout = 90.0
    binary_proc = spawn_sdr_binary(  # pragma: no cover
        binary_path,
        config_file=config_file,
        udp_port=SCAN_PORT,
        log_path="tmp/scan.log",
        attach=attach,
        udp_timeout=udp_timeout,
    )

    sock, _, _ = create_udp_subscriber(host, SCAN_PORT)
    collector = EventCollector(sock, {"scan_spectrum", "scan_sweep_end"})
    collector.start()
    if not attach:
        # B210 at 16 MS/s has ~8 s transient (master clock switch,
        # overflow burst, L1 energy settling).
        time.sleep(10.0)

    is_tuning_scan = _is_tuning_scan_config(config_file)
    if is_tuning_scan:
        info("(tuning scanner mode — extended flush timeouts)")

    info(f"\n--- scan: {len(matrix)} points, matrix={matrix_name} ---")
    if hypothesis:
        info(f"H: {hypothesis}")
    info("")

    results: list[PointResult] = []
    try:
        for i, point in enumerate(matrix):
            info(f"[{i + 1}/{len(matrix)}] {point_label(point)}")
            assert_all_alive()
            result = _run_scan_point(
                point, companion, collector, tuning_scan=is_tuning_scan
            )
            results.append(result)
            info(f"  => {result.det_count} det, ratio={result.best_ratio or 0:.1f}")
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
        mode="scan",
        binary=binary_path,
        config_file=config_file,
        matrix_name=matrix_name,
        results=results,
    )
    return 0


__all__ = ["_collect_scan", "_is_tuning_scan_config", "_run_scan_point", "run"]
