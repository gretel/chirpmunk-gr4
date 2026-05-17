#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Hardware-test result model + JSON output.

Carved out of the legacy ``lora_test.py`` in Phase 5.
Holds:

* :class:`PointResult` — one row per ConfigPoint after a TX/RX cycle.
* :func:`build_summary` — mode-aware aggregator (``decode`` / ``scan`` /
  ``tx``) returning a JSON-friendly dict.
* :func:`write_results` — stamps the run with ``git_sha``, ISO-8601
  ``timestamp``, and writes ``results + summary`` as pretty JSON to disk.
* :func:`git_sha` / :func:`info` / :func:`err` — small process-side helpers.

No I/O beyond ``write_results`` and the small stderr loggers.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class DutPointResult:
    """Per-DUT decode result for one config point. Same shape as the
    decode-relevant subset of :class:`PointResult`."""

    label: str = ""
    frames: list[dict[str, Any]] = field(default_factory=list)
    crc_ok: int = 0
    crc_fail: int = 0
    best_snr: float | None = None
    detected_sfs: dict[int, int] = field(default_factory=dict)


@dataclass
class MatchupResult:
    """Cross-DUT comparison for one config point.

    Computed only when 2+ DUTs participate in a decode test. Two
    matching axes co-exist:

    * **payload_hash axis** (``both_heard`` / ``only_by`` / ``snr_better``
      / ``snr_delta_avg``) — matches frames whose post-decode FNV-1a
      ``payload_hash`` is identical. Captures the "both DUTs decoded
      the same packet cleanly" subset. Misses CRC-fail-on-one-side
      cases (bit errors → different post-decode bytes → different
      hash).
    * **time-window axis** (``time_*`` parallel fields, default 100 ms
      cluster window) — clusters frames whose ``ts`` are within
      ``time_window_ms`` of each other into "same RF event" groups,
      regardless of CRC outcome or payload divergence. Catches the
      "concurrent decode events even when bytes diverge" subset, which
      is the common case in real bench setups where one DUT
      bit-errors what the other gets cleanly.

    For each axis:
      * ``both_heard`` — count of events heard by all DUTs.
      * ``only_by`` — label → count of events heard exclusively.
      * ``snr_better`` — label → count of all-DUT-heard events where
        the label had the highest ``snr_db``.
      * ``snr_delta_avg`` — ``"<a>_over_<b>"`` → mean of
        ``snr_a − snr_b`` across events heard by both. Only emitted
        for the 2-DUT case.
    """

    both_heard: int = 0
    only_by: dict[str, int] = field(default_factory=dict)
    snr_better: dict[str, int] = field(default_factory=dict)
    snr_delta_avg: dict[str, float] = field(default_factory=dict)
    time_window_ms: int = 100
    time_both_heard: int = 0
    time_only_by: dict[str, int] = field(default_factory=dict)
    time_snr_better: dict[str, int] = field(default_factory=dict)
    time_snr_delta_avg: dict[str, float] = field(default_factory=dict)


@dataclass
class PointResult:
    """Result for one config point.

    Decode mode populates ``frames`` / ``crc_*`` / ``best_snr`` /
    ``detected_sfs``. Scan mode populates ``detections`` /
    ``detection_clusters`` / ``det_count`` / ``raw_det_count`` /
    ``best_ratio`` / ``best_chirp_slope`` / ``best_sf`` /
    ``best_sync_word`` / ``best_snr`` / ``sweeps`` / ``avg_sweep_ms`` /
    ``overflows``. TX mode reuses the decode-side counters.

    Multi-DUT decode mode additionally populates ``per_dut`` (one
    :class:`DutPointResult` per DUT) and ``matchup`` (cross-DUT
    comparison). The legacy flat fields stay populated for the
    single-DUT case for backward compat with existing readers.
    """

    config: dict[str, Any]
    tx_ok: bool = False
    # decode mode
    frames: list[dict[str, Any]] = field(default_factory=list)
    crc_ok: int = 0
    crc_fail: int = 0
    best_snr: float | None = None
    detected_sfs: dict[int, int] = field(default_factory=dict)
    # scan mode
    detections: list[dict[str, Any]] = field(default_factory=list)
    detection_clusters: list[dict[str, Any]] = field(default_factory=list)
    det_count: int = 0  # cluster count (burst-deduped)
    raw_det_count: int = 0  # raw per-sweep hit count (pre-clustering)
    best_ratio: float | None = None
    best_chirp_slope: float | None = None
    best_sf: int | None = None
    best_sync_word: int | None = None
    sweeps: int = 0
    avg_sweep_ms: int = 0
    overflows: int = 0
    # multi-DUT decode mode
    per_dut: dict[str, DutPointResult] = field(default_factory=dict)
    matchup: MatchupResult | None = None


