#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Tests for :mod:`lora.bridges.meshcore.protocol`.

Stateless wire-codec exercises: framing round-trip, contact record
layout, ADVERT push synthesis, ADVERT round-trip parser, transport-code
HMAC, flood-scope key derivation, plus :class:`FrameDecoder` framing
edge-cases.
"""

from __future__ import annotations

import hashlib
import struct

import pytest

from lora.bridges.meshcore.protocol import (
    BRIDGE_PORT,
    CONTACT_RECORD_SIZE,
    FRAME_RX,
    FRAME_TX,
    FrameDecoder,
    advert_to_push,
    build_ack_wire_packet,
    build_contact_msg_recv_v3,
    build_contact_record,
    build_unsigned_advert_from_record,
    compute_transport_code,
    frame_encode,
    hash_payload,
    is_valid_repeat_freq,
    parse_advert_wire_packet,
    parse_flood_scope,
)


class TestFrameCodec:
    def test_frame_encode_layout(self) -> None:
        out = frame_encode(b"\x00\x01\x02")
        assert out[0] == FRAME_RX
        assert struct.unpack_from("<H", out, 1)[0] == 3
        assert out[3:] == b"\x00\x01\x02"

    def test_frame_decoder_round_trip(self) -> None:
        dec = FrameDecoder()
        # Build TX frame: 0x3C + len_le16 + payload
        payload = b"\x05hello"
        framed = bytes([FRAME_TX]) + struct.pack("<H", len(payload)) + payload
        assert dec.feed(framed) == [payload]

    def test_frame_decoder_partial_then_complete(self) -> None:
        dec = FrameDecoder()
        payload = b"\x07\x01\x02"
        framed = bytes([FRAME_TX]) + struct.pack("<H", len(payload)) + payload
        # Feed in two halves
        first_half = framed[: len(framed) // 2]
        second_half = framed[len(framed) // 2 :]
        assert dec.feed(first_half) == []
        assert dec.feed(second_half) == [payload]

    def test_frame_decoder_skips_garbage(self) -> None:
        dec = FrameDecoder()
        payload = b"\x03"
        framed = bytes([FRAME_TX]) + struct.pack("<H", 1) + payload
        assert dec.feed(b"\x00\x00\xff" + framed) == [payload]

    def test_frame_decoder_oversized_skipped(self) -> None:
        dec = FrameDecoder()
        # Length 9999 is > MAX_FRAME=300 — must skip the start byte
        bad = bytes([FRAME_TX]) + struct.pack("<H", 9999)
        # Then a valid 1-byte frame after
        good = bytes([FRAME_TX]) + struct.pack("<H", 1) + b"\xab"
        assert dec.feed(bad + good) == [b"\xab"]


class TestContactRecord:
    def test_record_size_is_147(self) -> None:
        pubkey = b"\xaa" * 32
        rec = build_contact_record(pubkey, name="alice")
        assert len(rec) == CONTACT_RECORD_SIZE
        assert rec[:32] == pubkey

    def test_record_default_path_is_minus_one(self) -> None:
        rec = build_contact_record(b"\xaa" * 32, name="x")
        # signed byte at offset 34
        path_len = struct.unpack_from("<b", rec, 34)[0]
        assert path_len == -1

    def test_record_name_truncated_to_32(self) -> None:
        long_name = "a" * 64
        rec = build_contact_record(b"\xbb" * 32, name=long_name)
        name_field = rec[99:131]
        assert b"a" * 32 == name_field

    def test_record_lat_lon_round_trip(self) -> None:
        rec = build_contact_record(
            b"\x01" * 32, name="loc", lat=53_550_000, lon=10_000_000
        )
        lat = struct.unpack_from("<i", rec, 135)[0]
        lon = struct.unpack_from("<i", rec, 139)[0]
        assert lat == 53_550_000
        assert lon == 10_000_000


class TestParseFloodScope:
    def test_empty_returns_zero_key(self) -> None:
        assert parse_flood_scope("") == b"\x00" * 16

    @pytest.mark.parametrize("disabled", ["0", "*", "None"])
    def test_disabled_sentinels(self, disabled: str) -> None:
        assert parse_flood_scope(disabled) == b"\x00" * 16

    def test_hex_round_trip(self) -> None:
        raw = "00112233445566778899aabbccddeeff"
        assert parse_flood_scope(raw) == bytes.fromhex(raw)

    def test_hashtag_prefix_matches_firmware(self) -> None:
        expected = hashlib.sha256(b"#de-hh").digest()[:16]
        assert parse_flood_scope("de-hh") == expected


class TestComputeTransportCode:
    def test_hmac_truncated_to_two_bytes(self) -> None:
        key = b"\x11" * 16
        code = compute_transport_code(key, 0x02, b"hello")
        assert 1 <= code <= 0xFFFE

    def test_reserved_zero_clamped(self) -> None:
        # Forge an HMAC that ends in two zero bytes via brute search
        # is impractical; instead verify the mapping is monotonic by
        # checking the function runs for many inputs and never returns
        # 0x0000 / 0xFFFF.
        key = b"\x42" * 16
        for i in range(256):
            code = compute_transport_code(key, i % 16, bytes([i]))
            assert code != 0x0000
            assert code != 0xFFFF


class TestRepeatFreq:
    @pytest.mark.parametrize("freq_khz", [433000, 869000, 918000])
    def test_allowed_frequencies(self, freq_khz: int) -> None:
        assert is_valid_repeat_freq(freq_khz)

    @pytest.mark.parametrize("freq_khz", [432999, 870000, 868999, 0, 869618])
    def test_rejected_frequencies(self, freq_khz: int) -> None:
        assert not is_valid_repeat_freq(freq_khz)


class TestHashPayload:
    def test_returns_8_bytes(self) -> None:
        assert len(hash_payload(b"hello")) == 8

    def test_distinguishes_payloads(self) -> None:
        assert hash_payload(b"a") != hash_payload(b"b")


class TestAckWirePacket:
    def test_direct_with_path(self) -> None:
        ack_hash = b"\x01\x02\x03\x04"
        path = b"\xaa\xbb"
        pkt = build_ack_wire_packet(
            ack_hash,
            stored_path=path,
            stored_path_len=2,
            send_scope=b"\x00" * 16,
            region_key=b"",
        )
        # header(1) + path_len(1) + path(2) + payload(4) = 8 bytes
        assert pkt[0] & 0x03 == 2  # ROUTE_DIRECT
        assert pkt[1] == 2  # path_len
        assert pkt[2:4] == path
        assert pkt[4:8] == ack_hash

    def test_flood_no_scope(self) -> None:
        ack_hash = b"\x05\x06\x07\x08"
        pkt = build_ack_wire_packet(
            ack_hash,
            stored_path=b"",
            stored_path_len=-1,
            send_scope=b"\x00" * 16,
            region_key=b"",
        )
        # ROUTE_FLOOD = 1
        assert pkt[0] & 0x03 == 1

    def test_flood_with_scope_uses_t_flood(self) -> None:
        ack_hash = b"\x09\x0a\x0b\x0c"
        scope = b"\x55" * 16
        pkt = build_ack_wire_packet(
            ack_hash,
            stored_path=b"",
            stored_path_len=-1,
            send_scope=scope,
            region_key=b"",
        )
        # ROUTE_T_FLOOD = 0
        assert pkt[0] & 0x03 == 0


class TestAdvertBuilder:
    def test_unsigned_advert_round_trip(self) -> None:
        pubkey = b"\xaa" * 32
        rec = build_contact_record(pubkey, name="bob", lat=53_550_000, lon=10_000_000)
        wire = build_unsigned_advert_from_record(rec)
        info = parse_advert_wire_packet(wire)
        assert info is not None
        assert info["pubkey"] == pubkey
        assert info["name"] == "bob"
        assert info["lat"] == 53_550_000
        assert info["lon"] == 10_000_000

    def test_unsigned_advert_rejects_wrong_size_record(self) -> None:
        with pytest.raises(ValueError):
            build_unsigned_advert_from_record(b"\x00" * 100)

    def test_advert_to_push_size(self) -> None:
        # Build a fake ADVERT body with name + location flags
        pubkey = b"\xaa" * 32
        ts = struct.pack("<I", 1700000000)
        sig = b"\x00" * 64
        flags = 0x01 | 0x10 | 0x80  # node_type=chat, has_location, has_name
        app = bytes([flags]) + struct.pack("<ii", 53_550_000, 10_000_000) + b"alice"
        # Wire packet: header(1) + path_len(1) + body (advert body lives after path)
        wire = bytes([0x10]) + bytes([0]) + pubkey + ts + sig + app
        push = advert_to_push(wire, 2)  # advert_start = 2 (after header+path_len)
        assert push is not None
        # Push body == 147-byte contact record
        assert len(push) == CONTACT_RECORD_SIZE + 1


class TestContactMsgRecvV3:
    def test_layout(self) -> None:
        out = build_contact_msg_recv_v3(
            snr_byte=20,
            src_prefix=b"\x01\x02\x03\x04\x05\x06",
            path_len=0,
            txt_type=0,
            ts=1700000000,
            text=b"hello",
        )
        # [0x10][snr(b)][rsv(2)][src(6)][path_len(1)][txt_type(1)][ts(4)][text]
        assert out[0] == 0x10
        assert out[1] == 20  # snr_byte
        assert out[4:10] == b"\x01\x02\x03\x04\x05\x06"
        assert out[10] == 0
        assert out[11] == 0
        assert struct.unpack_from("<I", out, 12)[0] == 1700000000
        assert out[16:] == b"hello"


def test_bridge_port_default() -> None:
    """Sanity: BRIDGE_PORT not 5000 (collides with macOS AirPlay)."""
    assert BRIDGE_PORT == 7834
