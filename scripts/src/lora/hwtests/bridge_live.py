#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Live TX integration test via meshcore_bridge.

Phase 5C port of ``bridge_live_test.py``. Connects to a
running ``meshcore_bridge`` over the companion-protocol TCP port,
broadcasts an ADVERT so the target companion learns our pubkey,
imports the target as a contact, sends a private encrypted message,
and verifies ``MSG_SENT`` is received back.

Requires:

* ``lora_trx`` running (SDR hardware attached)
* ``meshcore_bridge.py`` running and connected to ``lora_trx``

Usage:

* ``lora hwtest bridge-live``
* ``lora hwtest bridge-live --bridge 192.168.1.10:7834``
* ``lora hwtest bridge-live --message "custom text"``
* ``lora hwtest bridge-live --advert-repeats 3``
* ``lora hwtest bridge-live --skip-advert``

Note: the legacy file used Py2 ``except A, B:`` which would never parse;
the port uses ``except (A, B):`` instead.
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys
import time
from datetime import datetime, timezone

from lora.core.meshcore_crypto import (
    ADVERT_HAS_NAME,
    ADVERT_NODE_CHAT,
    DEFAULT_IDENTITY_FILE,
    SIGNATURE_SIZE,
)
from lora.core.udp import parse_host_port

# ---- Target companion ----

TARGET_PUBKEY = bytes.fromhex(
    "6be9724012b084ba53e544d05bce24b5587299fda5125b917d1f68bec35de039"
)
TARGET_NAME = "DO2THX"

# ---- Companion protocol constants ----

FRAME_TX = 0x3C
FRAME_RX = 0x3E

CMD_APP_START = 0x01
CMD_SEND_TXT_MSG = 0x02
CMD_GET_CONTACTS = 0x04
CMD_SEND_SELF_ADVERT = 0x07
CMD_IMPORT_CONTACT = 0x12
CMD_SYNC_NEXT_MESSAGE = 0x0A
CMD_DEVICE_QUERY = 0x16

RESP_OK = 0x00
RESP_ERROR = 0x01
RESP_CONTACT_START = 0x02
RESP_CONTACT = 0x03
RESP_CONTACT_END = 0x04
RESP_SELF_INFO = 0x05
RESP_MSG_SENT = 0x06
RESP_DEVICE_INFO = 0x0D

PUSH_MESSAGES_WAITING = 0x83
PUSH_NEW_ADVERT = 0x8A

BRIDGE_PORT = 7834


def frame_encode_tx(payload: bytes) -> bytes:
    """Encode a TX frame: ``0x3C`` + len_le16 + payload."""
    return bytes([FRAME_TX]) + struct.pack("<H", len(payload)) + payload


def recv_frames(sock: socket.socket, timeout: float = 5.0) -> list[bytes]:
    """Receive all available companion response frames within ``timeout``."""
    frames: list[bytes] = []
    buf = bytearray()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = max(0.1, deadline - time.monotonic())
        sock.settimeout(remaining)
        try:
            chunk = sock.recv(4096)
        except TimeoutError, OSError:
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
            except TimeoutError, OSError:
                pass
            break
    return frames


def send_cmd(sock: socket.socket, payload: bytes) -> list[bytes]:
    """Send a command and return response frames."""
    sock.sendall(frame_encode_tx(payload))
    return recv_frames(sock)


def build_synthetic_advert(pubkey: bytes, name: str) -> bytes:
    """Build a minimal synthetic ADVERT wire packet for IMPORT_CONTACT.

    Wire format::

        [header(1)][path_len(1)][pubkey(32)][timestamp(4)]
        [signature(64)][app_data(N)]
    """
    header = (0x04 << 2) | 0x01  # PAYLOAD_ADVERT | ROUTE_FLOOD = 0x11
    path_len = 0
    ts = struct.pack("<I", int(time.time()))
    sig = b"\x00" * SIGNATURE_SIZE
    flags = ADVERT_NODE_CHAT | ADVERT_HAS_NAME
    name_bytes = name.encode("utf-8")[:32]
    app_data = bytes([flags]) + name_bytes
    return bytes([header, path_len]) + pubkey + ts + sig + app_data


def find_contact(contacts_frames: list[bytes], pubkey: bytes) -> bool:
    """True if ``pubkey`` appears in CONTACT response frames."""
    for frame in contacts_frames:
        if frame[0] == RESP_CONTACT and len(frame) >= 33:
            if frame[1:33] == pubkey:
                return True
    return False


def extract_self_info_pubkey(frames: list[bytes]) -> bytes | None:
    """Extract our pubkey from a SELF_INFO response frame, if any."""
    for f in frames:
        if f[0] == RESP_SELF_INFO and len(f) >= 36:
            return f[4:36]
    return None


