#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""MeshCore companion-protocol command and event dispatchers.

Phase 4 split from ``meshcore_bridge.py``: every command
``CMD_*`` and every received-frame handler lives here.  The module is
deliberately the largest of the new ``lora.bridges.meshcore`` package —
each handler is a thin function that builds wire packets via
:mod:`lora.bridges.meshcore.protocol` or mutates state through the
:class:`lora.bridges.meshcore.state.BridgeState` accessor.

If this module exceeds ~2 000 lines, split into ``companion_msg`` and
``companion_admin`` modules per the Phase 4 plan.
"""

from __future__ import annotations

import logging
import shutil
import socket
import struct
import time
from typing import Any

from lora.bridges.meshcore.protocol import (
    CMD_ADD_UPDATE_CONTACT,
    CMD_APP_START,
    CMD_BINARY_REQ,
    CMD_DEVICE_QUERY,
    CMD_EXPORT_CONTACT,
    CMD_EXPORT_PRIVATE_KEY,
    CMD_FACTORY_RESET,
    CMD_GET_ADVERT_PATH,
    CMD_GET_ALLOWED_REPEAT_FREQ,
    CMD_GET_AUTOADD_CONFIG,
    CMD_GET_BATT_AND_STORAGE,
    CMD_GET_CHANNEL,
    CMD_GET_CONTACT_BY_KEY,
    CMD_GET_CONTACTS,
    CMD_GET_CUSTOM_VARS,
    CMD_GET_DEFAULT_FLOOD_SCOPE,
    CMD_GET_DEVICE_TIME,
    CMD_GET_STATS,
    CMD_GET_TUNING_PARAMS,
    CMD_HAS_CONNECTION,
    CMD_IMPORT_CONTACT,
    CMD_IMPORT_PRIVATE_KEY,
    CMD_LOGIN,
    CMD_LOGOUT,
    CMD_PATH_DISCOVERY,
    CMD_REBOOT,
    CMD_REMOVE_CONTACT,
    CMD_RESET_PATH,
    CMD_SEND_ANON_REQ,
    CMD_SEND_CHAN_TXT_MSG,
    CMD_SEND_CONTROL_DATA,
    CMD_SEND_SELF_ADVERT,
    CMD_SEND_TELEMETRY_REQ,
    CMD_SEND_TXT_MSG,
    CMD_SET_ADVERT_LATLON,
    CMD_SET_ADVERT_NAME,
    CMD_SET_AUTOADD_CONFIG,
    CMD_SET_CHANNEL,
    CMD_SET_CUSTOM_VAR,
    CMD_SET_DEFAULT_FLOOD_SCOPE,
    CMD_SET_DEVICE_PIN,
    CMD_SET_DEVICE_TIME,
    CMD_SET_FLOOD_SCOPE,
    CMD_SET_OTHER_PARAMS,
    CMD_SET_PATH_HASH_MODE,
    CMD_SET_RADIO_PARAMS,
    CMD_SET_RADIO_TX_POWER,
    CMD_SET_TUNING_PARAMS,
    CMD_SHARE_CONTACT,
    CMD_STATUS_REQ,
    CMD_SYNC_NEXT_MESSAGE,
    CMD_TRACE,
    CONTACT_RECORD_SIZE,
    ERR_CODE_ILLEGAL_ARG,
    ERR_CODE_NOT_FOUND,
    PUSH_ACK,
    PUSH_BINARY_RESPONSE,
    PUSH_CONTROL_DATA,
    PUSH_LOGIN_FAIL,
    PUSH_LOGIN_SUCCESS,
    PUSH_NEW_ADVERT,
    PUSH_PATH_UPDATE,
    PUSH_STATUS_RESPONSE,
    RESP_ADVERT_PATH,
    RESP_CODE_AUTOADD_CONFIG,
    RESP_CONTACT,
    RESP_CONTACT_URI,
    RESP_CUSTOM_VARS,
    RESP_DEFAULT_FLOOD_SCOPE,
    RESP_OK,
    RESP_TUNING_PARAMS,
    advert_to_push,
    build_ack_wire_packet,
    build_contact_msg_recv_v3,
    build_contact_record,
    build_path_ack_wire_packet,
    build_unsigned_advert_from_record,
    hash_payload,
    is_valid_repeat_freq,
    parse_advert_wire_packet,
)
from lora.bridges.meshcore.state import BridgeState
from lora.core.formatters import sanitize_text
from lora.core.meshcore_crypto import (
    PAYLOAD_ACK,
    PAYLOAD_ADVERT,
    PAYLOAD_ANON_REQ,
    PAYLOAD_CTRL,
    PAYLOAD_GRP_TXT,
    PAYLOAD_PATH,
    PAYLOAD_RESP,
    PAYLOAD_TRACE,
    PAYLOAD_TXT,
    PTYPE_MASK,
    PTYPE_SHIFT,
    ROUTE_DIRECT,
    ROUTE_FLOOD,
    ROUTE_MASK,
    ROUTE_T_FLOOD,
    build_grp_txt,
    compute_ack_hash,
    decode_path_len,
    extract_advert_name,
    extract_advert_pubkey,
    meshcore_encrypt_then_mac,
    meshcore_expanded_key,
    meshcore_mac_then_decrypt,
    meshcore_shared_secret,
    parse_meshcore_header,
    save_channel,
    try_decrypt_anon_req,
    try_decrypt_grp_txt,
    try_decrypt_txt_msg,
)
from lora.tools.meshcore_tx import (
    build_advert_raw,
    build_anon_req_raw,
    build_txt_msg_raw,
    build_wire_packet,
    make_cbor_tx_request,
    make_header,
)

log = logging.getLogger("lora.bridges.meshcore.companion")


# ---------------------------------------------------------------------------
# RX dispatch — converts a CBOR ``lora_frame`` to companion messages
# ---------------------------------------------------------------------------


def lora_frame_to_companion_msgs(
    frame: dict[str, Any],
    state: BridgeState,
) -> tuple[list[bytes], list[bytes]]:
    """Convert a CBOR ``lora_frame`` into companion-protocol messages.

    Returns ``(companion_msgs, ack_packets)``:

    * ``companion_msgs`` — raw response payloads queued for the TCP client.
    * ``ack_packets`` — MeshCore wire packets to TX as RF ACKs.
    """
    empty: tuple[list[bytes], list[bytes]] = ([], [])

    payload = frame.get("payload", b"")
    if not payload or len(payload) < 2:
        return empty
    if not frame.get("crc_valid", False):
        return empty

    carrier = frame.get("carrier", {})
    phy = frame.get("phy", {})
    if carrier.get("sync_word") != 0x12:
        return empty

    hdr = payload[0]
    ptype = (hdr >> PTYPE_SHIFT) & PTYPE_MASK
    route = hdr & ROUTE_MASK

    has_transport = route in (0, 3)
    off = 1
    if has_transport:
        off += 4
    if off >= len(payload):
        return empty
    path_len = payload[off]
    _, _, path_byte_len = decode_path_len(path_len)
    advert_start = off + 1 + path_byte_len

    snr_db = phy.get("snr_db", 0.0)
    snr_byte = max(-128, min(127, int(snr_db * 4)))
    state.last_snr_db = snr_db
    rssi = phy.get("rssi_dbm")
    if rssi is not None:
        state.last_rssi_dbm = max(-128, min(127, int(rssi)))

    state.pkt_recv += 1
    if route in (ROUTE_FLOOD, ROUTE_T_FLOOD):
        state.pkt_flood_rx += 1
    else:
        state.pkt_direct_rx += 1

    if ptype == PAYLOAD_ADVERT:
        return _handle_advert_rx(payload, advert_start, state), []
    if ptype == PAYLOAD_ACK:
        return _handle_ack_rx(payload, advert_start, state), []
    if ptype == PAYLOAD_TXT:
        path_start = off + 1
        return _handle_txt_msg_rx(
            payload,
            snr_byte,
            path_len,
            state,
            route=route,
            path_len_byte=payload[off] if off < len(payload) else 0,
            path_bytes=payload[path_start : path_start + path_byte_len],
        )
    if ptype == PAYLOAD_ANON_REQ:
        return _handle_anon_req_rx(payload, snr_byte, path_len, state, route=route)
    if ptype == PAYLOAD_GRP_TXT:
        return _handle_grp_txt_rx(payload, snr_byte, path_len, state), []
    if ptype == PAYLOAD_PATH:
        return _handle_path_rx(payload, path_len, state), []
    if ptype == PAYLOAD_CTRL:
        return _handle_ctrl_rx(payload, path_len, state), []
    if ptype == PAYLOAD_RESP:
        return _handle_resp_rx(payload, snr_byte, path_len, state), []
    return empty


# ---------------------------------------------------------------------------
# Per-payload-type RX handlers
# ---------------------------------------------------------------------------


def _handle_resp_rx(
    payload: bytes,
    snr_byte: int,
    path_len: int,
    state: BridgeState,
) -> list[bytes]:
    """Decrypt a RESP packet and produce login / status push events."""
    del snr_byte, path_len  # not used in push frames
    hdr_info = parse_meshcore_header(payload)
    if hdr_info is None:
        return []
    off = hdr_info["off"]
    inner = payload[off:]
    if len(inner) < 4:
        return []
    dest_hash = inner[0]
    src_hash = inner[1]
    encrypted = inner[2:]
    if dest_hash != state.pub_key[0]:
        return []

    candidates = [pub for pub in state.known_keys.values() if pub[0] == src_hash]
    plaintext: bytes | None = None
    sender_pub: bytes | None = None
    for peer_pub in candidates:
        secret = meshcore_shared_secret(state.expanded_prv, peer_pub)
        if secret is None:
            continue
        pt = meshcore_mac_then_decrypt(secret, encrypted)
        if pt is not None:
            plaintext = pt
            sender_pub = peer_pub
            break
    if plaintext is None or sender_pub is None or len(plaintext) < 4:
        log.debug("RESP: decryption failed or plaintext too short")
        return []
    tag = plaintext[:4]
    response_data = plaintext[4:]
    sender_prefix = sender_pub[:6]

    if len(response_data) >= 1 and response_data[0] == 0x00:
        permissions = response_data[2] if len(response_data) > 2 else 0
        acl_perms = response_data[3] if len(response_data) > 3 else 0
        fw_ver_level = response_data[8] if len(response_data) > 8 else 0
        push = (
            bytes([PUSH_LOGIN_SUCCESS])
            + bytes([permissions])
            + sender_prefix
            + tag
            + bytes([acl_perms])
            + bytes([fw_ver_level])
        )
        log.info("RESP: LOGIN_SUCCESS from %s..", sender_pub.hex()[:8])
        return [push]

    if len(response_data) >= 2 and response_data[:2] == b"OK":
        push = bytes([PUSH_LOGIN_SUCCESS, 0x00]) + sender_prefix
        log.info("RESP: LOGIN_SUCCESS (legacy OK) from %s..", sender_pub.hex()[:8])
        return [push]

    if state._pending_status and sender_pub[:4] == state._pending_status:
        push = bytes([PUSH_STATUS_RESPONSE, 0x00]) + sender_prefix + response_data
        state._pending_status = b""
        log.info("RESP: STATUS_RESPONSE from %s..", sender_pub.hex()[:8])
        return [push]

    push = bytes([PUSH_LOGIN_FAIL, 0x00]) + sender_prefix
    log.info("RESP: LOGIN_FAIL from %s..", sender_pub.hex()[:8])
    return [push]


def _handle_ctrl_rx(
    payload: bytes,
    path_len: int,
    state: BridgeState,
) -> list[bytes]:
    """Push a received CTRL packet as ``PUSH_CONTROL_DATA`` (0x8E)."""
    hdr_info = parse_meshcore_header(payload)
    if hdr_info is None:
        return []
    off = hdr_info["off"]
    ctrl_payload = payload[off:]
    snr_byte = max(-128, min(127, int(state.last_snr_db * 4)))
    rssi_byte = max(-128, min(127, state.last_rssi_dbm))
    push = (
        bytes([PUSH_CONTROL_DATA])
        + struct.pack("<b", snr_byte)
        + struct.pack("<b", rssi_byte)
        + bytes([path_len])
        + ctrl_payload
    )
    return [push]


def _handle_advert_rx(
    payload: bytes,
    advert_start: int,
    state: BridgeState,
) -> list[bytes]:
    """Handle a received ADVERT: push, auto-learn key, auto-add contact."""
    push = advert_to_push(payload, advert_start)
    if push is None:
        return []
    pubkey = extract_advert_pubkey(payload)
    if pubkey is not None and pubkey != state.pub_key:
        state.learn_pubkey(pubkey)
        pk_hex = pubkey.hex()
        if pk_hex not in state.contacts:
            record = push[1:]
            if len(record) == CONTACT_RECORD_SIZE:
                state.contacts[pk_hex] = record
                state.contacts_lastmod = int(time.time())
                _persist_contact(state, pk_hex, record)
                name = extract_advert_name(payload) or ""
                log.info("contact learned: %s.. '%s'", pk_hex[:8], sanitize_text(name))
    return [push]


def _match_pending_ack(ack_bytes: bytes, state: BridgeState) -> list[bytes]:
    """Match a 4-byte ACK hash against pending_acks → ``PUSH_ACK`` if found."""
    now = time.monotonic()
    expired = [
        k
        for k, (ts, _) in state.pending_acks.items()
        if now - ts > state.pending_ack_ttl
    ]
    for k in expired:
        del state.pending_acks[k]
    if ack_bytes in state.pending_acks:
        _, _dest_prefix = state.pending_acks.pop(ack_bytes)
        trip_time = struct.pack("<I", 0)
        push = bytes([PUSH_ACK]) + ack_bytes[:4] + trip_time
        log.info("ACK received for %s", ack_bytes.hex())
        return [push]
    return []


def _handle_ack_rx(
    payload: bytes,
    ack_start: int,
    state: BridgeState,
) -> list[bytes]:
    """Handle received bare ACK (ptype 0x03)."""
    if ack_start + 4 > len(payload):
        return []
    ack_bytes = payload[ack_start : ack_start + 4]
    return _match_pending_ack(ack_bytes, state)


def _update_contact_path(
    state: BridgeState, sender_pub: bytes, return_path: bytes
) -> bytes | None:
    """Update a contact record with a learned forward path → ``PUSH_PATH_UPDATE``."""
    pk_hex = sender_pub.hex()
    record = state.contacts.get(pk_hex)
    if record is None:
        return None
    forward_path = bytes(reversed(return_path))
    n = len(forward_path)
    if n > 64:
        forward_path = forward_path[:64]
        n = 64
    buf = bytearray(record)
    struct.pack_into("<b", buf, 34, n)
    buf[35 : 35 + n] = forward_path
    if n < 64:
        buf[35 + n : 99] = b"\x00" * (64 - n)
    state.contacts[pk_hex] = bytes(buf)
    _persist_contact(state, pk_hex, bytes(buf))
    log.info(
        "path learned for %s..: %d hops → %s",
        pk_hex[:8],
        n,
        forward_path.hex(),
    )
    return bytes([PUSH_PATH_UPDATE]) + sender_pub[:6] + bytes([n]) + forward_path


def _handle_path_rx(payload: bytes, path_len: int, state: BridgeState) -> list[bytes]:
    """Decrypt a PATH packet, learn return path, extract bundled ACK."""
    del path_len  # path_len_byte is parsed via header info
    hdr_info = parse_meshcore_header(payload)
    if hdr_info is None:
        return []
    off = hdr_info["off"]
    inner = payload[off:]
    if len(inner) < 4:
        return []
    dest_hash = inner[0]
    src_hash = inner[1]
    encrypted = inner[2:]
    if dest_hash != state.pub_key[0]:
        return []

    candidates = [pub for pub in state.known_keys.values() if pub[0] == src_hash]
    plaintext: bytes | None = None
    sender_pub: bytes | None = None
    for peer_pub in candidates:
        secret = meshcore_shared_secret(state.expanded_prv, peer_pub)
        if secret is None:
            continue
        pt = meshcore_mac_then_decrypt(secret, encrypted)
        if pt is not None:
            plaintext = pt
            sender_pub = peer_pub
            break
    if plaintext is None or len(plaintext) < 2:
        log.debug("PATH: decryption failed (not for us or unknown sender)")
        return []
    path_len_enc = plaintext[0]
    hash_count = path_len_enc & 0x3F
    hash_size = ((path_len_enc >> 6) & 0x03) + 1
    path_bytes = hash_count * hash_size
    idx = 1 + path_bytes
    return_path = plaintext[1 : 1 + path_bytes]
    results: list[bytes] = []
    if path_bytes > 0 and sender_pub is not None:
        path_push = _update_contact_path(state, sender_pub, return_path)
        if path_push is not None:
            results.append(path_push)
    if idx >= len(plaintext):
        return results
    extra_type = plaintext[idx] & 0x0F
    extra = plaintext[idx + 1 :]
    if extra_type == PAYLOAD_ACK and len(extra) >= 4:
        ack_bytes = extra[:4]
        log.info("PATH+ACK: extracted ACK hash %s", ack_bytes.hex())
        results.extend(_match_pending_ack(ack_bytes, state))
    return results


def _handle_txt_msg_rx(
    payload: bytes,
    snr_byte: int,
    path_len: int,
    state: BridgeState,
    *,
    route: int = ROUTE_DIRECT,
    path_len_byte: int = 0,
    path_bytes: bytes = b"",
) -> tuple[list[bytes], list[bytes]]:
    """Decrypt TXT_MSG → ``CONTACT_MSG_RECV_V3`` + RF ACK packet."""
    empty: tuple[list[bytes], list[bytes]] = ([], [])
    result = try_decrypt_txt_msg(
        payload, state.expanded_prv, state.pub_key, state.known_keys
    )
    if result is None:
        log.debug("TXT_MSG: decryption failed")
        return empty
    text, sender_pub = result
    log.info(">> TXT_MSG from %s..: %s", sender_pub.hex()[:8], sanitize_text(text))

    state.learn_pubkey(sender_pub)

    hdr_info = parse_meshcore_header(payload)
    if hdr_info is None:
        return empty
    off = hdr_info["off"]
    inner = payload[off:]
    secret = meshcore_shared_secret(state.expanded_prv, sender_pub)
    plaintext: bytes | None = None
    if secret is not None:
        plaintext = meshcore_mac_then_decrypt(secret, inner[2:])
    if plaintext is not None and len(plaintext) >= 5:
        ts = struct.unpack_from("<I", plaintext, 0)[0]
        txt_type = plaintext[4] >> 2
    else:
        ts = int(time.time())
        txt_type = 0

    msg = build_contact_msg_recv_v3(
        snr_byte,
        sender_pub[:6],
        path_len,
        txt_type,
        ts,
        text.encode("utf-8"),
    )

    ack_packets: list[bytes] = []
    if plaintext is not None and len(plaintext) >= 5:
        ack_hash = compute_ack_hash(plaintext, sender_pub)
        is_flood = route in (ROUTE_FLOOD, ROUTE_T_FLOOD)
        if is_flood:
            path_ack = build_path_ack_wire_packet(
                ack_hash,
                sender_pub=sender_pub,
                incoming_path_len_byte=path_len_byte,
                incoming_path_bytes=path_bytes,
                our_expanded_prv=state.expanded_prv,
                our_pub=state.pub_key,
                send_scope=state.send_scope,
                region_key=state.region_key,
            )
            if path_ack is not None:
                ack_pkt = path_ack
            else:
                ack_pkt = _build_ack_for_contact(state, ack_hash, sender_pub)
            log.info(
                "PATH+ACK hash: %s for %s.. (%d hops)",
                ack_hash.hex(),
                sender_pub.hex()[:8],
                path_len_byte & 0x3F,
            )
        else:
            ack_pkt = _build_ack_for_contact(state, ack_hash, sender_pub)
            label = "direct" if (ack_pkt[0] & ROUTE_MASK) == ROUTE_DIRECT else "flood"
            log.info(
                "ACK hash: %s for %s.. (%s)",
                ack_hash.hex(),
                sender_pub.hex()[:8],
                label,
            )
        ack_packets.append(ack_pkt)

    companion_msgs: list[bytes] = [msg]

    if txt_type == 1 and plaintext is not None and len(plaintext) >= 5:
        tag = compute_ack_hash(plaintext, sender_pub)
        data_bytes = plaintext[5:].rstrip(b"\x00")
        binary_resp = bytes([PUSH_BINARY_RESPONSE]) + tag[:4] + data_bytes
        companion_msgs.append(binary_resp)
        log.info(
            "BINARY_RESPONSE push: tag=%s from %s..",
            tag.hex(),
            sender_pub.hex()[:8],
        )

    return (companion_msgs, ack_packets)


def _build_ack_for_contact(
    state: BridgeState, ack_hash: bytes, sender_pub: bytes
) -> bytes:
    """Look up the contact's stored path and build the ACK accordingly."""
    stored_path = b""
    stored_path_len = -1
    contact_hex = sender_pub.hex() if sender_pub else ""
    if contact_hex and contact_hex in state.contacts:
        stored_path_len = struct.unpack_from("<b", state.contacts[contact_hex], 34)[0]
        if stored_path_len > 0:
            stored_path = state.contacts[contact_hex][35 : 35 + stored_path_len]
    return build_ack_wire_packet(
        ack_hash,
        stored_path=stored_path,
        stored_path_len=stored_path_len,
        send_scope=state.send_scope,
        region_key=state.region_key,
    )


