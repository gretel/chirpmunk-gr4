#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""MeshCore companion protocol — wire codec, constants, stateless builders.

Phase 4 split from ``meshcore_bridge.py``: this module owns
every byte of the framing layer plus the small stateless helpers that
build / parse MeshCore wire packets without needing access to bridge
runtime state.

Anything that requires :class:`BridgeState` access (per-command
handlers, RX dispatch) lives in :mod:`lora.bridges.meshcore.companion`.
EEPROM-bound state lives in :mod:`lora.bridges.meshcore.state`.
"""

from __future__ import annotations

import hashlib
import struct
from typing import Final, TypedDict

from lora.core.formatters import utf8_truncate
from lora.core.meshcore_crypto import (
    ADVERT_HAS_LOCATION,
    ADVERT_HAS_NAME,
    PAYLOAD_ACK,
    PAYLOAD_ADVERT,
    PAYLOAD_PATH,
    PTYPE_MASK,
    PTYPE_SHIFT,
    ROUTE_DIRECT,
    ROUTE_FLOOD,
    ROUTE_MASK,
    ROUTE_T_FLOOD,
    GroupChannel,
    decode_path_len,
    meshcore_encrypt_then_mac,
    meshcore_shared_secret,
)
from lora.tools.meshcore_tx import build_wire_packet, make_header

# ---------------------------------------------------------------------------
# Companion protocol framing
# ---------------------------------------------------------------------------

#: TCP listen port for the MeshCore companion bridge.  7834 instead of the
#: nominal 5000 because macOS AirPlay/Control Center binds 5000 by default.
BRIDGE_PORT: Final[int] = 7834

FRAME_TX: Final[int] = 0x3C  # client -> device ('<')
FRAME_RX: Final[int] = 0x3E  # device -> client ('>')
MAX_FRAME: Final[int] = 300  # max companion payload size

# ---------------------------------------------------------------------------
# Companion protocol commands (client -> bridge)
# ---------------------------------------------------------------------------

CMD_APP_START: Final[int] = 0x01
CMD_SEND_TXT_MSG: Final[int] = 0x02
CMD_SEND_CHAN_TXT_MSG: Final[int] = 0x03
CMD_GET_CONTACTS: Final[int] = 0x04
CMD_GET_DEVICE_TIME: Final[int] = 0x05
CMD_SET_DEVICE_TIME: Final[int] = 0x06
CMD_SEND_SELF_ADVERT: Final[int] = 0x07
CMD_SET_ADVERT_NAME: Final[int] = 0x08
CMD_ADD_UPDATE_CONTACT: Final[int] = 0x09
CMD_SYNC_NEXT_MESSAGE: Final[int] = 0x0A
CMD_SET_RADIO_PARAMS: Final[int] = 0x0B
CMD_SET_RADIO_TX_POWER: Final[int] = 0x0C
CMD_RESET_PATH: Final[int] = 0x0D
CMD_SET_ADVERT_LATLON: Final[int] = 0x0E
CMD_REMOVE_CONTACT: Final[int] = 0x0F
CMD_SHARE_CONTACT: Final[int] = 0x10
CMD_EXPORT_CONTACT: Final[int] = 0x11
CMD_IMPORT_CONTACT: Final[int] = 0x12
CMD_REBOOT: Final[int] = 0x13
CMD_GET_BATT_AND_STORAGE: Final[int] = 0x14
CMD_SET_TUNING_PARAMS: Final[int] = 0x15
CMD_DEVICE_QUERY: Final[int] = 0x16
CMD_EXPORT_PRIVATE_KEY: Final[int] = 0x17
CMD_IMPORT_PRIVATE_KEY: Final[int] = 0x18
CMD_LOGIN: Final[int] = 0x1A
CMD_STATUS_REQ: Final[int] = 0x1B
CMD_HAS_CONNECTION: Final[int] = 0x1C
CMD_LOGOUT: Final[int] = 0x1D
CMD_GET_CONTACT_BY_KEY: Final[int] = 0x1E
CMD_GET_CHANNEL: Final[int] = 0x1F
CMD_SET_CHANNEL: Final[int] = 0x20
CMD_TRACE: Final[int] = 0x24
CMD_SET_DEVICE_PIN: Final[int] = 0x25
CMD_SET_OTHER_PARAMS: Final[int] = 0x26
CMD_SEND_TELEMETRY_REQ: Final[int] = 0x27
CMD_GET_CUSTOM_VARS: Final[int] = 0x28
CMD_SET_CUSTOM_VAR: Final[int] = 0x29
CMD_GET_ADVERT_PATH: Final[int] = 0x2A
CMD_GET_TUNING_PARAMS: Final[int] = 0x2B
CMD_BINARY_REQ: Final[int] = 0x32
CMD_FACTORY_RESET: Final[int] = 0x33
CMD_PATH_DISCOVERY: Final[int] = 0x34
CMD_SET_FLOOD_SCOPE: Final[int] = 0x36
CMD_SEND_CONTROL_DATA: Final[int] = 0x37
CMD_GET_STATS: Final[int] = 0x38
CMD_SEND_ANON_REQ: Final[int] = 0x39
CMD_SET_AUTOADD_CONFIG: Final[int] = 0x3A
CMD_GET_AUTOADD_CONFIG: Final[int] = 0x3B
CMD_GET_ALLOWED_REPEAT_FREQ: Final[int] = 0x3C
CMD_SET_PATH_HASH_MODE: Final[int] = 0x3D
CMD_SET_DEFAULT_FLOOD_SCOPE: Final[int] = 0x3F
CMD_GET_DEFAULT_FLOOD_SCOPE: Final[int] = 0x40

# ---------------------------------------------------------------------------
# Companion protocol responses (bridge -> client)
# ---------------------------------------------------------------------------

RESP_OK: Final[int] = 0x00
RESP_ERROR: Final[int] = 0x01
RESP_CONTACT_START: Final[int] = 0x02
RESP_CONTACT: Final[int] = 0x03
RESP_CONTACT_END: Final[int] = 0x04
RESP_SELF_INFO: Final[int] = 0x05
RESP_MSG_SENT: Final[int] = 0x06
RESP_CONTACT_MSG_RECV: Final[int] = 0x07
RESP_CHANNEL_MSG_RECV: Final[int] = 0x08
RESP_CURRENT_TIME: Final[int] = 0x09
RESP_NO_MORE_MSGS: Final[int] = 0x0A
RESP_CONTACT_URI: Final[int] = 0x0B
RESP_BATTERY: Final[int] = 0x0C
RESP_DEVICE_INFO: Final[int] = 0x0D
RESP_CONTACT_MSG_RECV_V3: Final[int] = 0x10
RESP_CHANNEL_MSG_RECV_V3: Final[int] = 0x11
RESP_CHANNEL_INFO: Final[int] = 0x12
RESP_TUNING_PARAMS: Final[int] = 0x14
RESP_CUSTOM_VARS: Final[int] = 0x15
RESP_ADVERT_PATH: Final[int] = 0x16
RESP_STATS: Final[int] = 0x18
RESP_CODE_AUTOADD_CONFIG: Final[int] = 0x19
RESP_ALLOWED_REPEAT_FREQ: Final[int] = 0x1A
RESP_DEFAULT_FLOOD_SCOPE: Final[int] = 0x1C

# Companion error codes
ERR_CODE_NOT_FOUND: Final[int] = 2
ERR_CODE_ILLEGAL_ARG: Final[int] = 6

# ---------------------------------------------------------------------------
# Companion protocol push events (bridge -> client, unsolicited)
# ---------------------------------------------------------------------------

PUSH_ADVERTISEMENT: Final[int] = 0x80
PUSH_PATH_UPDATE: Final[int] = 0x81
PUSH_ACK: Final[int] = 0x82
PUSH_MESSAGES_WAITING: Final[int] = 0x83
PUSH_LOGIN_SUCCESS: Final[int] = 0x85
PUSH_LOGIN_FAIL: Final[int] = 0x86
PUSH_STATUS_RESPONSE: Final[int] = 0x87
PUSH_TRACE_DATA: Final[int] = 0x89
PUSH_NEW_ADVERT: Final[int] = 0x8A
PUSH_BINARY_RESPONSE: Final[int] = 0x8C
PUSH_PATH_DISCOVERY_RESPONSE: Final[int] = 0x8D
PUSH_CONTROL_DATA: Final[int] = 0x8E

# Repeat mode: valid frequencies (kHz) for client_repeat = 1
REPEAT_FREQ_RANGES: Final[list[tuple[int, int]]] = [
    (433000, 433000),
    (869000, 869000),
    (918000, 918000),
]

#: Size of a serialized contact record (firmware-compatible layout).
CONTACT_RECORD_SIZE: Final[int] = 147


def is_valid_repeat_freq(freq_khz: int) -> bool:
    """Return ``True`` if ``freq_khz`` is in an allowed repeat frequency range."""
    return any(lo <= freq_khz <= hi for lo, hi in REPEAT_FREQ_RANGES)


# ---------------------------------------------------------------------------
# Flood scope key derivation
# ---------------------------------------------------------------------------


def parse_flood_scope(value: str) -> bytes:
    """Parse a flood-scope config value into a 16-byte transport key.

    Accepted formats:

    * 32 hex characters (64 nibbles) → decoded as raw 16-byte key.
    * Any other non-empty string → ``sha256("#" + name)[:16]`` (matches
      MeshCore firmware ``RegionMap::findMatch()`` auto-key derivation).
    * Empty string / ``"*"`` / ``"0"`` / ``"None"`` → all-zero (disabled).
    """
    if not value or value in ("0", "None", "*"):
        return b"\x00" * 16
    if len(value) == 32:
        try:
            return bytes.fromhex(value)
        except ValueError:
            pass
    return hashlib.sha256(("#" + value).encode("utf-8")).digest()[:16]


# ---------------------------------------------------------------------------
# Companion frame codec
# ---------------------------------------------------------------------------


def frame_encode(payload: bytes) -> bytes:
    """Encode a response frame: ``0x3E + len_le16 + payload``."""
    return bytes([FRAME_RX]) + struct.pack("<H", len(payload)) + payload


class FrameDecoder:
    """Streaming decoder for companion protocol frames (``0x3C`` framing)."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._expected = 0
        self._header_done = False

    def feed(self, data: bytes) -> list[bytes]:
        """Feed raw TCP bytes, return list of complete payloads."""
        self._buf.extend(data)
        frames: list[bytes] = []
        while True:
            if not self._header_done:
                idx = self._buf.find(bytes([FRAME_TX]))
                if idx < 0:
                    self._buf.clear()
                    break
                if idx > 0:
                    del self._buf[:idx]
                if len(self._buf) < 3:
                    break
                self._expected = int.from_bytes(self._buf[1:3], "little")
                if self._expected > MAX_FRAME:
                    del self._buf[:1]
                    continue
                self._header_done = True
            total = 3 + self._expected
            if len(self._buf) < total:
                break
            payload = bytes(self._buf[3:total])
            del self._buf[:total]
            self._header_done = False
            frames.append(payload)
        return frames


