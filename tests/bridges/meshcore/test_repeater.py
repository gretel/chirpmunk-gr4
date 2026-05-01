#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Tests for :mod:`lora.bridges.meshcore.repeater`.

Covers MeshCore PR #1810 cases: deny-flood-broadcast (GRP_TXT, GRP_DATA,
ADVERT), path room check, loop detection, and TX-echo / RX-dedup
caches.
"""

from __future__ import annotations

from lora.bridges.meshcore.protocol import hash_payload
from lora.bridges.meshcore.repeater import (
    DENYF_PAYLOAD_TYPES,
    expire_recent_tx,
    is_rx_duplicate,
    is_tx_echo,
    prepare_repeat_packet,
)
from lora.core.meshcore_crypto import (
    PAYLOAD_ADVERT,
    PAYLOAD_GRP_DATA,
    PAYLOAD_GRP_TXT,
    PAYLOAD_TXT,
    ROUTE_DIRECT,
    ROUTE_FLOOD,
    ROUTE_T_FLOOD,
)


def _flood_pkt(ptype: int, *, path_count: int = 0, route: int = ROUTE_FLOOD) -> bytes:
    """Build a minimal flood packet with `path_count` 1-byte path hashes."""
    header = (ptype << 2) | route
    transport = b"\xaa\xaa\xbb\xbb" if route == ROUTE_T_FLOOD else b""
    # path_len byte: low 6 bits = hash_count; bits [7:6] = (hash_size-1)
    # hash_size = 1 → bits [7:6] = 00.
    path_len_byte = path_count & 0x3F
    path_bytes = bytes(range(1, path_count + 1))
    payload = b"\xde\xad\xbe\xef"
    return bytes([header]) + transport + bytes([path_len_byte]) + path_bytes + payload


class TestPrepareRepeatPacket:
    def test_skip_invalid_crc(self) -> None:
        pkt = _flood_pkt(PAYLOAD_TXT)
        out = prepare_repeat_packet(pkt, {"crc_valid": False}, b"\xaa" * 32)
        assert out is None

    def test_skip_non_flood(self) -> None:
        pkt = _flood_pkt(PAYLOAD_TXT, route=ROUTE_DIRECT)
        out = prepare_repeat_packet(pkt, {"crc_valid": True}, b"\xaa" * 32)
        assert out is None

    def test_denyf_grp_txt(self) -> None:
        pkt = _flood_pkt(PAYLOAD_GRP_TXT)
        out = prepare_repeat_packet(pkt, {"crc_valid": True}, b"\xaa" * 32)
        assert out is None

    def test_denyf_grp_data(self) -> None:
        pkt = _flood_pkt(PAYLOAD_GRP_DATA)
        out = prepare_repeat_packet(pkt, {"crc_valid": True}, b"\xaa" * 32)
        assert out is None

    def test_denyf_advert(self) -> None:
        pkt = _flood_pkt(PAYLOAD_ADVERT)
        out = prepare_repeat_packet(pkt, {"crc_valid": True}, b"\xaa" * 32)
        assert out is None

    def test_denyf_disabled_allows_grp_txt(self) -> None:
        pkt = _flood_pkt(PAYLOAD_GRP_TXT)
        out = prepare_repeat_packet(
            pkt, {"crc_valid": True}, b"\xaa" * 32, deny_flood_broadcast=False
        )
        assert out is not None

    def test_appends_our_hash(self) -> None:
        pkt = _flood_pkt(PAYLOAD_TXT)
        our_pub = b"\x42" * 32
        out = prepare_repeat_packet(pkt, {"crc_valid": True}, our_pub)
        assert out is not None
        # path_len byte at offset 1 (no transport_codes), low 6 bits = 1
        assert out[1] & 0x3F == 1
        # Our 1-byte hash inserted before payload
        assert out[2] == 0x42

    def test_loop_detection(self) -> None:
        our_pub = b"\xcc" * 32
        # Build a packet that already has our hash in the path
        ptype = PAYLOAD_TXT
        header = (ptype << 2) | ROUTE_FLOOD
        path_len_byte = 1  # 1 hop, hash_size 1
        # Our hash is the first byte of our_pub
        existing_path = bytes([our_pub[0]])
        payload = b"\xff"
        pkt = bytes([header]) + bytes([path_len_byte]) + existing_path + payload
        out = prepare_repeat_packet(pkt, {"crc_valid": True}, our_pub)
        assert out is None

    def test_path_room_check(self) -> None:
        # path_len byte: low 6 bits = hash_count, high 2 = hash_size-1.
        # hash_size=2, hash_count=32 → path = 32*2 = 64 bytes (= MAX_PATH_SIZE).
        # Adding one more hop: (32+1)*2 = 66 > 64 — rejected.
        ptype = PAYLOAD_TXT
        header = (ptype << 2) | ROUTE_FLOOD
        path_len_byte = (1 << 6) | 32  # hash_size=2, hash_count=32
        path_bytes = bytes(range(64))
        payload = b"\xff"
        pkt = bytes([header]) + bytes([path_len_byte]) + path_bytes + payload
        out = prepare_repeat_packet(pkt, {"crc_valid": True}, b"\x00" * 32)
        assert out is None


class TestExpireRecentTx:
    def test_expire_drops_old_entries(self) -> None:
        import time

        cache: dict[bytes, float] = {}
        now = time.monotonic()
        cache[b"old"] = now - 100.0
        cache[b"new"] = now
        expire_recent_tx(cache, ttl=30.0)
        assert b"old" not in cache
        assert b"new" in cache


class TestIsTxEcho:
    def test_match_pops_entry(self) -> None:
        import time

        payload = b"hello"
        h = hash_payload(payload)
        cache: dict[bytes, float] = {h: time.monotonic()}
        assert is_tx_echo(payload, cache) is True
        assert h not in cache

    def test_no_match_keeps_cache(self) -> None:
        import time

        payload = b"a"
        other_h = hash_payload(b"b")
        cache: dict[bytes, float] = {other_h: time.monotonic()}
        assert is_tx_echo(payload, cache) is False
        assert other_h in cache

    def test_empty_payload(self) -> None:
        assert is_tx_echo(b"", {}) is False


class TestIsRxDuplicate:
    def test_first_seen_not_dup(self) -> None:
        cache: dict[bytes, float] = {}
        assert is_rx_duplicate(b"hello", cache, ttl=5.0) is False
        assert hash_payload(b"hello") in cache

    def test_second_seen_is_dup(self) -> None:
        cache: dict[bytes, float] = {}
        is_rx_duplicate(b"hello", cache, ttl=5.0)
        assert is_rx_duplicate(b"hello", cache, ttl=5.0) is True

    def test_dedup_expires(self) -> None:
        import time

        cache: dict[bytes, float] = {}
        is_rx_duplicate(b"hello", cache, ttl=5.0)
        # Force expire by mutating timestamp directly
        cache[hash_payload(b"hello")] = time.monotonic() - 10.0
        assert is_rx_duplicate(b"hello", cache, ttl=5.0) is False


def test_denyf_constant() -> None:
    assert PAYLOAD_GRP_TXT in DENYF_PAYLOAD_TYPES
    assert PAYLOAD_GRP_DATA in DENYF_PAYLOAD_TYPES
    assert PAYLOAD_ADVERT in DENYF_PAYLOAD_TYPES
    assert PAYLOAD_TXT not in DENYF_PAYLOAD_TYPES
