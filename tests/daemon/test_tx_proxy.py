# SPDX-License-Identifier: ISC
"""Tests for :mod:`lora.daemon.tx_proxy`."""

from __future__ import annotations

import asyncio
import logging
import socket

import cbor2
import pytest

from lora.core.types import LoraTx
from lora.daemon.config import UpstreamConfig
from lora.daemon.tx_proxy import TxProxy


def _bind_mock_upstream() -> socket.socket:
    """Bind a UDP socket on 127.0.0.1:0 — caller drives recv/send."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    return s


async def _ack_responder(sock: socket.socket, *, ok: bool = True) -> dict[str, object]:
    """Receive one TX request on ``sock`` and reply with an ack on the source addr.

    Returns the parsed request dict for assertions.
    """
    sock.setblocking(False)
    loop = asyncio.get_running_loop()
    while True:
        try:
            data, src = sock.recvfrom(65536)
            break
        except BlockingIOError:
            await asyncio.sleep(0.01)
    req = cbor2.loads(data)
    ack = {"type": "lora_tx_ack", "seq": req.get("seq", 0), "ok": ok}
    await loop.sock_sendto(sock, cbor2.dumps(ack), src)
    return req


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


async def test_routes_by_source_field() -> None:
    up_a = _bind_mock_upstream()
    up_b = _bind_mock_upstream()
    try:
        cfg = (
            UpstreamConfig(
                name="radio_868", host="127.0.0.1", port=up_a.getsockname()[1]
            ),
            UpstreamConfig(
                name="radio_2g4", host="127.0.0.1", port=up_b.getsockname()[1]
            ),
        )
        proxy = TxProxy(cfg, ack_timeout_s=2.0)
        await proxy.start()
        try:
            req = LoraTx(type="lora_tx", payload=b"hello", source="radio_2g4")
            ack_task = asyncio.create_task(_ack_responder(up_b))
            client_addr = ("127.0.0.1", 12345)
            ack = await proxy.handle(client_addr, req)
            received = await asyncio.wait_for(ack_task, timeout=2.0)
            # up_a never received anything.
            up_a.setblocking(False)
            with pytest.raises((BlockingIOError, OSError)):
                up_a.recvfrom(65536)
        finally:
            await proxy.stop()
    finally:
        up_a.close()
        up_b.close()
    assert ack.ok is True
    assert received["payload"] == b"hello"


async def test_omitted_source_picks_first_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    up = _bind_mock_upstream()
    try:
        cfg = (
            UpstreamConfig(name="primary", host="127.0.0.1", port=up.getsockname()[1]),
            UpstreamConfig(name="secondary", host="127.0.0.1", port=9),
        )
        proxy = TxProxy(cfg, ack_timeout_s=2.0)
        await proxy.start()
        try:
            req = LoraTx(type="lora_tx", payload=b"x")  # no source field
            ack_task = asyncio.create_task(_ack_responder(up))
            with caplog.at_level(logging.WARNING, logger="lora.daemon.tx_proxy"):
                ack = await proxy.handle(("127.0.0.1", 0), req)
            await asyncio.wait_for(ack_task, timeout=2.0)
        finally:
            await proxy.stop()
    finally:
        up.close()
    assert ack.ok is True
    assert any("source" in r.getMessage().lower() for r in caplog.records)


async def test_unknown_source_warns_and_uses_first() -> None:
    up = _bind_mock_upstream()
    try:
        cfg = (
            UpstreamConfig(name="primary", host="127.0.0.1", port=up.getsockname()[1]),
        )
        proxy = TxProxy(cfg, ack_timeout_s=2.0)
        await proxy.start()
        try:
            req = LoraTx(type="lora_tx", payload=b"x", source="not_a_real_radio")
            ack_task = asyncio.create_task(_ack_responder(up))
            ack = await proxy.handle(("127.0.0.1", 0), req)
            await asyncio.wait_for(ack_task, timeout=2.0)
        finally:
            await proxy.stop()
    finally:
        up.close()
    assert ack.ok is True


# ---------------------------------------------------------------------------
# Ack timeout
# ---------------------------------------------------------------------------


async def test_ack_timeout_returns_error() -> None:
    """Upstream never responds → handle() returns ok=False with error='timeout'."""
    # Bind a socket but never read from it — requests will be received by the
    # kernel buffer and silently dropped on close.
    sink = _bind_mock_upstream()
    try:
        cfg = (
            UpstreamConfig(name="dead", host="127.0.0.1", port=sink.getsockname()[1]),
        )
        proxy = TxProxy(cfg, ack_timeout_s=0.2)
        await proxy.start()
        try:
            req = LoraTx(type="lora_tx", payload=b"x", source="dead", seq=99)
            ack = await proxy.handle(("127.0.0.1", 0), req)
        finally:
            await proxy.stop()
    finally:
        sink.close()
    assert ack.ok is False
    assert ack.error == "timeout"
    # Daemon preserves the client's seq when reporting timeout.
    assert ack.seq == 99


async def test_ack_seq_translated_to_client_seq() -> None:
    """The ack returned to the client has the client's seq, not the daemon's."""
    up = _bind_mock_upstream()
    try:
        cfg = (
            UpstreamConfig(name="primary", host="127.0.0.1", port=up.getsockname()[1]),
        )
        proxy = TxProxy(cfg, ack_timeout_s=2.0)
        await proxy.start()
        try:
            req = LoraTx(type="lora_tx", payload=b"x", source="primary", seq=42)
            ack_task = asyncio.create_task(_ack_responder(up))
            ack = await proxy.handle(("127.0.0.1", 0), req)
            await asyncio.wait_for(ack_task, timeout=2.0)
        finally:
            await proxy.stop()
    finally:
        up.close()
    assert ack.seq == 42
    assert ack.ok is True
