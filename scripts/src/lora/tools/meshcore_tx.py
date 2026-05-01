#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""MeshCore TX builder — Python library + ``lora tx`` CLI.

The library half exposes ergonomic builders consumable from
:mod:`lora.hwtests.*` and other Python callers:

  - :func:`build_advert(name, lat=None, lon=None, identity_path=None)`
  - :func:`build_txt_msg(text, recipient_pubkey, identity_path=None)`
  - :func:`build_anon_req(text, recipient_pubkey, identity_path=None)`
  - :func:`send_via_udp(packet, *, host, port, sync_word=0x12, **phy_overrides)`

Each builder loads (or creates) the local Ed25519 identity from
``identity_path`` (default :data:`lora.core.meshcore_crypto.DEFAULT_IDENTITY_FILE`)
and returns the wire bytes — ready to drop into a ``lora_tx`` CBOR map.

The CLI half (``lora tx``) mirrors the legacy ``meshcore_tx.py`` argparse
surface: ``advert``, ``send``, ``anon-req`` subcommands with the same
``--connect``, ``--dry-run``, ``--freq``, ``--sf``, ``--bw`` flags.
"""

from __future__ import annotations

import argparse
import logging
import socket
import struct
import sys
import time
from pathlib import Path
from typing import Any

import cbor2
import segno
from nacl.signing import SigningKey

from lora.core.logging import add_logging_args, setup_logging
from lora.core.meshcore_crypto import (
    ADVERT_HAS_LOCATION,
    ADVERT_HAS_NAME,
    ADVERT_NODE_CHAT,
    DEFAULT_IDENTITY_FILE,
    PATH_HASH_SIZE,
    PAYLOAD_ADVERT,
    PAYLOAD_ANON_REQ,
    PAYLOAD_TXT,
    PUB_KEY_SIZE,
    ROUTE_DIRECT,
    ROUTE_FLOOD,
    ROUTE_T_DIRECT,
    ROUTE_T_FLOOD,
    load_or_create_identity,
    meshcore_encrypt_then_mac,
    meshcore_shared_secret,
)
from lora.core.meshcore_uri import build_bizcard_uri, build_contact_uri
from lora.core.udp import parse_host_port

log = logging.getLogger("lora.tools.meshcore_tx")

ROUTE_NAMES: dict[str, int] = {
    "flood": ROUTE_FLOOD,
    "direct": ROUTE_DIRECT,
    "tc-flood": ROUTE_T_FLOOD,
    "tc-direct": ROUTE_T_DIRECT,
}


# ---- low-level packet construction ---------------------------------------


def make_header(route_type: int, payload_type: int, version: int = 0) -> int:
    """Build the MeshCore header byte ``[version|payload_type|route_type]``."""
    return (version << 6) | (payload_type << 2) | route_type


def build_wire_packet(
    header: int,
    payload: bytes,
    *,
    path: bytes = b"",
    transport_codes: tuple[int, int] | None = None,
) -> bytes:
    """Compose a complete MeshCore wire-format packet.

    Format: ``[header(1)][opt transport(4)][path_len(1)][path(N)][payload(M)]``.
    """
    parts: list[bytes] = [bytes([header])]
    if transport_codes is not None:
        parts.append(struct.pack("<HH", transport_codes[0], transport_codes[1]))
    parts.append(bytes([len(path)]))
    parts.append(path)
    parts.append(payload)
    return b"".join(parts)


# ---- low-level builders (require identity material) ---------------------


def build_advert_raw(
    seed: bytes,
    pub_key: bytes,
    *,
    node_type: int = ADVERT_NODE_CHAT,
    name: str = "",
    timestamp: int | None = None,
    lat: float | None = None,
    lon: float | None = None,
    route_type: int = ROUTE_FLOOD,
    transport_codes: tuple[int, int] | None = None,
) -> bytes:
    """Build a signed ADVERT wire packet from a 32-byte Ed25519 ``seed``.

    Body: ``[pubkey(32)][timestamp(4)][signature(64)][app_data]`` where
    ``app_data = [flags(1)][opt lat(4)+lon(4)][opt name(UTF-8)]``.
    """
    if timestamp is None:
        timestamp = int(time.time())

    flags = node_type & 0x0F
    if lat is not None and lon is not None:
        flags |= ADVERT_HAS_LOCATION
    if name:
        flags |= ADVERT_HAS_NAME

    app_parts: list[bytes] = [bytes([flags])]
    if lat is not None and lon is not None:
        app_parts.append(struct.pack("<ii", int(lat * 1e6), int(lon * 1e6)))
    if name:
        app_parts.append(name.encode("utf-8"))
    app_data = b"".join(app_parts)

    ts_bytes = struct.pack("<I", timestamp)
    message = pub_key + ts_bytes + app_data

    sk = SigningKey(seed)
    signature = sk.sign(message).signature  # 64 bytes

    payload = pub_key + ts_bytes + signature + app_data
    header = make_header(route_type, PAYLOAD_ADVERT)
    return build_wire_packet(header, payload, transport_codes=transport_codes)


def build_txt_msg_raw(
    expanded_prv: bytes,
    pub_key: bytes,
    dest_pub: bytes,
    text: str | bytes,
    *,
    timestamp: int | None = None,
    attempt: int = 0,
    txt_type: int = 0,
    route_type: int = ROUTE_DIRECT,
    transport_codes: tuple[int, int] | None = None,
) -> bytes:
    """Build an encrypted TXT_MSG wire packet for a known contact.

    Plaintext: ``[ts(4)][txt_type<<2|attempt(1)][text]``. Encrypted via
    AES-128-ECB + 2-byte HMAC-SHA256 MAC under the ECDH shared secret.
    """
    if timestamp is None:
        timestamp = int(time.time())

    secret = meshcore_shared_secret(expanded_prv, dest_pub)
    if secret is None:
        raise ValueError("invalid key material — ECDH failed")

    ts_bytes = struct.pack("<I", timestamp)
    text_bytes = text if isinstance(text, bytes) else text.encode("utf-8")
    plaintext = ts_bytes + bytes([(txt_type << 2) | (attempt & 0x03)]) + text_bytes

    encrypted = meshcore_encrypt_then_mac(secret, plaintext)

    dest_hash = dest_pub[:PATH_HASH_SIZE]
    src_hash = pub_key[:PATH_HASH_SIZE]
    payload = dest_hash + src_hash + encrypted

    header = make_header(route_type, PAYLOAD_TXT)
    return build_wire_packet(header, payload, transport_codes=transport_codes)


def build_anon_req_raw(
    expanded_prv: bytes,
    pub_key: bytes,
    dest_pub: bytes,
    data: bytes,
    *,
    timestamp: int | None = None,
    route_type: int = ROUTE_DIRECT,
) -> bytes:
    """Build an encrypted ANON_REQ wire packet (sender pubkey in cleartext)."""
    if timestamp is None:
        timestamp = int(time.time())

    secret = meshcore_shared_secret(expanded_prv, dest_pub)
    if secret is None:
        raise ValueError("invalid key material — ECDH failed")

    ts_bytes = struct.pack("<I", timestamp)
    plaintext = ts_bytes + data
    encrypted = meshcore_encrypt_then_mac(secret, plaintext)

    dest_hash = dest_pub[:PATH_HASH_SIZE]
    payload = dest_hash + pub_key + encrypted

    header = make_header(route_type, PAYLOAD_ANON_REQ)
    return build_wire_packet(header, payload)


# ---- ergonomic identity-loading wrappers (Phase 5 hwtests entrypoints) ----


def _load_identity(identity_path: Path | None) -> tuple[bytes, bytes, bytes]:
    """Return ``(expanded_prv, pub_key, seed)`` from disk, creating on first use."""
    path = identity_path or DEFAULT_IDENTITY_FILE
    return load_or_create_identity(path)


def build_advert(
    name: str = "",
    *,
    lat: float | None = None,
    lon: float | None = None,
    identity_path: Path | None = None,
    node_type: int = ADVERT_NODE_CHAT,
    route_type: int = ROUTE_FLOOD,
) -> bytes:
    """Build an ADVERT wire packet using the local identity.

    Loads (or creates) the local identity from ``identity_path``
    (default :data:`DEFAULT_IDENTITY_FILE`) and signs the ADVERT
    body with it.
    """
    _expanded_prv, pub_key, seed = _load_identity(identity_path)
    return build_advert_raw(
        seed,
        pub_key,
        node_type=node_type,
        name=name,
        lat=lat,
        lon=lon,
        route_type=route_type,
    )


def build_txt_msg(
    text: str | bytes,
    recipient_pubkey: bytes,
    *,
    identity_path: Path | None = None,
    route_type: int = ROUTE_DIRECT,
) -> bytes:
    """Build an encrypted TXT_MSG wire packet to ``recipient_pubkey``."""
    if len(recipient_pubkey) != PUB_KEY_SIZE:
        raise ValueError(
            f"recipient_pubkey must be {PUB_KEY_SIZE} bytes, got {len(recipient_pubkey)}"
        )
    expanded_prv, pub_key, _seed = _load_identity(identity_path)
    return build_txt_msg_raw(
        expanded_prv,
        pub_key,
        recipient_pubkey,
        text,
        route_type=route_type,
    )


def build_anon_req(
    text: str | bytes,
    recipient_pubkey: bytes,
    *,
    identity_path: Path | None = None,
    route_type: int = ROUTE_DIRECT,
) -> bytes:
    """Build an encrypted ANON_REQ wire packet to ``recipient_pubkey``."""
    if len(recipient_pubkey) != PUB_KEY_SIZE:
        raise ValueError(
            f"recipient_pubkey must be {PUB_KEY_SIZE} bytes, got {len(recipient_pubkey)}"
        )
    data = text if isinstance(text, bytes) else text.encode("utf-8")
    expanded_prv, pub_key, _seed = _load_identity(identity_path)
    return build_anon_req_raw(
        expanded_prv,
        pub_key,
        recipient_pubkey,
        data,
        route_type=route_type,
    )


# ---- UDP transport --------------------------------------------------------


def make_cbor_tx_request(packet: bytes, **phy_overrides: Any) -> bytes:
    """Wrap a MeshCore wire packet in a ``lora_tx`` CBOR map."""
    msg: dict[str, Any] = {"type": "lora_tx", "payload": packet}
    msg.update(phy_overrides)
    return cbor2.dumps(msg)


def send_via_udp(
    packet: bytes,
    *,
    host: str,
    port: int,
    sync_word: int = 0x12,
    **phy_overrides: Any,
) -> None:
    """Send a built MeshCore wire packet to a lora-core / lora_trx UDP listener.

    Wraps *packet* in a ``lora_tx`` CBOR map (with optional ``sync_word``
    and other PHY overrides) and sends it once. The daemon responds on
    a separate ack port; this helper does NOT wait for the ack.
    """
    msg: dict[str, Any] = {"type": "lora_tx", "payload": packet, "sync_word": sync_word}
    msg.update(phy_overrides)
    cbor_msg = cbor2.dumps(msg)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(cbor_msg, (host, port))
        log.debug("sent %dB lora_tx to %s:%d", len(cbor_msg), host, port)
    finally:
        sock.close()


def print_qr_code(uri: str) -> None:
    """Render *uri* as a terminal QR code via segno."""
    qr = segno.make(uri)
    qr.terminal(compact=True)


# ---- CLI -----------------------------------------------------------------


def _parse_dest_pubkey(hex_str: str) -> bytes:
    """Decode and validate a 64-char hex destination pubkey, exit(1) on error."""
    try:
        dest_pub = bytes.fromhex(hex_str)
    except ValueError:
        log.error("invalid hex in destination key: %r", hex_str)
        sys.exit(1)
    if len(dest_pub) != PUB_KEY_SIZE:
        log.error(
            "destination key must be %d hex chars (%d bytes), got %d chars",
            PUB_KEY_SIZE * 2,
            PUB_KEY_SIZE,
            len(hex_str),
        )
        sys.exit(1)
    return dest_pub


def _cmd_advert(
    args: argparse.Namespace, expanded_prv: bytes, pub_key: bytes, seed: bytes
) -> bytes:
    return build_advert_raw(
        seed,
        pub_key,
        node_type=args.node_type,
        name=args.name or "",
        lat=args.lat,
        lon=args.lon,
    )


def _cmd_send(
    args: argparse.Namespace, expanded_prv: bytes, pub_key: bytes, seed: bytes
) -> bytes:
    dest_pub = _parse_dest_pubkey(args.dest)
    message = " ".join(args.message)
    if not message:
        log.error("message cannot be empty")
        sys.exit(1)
    route = ROUTE_NAMES[args.route]
    return build_txt_msg_raw(expanded_prv, pub_key, dest_pub, message, route_type=route)


def _cmd_anon_req(
    args: argparse.Namespace, expanded_prv: bytes, pub_key: bytes, seed: bytes
) -> bytes:
    dest_pub = _parse_dest_pubkey(args.dest)
    message = " ".join(args.message).encode("utf-8")
    if not message:
        log.error("message cannot be empty")
        sys.exit(1)
    route = ROUTE_NAMES[args.route]
    return build_anon_req_raw(
        expanded_prv, pub_key, dest_pub, message, route_type=route
    )


_EPILOG = """\
examples:
  # Broadcast ADVERT to a lora-core daemon:
  %(prog)s advert --name MyNode --connect 127.0.0.1:5555

  # Print contact QR code for the MeshCore companion app:
  %(prog)s advert --name MyNode --qr

  # Send encrypted message to a known contact (DIRECT):
  %(prog)s send --dest <64hex> --connect 127.0.0.1:5555 Hello

  # Anonymous encrypted request (sender pubkey in cleartext):
  %(prog)s anon-req --dest <64hex> --connect 127.0.0.1:5555 Hi
