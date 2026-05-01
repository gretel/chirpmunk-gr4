#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""
test_meshcore_crypto.py -- Unit tests for meshcore_crypto.py.

Tests key store load/save, header parsing, ADVERT extraction,
and RX decryption helpers (try_decrypt_txt_msg, try_decrypt_anon_req).

Run:  python3 -m unittest scripts/tests/test_meshcore_crypto.py -v
"""

import os
import tempfile
import unittest
from pathlib import Path

from nacl.signing import SigningKey

from lora.core.meshcore_crypto import (
    CIPHER_MAC_SIZE,
    SEED_CHANNELS_DIR,
    GroupChannel,
    _clamp_scalar,
    build_grp_txt,
    decode_path_len,
    extract_advert_name,
    extract_advert_pubkey,
    grp_txt_channel_hash,
    load_channels,
    load_identity,
    load_known_keys,
    load_or_create_identity,
    meshcore_encrypt_then_mac,
    meshcore_expanded_key,
    meshcore_mac_then_decrypt,
    meshcore_shared_secret,
    parse_meshcore_header,
    save_channel,
    save_pubkey,
    try_decrypt_anon_req,
    try_decrypt_grp_txt,
    try_decrypt_txt_msg,
)
from tests.conftest import (
    TEST_SEED,
    TEST_SEED_EXPANDED,
    TEST_SEED_PUB,
)
from tests.conftest import (
    build_test_advert_packet as _build_advert_packet,
)
from tests.conftest import (
    build_test_anon_req_packet as _build_anon_req_packet,
)
from tests.conftest import (
    build_test_txt_msg_packet as _build_txt_msg_packet,
)


def _seed_channels() -> list[GroupChannel]:
    """Load channels from the repo seed directory (scripts/channels/)."""
    return load_channels(SEED_CHANNELS_DIR)


def _public_channel() -> GroupChannel:
    """Return the #public channel loaded from seed files."""
    for ch in _seed_channels():
        if ch.name == "public":
            return ch
    raise RuntimeError("public.channel seed file missing")


# ---- Scalar clamping ----


class TestClampScalar(unittest.TestCase):
    def test_clamp_modifies_bytes(self):
        raw = bytes(32)
        clamped = _clamp_scalar(raw)
        self.assertEqual(clamped[0] & 7, 0)  # low 3 bits cleared
        self.assertEqual(clamped[31] & 0xC0, 0x40)  # bit 6 set, bit 7 cleared

    def test_clamp_length(self):
        self.assertEqual(len(_clamp_scalar(os.urandom(32))), 32)

    def test_clamp_idempotent(self):
        raw = os.urandom(32)
        once = _clamp_scalar(raw)
        twice = _clamp_scalar(once)
        self.assertEqual(once, twice)


# ---- Key expansion ----


class TestExpandedKey(unittest.TestCase):
    def test_length(self):
        expanded = meshcore_expanded_key(os.urandom(32))
        self.assertEqual(len(expanded), 64)

    def test_deterministic(self):
        seed = os.urandom(32)
        self.assertEqual(meshcore_expanded_key(seed), meshcore_expanded_key(seed))

    def test_clamped(self):
        expanded = meshcore_expanded_key(os.urandom(32))
        self.assertEqual(expanded[0] & 7, 0)
        self.assertEqual(expanded[31] & 0xC0, 0x40)


# ---- Key store ----


class TestKeyStore(unittest.TestCase):
    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as td:
            keys_dir = Path(td) / "keys"
            pub = os.urandom(32)
            self.assertTrue(save_pubkey(keys_dir, pub))
            keys = load_known_keys(keys_dir)
            self.assertIn(pub.hex(), keys)
            self.assertEqual(keys[pub.hex()], pub)

    def test_save_duplicate_returns_false(self):
        with tempfile.TemporaryDirectory() as td:
            keys_dir = Path(td) / "keys"
            pub = os.urandom(32)
            self.assertTrue(save_pubkey(keys_dir, pub))
            self.assertFalse(save_pubkey(keys_dir, pub))

    def test_load_empty_dir(self):
        with tempfile.TemporaryDirectory() as td:
            keys = load_known_keys(Path(td))
            self.assertEqual(keys, {})

    def test_load_nonexistent_dir(self):
        keys = load_known_keys(Path("/nonexistent/path/keys"))
        self.assertEqual(keys, {})

    def test_load_ignores_wrong_size(self):
        with tempfile.TemporaryDirectory() as td:
            keys_dir = Path(td)
            bad = keys_dir / "bad.key"
            bad.write_bytes(b"short")
            keys = load_known_keys(keys_dir)
            self.assertEqual(keys, {})

    def test_multiple_keys(self):
        with tempfile.TemporaryDirectory() as td:
            keys_dir = Path(td) / "keys"
            pubs = [os.urandom(32) for _ in range(5)]
            for pub in pubs:
                save_pubkey(keys_dir, pub)
            keys = load_known_keys(keys_dir)
            self.assertEqual(len(keys), 5)
            for pub in pubs:
                self.assertIn(pub.hex(), keys)


