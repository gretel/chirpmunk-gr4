# SPDX-License-Identifier: ISC
"""Tests for ``lora.tools.meshcore_tx`` — TX builder library + CLI."""

from __future__ import annotations

import struct
import subprocess
import sys
from pathlib import Path

import pytest

from lora.core.meshcore_crypto import (
    PAYLOAD_ADVERT,
    PAYLOAD_TXT,
    PUB_KEY_SIZE,
    ROUTE_FLOOD,
    load_or_create_identity,
    try_decrypt_txt_msg,
)
from lora.tools import meshcore_tx as tx


def _temp_identity(tmp_path: Path) -> Path:
    """Create a fresh on-disk identity inside *tmp_path* and return its file."""
    ident_file = tmp_path / "identity.bin"
    load_or_create_identity(ident_file)
    return ident_file


def test_make_header_packs_fields() -> None:
    """Header byte layout: ``[version(2)|payload_type(4)|route_type(2)]``."""
    h = tx.make_header(route_type=ROUTE_FLOOD, payload_type=PAYLOAD_ADVERT, version=0)
    assert h == ((0 << 6) | (PAYLOAD_ADVERT << 2) | ROUTE_FLOOD)
    # All zeros possible
    assert tx.make_header(0, 0, 0) == 0
    # Top of the range
    assert tx.make_header(3, 0x0F, 3) == 0xFF


def test_build_wire_packet_layout() -> None:
    """Wire format: ``[header][opt transport(4)][path_len(1)][path][payload]``."""
    pkt = tx.build_wire_packet(0xAB, b"\x01\x02\x03")
    assert pkt[0] == 0xAB
    assert pkt[1] == 0  # path_len=0
    assert pkt[2:] == b"\x01\x02\x03"

    pkt2 = tx.build_wire_packet(0xCD, b"\xff", path=b"\xaa\xbb")
    assert pkt2[0] == 0xCD
    assert pkt2[1] == 2  # path_len=2
    assert pkt2[2:4] == b"\xaa\xbb"
    assert pkt2[4:] == b"\xff"


def test_build_advert_wire_shape(tmp_path: Path) -> None:
    """ADVERT must start with the FLOOD/ADVERT header and embed pubkey + name."""
    ident_file = _temp_identity(tmp_path)
    packet = tx.build_advert(name="testnode", identity_path=ident_file)

    # Header byte
    expected_header = (0 << 6) | (PAYLOAD_ADVERT << 2) | ROUTE_FLOOD
    assert packet[0] == expected_header

    # path_len byte (FLOOD has no transport codes)
    assert packet[1] == 0

    # body = pubkey(32) + ts(4) + sig(64) + flags(1) + name
    body = packet[2:]
    assert len(body) >= 32 + 4 + 64 + 1
    flags = body[32 + 4 + 64]
    assert flags & 0x80  # has_name
    name_bytes = body[32 + 4 + 64 + 1 :]
    assert name_bytes == b"testnode"


def test_build_advert_with_location(tmp_path: Path) -> None:
    ident_file = _temp_identity(tmp_path)
    packet = tx.build_advert(
        name="here",
        lat=53.55,
        lon=9.99,
        identity_path=ident_file,
    )
    body = packet[2:]
    flags = body[32 + 4 + 64]
    assert flags & 0x10  # has_location
    lat_e6, lon_e6 = struct.unpack_from("<ii", body, 32 + 4 + 64 + 1)
    assert lat_e6 == int(53.55 * 1e6)
    assert lon_e6 == int(9.99 * 1e6)


def test_build_txt_msg_roundtrip(tmp_path: Path) -> None:
    """Encrypted TXT_MSG must decrypt back to the original plaintext."""
    sender_id = tmp_path / "sender.bin"
    recipient_id = tmp_path / "recipient.bin"
    sender_expanded, sender_pub, _ = load_or_create_identity(sender_id)
    recipient_expanded, recipient_pub, _ = load_or_create_identity(recipient_id)

    text = "Hello from gr4-lora"
    packet = tx.build_txt_msg(
        text,
        recipient_pub,
        identity_path=sender_id,
    )
    # Header byte: DIRECT + TXT
    assert packet[0] == ((PAYLOAD_TXT << 2) | 2)
    # Recipient decrypts using their own keys + a known_keys map mapping
    # the sender's pubkey hash.
    known = {sender_pub.hex(): sender_pub}
    res = try_decrypt_txt_msg(
        packet,
        recipient_expanded,
        recipient_pub,
        known,
    )
    assert res is not None
    decrypted, src_pub = res
    assert decrypted == text
    assert src_pub == sender_pub


def test_build_txt_msg_validates_pubkey_length(tmp_path: Path) -> None:
    ident = _temp_identity(tmp_path)
    with pytest.raises(ValueError, match=str(PUB_KEY_SIZE)):
        tx.build_txt_msg("hi", b"\x00" * 8, identity_path=ident)


def test_build_anon_req_validates_pubkey_length(tmp_path: Path) -> None:
    ident = _temp_identity(tmp_path)
    with pytest.raises(ValueError, match=str(PUB_KEY_SIZE)):
        tx.build_anon_req("hi", b"\x00" * 16, identity_path=ident)


def test_send_via_udp_emits_lora_tx_cbor(tmp_path: Path) -> None:
    """``send_via_udp`` must drop a single CBOR datagram on the target port."""
    import socket

    import cbor2

    ident_file = _temp_identity(tmp_path)
    packet = tx.build_advert(name="udptest", identity_path=ident_file)

    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    server.settimeout(2.0)
    host, port = server.getsockname()
    try:
        tx.send_via_udp(packet, host=host, port=port, sync_word=0x12)
        data, _addr = server.recvfrom(65536)
    finally:
        server.close()

    msg = cbor2.loads(data)
    assert msg["type"] == "lora_tx"
    assert msg["payload"] == packet
    assert msg["sync_word"] == 0x12


def test_make_cbor_tx_request_includes_overrides() -> None:
    import cbor2

    raw = tx.make_cbor_tx_request(b"\xde\xad", sf=8, freq=869618000, dry_run=True)
    msg = cbor2.loads(raw)
    assert msg["type"] == "lora_tx"
    assert msg["payload"] == b"\xde\xad"
    assert msg["sf"] == 8
    assert msg["freq"] == 869618000
    assert msg["dry_run"] is True


def test_help_exits_zero() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "lora.tools.meshcore_tx", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout + result.stderr
    assert "advert" in out
    assert "send" in out
    assert "anon-req" in out


def test_advert_subcommand_help_exits_zero() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "lora.tools.meshcore_tx", "advert", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "--name" in result.stdout
    assert "--lat" in result.stdout
