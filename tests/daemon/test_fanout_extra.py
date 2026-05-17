# SPDX-License-Identifier: ISC
"""Additional fanout tests: telemetry dispatch, subscriber management, status/spectrum broadcast."""

from __future__ import annotations

import asyncio
import socket
import time
from pathlib import Path

import cbor2
import duckdb
import pytest

from lora.core.types import (
    Carrier,
    LoraFrame,
    LoraTxAck,
    MultisfDetect,
    MultisfFrame,
    MultisfSync,
    Phy,
    Spectrum,
    Status,
    StatusFrames,
    StatusPhy,
    WidebandSlot,
    WidebandSweep,
)
from lora.daemon.fanout import (
    FanOut,
    UdpBroadcaster,
    lora_tx_ack_to_cbor_dict,
    spectrum_to_cbor_dict,
    status_to_cbor_dict,
)
from lora.storage import DuckDBWriter, StorageConfig


def _frame(
    *,
    seq: int = 1,
    sync_word: int = 0x12,
    source: str = "radio_868",
    payload: bytes = b"hello",
) -> LoraFrame:
    return LoraFrame(
        type="lora_frame",
        ts="2026-01-01T00:00:00+00:00",
        seq=seq,
        payload=payload,
        payload_len=len(payload),
        crc_valid=True,
        cr=4,
        is_downchirp=False,
        payload_hash=0xCAFEBABE + seq,
        id=f"00000000-0000-0000-0000-{seq:012d}",
        phy=Phy(sf=8, bw=62500, cr=4, sync_word=sync_word, crc_valid=True, snr_db=5.0),
        carrier=Carrier(sync_word=sync_word, sf=8, bw=62500, cr=4, ldro_cfg=False),
        source=source,
    )


def _make_udp_pair() -> tuple[socket.socket, socket.socket]:
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.bind(("127.0.0.1", 0))
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    return sender, receiver


# ---------------------------------------------------------------------------
# UdpBroadcaster — subscriber management
# ---------------------------------------------------------------------------


def test_touch_subscriber_updates_last_seen() -> None:
    sender, _ = _make_udp_pair()
    try:
        bc = UdpBroadcaster(sender)
        bc.add_subscriber(("127.0.0.1", 9999), now=100.0)
        bc.touch_subscriber(("127.0.0.1", 9999), now=200.0)
        sub = bc._subs[("127.0.0.1", 9999)]  # noqa: SLF001
        assert sub.last_seen == 200.0
    finally:
        sender.close()


def test_touch_subscriber_unknown_addr_noop() -> None:
    sender, _ = _make_udp_pair()
    try:
        bc = UdpBroadcaster(sender)
        bc.touch_subscriber(("127.0.0.1", 9999), now=100.0)
    finally:
        sender.close()


def test_remove_subscriber() -> None:
    sender, _ = _make_udp_pair()
    try:
        bc = UdpBroadcaster(sender)
        bc.add_subscriber(("127.0.0.1", 9999), now=0.0)
        assert bc.subscriber_count == 1
        bc.remove_subscriber(("127.0.0.1", 9999))
        assert bc.subscriber_count == 0
    finally:
        sender.close()


def test_remove_unknown_subscriber_noop() -> None:
    sender, _ = _make_udp_pair()
    try:
        bc = UdpBroadcaster(sender)
        bc.remove_subscriber(("127.0.0.1", 9999))
    finally:
        sender.close()


def test_reap_stale_evicts_nothing_when_all_fresh() -> None:
    sender, _ = _make_udp_pair()
    try:
        bc = UdpBroadcaster(sender)
        bc.add_subscriber(("127.0.0.1", 9999), now=100.0)
        n = bc.reap_stale(now=110.0, max_age_s=60.0)
        assert n == 0
        assert bc.subscriber_count == 1
    finally:
        sender.close()


def test_broadcaster_sends_status() -> None:
    sender, receiver = _make_udp_pair()
    try:
        bc = UdpBroadcaster(sender)
        bc.add_subscriber(receiver.getsockname())
        status = Status(
            type="status",
            ts="2026-01-01T00:00:00+00:00",
            phy=StatusPhy(rx_gain=40.0, tx_gain=70.0),
            frames=StatusFrames(total=10, crc_ok=9, crc_fail=1),
            rx_overflows=0,
        )
        bc.broadcast_status(status)
        receiver.settimeout(1.0)
        data, _ = receiver.recvfrom(65536)
        msg = cbor2.loads(data)
        assert msg["type"] == "status"
    finally:
        sender.close()
        receiver.close()


def test_broadcaster_sends_spectrum() -> None:
    sender, receiver = _make_udp_pair()
    try:
        bc = UdpBroadcaster(sender)
        bc.add_subscriber(receiver.getsockname())
        spec = Spectrum(
            type="spectrum",
            bins=b"\x00" * 1024,
            fft_size=256,
            center_freq=869618000.0,
            sample_rate=250000.0,
        )
        bc.broadcast_spectrum(spec)
        receiver.settimeout(1.0)
        data, _ = receiver.recvfrom(65536)
        msg = cbor2.loads(data)
        assert msg["type"] == "spectrum"
    finally:
        sender.close()
        receiver.close()


