#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Tests for ``lora.hwtests.harness`` — IO-light pieces only.

Subprocess spawners and the MeshCoreCompanion class are exercised by
the live hardware harness; they're not unit-tested here. The pieces
that do not require external processes (event collector + readiness
probes) are covered.
"""

from __future__ import annotations

import socket
import threading
import time

import cbor2
import pytest

from lora.hwtests.harness import (
    EventCollector,
    assert_alive,
    assert_all_alive,
    companion_apply_and_advert,
    start_process,
    stop_process,
    wait_tcp,
    wait_udp,
)
from lora.hwtests.matrix import ConfigPoint


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestWaitTcp:
    def test_returns_true_when_port_open(self) -> None:
        port = _free_port()
        srv = socket.socket()
        srv.bind(("127.0.0.1", port))
        srv.listen(1)
        try:
            assert wait_tcp(port, timeout=2.0)
        finally:
            srv.close()

    def test_returns_false_when_port_closed(self) -> None:
        # 1 is well-known privileged; an unused free port is safer.
        port = _free_port()
        # Don't bind anything. Probe should time out.
        assert not wait_tcp(port, timeout=1.5)


class TestWaitUdp:
    def test_returns_true_when_subscribe_ack(self) -> None:
        port = _free_port()
        srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        srv.bind(("127.0.0.1", port))

        def _echo() -> None:
            srv.settimeout(3.0)
            try:
                _data, addr = srv.recvfrom(65536)
                # Reply with any bytes (wait_udp doesn't validate the ack body).
                srv.sendto(cbor2.dumps({"type": "ack"}), addr)
            except Exception:
                pass

        t = threading.Thread(target=_echo, daemon=True)
        t.start()
        try:
            assert wait_udp(port, timeout=3.0)
        finally:
            srv.close()
            t.join(timeout=1.0)

    def test_returns_false_when_silent(self) -> None:
        port = _free_port()
        # Bind but never reply.
        srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        srv.bind(("127.0.0.1", port))
        try:
            assert not wait_udp(port, timeout=1.5)
        finally:
            srv.close()


class TestEventCollector:
    def test_collects_typed_events(self) -> None:
        port = _free_port()
        recv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        recv.bind(("127.0.0.1", port))
        send = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        c = EventCollector(recv, event_types={"lora_frame"})
        c.start()
        try:
            send.sendto(
                cbor2.dumps({"type": "lora_frame", "n": 1}), ("127.0.0.1", port)
            )
            send.sendto(
                cbor2.dumps({"type": "scan_spectrum", "n": 2}), ("127.0.0.1", port)
            )
            send.sendto(
                cbor2.dumps({"type": "lora_frame", "n": 3}), ("127.0.0.1", port)
            )
            time.sleep(1.0)
            evs = c.drain()
        finally:
            c.stop()
            recv.close()
            send.close()
        ns = sorted(e["n"] for e in evs)
        assert ns == [1, 3]  # scan_spectrum filtered out

    def test_filter_none_keeps_all_dict_events(self) -> None:
        port = _free_port()
        recv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        recv.bind(("127.0.0.1", port))
        send = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        c = EventCollector(recv, event_types=None)
        c.start()
        try:
            send.sendto(cbor2.dumps({"type": "a"}), ("127.0.0.1", port))
            send.sendto(cbor2.dumps({"type": "b"}), ("127.0.0.1", port))
            send.sendto(cbor2.dumps([1, 2, 3]), ("127.0.0.1", port))  # not a dict
            time.sleep(1.0)
            evs = c.drain()
        finally:
            c.stop()
            recv.close()
            send.close()
        types = sorted(e["type"] for e in evs)
        assert types == ["a", "b"]  # list dropped

    def test_drain_empties_buffer(self) -> None:
        port = _free_port()
        recv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        recv.bind(("127.0.0.1", port))
        send = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        c = EventCollector(recv, event_types=None)
        c.start()
        try:
            send.sendto(cbor2.dumps({"type": "x"}), ("127.0.0.1", port))
            time.sleep(1.0)
            first = c.drain()
            second = c.drain()
        finally:
            c.stop()
            recv.close()
            send.close()
        assert len(first) == 1
        assert second == []


class TestStartStopProcess:
    def test_round_trip_with_short_lived_python(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        log = tmp_path / "log.txt"
        proc = start_process(
            ["python3", "-c", "import sys; sys.stderr.write('hi'); sys.exit(0)"],
            str(log),
        )
        proc.wait(timeout=5.0)
        assert proc.returncode == 0
        assert "hi" in log.read_text()

    def test_stop_handles_none(self) -> None:
        # Should be a no-op, never raise.
        stop_process(None)

    def test_stop_kills_long_running(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        log = tmp_path / "log2.txt"
        proc = start_process(
            ["python3", "-c", "import time; time.sleep(60)"],
            str(log),
        )
        time.sleep(0.3)
        stop_process(proc)
        # SIGINT + 5 s wait inside stop_process; if we got here it's done.
        assert proc.returncode is not None


class TestAssertAlive:
    @pytest.fixture(autouse=True)
    def _isolated_proc_registry(self):  # type: ignore[no-untyped-def]
        """Snapshot + restore harness's process registry around each test.

        ``_LIVE_PROCS`` is module-level so unrelated tests in this file
        can leak entries into it. Without isolation, ``assert_all_alive``
        would trip on those zombies before reaching this class's
        fixtures. Snapshot the lists, swap empty ones in, restore after.
        """
        from lora.hwtests import harness as _h

        live_snap = list(_h._LIVE_PROCS)
        log_snap = dict(_h._PROC_LOGS)
        _h._LIVE_PROCS.clear()
        _h._PROC_LOGS.clear()
        try:
            yield
        finally:
            _h._LIVE_PROCS.clear()
            _h._LIVE_PROCS.extend(live_snap)
            _h._PROC_LOGS.clear()
            _h._PROC_LOGS.update(log_snap)

    def test_none_is_noop(self) -> None:
        # No exit, no SystemExit — even with no stderr capture.
        assert_alive(None)

    def test_running_proc_is_noop(self, tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
        log = tmp_path / "alive.log"
        proc = start_process(
            ["python3", "-c", "import time; time.sleep(60)"],
            str(log),
            role="alive_test",
        )
        try:
            assert_alive(proc)  # must not raise
            captured = capsys.readouterr()
            assert "died" not in captured.err.lower()
        finally:
            stop_process(proc)

    def test_dead_proc_exits_with_log_pointer(self, tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
        log = tmp_path / "dead.log"
        proc = start_process(
            ["python3", "-c", "import sys; sys.stderr.write('boom'); sys.exit(7)"],
            str(log),
            role="canary",
        )
        proc.wait(timeout=5.0)
        with pytest.raises(SystemExit) as ei:
            assert_alive(proc)
        assert ei.value.code == 1
        captured = capsys.readouterr()
        assert "canary died (exit=7)" in captured.err
        assert str(log) in captured.err

    def test_assert_all_alive_aborts_on_any_dead(self, tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
        log_a = tmp_path / "alive.log"
        log_b = tmp_path / "dead.log"
        alive = start_process(
            ["python3", "-c", "import time; time.sleep(60)"],
            str(log_a),
            role="alive",
        )
        dead = start_process(
            ["python3", "-c", "import sys; sys.exit(3)"],
            str(log_b),
            role="dead_role",
        )
        dead.wait(timeout=5.0)
        try:
            with pytest.raises(SystemExit) as ei:
                assert_all_alive()
            assert ei.value.code == 1
            captured = capsys.readouterr()
            assert "dead_role died (exit=3)" in captured.err
        finally:
            stop_process(alive)


class _FakeCompanion:
    """Minimal stand-in for ``CompanionDriver`` — no subprocesses.

    Records every call so tests can assert on order and values. The
    flag attributes (``set_radio_ok`` / ``set_tx_power_ok`` /
    ``advert_results``) let the test pick which call sequence to
    simulate.
    """

    def __init__(
        self,
        *,
        set_radio_ok: bool = True,
        set_tx_power_ok: bool = True,
        advert_results: list[bool] | None = None,
    ) -> None:
        self.set_radio_ok = set_radio_ok
        self.set_tx_power_ok = set_tx_power_ok
        self._advert_results = list(advert_results or [True, True, True])
        self.calls: list[tuple[str, tuple, dict]] = []
        self._advert_idx = 0

    def set_radio(self, freq_mhz, bw_khz, sf, cr=8):  # type: ignore[no-untyped-def]
        self.calls.append(("set_radio", (freq_mhz, bw_khz, sf), {"cr": cr}))
        return self.set_radio_ok

    def set_tx_power(self, dbm):  # type: ignore[no-untyped-def]
        self.calls.append(("set_tx_power", (dbm,), {}))
        return self.set_tx_power_ok

    def send_advert(self):  # type: ignore[no-untyped-def]
        idx = self._advert_idx
        self._advert_idx += 1
        if idx < len(self._advert_results):
            ok = self._advert_results[idx]
        else:
            ok = False
        self.calls.append(("send_advert", (), {"idx": idx, "ok": ok}))
        return ok


class TestCompanionApplyAndAdvert:
    """Unit-tests for the shared decode/transmit helper.

    Times out the sleeps to keep the suite fast.
    """

    def test_set_radio_failure_returns_early(self) -> None:
        comp = _FakeCompanion(set_radio_ok=False)
        point = ConfigPoint(sf=8, bw=62500, freq_mhz=869.618)
        ok, count = companion_apply_and_advert(
            point,
            comp,  # type: ignore[arg-type]
            settle_s=0.0,
            gap_s=0.0,
        )
        assert ok is False
        assert count == 0
        # Only set_radio was attempted; tx_power / sends never reached.
        assert [c[0] for c in comp.calls] == ["set_radio"]

    def test_full_burst_counts_acks(self) -> None:
        comp = _FakeCompanion(advert_results=[True, False, True])
        point = ConfigPoint(sf=9, bw=125000, freq_mhz=869.618, tx_power=10)
        ok, count = companion_apply_and_advert(
            point,
            comp,  # type: ignore[arg-type]
            tx_repeats=3,
            settle_s=0.0,
            gap_s=0.0,
        )
        assert ok is True
        assert count == 2
        names = [c[0] for c in comp.calls]
        assert names == [
            "set_radio",
            "set_tx_power",
            "send_advert",
            "send_advert",
            "send_advert",
        ]

    def test_pre_send_hook_runs_after_settle(self) -> None:
        comp = _FakeCompanion()
        point = ConfigPoint(sf=7, bw=62500, freq_mhz=869.618)
        events: list[str] = []

        def hook() -> None:
            events.append("drained")

        ok, count = companion_apply_and_advert(
            point,
            comp,  # type: ignore[arg-type]
            tx_repeats=1,
            settle_s=0.0,
            gap_s=0.0,
            pre_send=hook,
        )
        assert ok is True
        assert count == 1
        assert events == ["drained"]
        # Hook fired between set_tx_power and the first send_advert.
        names = [c[0] for c in comp.calls]
        assert names == ["set_radio", "set_tx_power", "send_advert"]

    def test_set_tx_power_failure_does_not_abort(self) -> None:
        comp = _FakeCompanion(set_tx_power_ok=False)
        point = ConfigPoint(sf=8, bw=62500, freq_mhz=869.618)
        ok, count = companion_apply_and_advert(
            point,
            comp,  # type: ignore[arg-type]
            tx_repeats=2,
            settle_s=0.0,
            gap_s=0.0,
        )
        # set_tx_power returns False but the helper continues to TX.
        assert ok is True
        assert count == 2

    def test_passes_freq_bw_sf_cr_to_companion(self) -> None:
        comp = _FakeCompanion()
        point = ConfigPoint(sf=11, bw=250000, freq_mhz=869.618, tx_power=14, cr=5)
        companion_apply_and_advert(
            point,
            comp,  # type: ignore[arg-type]
            tx_repeats=1,
            settle_s=0.0,
            gap_s=0.0,
        )
        # First call should be set_radio with the matrix-point values
        # converted (bw Hz -> kHz). cr threads through as a kwarg.
        name, args, kwargs = comp.calls[0]
        assert name == "set_radio"
        assert args == (869.618, 250.0, 11)
        assert kwargs == {"cr": 5}
        # Second call: set_tx_power(14).
        assert comp.calls[1] == ("set_tx_power", (14,), {})
