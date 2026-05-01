# SPDX-License-Identifier: ISC
"""Tests for :mod:`lora.daemon.source`."""

from __future__ import annotations

import asyncio
import socket
import time
from typing import Any

import cbor2

from lora.core.types import LoraFrame
from lora.daemon.config import UpstreamConfig
from lora.daemon.source import UdpSource


def _bind_mock_upstream() -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    return s


def _wait_for_subscribe(sock: socket.socket, timeout_s: float = 2.0) -> tuple[str, int]:
    """Block (briefly) until a subscribe arrives; return the source addr."""
    sock.setblocking(False)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            data, src = sock.recvfrom(65536)
        except BlockingIOError:
            time.sleep(0.01)
            continue
        msg = cbor2.loads(data)
        if isinstance(msg, dict) and msg.get("type") == "subscribe":
            return src
    raise TimeoutError("no subscribe arrived")


def _build_frame_cbor(*, seq: int = 1, sync_word: int = 0x12) -> bytes:
    payload = b"hello"
    d: dict[str, Any] = {
        "type": "lora_frame",
        "ts": "2026-01-01T00:00:00+00:00",
        "seq": seq,
        "payload": payload,
        "payload_len": len(payload),
        "crc_valid": True,
        "cr": 4,
        "is_downchirp": False,
        "payload_hash": 0xCAFEBABE,
        "id": "00000000-0000-0000-0000-000000000001",
        "phy": {
            "sf": 8,
            "bw": 62500,
            "cr": 4,
            "sync_word": sync_word,
            "crc_valid": True,
        },
        "carrier": {
            "sync_word": sync_word,
            "sf": 8,
            "bw": 62500,
            "cr": 4,
            "ldro_cfg": False,
        },
    }
    return cbor2.dumps(d)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_source_emits_typed_lora_frame_with_source_tag() -> None:
    mock = _bind_mock_upstream()
    try:
        upstream = UpstreamConfig(
            name="radio_868",
            host="127.0.0.1",
            port=mock.getsockname()[1],
        )
        queue: asyncio.Queue[tuple[str, object]] = asyncio.Queue()
        source = UdpSource(upstream)
        task = asyncio.create_task(source.run(queue))
        try:
            # Wait for subscribe.
            client_addr = await asyncio.get_running_loop().run_in_executor(
                None, _wait_for_subscribe, mock
            )
            mock.sendto(_build_frame_cbor(seq=42), client_addr)
            src_name, frame = await asyncio.wait_for(queue.get(), timeout=2.0)
        finally:
            await source.stop()
            await asyncio.wait_for(task, timeout=2.0)
    finally:
        mock.close()
    assert src_name == "radio_868"
    assert isinstance(frame, LoraFrame)
    assert frame.seq == 42
    assert frame.source == "radio_868"


# ---------------------------------------------------------------------------
# Defence in depth
# ---------------------------------------------------------------------------


async def test_source_drops_malformed_cbor() -> None:
    mock = _bind_mock_upstream()
    try:
        upstream = UpstreamConfig(
            name="radio_868",
            host="127.0.0.1",
            port=mock.getsockname()[1],
        )
        queue: asyncio.Queue[tuple[str, object]] = asyncio.Queue()
        source = UdpSource(upstream)
        task = asyncio.create_task(source.run(queue))
        try:
            client_addr = await asyncio.get_running_loop().run_in_executor(
                None, _wait_for_subscribe, mock
            )
            mock.sendto(b"\xff\x00\xff\x99 not valid cbor", client_addr)
            # Then send a real frame; assert we still process it.
            mock.sendto(_build_frame_cbor(seq=7), client_addr)
            src_name, frame = await asyncio.wait_for(queue.get(), timeout=2.0)
        finally:
            await source.stop()
            await asyncio.wait_for(task, timeout=2.0)
    finally:
        mock.close()
    assert isinstance(frame, LoraFrame)
    assert frame.seq == 7