def _handle_anon_req_rx(
    payload: bytes,
    snr_byte: int,
    path_len: int,
    state: BridgeState,
    *,
    route: int = ROUTE_DIRECT,
) -> tuple[list[bytes], list[bytes]]:
    """Decrypt ANON_REQ + emit encrypted RESP back to sender."""
    empty: tuple[list[bytes], list[bytes]] = ([], [])
    result = try_decrypt_anon_req(payload, state.expanded_prv, state.pub_key)
    if result is None:
        log.debug("ANON_REQ: decryption failed")
        return empty
    text, sender_pub = result
    log.info(">> ANON_REQ from %s..: %s", sender_pub.hex()[:8], sanitize_text(text))
    if state.learn_pubkey(sender_pub):
        log.info("key learned: %s.. (ANON_REQ)", sender_pub.hex()[:8])
    ts = int(time.time())
    msg = build_contact_msg_recv_v3(
        snr_byte, sender_pub[:6], path_len, 0, ts, text.encode("utf-8")
    )
    shared = meshcore_shared_secret(state.expanded_prv, sender_pub)
    if shared is None:
        log.warning("ANON_REQ: ECDH failed, skipping RESPONSE")
        return ([msg], [])
    resp_plaintext = bytes([0x00]) + state.name.encode("utf-8")[:32]
    resp_enc = meshcore_encrypt_then_mac(shared, resp_plaintext)
    resp_payload = bytes([sender_pub[0], state.pub_key[0]]) + resp_enc
    is_flood = route in (ROUTE_FLOOD, ROUTE_T_FLOOD)
    if is_flood:
        active_key = state.send_scope if any(state.send_scope) else state.region_key
        if any(active_key):
            from lora.bridges.meshcore.protocol import compute_transport_code

            tc1 = compute_transport_code(active_key, PAYLOAD_RESP, resp_payload)
            hdr = make_header(ROUTE_T_FLOOD, PAYLOAD_RESP)
            resp_pkt = build_wire_packet(hdr, resp_payload, transport_codes=(tc1, 0))
        else:
            resp_pkt = build_wire_packet(
                make_header(ROUTE_FLOOD, PAYLOAD_RESP), resp_payload
            )
    else:
        resp_pkt = build_wire_packet(
            make_header(ROUTE_DIRECT, PAYLOAD_RESP), resp_payload
        )
    log.debug("ANON_REQ: sending RESPONSE to %s..", sender_pub.hex()[:8])
    return ([msg], [resp_pkt])


