#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""MeshCore protocol display-name tables.

Two parallel arrays indexed by the 2-bit `route_type` and 4-bit
`payload_type` fields of the MeshCore wire header. Used by formatters,
decoders, and offline replay tools that need a human-readable label
for a route or payload code without depending on the full crypto stack.
"""

from __future__ import annotations

ROUTE_NAMES: list[str] = ["T_FLOOD", "FLOOD", "DIRECT", "T_DIRECT"]

PAYLOAD_NAMES: list[str] = [
    "REQ",
    "RESP",
    "TXT",
    "ACK",
    "ADVERT",
    "GRP_TXT",
    "GRP_DATA",
    "ANON_REQ",
    "PATH",
    "TRACE",
    "MULTI",
    "CTRL",
    "rsv12",
    "rsv13",
    "rsv14",
    "RAW_CUSTOM",
]


def sync_word_name(sw: int) -> str:
    """Format sync word as hex string (e.g. 0x12 -> '0x12')."""
    return f"0x{sw:02X}"
