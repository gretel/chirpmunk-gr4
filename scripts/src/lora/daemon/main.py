#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""``lora core`` daemon entry point — Phase 2E.

Wires :class:`~lora.daemon.config.DaemonConfig` into a runnable
asyncio process: per-upstream :class:`~lora.daemon.source.UdpSource`,
:class:`~lora.aggregator.DiversityAggregator`,
:class:`~lora.daemon.decode_chain.DecodeChain`,
:class:`~lora.storage.DuckDBWriter`,
:class:`~lora.daemon.fanout.FanOut` (with
:class:`~lora.daemon.fanout.UdpBroadcaster`),
:class:`~lora.daemon.tx_proxy.TxProxy`, and
:class:`~lora.daemon.lifecycle.Lifecycle`.

Pipeline:

    UdpSource -> source_queue -> _pipeline_task ->
       LoraFrame:    DiversityAggregator.feed -> DecodeChain.process -> FanOut
       Status:       FanOut.feed_status
       telemetry:    FanOut.feed_telemetry

    listen_sock -> _listen_reader ->
       Subscribe:    UdpBroadcaster.add_subscriber
       LoraTx:       TxProxy.handle -> UdpBroadcaster.broadcast_tx_ack
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import cbor2
import setproctitle

from lora.aggregator.diversity import (
    DiversityAggregator,
    extract_candidate,
)
from lora.core.config import find_config, load_config
from lora.core.logging import setup_logging
from lora.core.schema import dispatch
from lora.core.types import LoraFrame, LoraTx, Status, Subscribe
from lora.core.udp import parse_host_port
from lora.daemon._decoders import build_decoders
from lora.daemon.config import DaemonConfig
from lora.daemon.decode_chain import DecodeChain
from lora.daemon.fanout import FanOut, UdpBroadcaster
from lora.daemon.hot_reload import HotReloader
from lora.daemon.lifecycle import Lifecycle
from lora.daemon.source import UdpSource
from lora.daemon.tx_proxy import TxProxy
from lora.identity import IdentityStore
from lora.storage import DatabaseLocked, DuckDBWriter

_log = logging.getLogger("lora.daemon")

_AGG_FLUSH_INTERVAL_S = 0.05  # 50ms — finer than the smallest expected window
_SUBSCRIBER_REAP_INTERVAL_S = 5.0
_SUBSCRIBER_MAX_AGE_S = 60.0


# ---------------------------------------------------------------------------
# Listen socket protocol — drains subscribe / lora_tx requests
# ---------------------------------------------------------------------------