# ---------------------------------------------------------------------------
# Helpers shared by RX/TX paths
# ---------------------------------------------------------------------------


def hash_payload(payload: bytes) -> bytes:
    """Hash a payload for TX echo / RX dedup filtering."""
    return hashlib.sha256(payload).digest()[:8]


def compute_transport_code(region_key: bytes, payload_type: int, payload: bytes) -> int:
    """Compute a 2-byte transport code via HMAC-SHA256.

    Mirrors MeshCore firmware ``TransportKey::calcTransportCode()``:
    HMAC-SHA256(key, payload_type(1) + payload) truncated to 2 bytes (LE).
    Values ``0x0000`` and ``0xFFFF`` are reserved and clamped to ``0x0001``
    / ``0xFFFE`` respectively.
    """
    import hmac

    msg = bytes([payload_type]) + payload
    mac = hmac.new(region_key, msg, hashlib.sha256).digest()[:2]
    code = int.from_bytes(mac, "little")
    if code == 0x0000:
        code = 0x0001
    elif code == 0xFFFF:
        code = 0xFFFE
    return code


# ---------------------------------------------------------------------------
# Wire-record builders (stateless — input is plain bytes / fields)
# ---------------------------------------------------------------------------


def build_contact_record(
    pubkey: bytes,
    name: str = "",
    *,
    node_type: int = 0x01,
    lat: int = 0,
    lon: int = 0,
    last_advert: int = 0,
) -> bytes:
    """Build a 147-byte contact record (canonical CONTACT response format).

    Layout: ``pubkey(32) + type(1) + flags(1) + path_len(1) + path(64) +
    name(32) + last_advert(4) + lat(4) + lon(4) + lastmod(4)``.
    ``path_len`` is signed; ``-1`` indicates an unknown path (flood-only).
    """
    import time as _time

    if last_advert == 0:
        last_advert = int(_time.time())
    buf = bytearray()
    buf.extend(pubkey[:32].ljust(32, b"\x00"))
    buf.append(node_type)
    buf.append(0)  # flags
    buf.extend(struct.pack("<b", -1))  # path_len: -1 (flood)
    buf.extend(b"\x00" * 64)  # path
    buf.extend(utf8_truncate(name, 32).ljust(32, b"\x00"))
    buf.extend(struct.pack("<I", last_advert))
    buf.extend(struct.pack("<i", lat))
    buf.extend(struct.pack("<i", lon))
    buf.extend(struct.pack("<I", last_advert))  # lastmod
    return bytes(buf)


