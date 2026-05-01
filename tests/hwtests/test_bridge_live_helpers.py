#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Tests for ``lora.hwtests.bridge_live`` pure helpers."""

from __future__ import annotations

import struct

from lora.hwtests.bridge_live import (
    FRAME_RX,
    FRAME_TX,
    RESP_CONTACT,
    RESP_SELF_INFO,
    build_synthetic_advert,
    extract_self_info_pubkey,
    find_contact,
    frame_encode_tx,
)


class TestFrameEncodeTx:
    def test_minimal(self) -> None:
        out = frame_encode_tx(b"\x01")
        assert out[0] == FRAME_TX
        assert struct.unpack_from("<H", out, 1)[0] == 1
        assert out[3:] == b"\x01"

    def test_long_payload(self) -> None:
        payload = b"x" * 4096
        out = frame_encode_tx(payload)
        assert struct.unpack_from("<H", out, 1)[0] == 4096
        assert out[3:] == payload


class TestBuildSyntheticAdvert:
    def test_field_offsets(self) -> None:
        pk = b"\xab" * 32
        adv = build_synthetic_advert(pk, "alice")
        # header(1) + path_len(1) + pubkey(32) + ts(4) + sig(64) + flags(1) + name
        assert len(adv) >= 1 + 1 + 32 + 4 + 64 + 1 + len("alice")
        assert adv[0] == (0x04 << 2) | 0x01  # PAYLOAD_ADVERT | ROUTE_FLOOD
        assert adv[1] == 0
        assert adv[2:34] == pk
        # Signature is zero-filled
        assert adv[38:102] == b"\x00" * 64

    def test_truncates_long_name(self) -> None:
        adv = build_synthetic_advert(b"\x00" * 32, "x" * 64)
        # name slot starts at offset 103 (header+path+pk+ts+sig+flags)
        name = adv[103:]
        assert len(name) == 32  # legacy clamps to 32 bytes


class TestFindContact:
    def test_present(self) -> None:
        pk = b"\xff" * 32
        contact_frame = bytes([RESP_CONTACT]) + pk + b"trailing"
        assert find_contact([contact_frame], pk) is True

    def test_absent(self) -> None:
        contact_frame = bytes([RESP_CONTACT]) + b"\x00" * 32
        assert find_contact([contact_frame], b"\xff" * 32) is False

    def test_skips_non_contact_frames(self) -> None:
        # Frame whose first byte isn't RESP_CONTACT must not match.
        wrong = bytes([0x05]) + b"\xff" * 32
        assert find_contact([wrong], b"\xff" * 32) is False

    def test_empty_list(self) -> None:
        assert find_contact([], b"\xff" * 32) is False


class TestExtractSelfInfoPubkey:
    def test_extracts_correctly(self) -> None:
        pk = b"\xaa" * 32
        # SELF_INFO: [0x05][adv_type(1)][tx_power(1)][max_tx_power(1)][pubkey(32)]
        f = bytes([RESP_SELF_INFO, 0x01, 0x02, 0x03]) + pk
        assert extract_self_info_pubkey([f]) == pk

    def test_returns_none_when_missing(self) -> None:
        # No SELF_INFO frame in list.
        assert extract_self_info_pubkey([bytes([RESP_CONTACT]) + b"x" * 33]) is None

    def test_returns_none_when_too_short(self) -> None:
        f = bytes([RESP_SELF_INFO]) + b"\x00" * 5  # < 36 bytes total
        assert extract_self_info_pubkey([f]) is None


class TestFrameConstants:
    def test_tx_rx_distinct(self) -> None:
        assert FRAME_TX != FRAME_RX
