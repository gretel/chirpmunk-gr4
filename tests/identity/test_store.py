#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Tests for the unified IdentityStore (Phase 2B)."""

from __future__ import annotations

import struct
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from lora.core.meshcore_crypto import (
    PUB_KEY_SIZE,
    GroupChannel,
    save_pubkey,
)
from lora.decoders.meshcore import MeshCoreKeys
from lora.identity import ContactRecord, IdentityConfig, IdentityStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(tmp_path: Path) -> IdentityConfig:
    return IdentityConfig(
        identity_file=tmp_path / "identity.bin",
        keys_dir=tmp_path / "keys",
        channels_dir=tmp_path / "channels",
        contacts_dir=tmp_path / "contacts",
        reload_interval_s=0.05,
    )


def _fake_contact_bytes(pubkey: bytes, name: str) -> bytes:
    """Build a minimal valid 147-byte MeshCore contact record."""
    assert len(pubkey) == 32
    buf = bytearray()
    buf.extend(pubkey)  # 32
    buf.append(0x01)  # type
    buf.append(0x00)  # flags
    buf.extend(struct.pack("<b", -1))  # path_len: -1 (flood)
    buf.extend(b"\x00" * 64)  # path
    name_bytes = name.encode("utf-8")[:32].ljust(32, b"\x00")
    buf.extend(name_bytes)  # 32
    now = int(time.time())
    buf.extend(struct.pack("<I", now))  # last_advert
    buf.extend(struct.pack("<i", 0))  # lat
    buf.extend(struct.pack("<i", 0))  # lon
    buf.extend(struct.pack("<I", now))  # lastmod
    assert len(buf) == 147, f"contact record is {len(buf)} bytes, want 147"
    return bytes(buf)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_load_or_create_identity_round_trip(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    store = IdentityStore(cfg)
    pub1 = store.get_self_pubkey()
    seed1 = store.get_self_seed()
    prv1 = store.get_self_expanded_prv()
    assert len(pub1) == PUB_KEY_SIZE
    assert len(seed1) == 32
    assert len(prv1) == 64

    # Recreate the store from disk and assert keys round-trip.
    store2 = IdentityStore(cfg)
    assert store2.get_self_pubkey() == pub1
    assert store2.get_self_seed() == seed1
    assert store2.get_self_expanded_prv() == prv1


def test_known_keys_property(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    cfg.keys_dir.mkdir(parents=True, exist_ok=True)
    fake_pub = b"\xaa" * PUB_KEY_SIZE
    (cfg.keys_dir / f"{fake_pub.hex()}.key").write_bytes(fake_pub)

    store = IdentityStore(cfg)
    assert fake_pub.hex() in store.known_keys
    assert store.known_keys[fake_pub.hex()] == fake_pub


def test_lookup_name_finds_pubkey(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    store = IdentityStore(cfg)
    fake_pub = b"\xbb" * PUB_KEY_SIZE
    rec = ContactRecord(
        name="Alice",
        pub_key=fake_pub,
        raw=_fake_contact_bytes(fake_pub, "Alice"),
    )
    assert store.add_contact(rec) is True
    assert store.lookup_name(fake_pub) == "Alice"
    # Unknown pubkey returns None.
    assert store.lookup_name(b"\xcc" * PUB_KEY_SIZE) is None


def test_atomic_write_no_partial_file(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    store = IdentityStore(cfg)
    fake_pub = b"\xdd" * PUB_KEY_SIZE
    target = cfg.keys_dir / f"{fake_pub.hex()}.key"

    with patch("os.replace", side_effect=OSError("simulated rename failure")):
        with pytest.raises(OSError):
            store.add_learned_pubkey(fake_pub, name="Bob")

    # Target must NOT exist (we rename only after a complete temp-file write).
    assert not target.exists(), (
        "atomic write left a partial / orphan file at the destination"
    )
    # No stray temp files in the keys dir either.
    leftover = [p for p in cfg.keys_dir.glob("*") if p.is_file()]
    assert leftover == [], f"atomic write leaked tempfiles: {leftover}"


def test_meshcore_keys_protocol_compliance(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    store = IdentityStore(cfg)
    # MeshCoreKeys is @runtime_checkable in Phase 2A.
    assert isinstance(store, MeshCoreKeys), (
        "IdentityStore must satisfy the MeshCoreKeys Protocol"
    )
    # Spot-check the four attributes the meshcore decoder reads.
    assert isinstance(store.our_prv, (bytes, bytearray))
    assert isinstance(store.our_pub, (bytes, bytearray))
    assert isinstance(cast(Mapping[str, bytes], store.known_keys), Mapping)
    assert isinstance(cast(Sequence[GroupChannel], store.channels), Sequence)


def test_reload_picks_up_new_pubkey(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    store = IdentityStore(cfg)
    # Force initial load.
    initial = dict(store.known_keys)

    # Write a new key directly via the underlying helper.
    fake_pub = b"\xee" * PUB_KEY_SIZE
    cfg.keys_dir.mkdir(parents=True, exist_ok=True)
    assert save_pubkey(cfg.keys_dir, fake_pub, name="Eve") is True

    # Without reload(), the cached known_keys does not see it yet.
    assert fake_pub.hex() not in initial
    store.reload()
    assert fake_pub.hex() in store.known_keys


def test_remove_contact_round_trip(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    store = IdentityStore(cfg)
    fake_pub = b"\xff" * PUB_KEY_SIZE
    rec = ContactRecord(
        name="Carol",
        pub_key=fake_pub,
        raw=_fake_contact_bytes(fake_pub, "Carol"),
    )
    store.add_contact(rec)
    assert any(c.pub_key == fake_pub for c in store.contacts())

    assert store.remove_contact(fake_pub[:6]) is True
    assert all(c.pub_key != fake_pub for c in store.contacts())
    # Deleting again returns False.
    assert store.remove_contact(fake_pub[:6]) is False


def test_lookup_pubkey_by_hex_prefix_and_name(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    store = IdentityStore(cfg)
    fake_pub = bytes.fromhex("ab" * 32)
    store.add_contact(
        ContactRecord(
            name="Dave",
            pub_key=fake_pub,
            raw=_fake_contact_bytes(fake_pub, "Dave"),
        )
    )
    assert store.lookup_pubkey("abab") == fake_pub
    assert store.lookup_pubkey("Dave") == fake_pub
    assert store.lookup_pubkey("nope") is None


def test_start_stop_watch_clean(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    store = IdentityStore(cfg)
    store.start_watch()
    # Idempotent re-start is a no-op, not a crash.
    store.start_watch()
    store.stop_watch()
    # Re-stop is also harmless.
    store.stop_watch()