def build_contact_msg_recv_v3(
    snr_byte: int,
    src_prefix: bytes,
    path_len: int,
    txt_type: int,
    ts: int,
    text: bytes,
) -> bytes:
    """Build a ``RESP_CONTACT_MSG_RECV_V3`` (0x10) response payload."""
    buf = bytearray()
    buf.append(RESP_CONTACT_MSG_RECV_V3)
    buf.extend(struct.pack("<b", snr_byte))  # SNR * 4
    buf.extend(b"\x00\x00")  # reserved
    buf.extend(src_prefix[:6].ljust(6, b"\x00"))
    buf.append(path_len)
    buf.append(txt_type)
    buf.extend(struct.pack("<I", ts))
    buf.extend(text)
    return bytes(buf)


def advert_to_push(payload: bytes, advert_start: int) -> bytes | None:
    """Convert an ADVERT payload to a ``PUSH_NEW_ADVERT`` (0x8A) frame.

    The push body uses the same layout as ``RESP_CONTACT``: 147-byte
    contact record after the response code.  Returns ``None`` when the
    ADVERT payload is too short.
    """
    if advert_start + 100 > len(payload):
        return None  # too short for pubkey + timestamp + signature

    pubkey = payload[advert_start : advert_start + 32]
    ts_bytes = payload[advert_start + 32 : advert_start + 36]
    app_start = advert_start + 100  # skip 64-byte signature
    app_data = payload[app_start:] if app_start < len(payload) else b""

    name = b""
    lat = 0
    lon = 0
    node_type = 0x01
    if app_data:
        flags = app_data[0]
        node_type = flags & 0x0F
        off = 1
        if flags & 0x10:
            if off + 8 <= len(app_data):
                lat, lon = struct.unpack_from("<ii", app_data, off)
            off += 8
        if flags & 0x20:
            off += 2
        if flags & 0x40:
            off += 2
        if flags & 0x80 and off < len(app_data):
            name = app_data[off:]

    last_advert = int.from_bytes(ts_bytes, "little") if ts_bytes else 0

    buf = bytearray()
    buf.append(PUSH_NEW_ADVERT)
    buf.extend(pubkey)
    buf.append(node_type)
    buf.append(0)  # flags
    buf.extend(struct.pack("<b", -1))  # out_path_len (flood)
    buf.extend(b"\x00" * 64)  # path
    buf.extend(name[:32].ljust(32, b"\x00"))
    buf.extend(struct.pack("<I", last_advert))
    buf.extend(struct.pack("<i", lat))
    buf.extend(struct.pack("<i", lon))
    buf.extend(struct.pack("<I", last_advert))  # lastmod
    return bytes(buf)


