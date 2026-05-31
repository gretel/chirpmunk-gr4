#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Decode-mode hardware test (Heltec TX → SDR RX, CRC/SNR matrix).

Originally carved out of ``lora_test.py`` in Phase 5B. Extended in
Phase 7.5.6 to support multi-DUT compete mode: one reference companion
TX is decoded by N device-under-test SDRs in parallel and per-frame
results are matched across DUTs via the FNV-1a ``payload_hash`` field
emitted by ``FrameSink``.

Single-DUT mode (one ``[[dut]]`` entry, or legacy ``--config /
--binary`` flags) preserves the original behaviour byte-for-byte: the
flat ``PointResult`` fields (frames / crc_ok / best_snr / detected_sfs)
stay populated and ``per_dut`` / ``matchup`` are empty.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Protocol

from lora.core.udp import create_udp_subscriber, parse_host_port
from lora.hwtests.harness import (
    FLUSH_DECODE_S,
    FREQ_TOL_HZ,
    EventCollector,
    MeshCoreCompanion,
    assert_all_alive,
    companion_apply_and_advert_async,
    spawn_sdr_binary,
    stop_process,
)
from lora.hwtests.matrix import (
    MATRICES,
    ConfigPoint,
    lora_airtime_s,
    point_label,
)
from lora.hwtests.report import (
    DutPointResult,
    MatchupResult,
    PointResult,
    err,
    info,
    write_results,
)
from lora.hwtests.test_config import (
    DutConfig,
    ReferenceConfig,
    TestConfig,
    synthesize_legacy,
)


class _DecodeAccumulator(Protocol):
    """Anything :func:`_collect_decode` mutates: shares fields with
    :class:`PointResult` and :class:`DutPointResult`."""

    frames: list[dict[str, Any]]
    crc_ok: int
    crc_fail: int
    best_snr: float | None
    detected_sfs: dict[int, int]


