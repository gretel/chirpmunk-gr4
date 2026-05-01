#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Tests for ``lora.hwtests.scan_test._collect_scan`` (Phase 5B)."""

from __future__ import annotations

from typing import Any

from lora.hwtests.report import PointResult
from lora.hwtests.scan_test import _collect_scan

TX_FREQ_HZ = 869.618e6


def _spectrum(detections: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "scan_spectrum", "detections": detections}


def _sweep_end(duration_ms: int = 555, overflows: int = 0) -> dict[str, Any]:
    return {
        "type": "scan_sweep_end",
        "duration_ms": duration_ms,
        "overflows": overflows,
    }


def _det(
    freq: float = TX_FREQ_HZ,
    ratio: float = 50.0,
    sf: int = 8,
    chirp_slope: float = 7800.0,
    sync_word: int | None = None,
    snr: float = 12.0,
) -> dict[str, Any]:
    d: dict[str, Any] = {
        "freq": freq,
        "ratio": ratio,
        "sf": sf,
        "chirp_slope": chirp_slope,
    }
    if sync_word is not None:
        d["sync_word"] = sync_word
        d["snr_db"] = snr
    return d


class TestCollectScan:
    def test_records_sweep_durations(self) -> None:
        r = PointResult(config={})
        events = [_sweep_end(500), _sweep_end(600), _sweep_end(550)]
        _collect_scan(r, events, TX_FREQ_HZ)
        assert r.sweeps == 3
        assert r.avg_sweep_ms == 550

    def test_zero_sweeps_yields_zero_avg(self) -> None:
        r = PointResult(config={})
        _collect_scan(r, [], TX_FREQ_HZ)
        assert r.sweeps == 0
        assert r.avg_sweep_ms == 0

    def test_skips_zero_duration_sweeps(self) -> None:
        r = PointResult(config={})
        _collect_scan(r, [_sweep_end(0), _sweep_end(500)], TX_FREQ_HZ)
        assert r.sweeps == 1
        assert r.avg_sweep_ms == 500

    def test_aggregates_overflows(self) -> None:
        r = PointResult(config={})
        _collect_scan(
            r,
            [_sweep_end(overflows=2), _sweep_end(overflows=3)],
            TX_FREQ_HZ,
        )
        assert r.overflows == 5

    def test_filters_off_freq_detections(self) -> None:
        r = PointResult(config={})
        events = [
            _spectrum([_det(freq=TX_FREQ_HZ + 1_000_000)]),  # 1 MHz off → drop
            _sweep_end(),
            _spectrum([_det(freq=TX_FREQ_HZ)]),
            _sweep_end(),
        ]
        _collect_scan(r, events, TX_FREQ_HZ)
        assert len(r.detections) == 1

    def test_drops_detections_with_zero_ratio(self) -> None:
        r = PointResult(config={})
        events = [
            _spectrum([_det(ratio=0)]),
            _sweep_end(),
            _spectrum([_det(ratio=50)]),
            _sweep_end(),
        ]
        _collect_scan(r, events, TX_FREQ_HZ)
        assert len(r.detections) == 1

    def test_clusters_consecutive_same_bin(self) -> None:
        # Two hits on the same freq bin in consecutive sweeps cluster as one.
        r = PointResult(config={})
        events = [
            _spectrum([_det(ratio=40)]),
            _sweep_end(),
            _spectrum([_det(ratio=55)]),  # higher ratio → cluster rep
            _sweep_end(),
        ]
        _collect_scan(r, events, TX_FREQ_HZ)
        assert r.raw_det_count == 2
        assert r.det_count == 1
        assert r.detection_clusters[0]["ratio"] == 55  # best-ratio rep

    def test_separates_clusters_when_gap_exceeds_kmaxgap(self) -> None:
        # Two hits separated by more than kMaxGap (=2) sweeps → two clusters.
        r = PointResult(config={})
        events = [
            _spectrum([_det()]),
            _sweep_end(),
            _sweep_end(),
            _sweep_end(),
            _sweep_end(),  # 4 sweeps gap
            _spectrum([_det()]),
            _sweep_end(),
        ]
        _collect_scan(r, events, TX_FREQ_HZ)
        assert r.det_count == 2

    def test_best_ratio_picks_max(self) -> None:
        r = PointResult(config={})
        events = [
            _spectrum([_det(ratio=40)]),
            _sweep_end(),
            _spectrum([_det(ratio=82.7)]),
            _sweep_end(),
        ]
        _collect_scan(r, events, TX_FREQ_HZ)
        assert r.best_ratio == 82.7

    def test_best_sf_is_mode_of_detected_sfs(self) -> None:
        r = PointResult(config={})
        events = [
            _spectrum([_det(sf=8)]),
            _sweep_end(),
            _sweep_end(),
            _sweep_end(),
            _spectrum([_det(sf=8)]),
            _sweep_end(),
            _sweep_end(),
            _sweep_end(),
            _spectrum([_det(sf=10)]),
            _sweep_end(),
        ]
        _collect_scan(r, events, TX_FREQ_HZ)
        assert r.best_sf == 8

    def test_sync_word_extracted_from_best_detection(self) -> None:
        r = PointResult(config={})
        events = [
            _spectrum([_det(ratio=30, sync_word=0x12, snr=10.0)]),
            _sweep_end(),
            _spectrum([_det(ratio=80, sync_word=0x34, snr=15.5)]),
            _sweep_end(),
        ]
        _collect_scan(r, events, TX_FREQ_HZ)
        assert r.best_sync_word == 0x34
        assert r.best_snr == 15.5