class _ListenProtocol(asyncio.DatagramProtocol):
    """Demux datagrams arriving on the daemon's listen socket."""

    def __init__(
        self,
        broadcaster: UdpBroadcaster,
        tx_proxy: TxProxy,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._broadcaster = broadcaster
        self._tx_proxy = tx_proxy
        self._loop = loop

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            msg = cbor2.loads(data)
        except Exception:
            return
        event = dispatch(msg) if isinstance(msg, dict) else None
        if isinstance(event, Subscribe):
            self._broadcaster.add_subscriber(
                addr,
                sync_words=event.sync_word,
                sources=event.source,
            )
            return
        if isinstance(event, LoraTx):
            self._loop.create_task(self._handle_tx(addr, event))
            return

    async def _handle_tx(self, addr: tuple[str, int], req: LoraTx) -> None:
        ack = await self._tx_proxy.handle(addr, req)
        self._broadcaster.broadcast_tx_ack(ack, addr)


# ---------------------------------------------------------------------------
# Pipeline task — drain source_queue
# ---------------------------------------------------------------------------


async def _pipeline_task(
    source_queue: asyncio.Queue[tuple[str, object]],
    aggregator: DiversityAggregator,
    decode_chain: DecodeChain,
    fanout: FanOut,
    stop: asyncio.Event,
) -> None:
    last_flush = time.monotonic()
    while not stop.is_set():
        try:
            item = await asyncio.wait_for(
                source_queue.get(), timeout=_AGG_FLUSH_INTERVAL_S
            )
        except TimeoutError:
            item = None
        if item is not None:
            source, event = item
            if isinstance(event, LoraFrame):
                cand = extract_candidate(event, time.monotonic(), source)
                if cand is not None:
                    aggregator.feed(cand)
            elif isinstance(event, Status):
                fanout.feed_status(source, event)
            else:
                fanout.feed_telemetry(source, event)
        now = time.monotonic()
        if now - last_flush >= _AGG_FLUSH_INTERVAL_S:
            for frame in aggregator.flush_expired(now):
                annotated = decode_chain.process(frame)
                fanout.feed_frame(annotated)
            last_flush = now


async def _reaper_task(broadcaster: UdpBroadcaster, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=_SUBSCRIBER_REAP_INTERVAL_S)
            return
        except TimeoutError:
            broadcaster.reap_stale(time.monotonic(), _SUBSCRIBER_MAX_AGE_S)


# ---------------------------------------------------------------------------
# run_daemon — main asyncio orchestration
# ---------------------------------------------------------------------------


async def run_daemon(config: DaemonConfig, *, config_path: Path | None = None) -> int:
    """Run the daemon until shutdown is signalled. Returns 0 on clean exit.

    ``config_path`` enables Phase 2F SIGHUP-driven hot reload. When it
    is ``None`` the daemon still installs SIGHUP (because TTY-disconnect
    would otherwise kill the process), but the handler is the
    placeholder log-only handler from :mod:`lora.daemon.lifecycle`.
    """
    if not config.upstreams:
        _log.error("no upstreams configured; nothing to do")
        return 2

    # Storage + identity.
    identity = IdentityStore(config.identity)
    identity.warm_up()
    writer = DuckDBWriter(config.storage)
    try:
        writer.start()
    except DatabaseLocked as exc:
        # Another lora core (or any DuckDB writer) holds the file lock.
        # DuckDB only allows one read-write process per database; refuse
        # to start cleanly rather than spilling a traceback.
        _log.error("storage: %s", exc)
        _log.error(
            "hint: stop the other lora core process (or point this one at "
            "a different db_path) before retrying"
        )
        return 3

    # Listen socket (single UDP socket — read + write).
    listen_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listen_sock.bind((config.listen_host, config.listen_port))
    listen_sock.setblocking(False)
    _log.info("listen: %s:%d", config.listen_host, config.listen_port)

    broadcaster = UdpBroadcaster(listen_sock)
    fanout = FanOut(
        queue_depth=config.fanout_queue_depth,
        writer=writer,
        broadcaster=broadcaster,
    )
    _log.info("fanout: queue_depth=%d", config.fanout_queue_depth)
    aggregator = DiversityAggregator(
        window_ms=config.aggregator_window_ms,
        max_candidates=config.aggregator_max_candidates,
    )
    _log.info(
        "aggregator: window_ms=%d max_candidates=%d",
        config.aggregator_window_ms,
        config.aggregator_max_candidates,
    )
    decode_chain = DecodeChain(
        decoders=build_decoders(config.decoders_enabled, config.decoders_options),
        identity=identity,
    )

    # tx_proxy needs to start within the loop to install asyncio readers.
    tx_proxy = TxProxy(config.upstreams)
    await tx_proxy.start()

    # Listen socket protocol.
    loop = asyncio.get_running_loop()
    listen_transport, _ = await loop.create_datagram_endpoint(
        lambda: _ListenProtocol(broadcaster, tx_proxy, loop),
        sock=listen_sock,
    )

    # Per-upstream sources.
    source_queue: asyncio.Queue[tuple[str, object]] = asyncio.Queue(
        maxsize=config.fanout_queue_depth
    )
    sources = [
        UdpSource(u, on_config=broadcaster.set_upstream_config)
        for u in config.upstreams
    ]
    source_tasks = [asyncio.create_task(s.run(source_queue)) for s in sources]
    _log.info(
        "upstreams: %s (%d total)",
        ", ".join(f"{u.name}={u.host}:{u.port}" for u in config.upstreams),
        len(config.upstreams),
    )

    # Lifecycle.
    lifecycle = Lifecycle()
    lifecycle.install_handlers()
    pipeline_stop = asyncio.Event()

    # Phase 2F SIGHUP -> hot reload. The reloader keeps a live
    # reference to the DecodeChain and IdentityStore so it can swap
    # decoders / drop identity caches in place without restarting the
    # daemon. Restart-required fields (listen, upstreams, aggregator,
    # storage, fanout depth) are logged and ignored.
    if config_path is not None:
        hot_reloader = HotReloader(
            config_path=config_path,
            current=config,
            decode_chain=decode_chain,
            identity=identity,
            log=_log,
        )
        lifecycle.install_sighup(hot_reloader)
        _log.info("hot-reload: enabled (config=%s)", config_path)
    else:
        _log.debug("hot-reload: disabled (no config file resolved)")

    fanout_task = asyncio.create_task(fanout.run())
    pipeline = asyncio.create_task(
        _pipeline_task(source_queue, aggregator, decode_chain, fanout, pipeline_stop)
    )
    reaper = asyncio.create_task(_reaper_task(broadcaster, pipeline_stop))
    parent_watcher = asyncio.create_task(lifecycle.watch_parent())

    _log.info("lora-core ready")

    try:
        await lifecycle.wait_for_shutdown()
    finally:
        _log.info("draining…")
        # Stop sources first so the queue stops growing.
        for s in sources:
            await s.stop()
        for t in source_tasks:
            try:
                await asyncio.wait_for(t, timeout=2.0)
            except TimeoutError, asyncio.CancelledError:  # pragma: no cover
                t.cancel()
        # Flush remaining aggregator candidates so we don't lose work.
        for frame in aggregator.flush_all():
            annotated = decode_chain.process(frame)
            fanout.feed_frame(annotated)
        # Tell the pipeline + reaper to exit.
        pipeline_stop.set()
        try:
            await asyncio.wait_for(pipeline, timeout=2.0)
        except TimeoutError, asyncio.CancelledError:  # pragma: no cover
            pipeline.cancel()
        try:
            await asyncio.wait_for(reaper, timeout=2.0)
        except TimeoutError, asyncio.CancelledError:  # pragma: no cover
            reaper.cancel()
        parent_watcher.cancel()
        try:
            await asyncio.wait_for(parent_watcher, timeout=1.0)
        except TimeoutError, asyncio.CancelledError:  # pragma: no cover
            pass
        # Stop the fanout (drains writer + broadcaster pending items).
        await fanout.stop()
        try:
            await asyncio.wait_for(fanout_task, timeout=2.0)
        except TimeoutError, asyncio.CancelledError:  # pragma: no cover
            fanout_task.cancel()
        # Close the listen socket and tx_proxy sockets.
        listen_transport.close()
        await tx_proxy.stop()
        # Final writer drain + close.
        writer.stop(timeout=5.0)
        lifecycle.uninstall_handlers()
    _log.info("lora-core stopped (rc=0)")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="lora core", description="lora-core daemon")
    parser.add_argument("--config", default=None, help="path to apps/config.toml")
    parser.add_argument(
        "--log-level",
        default=None,
        help="override log level (DEBUG/INFO/WARN/ERROR)",
    )
    parser.add_argument(
        "--listen",
        default=None,
        metavar="HOST:PORT",
        help=(
            "override the daemon's listen address. With no flag, the daemon "
            "honours [core].listen (default 127.0.0.1:5555)."
        ),
    )
    return parser.parse_args(argv)


