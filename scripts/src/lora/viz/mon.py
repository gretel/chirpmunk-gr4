#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Live frame viewer — thin renderer over lora-core's typed event bus.

Subscribes to a lora-core daemon (default ``127.0.0.1:5555``), validates
each incoming CBOR map via :mod:`lora.core.schema`, and renders
``lora_frame`` events with :func:`lora.core.formatters.format_frame`.
The daemon already runs the protocol-decoder layer and attaches a
``protocol`` sub-map to each frame, so this viewer never re-parses the
payload bytes.

Phase 3 thin-renderer responsibilities:

  - subscribe (no sync_word filter — show every decoded frame)
  - re-send keepalive every :data:`lora.core.udp.KEEPALIVE_INTERVAL` s
  - render ``lora_frame`` via ``format_frame`` (protocol sub-map path)
  - render ``status`` heartbeats and ``config`` greetings as one-liners
  - silently drop malformed CBOR / unknown event types
"""

from __future__ import annotations

import argparse
import logging
import socket
import sys
import time
from typing import Any

import cbor2
from setproctitle import setproctitle

from lora.core.constants import sync_word_name
from lora.core.formatters import format_frame, sanitize_text
from lora.core.logging import add_logging_args, setup_logging
from lora.core.udp import (
    KEEPALIVE_INTERVAL,
    create_udp_subscriber,
    parse_host_port,
)

log = logging.getLogger("lora.viz.mon")

#: Default lora-core listen address during the Phase 2/3 parallel-run.
#: Phase 7 cuts the legacy lora_trx :5555 path; until then ``--connect``
#: lets operators point at any local or remote daemon instance.
DEFAULT_CONNECT = "127.0.0.1:5555"


def format_config(msg: dict[str, Any]) -> str:
    """Format a daemon ``config`` greeting as a two-line summary.

    All string fields are sanitized (the message arrives over UDP and
    is not trusted). Mirrors the legacy ``lora_mon.format_config``
    look so existing operator muscle memory still works.
    """
    phy = msg.get("phy") or {}
    server = msg.get("server") or {}
    freq = phy.get("freq", 0) or 0
    freq_mhz = freq / 1e6 if freq else 0.0
    sf = phy.get("sf", "?")
    bw = phy.get("bw", 0) or 0
    bw_khz = bw / 1e3 if bw else 0
    cr = phy.get("cr", "?")
    sw = phy.get("sync_word", "?")
    sw_name = sync_word_name(sw) if isinstance(sw, int) else str(sw)
    rx_g = phy.get("rx_gain", "?")
    tx_g = phy.get("tx_gain", "?")
    device = sanitize_text(str(server.get("device", "?")))
    rate = server.get("sample_rate", 0) or 0
    rate_ks = rate / 1e3 if rate else 0
    si = server.get("status_interval", 0) or 0

    cr_label = f"CR4:{4 + cr}" if isinstance(cr, int) else f"CR4:{cr}"
    bw_str = f"BW{bw_khz:g}k" if bw_khz else "BW?"
    status_str = f"status every {si}s" if si else "status off"
    return (
        f"--- config: {freq_mhz:.3f} MHz SF{sf} {bw_str} {cr_label} {sw_name}\n"
        f"    gain: RX={rx_g} dB  TX={tx_g} dB  |  {device} @ {rate_ks:.0f} kS/s  "
        f"{status_str}"
    )


def format_status(msg: dict[str, Any]) -> str:
    """Format a periodic ``status`` heartbeat as one human-readable line."""
    ts = sanitize_text(str(msg.get("ts", "")))
    ts_short = ts[11:19] if len(ts) > 11 else ts
    phy = msg.get("phy") or {}
    frames = msg.get("frames") or {}
    total = frames.get("total", 0) or 0
    ok = frames.get("crc_ok", 0) or 0
    fail = frames.get("crc_fail", 0) or 0
    pct = ok / total * 100 if total > 0 else 0
    rx_g = phy.get("rx_gain", "?")
    return f"[{ts_short}] {total} frames ({ok} OK, {fail} fail, {pct:.0f}%) RX gain={rx_g} dB"


def _render(msg: dict[str, Any]) -> str | None:
    """Return one display line for a recognised CBOR event, or ``None``."""
    msg_type = msg.get("type")
    if msg_type == "lora_frame":
        # format_frame reads msg["protocol"] directly when present —
        # the daemon attaches it upstream during the decode pipeline.
        return format_frame(msg)
    if msg_type == "status":
        return format_status(msg)
    if msg_type == "config":
        return format_config(msg)
    return None


def _consume(host: str, port: int) -> int:
    """Subscribe to ``host:port`` and stream events until interrupted.

    Returns 0 on clean shutdown, never raises out of the hot path
    (cbor2 errors, schema mismatches, and short reads are all
    silently skipped — the viewer is best-effort).
    """
    # No sync_word filter: monitor every decoded frame regardless of
    # protocol so operators can see lorawan / private nets
    # alongside meshcore.
    sock, sub_msg, addr = create_udp_subscriber(host, port, sync_words=None)
    local_port = sock.getsockname()[1]
    log.info("connected to %s:%d (local port :%d)", host, port, local_port)

    last_keepalive = time.monotonic()
    waiting = False

    try:
        while True:
            try:
                data, _addr = sock.recvfrom(65536)
                waiting = False
            except TimeoutError, socket.timeout:
                # Re-subscribe in case the daemon restarted; this also
                # acts as the keepalive when traffic is sparse.
                sock.sendto(sub_msg, addr)
                last_keepalive = time.monotonic()
                if not waiting:
                    log.info("waiting for frames from %s:%d", host, port)
                    waiting = True
                continue

            now = time.monotonic()
            if now - last_keepalive >= KEEPALIVE_INTERVAL:
                sock.sendto(sub_msg, addr)
                last_keepalive = now

            try:
                msg = cbor2.loads(data)
            except Exception:
                continue
            if not isinstance(msg, dict):
                continue

            line = _render(msg)
            if line is not None:
                # status / config go to debug, frames to info — matches
                # the legacy lora_mon convention.
                if msg.get("type") == "lora_frame":
                    log.info("%s", line)
                else:
                    log.debug("%s", line)
    except KeyboardInterrupt:
        return 0
    finally:
        sock.close()


def main(argv: list[str] | None = None) -> int:
    """Entry point — blocking; returns 0 on Ctrl-C / EOF."""
    setproctitle("lora-mon")

    parser = argparse.ArgumentParser(
        prog="lora mon",
        description=(
            "Live frame viewer — subscribes to a lora-core daemon and "
            "prints decoded frames as they arrive."
        ),
    )
    parser.add_argument(
        "--connect",
        metavar="HOST:PORT",
        default=DEFAULT_CONNECT,
        help=f"daemon UDP address (default: {DEFAULT_CONNECT})",
    )
    add_logging_args(parser)
    args = parser.parse_args(argv)

    setup_logging("gr4.mon", log_level=args.log_level, no_color=args.no_color)

    try:
        host, port = parse_host_port(args.connect)
    except ValueError as exc:
        parser.error(str(exc))
        return 2  # parser.error already exits, this is for type checkers

    return _consume(host, port)


if __name__ == "__main__":
    sys.exit(main())
