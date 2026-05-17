#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Tests for ``lora.hwtests.decode_test._compute_matchup``."""

from __future__ import annotations

from typing import Any

from lora.hwtests.decode_test import _collect_decode, _compute_matchup
from lora.hwtests.report import DutPointResult

TX_FREQ_HZ = 869.618e6


def _frame(
    *,
    payload_hash: int | None = 0xDEADBEEF,
    snr: float | None = 12.0,
    sf: int = 8,
    bw: int = 62500,
    crc_ok: bool = True,
    freq_hz: float = TX_FREQ_HZ,
    ts: str | None = None,
) -> dict[str, Any]:
    return {
        "type": "lora_frame",
        "phy": {"channel_freq": freq_hz, "snr_db": snr, "sf": sf, "bw": bw},
        "carrier": {"sf": sf, "bw": bw},
        "crc_valid": crc_ok,
        "payload_hash": payload_hash,
        "ts": ts,
    }


def _ts(seconds_after_epoch: float) -> str:
    """Build an ISO-8601 ``ts`` string at offset ``seconds_after_epoch``
    from a fixed reference instant."""
    from datetime import datetime, timezone

    base = datetime(2026, 5, 9, 22, 31, 42, tzinfo=timezone.utc)
    t = base.timestamp() + seconds_after_epoch
    return (
        datetime.fromtimestamp(t, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _build_dut(label: str, frames: list[dict[str, Any]]) -> DutPointResult:
    dpr = DutPointResult(label=label)
    _collect_decode(dpr, frames, TX_FREQ_HZ)
    return dpr


class TestComputeMatchupSingleDut:
    def test_no_matchup_for_single_dut(self) -> None:
        only = _build_dut("only", [_frame()])
        m = _compute_matchup({"only": only})
        assert m.both_heard == 0
        assert m.only_by == {}
        assert m.snr_better == {}


class TestComputeMatchupTwoDut:
    def test_both_heard_same_payload(self) -> None:
        a = _build_dut("a", [_frame(payload_hash=1, snr=10.0)])
        b = _build_dut("b", [_frame(payload_hash=1, snr=8.0)])
        m = _compute_matchup({"a": a, "b": b})
        assert m.both_heard == 1
        assert m.only_by == {}
        assert m.snr_better == {"a": 1}
        # snr_delta_avg has key "a_over_b" with delta 10-8=2
        assert m.snr_delta_avg == {"a_over_b": 2.0}

    def test_exclusive_hear(self) -> None:
        a = _build_dut("a", [_frame(payload_hash=1)])
        b = _build_dut("b", [_frame(payload_hash=2)])
        m = _compute_matchup({"a": a, "b": b})
        assert m.both_heard == 0
        assert m.only_by == {"a": 1, "b": 1}
        assert m.snr_better == {}

    def test_mixed_exclusive_and_both(self) -> None:
        a = _build_dut(
            "a",
            [_frame(payload_hash=1, snr=10.0), _frame(payload_hash=99)],
        )
        b = _build_dut("b", [_frame(payload_hash=1, snr=12.0)])
        m = _compute_matchup({"a": a, "b": b})
        assert m.both_heard == 1
        assert m.only_by == {"a": 1}
        assert m.snr_better == {"b": 1}

    def test_frames_without_payload_hash_skipped(self) -> None:
        a = _build_dut("a", [_frame(payload_hash=None)])
        b = _build_dut("b", [_frame(payload_hash=None)])
        m = _compute_matchup({"a": a, "b": b})
        assert m.both_heard == 0
        assert m.only_by == {}
        assert m.snr_better == {}

    def test_snr_delta_only_for_two_dut(self) -> None:
        a = _build_dut("a", [_frame(payload_hash=1, snr=10.0)])
        b = _build_dut("b", [_frame(payload_hash=1, snr=8.0)])
        c = _build_dut("c", [_frame(payload_hash=1, snr=6.0)])
        m = _compute_matchup({"a": a, "b": b, "c": c})
        # 3-DUT: snr_delta_avg stays empty.
        assert m.snr_delta_avg == {}
        # All three heard hash=1, so both_heard=1 and best snr is 'a'.
        assert m.both_heard == 1
        assert m.snr_better == {"a": 1}

    def test_missing_snr_does_not_count_as_better(self) -> None:
        a = _build_dut("a", [_frame(payload_hash=1, snr=None)])
        b = _build_dut("b", [_frame(payload_hash=1, snr=5.0)])
        m = _compute_matchup({"a": a, "b": b})
        assert m.both_heard == 1
        # snr_better skipped when any DUT lacks snr
        assert m.snr_better == {}
        # delta uses only frames where both have snr — none here
        assert m.snr_delta_avg == {}


class TestComputeMatchupTimeAxis:
    """The time-window axis matches concurrent decodes regardless of
    payload_hash divergence (the typical CRC-fail-on-one-side case)."""

    def test_default_window_is_100ms(self) -> None:
        a = _build_dut("a", [_frame(payload_hash=1)])
        b = _build_dut("b", [_frame(payload_hash=1)])
        m = _compute_matchup({"a": a, "b": b})
        assert m.time_window_ms == 100

    def test_concurrent_decode_with_different_hashes(self) -> None:
        # Same RF event but Pluto bit-errored → different post-decode
        # bytes → different payload_hash. payload_hash axis misses it.
        # time-window axis catches it via the 100 ms cluster.
        a = _build_dut(
            "a",
            [_frame(payload_hash=1, snr=12.0, ts=_ts(0.0))],
        )
        b = _build_dut(
            "b",
            [_frame(payload_hash=999, snr=4.0, ts=_ts(0.020))],  # 20 ms later
        )
        m = _compute_matchup({"a": a, "b": b})
        # payload_hash axis sees them as exclusive
        assert m.both_heard == 0
        assert m.only_by == {"a": 1, "b": 1}
        # time axis sees them as one cluster
        assert m.time_both_heard == 1
        assert m.time_only_by == {}
        assert m.time_snr_better == {"a": 1}
        assert m.time_snr_delta_avg == {"a_over_b": 8.0}

    def test_outside_window_treated_as_separate(self) -> None:
        a = _build_dut("a", [_frame(payload_hash=1, ts=_ts(0.0))])
        b = _build_dut("b", [_frame(payload_hash=2, ts=_ts(5.0))])  # 5 s later
        m = _compute_matchup({"a": a, "b": b})
        assert m.time_both_heard == 0
        assert m.time_only_by == {"a": 1, "b": 1}

    def test_missing_ts_skipped_on_time_axis(self) -> None:
        # No ts → can't be clustered by time. Still works on payload_hash axis.
        a = _build_dut("a", [_frame(payload_hash=1, ts=None)])
        b = _build_dut("b", [_frame(payload_hash=1, ts=None)])
        m = _compute_matchup({"a": a, "b": b})
        assert m.both_heard == 1  # payload_hash matched
        assert m.time_both_heard == 0  # ts missing → no time cluster

    def test_dual_rx_channel_collapsed_into_one_cluster(self) -> None:
        # Single-DUT case where two RX channels both decode the same packet.
        # Even with two DUTs, the same-DUT entries should fold into one
        # cluster (they share a label) and the cluster still counts as
        # "heard by that DUT".
        a = _build_dut(
            "a",
            [
                _frame(payload_hash=1, snr=10.0, ts=_ts(0.0)),
                _frame(payload_hash=1, snr=8.0, ts=_ts(0.005)),  # ch1, 5 ms
            ],
        )
        b = _build_dut("b", [_frame(payload_hash=1, snr=6.0, ts=_ts(0.010))])
        m = _compute_matchup({"a": a, "b": b})
        assert m.time_both_heard == 1
        # a's best snr in cluster is 10.0 vs b's 6.0
        assert m.time_snr_better == {"a": 1}
        assert m.time_snr_delta_avg == {"a_over_b": 4.0}

    def test_window_override(self) -> None:
        a = _build_dut("a", [_frame(payload_hash=1, ts=_ts(0.0))])
        b = _build_dut("b", [_frame(payload_hash=2, ts=_ts(0.500))])  # 500 ms
        m = _compute_matchup({"a": a, "b": b}, time_window_ms=1000)
        assert m.time_window_ms == 1000
        # 500 ms < 1000 ms window → clustered
        assert m.time_both_heard == 1