def _apply_listen_override(config: DaemonConfig, listen: str | None) -> DaemonConfig:
    """Resolve the daemon's actual listen address.

    Order of precedence:
    1. ``--listen HOST:PORT`` CLI flag (operator wins).
    2. Whatever the typed config said.
    """
    if listen is not None:
        host, port = parse_host_port(
            listen, default_host=config.listen_host, default_port=config.listen_port
        )
        return replace(config, listen_host=host, listen_port=port)
    return config


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    setproctitle.setproctitle("lora-core")
    args = _parse_args(argv)

    raw_cfg: dict[str, Any] = load_config(args.config)
    config = DaemonConfig.from_legacy_toml(raw_cfg)
    config = _apply_listen_override(config, args.listen)
    log_level = (args.log_level or config.log_level).upper()
    setup_logging("lora.daemon", log_level=log_level)

    # Resolve the config path so the SIGHUP reloader can re-read it.
    # ``find_config`` returns ``None`` if no file was located —
    # ``run_daemon`` then skips the reload wiring.
    config_path: Path | None = find_config(args.config)

    _log.info("lora-core starting (pid=%d)", os.getpid())
    _log.info("config: %s", config_path or "<defaults>")
    _log.debug("log level: %s", log_level)

    try:
        rc = asyncio.run(run_daemon(config, config_path=config_path))
    except KeyboardInterrupt:  # pragma: no cover - interactive
        rc = 130
    _log.info("lora-core stopped (rc=%d)", rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