async def test_source_strict_drops_unknown_address() -> None:
    """Strict mode: datagram from an unexpected addr is dropped."""
    mock = _bind_mock_upstream()
    other = _bind_mock_upstream()  # NOT the configured upstream
    try:
        upstream = UpstreamConfig(
            name="radio_868",
            host="127.0.0.1",
            port=mock.getsockname()[1],
            strict=True,
        )
        queue: asyncio.Queue[tuple[str, object]] = asyncio.Queue()
        source = UdpSource(upstream)
        task = asyncio.create_task(source.run(queue))
        try:
            client_addr = await asyncio.get_running_loop().run_in_executor(
                None, _wait_for_subscribe, mock
            )
            # Send from the OTHER socket (different src port) — should be dropped.
            other.sendto(_build_frame_cbor(seq=3), client_addr)
            # Confirm by sending from the real mock — should arrive.
            mock.sendto(_build_frame_cbor(seq=4), client_addr)
            src_name, frame = await asyncio.wait_for(queue.get(), timeout=2.0)
            assert isinstance(frame, LoraFrame)
            assert frame.seq == 4
            # Queue should be empty after that — the wrong-addr datagram was dropped.
            assert queue.empty()
        finally:
            await source.stop()
            await asyncio.wait_for(task, timeout=2.0)
    finally:
        mock.close()
        other.close()


async def test_source_handles_near_max_datagram() -> None:
    """A near-max-size datagram is processed without erroring."""
    mock = _bind_mock_upstream()
    try:
        upstream = UpstreamConfig(
            name="radio_868",
            host="127.0.0.1",
            port=mock.getsockname()[1],
            strict=False,
        )
        queue: asyncio.Queue[tuple[str, object]] = asyncio.Queue()
        source = UdpSource(upstream)
        task = asyncio.create_task(source.run(queue))
        try:
            client_addr = await asyncio.get_running_loop().run_in_executor(
                None, _wait_for_subscribe, mock
            )
            # Spectrum frame near typical UDP MTU (~8 KB; macOS rejects
            # sends > kernel-level limits well below the 64 KB IP cap).
            fft = 1024
            big_bins = b"\x00" * (4 * fft)
            spectrum = {
                "type": "spectrum",
                "bins": big_bins,
                "fft_size": fft,
                "center_freq": 8.69618e8,
                "sample_rate": 1e6,
            }
            mock.sendto(cbor2.dumps(spectrum), client_addr)
            src_name, event = await asyncio.wait_for(queue.get(), timeout=2.0)
            assert src_name == "radio_868"
            # Received as a typed Spectrum (not a LoraFrame).
            from lora.core.types import Spectrum

            assert isinstance(event, Spectrum)
        finally:
            await source.stop()
            await asyncio.wait_for(task, timeout=2.0)
    finally:
        mock.close()


# ---------------------------------------------------------------------------
# Multi-source: two UdpSources tag with their own names
# ---------------------------------------------------------------------------


async def test_two_sources_tag_independently() -> None:
    mock_a = _bind_mock_upstream()
    mock_b = _bind_mock_upstream()
    try:
        up_a = UpstreamConfig(
            name="radio_868", host="127.0.0.1", port=mock_a.getsockname()[1]
        )
        up_b = UpstreamConfig(
            name="radio_2g4", host="127.0.0.1", port=mock_b.getsockname()[1]
        )
        queue: asyncio.Queue[tuple[str, object]] = asyncio.Queue()
        s_a = UdpSource(up_a)
        s_b = UdpSource(up_b)
        t_a = asyncio.create_task(s_a.run(queue))
        t_b = asyncio.create_task(s_b.run(queue))
        try:
            loop = asyncio.get_running_loop()
            ca = await loop.run_in_executor(None, _wait_for_subscribe, mock_a)
            cb = await loop.run_in_executor(None, _wait_for_subscribe, mock_b)
            mock_a.sendto(_build_frame_cbor(seq=1), ca)
            mock_b.sendto(_build_frame_cbor(seq=2), cb)
            seen: dict[str, int] = {}
            for _ in range(2):
                name, frame = await asyncio.wait_for(queue.get(), timeout=2.0)
                assert isinstance(frame, LoraFrame)
                seen[name] = frame.seq
        finally:
            await s_a.stop()
            await s_b.stop()
            await asyncio.wait_for(t_a, timeout=2.0)
            await asyncio.wait_for(t_b, timeout=2.0)
    finally:
        mock_a.close()
        mock_b.close()
    assert seen == {"radio_868": 1, "radio_2g4": 2}