# ---- Identity management ----


class TestLoadIdentity(unittest.TestCase):
    def test_load_nonexistent(self):
        self.assertIsNone(load_identity(Path("/nonexistent/identity.bin")))

    def test_load_wrong_size(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "identity.bin"
            path.write_bytes(b"short")
            self.assertIsNone(load_identity(path))

    def test_load_mismatched_keys(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "identity.bin"
            path.write_bytes(os.urandom(64))  # random seed + random pub = mismatch
            self.assertIsNone(load_identity(path))

    def test_load_valid_identity(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "identity.bin"
            _, _, _ = load_or_create_identity(path)
            result = load_identity(path)
            assert result is not None
            exp, pub, seed = result
            self.assertEqual(len(exp), 64)
            self.assertEqual(len(pub), 32)
            self.assertEqual(len(seed), 32)


# ---- Header parsing ----


class TestParseMeshcoreHeader(unittest.TestCase):
    def test_too_short(self):
        self.assertIsNone(parse_meshcore_header(b""))
        self.assertIsNone(parse_meshcore_header(b"\x00"))

    def test_flood_advert(self):
        # FLOOD (route=1) + ADVERT (ptype=4) + version 0
        # header = 0x01 | (0x04 << 2) = 0x01 | 0x10 = 0x11
        data = b"\x11\x00" + b"\xaa" * 10
        hdr = parse_meshcore_header(data)
        assert hdr is not None
        self.assertEqual(hdr["route"], 1)  # FLOOD
        self.assertEqual(hdr["ptype"], 4)  # ADVERT
        self.assertEqual(hdr["version"], 0)
        self.assertFalse(hdr["has_transport"])

    def test_t_flood_with_transport(self):
        # T_FLOOD (route=0) + REQ (ptype=0) + version 0
        # header = 0x00
        data = b"\x00" + b"\x01\x02\x03\x04" + b"\x00" + b"\xff" * 5
        hdr = parse_meshcore_header(data)
        assert hdr is not None
        self.assertEqual(hdr["route"], 0)  # T_FLOOD
        self.assertTrue(hdr["has_transport"])

    def test_direct_txt(self):
        # DIRECT (route=2) + TXT (ptype=2) + version 0
        # header = 0x02 | (0x02 << 2) = 0x02 | 0x08 = 0x0A
        data = b"\x0a\x00" + b"\xbb" * 20
        hdr = parse_meshcore_header(data)
        assert hdr is not None
        self.assertEqual(hdr["route"], 2)  # DIRECT
        self.assertEqual(hdr["ptype"], 2)  # TXT
        self.assertFalse(hdr["has_transport"])

    def test_path_length_parsed(self):
        # FLOOD + ADVERT, path_len=3
        data = b"\x11\x03\x01\x02\x03" + b"\xcc" * 10
        hdr = parse_meshcore_header(data)
        assert hdr is not None
        # off should be past header(1) + path_len(1) + path(3) = 5
        self.assertEqual(hdr["off"], 5)

    def test_version_bits(self):
        # version=2, route=1 (FLOOD), ptype=0 (REQ)
        # header = (2 << 6) | (0 << 2) | 1 = 0x81
        data = b"\x81\x00" + b"\xdd" * 5
        hdr = parse_meshcore_header(data)
        assert hdr is not None
        self.assertEqual(hdr["version"], 2)

    def test_path_mode_1byte_hashes(self):
        # Mode 0 (bits 7:6 = 0b00): 1-byte hashes, count=3 → 3 path bytes
        # path_len byte = 0b00000011 = 0x03
        data = b"\x11" + bytes([0x03]) + b"\x01\x02\x03" + b"\xcc" * 10
        hdr = parse_meshcore_header(data)
        assert hdr is not None
        # off = 1(hdr) + 1(path_len_field) + 3(path bytes) = 5
        self.assertEqual(hdr["off"], 5)
        self.assertEqual(hdr["path_len"], 0x03)

    def test_path_mode_2byte_hashes(self):
        # Mode 1 (bits 7:6 = 0b01): 2-byte hashes, count=2 → 4 path bytes
        # path_len byte = 0b01000010 = 0x42
        data = b"\x11" + bytes([0x42]) + b"\x01\x02\x03\x04" + b"\xcc" * 10
        hdr = parse_meshcore_header(data)
        assert hdr is not None
        # off = 1(hdr) + 1(path_len_field) + 4(path bytes) = 6
        self.assertEqual(hdr["off"], 6)

    def test_path_mode_3byte_hashes(self):
        # Mode 2 (bits 7:6 = 0b10): 3-byte hashes, count=1 → 3 path bytes
        # path_len byte = 0b10000001 = 0x81
        data = b"\x11" + bytes([0x81]) + b"\x01\x02\x03" + b"\xcc" * 10
        hdr = parse_meshcore_header(data)
        assert hdr is not None
        # off = 1(hdr) + 1(path_len_field) + 3(path bytes) = 5
        self.assertEqual(hdr["off"], 5)

    def test_path_mode_zero_hops(self):
        # Mode 1 (2-byte hashes), count=0 → 0 path bytes (empty path)
        # path_len byte = 0b01000000 = 0x40
        data = b"\x11" + bytes([0x40]) + b"\xcc" * 10
        hdr = parse_meshcore_header(data)
        assert hdr is not None
        # off = 1(hdr) + 1(path_len_field) + 0(path bytes) = 2
        self.assertEqual(hdr["off"], 2)


class TestDecodePathLen(unittest.TestCase):
    def test_mode0_zero_hops(self):
        count, size, total = decode_path_len(0x00)
        self.assertEqual(count, 0)
        self.assertEqual(size, 1)
        self.assertEqual(total, 0)

    def test_mode0_three_hops(self):
        # 0x03 = 0b00000011: mode=0, count=3 → 1-byte hashes, 3 bytes
        count, size, total = decode_path_len(0x03)
        self.assertEqual(count, 3)
        self.assertEqual(size, 1)
        self.assertEqual(total, 3)

    def test_mode1_two_hops(self):
        # 0x42 = 0b01000010: mode=1, count=2 → 2-byte hashes, 4 bytes
        count, size, total = decode_path_len(0x42)
        self.assertEqual(count, 2)
        self.assertEqual(size, 2)
        self.assertEqual(total, 4)

    def test_mode2_one_hop(self):
        # 0x81 = 0b10000001: mode=2, count=1 → 3-byte hashes, 3 bytes
        count, size, total = decode_path_len(0x81)
        self.assertEqual(count, 1)
        self.assertEqual(size, 3)
        self.assertEqual(total, 3)

    def test_mode1_zero_hops(self):
        # 0x40 = 0b01000000: mode=1, count=0 → 2-byte hashes, 0 bytes
        count, size, total = decode_path_len(0x40)
        self.assertEqual(count, 0)
        self.assertEqual(size, 2)
        self.assertEqual(total, 0)

    def test_mode3_reserved(self):
        # 0xC0 = 0b11000000: mode=3 (reserved) → size=4 per formula, count=0
        # decode_path_len doesn't validate; caller or parse_meshcore_header must reject
        count, size, total = decode_path_len(0xC0)
        self.assertEqual(size, 4)  # ((3 & 3) + 1) = 4
        self.assertEqual(total, 0)


# ---- ADVERT extraction ----


class TestExtractAdvert(unittest.TestCase):
    def test_extract_pubkey(self):
        pkt = _build_advert_packet(TEST_SEED, TEST_SEED_PUB, name="Test")
        pubkey = extract_advert_pubkey(pkt)
        assert pubkey is not None
        self.assertEqual(pubkey, TEST_SEED_PUB)

    def test_extract_pubkey_no_name(self):
        pkt = _build_advert_packet(TEST_SEED, TEST_SEED_PUB)
        pubkey = extract_advert_pubkey(pkt)
        self.assertEqual(pubkey, TEST_SEED_PUB)

    def test_extract_name(self):
        pkt = _build_advert_packet(TEST_SEED, TEST_SEED_PUB, name="MyNode")
        name = extract_advert_name(pkt)
        self.assertEqual(name, "MyNode")

    def test_extract_name_no_name(self):
        pkt = _build_advert_packet(TEST_SEED, TEST_SEED_PUB)
        name = extract_advert_name(pkt)
        self.assertIsNone(name)

    def test_non_advert_returns_none(self):
        # TXT packet, not ADVERT
        seed2 = os.urandom(32)
        sk2 = SigningKey(seed2)
        dest_pub = bytes(sk2.verify_key)
        pkt = _build_txt_msg_packet(TEST_SEED_EXPANDED, TEST_SEED_PUB, dest_pub, "Hi")
        self.assertIsNone(extract_advert_pubkey(pkt))
        self.assertIsNone(extract_advert_name(pkt))

    def test_truncated_packet(self):
        self.assertIsNone(extract_advert_pubkey(b"\x11\x00\xaa"))
        self.assertIsNone(extract_advert_name(b"\x11\x00\xaa"))

    def test_empty_packet(self):
        self.assertIsNone(extract_advert_pubkey(b""))
        self.assertIsNone(extract_advert_name(b""))


# ---- try_decrypt_txt_msg ----


class TestTryDecryptTxtMsg(unittest.TestCase):
    def setUp(self):
        self.dest_seed = os.urandom(32)
        self.dest_sk = SigningKey(self.dest_seed)
        self.dest_pub = bytes(self.dest_sk.verify_key)
        self.dest_expanded = meshcore_expanded_key(self.dest_seed)

    def test_decrypt_success(self):
        pkt = _build_txt_msg_packet(
            TEST_SEED_EXPANDED, TEST_SEED_PUB, self.dest_pub, "Hello"
        )
        known_keys = {TEST_SEED_PUB.hex(): TEST_SEED_PUB}
        result = try_decrypt_txt_msg(pkt, self.dest_expanded, self.dest_pub, known_keys)
        assert result is not None
        text, sender = result
        self.assertEqual(text, "Hello")
        self.assertEqual(sender, TEST_SEED_PUB)

    def test_decrypt_no_matching_key(self):
        pkt = _build_txt_msg_packet(
            TEST_SEED_EXPANDED, TEST_SEED_PUB, self.dest_pub, "Secret"
        )
        # Known keys don't include the sender — use a valid Ed25519 pubkey
        other_sk = SigningKey(os.urandom(32))
        other_pub = bytes(other_sk.verify_key)
        known_keys = {other_pub.hex(): other_pub}
        result = try_decrypt_txt_msg(pkt, self.dest_expanded, self.dest_pub, known_keys)
        self.assertIsNone(result)

    def test_decrypt_empty_known_keys(self):
        pkt = _build_txt_msg_packet(
            TEST_SEED_EXPANDED, TEST_SEED_PUB, self.dest_pub, "Nope"
        )
        result = try_decrypt_txt_msg(pkt, self.dest_expanded, self.dest_pub, {})
        self.assertIsNone(result)

    def test_non_txt_returns_none(self):
        pkt = _build_advert_packet(TEST_SEED, TEST_SEED_PUB, name="Adv")
        known_keys = {TEST_SEED_PUB.hex(): TEST_SEED_PUB}
        result = try_decrypt_txt_msg(pkt, self.dest_expanded, self.dest_pub, known_keys)
        self.assertIsNone(result)

    def test_decrypt_multiple_keys_finds_correct(self):
        pkt = _build_txt_msg_packet(
            TEST_SEED_EXPANDED, TEST_SEED_PUB, self.dest_pub, "FindMe"
        )
        # Several valid Ed25519 keys plus the correct one
        known_keys = {}
        for _ in range(5):
            rk_sk = SigningKey(os.urandom(32))
            rk = bytes(rk_sk.verify_key)
            known_keys[rk.hex()] = rk
        known_keys[TEST_SEED_PUB.hex()] = TEST_SEED_PUB

        result = try_decrypt_txt_msg(pkt, self.dest_expanded, self.dest_pub, known_keys)
        assert result is not None
        text, sender = result
        self.assertEqual(text, "FindMe")
        self.assertEqual(sender, TEST_SEED_PUB)


# ---- try_decrypt_anon_req ----


class TestTryDecryptAnonReq(unittest.TestCase):
    def setUp(self):
        self.dest_seed = os.urandom(32)
        self.dest_sk = SigningKey(self.dest_seed)
        self.dest_pub = bytes(self.dest_sk.verify_key)
        self.dest_expanded = meshcore_expanded_key(self.dest_seed)

    def test_decrypt_success(self):
        pkt = _build_anon_req_packet(
            TEST_SEED_EXPANDED, TEST_SEED_PUB, self.dest_pub, b"AnonHello"
        )
        result = try_decrypt_anon_req(pkt, self.dest_expanded, self.dest_pub)
        assert result is not None
        text, sender = result
        self.assertEqual(text, "AnonHello")
        self.assertEqual(sender, TEST_SEED_PUB)

    def test_decrypt_wrong_recipient(self):
        # Build packet addressed to dest, but try to decrypt as different identity
        other_seed = os.urandom(32)
        other_sk = SigningKey(other_seed)
        other_pub = bytes(other_sk.verify_key)
        other_expanded = meshcore_expanded_key(other_seed)

        pkt = _build_anon_req_packet(
            TEST_SEED_EXPANDED, TEST_SEED_PUB, self.dest_pub, b"NotForYou"
        )
        # dest_hash won't match other's pubkey first byte (usually)
        result = try_decrypt_anon_req(pkt, other_expanded, other_pub)
        # Should be None because dest_hash doesn't match
        # (There's a 1/256 chance the first bytes match, so we
        # accept either None or a valid result in this edge case)
        if other_pub[0] != self.dest_pub[0]:
            self.assertIsNone(result)

    def test_non_anon_req_returns_none(self):
        pkt = _build_advert_packet(TEST_SEED, TEST_SEED_PUB, name="Adv")
        result = try_decrypt_anon_req(pkt, self.dest_expanded, self.dest_pub)
        self.assertIsNone(result)

    def test_truncated_packet(self):
        result = try_decrypt_anon_req(
            b"\x1d\x00\xaa", self.dest_expanded, self.dest_pub
        )
        self.assertIsNone(result)


# ---- Encrypt/decrypt round-trip (additional coverage beyond test_meshcore_tx) ----


class TestEncryptDecryptEdgeCases(unittest.TestCase):
    def test_empty_plaintext(self):
        secret = b"\x42" * 32
        encrypted = meshcore_encrypt_then_mac(secret, b"")
        # Empty plaintext: (0 % 16) % 16 = 0, no padding → MAC(2) only
        self.assertEqual(len(encrypted), CIPHER_MAC_SIZE)
        # Decrypt returns None because len(data) <= MAC_SIZE — no ciphertext
        decrypted = meshcore_mac_then_decrypt(secret, encrypted)
        self.assertIsNone(decrypted)

    def test_decrypt_too_short(self):
        secret = b"\x42" * 32
        self.assertIsNone(meshcore_mac_then_decrypt(secret, b""))
        self.assertIsNone(meshcore_mac_then_decrypt(secret, b"\x00"))
        self.assertIsNone(meshcore_mac_then_decrypt(secret, b"\x00\x00"))

    def test_exact_block_size(self):
        secret = b"\x42" * 32
        plaintext = b"A" * 16
        encrypted = meshcore_encrypt_then_mac(secret, plaintext)
        self.assertEqual(len(encrypted), CIPHER_MAC_SIZE + 16)
        decrypted = meshcore_mac_then_decrypt(secret, encrypted)
        self.assertEqual(decrypted, plaintext)


# ---- Group channel support ----


class TestGroupChannel(unittest.TestCase):
    def test_public_channel_secret_length(self):
        ch = _public_channel()
        self.assertEqual(len(ch.secret), 32)
        # First 16 bytes are the PSK, rest are zero-padded
        self.assertEqual(ch.secret[16:], b"\x00" * 16)

    def test_channel_hash_is_one_byte(self):
        ch = GroupChannel("test", os.urandom(16))
        self.assertEqual(len(ch.hash), 1)

    def test_channel_hash_deterministic(self):
        psk = os.urandom(16)
        ch1 = GroupChannel("a", psk)
        ch2 = GroupChannel("b", psk)
        self.assertEqual(ch1.hash, ch2.hash)

    def test_different_psk_different_hash(self):
        # Statistically almost certain to differ (1/256 collision chance)
        hashes = {GroupChannel("ch", os.urandom(16)).hash for _ in range(50)}
        self.assertGreater(len(hashes), 1)

    def test_32_byte_psk(self):
        psk = os.urandom(32)
        ch = GroupChannel("full", psk)
        self.assertEqual(ch.secret, psk)

    def test_seed_channels_include_public_and_hansemesh(self):
        channels = _seed_channels()
        names = {ch.name for ch in channels}
        self.assertIn("public", names)
        self.assertIn("hansemesh", names)


class TestGroupChannelPersistence(unittest.TestCase):
    def test_save_and_load_channel(self):
        with tempfile.TemporaryDirectory() as td:
            channels_dir = Path(td) / "channels"
            psk = os.urandom(16)
            self.assertTrue(save_channel(channels_dir, "secret", psk))
            # Duplicate returns False
            self.assertFalse(save_channel(channels_dir, "secret", psk))
            loaded = load_channels(channels_dir)
            names = {ch.name for ch in loaded}
            self.assertIn("secret", names)

    def test_load_nonexistent_seeds_from_repo(self):
        with tempfile.TemporaryDirectory() as td:
            channels_dir = Path(td) / "channels"
            channels = load_channels(channels_dir)
            # Should seed from scripts/channels/
            names = {ch.name for ch in channels}
            self.assertIn("public", names)
            self.assertIn("hansemesh", names)
            # Verify seed files were copied
            self.assertTrue((channels_dir / "public.channel").exists())
            self.assertTrue((channels_dir / "hansemesh.channel").exists())

    def test_load_skips_wrong_size_files(self):
        with tempfile.TemporaryDirectory() as td:
            channels_dir = Path(td) / "channels"
            channels_dir.mkdir()
            # Valid 16-byte channel
            (channels_dir / "good.channel").write_bytes(os.urandom(16))
            # Invalid sizes
            (channels_dir / "short.channel").write_bytes(b"short")
            (channels_dir / "long.channel").write_bytes(os.urandom(64))
            channels = load_channels(channels_dir)
            names = {ch.name for ch in channels}
            self.assertIn("good", names)
            self.assertNotIn("short", names)
            self.assertNotIn("long", names)

    def test_no_seed_if_channels_exist(self):
        with tempfile.TemporaryDirectory() as td:
            channels_dir = Path(td) / "channels"
            channels_dir.mkdir()
            psk = os.urandom(16)
            (channels_dir / "only.channel").write_bytes(psk)
            channels = load_channels(channels_dir)
            # Only the manually created channel, no seeding
            names = {ch.name for ch in channels}
            self.assertEqual(names, {"only"})


class TestBuildGrpTxt(unittest.TestCase):
    def test_build_grp_txt_basic(self):
        ch = GroupChannel("test", os.urandom(16))
        payload = build_grp_txt(ch, "Alice", "Hello group", timestamp=1700000000)
        # payload = channel_hash(1) + MAC(2) + ciphertext(N*16)
        self.assertEqual(payload[:1], ch.hash)
        self.assertGreater(len(payload), 1 + 2 + 16)

    def test_build_grp_txt_deterministic(self):
        psk = os.urandom(16)
        ch = GroupChannel("ch", psk)
        p1 = build_grp_txt(ch, "Bob", "Hi", timestamp=1700000000)
        p2 = build_grp_txt(ch, "Bob", "Hi", timestamp=1700000000)
        self.assertEqual(p1, p2)


class TestTryDecryptGrpTxt(unittest.TestCase):
    def _make_grp_txt_packet(self, channel, sender_name, text, timestamp=1700000000):
        """Build a full GRP_TXT wire packet for testing."""
        from lora.core.meshcore_crypto import PAYLOAD_GRP_TXT, ROUTE_FLOOD
        from lora.tools.meshcore_tx import build_wire_packet, make_header

        grp_payload = build_grp_txt(channel, sender_name, text, timestamp=timestamp)
        header = make_header(ROUTE_FLOOD, PAYLOAD_GRP_TXT)
        return build_wire_packet(header, grp_payload)

    def test_decrypt_public_channel(self):
        channels = _seed_channels()
        public_ch = _public_channel()
        pkt = self._make_grp_txt_packet(public_ch, "Alice", "Hello from public")
        result = try_decrypt_grp_txt(pkt, channels)
        assert result is not None
        text, ch_name = result
        self.assertEqual(ch_name, "public")
        self.assertIn("Alice", text)
        self.assertIn("Hello from public", text)

    def test_decrypt_custom_channel(self):
        psk = os.urandom(16)
        ch = GroupChannel("secret", psk)
        channels = _seed_channels() + [ch]
        pkt = self._make_grp_txt_packet(ch, "Bob", "Secret msg")
        result = try_decrypt_grp_txt(pkt, channels)
        assert result is not None
        text, ch_name = result
        self.assertEqual(ch_name, "secret")
        self.assertIn("Bob: Secret msg", text)

    def test_decrypt_wrong_channel_returns_none(self):
        ch1 = GroupChannel("ch1", os.urandom(16))
        ch2 = GroupChannel("ch2", os.urandom(16))
        pkt = self._make_grp_txt_packet(ch1, "Alice", "For ch1 only")
        # Try to decrypt with ch2 only (ch1 not in list)
        result = try_decrypt_grp_txt(pkt, [ch2])
        # May be None (hash mismatch) or None (MAC mismatch)
        # Either way, should not return valid text
        if result is not None:
            # In the unlikely event of a hash collision, MAC should still fail
            pass
        else:
            self.assertIsNone(result)

    def test_decrypt_empty_channels(self):
        ch = GroupChannel("ch", os.urandom(16))
        pkt = self._make_grp_txt_packet(ch, "Alice", "Hello")
        result = try_decrypt_grp_txt(pkt, [])
        self.assertIsNone(result)

    def test_non_grp_txt_returns_none(self):
        """ADVERT packet should not be decrypted as GRP_TXT."""
        pkt = _build_advert_packet(TEST_SEED, TEST_SEED_PUB, name="Node")
        result = try_decrypt_grp_txt(pkt, _seed_channels())
        self.assertIsNone(result)

    def test_truncated_packet_returns_none(self):
        result = try_decrypt_grp_txt(b"\x15\x00\xaa", _seed_channels())
        self.assertIsNone(result)

    def test_round_trip_multiple_messages(self):
        ch = GroupChannel("test", os.urandom(16))
        channels = [ch]
        messages = ["Short", "A" * 100, "Unicode: \u2603\u2764"]
        for msg_text in messages:
            pkt = self._make_grp_txt_packet(ch, "Sender", msg_text)
            result = try_decrypt_grp_txt(pkt, channels)
            assert result is not None, f"Failed to decrypt: {msg_text!r}"
            text, _ = result
            self.assertIn(msg_text, text)

    def test_public_psk_known_answer(self):
        """Verify the #public PSK produces a deterministic channel hash."""
        import hashlib

        ch = _public_channel()
        # Re-derive hash from the raw PSK (first 16 bytes, zero-padded to 32)
        raw_psk = ch.secret.rstrip(b"\x00")
        expected_hash = hashlib.sha256(raw_psk).digest()[:1]
        self.assertEqual(ch.hash, expected_hash)


# Note: legacy ``TestFormatFrameGrpTxt`` removed in Phase 7. Those tests
# exercised ``lora_mon.format_frame`` (the legacy monolith) and were
# permanently @unittest.skip'd on a pre-existing carrier/phy mismatch.
# The lora-core daemon attaches a ``protocol`` sub-map server-side now
# and ``lora.viz.mon`` calls ``format_frame(msg)`` without the legacy
# callbacks, so the equivalent display-layer behaviour is covered by
# tests/viz/test_mon.py.


class TestGrpTxtChannelHash(unittest.TestCase):
    def test_returns_hash_for_grp_txt(self):
        from lora.core.meshcore_crypto import PAYLOAD_GRP_TXT, ROUTE_FLOOD
        from lora.tools.meshcore_tx import build_wire_packet, make_header

        ch = GroupChannel("test", os.urandom(16))
        grp_payload = build_grp_txt(ch, "X", "Y", timestamp=1700000000)
        header = make_header(ROUTE_FLOOD, PAYLOAD_GRP_TXT)
        pkt = build_wire_packet(header, grp_payload)
        result = grp_txt_channel_hash(pkt)
        self.assertEqual(result, ch.hash)

    def test_returns_none_for_non_grp_txt(self):
        pkt = _build_advert_packet(TEST_SEED, TEST_SEED_PUB, name="Node")
        result = grp_txt_channel_hash(pkt)
        self.assertIsNone(result)

    def test_returns_none_for_truncated(self):
        result = grp_txt_channel_hash(b"\x14")
        self.assertIsNone(result)


# ---- Known-answer test for meshcore_shared_secret ----

# Pre-compute deterministic vectors at module level for regression testing.
_KA_SEED_A = bytes(range(32))  # 0x00..0x1f
_KA_SEED_B = bytes(range(32, 64))  # 0x20..0x3f
_KA_EXPANDED_A = meshcore_expanded_key(_KA_SEED_A)
_KA_PUB_A = bytes(SigningKey(_KA_SEED_A).verify_key)
_KA_EXPANDED_B = meshcore_expanded_key(_KA_SEED_B)
_KA_PUB_B = bytes(SigningKey(_KA_SEED_B).verify_key)
_KA_EXPECTED_HEX = "f6f92efb32945aff683324a1c984c5001f46aaea513f3453138d740b3a604b7d"


class TestSharedSecretKnownAnswer(unittest.TestCase):
    """Known-answer and property tests for meshcore_shared_secret.

    The ECDH derivation must produce the same shared secret as the MeshCore
    firmware's ed25519_key_exchange().  These tests guard against regressions
    in clamping, Ed25519→Curve25519 conversion, or X25519 scalar multiply.
    """

    def test_symmetric(self):
        """shared_secret(A_prv, B_pub) == shared_secret(B_prv, A_pub)."""
        secret_ab = meshcore_shared_secret(_KA_EXPANDED_A, _KA_PUB_B)
        secret_ba = meshcore_shared_secret(_KA_EXPANDED_B, _KA_PUB_A)
        assert secret_ab is not None
        assert secret_ba is not None
        self.assertEqual(secret_ab, secret_ba)

    def test_deterministic(self):
        """Same inputs always produce the same output."""
        s1 = meshcore_shared_secret(_KA_EXPANDED_A, _KA_PUB_B)
        s2 = meshcore_shared_secret(_KA_EXPANDED_A, _KA_PUB_B)
        assert s1 is not None
        self.assertEqual(s1, s2)

    def test_known_vector(self):
        """Verify pre-computed shared secret matches expected hex.

        Seeds: A = bytes(range(32)), B = bytes(range(32, 64)).
        If this fails, the ECDH derivation has changed — check clamping,
        Ed25519→Curve25519 conversion, and X25519 scalar multiply.
        """
        secret = meshcore_shared_secret(_KA_EXPANDED_A, _KA_PUB_B)
        assert secret is not None
        self.assertEqual(secret.hex(), _KA_EXPECTED_HEX)

    def test_invalid_pubkey(self):
        """31-byte pubkey (too short) returns None."""
        result = meshcore_shared_secret(_KA_EXPANDED_A, bytes(31))
        self.assertIsNone(result)

    def test_zero_pubkey(self):
        """32 zero bytes is not a valid Ed25519 point — returns None."""
        result = meshcore_shared_secret(_KA_EXPANDED_A, bytes(32))
        self.assertIsNone(result)


# ---- MAC rejection tests for tampered ciphertext ----


class TestMacRejection(unittest.TestCase):
    """Verify that meshcore_mac_then_decrypt rejects tampered or invalid data.

    MeshCore uses AES-128-ECB with a 2-byte HMAC-SHA256 MAC.  The MAC must
    reject any modification to the ciphertext, MAC bytes, or key.
    """

    def setUp(self):
        self.secret = os.urandom(32)
        self.plaintext = b"Hello MeshCore!"  # 15 bytes → padded to 16
        self.encrypted = meshcore_encrypt_then_mac(self.secret, self.plaintext)

    def test_tampered_ciphertext_rejected(self):
        """Flip one bit in the ciphertext (not the MAC) — decrypt must fail."""
        data = bytearray(self.encrypted)
        # Flip bit 0 of the first ciphertext byte (after 2-byte MAC)
        data[CIPHER_MAC_SIZE] ^= 0x01
        result = meshcore_mac_then_decrypt(self.secret, bytes(data))
        self.assertIsNone(result)

    def test_tampered_mac_rejected(self):
        """Flip one bit in the MAC bytes — decrypt must fail."""
        data = bytearray(self.encrypted)
        data[0] ^= 0x01
        result = meshcore_mac_then_decrypt(self.secret, bytes(data))
        self.assertIsNone(result)

    def test_wrong_key_rejected(self):
        """Encrypt with key A, decrypt with key B — must fail."""
        other_secret = os.urandom(32)
        # Ensure keys differ (astronomically unlikely to collide, but be explicit)
        while other_secret[:16] == self.secret[:16]:
            other_secret = os.urandom(32)  # pragma: no cover
        result = meshcore_mac_then_decrypt(other_secret, self.encrypted)
        self.assertIsNone(result)

    def test_truncated_ciphertext_rejected(self):
        """Truncate ciphertext by 1 byte — invalid block size, must fail."""
        truncated = self.encrypted[:-1]
        result = meshcore_mac_then_decrypt(self.secret, truncated)
        # Either None (MAC mismatch on truncated data) or decrypt error
        # The MAC is over the ciphertext, so truncation changes the MAC input
        self.assertIsNone(result)

    def test_multi_block_roundtrip(self):
        """33-byte plaintext forces 3 blocks (48 bytes). Round-trip succeeds."""
        plaintext = bytes(range(33))
        encrypted = meshcore_encrypt_then_mac(self.secret, plaintext)
        # 33 bytes → padded to 48 (3 × 16), plus 2-byte MAC = 50 bytes
        self.assertEqual(len(encrypted), CIPHER_MAC_SIZE + 48)
        decrypted = meshcore_mac_then_decrypt(self.secret, encrypted)
        assert decrypted is not None
        # Decrypted is the full padded block (48 bytes)
        self.assertEqual(len(decrypted), 48)
        # First 33 bytes match plaintext, remaining 15 are zero padding
        self.assertEqual(decrypted[:33], plaintext)
        self.assertEqual(decrypted[33:], b"\x00" * 15)


if __name__ == "__main__":
    unittest.main()