class AdvertInfo(TypedDict):
    """Parsed ADVERT wire-packet fields used by IMPORT/EXPORT/SHARE handlers."""

    pubkey: bytes
    name: str
    node_type: int
    lat: int
    lon: int
    last_advert: int


def parse_advert_wire_packet(data: bytes) -> AdvertInfo | None:
    """Parse a raw MeshCore ADVERT wire packet, return contact info or ``None``.

    Wire format: ``[header(1)][opt transport(4)][path_len(1)][path(N)] +
    [pubkey(32)][timestamp(4)][signature(64)][app_data(N)]``.
    """
    if len(data) < 2:
        return None
    hdr = data[0]
    ptype = (hdr >> PTYPE_SHIFT) & PTYPE_MASK
    route = hdr & ROUTE_MASK
    if ptype != PAYLOAD_ADVERT:
        return None
    has_transport = route in (0, 3)
    off = 1
    if has_transport:
        off += 4
    if off >= len(data):
        return None
    path_len = data[off]
    _, _, path_byte_len = decode_path_len(path_len)
    off += 1 + path_byte_len
    advert_start = off
    if advert_start + 100 > len(data):
        return None
    pubkey = data[advert_start : advert_start + 32]
    ts_bytes = data[advert_start + 32 : advert_start + 36]
    app_start = advert_start + 100
    app_data = data[app_start:] if app_start < len(data) else b""

    name_b = b""
    lat = 0
    lon = 0
    node_type = 0x01
    if app_data:
        flags = app_data[0]
        node_type = flags & 0x0F
        aoff = 1
        if flags & 0x10:
            if aoff + 8 <= len(app_data):
                lat, lon = struct.unpack_from("<ii", app_data, aoff)
            aoff += 8
        if flags & 0x20:
            aoff += 2
        if flags & 0x40:
            aoff += 2
        if flags & 0x80 and aoff < len(app_data):
            name_b = app_data[aoff:]
    last_advert = int.from_bytes(ts_bytes, "little") if ts_bytes else 0
    return {
        "pubkey": bytes(pubkey),
        "name": name_b.decode("utf-8", errors="replace").rstrip("\x00"),
        "node_type": node_type,
        "lat": lat,
        "lon": lon,
        "last_advert": last_advert,
    }