def _handle_grp_txt_rx(
    payload: bytes,
    snr_byte: int,
    path_len: int,
    state: BridgeState,
) -> list[bytes]:
    """Decrypt GRP_TXT → ``RESP_CHANNEL_MSG_RECV_V3`` (no ACK)."""
    from lora.bridges.meshcore.protocol import RESP_CHANNEL_MSG_RECV_V3

    result = try_decrypt_grp_txt(payload, state.channels)
    if result is None:
        log.debug("GRP_TXT: decryption failed")
        return []
    text, channel_name = result
    log.info(">> GRP_TXT #%s: %s", sanitize_text(channel_name), sanitize_text(text))

    chan_idx = 0
    for i, ch in enumerate(state.channels):
        if ch.name == channel_name:
            chan_idx = i
            break

    hdr_info = parse_meshcore_header(payload)
    ts = int(time.time())
    if hdr_info is not None:
        off = hdr_info["off"]
        inner = payload[off:]
        ch_obj = state.channels[chan_idx] if chan_idx < len(state.channels) else None
        if ch_obj is not None:
            plaintext = meshcore_mac_then_decrypt(ch_obj.secret, inner[1:])
            if plaintext is not None and len(plaintext) >= 4:
                ts = struct.unpack_from("<I", plaintext, 0)[0]

    buf = bytearray()
    buf.append(RESP_CHANNEL_MSG_RECV_V3)
    buf.extend(struct.pack("<b", snr_byte))
    buf.extend(b"\x00\x00")
    buf.append(chan_idx)
    buf.append(path_len)
    buf.append(0)
    buf.extend(struct.pack("<I", ts))
    buf.extend(text.encode("utf-8"))
    return [bytes(buf)]