def info(msg: str) -> None:
    """Write an informational line to stderr (flushed)."""
    print(msg, file=sys.stderr, flush=True)


def err(msg: str) -> None:
    """Write an error line (``ERROR: ...`` prefix) to stderr (flushed)."""
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)


def git_sha() -> str:
    """Return the short Git SHA of the current HEAD, or ``"unknown"``.

    Best-effort: returns ``"unknown"`` on any error (no git, detached
    HEAD outside a repo, timeout, etc).
    """
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return r.stdout.strip() if r.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def _per_dut_summary(results: list[PointResult]) -> dict[str, dict[str, Any]]:
    """Aggregate ``per_dut`` results across all matrix points, by label."""
    by_label: dict[str, dict[str, Any]] = {}
    for r in results:
        for label, dpr in r.per_dut.items():
            agg = by_label.setdefault(
                label,
                {
                    "total_frames": 0,
                    "total_packets": 0,
                    "total_crc_ok": 0,
                    "total_crc_fail": 0,
                    "best_snrs": [],
                },
            )
            agg["total_frames"] += len(dpr.frames)
            agg["total_packets"] = agg.get("total_packets", 0) + len(
                {
                    f.get("payload_hash")
                    for f in dpr.frames
                    if f.get("payload_hash") is not None
                }
            )
            agg["total_crc_ok"] += dpr.crc_ok
            agg["total_crc_fail"] += dpr.crc_fail
            if dpr.best_snr is not None:
                agg["best_snrs"].append(dpr.best_snr)
    out: dict[str, dict[str, Any]] = {}
    for label, agg in by_label.items():
        snrs = agg.pop("best_snrs")
        tf = agg["total_frames"]
        out[label] = {
            **agg,
            "crc_ok_rate": (round(agg["total_crc_ok"] / tf, 3) if tf else 0),
            "snr_min": min(snrs) if snrs else None,
            "snr_max": max(snrs) if snrs else None,
        }
    return out


def _head_to_head_summary(results: list[PointResult]) -> dict[str, Any]:
    """Aggregate ``matchup`` across matrix points into one head-to-head.

    Aggregates both axes (payload_hash + time-window) in parallel.
    """
    payload_both = 0
    payload_only_by: dict[str, int] = {}
    payload_snr_better: dict[str, int] = {}
    payload_snr_delta_sums: dict[str, list[float]] = {}
    time_both = 0
    time_only_by: dict[str, int] = {}
    time_snr_better: dict[str, int] = {}
    time_snr_delta_sums: dict[str, list[float]] = {}
    for r in results:
        m = r.matchup
        if m is None:
            continue
        payload_both += m.both_heard
        for k, v in m.only_by.items():
            payload_only_by[k] = payload_only_by.get(k, 0) + v
        for k, v in m.snr_better.items():
            payload_snr_better[k] = payload_snr_better.get(k, 0) + v
        for k, v in m.snr_delta_avg.items():
            payload_snr_delta_sums.setdefault(k, []).append(v)
        time_both += m.time_both_heard
        for k, v in m.time_only_by.items():
            time_only_by[k] = time_only_by.get(k, 0) + v
        for k, v in m.time_snr_better.items():
            time_snr_better[k] = time_snr_better.get(k, 0) + v
        for k, v in m.time_snr_delta_avg.items():
            time_snr_delta_sums.setdefault(k, []).append(v)
    return {
        "both_heard": payload_both,
        "only_by": payload_only_by,
        "snr_better": payload_snr_better,
        "snr_delta_avg": {
            k: round(sum(v) / len(v), 2) for k, v in payload_snr_delta_sums.items() if v
        },
        "time_both_heard": time_both,
        "time_only_by": time_only_by,
        "time_snr_better": time_snr_better,
        "time_snr_delta_avg": {
            k: round(sum(v) / len(v), 2) for k, v in time_snr_delta_sums.items() if v
        },
    }


