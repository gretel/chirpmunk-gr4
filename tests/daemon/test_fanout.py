# SPDX-License-Identifier: ISC
"""Tests for :mod:`lora.daemon.fanout`."""

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
    Phy,
    Status,
    StatusFrames,
    StatusPhy,
)
from lora.daemon.fanout import (
    FanOut,
    UdpBroadcaster,
    frame_to_cbor_dict,
)
from lora.storage import DuckDBWriter, StorageConfig

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


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
    """Return (sender, receiver) UDP sockets bound to ephemeral 127.0.0.1 ports."""
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.bind(("127.0.0.1", 0))
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    return sender, receiver


# ---------------------------------------------------------------------------
# frame_to_cbor_dict shape
# ---------------------------------------------------------------------------


def test_frame_to_cbor_dict_round_trips_required_fields() -> None:
    frame = _frame()
    d = frame_to_cbor_dict(frame)
    assert d["type"] == "lora_frame"
    assert d["seq"] == 1
    assert d["source"] == "radio_868"
    assert d["phy"]["sync_word"] == 0x12
    assert d["phy"]["snr_db"] == 5.0
    assert d["carrier"]["sf"] == 8
    raw = cbor2.dumps(d)
    parsed = cbor2.loads(raw)
    assert parsed["payload"] == b"hello"


# ---------------------------------------------------------------------------
# UdpBroadcaster
# ---------------------------------------------------------------------------


def test_broadcaster_sends_to_subscribed_client() -> None:
    sender, receiver = _make_udp_pair()
    try:
        bc = UdpBroadcaster(sender)
        bc.add_subscriber(receiver.getsockname())
        bc.broadcast(_frame())
        receiver.settimeout(1.0)
        data, _ = receiver.recvfrom(65536)
        msg = cbor2.loads(data)
        assert msg["type"] == "lora_frame"
        assert msg["seq"] == 1
    finally:
        sender.close()
        receiver.close()


def test_broadcaster_filters_by_sync_word() -> None:
    sender, receiver = _make_udp_pair()
    try:
        bc = UdpBroadcaster(sender)
        # Subscriber only wants sync_word=0x12.
        bc.add_subscriber(receiver.getsockname(), sync_words=[0x12])
        bc.broadcast(_frame(sync_word=0x12, seq=1))
        bc.broadcast(_frame(sync_word=0x34, seq=2))
        receiver.settimeout(0.5)
        data, _ = receiver.recvfrom(65536)
        msg = cbor2.loads(data)
        assert msg["seq"] == 1
        # Second send is filtered — recvfrom should time out.
        receiver.settimeout(0.2)
        with pytest.raises((socket.timeout, BlockingIOError, TimeoutError)):
            receiver.recvfrom(65536)
    finally:
        sender.close()
        receiver.close()


def test_broadcaster_filters_by_source() -> None:
    sender, receiver = _make_udp_pair()
    try:
        bc = UdpBroadcaster(sender)
        bc.add_subscriber(receiver.getsockname(), sources=["radio_868"])
        bc.broadcast(_frame(source="radio_868", seq=1))
        bc.broadcast(_frame(source="radio_2g4", seq=2))
        receiver.settimeout(0.5)
        data, _ = receiver.recvfrom(65536)
        msg = cbor2.loads(data)
        assert msg["seq"] == 1
        receiver.settimeout(0.2)
        with pytest.raises((socket.timeout, BlockingIOError, TimeoutError)):
            receiver.recvfrom(65536)
    finally:
        sender.close()
        receiver.close()


def test_broadcaster_reaps_stale_subscriber() -> None:
    sender, _ = _make_udp_pair()
    try:
        bc = UdpBroadcaster(sender)
        bc.add_subscriber(("127.0.0.1", 9999), now=100.0)
        assert bc.subscriber_count == 1
        n = bc.reap_stale(now=200.0, max_age_s=60.0)
        assert n == 1
        assert bc.subscriber_count == 0
    finally:
        sender.close()


# ---------------------------------------------------------------------------
# FanOut
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "fanout.duckdb"


