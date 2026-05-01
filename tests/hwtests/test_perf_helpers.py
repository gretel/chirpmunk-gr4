#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Tests for ``lora.hwtests.{scan_perf,trx_perf}`` pure helpers."""

from __future__ import annotations

from typing import Any

from lora.hwtests.scan_perf import (
    RunningStats,
    _avg,
    _p50,
)
from lora.hwtests.scan_perf import (
    emit_summary as scan_emit_summary,
)
from lora.hwtests.scan_perf import (
    emit_sweep as scan_emit_sweep,
)
from lora.hwtests.trx_perf import (
    FrameStats,
    WidebandStats,
    format_bw,
)
from lora.hwtests.trx_perf import (
    emit_frame as trx_emit_frame,
)
from lora.hwtests.trx_perf import (
    emit_summary as trx_emit_summary,
)


class TestAvgP50:
    def test_avg_empty(self) -> None:
        assert _avg([]) == 0.0

    def test_avg_simple(self) -> None:
        assert _avg([1, 2, 3, 4]) == 2.5

    def test_p50_empty(self) -> None:
        assert _p50([]) == 0.0

    def test_p50_odd(self) -> None:
        assert _p50([1, 2, 3]) == 2.0

    def test_p50_even(self) -> None:
        assert _p50([1, 2, 3, 4]) == 2.5


class TestScanRunningStats:
    def test_record_detections_categorises(self) -> None:
        s = RunningStats()
        s.record_detections(
            [
                {"ratio": 50.0, "chirp_slope": 7800, "sf": 8},
                {"ratio": 0.0, "chirp_slope": 7800, "sf": 8},  # ratio=0 skipped
                {"ratio": 75.5, "chirp_slope": 488, "sf": 12},
            ]
        )
        # ratio==0 only skips the ratio buffer; SF + slope are still
        # counted because the legacy intent is to track every detection.
        assert s.det_ratios == [50.0, 75.5]
        assert s.det_sfs == {"SF8": 2, "SF12": 1}
        # 7800 -> "8k" (rounded), 488 -> "488"
        assert s.det_slopes == {"8k": 2, "488": 1}

    def test_record_sweep_appends(self) -> None:
        s = RunningStats()
        s.record_sweep(dur_ms=500, hot=2, det=1, ovf=0)
        s.record_sweep(dur_ms=600, hot=1, det=0, ovf=3)
        assert s.sweep_durations == [500, 600]
        assert s.hot_counts == [2, 1]
        assert s.detection_counts == [1, 0]
        assert s.total_overflows == 3  # last write wins (legacy behaviour)


class TestScanEmit:
    def test_emit_sweep_format(self) -> None:
        msg: dict[str, Any] = {
            "duration_ms": 524,
            "hot_count": 3,
            "detections": 1,
            "overflows": 0,
            "sweep": 7,
            "l1_snapshots": 14,
        }
        s = scan_emit_sweep(msg)
        assert "SWEEP sweep=7" in s
        assert "dur=524ms" in s
        assert "hot=3" in s
        assert "det=1" in s
        assert "ovf=0" in s
        assert "l1=14" in s

    def test_emit_summary_zero(self) -> None:
        assert scan_emit_summary(RunningStats()) == "SUMMARY sweeps=0"

    def test_emit_summary_includes_stats(self) -> None:
        s = RunningStats()
        s.record_sweep(500, 2, 1, 0)
        s.record_sweep(600, 1, 0, 3)
        s.record_detections([{"ratio": 50.0, "chirp_slope": 0, "sf": 8}])
        out = scan_emit_summary(s)
        assert "sweeps=2" in out
        assert "avg_dur=550ms" in out
        assert "min_dur=500ms" in out
        assert "max_dur=600ms" in out
        assert "total_det=1" in out
        assert "SF8:1" in out


class TestTrxFormatBw:
    def test_kilohertz_with_decimal(self) -> None:
        assert format_bw(62500) == "62.5k"

    def test_kilohertz_round(self) -> None:
        assert format_bw(125000) == "125k"

    def test_subkilohertz(self) -> None:
        assert format_bw(500) == "500"


class TestTrxEmitFrame:
    def test_minimal(self) -> None:
        msg: dict[str, Any] = {
            "seq": 1,
            "phy": {
                "sf": 8,
                "bw": 62500,
                "snr_db": 12.3,
                "channel_freq": 869_618_000.0,
            },
            "crc_valid": True,
        }
        s = trx_emit_frame(msg)
        assert "FRAME seq=1" in s
        assert "SF8" in s
        assert "BW62.5k" in s
        assert "CRC=OK" in s
        assert "SNR=12.3" in s
        assert "freq=869.618" in s

    def test_crc_fail(self) -> None:
        msg: dict[str, Any] = {
            "seq": 2,
            "phy": {"sf": 12, "bw": 62500, "snr_db": -5.0},
            "crc_valid": False,
        }
        assert "CRC=FAIL" in trx_emit_frame(msg)

    def test_optional_fields(self) -> None:
        msg: dict[str, Any] = {
            "seq": 3,
            "phy": {
                "sf": 8,
                "bw": 62500,
                "snr_db": 10.0,
                "cfo_int": -2,
                "cfo_frac": 0.123,
                "sfo_hat": 1.234,
                "decode_bw": 125000,
            },
            "crc_valid": True,
            "payload": b"hello",
        }
        s = trx_emit_frame(msg)
        assert "cfo_int=-2" in s
        assert "cfo_frac=0.123" in s
        assert "sfo=1.234" in s
        assert "dec_bw=125k" in s
        assert "len=5" in s


class TestTrxEmitSummary:
    def test_zero(self) -> None:
        assert trx_emit_summary(FrameStats()) == "SUMMARY frames=0"

    def test_nonzero(self) -> None:
        s = FrameStats()
        s.frame_count = 10
        s.crc_ok = 8
        s.crc_fail = 2
        s.snr_values = [10.0, 12.0, 11.0]
        s.sf_counts = {7: 3, 8: 7}
        out = trx_emit_summary(s)
        assert "frames=10" in out
        assert "crc_ok=8" in out
        assert "crc_fail=2" in out
        assert "avg_snr=11.0" in out
        assert "SF7:3" in out
        assert "SF8:7" in out


class TestWidebandStats:
    def test_defaults(self) -> None:
        wb = WidebandStats()
        assert wb.sweep_count == 0
        assert wb.tainted_count == 0
        assert wb.sweep_durations == []
        assert wb.hot_counts == []