def build_unsigned_advert_from_record(record: bytes) -> bytes:
    """Reconstruct an unsigned ADVERT wire packet from a 147-byte contact record.

    Used for ``CMD_EXPORT_CONTACT``: the original 64-byte signature was
    not retained, so we emit a wire packet with the signature zeroed.
    """
    if len(record) != CONTACT_RECORD_SIZE:
        raise ValueError(
            f"contact record must be {CONTACT_RECORD_SIZE} bytes, got {len(record)}"
        )
    pubkey = record[0:32]
    node_type = record[32]
    name_raw = record[99:131].rstrip(b"\x00")
    last_adv = struct.unpack_from("<I", record, 131)[0]
    lat_e6 = struct.unpack_from("<i", record, 135)[0]
    lon_e6 = struct.unpack_from("<i", record, 139)[0]
    flags = node_type & 0x0F
    app_data_body = bytearray()
    if lat_e6 != 0 or lon_e6 != 0:
        flags |= ADVERT_HAS_LOCATION
        app_data_body.extend(struct.pack("<ii", lat_e6, lon_e6))
    if name_raw:
        flags |= ADVERT_HAS_NAME
        app_data_body.extend(name_raw)
    app_data = bytes([flags]) + bytes(app_data_body)
    advert_payload = pubkey + struct.pack("<I", last_adv) + b"\x00" * 64 + app_data
    return build_wire_packet(make_header(ROUTE_FLOOD, PAYLOAD_ADVERT), advert_payload)


# ---------------------------------------------------------------------------
# ACK / PATH wire packet builders
# ---------------------------------------------------------------------------