# ---------------------------------------------------------------------------
# Contact persistence (atomic per-record write — bridge-local)
# ---------------------------------------------------------------------------


def _persist_contact(state: BridgeState, pk_hex: str, record: bytes) -> None:
    from lora.bridges.meshcore.state import persist_contacts

    persist_contacts(state.contacts_dir, {pk_hex: record})


def _delete_persisted_contact(state: BridgeState, pk_hex: str) -> None:
    from lora.bridges.meshcore.state import delete_persisted_contact

    delete_persisted_contact(state.contacts_dir, pk_hex)


# ---------------------------------------------------------------------------
# Helper: resolve / ensure contacts
# ---------------------------------------------------------------------------


def _ensure_self_contact(state: BridgeState) -> None:
    pk_hex = state.pub_key.hex()
    if pk_hex not in state.contacts:
        record = build_contact_record(state.pub_key, name=state.name)
        state.contacts[pk_hex] = record
        state.contacts_lastmod = int(time.time())


def _resolve_contact(state: BridgeState, prefix: bytes) -> tuple[bytes, bytes] | None:
    """Resolve a pubkey prefix to ``(pubkey, 147-byte record)`` or ``None``."""
    for pk_hex, record in state.contacts.items():
        pk_bytes = bytes.fromhex(pk_hex)
        if pk_bytes[: len(prefix)] == prefix:
            return pk_bytes, record
    return None


def _build_self_advert_push(state: BridgeState) -> bytes:
    record = build_contact_record(state.pub_key, name=state.name)
    return bytes([PUSH_NEW_ADVERT]) + record


def _send_cli_txt_msg(
    dst_pub: bytes,
    text: str,
    state: BridgeState,
    udp_sock: socket.socket,
    udp_addr: tuple[str, int],
) -> list[bytes]:
    """Send a ``TXT_TYPE_CLI`` (msg_type=1) message to a known contact."""
    ts = int(time.time())
    data = (
        bytes([0x01, 0x00]) + struct.pack("<I", ts) + dst_pub[:6] + text.encode("utf-8")
    )
    return _handle_send_txt_msg(data, state, udp_sock, udp_addr)


# ---------------------------------------------------------------------------
# Per-CMD handlers (TX side)
# ---------------------------------------------------------------------------


def _handle_send_txt_msg(
    data: bytes,
    state: BridgeState,
    udp_sock: socket.socket,
    udp_addr: tuple[str, int],
    txt_type: int = 0,
) -> list[bytes]:
    if len(data) < 12:
        return [state.build_error()]
    msg_type = data[0]
    attempt = data[1]
    ts = struct.unpack_from("<I", data, 2)[0]
    dst_prefix = data[6:12]
    text = data[12:]
    log.info(
        "companion: SEND_TXT_MSG type=%d txt_type=%d dst=%s '%s'",
        msg_type,
        txt_type,
        dst_prefix.hex(),
        sanitize_text(text.decode("utf-8", errors="replace")),
    )
    resolved = _resolve_contact(state, dst_prefix)
    if resolved is None:
        log.error("no contact matching prefix %s", dst_prefix.hex())
        return [state.build_error(2)]
    dest_pub, contact_record = resolved
    out_path_len = struct.unpack_from("<b", contact_record, 34)[0]
    use_flood = out_path_len < 0
    active_key = state.send_scope if any(state.send_scope) else state.region_key

    if use_flood:
        tmp = build_txt_msg_raw(
            state.expanded_prv,
            state.pub_key,
            dest_pub,
            text,
            timestamp=ts,
            attempt=attempt,
            txt_type=txt_type,
            route_type=ROUTE_FLOOD,
        )
        if any(active_key):
            from lora.bridges.meshcore.protocol import compute_transport_code

            txt_payload = tmp[2:]
            tc1 = compute_transport_code(active_key, PAYLOAD_TXT, txt_payload)
            header = make_header(ROUTE_T_FLOOD, PAYLOAD_TXT)
            packet = build_wire_packet(header, txt_payload, transport_codes=(tc1, 0))
        else:
            packet = tmp
        log.info("TX: %dB TXT_MSG (flood) to %s", len(packet), dst_prefix.hex())
    else:
        stored_path = b""
        if out_path_len > 0:
            stored_path = contact_record[35 : 35 + out_path_len]
        tmp = build_txt_msg_raw(
            state.expanded_prv,
            state.pub_key,
            dest_pub,
            text,
            timestamp=ts,
            attempt=attempt,
            txt_type=txt_type,
            route_type=ROUTE_DIRECT,
        )
        if stored_path:
            txt_payload = tmp[2:]
            header = make_header(ROUTE_DIRECT, PAYLOAD_TXT)
            packet = build_wire_packet(header, txt_payload, path=stored_path)
        else:
            packet = tmp
        log.info(
            "TX: %dB TXT_MSG (direct, path_len=%d) to %s",
            len(packet),
            out_path_len,
            dst_prefix.hex(),
        )

    h = hash_payload(packet)
    state.recent_tx[h] = time.monotonic()
    cbor_msg = make_cbor_tx_request(packet)
    udp_sock.sendto(cbor_msg, udp_addr)
    state.pkt_sent += 1
    if use_flood:
        state.pkt_flood_tx += 1
    else:
        state.pkt_direct_tx += 1

    plaintext = (
        struct.pack("<I", ts) + bytes([(txt_type << 2) | (attempt & 0x03)]) + text
    )
    expected_ack = compute_ack_hash(plaintext, state.pub_key)
    resp = state.build_msg_sent(flood=use_flood, ack_hash=expected_ack)
    state.pending_acks[expected_ack] = (time.monotonic(), dst_prefix)
    return [resp]