def test_broadcaster_sends_tx_ack_to_specific_addr() -> None:
    sender, receiver = _make_udp_pair()
    try:
        bc = UdpBroadcaster(sender)
        ack = LoraTxAck(type="lora_tx_ack", seq=42, ok=True, error=None)
        bc.broadcast_tx_ack(ack, receiver.getsockname())
        receiver.settimeout(1.0)
        data, _ = receiver.recvfrom(65536)
        msg = cbor2.loads(data)
        assert msg["type"] == "lora_tx_ack"
        assert msg["seq"] == 42
        assert msg["ok"] is True
    finally:
        sender.close()
        receiver.close()


def test_set_upstream_config_caches_bytes() -> None:
    sender, _ = _make_udp_pair()
    try:
        bc = UdpBroadcaster(sender)
        bc.set_upstream_config({"type": "config", "sf": 8})
        assert bc._upstream_config_bytes is not None  # noqa: SLF001
    finally:
        sender.close()


def test_upstream_config_replayed_on_new_subscriber() -> None:
    sender, receiver = _make_udp_pair()
    try:
        bc = UdpBroadcaster(sender)
        bc.set_upstream_config({"type": "config", "sf": 8})
        bc.add_subscriber(receiver.getsockname())
        receiver.settimeout(1.0)
        data, _ = receiver.recvfrom(65536)
        msg = cbor2.loads(data)
        assert msg["type"] == "config"
    finally:
        sender.close()
        receiver.close()


# ---------------------------------------------------------------------------
# Telemetry dispatch via FanOut._dispatch
# ---------------------------------------------------------------------------


def _make_fanout(sender: socket.socket) -> FanOut:
    unused = StorageConfig(db_path=Path("/tmp/_unused.duckdb"))
    writer = DuckDBWriter(unused)
    bc = UdpBroadcaster(sender)
    return FanOut(queue_depth=64, writer=writer, broadcaster=bc)


def test_dispatch_spectrum_telemetry_broadcasts_only() -> None:
    sender, receiver = _make_udp_pair()
    try:
        fanout = _make_fanout(sender)
        fanout._broadcaster.add_subscriber(receiver.getsockname())  # noqa: SLF001
        spec = Spectrum(
            type="spectrum",
            bins=b"\x00" * 1024,
            fft_size=256,
            center_freq=869618000.0,
            sample_rate=250000.0,
        )
        fanout._dispatch_telemetry("radio_868", spec)  # noqa: SLF001
        receiver.settimeout(1.0)
        data, _ = receiver.recvfrom(65536)
        msg = cbor2.loads(data)
        assert msg["type"] == "spectrum"
    finally:
        sender.close()
        receiver.close()


def _read(db: Path, sql: str) -> list[tuple[object, ...]]:
    con = duckdb.connect(str(db))
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def test_dispatch_multisf_detect(db_path: Path) -> None:
    sender, _ = _make_udp_pair()
    writer = DuckDBWriter(StorageConfig(db_path=db_path, batch_rows=1))
    writer.start()
    try:
        fanout = _make_fanout(sender)
        fanout._writer = writer  # noqa: SLF001
        event = MultisfDetect(
            type="multisf_detect", ts="2026-01-01T00:00:00Z", sf=8, bin=42
        )
        fanout._dispatch_telemetry("radio_868", event)  # noqa: SLF001
        writer.checkpoint()
    finally:
        writer.stop(timeout=2.0)
        sender.close()
    rows = _read(db_path, "SELECT count(*) FROM multisf_detect")
    assert rows == [(1,)]


def test_dispatch_multisf_sync(db_path: Path) -> None:
    sender, _ = _make_udp_pair()
    writer = DuckDBWriter(StorageConfig(db_path=db_path, batch_rows=1))
    writer.start()
    try:
        fanout = _make_fanout(sender)
        fanout._writer = writer  # noqa: SLF001
        event = MultisfSync(
            type="multisf_sync",
            ts="2026-01-01T00:00:00Z",
            sf=8,
            cfo_int=1,
            cfo_frac=0.1,
            snr_db=4.0,
        )
        fanout._dispatch_telemetry("radio_868", event)  # noqa: SLF001
        writer.checkpoint()
    finally:
        writer.stop(timeout=2.0)
        sender.close()
    rows = _read(db_path, "SELECT count(*) FROM multisf_sync")
    assert rows == [(1,)]


def test_dispatch_multisf_frame(db_path: Path) -> None:
    sender, _ = _make_udp_pair()
    writer = DuckDBWriter(StorageConfig(db_path=db_path, batch_rows=1))
    writer.start()
    try:
        fanout = _make_fanout(sender)
        fanout._writer = writer  # noqa: SLF001
        event = MultisfFrame(
            type="multisf_frame",
            ts="2026-01-01T00:00:00Z",
            sf=8,
            crc_ok=True,
            len=12,
            cr=4,
            snr_db=4.0,
        )
        fanout._dispatch_telemetry("radio_868", event)  # noqa: SLF001
        writer.checkpoint()
    finally:
        writer.stop(timeout=2.0)
        sender.close()
    rows = _read(db_path, "SELECT count(*) FROM multisf_frame")
    assert rows == [(1,)]


