#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Fan-out from the decode pipeline to DuckDB + UDP subscribers.

The daemon's decode chain emits annotated :class:`~lora.core.types.LoraFrame`
records and various telemetry events. This module hands them to:

* :class:`~lora.storage.DuckDBWriter` (own thread, own bounded queue,
  oldest-drop on overflow);
* :class:`UdpBroadcaster` — a CBOR re-encoder that ``sendto``'s each
  subscribed client, filtered by ``sync_word`` / ``source``.

Two queues compose here. :class:`FanOut` owns its own bounded
:class:`asyncio.Queue` between the decode coroutine and the sinks
(oldest-drop). :class:`DuckDBWriter` then has its own bounded queue
inside its writer thread (also oldest-drop). The :attr:`FanOut.stats`
counter tracks the FanOut-side drops only; the writer's drops are on
:attr:`DuckDBWriter.stats`.

The :func:`frame_to_cbor_dict` helper flattens a typed frame back to
the legacy wire-shape dict so existing consumers (lora_mon,
meshcore_bridge) keep parsing without changes.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

import cbor2

from lora.core.types import (
    LoraFrame,
    LoraTxAck,
    MultisfDetect,
    MultisfFrame,
    MultisfSync,
    Spectrum,
    Status,
    WidebandSlot,
    WidebandSweep,
)
from lora.daemon.wire import (
    frame_to_cbor_dict,
    lora_tx_ack_to_cbor_dict,
    spectrum_to_cbor_dict,
    status_to_cbor_dict,
)
from lora.storage import DuckDBWriter

_log = logging.getLogger("lora.daemon.fanout")

# Re-export wire helpers — fanout.frame_to_cbor_dict is the public path.
__all__ = [
    "FanOut",
    "FanoutStats",
    "UdpBroadcaster",
    "frame_to_cbor_dict",
    "lora_tx_ack_to_cbor_dict",
    "spectrum_to_cbor_dict",
    "status_to_cbor_dict",
]


# ---------------------------------------------------------------------------
# Subscriber registry
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Subscriber:
    """One UDP client subscribed to the daemon's broadcast stream."""

    addr: tuple[str, int]
    sync_words: frozenset[int]
    sources: frozenset[str]
    last_seen: float


