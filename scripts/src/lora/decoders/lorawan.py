#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""LoRaWAN MAC-layer header decoder (cleartext fields only).

The FRMPayload of a data frame is AES-128 encrypted under the
device's NwkSKey/AppSKey; we do not have those keys and only decode
the cleartext envelope. Ported from
:mod:`scripts.apps.lora_decode_lorawan`.
"""

from __future__ import annotations

import struct
from typing import Any

from lora.core.types import LoraFrame, ProtocolAnnotation
from lora.decoders.base import DecodeContext

LORAWAN_SYNC_WORD = 0x34

_MTYPE_NAMES: tuple[str, ...] = (
    "JoinRequest",
    "JoinAccept",
    "UnconfDataUp",
    "UnconfDataDown",
    "ConfDataUp",
    "ConfDataDown",
    "RFU",
    "Proprietary",
)

# MType values that are uplinks vs data vs join.
_UPLINK_MTYPES = frozenset({0, 2, 4})
_DATA_MTYPES = frozenset({2, 3, 4, 5})
_JOIN_MTYPES = frozenset({0, 1})


def _mtype_name(mtype: int) -> str:
    if 0 <= mtype < len(_MTYPE_NAMES):
        return _MTYPE_NAMES[mtype]
    return f"?{mtype}"


class LoRaWANDecoder:
    """LoRaWAN R1 MAC frame header decoder (sync_word 0x34)."""

    name: str = "lorawan"
    sync_words: tuple[int, ...] = (LORAWAN_SYNC_WORD,)

    def __init__(self, **options: Any) -> None:
        self.options: dict[str, Any] = dict(options)

    def process(
        self, frame: LoraFrame, ctx: DecodeContext
    ) -> ProtocolAnnotation | None:
        data = frame.payload
        # MHDR(1) + MIC(4) is the absolute minimum.
        if len(data) < 5:
            return ProtocolAnnotation(
                name=self.name, ok=False, error="payload_too_short"
            )

        mhdr = data[0]
        mtype = (mhdr >> 5) & 0x07
        # Last 4 bytes are the MIC, always present.
        mic = bytes(data[-4:])
        mac_payload = data[1:-4]

        is_uplink = mtype in _UPLINK_MTYPES
        is_data = mtype in _DATA_MTYPES
        is_join = mtype in _JOIN_MTYPES

        fields: dict[str, object] = {
            "mtype": _mtype_name(mtype),
            "is_uplink": is_uplink,
            "is_data": is_data,
            "is_join": is_join,
            "mic": mic,
        }

        if is_data:
            # FHDR = DevAddr(4 LE) + FCtrl(1) + FCnt(2 LE) + FOpts(0..15)
            if len(mac_payload) < 7:
                return ProtocolAnnotation(
                    name=self.name, ok=False, error="fhdr_truncated"
                )

            # DevAddr is little-endian on the wire; spec field is the
            # network byte order (big-endian) hex string with no prefix.
            dev_addr_le = struct.unpack_from("<I", mac_payload, 0)[0]
            fctrl = mac_payload[4]
            fcnt = struct.unpack_from("<H", mac_payload, 5)[0]
            fopts_len = fctrl & 0x0F
            fhdr_len = 7 + fopts_len
            if fhdr_len > len(mac_payload):
                return ProtocolAnnotation(
                    name=self.name, ok=False, error="fopts_truncated"
                )

            adr = bool(fctrl & 0x80)
            ack = bool(fctrl & 0x20)
            # Bit 6 / bit 4 are direction-dependent.
            adr_ack_req = bool(fctrl & 0x40) if is_uplink else False
            fpending = bool(fctrl & 0x10) if not is_uplink else False

            fctrl_map: dict[str, object] = {
                "adr": adr,
                "ack": ack,
                "adr_ack_req": adr_ack_req,
                "fpending": fpending,
                "fopts_len": fopts_len,
            }

            fields["dev_addr"] = f"{dev_addr_le:08x}"
            fields["fcnt"] = fcnt
            fields["fctrl"] = fctrl_map

            remaining = mac_payload[fhdr_len:]
            if remaining:
                fields["fport"] = remaining[0]

        return ProtocolAnnotation(name=self.name, ok=True, fields=fields)