async def _run_fanout_with(
    fanout: FanOut, feeds: list[LoraFrame], drain_timeout_s: float = 1.0
) -> None:
    task = asyncio.create_task(fanout.run())
    for f in feeds:
        fanout.feed_frame(f)
    # Wait for the queue to drain.
    deadline = time.monotonic() + drain_timeout_s
    while fanout._queue.qsize() > 0 and time.monotonic() < deadline:  # noqa: SLF001
        await asyncio.sleep(0.01)
    await fanout.stop()
    await asyncio.wait_for(task, timeout=drain_timeout_s)


async def test_fanout_writes_frame_to_duckdb(db_path: Path) -> None:
    sender, _ = _make_udp_pair()
    writer = DuckDBWriter(StorageConfig(db_path=db_path, batch_rows=1, batch_ms=10))
    writer.start()
    try:
        bc = UdpBroadcaster(sender)
        fanout = FanOut(queue_depth=64, writer=writer, broadcaster=bc)
        await _run_fanout_with(fanout, [_frame(seq=1), _frame(seq=2)])
        writer.checkpoint()
    finally:
        writer.stop(timeout=2.0)
        sender.close()
    con = duckdb.connect(str(db_path), read_only=True)
    rows = con.execute("SELECT seq FROM lora_frames ORDER BY seq").fetchall()
    con.close()
    assert [r[0] for r in rows] == [1, 2]


async def test_fanout_broadcasts_to_subscriber(db_path: Path) -> None:
    sender, receiver = _make_udp_pair()
    writer = DuckDBWriter(StorageConfig(db_path=db_path, batch_rows=1))
    writer.start()
    try:
        bc = UdpBroadcaster(sender)
        bc.add_subscriber(receiver.getsockname())
        fanout = FanOut(queue_depth=64, writer=writer, broadcaster=bc)
        await _run_fanout_with(fanout, [_frame(seq=42)])
    finally:
        writer.stop(timeout=2.0)
        sender.close()
    receiver.settimeout(1.0)
    data, _ = receiver.recvfrom(65536)
    receiver.close()
    msg = cbor2.loads(data)
    assert msg["seq"] == 42


async def test_fanout_dispatches_status(db_path: Path) -> None:
    sender, _ = _make_udp_pair()
    writer = DuckDBWriter(StorageConfig(db_path=db_path, batch_rows=1))
    writer.start()
    try:
        bc = UdpBroadcaster(sender)
        fanout = FanOut(queue_depth=16, writer=writer, broadcaster=bc)
        task = asyncio.create_task(fanout.run())
        status = Status(
            type="status",
            ts="2026-01-01T00:00:00+00:00",
            phy=StatusPhy(rx_gain=40.0, tx_gain=0.0),
            frames=StatusFrames(total=10, crc_ok=8, crc_fail=2),
            rx_overflows=0,
        )
        fanout.feed_status("radio_868", status)
        # Drain.
        deadline = time.monotonic() + 1.0
        while fanout._queue.qsize() > 0 and time.monotonic() < deadline:  # noqa: SLF001
            await asyncio.sleep(0.01)
        await fanout.stop()
        await asyncio.wait_for(task, timeout=1.0)
        writer.checkpoint()
    finally:
        writer.stop(timeout=2.0)
        sender.close()
    con = duckdb.connect(str(db_path), read_only=True)
    n = con.execute("SELECT COUNT(*) FROM status_heartbeats").fetchone()
    con.close()
    assert n is not None and n[0] == 1


def test_fanout_drops_oldest_on_queue_overflow() -> None:
    """Feed more frames than queue_depth without running run() — dropped > 0."""
    sender, _ = _make_udp_pair()
    try:
        # Use a fake DuckDBWriter — we never await run() in this test.
        writer = DuckDBWriter(StorageConfig(db_path=Path("/tmp/_unused.duckdb")))

        bc = UdpBroadcaster(sender)
        fanout = FanOut(queue_depth=4, writer=writer, broadcaster=bc)
        for i in range(10):
            fanout.feed_frame(_frame(seq=i + 1))
        assert fanout.stats.dropped > 0
    finally:
        sender.close()