class UdpBroadcaster:
    """UDP fan-out to subscribed clients.

    Clients send a ``subscribe`` CBOR map to the daemon's listen socket
    (the same socket the legacy ``lora_agg`` accepts subscriptions on).
    The broadcaster maintains a registry keyed by client address and a
    periodic reaper drops clients that haven't keepalived in
    ``max_age_s`` seconds.

    Filtering rules per :class:`~lora.core.types.Subscribe`:

    * ``sync_words`` empty → all sync words match;
    * ``sources`` empty → all sources match;
    * otherwise the frame's ``phy.sync_word`` / ``source`` must be in
      the configured set.
    """

    def __init__(
        self, sock: socket.socket, *, log: logging.Logger | None = None
    ) -> None:
        self._sock = sock
        self._subs: dict[tuple[str, int], _Subscriber] = {}
        self._log = log or _log
        # Cached upstream ``config`` message, encoded once. Sent verbatim
        # to every newly-registered subscriber so legacy clients (the
        # bridge, viz tools) can do their existing ``wait_for_config``
        # handshake against ``lora-core`` instead of ``lora_trx`` directly.
        self._upstream_config_bytes: bytes | None = None

    # -- registry -----------------------------------------------------------

    def set_upstream_config(self, msg: Mapping[str, Any]) -> None:
        """Cache and re-encode the upstream ``config`` CBOR message.

        Called by :class:`UdpSource` when it receives a control-plane
        ``config`` map from ``lora_trx`` on subscribe. The cached blob
        is replayed to every new subscriber on
        :meth:`add_subscriber`.
        """
        try:
            self._upstream_config_bytes = cbor2.dumps(dict(msg))
        except Exception as exc:  # pragma: no cover - serialization safety
            self._log.warning("failed to cache upstream config: %s", exc)

    def add_subscriber(
        self,
        addr: tuple[str, int],
        sync_words: Iterable[int] | None = None,
        sources: Iterable[str] | None = None,
        *,
        now: float | None = None,
    ) -> None:
        sub = _Subscriber(
            addr=addr,
            sync_words=frozenset(sync_words) if sync_words else frozenset(),
            sources=frozenset(sources) if sources else frozenset(),
            last_seen=now if now is not None else time.monotonic(),
        )
        is_new = addr not in self._subs
        self._subs[addr] = sub
        if is_new:
            sw_str = (
                ",".join(f"0x{x:02x}" for x in sorted(sub.sync_words))
                if sub.sync_words
                else "all"
            )
            src_str = ",".join(sorted(sub.sources)) if sub.sources else "all"
            self._log.info(
                "subscriber %s:%d sw=%s src=%s",
                addr[0],
                addr[1],
                sw_str,
                src_str,
            )
            # Replay cached upstream config so clients can complete their
            # ``wait_for_config`` handshake.
            if self._upstream_config_bytes is not None:
                try:
                    self._sock.sendto(self._upstream_config_bytes, addr)
                except OSError as exc:  # pragma: no cover - send failure
                    self._log.debug(
                        "config replay to %s:%d failed: %s",
                        addr[0],
                        addr[1],
                        exc,
                    )

    def touch_subscriber(self, addr: tuple[str, int], now: float) -> None:
        sub = self._subs.get(addr)
        if sub is not None:
            sub.last_seen = now

    def remove_subscriber(self, addr: tuple[str, int]) -> None:
        self._subs.pop(addr, None)

    def reap_stale(self, now: float, max_age_s: float) -> int:
        stale = [a for a, s in self._subs.items() if (now - s.last_seen) >= max_age_s]
        for a in stale:
            self._log.info("evicting stale subscriber %s:%d", a[0], a[1])
            self._subs.pop(a, None)
        return len(stale)

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    # -- broadcast helpers --------------------------------------------------

    def broadcast(self, frame: LoraFrame) -> None:
        """Send ``frame`` (CBOR-encoded) to every matching subscriber."""
        data = cbor2.dumps(frame_to_cbor_dict(frame))
        sw = frame.phy.sync_word
        src = frame.source
        self._send_filtered(data, sw=sw, src=src)

    def broadcast_status(self, status: Status) -> None:
        data = cbor2.dumps(status_to_cbor_dict(status))
        # status / spectrum bypass filters — every subscriber gets them.
        self._send_unfiltered(data)

    def broadcast_spectrum(self, spec: Spectrum) -> None:
        data = cbor2.dumps(spectrum_to_cbor_dict(spec))
        self._send_unfiltered(data)

    def broadcast_tx_ack(self, ack: LoraTxAck, addr: tuple[str, int]) -> None:
        """Send a ``lora_tx_ack`` directly to the requesting client."""
        data = cbor2.dumps(lora_tx_ack_to_cbor_dict(ack))
        try:
            self._sock.sendto(data, addr)
        except OSError as exc:  # pragma: no cover - network edge
            self._log.warning("tx_ack to %s:%d failed: %s", addr[0], addr[1], exc)

    # -- internals ----------------------------------------------------------

    def _send_filtered(self, data: bytes, *, sw: int, src: str) -> None:
        for addr, sub in list(self._subs.items()):
            if sub.sync_words and sw not in sub.sync_words:
                continue
            if sub.sources and src not in sub.sources:
                continue
            try:
                self._sock.sendto(data, addr)
            except OSError as exc:  # pragma: no cover - network edge
                self._log.debug("send to %s:%d failed: %s", addr[0], addr[1], exc)

    def _send_unfiltered(self, data: bytes) -> None:
        for addr in list(self._subs.keys()):
            try:
                self._sock.sendto(data, addr)
            except OSError as exc:  # pragma: no cover - network edge
                self._log.debug("send to %s:%d failed: %s", addr[0], addr[1], exc)


