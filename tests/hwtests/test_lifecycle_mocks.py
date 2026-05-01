#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Lifecycle tests with mocked companions / spawners.

These exercise the per-mode runners' non-hardware branches:

* ``spawn_*`` with ``attach=True`` returns ``None`` cleanly.
* ``_run_decode_point`` / ``_run_scan_point`` with a stub companion
  walks through set_radio + drain + collect.
"""

from __future__ import annotations

import socket
from typing import Any
from unittest.mock import MagicMock

from lora.hwtests import harness
from lora.hwtests.decode_test import _run_decode_point
from lora.hwtests.harness import (
    EventCollector,
    spawn_lora_core,
    spawn_meshcore_bridge,
    spawn_sdr_binary,
    spawn_serial_bridge,
)
from lora.hwtests.matrix import ConfigPoint
from lora.hwtests.scan_test import _run_scan_point


class TestSpawnAttachReturnsNone:
    def test_serial_bridge(self) -> None:
        assert spawn_serial_bridge("/dev/null", attach=True) is None

    def test_sdr_binary(self) -> None:
        assert (
            spawn_sdr_binary(
                "binary",
                config_file="x",
                udp_port=0,
                log_path="tmp/x.log",
                attach=True,
            )
            is None
        )

    def test_lora_core(self) -> None:
        assert spawn_lora_core(attach=True) is None

    def test_meshcore_bridge(self) -> None:
        assert spawn_meshcore_bridge(attach=True) is None


def _make_stub_companion(
    *, set_radio: bool = True, set_tx: bool = True, tx_repeats_ok: int = 3
) -> MagicMock:
    """Build a CompanionDriver-shaped stub for _run_*_point tests."""
    stub = MagicMock()
    stub.set_radio.return_value = set_radio
    stub.set_tx_power.return_value = set_tx
    stub.send_advert.side_effect = [True] * tx_repeats_ok + [False] * 10
    return stub


def _free_port() -> tuple[socket.socket, int]:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    return s, s.getsockname()[1]


class TestRunDecodePoint:
    def test_set_radio_failure_short_circuits(self, monkeypatch: Any) -> None:
        # If set_radio returns False, the function returns immediately
        # with tx_ok=False and no events.
        monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
        comp = _make_stub_companion(set_radio=False)
        recv, _port = _free_port()
        try:
            collector = EventCollector(recv, {"lora_frame"})
            collector.start()
            try:
                p = ConfigPoint(sf=8, bw=62500, freq_mhz=869.618)
                # Compress flush_s by stubbing lora_airtime_s? We just
                # rely on monkeypatched time.sleep above.
                result = _run_decode_point(p, comp, collector)
            finally:
                collector.stop()
        finally:
            recv.close()
        assert result.tx_ok is False
        assert result.frames == []
        comp.set_radio.assert_called_once()

    def test_send_advert_called_three_times(self, monkeypatch: Any) -> None:
        # End-to-end frame collection has too much timing slack to test
        # reliably without real hardware (collector.drain at step 4
        # races with the timer-injected event). Just verify the TX
        # repeat loop fires three times.
        monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
        comp = _make_stub_companion()
        recv, _port = _free_port()
        try:
            collector = EventCollector(recv, {"lora_frame"})
            collector.start()
            try:
                p = ConfigPoint(sf=8, bw=62500, freq_mhz=869.618)
                result = _run_decode_point(p, comp, collector)
            finally:
                collector.stop()
        finally:
            recv.close()
        assert result.tx_ok is True
        assert comp.send_advert.call_count == 3


class TestRunScanPoint:
    def test_set_radio_failure_short_circuits(self, monkeypatch: Any) -> None:
        monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
        comp = _make_stub_companion(set_radio=False)
        recv, _port = _free_port()
        try:
            collector = EventCollector(recv, {"scan_spectrum", "scan_sweep_end"})
            collector.start()
            try:
                p = ConfigPoint(sf=8, bw=62500, freq_mhz=869.618)
                result = _run_scan_point(p, comp, collector, tuning_scan=False)
            finally:
                collector.stop()
        finally:
            recv.close()
        assert result.tx_ok is False
        assert result.detections == []


class TestHarnessTimingConstantsPresent:
    """Cheap pin: timing constants must stay accessible after refactors."""

    def test_constants_have_expected_orders_of_magnitude(self) -> None:
        assert 1 < harness.SETTLE_S < 10
        assert 1 < harness.FLUSH_TX_S < 30
        assert 5 <= harness.FLUSH_DECODE_S < 60
        assert 5 <= harness.FLUSH_SCAN_S < 60
        assert harness.FLUSH_SCAN_TUNING_S > harness.FLUSH_SCAN_S
        assert harness.BRIDGE_PORT == 7835
        assert harness.MESHCORE_BRIDGE_PORT == 7834
        assert harness.TRX_PORT == 5556
        assert harness.AGG_PORT == 5555
        assert harness.SCAN_PORT == 5557
        assert harness.MIN_RATIO == 8.0
        assert harness.FREQ_TOL_HZ == 200_000.0
