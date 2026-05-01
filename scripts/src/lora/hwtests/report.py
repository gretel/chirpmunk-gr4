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
class PointResult:
    """Result for one config point.

    Decode mode populates ``frames`` / ``crc_*`` / ``best_snr`` /
    ``detected_sfs``. Scan mode populates ``detections`` /
    ``detection_clusters`` / ``det_count`` / ``raw_det_count`` /
    ``best_ratio`` / ``best_chirp_slope`` / ``best_sf`` /
    ``best_sync_word`` / ``best_snr`` / ``sweeps`` / ``avg_sweep_ms`` /
    ``overflows``. TX mode reuses the decode-side counters.
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
        return {
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


def _result_to_dict(r: PointResult) -> dict[str, Any]:
    """Serialise a :class:`PointResult` for JSON output (legacy field set)."""
    return {
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
) -> dict[str, Any]:
    """Write a fully-stamped JSON result document to ``output_path``.

    Creates parent directories as needed. Echoes the summary block to
    stdout for the calling shell. Returns the document dict (so tests
    don't have to re-parse the file).
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
    "PointResult",
    "build_summary",
    "err",
    "git_sha",
    "info",
    "write_bridge_results",
    "write_results",
]
