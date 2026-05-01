#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Tests for ``lora.hwtests.decode_test._collect_decode`` (Phase 5B)."""

from __future__ import annotations

from typing import Any

from lora.hwtests.decode_test import _collect_decode
from lora.hwtests.report import PointResult

TX_FREQ_HZ = 869.618e6


def _frame(
    freq_hz: float = TX_FREQ_HZ,
    *,
    crc_ok: bool = True,
    snr: float | None = 12.5,
    sf: int = 8,
    bw: int = 62500,
    cfo_int: int | None = 0,
) -> dict[str, Any]:
    return {
        "type": "lora_frame",
        "phy": {
            "channel_freq": freq_hz,
            "snr_db": snr,
            "sf": sf,
            "bw": bw,
            "cfo_int": cfo_int,
        },
        "carrier": {"sf": sf, "bw": bw},
        "crc_valid": crc_ok,
    }


class TestCollectDecode:
    def test_increments_crc_ok_on_valid(self) -> None:
        r = PointResult(config={})
        _collect_decode(r, [_frame(crc_ok=True), _frame(crc_ok=True)], TX_FREQ_HZ)
        assert r.crc_ok == 2
        assert r.crc_fail == 0

    def test_increments_crc_fail_on_invalid(self) -> None:
        r = PointResult(config={})
        _collect_decode(r, [_frame(crc_ok=False)], TX_FREQ_HZ)
        assert r.crc_ok == 0
        assert r.crc_fail == 1

    def test_filters_off_frequency_frames(self) -> None:
        r = PointResult(config={})
        # 1 MHz away — outside ±200 kHz tolerance.
        far = _frame(freq_hz=TX_FREQ_HZ + 1_000_000)
        on = _frame(freq_hz=TX_FREQ_HZ)
        _collect_decode(r, [far, on], TX_FREQ_HZ)
        assert len(r.frames) == 1
        assert r.crc_ok == 1

    def test_keeps_within_tolerance(self) -> None:
        r = PointResult(config={})
        edge = _frame(freq_hz=TX_FREQ_HZ + 150_000)  # within ±200 kHz
        _collect_decode(r, [edge], TX_FREQ_HZ)
        assert len(r.frames) == 1

    def test_skips_non_frame_events(self) -> None:
        r = PointResult(config={})
        events = [
            {"type": "scan_spectrum"},
            _frame(),
            {"type": "lora_status"},
        ]
        _collect_decode(r, events, TX_FREQ_HZ)
        assert len(r.frames) == 1

    def test_records_best_snr(self) -> None:
        r = PointResult(config={})
        events = [
            _frame(snr=8.1),
            _frame(snr=12.4),
            _frame(snr=10.0),
        ]
        _collect_decode(r, events, TX_FREQ_HZ)
        assert r.best_snr == 12.4

    def test_skips_snr_when_missing(self) -> None:
        r = PointResult(config={})
        _collect_decode(r, [_frame(snr=None)], TX_FREQ_HZ)
        assert r.best_snr is None

    def test_counts_detected_sfs(self) -> None:
        r = PointResult(config={})
        events = [_frame(sf=7), _frame(sf=8), _frame(sf=8), _frame(sf=12)]
        _collect_decode(r, events, TX_FREQ_HZ)
        assert r.detected_sfs == {7: 1, 8: 2, 12: 1}

    def test_frame_record_fields(self) -> None:
        r = PointResult(config={})
        _collect_decode(r, [_frame(snr=12.34, cfo_int=-3)], TX_FREQ_HZ)
        f = r.frames[0]
        assert f["sf"] == 8
        assert f["bw"] == 62500
        assert f["crc_ok"] is True
        assert f["snr_db"] == 12.3  # rounded
        assert f["channel_freq"] == TX_FREQ_HZ
        assert f["cfo_int"] == -3

    def test_zero_channel_freq_is_accepted(self) -> None:
        # Some events lack channel_freq (=0); legacy harness keeps these.
        r = PointResult(config={})
        ev = _frame()
        ev["phy"]["channel_freq"] = 0.0
        _collect_decode(r, [ev], TX_FREQ_HZ)
        assert len(r.frames) == 1