# ---------------------------------------------------------------------------
# FanOut
# ---------------------------------------------------------------------------


@dataclass
class FanoutStats:
    enqueued: int = 0
    dropped: int = 0


_QueueItem = tuple[str, str, Any]


class FanOut:
    """Bounded queue between the decode pipeline and the downstream sinks."""

    _STOP: ClassVar[object] = object()

    def __init__(
        self,
        *,
        queue_depth: int,
        writer: DuckDBWriter,
        broadcaster: UdpBroadcaster,
    ) -> None:
        self._queue: asyncio.Queue[_QueueItem | object] = asyncio.Queue(
            maxsize=queue_depth
        )
        self._writer = writer
        self._broadcaster = broadcaster
        self._stats = FanoutStats()

    # -- producers (sync; called from receivers) ----------------------------

    def feed_frame(self, frame: LoraFrame) -> bool:
        return self._enqueue(("frame", "", frame))

    def feed_status(self, source: str, status: Status) -> bool:
        return self._enqueue(("status", source, status))

    def feed_telemetry(self, source: str, event: object) -> bool:
        return self._enqueue(("telemetry", source, event))

    @property
    def stats(self) -> FanoutStats:
        return FanoutStats(enqueued=self._stats.enqueued, dropped=self._stats.dropped)

    # -- async consumer -----------------------------------------------------

    async def run(self) -> None:
        """Drain the queue until a stop sentinel arrives."""
        while True:
            item = await self._queue.get()
            if item is self._STOP:
                return
            assert isinstance(item, tuple)
            kind, source, payload = item
            try:
                self._dispatch(kind, source, payload)
            except Exception:  # noqa: BLE001 — defence in depth
                _log.exception("fanout: dispatch failed for kind=%s", kind)

    async def stop(self) -> None:
        """Signal the run() coroutine to exit at its next iteration."""
        await self._queue.put(self._STOP)

    # -- internals ----------------------------------------------------------

    def _enqueue(self, item: _QueueItem) -> bool:
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
                self._stats.dropped += 1
            except asyncio.QueueEmpty:  # pragma: no cover - racy edge
                pass
            try:
                self._queue.put_nowait(item)
                self._stats.enqueued += 1
            except asyncio.QueueFull:  # pragma: no cover - racy edge
                self._stats.dropped += 1
            return False
        self._stats.enqueued += 1
        return True

    def _dispatch(self, kind: str, source: str, payload: object) -> None:
        if kind == "frame":
            assert isinstance(payload, LoraFrame)
            self._writer.put(payload)
            self._broadcaster.broadcast(payload)
            return
        if kind == "status":
            assert isinstance(payload, Status)
            self._writer.put_status(payload)
            self._broadcaster.broadcast_status(payload)
            return
        if kind == "telemetry":
            self._dispatch_telemetry(source, payload)
            return
        _log.debug("fanout: unknown kind=%s discarded", kind)

    def _dispatch_telemetry(self, source: str, event: object) -> None:
        if isinstance(event, Spectrum):
            self._broadcaster.broadcast_spectrum(event)
            return
        if isinstance(event, MultisfDetect):
            self._writer.put_multisf_detect(event, source)
            return
        if isinstance(event, MultisfSync):
            self._writer.put_multisf_sync(event, source)
            return
        if isinstance(event, MultisfFrame):
            self._writer.put_multisf_frame(event, source)
            return
        if isinstance(event, WidebandSweep):
            self._writer.put_wideband_sweep(event, source)
            return
        if isinstance(event, WidebandSlot):
            self._writer.put_wideband_slot(event, source)
            return
        _log.debug("fanout: unhandled telemetry %s", type(event).__name__)
