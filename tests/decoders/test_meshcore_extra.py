# SPDX-License-Identifier: ISC
"""Additional meshcore decoder tests: _coerce_keys, _path_bytes, _extract_advert_location, edge cases."""

from __future__ import annotations

from pathlib import Path

from lora.core.meshcore_crypto import (
    GroupChannel,
    parse_meshcore_header,
)
from lora.core.types import LoraFrame
from lora.decoders.meshcore import (
    MESHCORE_SYNC_WORD,
    MeshCoreDecoder,
    _coerce_keys,
    _extract_advert_location,
    _path_bytes,
)
from lora.tools.meshcore_tx import build_advert_raw
from tests.conftest import (
    build_test_txt_msg_packet,
    make_two_identities,
)

from .conftest import make_ctx, make_frame

_PUBLIC_CHANNEL_FILE = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "data"
    / "meshcore"
    / "channels"
    / "public.channel"
)


def _frame(payload: bytes) -> LoraFrame:
    return make_frame(payload, sync_word=MESHCORE_SYNC_WORD)


# ---- _coerce_keys ---------------------------------------------------------


def test_coerce_keys_none_returns_none() -> None:
    assert _coerce_keys(None) is None


def test_coerce_keys_missing_attrs_returns_none() -> None:
    obj = object()
    assert _coerce_keys(obj) is None


def test_coerce_keys_partial_attrs_returns_none() -> None:
    class Partial:
        our_prv: bytes | None = None
        our_pub: bytes | None = None

    assert _coerce_keys(Partial()) is None


def test_coerce_keys_full_protocol_satisfied() -> None:
    class FullKeys:
        our_prv: bytes | None = b"\x01" * 64
        our_pub: bytes | None = b"\x02" * 32
        known_keys: dict[str, bytes] = {}
        channels: list[GroupChannel] = []

    result = _coerce_keys(FullKeys())
    assert result is not None


# ---- _path_bytes ----------------------------------------------------------


def test_path_bytes_empty_for_none_header() -> None:
    result = _path_bytes(b"\x01")
    assert result == b""


def test_path_bytes_empty_when_no_path_section() -> None:
    # Minimal frame: version=0, ptype=ADVERT(unker), route=FLOOR
    # Just 2 bytes: header(0) + path_len(0)
    data = bytes([0, 0])
    result = _path_bytes(data)
    assert result == b""


def test_path_bytes_extracts_path() -> None:
    (_, pub_a, seed_a), _ = make_two_identities()
    pkt = build_advert_raw(seed_a, pub_a, name="Test")
    result = _path_bytes(pkt)
    assert isinstance(result, bytes)


# ---- _extract_advert_location ---------------------------------------------


def test_extract_advert_location_none_for_non_advert() -> None:
    # Build a TXT packet (not ADVERT)
    (prv_a, pub_a, _seed_a), (prv_b, pub_b, _seed_b) = make_two_identities()
    pkt = build_test_txt_msg_packet(prv_a, pub_a, pub_b, "hello", timestamp=1700000000)
    result = _extract_advert_location(pkt)
    assert result is None


def test_extract_advert_location_none_when_too_short() -> None:
    result = _extract_advert_location(b"\x00" * 10)
    assert result is None


def test_extract_advert_location_none_when_location_flag_not_set() -> None:
    (_, pub_a, seed_a), _ = make_two_identities()
    pkt = build_advert_raw(seed_a, pub_a, name="NoLoc")
    result = _extract_advert_location(pkt)
    assert result is None


# ---- decrypt=False mode ---------------------------------------------------


def test_decoder_decrypt_false_skips_decryption() -> None:
    (prv_a, pub_a, _seed_a), (prv_b, pub_b, _seed_b) = make_two_identities()
    pkt = build_test_txt_msg_packet(prv_a, pub_a, pub_b, "secret", timestamp=1700000000)
    ann = MeshCoreDecoder(decrypt=False).process(_frame(pkt), make_ctx(None))
    assert ann is not None
    assert ann.ok is True
    assert ann.fields["payload_type"] == "TXT"
    assert "decrypted_text" not in ann.fields


# ---- transport codes and ACK ----------------------------------------------


def test_frame_with_transport_codes() -> None:
    # Build a minimal frame with transport codes set.
    # route=FLOOD(0) always has transport. ptype=ADVERT(6), version=0
    # Header byte: (0 << 6) | (6 << 2) | 0 = 0x18
    # After header: 4 bytes transport (tc1, tc2 as uint16 LE)
    # After transport: path_len=0, then ADVERT payload
    (_, pub_a, seed_a), _ = make_two_identities()
    pkt = build_advert_raw(seed_a, pub_a, name="T")
    header_byte = 0x18  # version=0, ptype=ADVERT, route=FLOOD
    transport = b"\x01\x00\x02\x00"  # tc1=1, tc2=2 (uint16 LE)
    # Parse original header to find app payload offset
    hdr = parse_meshcore_header(pkt)
    assert hdr is not None
    app_data = pkt[hdr["off"] :]
    # Rebuild: header + transport + path_len(0) + app_data
    wire = bytes([header_byte]) + transport + bytes([0]) + app_data
    ann = MeshCoreDecoder().process(_frame(wire), make_ctx(None))
    assert ann is not None
    assert ann.ok is True
    assert ann.fields.get("transport_codes") == [1, 2]


def test_ack_parses_hash() -> None:
    # Build minimal ACK frame: version=0, ptype=ACK(0x03), route=FLOOD(0)
    # Header byte = (0 << 6) | (0x03 << 2) | 0 = 0x0C
    # route=FLOOD means has_transport=True, so 4 bytes transport follow header
    # But for minimal test frame, just use route=DIRECT(3) which also has transport
    # Actually let's use route=DIRECT=3 which means has_transport=False
    # Wait: route in (0,3) means has_transport=True. route=1 or 2 means no transport.
    # Let's use route=FLOOD_ACK=2 (no transport): header = (0 << 6) | (0x03 << 2) | 2 = 0x0E
    # path_len=0, then ack payload: dest_hash(1) + msg_hash(4)
    ack_payload = b"\x00\xde\xad\xbe\xef"
    wire = bytes([0x0E, 0x00]) + ack_payload
    ann = MeshCoreDecoder().process(_frame(wire), make_ctx(None))
    assert ann is not None
    assert ann.ok is True
    assert ann.fields["payload_type"] == "ACK"
    assert ann.fields.get("ack_hash") == b"\xde\xad\xbe\xef"


def test_ack_too_short_no_hash() -> None:
    wire = bytes([0x0E, 0x00, 0x00])  # only 1 byte of ack data
    ann = MeshCoreDecoder().process(_frame(wire), make_ctx(None))
    assert ann is not None
    assert "ack_hash" not in ann.fields


# ---- GRP_TXT with no channels does not decrypt -----------------------------


def test_grp_txt_without_channels_decrypt_skipped() -> None:
    ch_psk = _PUBLIC_CHANNEL_FILE.read_bytes()
    ch = GroupChannel("public", ch_psk)
    from lora.core.meshcore_crypto import build_grp_txt

    pkt_payload = build_grp_txt(ch, "Alice", "hi", timestamp=1700000000)
    header = (0 << 6) | (5 << 2) | 0x01
    wire = bytes([header, 0x00]) + pkt_payload

    ann = MeshCoreDecoder().process(_frame(wire), make_ctx(None))
    assert ann is not None
    assert ann.ok is True
    assert ann.fields["payload_type"] == "GRP_TXT"
    assert ann.fields.get("decrypted") is False
    assert "decrypted_text" not in ann.fields
