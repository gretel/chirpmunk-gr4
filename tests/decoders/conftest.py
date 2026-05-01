# SPDX-License-Identifier: ISC
"""Test helpers for the lora.decoders test suite.

Provides a :func:`make_frame` factory that builds a fully-populated
:class:`lora.core.types.LoraFrame` so individual tests don't have to
spell out 14 boilerplate fields each time, plus a :func:`make_ctx`
factory that builds a :class:`lora.decoders.base.DecodeContext` with a
silent logger.
"""

from __future__ import annotations

import logging

from lora.core.types import Carrier, LoraFrame, Phy
from lora.decoders.base import DecodeContext


def make_phy(sync_word: int = 0x12, sf: int = 8, bw: int = 62500) -> Phy:
    return Phy(sf=sf, bw=bw, cr=8, sync_word=sync_word, crc_valid=True)


def make_carrier(sync_word: int = 0x12, sf: int = 8, bw: int = 62500) -> Carrier:
    return Carrier(sync_word=sync_word, sf=sf, bw=bw, cr=8, ldro_cfg=False)


def make_frame(
    payload: bytes = b"",
    *,
    sync_word: int = 0x12,
    seq: int = 1,
    sf: int = 8,
    bw: int = 62500,
) -> LoraFrame:
    return LoraFrame(
        type="lora_frame",
        ts="2026-01-01T00:00:00Z",
        seq=seq,
        payload=payload,
        payload_len=len(payload),
        crc_valid=True,
        cr=8,
        is_downchirp=False,
        payload_hash=0,
        id="test",
        phy=make_phy(sync_word=sync_word, sf=sf, bw=bw),
        carrier=make_carrier(sync_word=sync_word, sf=sf, bw=bw),
        source="test",
    )


def make_ctx(identity: object = None) -> DecodeContext:
    log = logging.getLogger("test.decoders")
    log.addHandler(logging.NullHandler())
    return DecodeContext(identity=identity, log=log)
