#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Multi-source UDP receiver — one task per :class:`UpstreamConfig`.

Each :class:`UdpSource` opens an asyncio datagram endpoint, subscribes
to a single ``lora_trx`` instance via CBOR (``{"type":"subscribe"}``),
and loops:

1. **L1** — size cap (``MAX_DATAGRAM`` = 65536; kernel-level UDP cap).
2. **L1.5** — strict source-addr check (drop datagrams from unexpected
   ``host:port`` when ``upstream.strict=True``).
3. **L2** — ``cbor2.loads`` inside a guarded ``try/except``.
4. **L3** — :func:`lora.core.schema.dispatch` to a typed dataclass.
5. **Tag** — stamp the upstream's ``source`` name onto the typed event
   and push onto the shared output queue as a ``(source, event)``
   tuple.

A periodic keepalive resends the subscribe message every
``KEEPALIVE_INTERVAL`` seconds so the upstream's stale-subscriber
reaper keeps us in its registry.

Output queue items are ALL tuples ``(source: str, event: object)``.
For :class:`~lora.core.types.LoraFrame` the ``source`` field is also
stamped onto the frame itself (frozen dataclass via
:func:`dataclasses.replace`); for everything else the consumer reads
the source from the tuple.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

import cbor2

from lora.core.schema import dispatch
from lora.core.types import LoraFrame
from lora.core.udp import KEEPALIVE_INTERVAL
from lora.daemon.config import UpstreamConfig

_log = logging.getLogger("lora.daemon.source")

# Output tuple shape: (source_name, typed_event).
SourceItem = tuple[str, object]


class _SubscriberProtocol(asyncio.DatagramProtocol):
    """asyncio datagram protocol bound to one UDP socket."""

    def __init__(self, on_datagram) -> None:  # type: ignore[no-untyped-def]
        self._on_datagram = on_datagram
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, asyncio.DatagramTransport)
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._on_datagram(data, addr)

    def error_received(self, exc: Exception) -> None:
        _log.debug("datagram socket error: %s", exc)


class UdpSource:
    """Subscribe to one ``lora_trx`` instance, validate, tag, enqueue."""

    MAX_DATAGRAM: int = 65536

    def __init__(
        self,
        upstream: UpstreamConfig,
        *,
        keepalive_interval_s: float = KEEPALIVE_INTERVAL,
        on_config: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> None:
        self._upstream = upstream
        self._keepalive_interval_s = keepalive_interval_s
        self._stop_event = asyncio.Event()
        self._transport: asyncio.DatagramTransport | None = None
        self._queue: asyncio.Queue[SourceItem] | None = None
        # ``config`` is a control-plane message lora_trx sends on subscribe.
        # The L3 schema validator does not have a ``config`` validator, so it
        # would otherwise be dropped. Forward it to a callback (typically the
        # broadcaster's ``set_upstream_config``) before dispatching.
        self._on_config = on_config

    async def run(self, out_queue: asyncio.Queue[SourceItem]) -> None:
        """Subscribe + keepalive + drain until :meth:`stop` is called."""
        self._queue = out_queue
        loop = asyncio.get_running_loop()

        transport, _ = await loop.create_datagram_endpoint(
            lambda: _SubscriberProtocol(self._on_datagram),
            local_addr=("0.0.0.0", 0),
        )
        self._transport = transport
        _log.debug(
            "upstream %s=%s:%d: source loop started",
            self._upstream.name,
            self._upstream.host,
            self._upstream.port,
        )
        try:
            self._send_subscribe()
            keepalive = asyncio.create_task(self._keepalive_loop())
            try:
                await self._stop_event.wait()
            finally:
                keepalive.cancel()
                try:
                    await keepalive
                except asyncio.CancelledError, BaseException:  # pragma: no cover
                    pass
        finally:
            transport.close()
            self._transport = None

    async def stop(self) -> None:
        self._stop_event.set()

    # -- internals ----------------------------------------------------------

    def _send_subscribe(self) -> None:
        if self._transport is None:  # pragma: no cover - defensive
            return
        addr = (self._upstream.host, self._upstream.port)
        try:
            self._transport.sendto(cbor2.dumps({"type": "subscribe"}), addr)
        except OSError as exc:  # pragma: no cover - rare
            _log.warning("subscribe to %s:%d failed: %s", addr[0], addr[1], exc)

    async def _keepalive_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._keepalive_interval_s
                )
                return
            except TimeoutError:
                self._send_subscribe()

    def _on_datagram(self, data: bytes, src_addr: tuple[str, int]) -> None:
        # L1: size cap (kernel already enforces 65507; this is a belt-and-braces).
        if len(data) > self.MAX_DATAGRAM:
            _log.debug(
                "%s: dropping oversized datagram (%d bytes)",
                self._upstream.name,
                len(data),
            )
            return
        # L1.5: strict address filter.
        if self._upstream.strict and not self._addr_matches(src_addr):
            _log.debug(
                "%s: dropping datagram from unexpected addr %r",
                self._upstream.name,
                src_addr,
            )
            return
        # L2: CBOR parse.
        try:
            msg = cbor2.loads(data)
        except Exception:
            _log.debug("%s: undecodable CBOR", self._upstream.name)
            return
        # ``config`` (control-plane handshake from lora_trx) is forwarded
        # raw to the broadcaster so it can be cached and replayed to new
        # subscribers — there's no L3 validator for it.
        if (
            isinstance(msg, dict)
            and msg.get("type") == "config"
            and self._on_config is not None
        ):
            try:
                self._on_config(msg)
            except Exception as exc:  # pragma: no cover - callback safety
                _log.debug(
                    "%s: on_config callback raised: %s",
                    self._upstream.name,
                    exc,
                )
            return
        # L3: schema dispatch.
        event: Any = dispatch(msg) if isinstance(msg, dict) else None
        if event is None:
            return
        # Tag + enqueue.
        if isinstance(event, LoraFrame):
            event = replace(event, source=self._upstream.name)
        if self._queue is None:  # pragma: no cover - defensive
            return
        try:
            self._queue.put_nowait((self._upstream.name, event))
        except asyncio.QueueFull:
            _log.debug(
                "%s: out_queue full, dropping %s",
                self._upstream.name,
                type(event).__name__,
            )

    def _addr_matches(self, src_addr: tuple[str, int]) -> bool:
        """Strict-mode equality check between observed and configured addr."""
        return src_addr[0] == self._upstream.host and src_addr[1] == self._upstream.port