def build_summary(mode: str, results: list[PointResult]) -> dict[str, Any]:
    """Aggregate per-point results into a mode-specific summary dict."""
    n = len(results)
    tx_ok = sum(1 for r in results if r.tx_ok)

    if mode == "decode":
        total_frames = sum(len(r.frames) for r in results)
        total_crc_ok = sum(r.crc_ok for r in results)
        total_crc_fail = sum(r.crc_fail for r in results)
        pts_decode = sum(1 for r in results if r.frames)
        snrs = [r.best_snr for r in results if r.best_snr is not None]
        out: dict[str, Any] = {
            "points": n,
            "tx_ok": tx_ok,
            "points_with_decode": pts_decode,
            "total_frames": total_frames,
            "total_crc_ok": total_crc_ok,
            "total_crc_fail": total_crc_fail,
            "crc_ok_rate": (
                round(total_crc_ok / total_frames, 3) if total_frames else 0
            ),
            "snr_min": min(snrs) if snrs else None,
            "snr_max": max(snrs) if snrs else None,
        }
        per_dut = _per_dut_summary(results)
        if per_dut:
            out["per_dut"] = per_dut
        if any(r.matchup is not None for r in results):
            out["head_to_head"] = _head_to_head_summary(results)
        return out
    if mode == "transmit":
        total_adverts = sum(r.crc_ok + r.crc_fail for r in results)
        adverts_ok = sum(r.crc_ok for r in results)
        return {
            "points": n,
            "points_with_tx": tx_ok,
            "total_adverts": total_adverts,
            "adverts_ok": adverts_ok,
            "adverts_fail": total_adverts - adverts_ok,
            "ok_rate": (round(adverts_ok / total_adverts, 3) if total_adverts else 0),
        }
    if mode == "scan":
        total_det = sum(r.det_count for r in results)
        pts_det = sum(1 for r in results if r.det_count > 0)
        ratios = [r.best_ratio for r in results if r.best_ratio is not None]
        total_ovf = sum(r.overflows for r in results)
        return {
            "points": n,
            "tx_ok": tx_ok,
            "points_detected": pts_det,
            "total_detections": total_det,
            "total_overflows": total_ovf,
            "ratio_min": min(ratios) if ratios else None,
            "ratio_max": max(ratios) if ratios else None,
        }
    # tx
    total_tests = sum(r.crc_ok + r.crc_fail for r in results)
    total_pass = sum(r.crc_ok for r in results)
    total_fail = sum(r.crc_fail for r in results)
    return {
        "points": n,
        "tx_ok": tx_ok,
        "total_tests": total_tests,
        "total_pass": total_pass,
        "total_fail": total_fail,
        "pass_rate": (round(total_pass / total_tests, 3) if total_tests else 0),
    }


def _dut_result_to_dict(d: DutPointResult) -> dict[str, Any]:
    return {
        "label": d.label,
        "frames": d.frames,
        "crc_ok": d.crc_ok,
        "crc_fail": d.crc_fail,
        "best_snr": d.best_snr,
        "detected_sfs": d.detected_sfs,
    }


