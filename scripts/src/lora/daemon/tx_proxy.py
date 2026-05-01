#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Relay ``lora_tx`` requests from clients to a named upstream ``lora_trx``.

Each :class:`~lora.daemon.config.UpstreamConfig` gets its own UDP
socket, so ``lora_trx`` can reply with ``lora_tx_ack`` on the source
address of the inbound request (parity with the legacy ``lora_agg``
behaviour). The proxy maintains a ``{daemon_seq: (client_addr,
client_seq)}`` map for ack correlation; on ack the original client seq
is restored before the ack is returned to the caller.

If a request omits ``source`` (or names an unknown upstream) the proxy
falls back to the first configured upstream and logs at WARNING — the
log is the operator's nudge to be explicit.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import socket
from typing import Any

import cbor2

from lora.core.schema import validate_lora_tx_ack
from lora.core.types import LoraTx, LoraTxAck
from lora.daemon.config import UpstreamConfig

_log = logging.getLogger("lora.daemon.tx_proxy")


class TxProxy:
    """Forward ``lora_tx`` requests to the right upstream and relay the ack."""

    def __init__(
        self,
        upstreams: tuple[UpstreamConfig, ...],
        *,
        ack_timeout_s: float = 5.0,
    ) -> None:
        if not upstreams:
            raise ValueError("TxProxy requires at least one upstream")
        self._upstreams: dict[str, UpstreamConfig] = {u.name: u for u in upstreams}
        self._first: str = upstreams[0].name
        self._ack_timeout_s = ack_timeout_s
        self._sockets: dict[str, socket.socket] = {}
        # daemon_seq -> (client_addr, client_seq, future)
        self._inflight: dict[
            int, tuple[tuple[str, int], int, asyncio.Future[LoraTxAck]]
        ] = {}
        self._seq_counter = itertools.count(1)
        self._loop: asyncio.AbstractEventLoop | None = None

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Bind one UDP socket per upstream and register ack readers."""
        self._loop = asyncio.get_running_loop()
        for name in self._upstreams:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("127.0.0.1", 0))
            sock.setblocking(False)
            self._sockets[name] = sock
            self._loop.add_reader(sock.fileno(), self._on_ack_ready, name)
        _log.info("tx_proxy: bound %d sockets", len(self._sockets))

    async def stop(self) -> None:
        loop = self._loop
        for name, sock in self._sockets.items():
            if loop is not None:
                try:
                    loop.remove_reader(sock.fileno())
                except ValueError, OSError:  # pragma: no cover - best effort
                    pass
            sock.close()
        self._sockets.clear()
        # Cancel any outstanding futures so callers don't block forever.
        for _, _, fut in self._inflight.values():
            if not fut.done():
                fut.cancel()
        self._inflight.clear()

    # -- public API ---------------------------------------------------------

    async def handle(self, client_addr: tuple[str, int], req: LoraTx) -> LoraTxAck:
        """Forward ``req`` to the right upstream, return the ack."""
        if self._loop is None:
            raise RuntimeError("TxProxy.start() must be awaited before handle()")
        name = self._pick_upstream(req)
        upstream = self._upstreams[name]
        sock = self._sockets[name]

        client_seq = req.seq if req.seq is not None else 0
        daemon_seq = next(self._seq_counter)

        fut: asyncio.Future[LoraTxAck] = self._loop.create_future()
        self._inflight[daemon_seq] = (client_addr, client_seq, fut)

        wire = self._build_request(req, daemon_seq)
        try:
            sock.sendto(cbor2.dumps(wire), (upstream.host, upstream.port))
        except OSError as exc:
            self._inflight.pop(daemon_seq, None)
            return LoraTxAck(
                type="lora_tx_ack",
                seq=client_seq,
                ok=False,
                error=f"send failed: {exc}",
            )

        try:
            ack = await asyncio.wait_for(fut, timeout=self._ack_timeout_s)
        except TimeoutError:
            self._inflight.pop(daemon_seq, None)
            return LoraTxAck(
                type="lora_tx_ack", seq=client_seq, ok=False, error="timeout"
            )
        # Translate daemon_seq back to the client's seq.
        return LoraTxAck(type="lora_tx_ack", seq=client_seq, ok=ack.ok, error=ack.error)

    # -- internals ----------------------------------------------------------

    def _pick_upstream(self, req: LoraTx) -> str:
        if req.source is None:
            _log.warning(
                "lora_tx without source field — routing to first upstream %r",
                self._first,
            )
            return self._first
        if req.source not in self._upstreams:
            _log.warning(
                "lora_tx source=%r unknown — routing to first upstream %r",
                req.source,
                self._first,
            )
            return self._first
        return req.source

    @staticmethod
    def _build_request(req: LoraTx, daemon_seq: int) -> dict[str, Any]:
        d: dict[str, Any] = {
            "type": "lora_tx",
            "payload": req.payload,
            "seq": daemon_seq,
        }
        for attr in (
            "cr",
            "sync_word",
            "preamble_len",
            "repeat",
            "gap_ms",
            "dry_run",
        ):
            v = getattr(req, attr)
            if v is not None:
                d[attr] = v
        # ``source`` is daemon-internal routing; do not forward to lora_trx.
        return d

    def _on_ack_ready(self, name: str) -> None:
        sock = self._sockets.get(name)
        if sock is None:  # pragma: no cover - racy stop()
            return
        try:
            data, _ = sock.recvfrom(65536)
        except OSError:  # pragma: no cover - racy stop()
            return
        try:
            msg = cbor2.loads(data)
        except Exception:
            _log.debug("tx_proxy %s: unparseable CBOR", name)
            return
        if not isinstance(msg, dict) or msg.get("type") != "lora_tx_ack":
            return
        try:
            ack = validate_lora_tx_ack(msg)
        except Exception:
            _log.debug("tx_proxy %s: invalid lora_tx_ack", name)
            return
        entry = self._inflight.pop(ack.seq, None)
        if entry is None:
            _log.debug("tx_proxy %s: ack for unknown seq=%d", name, ack.seq)
            return
        _, _, fut = entry
        if not fut.done():
            fut.set_result(ack)
