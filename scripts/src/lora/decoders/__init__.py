# SPDX-License-Identifier: ISC
"""Protocol-decoder registry for the lora-core daemon.

The daemon iterates :data:`REGISTRY` for every aggregated frame and
dispatches to each decoder whose ``sync_words`` claim the frame's
sync_word (catch-all decoders run last).
"""

from __future__ import annotations

from lora.decoders.base import DecodeContext, Decoder, safe_run
from lora.decoders.lorawan import LoRaWANDecoder
from lora.decoders.meshcore import MeshCoreDecoder
from lora.decoders.raw import RawDecoder

REGISTRY: dict[str, type] = {
    "meshcore": MeshCoreDecoder,
    "lorawan": LoRaWANDecoder,
    "raw": RawDecoder,
}

__all__ = [
    "REGISTRY",
    "DecodeContext",
    "Decoder",
    "LoRaWANDecoder",
    "MeshCoreDecoder",
    "RawDecoder",
    "safe_run",
]