def _matchup_to_dict(m: MatchupResult) -> dict[str, Any]:
    return {
        "both_heard": m.both_heard,
        "only_by": m.only_by,
        "snr_better": m.snr_better,
        "snr_delta_avg": m.snr_delta_avg,
        "time_window_ms": m.time_window_ms,
        "time_both_heard": m.time_both_heard,
        "time_only_by": m.time_only_by,
        "time_snr_better": m.time_snr_better,
        "time_snr_delta_avg": m.time_snr_delta_avg,
    }


def _result_to_dict(r: PointResult) -> dict[str, Any]:
    """Serialise a :class:`PointResult` for JSON output (legacy field set).

    Multi-DUT decode runs additionally emit ``per_dut`` and ``matchup``.
    """
    out: dict[str, Any] = {
        "config": r.config,
        "tx_ok": r.tx_ok,
        "frames": r.frames,
        "crc_ok": r.crc_ok,
        "crc_fail": r.crc_fail,
        "best_snr": r.best_snr,
        "detected_sfs": r.detected_sfs,
        "detections": r.detections,
        "detection_clusters": r.detection_clusters,
        "det_count": r.det_count,
        "raw_det_count": r.raw_det_count,
        "best_ratio": r.best_ratio,
        "best_chirp_slope": r.best_chirp_slope,
        "best_sf": r.best_sf,
        "best_sync_word": r.best_sync_word,
        "best_snr_scan": r.best_snr,
        "sweeps": r.sweeps,
        "avg_sweep_ms": r.avg_sweep_ms,
        "overflows": r.overflows,
    }
    if r.per_dut:
        out["per_dut"] = {
            label: _dut_result_to_dict(dpr) for label, dpr in r.per_dut.items()
        }
    if r.matchup is not None:
        out["matchup"] = _matchup_to_dict(r.matchup)
    return out


def write_results(
    *,
    output_path: str,
    label: str,
    hypothesis: str,
    mode: str,
    binary: str,
    config_file: str,
    matrix_name: str,
    results: list[PointResult],
    duts: list[dict[str, Any]] | None = None,
    reference: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write a fully-stamped JSON result document to ``output_path``.

    Creates parent directories as needed. Echoes the summary block to
    stdout for the calling shell. Returns the document dict (so tests
    don't have to re-parse the file).

    Multi-DUT decode runs pass ``duts`` (list of per-DUT descriptors)
    and ``reference`` (companion descriptor); these get embedded in
    the result document alongside the legacy ``binary`` /
    ``config_file`` fields (which mirror the first DUT for backward
    compat).
    """
    doc: dict[str, Any] = {
        "experiment": label,
        "hypothesis": hypothesis,
        "mode": mode,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "git_sha": git_sha(),
        "binary": binary,
        "config_file": config_file,
        "matrix": matrix_name,
        "results": [_result_to_dict(r) for r in results],
        "summary": build_summary(mode, results),
    }
    if duts is not None:
        doc["duts"] = duts
    if reference is not None:
        doc["reference"] = reference
    parent = os.path.dirname(output_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
    info(f"\nResults: {output_path}")
    json.dump(doc["summary"], sys.stdout, indent=2)
    print(file=sys.stdout)
    return doc


def write_bridge_results(
    *,
    output_path: str,
    label: str,
    hypothesis: str,
    binary: str,
    config_file: str,
    matrix_name: str,
    bridge_results: list[dict[str, Any]],
    summary: dict[str, Any],
) -> dict[str, Any]:
    """Write a bridge-mode result document. Bridge results are plain dicts."""
    doc: dict[str, Any] = {
        "experiment": label,
        "hypothesis": hypothesis,
        "mode": "bridge",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "git_sha": git_sha(),
        "binary": binary,
        "config_file": config_file,
        "matrix": matrix_name,
        "results": bridge_results,
        "summary": summary,
    }
    parent = os.path.dirname(output_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
    info(f"\nResults: {output_path}")
    return doc


__all__ = [
    "DutPointResult",
    "MatchupResult",
    "PointResult",
    "build_summary",
    "err",
    "git_sha",
    "info",
    "write_bridge_results",
    "write_results",
]