def _handle_send_advert(
    data: bytes,
    state: BridgeState,
    udp_sock: socket.socket,
    udp_addr: tuple[str, int],
) -> list[bytes]:
    del data  # flood flag is currently ignored — ADVERTs always flood
    lat = state.lat_e6 / 1e6 if state.lat_e6 != 0 else None
    lon = state.lon_e6 / 1e6 if state.lon_e6 != 0 else None
    active_key = state.send_scope if any(state.send_scope) else state.region_key
    if any(active_key):
        from lora.bridges.meshcore.protocol import compute_transport_code

        tmp = build_advert_raw(
            state.seed, state.pub_key, name=state.name, lat=lat, lon=lon
        )
        advert_payload = tmp[2:]
        tc1 = compute_transport_code(active_key, PAYLOAD_ADVERT, advert_payload)
        header = make_header(ROUTE_T_FLOOD, PAYLOAD_ADVERT)
        packet = build_wire_packet(header, advert_payload, transport_codes=(tc1, 0))
    else:
        packet = build_advert_raw(
            state.seed, state.pub_key, name=state.name, lat=lat, lon=lon
        )
    h = hash_payload(packet)
    state.recent_tx[h] = time.monotonic()
    cbor_msg = make_cbor_tx_request(packet)
    udp_sock.sendto(cbor_msg, udp_addr)
    state.pkt_sent += 1
    state.pkt_flood_tx += 1
    log.info("TX: ADVERT '%s' %dB", state.name, len(packet))
    return [state.build_ok()]


def _handle_send_chan_txt_msg(
    data: bytes,
    state: BridgeState,
    udp_sock: socket.socket,
    udp_addr: tuple[str, int],
) -> list[bytes]:
    if len(data) < 7:
        return [state.build_error()]
    _msg_type = data[0]
    chan_idx = data[1]
    ts = struct.unpack_from("<I", data, 2)[0]
    text = data[6:]
    if chan_idx >= len(state.channels):
        log.error(
            "channel index %d out of range (%d loaded)",
            chan_idx,
            len(state.channels),
        )
        return [state.build_error(3)]
    channel = state.channels[chan_idx]
    text_str = text.decode("utf-8", errors="replace")
    log.info(
        "companion: SEND_CHAN_TXT_MSG ch=%d '%s': '%s'",
        chan_idx,
        sanitize_text(channel.name),
        sanitize_text(text_str),
    )
    grp_payload = build_grp_txt(channel, state.name, text_str, timestamp=ts)
    active_key = state.send_scope if any(state.send_scope) else state.region_key
    if any(active_key):
        from lora.bridges.meshcore.protocol import compute_transport_code

        tc1 = compute_transport_code(active_key, PAYLOAD_GRP_TXT, grp_payload)
        header = make_header(ROUTE_T_FLOOD, PAYLOAD_GRP_TXT)
        packet = build_wire_packet(header, grp_payload, transport_codes=(tc1, 0))
    else:
        header = make_header(ROUTE_FLOOD, PAYLOAD_GRP_TXT)
        packet = build_wire_packet(header, grp_payload)
    h = hash_payload(packet)
    state.recent_tx[h] = time.monotonic()
    cbor_msg = make_cbor_tx_request(packet)
    udp_sock.sendto(cbor_msg, udp_addr)
    state.pkt_sent += 1
    state.pkt_flood_tx += 1
    log.info("TX: %dB GRP_TXT to #%s", len(packet), channel.name)
    return [state.build_ok()]


def _handle_send_control_data(
    data: bytes,
    state: BridgeState,
    udp_sock: socket.socket,
    udp_addr: tuple[str, int],
) -> list[bytes]:
    if not data:
        return [state.build_error()]
    active_key = state.send_scope if any(state.send_scope) else state.region_key
    if any(active_key):
        from lora.bridges.meshcore.protocol import compute_transport_code

        tc1 = compute_transport_code(active_key, PAYLOAD_CTRL, data)
        header = make_header(ROUTE_T_FLOOD, PAYLOAD_CTRL)
        packet = build_wire_packet(header, data, transport_codes=(tc1, 0))
    else:
        header = make_header(ROUTE_FLOOD, PAYLOAD_CTRL)
        packet = build_wire_packet(header, data)
    h = hash_payload(packet)
    state.recent_tx[h] = time.monotonic()
    cbor_msg = make_cbor_tx_request(packet)
    udp_sock.sendto(cbor_msg, udp_addr)
    state.pkt_sent += 1
    state.pkt_flood_tx += 1
    log.info("TX: %dB CTRL type=0x%02x", len(packet), data[0])
    return [state.build_ok()]


def _handle_set_channel(data: bytes, state: BridgeState) -> list[bytes]:
    if len(data) < 65:
        return [state.build_error(2)]
    idx = data[0]
    name_raw = data[1:33].rstrip(b"\x00")
    secret_raw = data[33:65]
    if idx >= 8:
        return [state.build_error(5)]
    name = name_raw.decode("utf-8", errors="replace").rstrip("\x00")
    if not name:
        # Clearing through reload — channels are file-backed; the
        # IdentityStore caches them.  Best-effort: drop in-memory by
        # forcing reload after no-op.  (Legacy behaviour only cleared
        # the live list; we leave on-disk channels intact.)
        log.info("companion: clear channel %d (in-memory)", idx)
        return [state.build_ok()]
    psk_16 = secret_raw[:16]
    save_channel(state.channels_dir, name, psk_16)
    state.identity.reload()
    log.info("companion: SET_CHANNEL %d '%s'", idx, sanitize_text(name))
    return [state.build_ok()]


def _handle_add_contact(data: bytes, state: BridgeState) -> list[bytes]:
    if len(data) < 32:
        return [state.build_error()]
    pk = data[:32]
    pk_hex = pk.hex()
    name_offset = 32 + 1 + 1 + 1 + 64
    if name_offset + 32 <= len(data):
        name = (
            data[name_offset : name_offset + 32]
            .rstrip(b"\x00")
            .decode("utf-8", errors="replace")
        )
    else:
        name = ""
    now = int(time.time())
    record = data[:143].ljust(143, b"\x00") + struct.pack("<I", now)
    state.contacts[pk_hex] = record
    state.contacts_lastmod = now
    _persist_contact(state, pk_hex, record)
    log.info("contact: %s.. '%s'", pk_hex[:8], sanitize_text(name))
    return [state.build_ok()]