def main(
    argv: list[str] | None = None,
) -> int:  # pragma: no cover  # live bridge round-trip
    parser = argparse.ArgumentParser(
        prog="lora hwtest bridge-live",
        description="Live TX integration test via meshcore_bridge companion protocol.",
    )
    parser.add_argument(
        "--bridge",
        metavar="HOST:PORT",
        default=f"127.0.0.1:{BRIDGE_PORT}",
        help=f"Bridge TCP address (default: 127.0.0.1:{BRIDGE_PORT})",
    )
    parser.add_argument(
        "--message",
        default=None,
        help="Custom message text (default: timestamped test message)",
    )
    parser.add_argument(
        "--identity",
        default=str(DEFAULT_IDENTITY_FILE),
        help=f"Identity file (default: {DEFAULT_IDENTITY_FILE})",
    )
    parser.add_argument(
        "--advert-repeats",
        type=int,
        default=1,
        help="Number of ADVERTs to broadcast (default: 1)",
    )
    parser.add_argument(
        "--skip-advert",
        action="store_true",
        help="Skip ADVERT broadcast (companion already knows us)",
    )
    args = parser.parse_args(argv)

    try:
        bridge_host, bridge_port = parse_host_port(args.bridge)
    except ValueError as exc:
        parser.error(str(exc))

    if args.message:
        msg_text = args.message
    else:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        msg_text = f"gr4-lora test {ts}"

    print(f"Target: {TARGET_NAME} ({TARGET_PUBKEY.hex()[:16]}..)")
    print(f"Message: {msg_text}")
    print(f"Bridge: {bridge_host}:{bridge_port}")
    print()

    print("1. Connecting to bridge...", end=" ", flush=True)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5.0)
    try:
        sock.connect((bridge_host, bridge_port))
    except (ConnectionRefusedError, TimeoutError, OSError) as exc:
        print(f"FAILED: {exc}")
        print("   Is meshcore_bridge.py running?")
        return 1
    print("OK")

    try:
        print("2. Sending APP_START...", end=" ", flush=True)
        frames = send_cmd(sock, bytes([CMD_APP_START]))
        resp_types = [f[0] for f in frames]
        if RESP_SELF_INFO not in resp_types:
            print(f"FAILED: expected SELF_INFO, got {[hex(t) for t in resp_types]}")
            return 1
        our_pubkey = extract_self_info_pubkey(frames)
        if our_pubkey:
            print(f"OK (our pubkey: {our_pubkey.hex()[:16]}..)")
        else:
            print(f"OK ({len(frames)} frames)")

        if not args.skip_advert:
            n = args.advert_repeats
            print(f"3. Broadcasting ADVERT ({n}x)...", end=" ", flush=True)
            for i in range(n):
                advert_frames = send_cmd(sock, bytes([CMD_SEND_SELF_ADVERT]))
                advert_resp = [f[0] for f in advert_frames]
                if RESP_OK not in advert_resp and RESP_MSG_SENT not in advert_resp:
                    print(
                        f"\n   ADVERT {i + 1}/{n} FAILED: "
                        f"{[hex(t) for t in advert_resp]}"
                    )
                    return 1
            print("OK")
            settle = 2.0
            print(
                f"   Waiting {settle:.0f}s for companion to process ADVERT...",
                flush=True,
            )
            time.sleep(settle)
        else:
            print("3. Skipping ADVERT (--skip-advert)")

        print("4. Importing target contact...", end=" ", flush=True)
        advert = build_synthetic_advert(TARGET_PUBKEY, TARGET_NAME)
        frames = send_cmd(sock, bytes([CMD_IMPORT_CONTACT]) + advert)
        resp_types = [f[0] for f in frames]
        if RESP_OK not in resp_types:
            if RESP_ERROR in resp_types:
                print("FAILED: bridge returned ERROR")
                return 1
            print(f"FAILED: unexpected response {[hex(t) for t in resp_types]}")
            return 1
        print("OK")

        print("5. Verifying contact...", end=" ", flush=True)
        frames = send_cmd(sock, bytes([CMD_GET_CONTACTS]))
        if not find_contact(frames, TARGET_PUBKEY):
            print("FAILED: contact not found in GET_CONTACTS response")
            print(f"   Got {len(frames)} frames, types: {[hex(f[0]) for f in frames]}")
            return 1
        print("OK")

        print("6. Sending message...", end=" ", flush=True)
        msg_bytes = msg_text.encode("utf-8")
        ts_epoch = int(time.time())
        cmd_data = bytearray()
        cmd_data.append(0x00)  # msg_type = text
        cmd_data.append(0x00)  # attempt = 0
        cmd_data.extend(struct.pack("<I", ts_epoch))
        cmd_data.extend(TARGET_PUBKEY[:6])
        cmd_data.extend(msg_bytes)
        frames = send_cmd(sock, bytes([CMD_SEND_TXT_MSG]) + bytes(cmd_data))
        resp_types = [f[0] for f in frames]
        if RESP_MSG_SENT in resp_types:
            for f in frames:
                if f[0] == RESP_MSG_SENT and len(f) >= 6:
                    ack_tag = struct.unpack_from("<I", f, 2)[0]
                    print(f"OK (ack_tag={ack_tag})")
                    break
            else:
                print("OK")
        elif RESP_ERROR in resp_types:
            for f in frames:
                if f[0] == RESP_ERROR:
                    err_code = f[1] if len(f) > 1 else 0
                    print(f"FAILED: bridge returned ERROR code {err_code}")
                    if err_code == 2:
                        print("   Error 2 = contact not found (prefix mismatch)")
                    break
            return 1
        else:
            print(f"FAILED: unexpected {[hex(t) for t in resp_types]}")
            return 1

        print()
        print(f"SUCCESS: message '{msg_text}' sent to {TARGET_NAME}")
        print("Check your companion device for the message.")
        if our_pubkey:
            print("\nOur pubkey (add as contact on companion if needed):")
            print(f"  {our_pubkey.hex()}")
    finally:
        sock.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
