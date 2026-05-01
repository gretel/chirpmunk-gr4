#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""MeshCore repeater forwarding logic (PR #1810).

Phase 4 split from ``meshcore_bridge.py``: pure-function
helpers that decide whether (and how) to forward a received flood
packet, plus the maintenance routines for the bridge's TX-echo and
RX-dedup caches.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Final, Mapping

from lora.bridges.meshcore.protocol import hash_payload
from lora.core.meshcore_crypto import (
    PAYLOAD_ADVERT,
    PAYLOAD_GRP_DATA,
    PAYLOAD_GRP_TXT,
    ROUTE_FLOOD,
    ROUTE_MASK,
    decode_path_len,
)

log = logging.getLogger("lora.bridges.meshcore.repeater")

#: Hard upper bound on path-hash bytes per firmware ``Packet.h``.
MAX_PATH_SIZE: Final[int] = 64

#: Payload types that are denied flood-forwarding when ``deny_flood_broadcast``
#: is set.  Per MeshCore PR #1810: GRP_TXT, GRP_DATA, and ADVERT are
#: channel/broadcast traffic that clogs the network.  All other types
#: (REQ, RESP, TXT_MSG, ACK, ANON_REQ, PATH, TRACE, MULTI, CTRL) are
#: forwarded because they participate in establishing direct paths.
DENYF_PAYLOAD_TYPES: Final[frozenset[int]] = frozenset(
    {PAYLOAD_GRP_TXT, PAYLOAD_GRP_DATA, PAYLOAD_ADVERT}
)


def prepare_repeat_packet(
    rx_payload: bytes,
    frame_msg: Mapping[str, Any],
    our_pub: bytes,
    *,
    deny_flood_broadcast: bool = True,
) -> bytes | None:
    """Prepare a forwarded packet, or return ``None`` to skip.

    Replicates firmware ``Mesh::routeRecvPacket()`` +
    ``filterRecvFloodPacket()``:

    * Only flood packets (``ROUTE_FLOOD`` / ``ROUTE_T_FLOOD``).
    * CRC must be valid.
    * Payload type filter (PR #1810).
    * Path has room for one more hash.
    * Loop detection: our hash must not already be in the path.
    * Append our 1-byte path hash before retransmit.
    """
    if not frame_msg.get("crc_valid", False):
        log.debug("repeat: skip CRC-invalid packet")
        return None
    if len(rx_payload) < 2:
        return None

    hdr_byte = rx_payload[0]
    route = hdr_byte & ROUTE_MASK
    ptype = (hdr_byte >> 2) & 0x0F

    if route > ROUTE_FLOOD:
        log.debug("repeat: skip non-flood route=%d", route)
        return None

    if deny_flood_broadcast and ptype in DENYF_PAYLOAD_TYPES:
        log.debug("repeat: skip denied payload type 0x%02x", ptype)
        return None

    has_transport = route != ROUTE_FLOOD  # i.e. ROUTE_T_FLOOD == 0
    path_len_offset = 1 + (4 if has_transport else 0)
    if path_len_offset >= len(rx_payload):
        return None
    path_len_byte = rx_payload[path_len_offset]
    hash_count, hash_size, path_byte_len = decode_path_len(path_len_byte)

    if (hash_count + 1) * hash_size > MAX_PATH_SIZE:
        log.debug("repeat: skip — path full (%d hops)", hash_count)
        return None

    our_hash = our_pub[:hash_size]
    path_start = path_len_offset + 1
    path_end = path_start + path_byte_len
    if path_end > len(rx_payload):
        return None
    path_bytes = rx_payload[path_start:path_end]
    for i in range(hash_count):
        hop = path_bytes[i * hash_size : (i + 1) * hash_size]
        if hop == our_hash:
            log.debug("repeat: skip — loop detected (our hash at hop %d)", i)
            return None

    new_count = hash_count + 1
    new_path_len_byte = (path_len_byte & 0xC0) | (new_count & 0x3F)
    pkt = bytearray(rx_payload)
    pkt[path_len_offset] = new_path_len_byte
    pkt[path_end:path_end] = our_hash
    return bytes(pkt)


# ---------------------------------------------------------------------------
# TX-echo / RX-dedup cache helpers
# ---------------------------------------------------------------------------


def expire_recent_tx(cache: dict[bytes, float], ttl: float) -> None:
    """Drop entries older than ``ttl`` seconds (monotonic)."""
    now = time.monotonic()
    cutoff = now - ttl
    expired = [k for k, ts in cache.items() if ts < cutoff]
    for k in expired:
        log.debug("echo-filter: expired hash %s", k.hex())
        del cache[k]


def is_tx_echo(rx_payload: bytes, recent_tx: dict[bytes, float]) -> bool:
    """Return ``True`` if ``rx_payload`` matches a recently-sent TX hash.

    Removes the matching entry from ``recent_tx`` so a single TX echoes
    only once.
    """
    if not rx_payload:
        return False
    h = hash_payload(rx_payload)
    if h in recent_tx:
        log.debug("echo-filter: matched TX hash %s, dropping echo", h.hex())
        del recent_tx[h]
        return True
    log.debug("echo-filter: RX hash %s (no match, %d tracked)", h.hex(), len(recent_tx))
    return False


def is_rx_duplicate(
    rx_payload: bytes, recent_rx: dict[bytes, float], ttl: float
) -> bool:
    """Track ``rx_payload`` for dedup; return ``True`` if seen recently."""
    if not rx_payload:
        return False
    rx_h = hash_payload(rx_payload)
    now = time.monotonic()
    expired = [k for k, ts in recent_rx.items() if now - ts > ttl]
    for k in expired:
        del recent_rx[k]
    if rx_h in recent_rx:
        log.debug("dedup: suppressed duplicate RX %s", rx_h.hex())
        return True
    recent_rx[rx_h] = now
    return False


__all__ = [
    "DENYF_PAYLOAD_TYPES",
    "MAX_PATH_SIZE",
    "expire_recent_tx",
    "is_rx_duplicate",
    "is_tx_echo",
    "prepare_repeat_packet",
]