def build_ack_wire_packet(
    ack_hash: bytes,
    *,
    stored_path: bytes,
    stored_path_len: int,
    send_scope: bytes,
    region_key: bytes,
) -> bytes:
    """Build a bare MeshCore ACK wire packet (firmware ``sendAckTo()``).

    * ``stored_path_len >= 0`` → ``ROUTE_DIRECT`` ACK with stored path.
    * ``stored_path_len == -1`` → flood ACK (with optional T_FLOOD scope).

    ``send_scope`` is the per-session 16-byte transport key
    (``CMD_SET_FLOOD_SCOPE``); ``region_key`` is the long-lived 16-byte
    key derived from a hashtag region — first non-zero key wins, matching
    firmware ``sendFloodScoped()``.
    """
    if stored_path_len >= 0:
        header = make_header(ROUTE_DIRECT, PAYLOAD_ACK)
        return build_wire_packet(header, ack_hash, path=stored_path)
    active_key = send_scope if any(send_scope) else region_key
    if any(active_key):
        tc1 = compute_transport_code(active_key, PAYLOAD_ACK, ack_hash)
        header = make_header(ROUTE_T_FLOOD, PAYLOAD_ACK)
        return build_wire_packet(header, ack_hash, transport_codes=(tc1, 0))
    return build_wire_packet(make_header(ROUTE_FLOOD, PAYLOAD_ACK), ack_hash)


def build_path_ack_wire_packet(
    ack_hash: bytes,
    *,
    sender_pub: bytes,
    incoming_path_len_byte: int,
    incoming_path_bytes: bytes,
    our_expanded_prv: bytes,
    our_pub: bytes,
    send_scope: bytes,
    region_key: bytes,
) -> bytes | None:
    """Build a PATH+ACK wire packet (firmware ``createPathReturn()``).

    Returns ``None`` if ECDH shared-secret derivation fails — caller
    should fall back to :func:`build_ack_wire_packet`.
    """
    plaintext = (
        bytes([incoming_path_len_byte])
        + incoming_path_bytes
        + bytes([PAYLOAD_ACK])
        + ack_hash
    )
    secret = meshcore_shared_secret(our_expanded_prv, sender_pub)
    if secret is None:
        return None
    enc = meshcore_encrypt_then_mac(secret, plaintext)
    path_payload = bytes([sender_pub[0], our_pub[0]]) + enc
    active_key = send_scope if any(send_scope) else region_key
    if any(active_key):
        tc1 = compute_transport_code(active_key, PAYLOAD_PATH, path_payload)
        header = make_header(ROUTE_T_FLOOD, PAYLOAD_PATH)
        return build_wire_packet(header, path_payload, transport_codes=(tc1, 0))
    return build_wire_packet(make_header(ROUTE_FLOOD, PAYLOAD_PATH), path_payload)


# ---------------------------------------------------------------------------
# Channel info builder (uses the public GroupChannel record)
# ---------------------------------------------------------------------------


def build_channel_info(idx: int, channel: GroupChannel | None) -> bytes:
    """Build a ``RESP_CHANNEL_INFO`` (0x12) frame for slot ``idx``.

    ``channel`` may be ``None`` to emit an empty record (firmware
    convention: name + secret are zero-padded blanks).
    """
    buf = bytearray()
    buf.append(RESP_CHANNEL_INFO)
    buf.append(idx)
    if channel is not None:
        buf.extend(utf8_truncate(channel.name, 32).ljust(32, b"\x00"))
        buf.extend(channel.secret[:16])
    else:
        buf.extend(b"\x00" * 32)
        buf.extend(b"\x00" * 16)
    return bytes(buf)