def _handle_import_contact(data: bytes, state: BridgeState) -> list[bytes]:
    info = parse_advert_wire_packet(data)
    if info is None:
        log.warning("companion: IMPORT_CONTACT — failed to parse ADVERT")
        return [state.build_error()]
    pk_hex = info["pubkey"].hex()
    record = build_contact_record(
        info["pubkey"],
        name=info["name"],
        node_type=info["node_type"],
        lat=info["lat"],
        lon=info["lon"],
        last_advert=info["last_advert"],
    )
    state.contacts[pk_hex] = record
    state.contacts_lastmod = int(time.time())
    _persist_contact(state, pk_hex, record)
    log.info(
        "companion: IMPORT_CONTACT %s.. '%s'",
        pk_hex[:8],
        sanitize_text(str(info["name"])),
    )
    return [state.build_ok()]


def _handle_export_contact(data: bytes, state: BridgeState) -> list[bytes]:
    prefix = data[:6] if len(data) >= 6 else data
    if not prefix or state.pub_key[: len(prefix)] == prefix:
        lat = state.lat_e6 / 1e6 if state.lat_e6 != 0 else None
        lon = state.lon_e6 / 1e6 if state.lon_e6 != 0 else None
        advert_pkt = build_advert_raw(
            state.seed, state.pub_key, name=state.name, lat=lat, lon=lon
        )
        return [bytes([RESP_CONTACT_URI]) + advert_pkt]
    resolved = _resolve_contact(state, prefix)
    if resolved is None:
        log.warning("companion: EXPORT_CONTACT not found: %s", prefix.hex())
        return [state.build_error(2)]
    full_pub, record = resolved
    wire_pkt = build_unsigned_advert_from_record(record)
    log.info("companion: EXPORT_CONTACT %s.. (unsigned)", full_pub.hex()[:8])
    return [bytes([RESP_CONTACT_URI]) + wire_pkt]


# ---------------------------------------------------------------------------
# Main command dispatcher
# ---------------------------------------------------------------------------


