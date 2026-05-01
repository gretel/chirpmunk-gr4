#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Catch-all decoder for frames whose sync_word is not claimed by any
protocol-specific decoder.

Annotates the frame with the sync_word in canonical hex form so that
downstream tools (DuckDB queries, dashboards) can group untyped traffic
without having to look at the raw byte themselves.
"""

from __future__ import annotations

from typing import Any

from lora.core.types import LoraFrame, ProtocolAnnotation
from lora.decoders.base import DecodeContext


class RawDecoder:
    """Catch-all decoder. Empty ``sync_words`` makes it dispatch-last."""

    name: str = "raw"
    sync_words: tuple[int, ...] = ()

    def __init__(self, **options: Any) -> None:
        # Accept arbitrary per-decoder options for forward-compat with
        # ``[core.decoders.raw].*`` config blocks. Phase 2F's hot
        # reload introspects ``self.options`` to confirm a rebuild.
        self.options: dict[str, Any] = dict(options)

    def process(
        self, frame: LoraFrame, ctx: DecodeContext
    ) -> ProtocolAnnotation | None:
        sw = frame.phy.sync_word
        return ProtocolAnnotation(
            name=self.name,
            ok=True,
            fields={"sync_word_hex": f"0x{sw:02X}"},
        )