"""


def _build_parser() -> argparse.ArgumentParser:
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument(
        "--identity",
        type=Path,
        default=DEFAULT_IDENTITY_FILE,
        help=f"identity file (default: {DEFAULT_IDENTITY_FILE})",
    )
    shared.add_argument(
        "--dry-run",
        action="store_true",
        help="print packet hex to stderr; emit CBOR to stdout, do not transmit",
    )
    shared.add_argument(
        "--freq",
        type=int,
        default=None,
        help="override TX frequency (Hz)",
    )
    shared.add_argument(
        "--sf",
        type=int,
        default=None,
        help="override spreading factor (7-12)",
    )
    shared.add_argument(
        "--bw",
        type=int,
        default=None,
        help="override bandwidth (Hz)",
    )
    shared.add_argument(
        "--connect",
        metavar="HOST:PORT",
        default=None,
        help="send via UDP to this lora-core / lora_trx address",
    )

    parser = argparse.ArgumentParser(
        prog="lora tx",
        description="MeshCore TX builder. Emits lora_tx CBOR for the daemon.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[shared],
    )
    parser.add_argument(
        "--show-key", action="store_true", help="print local public key hex and exit"
    )

    sub = parser.add_subparsers(dest="command")

    p_advert = sub.add_parser(
        "advert",
        parents=[shared],
        help="broadcast ADVERT beacon",
        description="Broadcast an ADVERT advertising this node's identity.",
    )
    p_advert.add_argument("--name", type=str, default="", help="node name (UTF-8)")
    p_advert.add_argument(
        "--node-type",
        type=int,
        default=ADVERT_NODE_CHAT,
        choices=[1, 2, 3, 4],
        help="node type: 1=chat, 2=repeater, 3=room, 4=sensor",
    )
    p_advert.add_argument("--lat", type=float, default=None, help="latitude")
    p_advert.add_argument("--lon", type=float, default=None, help="longitude")
    p_advert.add_argument(
        "--qr",
        action="store_true",
        help="print contact QR code and exit",
    )

    p_send = sub.add_parser(
        "send",
        parents=[shared],
        help="encrypted message to known contact",
        description="Encrypted TXT_MSG to a contact whose pubkey is known.",
    )
    p_send.add_argument("--dest", required=True, help="destination pubkey (64 hex)")
    p_send.add_argument(
        "--route",
        choices=list(ROUTE_NAMES.keys()),
        default="direct",
        help="routing type",
    )
    p_send.add_argument("message", nargs="+", help="message text")

    p_anon = sub.add_parser(
        "anon-req",
        parents=[shared],
        help="anonymous encrypted request",
        description="Encrypted ANON_REQ; sender pubkey included in cleartext.",
    )
    p_anon.add_argument("--dest", required=True, help="destination pubkey (64 hex)")
    p_anon.add_argument(
        "--route",
        choices=list(ROUTE_NAMES.keys()),
        default="direct",
        help="routing type",
    )
    p_anon.add_argument("message", nargs="+", help="message text")

    add_logging_args(parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    from setproctitle import setproctitle

    setproctitle("lora-tx")

    parser = _build_parser()
    args = parser.parse_args(argv)

    setup_logging("gr4.tx", log_level=args.log_level, no_color=args.no_color)

    expanded_prv, pub_key, seed = _load_identity(args.identity)

    if args.show_key:
        print(pub_key.hex())
        return 0

    if args.command is None:
        parser.print_help()
        return 1

    if args.command == "advert" and getattr(args, "qr", False):
        name = args.name or ""
        uri = build_contact_uri(pub_key, name, args.node_type)
        log.info("contact URI: %s", uri)
        log.info("public key: %s", pub_key.hex())
        if name:
            log.info("name: %s", name)
        packet_for_biz = _cmd_advert(args, expanded_prv, pub_key, seed)
        biz_uri = build_bizcard_uri(packet_for_biz)
        log.info("biz card: %s", biz_uri)
        print_qr_code(uri)
        return 0

    if args.command == "advert":
        packet = _cmd_advert(args, expanded_prv, pub_key, seed)
    elif args.command == "send":
        packet = _cmd_send(args, expanded_prv, pub_key, seed)
    elif args.command == "anon-req":
        packet = _cmd_anon_req(args, expanded_prv, pub_key, seed)
    else:
        parser.print_help()
        return 1

    log.info("packet: %dB header=0x%02X hex=%s", len(packet), packet[0], packet.hex())
    if args.dry_run:
        log.info("dry run -- CBOR written to stdout, not transmitted")

    phy: dict[str, Any] = {}
    if args.freq is not None:
        phy["freq"] = args.freq
    if args.sf is not None:
        phy["sf"] = args.sf
    if args.bw is not None:
        phy["bw"] = args.bw
    if args.dry_run:
        phy["dry_run"] = True

    cbor_msg = make_cbor_tx_request(packet, **phy)

    if args.connect:
        try:
            host, port = parse_host_port(args.connect)
        except ValueError as exc:
            log.error("%s", exc)
            return 1
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.sendto(cbor_msg, (host, port))
        finally:
            sock.close()
        log.info("sent %dB to %s:%d", len(cbor_msg), host, port)
    else:
        sys.stdout.buffer.write(cbor_msg)
        sys.stdout.buffer.flush()

    return 0


if __name__ == "__main__":
    sys.exit(main())