def handle_command(
    cmd_payload: bytes,
    state: BridgeState,
    udp_sock: socket.socket | None,
    udp_addr: tuple[str, int] | None,
) -> list[bytes]:
    """Dispatch a companion-protocol command to its handler."""
    if not cmd_payload:
        return []
    cmd = cmd_payload[0]
    data = cmd_payload[1:]

    if cmd == CMD_APP_START:
        log.info("companion: APP_START")
        state.apply_startup_config()
        responses = [state.build_self_info()]
        responses.append(_build_self_advert_push(state))
        _ensure_self_contact(state)
        return responses

    if cmd == CMD_DEVICE_QUERY:
        return [state.build_device_info()]

    if cmd == CMD_GET_CONTACTS:
        return state.build_contacts_response()

    if cmd == CMD_GET_DEVICE_TIME:
        return [state.build_current_time()]

    if cmd == CMD_SET_DEVICE_TIME:
        return [state.build_ok()]

    if cmd == CMD_GET_BATT_AND_STORAGE:
        return [state.build_battery()]

    if cmd == CMD_GET_CHANNEL:
        idx = data[0] if data else 0
        if idx >= 8:
            return [state.build_error()]
        return [state.build_channel_info(idx)]

    if cmd == CMD_GET_STATS:
        stats_type = data[0] if data else 0
        return [state.build_stats(stats_type)]

    if cmd == CMD_SYNC_NEXT_MESSAGE:
        if state.msg_queue:
            return [state.msg_queue.popleft()]
        return [state.build_no_more_msgs()]

    if cmd == CMD_SEND_TXT_MSG:
        assert udp_sock is not None and udp_addr is not None
        return _handle_send_txt_msg(data, state, udp_sock, udp_addr)

    if cmd == CMD_SEND_CHAN_TXT_MSG:
        assert udp_sock is not None and udp_addr is not None
        return _handle_send_chan_txt_msg(data, state, udp_sock, udp_addr)

    if cmd == CMD_SEND_CONTROL_DATA:
        assert udp_sock is not None and udp_addr is not None
        return _handle_send_control_data(data, state, udp_sock, udp_addr)

    if cmd == CMD_SEND_SELF_ADVERT:
        assert udp_sock is not None and udp_addr is not None
        return _handle_send_advert(data, state, udp_sock, udp_addr)

    if cmd == CMD_SET_ADVERT_NAME:
        name = data.decode("utf-8", errors="replace").rstrip("\x00")
        state.name = name
        state.persist_config()
        log.info("companion: SET_ADVERT_NAME '%s' (EEPROM saved)", sanitize_text(name))
        return [state.build_ok()]

    if cmd == CMD_SET_RADIO_PARAMS:
        if len(data) >= 10:
            freq_hz = struct.unpack_from("<I", data, 0)[0]
            bw_hz = struct.unpack_from("<I", data, 4)[0]
            sf_new = data[8]
            cr_new = data[9]
            if len(data) >= 11:
                repeat = data[10]
                if repeat and not is_valid_repeat_freq(freq_hz // 1000):
                    log.warning(
                        "SET_RADIO_PARAMS repeat=1 rejected: freq %d kHz not allowed",
                        freq_hz // 1000,
                    )
                    return [state.build_error(ERR_CODE_ILLEGAL_ARG)]
                state.client_repeat = repeat
            state.freq_mhz = freq_hz / 1_000_000.0
            state.bw_khz = bw_hz / 1_000.0
            state.sf = sf_new
            state.cr = cr_new
            log.warning(
                "SET_RADIO_PARAMS freq=%.3f MHz bw=%.1f kHz sf=%d cr=%d repeat=%d",
                state.freq_mhz,
                state.bw_khz,
                state.sf,
                state.cr,
                state.client_repeat,
            )
        return [state.build_ok()]

    if cmd == CMD_SET_RADIO_TX_POWER:
        if data:
            dbm = data[0] if data[0] < 128 else data[0] - 256
            state.tx_power = dbm
            log.info("SET_RADIO_TX_POWER %d dBm", dbm)
        return [state.build_ok()]

    if cmd == CMD_IMPORT_CONTACT:
        return _handle_import_contact(data, state)

    if cmd == CMD_ADD_UPDATE_CONTACT:
        return _handle_add_contact(data, state)

    if cmd == CMD_REMOVE_CONTACT:
        if len(data) >= 6:
            prefix_hex = data[:6].hex()
            matched = [k for k in state.contacts if k.startswith(prefix_hex)]
            for pk_hex in matched:
                state.contacts.pop(pk_hex, None)
                _delete_persisted_contact(state, pk_hex)
                log.info("REMOVE_CONTACT %s..", pk_hex[:8])
            if not matched:
                log.warning("REMOVE_CONTACT: no match for prefix %s", prefix_hex)
        return [state.build_ok()]

    if cmd == CMD_EXPORT_CONTACT:
        return _handle_export_contact(data, state)

    if cmd == CMD_SHARE_CONTACT:
        frames = _handle_export_contact(data, state)
        if frames and frames[0][0] == RESP_CONTACT_URI:
            wire_pkt = frames[0][1:]
            log.info("share_contact URI: meshcore://%s", wire_pkt.hex())
        else:
            return frames
        return [state.build_ok()]

    if cmd == CMD_SET_CHANNEL:
        return _handle_set_channel(data, state)

    if cmd == CMD_SET_ADVERT_LATLON:
        if len(data) >= 8:
            state.lat_e6 = struct.unpack_from("<i", data, 0)[0]
            state.lon_e6 = struct.unpack_from("<i", data, 4)[0]
            state.persist_config()
            log.info(
                "SET_ADVERT_LATLON lat=%.6f lon=%.6f (EEPROM saved)",
                state.lat_e6 / 1e6,
                state.lon_e6 / 1e6,
            )
        return [state.build_ok()]

    if cmd == CMD_SET_FLOOD_SCOPE:
        if not data or data[0] != 0:
            return [state.build_error(ERR_CODE_ILLEGAL_ARG)]
        if len(data) >= 17:
            new_key = bytes(data[1:17])
            if not any(new_key) and any(state.startup.send_scope):
                log.info(
                    "SET_FLOOD_SCOPE: ignoring all-zero (config scope active: %s..)",
                    state.startup.send_scope.hex()[:16],
                )
            else:
                state.send_scope = new_key
                log.info("SET_FLOOD_SCOPE set: %s..", state.send_scope.hex()[:16])
        else:
            if any(state.startup.send_scope):
                log.info(
                    "SET_FLOOD_SCOPE: ignoring clear (config scope active: %s..)",
                    state.startup.send_scope.hex()[:16],
                )
            else:
                state.send_scope = b"\x00" * 16
                log.info("SET_FLOOD_SCOPE cleared")
        return [state.build_ok()]

    if cmd == CMD_GET_ALLOWED_REPEAT_FREQ:
        return [state.build_allowed_repeat_freq()]

    if cmd == CMD_SET_AUTOADD_CONFIG:
        state.autoadd_config = data[0] if data else 0
        if len(data) >= 2:
            state.autoadd_max_hops = min(data[1], 64)
        state.persist_config()
        log.debug(
            "SET_AUTOADD_CONFIG config=0x%02x max_hops=%d (EEPROM saved)",
            state.autoadd_config,
            state.autoadd_max_hops,
        )
        return [state.build_ok()]

    if cmd == CMD_GET_AUTOADD_CONFIG:
        return [
            bytes(
                [
                    RESP_CODE_AUTOADD_CONFIG,
                    state.autoadd_config,
                    state.autoadd_max_hops,
                ]
            )
        ]

    if cmd == CMD_SET_PATH_HASH_MODE:
        if len(data) < 2 or data[0] != 0:
            return [state.build_error(ERR_CODE_ILLEGAL_ARG)]
        if data[1] >= 3:
            return [state.build_error(ERR_CODE_ILLEGAL_ARG)]
        state.path_hash_mode = data[1]
        state.persist_config()
        log.debug("SET_PATH_HASH_MODE mode=%d (EEPROM saved)", state.path_hash_mode)
        return [state.build_ok()]

    if cmd == CMD_SET_OTHER_PARAMS:
        if len(data) >= 3:
            state.manual_add_contacts = data[0]
            state.telemetry_mode = data[1]
            state.adv_loc_policy = data[2]
            state.multi_acks = data[3] if len(data) >= 4 else 0
            state.persist_config()
            log.debug(
                "SET_OTHER_PARAMS manual_add=%d telemetry=0x%02x adv_loc=%d "
                "multi_acks=%d (EEPROM saved)",
                state.manual_add_contacts,
                state.telemetry_mode,
                state.adv_loc_policy,
                state.multi_acks,
            )
        else:
            log.warning("SET_OTHER_PARAMS too short (%d bytes)", len(data))
        return [state.build_ok()]

    if cmd == CMD_GET_CUSTOM_VARS:
        return [bytes([RESP_CUSTOM_VARS])]

    if cmd == CMD_SET_CUSTOM_VAR:
        return [state.build_error(ERR_CODE_ILLEGAL_ARG)]

    if cmd == CMD_RESET_PATH:
        reset_count = 0
        for pk_hex, record in state.contacts.items():
            current_path_len = struct.unpack_from("<b", record, 34)[0]
            if current_path_len >= 0:
                buf = bytearray(record)
                struct.pack_into("<b", buf, 34, -1)
                buf[35:99] = b"\x00" * 64
                state.contacts[pk_hex] = bytes(buf)
                _persist_contact(state, pk_hex, bytes(buf))
                reset_count += 1
        if reset_count > 0:
            log.info("RESET_PATH cleared paths for %d contacts", reset_count)
        return [state.build_ok()]

    if cmd == CMD_SET_TUNING_PARAMS:
        return [state.build_ok()]

    if cmd == CMD_REBOOT:
        state.apply_startup_config()
        return [state.build_ok()]

    if cmd == CMD_LOGIN:
        assert udp_sock is not None and udp_addr is not None
        if len(data) < 32:
            return [state.build_error()]
        dst_pub = data[:32]
        password = data[32:].decode("utf-8", errors="replace")
        log.info("LOGIN to %s.. pwd='%s'", dst_pub.hex()[:8], password)
        return _send_cli_txt_msg(
            dst_pub, f"login {password}", state, udp_sock, udp_addr
        )

    if cmd == CMD_STATUS_REQ:
        assert udp_sock is not None and udp_addr is not None
        if len(data) < 32:
            return [state.build_error()]
        dst_pub = data[:32]
        log.info("STATUS_REQ to %s..", dst_pub.hex()[:8])
        state._pending_status = dst_pub[:4]
        return _send_cli_txt_msg(dst_pub, "status", state, udp_sock, udp_addr)

    if cmd == CMD_LOGOUT:
        assert udp_sock is not None and udp_addr is not None
        if len(data) < 32:
            return [state.build_error()]
        dst_pub = data[:32]
        log.info("LOGOUT from %s..", dst_pub.hex()[:8])
        result = _send_cli_txt_msg(dst_pub, "logout", state, udp_sock, udp_addr)
        if result and result[0][0] == state.build_error()[0]:
            return result
        return [state.build_ok()]

    if cmd == CMD_GET_ADVERT_PATH:
        if len(data) < 33:
            return [state.build_error(ERR_CODE_ILLEGAL_ARG)]
        pubkey = data[1:33]
        pk_hex = pubkey.hex()
        if pk_hex not in state.contacts:
            return [state.build_error(ERR_CODE_NOT_FOUND)]
        record = state.contacts[pk_hex]
        last_advert = struct.unpack_from("<I", record, 131)[0]
        path_len_enc = record[34]
        hop_count, _, path_byte_len = decode_path_len(path_len_enc & 0xFF)
        path_bytes = record[35 : 35 + path_byte_len]
        resp = (
            bytes([RESP_ADVERT_PATH])
            + struct.pack("<I", last_advert)
            + bytes([path_len_enc])
            + path_bytes
        )
        log.info("GET_ADVERT_PATH %s.. → %d hops", pk_hex[:8], hop_count)
        return [resp]

    if cmd == CMD_TRACE:
        assert udp_sock is not None and udp_addr is not None
        if len(data) <= 9:
            return [state.build_error(ERR_CODE_ILLEGAL_ARG)]
        tag_bytes = data[:4]
        auth_bytes = data[4:8]
        flags_byte = data[8:9]
        path_hashes = data[9:]
        trace_payload = tag_bytes + auth_bytes + flags_byte + path_hashes
        header = make_header(ROUTE_DIRECT, PAYLOAD_TRACE)
        packet = build_wire_packet(header, trace_payload)
        h = hash_payload(packet)
        state.recent_tx[h] = time.monotonic()
        state.pkt_sent += 1
        state.pkt_direct_tx += 1
        cbor_msg = make_cbor_tx_request(packet)
        udp_sock.sendto(cbor_msg, udp_addr)
        log.info("TRACE tag=%s flags=0x%02x", tag_bytes.hex(), data[8])
        return [state.build_msg_sent(flood=False, ack_hash=tag_bytes)]

    if cmd == CMD_PATH_DISCOVERY:
        assert udp_sock is not None and udp_addr is not None
        if len(data) < 33:
            return [state.build_error(ERR_CODE_ILLEGAL_ARG)]
        dst_pub = data[1:33]
        pk_hex = dst_pub.hex()
        if pk_hex not in state.contacts:
            resolved = _resolve_contact(state, dst_pub[:6])
            if resolved is None:
                return [state.build_error(ERR_CODE_NOT_FOUND)]
            dst_pub = resolved[0]
        log.info("PATH_DISCOVERY to %s..", dst_pub.hex()[:8])
        state._pending_discovery = dst_pub[:4]
        return _send_cli_txt_msg(dst_pub, "status", state, udp_sock, udp_addr)

    if cmd == CMD_BINARY_REQ:
        assert udp_sock is not None and udp_addr is not None
        if len(data) < 33:
            return [state.build_error()]
        dst_pub = data[:32]
        req_data = data[32:]
        ts = int(time.time())
        companion_data = (
            bytes([0x01, 0x00]) + struct.pack("<I", ts) + dst_pub[:6] + req_data
        )
        log.info(
            "BINARY_REQ type=0x%02x to %s..",
            req_data[0] if req_data else 0,
            dst_pub.hex()[:8],
        )
        return _handle_send_txt_msg(
            companion_data, state, udp_sock, udp_addr, txt_type=1
        )

    if cmd == CMD_SEND_ANON_REQ:
        assert udp_sock is not None and udp_addr is not None
        if len(data) < 33:
            return [state.build_error()]
        dst_pub = data[:32]
        req_type = data[32] if len(data) > 32 else 0
        ts = int(time.time())
        pkt = build_anon_req_raw(
            state.expanded_prv,
            state.pub_key,
            dst_pub,
            bytes([req_type]),
            timestamp=ts,
            route_type=ROUTE_DIRECT,
        )
        h = hash_payload(pkt)
        state.recent_tx[h] = time.monotonic()
        state.pkt_sent += 1
        state.pkt_direct_tx += 1
        cbor_msg = make_cbor_tx_request(pkt)
        udp_sock.sendto(cbor_msg, udp_addr)
        expected_ack = compute_ack_hash(
            struct.pack("<I", ts) + bytes([req_type]), state.pub_key
        )
        resp = state.build_msg_sent(flood=False, ack_hash=expected_ack)
        state.pending_acks[expected_ack] = (time.monotonic(), dst_pub[:6])
        log.info("SEND_ANON_REQ to %s..", dst_pub.hex()[:8])
        return [resp]

    if cmd == CMD_EXPORT_PRIVATE_KEY:
        log.info("EXPORT_PRIVATE_KEY")
        return [bytes([RESP_OK]) + state.seed[:32]]

    if cmd == CMD_IMPORT_PRIVATE_KEY:
        if len(data) < 32:
            return [state.build_error(ERR_CODE_ILLEGAL_ARG)]
        # IMPORT_PRIVATE_KEY mutates identity in-memory only; persistence
        # to ``identity.bin`` lives in the IdentityStore layer (Phase 5+).
        from nacl.signing import SigningKey

        new_seed = data[:32]
        sk = SigningKey(new_seed)
        new_pub = bytes(sk.verify_key)
        new_expanded = meshcore_expanded_key(new_seed)
        state.identity._our_seed = new_seed
        state.identity._our_pub = new_pub
        state.identity._our_prv = new_expanded
        log.info("IMPORT_PRIVATE_KEY → new pubkey %s..", new_pub.hex()[:8])
        return [state.build_ok()]

    if cmd == CMD_SEND_TELEMETRY_REQ:
        assert udp_sock is not None and udp_addr is not None
        if len(data) < 32:
            return [state.build_error()]
        dst_pub = data[:32]
        log.info("SEND_TELEMETRY_REQ to %s..", dst_pub.hex()[:8])
        return _send_cli_txt_msg(dst_pub, "status", state, udp_sock, udp_addr)

    if cmd == CMD_GET_TUNING_PARAMS:
        return [bytes([RESP_TUNING_PARAMS]) + struct.pack("<II", 0, 0)]

    if cmd == CMD_FACTORY_RESET:
        state.reset_config()
        for d in (state.contacts_dir, state.keys_dir):
            if d is not None and d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
        state.contacts.clear()
        state.identity.reload()
        state.msg_queue.clear()
        state.pkt_recv = state.pkt_sent = 0
        state.pkt_flood_tx = state.pkt_direct_tx = 0
        state.pkt_flood_rx = state.pkt_direct_rx = 0
        state.send_scope = b"\x00" * 16
        state.client_repeat = 0
        state.autoadd_config = 0
        state.autoadd_max_hops = 0
        state.path_hash_mode = 0
        state.default_scope_name = ""
        state.default_scope_key = b"\x00" * 16
        log.warning("FACTORY_RESET — data and EEPROM wiped")
        return [state.build_ok()]

    if cmd == CMD_HAS_CONNECTION:
        return [state.build_error(ERR_CODE_NOT_FOUND)]

    if cmd == CMD_GET_CONTACT_BY_KEY:
        if len(data) < 6:
            return [state.build_error(ERR_CODE_ILLEGAL_ARG)]
        prefix = data[:6]
        resolved = _resolve_contact(state, prefix)
        if resolved is None:
            return [state.build_error(ERR_CODE_NOT_FOUND)]
        _, record = resolved
        return [bytes([RESP_CONTACT]) + record]

    if cmd == CMD_SET_DEVICE_PIN:
        if len(data) >= 4:
            state.device_pin = struct.unpack_from("<I", data, 0)[0]
        else:
            state.device_pin = 0
        state.persist_config()
        log.debug("SET_DEVICE_PIN %d (EEPROM saved)", state.device_pin)
        return [state.build_ok()]

    if cmd == CMD_SET_DEFAULT_FLOOD_SCOPE:
        # Wire format (meshcore_py messaging.py:374-377, mirrors firmware
        # set_default_flood_scope):
        #   [cmd][scope_name utf-8, zero-padded to 31][scope_key 16]
        # Empty name + all-zero key = clear default.
        if len(data) < 47:
            return [state.build_error(ERR_CODE_ILLEGAL_ARG)]
        name_raw = bytes(data[:31])
        new_key = bytes(data[31:47])
        state.default_scope_name = name_raw.rstrip(b"\x00").decode(
            "utf-8", errors="replace"
        )
        state.default_scope_key = new_key
        # Firmware semantic: setting the default also takes immediate effect
        # on the per-session send_scope (mirrors set_default_flood_scope in
        # MeshCore firmware, which writes through to the active radio scope).
        state.send_scope = new_key
        state.persist_config()
        log.info(
            "SET_DEFAULT_FLOOD_SCOPE name=%r key=%s.. (EEPROM saved)",
            state.default_scope_name,
            new_key.hex()[:16] if any(new_key) else "cleared",
        )
        return [state.build_ok()]

    if cmd == CMD_GET_DEFAULT_FLOOD_SCOPE:
        # Wire format (meshcore_py reader.py:185-189):
        #   [resp_code][scope_name utf-8 31 bytes][scope_key 16 bytes]
        name_b = state.default_scope_name.encode("utf-8")[:31].ljust(31, b"\x00")
        return [bytes([RESP_DEFAULT_FLOOD_SCOPE]) + name_b + state.default_scope_key]

    return [state.build_ok()]


__all__ = [
    "handle_command",
    "lora_frame_to_companion_msgs",
]
