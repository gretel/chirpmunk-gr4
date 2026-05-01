#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""
conftest.py -- Shared test fixtures for the gr4-lora test suite.

Single source of truth for: identity helpers, BridgeState factory, mock
sockets, deterministic test seeds, packet builders, and companion protocol
constants/frame codec helpers.

Usage from any test_*.py in the same directory:

    import conftest
    state = conftest.make_state(sf=12)
    alice, bob = conftest.make_two_identities()
"""

from __future__ import annotations

import socket
import struct
import tempfile
import time
from pathlib import Path
from typing import Any

from lora.bridges.meshcore import protocol as bridge_protocol  # noqa: F401
from lora.bridges.meshcore.protocol import build_contact_record
from lora.bridges.meshcore.state import BridgeState
from lora.core.meshcore_crypto import (
    load_or_create_identity,
    meshcore_expanded_key,
)
from lora.identity import IdentityConfig, IdentityStore
from lora.tools.meshcore_tx import (
    build_advert_raw as build_advert,
)
from lora.tools.meshcore_tx import (
    build_anon_req_raw as build_anon_req,
)
from lora.tools.meshcore_tx import (
    build_txt_msg_raw as build_txt_msg,
)


# Namespace alias kept so tests can call ``conftest.bridge.BridgeState``
# (and ``_build_contact_record``) without importing the canonical paths
# directly.
class _BridgeNS:
    BridgeState = BridgeState
    _build_contact_record = staticmethod(build_contact_record)


bridge = _BridgeNS()

# ---------------------------------------------------------------------------
# Identity helpers
# ---------------------------------------------------------------------------


def make_identity():
    """Create a temporary identity for testing."""
    tmpdir = tempfile.mkdtemp()
    path = Path(tmpdir) / "identity.bin"
    expanded_prv, pub_key, seed = load_or_create_identity(path)
    return expanded_prv, pub_key, seed


def make_two_identities():
    """Create two identities (sender + receiver) for crypto tests."""
    d1 = tempfile.mkdtemp()
    d2 = tempfile.mkdtemp()
    prv_a, pub_a, seed_a = load_or_create_identity(Path(d1) / "a.bin")
    prv_b, pub_b, seed_b = load_or_create_identity(Path(d2) / "b.bin")
    return (prv_a, pub_a, seed_a), (prv_b, pub_b, seed_b)


# ---------------------------------------------------------------------------
# BridgeState factory
# ---------------------------------------------------------------------------


def make_state(**kwargs):
    """Create a BridgeState with test defaults using temp directories.

    Backward-compatible signature: tests pass legacy ``pub_key`` /
    ``expanded_prv`` / ``seed`` / ``keys_dir`` / ``channels_dir`` /
    ``contacts_dir``; we materialise them into a temp
    :class:`IdentityStore` and forward the rest to the new
    :class:`BridgeState` constructor.
    """
    tmpdir = Path(tempfile.mkdtemp())
    keys_dir = kwargs.pop("keys_dir", tmpdir / "keys")
    channels_dir = kwargs.pop("channels_dir", tmpdir / "channels")
    contacts_dir = kwargs.pop("contacts_dir", tmpdir / "contacts")

    # Identity material: explicit seed > legacy args > newly minted.
    seed = kwargs.pop("seed", None)
    pub_key = kwargs.pop("pub_key", None)
    expanded_prv = kwargs.pop("expanded_prv", None)
    identity_file = tmpdir / "identity.bin"
    if seed is None or pub_key is None or expanded_prv is None:
        from nacl.signing import SigningKey

        sk = SigningKey.generate()
        seed = bytes(sk)
        identity_file.write_bytes(seed)
        # Re-load through helper so caches stay consistent.
        expanded_prv2, pub_key2, seed2 = load_or_create_identity(identity_file)
        seed = seed2
        pub_key = pub_key2
        expanded_prv = expanded_prv2
    else:
        identity_file.write_bytes(seed)

    identity_cfg = IdentityConfig(
        identity_file=identity_file,
        keys_dir=keys_dir,
        channels_dir=channels_dir,
        contacts_dir=contacts_dir,
    )
    store = IdentityStore(identity_cfg)
    store._ensure_loaded()

    eeprom_path = kwargs.pop("eeprom_path", tmpdir / "config.cbor")
    kwargs.setdefault("name", "test-node")
    kwargs.setdefault("freq_mhz", 869.618)
    kwargs.setdefault("bw_khz", 62.5)
    kwargs.setdefault("sf", 8)
    kwargs.setdefault("cr", 4)
    kwargs.setdefault("tx_power", 14)
    return BridgeState(
        identity=store,
        eeprom_path=eeprom_path,
        contacts_dir=contacts_dir,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Mock socket
# ---------------------------------------------------------------------------


class FakeUDPSock(socket.socket):
    """Minimal UDP socket mock that captures sendto calls."""

    def __init__(self) -> None:
        super().__init__(socket.AF_INET, socket.SOCK_DGRAM)
        self.close()  # release the FD immediately; we only need the type
        self.sent: list[bytes] = []

    def sendto(self, *args: Any, **kwargs: Any) -> int:  # type: ignore[override]
        # Signature flexibility: real socket has multiple overloads
        # (data, addr) and (data, flags, addr). Capture the data buffer.
        data = args[0] if args else kwargs.get("data", b"")
        self.sent.append(bytes(data))
        return len(data)


# ---------------------------------------------------------------------------
# Deterministic test seeds
# ---------------------------------------------------------------------------

TEST_SEED = bytes.fromhex(
    "deadbeefcafebabe0123456789abcdeffedcba9876543210baadf00ddeadbeef"
)
TEST_SEED_PUB = bytes.fromhex(
    "682500378e875577ff16b044bb6f2823bc4a7fcb3f34cab962f13553fb2369b5"
)
TEST_SEED_EXPANDED = meshcore_expanded_key(TEST_SEED)


# ---------------------------------------------------------------------------
# Packet builder helpers
# ---------------------------------------------------------------------------


def build_test_advert_packet(seed: bytes, pub: bytes, name: str = "") -> bytes:
    """Build a minimal ADVERT packet for testing extraction functions."""
    return build_advert(seed, pub, name=name)


def build_test_txt_msg_packet(
    sender_expanded: bytes,
    sender_pub: bytes,
    dest_pub: bytes,
    text: str,
    timestamp: int = 1700000000,
) -> bytes:
    """Build a TXT_MSG packet for testing decryption."""
    return build_txt_msg(
        sender_expanded, sender_pub, dest_pub, text, timestamp=timestamp
    )


def build_test_anon_req_packet(
    sender_expanded: bytes,
    sender_pub: bytes,
    dest_pub: bytes,
    message: bytes,
    timestamp: int = 1700000000,
) -> bytes:
    """Build an ANON_REQ packet for testing decryption."""
    return build_anon_req(
        sender_expanded, sender_pub, dest_pub, message, timestamp=timestamp
    )


# ---------------------------------------------------------------------------
# Contact record builder (used by bridge tests)
# ---------------------------------------------------------------------------


def make_contact_record(
    pubkey: bytes,
    name: str = "peer",
    node_type: int = 0x01,
    lat_e6: int = 0,
    lon_e6: int = 0,
    last_advert: int = 0,
) -> bytes:
    """Build a 147-byte contact record for testing."""
    return bridge._build_contact_record(
        pubkey,
        name=name,
        node_type=node_type,
        lat=lat_e6,
        lon=lon_e6,
        last_advert=last_advert,
    )


# ---------------------------------------------------------------------------
# Companion protocol constants
# ---------------------------------------------------------------------------

FRAME_TX = 0x3C
FRAME_RX = 0x3E

CMD_APP_START = 0x01
CMD_SEND_TXT_MSG = 0x02
CMD_GET_CONTACTS = 0x04
CMD_SEND_SELF_ADVERT = 0x07
CMD_SYNC_NEXT_MESSAGE = 0x0A
CMD_EXPORT_CONTACT = 0x11
CMD_IMPORT_CONTACT = 0x12
CMD_DEVICE_QUERY = 0x16
CMD_GET_CHANNEL = 0x1F
CMD_SET_ADVERT_NAME = 0x08

RESP_OK = 0x00
RESP_ERROR = 0x01
RESP_CONTACT_START = 0x02
RESP_CONTACT = 0x03
RESP_CONTACT_END = 0x04
RESP_SELF_INFO = 0x05
RESP_MSG_SENT = 0x06
RESP_CONTACT_MSG_RECV = 0x07
RESP_NO_MORE_MSGS = 0x0A
RESP_CONTACT_URI = 0x0B
RESP_DEVICE_INFO = 0x0D
RESP_CONTACT_MSG_RECV_V3 = 0x10
RESP_CHANNEL_INFO = 0x12

PUSH_MESSAGES_WAITING = 0x83
PUSH_NEW_ADVERT = 0x8A

BRIDGE_PORT = 7834


# ---------------------------------------------------------------------------
# Companion protocol frame helpers
# ---------------------------------------------------------------------------


def frame_encode_tx(payload: bytes) -> bytes:
    """Encode a TX frame: 0x3C + len_le16 + payload."""
    return bytes([FRAME_TX]) + struct.pack("<H", len(payload)) + payload


def recv_frames(sock: socket.socket, timeout: float = 5.0) -> list[bytes]:
    """Receive all available companion response frames within timeout."""
    frames: list[bytes] = []
    buf = bytearray()
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        remaining = max(0.1, deadline - time.monotonic())
        sock.settimeout(remaining)
        try:
            chunk = sock.recv(4096)
        except socket.timeout, TimeoutError:
            break
        if not chunk:
            break
        buf.extend(chunk)

        while len(buf) >= 3:
            if buf[0] != FRAME_RX:
                buf.pop(0)
                continue
            length = struct.unpack_from("<H", buf, 1)[0]
            if len(buf) < 3 + length:
                break
            frames.append(bytes(buf[3 : 3 + length]))
            del buf[: 3 + length]

        has_response = any(
            f[0] not in (PUSH_MESSAGES_WAITING, PUSH_NEW_ADVERT) for f in frames
        )
        if has_response:
            time.sleep(0.1)
            try:
                sock.settimeout(0.2)
                extra = sock.recv(4096)
                if extra:
                    buf.extend(extra)
                    while len(buf) >= 3:
                        if buf[0] != FRAME_RX:
                            buf.pop(0)
                            continue
                        length = struct.unpack_from("<H", buf, 1)[0]
                        if len(buf) < 3 + length:
                            break
                        frames.append(bytes(buf[3 : 3 + length]))
                        del buf[: 3 + length]
            except socket.timeout, TimeoutError:
                pass
            break

    return frames


def recv_until(
    sock: socket.socket,
    count: int,
    timeout: float = 3.0,
) -> list[bytes]:
    """Receive exactly `count` companion frames, or return what we got on timeout."""
    sock.settimeout(timeout)
    buf = bytearray()
    frames: list[bytes] = []
    deadline = time.monotonic() + timeout

    while len(frames) < count:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        sock.settimeout(max(remaining, 0.01))
        try:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf.extend(chunk)
        except socket.timeout, TimeoutError:
            break

        i = 0
        while i < len(buf):
            if buf[i] != FRAME_RX:
                i += 1
                continue
            if i + 3 > len(buf):
                break
            length = struct.unpack_from("<H", buf, i + 1)[0]
            if i + 3 + length > len(buf):
                break
            frames.append(bytes(buf[i + 3 : i + 3 + length]))
            i += 3 + length
        buf = buf[i:]

    return frames


def send_cmd(sock: socket.socket, payload: bytes) -> list[bytes]:
    """Send a command and return response frames."""
    sock.sendall(frame_encode_tx(payload))
    return recv_frames(sock)


# ---------------------------------------------------------------------------
# Free port helper
# ---------------------------------------------------------------------------


def free_port() -> int:
    """Bind to port 0 and return the OS-assigned port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