def _collect_decode(
    result: _DecodeAccumulator,
    events: list[dict[str, Any]],
    tx_freq_hz: float,
) -> None:
    """Extract decode results from ``lora_frame`` events into ``result``.

    Filters off-frequency frames (``|channel_freq - tx_freq_hz| > FREQ_TOL_HZ``,
    when ``channel_freq > 0``) and aggregates CRC, best SNR, and per-SF
    counts. Captures ``payload_hash`` per frame for cross-DUT matching.
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
        payload_hash = ev.get("payload_hash")
        ts = ev.get("ts")

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
                "noise_floor_db": phy.get("noise_floor_db"),
                "peak_db": phy.get("peak_db"),
                "payload_hash": payload_hash,
                "ts": ts,
            }
        )


def _parse_ts(ts: object) -> float | None:
    """Parse an ISO-8601 ``ts`` string (FrameSink ``ts_now``: ``2026-...Z``)
    to epoch seconds. Returns None on failure."""
    if not isinstance(ts, str):
        return None
    try:
        from datetime import datetime

        return datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return None


def _evaluate_clusters(
    out: MatchupResult,
    clusters: list[dict[str, list[float | None]]],
    labels: list[str],
    *,
    time_axis: bool,
) -> None:
    """For each cluster (label → list of SNRs), tally both_heard /
    only_by / snr_better / snr_delta_avg into ``out`` on the chosen axis.
    """
    label_set = set(labels)
    delta_key: str | None = None
    deltas: list[float] = []
    if len(labels) == 2:
        delta_key = f"{labels[0]}_over_{labels[1]}"

    for cluster in clusters:
        heard = set(cluster.keys())
        if heard == label_set:
            if time_axis:
                out.time_both_heard += 1
            else:
                out.both_heard += 1
            best_per_label: dict[str, float] = {}
            for lbl, snrs in cluster.items():
                valid = [s for s in snrs if s is not None]
                if valid:
                    best_per_label[lbl] = max(valid)
            if len(best_per_label) == len(labels):
                best_label = max(best_per_label.items(), key=lambda kv: kv[1])[0]
                tgt = out.time_snr_better if time_axis else out.snr_better
                tgt[best_label] = tgt.get(best_label, 0) + 1
                if delta_key is not None:
                    deltas.append(best_per_label[labels[0]] - best_per_label[labels[1]])
        else:
            for lbl in heard:
                tgt = out.time_only_by if time_axis else out.only_by
                tgt[lbl] = tgt.get(lbl, 0) + 1

    if delta_key is not None and deltas:
        tgt = out.time_snr_delta_avg if time_axis else out.snr_delta_avg
        tgt[delta_key] = round(sum(deltas) / len(deltas), 2)


def _cluster_by_time(
    per_dut: dict[str, DutPointResult],
    window_s: float,
) -> list[dict[str, list[float | None]]]:
    """Cluster all per-DUT frames into "same RF event" groups by ``ts``.

    Greedy: sort all (ts, label, snr) by ts, then walk: if the gap
    between the current frame and the previous cluster's last ts is
    within ``window_s``, the frame joins that cluster; otherwise a new
    cluster starts. Same-label frames in one cluster (e.g. two RX
    channels of one DUT decoding the same packet) are kept as a list
    of SNRs so the per-label "best SNR" can be computed.

    Returns one dict per cluster: ``{label: [snr_db, ...]}``.
    """
    items: list[tuple[float, str, float | None]] = []
    for label, dpr in per_dut.items():
        for f in dpr.frames:
            ts = _parse_ts(f.get("ts"))
            if ts is None:
                continue
            items.append((ts, label, f.get("snr_db")))
    items.sort(key=lambda t: t[0])

    clusters: list[dict[str, list[float | None]]] = []
    last_ts: float | None = None
    current: dict[str, list[float | None]] = {}
    for ts, label, snr in items:
        if last_ts is None or (ts - last_ts) > window_s:
            if current:
                clusters.append(current)
            current = {}
        current.setdefault(label, []).append(snr)
        last_ts = ts
    if current:
        clusters.append(current)
    return clusters


def _compute_matchup(
    per_dut: dict[str, DutPointResult],
    *,
    time_window_ms: int = 100,
) -> MatchupResult:
    """Frame-match across DUTs on two axes.

    Axis 1 — ``payload_hash``: matches frames whose post-decode
    FNV-1a hash is identical. Captures the "both DUTs decoded the
    same packet cleanly" subset; misses CRC-fail-on-one-side cases.

    Axis 2 — time-window: clusters frames whose ``ts`` are within
    ``time_window_ms`` of each other. Catches the "concurrent decode
    events even when bytes diverge" subset, including CRC failures
    and dual-RX same-packet duplicates.

    Both axes populate parallel fields on the returned
    :class:`MatchupResult`. Frames lacking the relevant field
    (``payload_hash`` or parseable ``ts``) are skipped on that axis.
    """
    out = MatchupResult(time_window_ms=time_window_ms)
    if len(per_dut) < 2:
        return out

    labels = list(per_dut.keys())

    # Axis 1: payload_hash clusters.
    by_hash: dict[int, dict[str, list[float | None]]] = {}
    for label, dpr in per_dut.items():
        for f in dpr.frames:
            ph = f.get("payload_hash")
            if ph is None:
                continue
            by_hash.setdefault(ph, {}).setdefault(label, []).append(f.get("snr_db"))
    _evaluate_clusters(out, list(by_hash.values()), labels, time_axis=False)

    # Axis 2: time-window clusters.
    time_clusters = _cluster_by_time(per_dut, window_s=time_window_ms / 1000.0)
    _evaluate_clusters(out, time_clusters, labels, time_axis=True)
    return out

    by_hash: dict[int, dict[str, float | None]] = {}
    for label, dpr in per_dut.items():
        for f in dpr.frames:
            ph = f.get("payload_hash")
            if ph is None:
                continue
            by_hash.setdefault(ph, {})[label] = f.get("snr_db")

    labels = list(per_dut.keys())
    label_set = set(labels)
    for ph, snrs in by_hash.items():
        heard = set(snrs.keys())
        if heard == label_set:
            out.both_heard += 1
            valid = [(lbl, s) for lbl, s in snrs.items() if s is not None]
            if len(valid) == len(labels):
                best_label = max(valid, key=lambda kv: kv[1])[0]
                out.snr_better[best_label] = out.snr_better.get(best_label, 0) + 1
        else:
            for lbl in heard:
                out.only_by[lbl] = out.only_by.get(lbl, 0) + 1

    if len(labels) == 2:
        a, b = labels
        deltas: list[float] = []
        for snrs in by_hash.values():
            sa, sb = snrs.get(a), snrs.get(b)
            if sa is not None and sb is not None:
                deltas.append(float(sa) - float(sb))
        if deltas:
            out.snr_delta_avg[f"{a}_over_{b}"] = round(sum(deltas) / len(deltas), 2)
    return out


async def _run_decode_point(
    point: ConfigPoint,
    companion: MeshCoreCompanion,
    collectors: list[tuple[DutConfig, EventCollector]],
) -> PointResult:
    """Configure companion via MeshCoreCompanion, send ADVERTs, drain every DUT collector."""
    flush_s = max(FLUSH_DECODE_S, lora_airtime_s(point.sf, point.bw) + 3.0)
    result = PointResult(config=asdict(point))

    def drain_all() -> None:
        for _, c in collectors:
            c.drain()

    set_ok, ok_count = await companion_apply_and_advert_async(
        point, companion, pre_send=drain_all
    )
    if not set_ok:
        err(f"set_radio failed for {point_label(point)}")
        return result
    result.tx_ok = ok_count > 0
    info(f"  TX {ok_count}/3 ok")

    await asyncio.sleep(flush_s)
    tx_freq_hz = point.freq_mhz * 1e6

    per_dut: dict[str, DutPointResult] = {}
    for dut, collector in collectors:
        dpr = DutPointResult(label=dut.label)
        events = collector.drain()
        _collect_decode(dpr, events, tx_freq_hz)
        per_dut[dut.label] = dpr

    if len(collectors) == 1:
        # Backward compat: surface the single DUT's stats on the flat
        # PointResult fields so existing readers see what they always saw.
        only = next(iter(per_dut.values()))
        result.frames = only.frames
        result.crc_ok = only.crc_ok
        result.crc_fail = only.crc_fail
        result.best_snr = only.best_snr
        result.detected_sfs = only.detected_sfs
        result.per_dut = per_dut
    else:
        result.per_dut = per_dut
        result.matchup = _compute_matchup(per_dut)
    return result


async def _connect_companion(
    reference: ReferenceConfig,
    *,
    attach: bool,
) -> tuple[MeshCoreCompanion, Any]:
    """Open a persistent connection to the companion via meshcore_py.

    Returns ``(companion, None)``. ``attach`` is accepted for API
    compatibility but ignored — the connection is always fresh.
    ``bridge_proc`` is always None (no serial_bridge subprocess needed).
    """
    companion = MeshCoreCompanion()
    if reference.tcp:
        host, port = parse_host_port(reference.tcp)
        info(f"Connecting to companion via TCP {host}:{port}")
        connected = await companion.connect(tcp_host=host, tcp_port=port)
    else:
        assert reference.serial is not None
        info(f"Connecting to companion on {reference.serial}")
        connected = await companion.connect(serial_port=reference.serial)
    if not connected:
        err("companion not responding")
    return companion, None


async def run_test(
    test_config: TestConfig,
    *,
    output_dir: str = "data/testing",
    attach: bool = False,
    matrix_override: str | None = None,
    tx_power_override: int | None = None,
) -> int:
    """Execute a decode test described by a :class:`TestConfig`.

    Used by both the new ``--test-config`` flow and the legacy single-DUT
    flow (which synthesizes a 1-DUT TestConfig before calling here).
    """
    matrix_name = matrix_override or test_config.matrix.name
    if matrix_name not in MATRICES:
        err(f"unknown matrix '{matrix_name}'")
        return 2
    matrix = list(MATRICES[matrix_name])
    if tx_power_override is not None:
        import dataclasses

        matrix = [dataclasses.replace(p, tx_power=tx_power_override) for p in matrix]

    if test_config.reference is None:
        err(
            "decode requires a reference companion — set [reference] in "
            "the test-config TOML, or pass --tcp / --serial on the CLI."
        )
        return 2

    label = test_config.label or f"decode_{matrix_name}"
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_path = os.path.join(output_dir, f"lora_test_{label}_{ts}.json")

    # ---- companion (reference) -------------------------------------------
    companion, bridge_proc = await _connect_companion(
        test_config.reference, attach=attach
    )
    radio_info = await companion.get_radio()  # pragma: no cover
    if not radio_info:  # pragma: no cover
        err("companion not responding")
        return 1
    info(f"companion: {radio_info}")  # pragma: no cover

    # ---- DUT SDR binaries ------------------------------------------------
    dut_procs: list[Any] = []
    for dut in test_config.duts:  # pragma: no cover
        proc = spawn_sdr_binary(
            dut.binary,
            config_file=dut.config_file,
            udp_port=dut.port,
            log_path=f"tmp/decode_{dut.label}.log",
            attach=attach,
            udp_timeout=30.0,
        )
        dut_procs.append(proc)

    # ---- subscribe + collect (one collector per DUT) --------------------
    collectors: list[tuple[DutConfig, EventCollector]] = []
    socks: list[socket.socket] = []
    for dut in test_config.duts:
        sock, _, _ = create_udp_subscriber(dut.host, dut.port)
        coll = EventCollector(sock, {"lora_frame"})
        coll.start()
        collectors.append((dut, coll))
        socks.append(sock)
    if not attach:
        time.sleep(1.0)

    n_duts = len(test_config.duts)
    info(
        f"\n--- decode: {len(matrix)} points, matrix={matrix_name}, "
        f"DUTs={n_duts} ({', '.join(d.label for d in test_config.duts)}) ---"
    )
    if test_config.hypothesis:
        info(f"H: {test_config.hypothesis}")
    info("")

    results: list[PointResult] = []
    try:
        for i, point in enumerate(matrix):
            info(f"[{i + 1}/{len(matrix)}] {point_label(point)}")
            assert_all_alive()
            result = await _run_decode_point(point, companion, collectors)
            results.append(result)
            if n_duts > 1 and result.per_dut:
                for lbl, dpr in result.per_dut.items():
                    snr = f" SNR={dpr.best_snr}" if dpr.best_snr is not None else ""
                    info(
                        f"    [{lbl}] {len(dpr.frames)} frames, "
                        f"{dpr.crc_ok} CRC OK{snr}"
                    )
                if result.matchup is not None:
                    m = result.matchup
                    info(
                        f"    payload_hash: both={m.both_heard} "
                        f"only_by={dict(m.only_by)} "
                        f"snr_better={dict(m.snr_better)}"
                    )
                    info(
                        f"    time({m.time_window_ms}ms): "
                        f"both={m.time_both_heard} "
                        f"only_by={dict(m.time_only_by)} "
                        f"snr_better={dict(m.time_snr_better)}"
                    )
            else:
                snr = f" SNR={result.best_snr}" if result.best_snr is not None else ""
                info(f"  => {len(result.frames)} frames, {result.crc_ok} CRC OK{snr}")
    except KeyboardInterrupt:
        info("\nInterrupted")
    finally:
        for _, c in collectors:
            c.stop()
        for s in socks:
            s.close()
        try:
            await companion.set_radio(869.618, 62.5, 8, cr=8)
        except Exception:
            pass
        for proc in dut_procs:
            stop_process(proc)

    # First DUT's binary/config preserved as the legacy scalar fields.
    first = test_config.duts[0]
    duts_doc = [
        {
            "label": d.label,
            "binary": d.binary,
            "config_file": d.config_file,
            "host": d.host,
            "port": d.port,
        }
        for d in test_config.duts
    ]
    ref_doc: dict[str, Any] | None = None
    if test_config.reference is not None:
        ref_doc = {}
        if test_config.reference.tcp:
            ref_doc["tcp"] = test_config.reference.tcp
        if test_config.reference.serial:
            ref_doc["serial"] = test_config.reference.serial

    write_results(
        output_path=output_path,
        label=label,
        hypothesis=test_config.hypothesis,
        mode="decode",
        binary=first.binary,
        config_file=first.config_file,
        matrix_name=matrix_name,
        results=results,
        duts=duts_doc,
        reference=ref_doc,
    )
    return 0


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
    test_config: TestConfig | None = None,
    tx_power: int | None = None,
) -> int:
    """Execute the decode hardware test. Returns process exit code.

    Two entry shapes:

    * ``test_config`` non-None: new multi-DUT path. Other DUT-related
      kwargs (``config_file``, ``binary``, ``host``) are ignored.
      ``matrix_name`` overrides ``test_config.matrix.name`` when set.
    * ``test_config`` None: legacy single-DUT path. The remaining
      kwargs synthesize a 1-DUT TestConfig internally.
    """
    if test_config is None:
        from lora.hwtests.harness import TRX_PORT

        test_config = synthesize_legacy(
            serial_port=serial_port,
            tcp_host=tcp_host,
            tcp_port=tcp_port,
            matrix_name=matrix_name,
            config_file=config_file,
            binary=binary,
            label=label,
            hypothesis=hypothesis,
            host=host,
            udp_port=TRX_PORT,
        )
        matrix_override = None
    else:
        # ``--matrix`` on the CLI overrides the TOML when provided.
        matrix_override = matrix_name if matrix_name else None

    return asyncio.run(
        run_test(
            test_config,
            output_dir=output_dir,
            attach=attach,
            matrix_override=matrix_override,
            tx_power_override=tx_power,
        )
    )


def parse_tcp(value: str | None) -> tuple[str | None, int | None]:
    """Parse ``HOST:PORT`` or return ``(None, None)`` for None / empty."""
    if not value:
        return None, None
    return parse_host_port(value)


__all__ = [
    "_collect_decode",
    "_compute_matchup",
    "_run_decode_point",
    "parse_tcp",
    "run",
    "run_test",
]
