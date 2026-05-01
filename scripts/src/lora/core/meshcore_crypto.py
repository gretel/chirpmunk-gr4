#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""
meshcore_crypto.py -- Shared MeshCore cryptographic primitives and key management.

Provides ECDH key exchange, AES-128-ECB encryption, HMAC-SHA256 MAC, identity
management, and a contact key store for MeshCore protocol operations.

Used by meshcore_tx.py (TX) and lora_mon.py (RX decryption).

Crypto stack:
  - Ed25519 signing (pynacl / libsodium)
  - X25519 ECDH key exchange (Ed25519 keys converted to Curve25519)
  - AES-128-ECB + 2-byte HMAC-SHA256 MAC (pycryptodome)
"""

from __future__ import annotations

import hashlib
import hmac as hmac_mod
import logging
import os
import shutil
import struct
import time
from pathlib import Path
from typing import Any

# NOTE: This module is imported as a library.  It creates a logger but does NOT
# call logging.basicConfig() — the calling script is responsible for
# configuring the root logger (via lora_common.setup_logging).
from Crypto.Cipher import AES
from nacl.bindings import (
    crypto_scalarmult,
    crypto_sign_ed25519_pk_to_curve25519,
)
from nacl.signing import SigningKey

log = logging.getLogger("lora.core.meshcore_crypto")

# ---- Constants ----

PUB_KEY_SIZE = 32
PRV_KEY_SIZE = 64
SIGNATURE_SIZE = 64
CIPHER_KEY_SIZE = 16
CIPHER_BLOCK_SIZE = 16
CIPHER_MAC_SIZE = 2
PATH_HASH_SIZE = 1

# Path hash size selector (upper 2 bits of path_len byte)
PATH_MODE_1BYTE = 0x00  # 1-byte hashes (default)
PATH_MODE_2BYTE = 0x01  # 2-byte hashes
PATH_MODE_3BYTE = 0x02  # 3-byte hashes
PATH_MODE_RSVD = 0x03  # reserved / invalid


def decode_path_len(raw: int) -> tuple[int, int, int]:
    """Decode the MeshCore packed path_len byte.

    The path_len byte on the wire encodes two fields:
      bits [7:6]  path_mode  — hash size selector: 0=1B, 1=2B, 2=3B, 3=reserved
      bits [5:0]  hash_count — number of hop hashes (0–63)

    The total number of path bytes to skip is: hash_count * hash_size.

    Returns:
        (hash_count, hash_size, path_byte_len)

    Source: MeshCore/src/Packet.h getPathHashSize(), getPathHashCount(), getPathByteLen()
    """
    hash_count = raw & 0x3F
    hash_size = ((raw >> 6) & 0x03) + 1
    return hash_count, hash_size, hash_count * hash_size


# MeshCore header field masks
ROUTE_MASK = 0x03  # bits [1:0]
PTYPE_SHIFT = 2  # bits [5:2]
PTYPE_MASK = 0x0F

# Route types (2 LSBs of header byte)
ROUTE_T_FLOOD = 0x00
ROUTE_FLOOD = 0x01
ROUTE_DIRECT = 0x02
ROUTE_T_DIRECT = 0x03

# Payload types (bits [5:2] of header byte)
PAYLOAD_REQ = 0x00
PAYLOAD_RESP = 0x01
PAYLOAD_TXT = 0x02
PAYLOAD_ACK = 0x03
PAYLOAD_ADVERT = 0x04
PAYLOAD_GRP_TXT = 0x05
PAYLOAD_GRP_DATA = 0x06
PAYLOAD_ANON_REQ = 0x07
PAYLOAD_PATH = 0x08
PAYLOAD_TRACE = 0x09
PAYLOAD_MULTI = 0x0A
PAYLOAD_CTRL = 0x0B
PAYLOAD_RAW_CUSTOM = 0x0F

# ADVERT node type flags
ADVERT_NODE_CHAT = 0x01
ADVERT_NODE_REPEATER = 0x02
ADVERT_NODE_ROOM = 0x03
ADVERT_NODE_SENSOR = 0x04
ADVERT_HAS_LOCATION = 0x10
ADVERT_HAS_FEATURE1 = 0x20
ADVERT_HAS_FEATURE2 = 0x40
ADVERT_HAS_NAME = 0x80

# Default paths — relative to the scripts/ directory so data stays in the project tree
# File path: scripts/src/lora/core/meshcore_crypto.py
#   parents[0] = scripts/src/lora/core
#   parents[1] = scripts/src/lora
#   parents[2] = scripts/src
#   parents[3] = scripts        <- anchor: bundled data lives at scripts/data/...
# Phase 1 keeps the legacy scripts/data/ location; Phase 5 will switch
# bridge/identity to read paths from [core.identity] in config.toml.
DEFAULT_DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "meshcore"
DEFAULT_IDENTITY_FILE = DEFAULT_DATA_DIR / "identity.bin"
DEFAULT_KEYS_DIR = DEFAULT_DATA_DIR / "keys"


# ---- Scalar clamping ----


def _clamp_scalar(scalar: bytes) -> bytes:
    """Apply Curve25519 clamping to a 32-byte scalar."""
    ba = bytearray(scalar)
    ba[0] &= 248
    ba[31] &= 63
    ba[31] |= 64
    return bytes(ba)


# ---- Key expansion ----


def meshcore_expanded_key(seed: bytes) -> bytes:
    """Expand a 32-byte seed into MeshCore's 64-byte private key format.

    SHA-512(seed), then clamp bytes [0] and [31]. First 32 bytes are the
    Ed25519 scalar, second 32 bytes are the signing nonce half.
    """
    expanded = hashlib.sha512(seed).digest()
    return _clamp_scalar(expanded[:32]) + expanded[32:]


# ---- ECDH shared secret ----


def meshcore_shared_secret(my_prv_64: bytes, other_pub_32: bytes) -> bytes | None:
    """Compute MeshCore ECDH shared secret via X25519.

    my_prv_64: 64-byte expanded private key (first 32 bytes = scalar).
    other_pub_32: 32-byte Ed25519 public key.

    Returns 32-byte shared secret, or None if other_pub_32 is not a valid
    Ed25519 public key (e.g. truncated, corrupted, or wrong key type).
    """
    try:
        curve_sk = _clamp_scalar(my_prv_64[:32])
        curve_pk = crypto_sign_ed25519_pk_to_curve25519(other_pub_32)
        return crypto_scalarmult(curve_sk, curve_pk)
    except Exception:
        return None


# ---- Encrypt / Decrypt ----


def meshcore_encrypt_then_mac(shared_secret: bytes, plaintext: bytes) -> bytes:
    """AES-128-ECB encrypt then HMAC-SHA256 MAC (truncated to 2 bytes).

    Returns: MAC(2) + ciphertext(N*16).
    """
    key = shared_secret[:CIPHER_KEY_SIZE]
    padded = plaintext + b"\x00" * (
        (CIPHER_BLOCK_SIZE - len(plaintext) % CIPHER_BLOCK_SIZE) % CIPHER_BLOCK_SIZE
    )
    cipher = AES.new(key, AES.MODE_ECB)
    ciphertext = cipher.encrypt(padded)
    mac = hmac_mod.new(
        shared_secret[:PUB_KEY_SIZE], ciphertext, hashlib.sha256
    ).digest()[:CIPHER_MAC_SIZE]
    return mac + ciphertext


def meshcore_mac_then_decrypt(shared_secret: bytes, data: bytes) -> bytes | None:
    """Verify 2-byte HMAC-SHA256 MAC then AES-128-ECB decrypt.

    Returns plaintext on success, None on MAC failure.
    """
    if len(data) <= CIPHER_MAC_SIZE:
        return None
    mac_received = data[:CIPHER_MAC_SIZE]
    ciphertext = data[CIPHER_MAC_SIZE:]
    mac_computed = hmac_mod.new(
        shared_secret[:PUB_KEY_SIZE], ciphertext, hashlib.sha256
    ).digest()[:CIPHER_MAC_SIZE]
    if mac_received != mac_computed:
        return None
    cipher = AES.new(shared_secret[:CIPHER_KEY_SIZE], AES.MODE_ECB)
    return cipher.decrypt(ciphertext)


# ---- Identity management ----


def load_or_create_identity(path: Path) -> tuple[bytes, bytes, bytes]:
    """Load or generate an Ed25519 keypair in MeshCore format.

    Returns (expanded_prv_64, pub_key_32, seed_32).
    File format: [seed(32) | pub_key(32)] = 64 bytes.
    """
    if path.exists():
        data = path.read_bytes()
        if len(data) == 64:
            seed = data[:32]
            pub = data[32:64]
            sk = SigningKey(seed)
            if bytes(sk.verify_key) == pub:
                expanded = meshcore_expanded_key(seed)
                log.info("loaded identity from %s pubkey=%s", path, pub.hex())
                return expanded, pub, seed
            else:
                log.warning("identity file %s has mismatched keys, regenerating", path)
        else:
            log.warning(
                "identity file %s has wrong size (%d), regenerating",
                path,
                len(data),
            )

    seed = os.urandom(32)
    sk = SigningKey(seed)
    pub = bytes(sk.verify_key)
    expanded = meshcore_expanded_key(seed)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(seed + pub)
    path.chmod(0o600)
    log.info("generated new identity at %s pubkey=%s", path, pub.hex())
    return expanded, pub, seed


def load_identity(path: Path) -> tuple[bytes, bytes, bytes] | None:
    """Load an existing identity (no generation). Returns None if missing."""
    if not path.exists():
        return None
    data = path.read_bytes()
    if len(data) != 64:
        return None
    seed = data[:32]
    pub = data[32:64]
    sk = SigningKey(seed)
    if bytes(sk.verify_key) != pub:
        return None
    return meshcore_expanded_key(seed), pub, seed


# ---- Contact key store ----


def load_known_keys(keys_dir: Path) -> dict[str, bytes]:
    """Load all known public keys from the keys directory.

    Returns {hex_pubkey: raw_32_bytes} for each .key file.
    File naming: <64-hex-chars>.key containing 32 raw bytes.
    """
    keys: dict[str, bytes] = {}
    if not keys_dir.is_dir():
        return keys
    for f in keys_dir.glob("*.key"):
        data = f.read_bytes()
        if len(data) == PUB_KEY_SIZE:
            keys[data.hex()] = data
        else:
            log.warning(
                "skipping corrupt key file %s (%d bytes, expected %d)",
                f,
                len(data),
                PUB_KEY_SIZE,
            )
    return keys


def save_pubkey(keys_dir: Path, pubkey: bytes, name: str | None = None) -> bool:
    """Save a public key to the key store. Returns True if newly saved."""
    try:
        keys_dir.mkdir(parents=True, exist_ok=True)
        path = keys_dir / f"{pubkey.hex()}.key"
        if path.exists():
            return False
        path.write_bytes(pubkey)
        return True
    except OSError as exc:
        log.warning("could not save key %s..: %s", pubkey.hex()[:8], exc)
        return False


# ---- RX decryption helpers ----


def parse_meshcore_header(data: bytes) -> dict[str, Any] | None:
    """Parse the MeshCore header byte and transport/path prefix.

    Returns dict with route, ptype, version, off (offset to app_payload), path_len
    or None if data is too short.
    """
    if len(data) < 2:
        return None
    hdr = data[0]
    route = hdr & 0x03
    ptype = (hdr >> 2) & 0x0F
    version = (hdr >> 6) & 0x03
    has_transport = route in (0, 3)

    off = 1
    if has_transport:
        if off + 4 > len(data):
            return None
        off += 4

    if off >= len(data):
        return None
    path_len = data[off]
    _, _, path_byte_len = decode_path_len(path_len)
    off += 1 + path_byte_len

    return {
        "route": route,
        "ptype": ptype,
        "version": version,
        "off": off,
        "path_len": path_len,
        "has_transport": has_transport,
    }


def extract_advert_pubkey(data: bytes) -> bytes | None:
    """Extract the 32-byte public key from an ADVERT packet payload."""
    hdr = parse_meshcore_header(data)
    if hdr is None or hdr["ptype"] != PAYLOAD_ADVERT:
        return None
    off = hdr["off"]
    if off + PUB_KEY_SIZE > len(data):
        return None
    return data[off : off + PUB_KEY_SIZE]


def extract_advert_name(data: bytes) -> str | None:
    """Extract the node name from an ADVERT packet (if present)."""
    hdr = parse_meshcore_header(data)
    if hdr is None or hdr["ptype"] != PAYLOAD_ADVERT:
        return None
    off = hdr["off"]
    # ADVERT payload: pubkey(32) + timestamp(4) + signature(64) + app_data
    app_off = off + PUB_KEY_SIZE + 4 + SIGNATURE_SIZE
    if app_off >= len(data):
        return None
    flags = data[app_off]
    name_off = app_off + 1
    if flags & 0x10:  # HAS_LOCATION
        name_off += 8
    if flags & 0x20:  # HAS_FEATURE1
        name_off += 2
    if flags & 0x40:  # HAS_FEATURE2
        name_off += 2
    if (flags & 0x80) and name_off < len(data):  # HAS_NAME
        # MeshCore names are max MAX_ADVERT_DATA_SIZE (32) bytes; bound the
        # slice to avoid unbounded RF-origin strings.
        raw = data[name_off : name_off + 32]
        return raw.decode("utf-8", errors="replace").rstrip("\x00")
    return None


def try_decrypt_txt_msg(
    data: bytes, our_prv: bytes, our_pub: bytes, known_keys: dict[str, bytes]
) -> tuple[str, bytes] | None:
    """Try to decrypt a TXT_MSG using all known keys.

    TXT_MSG payload: dest_hash(1) + src_hash(1) + MAC(2) + ciphertext
    Plaintext: timestamp(4) + txt_type_attempt(1) + text
    txt_type_attempt: upper 6 bits = txt_type, lower 2 bits = attempt (0-3)
    txt_type 0x00=plain text, 0x01=CLI command, 0x02=signed (4-byte pubkey prefix before text)

    Returns (decrypted_text, matched_pubkey) or None.
    """
    hdr = parse_meshcore_header(data)
    if hdr is None or hdr["ptype"] != PAYLOAD_TXT:
        return None
    off = hdr["off"]
    payload = data[off:]
    if len(payload) < 2 + CIPHER_MAC_SIZE + CIPHER_BLOCK_SIZE:
        return None

    # Payload layout: payload[0:1] = dest_hash, payload[1:2] = src_hash,
    # payload[2:] = encrypted body. dest_hash / src_hash are not consulted
    # here — we trial-decrypt against every known pubkey instead.
    encrypted = payload[2:]

    def _extract_text(plaintext: bytes) -> str | None:
        """Extract text from decrypted TXT_MSG plaintext."""
        if len(plaintext) <= 5:
            return None
        txt_type = plaintext[4] >> 2
        if txt_type == 0x02 and len(plaintext) > 9:
            # Signed message: skip 4-byte sender pubkey prefix
            return plaintext[9:].rstrip(b"\x00").decode("utf-8", errors="replace")
        return plaintext[5:].rstrip(b"\x00").decode("utf-8", errors="replace")

    for hex_key, pubkey_bytes in known_keys.items():
        secret = meshcore_shared_secret(our_prv, pubkey_bytes)
        if secret is None:
            continue
        plaintext = meshcore_mac_then_decrypt(secret, encrypted)
        if plaintext is not None:
            text = _extract_text(plaintext)
            if text is not None:
                return text, pubkey_bytes

    return None


def try_decrypt_anon_req(
    data: bytes, our_prv: bytes, our_pub: bytes
) -> tuple[str, bytes] | None:
    """Try to decrypt an ANON_REQ addressed to us.

    ANON_REQ payload: dest_hash(1) + sender_pub(32) + MAC(2) + ciphertext
    Plaintext: timestamp(4) + message_data

    Returns (decrypted_text, sender_pubkey) or None.
    """
    hdr = parse_meshcore_header(data)
    if hdr is None or hdr["ptype"] != PAYLOAD_ANON_REQ:
        return None
    off = hdr["off"]
    payload = data[off:]
    if len(payload) < 1 + PUB_KEY_SIZE + CIPHER_MAC_SIZE + CIPHER_BLOCK_SIZE:
        return None

    dest_hash = payload[0:1]
    sender_pub = payload[1 : 1 + PUB_KEY_SIZE]
    encrypted = payload[1 + PUB_KEY_SIZE :]

    our_hash = our_pub[:PATH_HASH_SIZE]
    if dest_hash != our_hash:
        return None

    secret = meshcore_shared_secret(our_prv, sender_pub)
    if secret is None:
        return None
    plaintext = meshcore_mac_then_decrypt(secret, encrypted)
    if plaintext is None:
        return None

    # Extract text: skip timestamp(4)
    if len(plaintext) > 4:
        text = plaintext[4:].rstrip(b"\x00").decode("utf-8", errors="replace")
        return text, sender_pub

    return None


# ---- Group channel support ----


# Seed channels bundled with the repo (scripts/data/meshcore/channels/*.channel).
# See DEFAULT_DATA_DIR above for the parents[3] anchor rationale.
SEED_CHANNELS_DIR = (
    Path(__file__).resolve().parents[3] / "data" / "meshcore" / "channels"
)

DEFAULT_CHANNELS_DIR = DEFAULT_DATA_DIR / "channels"


class GroupChannel:
    """A MeshCore group channel identified by a pre-shared key."""

    __slots__ = ("name", "secret", "hash")

    def __init__(self, name: str, psk_raw: bytes) -> None:
        self.name = name
        # Zero-pad to 32 bytes (MeshCore uses PUB_KEY_SIZE for HMAC key)
        self.secret = (psk_raw + b"\x00" * PUB_KEY_SIZE)[:PUB_KEY_SIZE]
        # Channel hash: first byte of SHA-256(psk_raw)
        self.hash = hashlib.sha256(psk_raw).digest()[:PATH_HASH_SIZE]

    def __repr__(self) -> str:
        return f"GroupChannel(name={self.name!r}, hash={self.hash.hex()})"


PUBLIC_CHANNEL_PSK = bytes.fromhex("8b3387e9c5cdea6ac9e5edbaa115cd72")


def load_channels(channels_dir: Path) -> list[GroupChannel]:
    """Load group channels from a directory.

    Each file is <name>.channel containing 16 or 32 raw PSK bytes.
    If the directory doesn't exist, seeds it from the bundled seed channels.

    Channel ordering follows MeshCore firmware convention: the "public" channel
    (fixed PSK 8b3387e9…) is always at index 0.  Other channels follow in
    alphabetical order by filename.  This ensures mccli channel indices match
    what companion apps expect (index 0 = public, index 1+ = named channels).
    """
    _seed_channels(channels_dir)
    channels: list[GroupChannel] = []
    if not channels_dir.is_dir():
        return channels
    for f in sorted(channels_dir.glob("*.channel")):
        data = f.read_bytes()
        if len(data) in (16, 32):
            channels.append(GroupChannel(f.stem, data))
        else:
            log.warning(
                "skipping corrupt channel file %s (%d bytes, expected 16 or 32)",
                f,
                len(data),
            )
    # Ensure public channel (fixed PSK) is always at index 0.
    # MeshCore firmware hardcodes public at slot 0; mccli relies on this ordering.
    public_psk_hash = hashlib.sha256(PUBLIC_CHANNEL_PSK).digest()[:PATH_HASH_SIZE]
    pub_idx = next(
        (i for i, ch in enumerate(channels) if ch.hash == public_psk_hash), None
    )
    if pub_idx is not None and pub_idx != 0:
        channels.insert(0, channels.pop(pub_idx))
    return channels


def save_channel(channels_dir: Path, name: str, psk_raw: bytes) -> bool:
    """Save a channel to the channel store. Returns True if newly saved."""
    try:
        channels_dir.mkdir(parents=True, exist_ok=True)
        path = channels_dir / f"{name}.channel"
        if path.exists():
            return False
        path.write_bytes(psk_raw)
        return True
    except OSError as exc:
        log.warning("could not save channel %r: %s", name, exc)
        return False


def _seed_channels(channels_dir: Path) -> None:
    """Copy bundled seed channels into the config dir if it has no channels."""
    if channels_dir.is_dir() and any(channels_dir.glob("*.channel")):
        return
    if not SEED_CHANNELS_DIR.is_dir():
        return
    try:
        channels_dir.mkdir(parents=True, exist_ok=True)
        for src in SEED_CHANNELS_DIR.glob("*.channel"):
            dest = channels_dir / src.name
            if not dest.exists():
                shutil.copy2(src, dest)
    except OSError as exc:
        log.warning("could not seed channels into %s: %s", channels_dir, exc)


def try_decrypt_grp_txt(
    data: bytes, channels: list[GroupChannel]
) -> tuple[str, str] | None:
    """Try to decrypt a GRP_TXT using known group channels.

    GRP_TXT payload: channel_hash(1) + MAC(2) + ciphertext(N*16)
    Plaintext: timestamp(4) + txt_type_attempt(1) + text(UTF-8)
    txt_type_attempt: upper 6 bits = txt_type, lower 2 bits = attempt (0-3)

    The text is formatted as "SenderName: message" by MeshCore firmware.

    Returns (text, channel_name) or None.
    """
    hdr = parse_meshcore_header(data)
    if hdr is None or hdr["ptype"] != PAYLOAD_GRP_TXT:
        return None
    off = hdr["off"]
    payload = data[off:]
    if len(payload) < PATH_HASH_SIZE + CIPHER_MAC_SIZE + CIPHER_BLOCK_SIZE:
        return None

    channel_hash = payload[:PATH_HASH_SIZE]
    encrypted = payload[PATH_HASH_SIZE:]  # MAC(2) + ciphertext

    for ch in channels:
        if ch.hash != channel_hash:
            continue
        plaintext = meshcore_mac_then_decrypt(ch.secret, encrypted)
        if plaintext is not None and len(plaintext) > 5:
            txt_type = plaintext[4]
            if (txt_type >> 2) == 0:  # TXT_TYPE_PLAIN
                text = plaintext[5:].rstrip(b"\x00").decode("utf-8", errors="replace")
                return text, ch.name
    return None


def compute_ack_hash(
    plaintext: bytes,
    sender_pub: bytes,
) -> bytes:
    """Compute the 4-byte MeshCore ACK hash for a received TXT_MSG.

    The ACK hash is SHA-256(plaintext_content, sender_pubkey) truncated to 4 bytes.
    plaintext_content = timestamp(4) + flags(1) + text (no null terminator).

    For TXT_TYPE_PLAIN (flags>>2 == 0): hash uses sender's pubkey.
    For TXT_TYPE_SIGNED (flags>>2 == 2): hash uses receiver's own pubkey.
    We handle only TXT_TYPE_PLAIN here (the common case).

    Args:
        plaintext: Decrypted message plaintext (timestamp + flags + text + zero padding).
        sender_pub: Sender's 32-byte Ed25519 public key.

    Returns: 4-byte ACK hash (little-endian uint32 on ARM, but we treat as raw bytes).
    """
    # Strip trailing zero padding from AES-ECB
    # Content is: timestamp(4) + flags(1) + text
    # Find the end of the text (first null byte after flags, or end of data)
    if len(plaintext) < 5:
        return hashlib.sha256(plaintext + sender_pub).digest()[:4]

    # Find text end: skip timestamp(4) + flags(1), then find null or use all
    flags = plaintext[4]
    txt_type = flags >> 2

    if txt_type == 0x02:
        # TXT_TYPE_SIGNED: timestamp(4) + flags(1) + sig_prefix(4) + text
        text_start = 9
    else:
        # TXT_TYPE_PLAIN: timestamp(4) + flags(1) + text
        text_start = 5

    # Find end of text (first null byte after text_start)
    text_end = text_start
    while text_end < len(plaintext) and plaintext[text_end] != 0:
        text_end += 1

    # Content to hash: everything from start through text (no null)
    content = plaintext[:text_end]

    h = hashlib.sha256()
    h.update(content)
    h.update(sender_pub)
    return h.digest()[:4]


def grp_txt_channel_hash(data: bytes) -> bytes | None:
    """Extract the channel hash byte from a GRP_TXT packet.

    Returns the 1-byte hash, or None if not a GRP_TXT.
    """
    hdr = parse_meshcore_header(data)
    if hdr is None or hdr["ptype"] != PAYLOAD_GRP_TXT:
        return None
    off = hdr["off"]
    if off >= len(data):
        return None
    return data[off : off + PATH_HASH_SIZE]


def build_grp_txt(
    channel: GroupChannel,
    sender_name: str,
    text: str,
    *,
    timestamp: int | None = None,
) -> bytes:
    """Build a GRP_TXT encrypted payload (without wire header).

    Plaintext: timestamp(4 LE) + txt_type(1) + "sender_name: text"
    Wire payload: channel_hash(1) + MAC(2) + ciphertext(N*16)
    """
    if timestamp is None:
        timestamp = int(time.time())

    ts_bytes = struct.pack("<I", timestamp)
    body = f"{sender_name}: {text}".encode("utf-8")
    plaintext = ts_bytes + b"\x00" + body  # txt_type=0 (plain)
    encrypted = meshcore_encrypt_then_mac(channel.secret, plaintext)
    return channel.hash + encrypted
