#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""MeshCore contact / business-card URI builders.

Two flavours:

  - ``build_contact_uri`` — the QR-friendly form documented in
    MeshCore/docs/qr_codes.md (``meshcore://contact/add?...``); produced
    by ``meshcore-tx advert --qr`` and parsed by the mobile companion
    apps.
  - ``build_bizcard_uri`` — the meshcore-cli ``import_contact`` form
    (``meshcore://<hex>``) where the hex blob is a raw ADVERT wire
    packet; the CLI calls ``bytes.fromhex(uri[11:])`` and feeds the
    result into ``importContact()``.
"""

from __future__ import annotations

from urllib.parse import quote

# ADVERT node types (used as default in URI builder).
ADVERT_NODE_CHAT = 0x01


def build_contact_uri(
    pub_key: bytes, name: str, node_type: int = ADVERT_NODE_CHAT
) -> str:
    """Build a MeshCore contact URI for QR scanning by the companion app.

    Format: ``meshcore://contact/add?name=<name>&public_key=<64hex>&type=<int>``
    """
    params: list[str] = []
    if name:
        params.append(f"name={quote(name, safe='')}")
    params.append(f"public_key={pub_key.hex()}")
    params.append(f"type={node_type}")
    return "meshcore://contact/add?" + "&".join(params)


def build_bizcard_uri(advert_packet: bytes) -> str:
    """Build a MeshCore CLI biz-card URI from a raw ADVERT wire packet.

    Format: ``meshcore://<hex-encoded raw ADVERT packet>``

    Used by ``meshcore-cli import_contact`` and the firmware companion
    serial / BLE protocol. The CLI calls ``bytes.fromhex(uri[11:])``
    and feeds the result into ``importContact()`` which deserialises it
    as a Packet.
    """
    return "meshcore://" + advert_packet.hex()