__all__ = [
    "ADVERT_HAS_LOCATION",
    "ADVERT_HAS_NAME",
    "BRIDGE_PORT",
    "CMD_ADD_UPDATE_CONTACT",
    "CMD_APP_START",
    "CMD_BINARY_REQ",
    "CMD_DEVICE_QUERY",
    "CMD_EXPORT_CONTACT",
    "CMD_EXPORT_PRIVATE_KEY",
    "CMD_FACTORY_RESET",
    "CMD_GET_ADVERT_PATH",
    "CMD_GET_ALLOWED_REPEAT_FREQ",
    "CMD_GET_AUTOADD_CONFIG",
    "CMD_GET_BATT_AND_STORAGE",
    "CMD_GET_CHANNEL",
    "CMD_GET_CONTACT_BY_KEY",
    "CMD_GET_CONTACTS",
    "CMD_GET_CUSTOM_VARS",
    "CMD_GET_DEFAULT_FLOOD_SCOPE",
    "CMD_GET_DEVICE_TIME",
    "CMD_GET_STATS",
    "CMD_GET_TUNING_PARAMS",
    "CMD_HAS_CONNECTION",
    "CMD_IMPORT_CONTACT",
    "CMD_IMPORT_PRIVATE_KEY",
    "CMD_LOGIN",
    "CMD_LOGOUT",
    "CMD_PATH_DISCOVERY",
    "CMD_REBOOT",
    "CMD_REMOVE_CONTACT",
    "CMD_RESET_PATH",
    "CMD_SEND_ANON_REQ",
    "CMD_SEND_CHAN_TXT_MSG",
    "CMD_SEND_CONTROL_DATA",
    "CMD_SEND_SELF_ADVERT",
    "CMD_SEND_TELEMETRY_REQ",
    "CMD_SEND_TXT_MSG",
    "CMD_SET_ADVERT_LATLON",
    "CMD_SET_ADVERT_NAME",
    "CMD_SET_AUTOADD_CONFIG",
    "CMD_SET_CHANNEL",
    "CMD_SET_CUSTOM_VAR",
    "CMD_SET_DEFAULT_FLOOD_SCOPE",
    "CMD_SET_DEVICE_PIN",
    "CMD_SET_DEVICE_TIME",
    "CMD_SET_FLOOD_SCOPE",
    "CMD_SET_OTHER_PARAMS",
    "CMD_SET_PATH_HASH_MODE",
    "CMD_SET_RADIO_PARAMS",
    "CMD_SET_RADIO_TX_POWER",
    "CMD_SET_TUNING_PARAMS",
    "CMD_SHARE_CONTACT",
    "CMD_STATUS_REQ",
    "CMD_SYNC_NEXT_MESSAGE",
    "CMD_TRACE",
    "CONTACT_RECORD_SIZE",
    "ERR_CODE_ILLEGAL_ARG",
    "ERR_CODE_NOT_FOUND",
    "FRAME_RX",
    "FRAME_TX",
    "FrameDecoder",
    "GroupChannel",
    "MAX_FRAME",
    "PUSH_ACK",
    "PUSH_ADVERTISEMENT",
    "PUSH_BINARY_RESPONSE",
    "PUSH_CONTROL_DATA",
    "PUSH_LOGIN_FAIL",
    "PUSH_LOGIN_SUCCESS",
    "PUSH_MESSAGES_WAITING",
    "PUSH_NEW_ADVERT",
    "PUSH_PATH_DISCOVERY_RESPONSE",
    "PUSH_PATH_UPDATE",
    "PUSH_STATUS_RESPONSE",
    "PUSH_TRACE_DATA",
    "REPEAT_FREQ_RANGES",
    "RESP_ADVERT_PATH",
    "RESP_ALLOWED_REPEAT_FREQ",
    "RESP_BATTERY",
    "RESP_CHANNEL_INFO",
    "RESP_CHANNEL_MSG_RECV",
    "RESP_CHANNEL_MSG_RECV_V3",
    "RESP_CODE_AUTOADD_CONFIG",
    "RESP_CONTACT",
    "RESP_CONTACT_END",
    "RESP_CONTACT_MSG_RECV",
    "RESP_CONTACT_MSG_RECV_V3",
    "RESP_CONTACT_START",
    "RESP_CONTACT_URI",
    "RESP_CURRENT_TIME",
    "RESP_CUSTOM_VARS",
    "RESP_DEFAULT_FLOOD_SCOPE",
    "RESP_DEVICE_INFO",
    "RESP_ERROR",
    "RESP_MSG_SENT",
    "RESP_NO_MORE_MSGS",
    "RESP_OK",
    "RESP_SELF_INFO",
    "RESP_STATS",
    "RESP_TUNING_PARAMS",
    "advert_to_push",
    "build_ack_wire_packet",
    "build_channel_info",
    "build_contact_msg_recv_v3",
    "build_contact_record",
    "build_path_ack_wire_packet",
    "build_unsigned_advert_from_record",
    "compute_transport_code",
    "frame_encode",
    "hash_payload",
    "is_valid_repeat_freq",
    "parse_advert_wire_packet",
    "parse_flood_scope",
]