def test_dispatch_wideband_sweep(db_path: Path) -> None:
    sender, _ = _make_udp_pair()
    writer = DuckDBWriter(StorageConfig(db_path=db_path, batch_rows=1))
    writer.start()
    try:
        fanout = _make_fanout(sender)
        fanout._writer = writer  # noqa: SLF001
        event = WidebandSweep(
            type="wideband_sweep",
            ts="2026-01-01T00:00:00Z",
            sweep=1,
            tainted=False,
            overflows=0,
            zero_calls=0,
            total_calls=10,
            duration_ms=42,
            n_snapshots=8,
            n_hot=2,
            n_active=3,
            max_slots=4,
        )
        fanout._dispatch_telemetry("radio_868", event)  # noqa: SLF001
        writer.checkpoint()
    finally:
        writer.stop(timeout=2.0)
        sender.close()
    rows = _read(db_path, "SELECT count(*) FROM wideband_sweep")
    assert rows == [(1,)]


def test_dispatch_wideband_slot(db_path: Path) -> None:
    sender, _ = _make_udp_pair()
    writer = DuckDBWriter(StorageConfig(db_path=db_path, batch_rows=1))
    writer.start()
    try:
        fanout = _make_fanout(sender)
        fanout._writer = writer  # noqa: SLF001
        event = WidebandSlot(
            type="wideband_slot",
            ts="2026-01-01T00:00:00Z",
            action="activate",
            slot=0,
            channel=2,
            freq=8.69618e8,
            bw=62500,
        )
        fanout._dispatch_telemetry("radio_868", event)  # noqa: SLF001
        writer.checkpoint()
    finally:
        writer.stop(timeout=2.0)
        sender.close()
    rows = _read(db_path, "SELECT count(*) FROM wideband_slot")
    assert rows == [(1,)]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "fanout_extra.duckdb"


# ---------------------------------------------------------------------------
# FanOut feed_telemetry via run()
# ---------------------------------------------------------------------------


async def test_fanout_feeds_telemetry_via_run(db_path: Path) -> None:
    sender, _ = _make_udp_pair()
    writer = DuckDBWriter(StorageConfig(db_path=db_path, batch_rows=1, batch_ms=10))
    writer.start()
    try:
        bc = UdpBroadcaster(sender)
        fanout = FanOut(queue_depth=64, writer=writer, broadcaster=bc)
        task = asyncio.create_task(fanout.run())
        detect = MultisfDetect(
            type="multisf_detect", ts="2026-01-01T00:00:00Z", sf=7, bin=10
        )
        fanout.feed_telemetry("radio_868", detect)
        deadline = time.monotonic() + 2.0
        while fanout._queue.qsize() > 0 and time.monotonic() < deadline:  # noqa: SLF001
            await asyncio.sleep(0.01)
        await fanout.stop()
        await asyncio.wait_for(task, timeout=2.0)
        writer.checkpoint()
    finally:
        writer.stop(timeout=2.0)
        sender.close()
    rows = _read(db_path, "SELECT sf, bin FROM multisf_detect")
    assert rows == [(7, 10)]


# ---------------------------------------------------------------------------
# status_to_cbor_dict / spectrum_to_cbor_dict / lora_tx_ack_to_cbor_dict
# ---------------------------------------------------------------------------


def test_status_to_cbor_dict_shape() -> None:
    status = Status(
        type="status",
        ts="2026-01-01T00:00:00+00:00",
        phy=StatusPhy(rx_gain=40.0, tx_gain=70.0),
        frames=StatusFrames(total=100, crc_ok=99, crc_fail=1),
        rx_overflows=0,
    )
    d = status_to_cbor_dict(status)
    assert d["type"] == "status"
    assert d["phy"]["rx_gain"] == 40.0
    assert d["frames"]["total"] == 100
    raw = cbor2.dumps(d)
    parsed = cbor2.loads(raw)
    assert parsed["rx_overflows"] == 0


def test_spectrum_to_cbor_dict_shape() -> None:
    spec = Spectrum(
        type="spectrum",
        bins=b"\x01\x02\x03\x04" * 256,
        fft_size=256,
        center_freq=869618000.0,
        sample_rate=250000.0,
    )
    d = spectrum_to_cbor_dict(spec)
    assert d["type"] == "spectrum"
    assert d["fft_size"] == 256
    raw = cbor2.dumps(d)
    parsed = cbor2.loads(raw)
    assert len(parsed["bins"]) == 1024


def test_lora_tx_ack_to_cbor_dict_shape() -> None:
    ack = LoraTxAck(type="lora_tx_ack", seq=7, ok=False, error="timeout")
    d = lora_tx_ack_to_cbor_dict(ack)
    assert d["type"] == "lora_tx_ack"
    assert d["seq"] == 7
    assert d["ok"] is False
    assert d["error"] == "timeout"
    raw = cbor2.dumps(d)
    parsed = cbor2.loads(raw)
    assert parsed["error"] == "timeout"
