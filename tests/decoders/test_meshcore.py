# SPDX-License-Identifier: ISC
"""Tests for :class:`lora.decoders.meshcore.MeshCoreDecoder`.

Uses the conftest helpers (`make_two_identities`, `build_advert`,
`build_txt_msg`) and the bundled `public.channel` PSK to exercise
ADVERT extraction, TXT_MSG decryption, GRP_TXT decryption against the
public channel, and the malformed-header failure mode.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from lora.core.meshcore_crypto import (
    GroupChannel,
    build_grp_txt,
)
from lora.core.types import LoraFrame
from lora.decoders.meshcore import MESHCORE_SYNC_WORD, MeshCoreDecoder
from tests.conftest import (
    build_test_advert_packet,
    build_test_txt_msg_packet,
    make_two_identities,
)

from .conftest import make_ctx, make_frame

# Bundled public-channel PSK shipped with the repo.
_PUBLIC_CHANNEL_FILE = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "data"
    / "meshcore"
    / "channels"
    / "public.channel"
)


@dataclass
class _Keys:
    """Minimal MeshCoreKeys-shaped object for tests."""

    our_prv: bytes | None = None
    our_pub: bytes | None = None
    known_keys: Mapping[str, bytes] = field(default_factory=dict)
    channels: Sequence[GroupChannel] = field(default_factory=tuple)


def _frame(payload: bytes) -> LoraFrame:
    return make_frame(payload, sync_word=MESHCORE_SYNC_WORD)


# ---- ADVERT --------------------------------------------------------------


def test_advert_extracts_pubkey_and_name() -> None:
    (prv_a, pub_a, seed_a), _ = make_two_identities()
    pkt = build_test_advert_packet(seed_a, pub_a, name="Alice")

    ann = MeshCoreDecoder().process(_frame(pkt), make_ctx(_Keys()))
    assert ann is not None
    assert ann.ok is True
    assert ann.fields["payload_type"] == "ADVERT"
    assert ann.fields["sender_pubkey"] == pub_a
    assert ann.fields["sender_name"] == "Alice"
    assert ann.fields["decrypted"] is False  # ADVERTs are signed, not encrypted
    assert ann.fields["version"] == 0


def test_advert_with_location_extracts_lat_lon_e6() -> None:
    from lora.tools.meshcore_tx import build_advert_raw as build_advert

    (_prv, pub_a, seed_a), _ = make_two_identities()
    pkt = build_advert(seed_a, pub_a, name="HQ", lat=53.5511, lon=9.9937)
    ann = MeshCoreDecoder().process(_frame(pkt), make_ctx(_Keys()))
    assert ann is not None
    assert ann.ok is True
    assert ann.fields["payload_type"] == "ADVERT"
    assert ann.fields["sender_name"] == "HQ"
    # int(53.5511 * 1e6) == 53551099 (binary float rounding); accept both.
    assert ann.fields["lat_e6"] in (53551099, 53551100)
    assert ann.fields["lon_e6"] in (9993699, 9993700)


# ---- TXT_MSG -------------------------------------------------------------


def test_txt_msg_decrypts_with_known_recipient() -> None:
    (prv_a, pub_a, _seed_a), (prv_b, pub_b, _seed_b) = make_two_identities()

    # Alice sends to Bob. Bob's identity store knows Alice's pubkey.
    pkt = build_test_txt_msg_packet(
        prv_a, pub_a, pub_b, "hello bob", timestamp=1700000000
    )

    keys = _Keys(
        our_prv=prv_b,
        our_pub=pub_b,
        known_keys={pub_a.hex(): pub_a},
    )
    ann = MeshCoreDecoder().process(_frame(pkt), make_ctx(keys))
    assert ann is not None
    assert ann.ok is True
    assert ann.fields["payload_type"] == "TXT"
    assert ann.fields["decrypted"] is True
    assert ann.fields["decrypted_text"] == "hello bob"
    assert ann.fields["sender_pubkey"] == pub_a


def test_txt_msg_unknown_sender_stays_undecrypted_but_ok() -> None:
    (prv_a, pub_a, _seed_a), (prv_b, pub_b, _seed_b) = make_two_identities()
    pkt = build_test_txt_msg_packet(prv_a, pub_a, pub_b, "hello", timestamp=1700000000)
    # Bob doesn't know Alice's pubkey -> can't derive shared secret.
    keys = _Keys(our_prv=prv_b, our_pub=pub_b, known_keys={})
    ann = MeshCoreDecoder().process(_frame(pkt), make_ctx(keys))
    assert ann is not None
    assert ann.ok is True
    assert ann.fields["decrypted"] is False
    assert "decrypted_text" not in ann.fields


# ---- GRP_TXT -------------------------------------------------------------


def _public_channel() -> GroupChannel:
    psk = _PUBLIC_CHANNEL_FILE.read_bytes()
    return GroupChannel("public", psk)


def test_grp_txt_decrypts_via_public_channel() -> None:
    ch = _public_channel()
    pkt_payload = build_grp_txt(ch, "Alice", "hi everyone", timestamp=1700000000)
    # Wrap the GRP_TXT app payload in a MeshCore wire header (FLOOD route, no
    # transport, empty path).
    header = (0 << 6) | (5 << 2) | 0x01  # version=0, ptype=GRP_TXT, route=FLOOD
    wire = bytes([header, 0x00]) + pkt_payload  # path_len=0, then payload

    keys = _Keys(channels=(ch,))
    ann = MeshCoreDecoder().process(_frame(wire), make_ctx(keys))
    assert ann is not None
    assert ann.ok is True
    assert ann.fields["payload_type"] == "GRP_TXT"
    assert ann.fields["decrypted"] is True
    assert ann.fields["channel"] == "public"
    assert ann.fields["decrypted_text"] == "Alice: hi everyone"


# ---- header parsing failure ---------------------------------------------


def test_bad_header_returns_failure_annotation() -> None:
    # 1-byte payload: parse_meshcore_header needs at least 2 bytes.
    ann = MeshCoreDecoder().process(_frame(b"\x40"), make_ctx(_Keys()))
    assert ann is not None
    assert ann.ok is False
    assert ann.error == "header_parse"


# ---- header-only fields when no keys present ---------------------------


def test_runs_without_identity_for_header_only_fields() -> None:
    (_, pub_a, seed_a), _ = make_two_identities()
    pkt = build_test_advert_packet(seed_a, pub_a, name="N")
    # ctx.identity = None means no keys; ADVERT should still parse.
    ann = MeshCoreDecoder().process(_frame(pkt), make_ctx(None))
    assert ann is not None
    assert ann.ok is True
    assert ann.fields["payload_type"] == "ADVERT"
    assert ann.fields["sender_pubkey"] == pub_a
