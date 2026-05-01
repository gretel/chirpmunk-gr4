#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Tests for :mod:`lora.bridges.meshcore.companion`.

Covers per-command round-trips with mocked UDP socket — APP_START,
GET_CONTACTS, SEND_TXT_MSG, SEND_CHAN_TXT_MSG, GET_CONTACT_BY_KEY,
SEND_SELF_ADVERT, SET_RADIO_PARAMS, RESET_PATH — plus a basic RX-frame
ingest exercise (lora_frame_to_companion_msgs ADVERT path).
"""

from __future__ import annotations

import struct

import conftest

from lora.bridges.meshcore.companion import (
    handle_command,
    lora_frame_to_companion_msgs,
)
from lora.bridges.meshcore.protocol import (
    CMD_APP_START,
    CMD_DEVICE_QUERY,
    CMD_GET_CONTACT_BY_KEY,
    CMD_GET_CONTACTS,
    CMD_GET_DEFAULT_FLOOD_SCOPE,
    CMD_RESET_PATH,
    CMD_SEND_CHAN_TXT_MSG,
    CMD_SEND_SELF_ADVERT,
    CMD_SEND_TXT_MSG,
    CMD_SET_DEFAULT_FLOOD_SCOPE,
    CMD_SET_RADIO_PARAMS,
    CMD_SYNC_NEXT_MESSAGE,
    PUSH_NEW_ADVERT,
    RESP_CONTACT,
    RESP_CONTACT_END,
    RESP_CONTACT_START,
    RESP_DEFAULT_FLOOD_SCOPE,
    RESP_DEVICE_INFO,
    RESP_ERROR,
    RESP_MSG_SENT,
    RESP_NO_MORE_MSGS,
    RESP_OK,
    RESP_SELF_INFO,
    build_contact_record,
)


class TestAppStart:
    def test_responses_include_self_info_and_advert_push(self) -> None:
        state = conftest.make_state(name="alice")
        responses = handle_command(bytes([CMD_APP_START]), state, None, None)
        # SELF_INFO, then PUSH_NEW_ADVERT
        assert responses[0][0] == RESP_SELF_INFO
        assert responses[1][0] == PUSH_NEW_ADVERT
        # APP_START seeds self into contacts
        assert state.pub_key.hex() in state.contacts


class TestDeviceQuery:
    def test_returns_82_byte_device_info(self) -> None:
        state = conftest.make_state()
        out = handle_command(bytes([CMD_DEVICE_QUERY]), state, None, None)
        assert len(out) == 1
        assert out[0][0] == RESP_DEVICE_INFO


class TestGetContacts:
    def test_empty_contacts_emits_start_end(self) -> None:
        state = conftest.make_state()
        out = handle_command(bytes([CMD_GET_CONTACTS]), state, None, None)
        # CONTACT_START + CONTACT_END
        assert len(out) == 2
        assert out[0][0] == RESP_CONTACT_START
        assert out[1][0] == RESP_CONTACT_END
        # count = 0
        assert struct.unpack_from("<I", out[0], 1)[0] == 0

    def test_returns_added_contact(self) -> None:
        state = conftest.make_state()
        peer = b"\xaa" * 32
        record = build_contact_record(peer, name="peer")
        state.contacts[peer.hex()] = record
        out = handle_command(bytes([CMD_GET_CONTACTS]), state, None, None)
        assert out[0][0] == RESP_CONTACT_START
        assert struct.unpack_from("<I", out[0], 1)[0] == 1
        assert out[1][0] == RESP_CONTACT
        assert out[1][1:] == record
        assert out[2][0] == RESP_CONTACT_END


class TestGetContactByKey:
    def test_resolves_full_pubkey_via_prefix(self) -> None:
        state = conftest.make_state()
        peer = b"\xbb" * 32
        record = build_contact_record(peer, name="bob")
        state.contacts[peer.hex()] = record
        out = handle_command(
            bytes([CMD_GET_CONTACT_BY_KEY]) + peer[:6], state, None, None
        )
        assert out[0][0] == RESP_CONTACT
        assert out[0][1:] == record


class TestSyncNextMessage:
    def test_empty_returns_no_more_msgs(self) -> None:
        state = conftest.make_state()
        out = handle_command(bytes([CMD_SYNC_NEXT_MESSAGE]), state, None, None)
        assert out[0][0] == RESP_NO_MORE_MSGS

    def test_pops_queued_message(self) -> None:
        state = conftest.make_state()
        state.msg_queue.append(b"\x07hello")
        out = handle_command(bytes([CMD_SYNC_NEXT_MESSAGE]), state, None, None)
        assert out == [b"\x07hello"]
        assert not state.msg_queue


class TestSetRadioParams:
    def test_in_memory_phy_update(self) -> None:
        state = conftest.make_state()
        # freq_hz(4)+bw_hz(4)+sf(1)+cr(1)
        data = struct.pack("<II", 868_000_000, 125_000) + bytes([10, 5])
        out = handle_command(bytes([CMD_SET_RADIO_PARAMS]) + data, state, None, None)
        assert out[0][0] == RESP_OK
        assert state.freq_mhz == 868.0
        assert state.bw_khz == 125.0
        assert state.sf == 10
        assert state.cr == 5


class TestResetPath:
    def test_resets_all_contact_paths(self) -> None:
        state = conftest.make_state()
        peer = b"\xcc" * 32
        record = bytearray(build_contact_record(peer, name="x"))
        # Set out_path_len = 3
        struct.pack_into("<b", record, 34, 3)
        record[35:38] = b"\x01\x02\x03"
        state.contacts[peer.hex()] = bytes(record)
        handle_command(bytes([CMD_RESET_PATH]), state, None, None)
        new_path_len = struct.unpack_from("<b", state.contacts[peer.hex()], 34)[0]
        assert new_path_len == -1


class TestSendTxtMsg:
    def test_unknown_contact_returns_error(self) -> None:
        state = conftest.make_state()
        sock = conftest.FakeUDPSock()
        ts = 1700000000
        # type(1)+attempt(1)+ts(4)+dst(6) — destination not in contacts
        data = (
            bytes([0x00, 0x00])
            + struct.pack("<I", ts)
            + b"\xde\xad\xbe\xef\xca\xfe"
            + b"hello"
        )
        out = handle_command(
            bytes([CMD_SEND_TXT_MSG]) + data, state, sock, ("127.0.0.1", 5555)
        )
        assert out[0][0] == 0x01  # RESP_ERROR
        assert sock.sent == []

    def test_known_contact_sends_msg(self) -> None:
        state = conftest.make_state()
        sock = conftest.FakeUDPSock()
        # Add a contact (we won't actually decrypt — just need a destination)
        from nacl.signing import SigningKey

        peer_seed = b"\x11" * 32
        peer_pub = bytes(SigningKey(peer_seed).verify_key)
        record = build_contact_record(peer_pub, name="peer")
        state.contacts[peer_pub.hex()] = record
        ts = 1700000000
        data = bytes([0x00, 0x00]) + struct.pack("<I", ts) + peer_pub[:6] + b"hi"
        out = handle_command(
            bytes([CMD_SEND_TXT_MSG]) + data, state, sock, ("127.0.0.1", 5555)
        )
        assert out[0][0] == RESP_MSG_SENT
        # Flooding (path_len=-1 default), routing byte 1
        assert out[0][1] == 1
        # UDP send happened
        assert len(sock.sent) == 1
        assert state.pkt_sent == 1


class TestSendSelfAdvert:
    def test_emits_advert_via_udp(self) -> None:
        state = conftest.make_state()
        sock = conftest.FakeUDPSock()
        out = handle_command(
            bytes([CMD_SEND_SELF_ADVERT]), state, sock, ("127.0.0.1", 5555)
        )
        assert out[0][0] == RESP_OK
        assert len(sock.sent) == 1
        assert state.pkt_sent == 1
        assert state.pkt_flood_tx == 1


class TestSendChanTxtMsg:
    def test_chan_idx_out_of_range_returns_error(self) -> None:
        state = conftest.make_state()
        sock = conftest.FakeUDPSock()
        ts = 1700000000
        # chan_idx=99 — out of range regardless of seed channels.
        data = bytes([0x00, 99]) + struct.pack("<I", ts) + b"hi"
        out = handle_command(
            bytes([CMD_SEND_CHAN_TXT_MSG]) + data,
            state,
            sock,
            ("127.0.0.1", 5555),
        )
        assert out[0][0] == 0x01  # RESP_ERROR

    def test_known_channel_sends_grp_txt(self) -> None:
        state = conftest.make_state()
        # IdentityStore seeds 'public' + 'hansemesh' on first load
        assert len(state.channels) >= 1
        sock = conftest.FakeUDPSock()
        ts = 1700000000
        data = bytes([0x00, 0x00]) + struct.pack("<I", ts) + b"hi"
        out = handle_command(
            bytes([CMD_SEND_CHAN_TXT_MSG]) + data,
            state,
            sock,
            ("127.0.0.1", 5555),
        )
        assert out[0][0] == RESP_OK
        assert len(sock.sent) == 1
        assert state.pkt_flood_tx == 1


class TestLoraFrameToCompanionMsgs:
    def test_invalid_crc_drops(self) -> None:
        state = conftest.make_state()
        msgs, acks = lora_frame_to_companion_msgs(
            {"payload": b"\x00\x00", "crc_valid": False}, state
        )
        assert msgs == [] and acks == []

    def test_wrong_sync_word_drops(self) -> None:
        state = conftest.make_state()
        msgs, acks = lora_frame_to_companion_msgs(
            {
                "payload": b"\x00\x00",
                "crc_valid": True,
                "carrier": {"sync_word": 0x34},
            },
            state,
        )
        assert msgs == [] and acks == []


class TestDefaultFloodScope:
    """Round-trip + persistence + startup-restore for CMD_*_DEFAULT_FLOOD_SCOPE."""

    def _set_payload(self, name: str, key: bytes) -> bytes:
        # Wire format: 31-byte name (utf-8, null-padded) + 16-byte key.
        name_b = name.encode("utf-8")[:31].ljust(31, b"\x00")
        assert len(key) == 16
        return name_b + key

    def test_set_then_get_round_trip(self) -> None:
        state = conftest.make_state()
        key = bytes.fromhex("00112233445566778899aabbccddeeff")
        payload = self._set_payload("#test", key)
        out = handle_command(
            bytes([CMD_SET_DEFAULT_FLOOD_SCOPE]) + payload, state, None, None
        )
        assert out[0][0] == RESP_OK
        assert state.default_scope_name == "#test"
        assert state.default_scope_key == key

        out = handle_command(bytes([CMD_GET_DEFAULT_FLOOD_SCOPE]), state, None, None)
        assert out[0][0] == RESP_DEFAULT_FLOOD_SCOPE
        # 1 byte resp + 31 bytes name + 16 bytes key = 48 bytes
        assert len(out[0]) == 48
        name_b = out[0][1:32]
        key_b = out[0][32:48]
        assert name_b.rstrip(b"\x00").decode("utf-8") == "#test"
        assert key_b == key

    def test_set_default_writes_through_to_send_scope(self) -> None:
        # Firmware semantic (mirrored from MeshCore's set_default_flood_scope):
        # setting the default also updates the active per-session send_scope so
        # subsequent flood TX retunes immediately, not on next APP_START. The
        # earlier dead-code variant in companion.py omitted this; a regression
        # would silently delay the retune until reconnect.
        state = conftest.make_state(send_scope=b"\x11" * 16)
        key = bytes.fromhex("aabbccddeeff00112233445566778899")
        payload = self._set_payload("#live", key)
        out = handle_command(
            bytes([CMD_SET_DEFAULT_FLOOD_SCOPE]) + payload, state, None, None
        )
        assert out[0][0] == RESP_OK
        assert state.default_scope_key == key
        assert state.send_scope == key, (
            "SET_DEFAULT_FLOOD_SCOPE must write through to active send_scope"
        )

    def test_get_default_with_no_scope_returns_zeros(self) -> None:
        state = conftest.make_state()
        out = handle_command(bytes([CMD_GET_DEFAULT_FLOOD_SCOPE]), state, None, None)
        assert out[0][0] == RESP_DEFAULT_FLOOD_SCOPE
        assert out[0][1:32] == b"\x00" * 31
        assert out[0][32:48] == b"\x00" * 16

    def test_set_too_short_returns_error(self) -> None:
        state = conftest.make_state()
        # 46 bytes = 1 short of name(31) + key(16)
        out = handle_command(
            bytes([CMD_SET_DEFAULT_FLOOD_SCOPE]) + b"\x00" * 46, state, None, None
        )
        assert out[0][0] == RESP_ERROR

    def test_persisted_to_eeprom(self) -> None:
        state = conftest.make_state()
        key = b"\x42" * 16
        payload = self._set_payload("#scope1", key)
        handle_command(
            bytes([CMD_SET_DEFAULT_FLOOD_SCOPE]) + payload, state, None, None
        )
        # Persistence path: round-trip through CBOR EEPROM.
        from lora.bridges.meshcore.state import load_eeprom

        cfg = load_eeprom(state.eeprom_path)
        routing = cfg.get("routing", {})
        assert routing.get("default_scope_name") == "#scope1"
        assert bytes(routing.get("default_scope_key")) == key

    def test_apply_startup_config_restores_send_scope_from_default(self) -> None:
        # Simulate a fresh session: bridge constructed with persisted default,
        # send_scope starts empty, apply_startup_config() copies default → send.
        key = b"\x77" * 16
        state = conftest.make_state(
            default_scope_name="#preset",
            default_scope_key=key,
            send_scope=b"\x00" * 16,
        )
        assert state.send_scope == b"\x00" * 16
        state.apply_startup_config()
        assert state.send_scope == key
        assert state.default_scope_name == "#preset"

    def test_apply_startup_config_no_default_keeps_send_scope(self) -> None:
        # When no default is set, apply_startup_config must not stomp the
        # session-supplied send_scope (preserves CMD_SET_FLOOD_SCOPE semantics
        # set before the next APP_START).
        explicit = b"\x33" * 16
        state = conftest.make_state(send_scope=explicit)
        state.apply_startup_config()
        assert state.send_scope == explicit
