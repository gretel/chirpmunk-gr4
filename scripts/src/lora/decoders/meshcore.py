#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""MeshCore v1 decoder.

Parses the MeshCore wire header (route, payload type, version,
transport codes, path) and — when key material is available via the
:class:`DecodeContext` — decrypts TXT_MSG, ANON_REQ and GRP_TXT
bodies.

Logic is ported from :mod:`scripts.apps.lora_decode_meshcore` (header
parsing) and :mod:`scripts.apps.lora_mon` (decryption dispatch).
Crypto primitives live in :mod:`lora.core.meshcore_crypto`.

Phase 2A boundary
-----------------
The ``DecodeContext.identity`` placeholder is duck-typed against the
narrow :class:`MeshCoreKeys` Protocol declared below. Phase 2B will
introduce a richer ``IdentityStore`` whose attributes already satisfy
this Protocol; no decoder changes will be required at that point.
"""

from __future__ import annotations

import struct
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from lora.core.constants import PAYLOAD_NAMES, ROUTE_NAMES
from lora.core.meshcore_crypto import (
    PAYLOAD_ACK,
    PAYLOAD_ADVERT,
    PAYLOAD_ANON_REQ,
    PAYLOAD_GRP_TXT,
    PAYLOAD_TXT,
    GroupChannel,
    extract_advert_name,
    extract_advert_pubkey,
    parse_meshcore_header,
    try_decrypt_anon_req,
    try_decrypt_grp_txt,
    try_decrypt_txt_msg,
)
from lora.core.types import LoraFrame, ProtocolAnnotation
from lora.decoders.base import DecodeContext

MESHCORE_SYNC_WORD = 0x12

# ADVERT app-data layout: [pubkey(32)][timestamp(4)][signature(64)][app_data]
_ADVERT_HEADER_LEN = 32 + 4 + 64
_ADVERT_HAS_LOCATION = 0x10
_ADVERT_HAS_FEATURE1 = 0x20
_ADVERT_HAS_FEATURE2 = 0x40
_ADVERT_HAS_NAME = 0x80


@runtime_checkable
class MeshCoreKeys(Protocol):
    """Narrow view of an identity store from the meshcore decoder's POV.

    Phase 2A accepts any object exposing these four attributes:

    - ``our_prv``: 64-byte expanded MeshCore private key, or ``None``.
    - ``our_pub``: 32-byte Ed25519 public key, or ``None``.
    - ``known_keys``: mapping of hex pubkey -> 32-byte public key.
    - ``channels``: sequence of :class:`GroupChannel` PSKs.

    A real ``IdentityStore`` (Phase 2B) satisfies the same shape; for
    tests a plain object with these four attributes works too.
    """

    our_prv: bytes | None
    our_pub: bytes | None
    known_keys: Mapping[str, bytes]
    channels: Sequence[GroupChannel]


def _route_name(route: int) -> str:
    return ROUTE_NAMES[route] if 0 <= route < len(ROUTE_NAMES) else f"?{route}"


def _payload_name(ptype: int) -> str:
    return PAYLOAD_NAMES[ptype] if 0 <= ptype < len(PAYLOAD_NAMES) else f"?{ptype}"


def _coerce_keys(identity: object) -> MeshCoreKeys | None:
    """Best-effort cast of the placeholder identity to MeshCoreKeys.

    Returns ``None`` if any required attribute is missing — the decoder
    then runs in header-only mode, leaving ``decrypted=False``. This
    keeps the daemon working even before Phase 2B's IdentityStore is
    wired in.
    """
    if identity is None:
        return None
    needed = ("our_prv", "our_pub", "known_keys", "channels")
    if not all(hasattr(identity, n) for n in needed):
        return None
    # mypy/pyright: at runtime the duck check above is the actual
    # contract; the runtime_checkable Protocol does the rest.
    if isinstance(identity, MeshCoreKeys):
        return identity
    return None


def _extract_advert_location(data: bytes) -> tuple[int, int] | None:
    """Return (lat_e6, lon_e6) from an ADVERT, or ``None`` if absent."""
    hdr = parse_meshcore_header(data)
    if hdr is None or hdr["ptype"] != PAYLOAD_ADVERT:
        return None
    off = hdr["off"]
    app_off = off + _ADVERT_HEADER_LEN
    if app_off >= len(data):
        return None
    flags = data[app_off]
    if not (flags & _ADVERT_HAS_LOCATION):
        return None
    loc_off = app_off + 1
    if loc_off + 8 > len(data):
        return None
    lat_e6, lon_e6 = struct.unpack_from("<ii", data, loc_off)
    return int(lat_e6), int(lon_e6)


def _path_bytes(data: bytes) -> bytes:
    """Extract the raw path-hash bytes from a MeshCore packet, or b''.

    ``parse_meshcore_header`` returns the offset where the application
    payload starts; the path bytes sit just before that, after the
    header byte and optional 4-byte transport-code block.
    """
    hdr = parse_meshcore_header(data)
    if hdr is None:
        return b""
    off = hdr["off"]
    has_transport = bool(hdr["has_transport"])
    path_byte_pos = 1 + (4 if has_transport else 0) + 1  # +1 skips path_len byte
    if path_byte_pos > off:
        return b""
    return bytes(data[path_byte_pos:off])


class MeshCoreDecoder:
    """MeshCore v1 decoder for sync_word 0x12.

    Header-only path always succeeds (route/payload_type/version always
    present). Decryption is best-effort: when keys are available and a
    decryption succeeds the resulting plaintext is added to ``fields``;
    a decryption *failure* is not an error — the wire packet is still a
    valid MeshCore frame.
    """

    name: str = "meshcore"
    sync_words: tuple[int, ...] = (MESHCORE_SYNC_WORD,)

    def __init__(self, *, decrypt: bool = True, **options: Any) -> None:
        """Construct a MeshCore decoder.

        ``decrypt``: when False, header parsing still runs but TXT /
        ANON_REQ / GRP_TXT plaintext extraction is skipped (Phase 2F
        adds this hook so operators can disable decryption at runtime
        via ``[core.decoders.meshcore].decrypt = false``).
        Other unknown kwargs are stashed in ``self.options`` for
        forward-compat with future per-decoder TOML keys.
        """
        self.decrypt: bool = bool(decrypt)
        self.options: dict[str, Any] = dict(options)
        self.options["decrypt"] = self.decrypt

    def process(
        self, frame: LoraFrame, ctx: DecodeContext
    ) -> ProtocolAnnotation | None:
        data = frame.payload
        hdr = parse_meshcore_header(data)
        if hdr is None:
            return ProtocolAnnotation(name=self.name, ok=False, error="header_parse")

        route = int(hdr["route"])
        ptype = int(hdr["ptype"])
        version = int(hdr["version"])
        path_len_byte = int(hdr["path_len"])
        has_transport = bool(hdr["has_transport"])

        fields: dict[str, object] = {
            "route": _route_name(route),
            "payload_type": _payload_name(ptype),
            "version": version,
            "path_len": path_len_byte,
        }

        path = _path_bytes(data)
        if path:
            fields["path"] = path

        if has_transport and len(data) >= 5:
            tc1, tc2 = struct.unpack_from("<HH", data, 1)
            fields["transport_codes"] = [int(tc1), int(tc2)]

        keys = _coerce_keys(ctx.identity) if self.decrypt else None
        decrypted = False

        if ptype == PAYLOAD_ADVERT:
            try:
                pubkey = extract_advert_pubkey(data)
                if pubkey is not None:
                    fields["sender_pubkey"] = bytes(pubkey)
                name = extract_advert_name(data)
                if name:
                    fields["sender_name"] = name
                loc = _extract_advert_location(data)
                if loc is not None:
                    fields["lat_e6"] = loc[0]
                    fields["lon_e6"] = loc[1]
            except Exception as exc:  # noqa: BLE001 — bound rest of decode
                ctx.log.debug("ADVERT field-extraction failed: %s", exc)

        elif ptype == PAYLOAD_TXT and keys is not None:
            if keys.our_prv is not None and keys.our_pub is not None:
                try:
                    res = try_decrypt_txt_msg(
                        data,
                        keys.our_prv,
                        keys.our_pub,
                        dict(keys.known_keys),
                    )
                except Exception as exc:  # noqa: BLE001
                    ctx.log.debug("TXT decrypt raised: %s", exc)
                    res = None
                if res is not None:
                    text, sender_pub = res
                    decrypted = True
                    fields["decrypted_text"] = text
                    fields["sender_pubkey"] = bytes(sender_pub)
                    # Best-effort name lookup using known_keys.
                    # Phase 2B's IdentityStore will provide a richer
                    # contact lookup (name / last_seen / etc.).

        elif ptype == PAYLOAD_ANON_REQ and keys is not None:
            if keys.our_prv is not None and keys.our_pub is not None:
                try:
                    res = try_decrypt_anon_req(data, keys.our_prv, keys.our_pub)
                except Exception as exc:  # noqa: BLE001
                    ctx.log.debug("ANON_REQ decrypt raised: %s", exc)
                    res = None
                if res is not None:
                    text, sender_pub = res
                    decrypted = True
                    fields["decrypted_text"] = text
                    fields["sender_pubkey"] = bytes(sender_pub)

        elif ptype == PAYLOAD_GRP_TXT and keys is not None and keys.channels:
            try:
                grp = try_decrypt_grp_txt(data, list(keys.channels))
            except Exception as exc:  # noqa: BLE001
                ctx.log.debug("GRP_TXT decrypt raised: %s", exc)
                grp = None
            if grp is not None:
                text, channel_name = grp
                decrypted = True
                fields["decrypted_text"] = text
                fields["channel"] = channel_name

        elif ptype == PAYLOAD_ACK:
            # ACK payload: dest_hash(1) + msg_hash(4)
            off = int(hdr["off"])
            if off + 1 + 4 <= len(data):
                fields["ack_hash"] = bytes(data[off + 1 : off + 5])

        fields["decrypted"] = decrypted
        return ProtocolAnnotation(name=self.name, ok=True, fields=fields)
