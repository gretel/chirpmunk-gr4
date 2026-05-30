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
import pytest

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
from lora.hwtests.test_config import DutConfig


def _solo_collectors(
    collector: EventCollector, label: str = "default"
) -> list[tuple[DutConfig, EventCollector]]:
    """Wrap a single EventCollector in the multi-DUT collectors list shape."""
    return [
        (
            DutConfig(label=label, binary="b", config_file="c", port=0),
            collector,
        )
    ]


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
    """Build a MeshCoreCompanion-shaped stub for _run_*_point tests.

    Provides both sync and async methods for transition compatibility.
    Async methods return awaitables for decode/transmit paths.
    Sync methods return values directly for scan path.
    """
    import asyncio
    stub = MagicMock()
    calls: list[tuple] = []
    adv_idx = 0
    adv_results = [True] * tx_repeats_ok + [False] * 10
    # Async versions track calls in `calls` list
    async def _sr(name, *a, **kw):
        calls.append((name, a, kw))
        return set_radio
    async def _st(name, *a, **kw):
        calls.append((name, a, kw))
        return set_tx
    async def _sa(name, *a, **kw):
        nonlocal adv_idx
        idx = adv_idx
        adv_idx += 1
        ok = adv_results[idx] if idx < len(adv_results) else False
        calls.append(("send_advert", (), {"idx": idx, "ok": ok}))
        return ok
    # Wrap each method to pass its name
    stub.set_radio = lambda *a, **kw: _sr("set_radio", *a, **kw)
    stub.set_tx_power = lambda *a, **kw: _st("set_tx_power", *a, **kw)
    stub.send_advert = lambda *a, **kw: _sa("send_advert", *a, **kw)
    stub.connect = lambda *a, **kw: _sr("connect", *a, **kw)
    stub.get_radio = lambda *a, **kw: _st("get_radio", *a, **kw)
    stub.close = lambda *a, **kw: _sa("close", *a, **kw)
    stub._calls = calls
    stub._adv_idx_ref = lambda: adv_idx
    return stub


def _free_port() -> tuple[socket.socket, int]:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    return s, s.getsockname()[1]


class TestRunDecodePoint:
    @pytest.mark.asyncio
    async def test_set_radio_failure_short_circuits(self, monkeypatch: Any) -> None:
        monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
        comp = _make_stub_companion(set_radio=False)
        recv, _port = _free_port()
        try:
            collector = EventCollector(recv, {"lora_frame"})
            collector.start()
            try:
                p = ConfigPoint(sf=8, bw=62500, freq_mhz=869.618)
                result = await _run_decode_point(p, comp, _solo_collectors(collector))
            finally:
                collector.stop()
        finally:
            recv.close()
        assert result.tx_ok is False
        assert result.frames == []
        assert "set_radio" in {c[0] for c in comp._calls}

    @pytest.mark.asyncio
    async def test_send_advert_called_three_times(self, monkeypatch: Any) -> None:
        monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
        comp = _make_stub_companion()
        recv, _port = _free_port()
        try:
            collector = EventCollector(recv, {"lora_frame"})
            collector.start()
            try:
                p = ConfigPoint(sf=8, bw=62500, freq_mhz=869.618)
                result = await _run_decode_point(p, comp, _solo_collectors(collector))
            finally:
                collector.stop()
        finally:
            recv.close()
        assert result.tx_ok is True
        sent = [c for c in comp._calls if c[0] == "send_advert"]
        assert len(sent) == 3
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
