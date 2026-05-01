# SPDX-License-Identifier: ISC
"""Tests for :class:`lora.decoders.raw.RawDecoder`."""

from __future__ import annotations

import pytest

from lora.decoders.raw import RawDecoder

from .conftest import make_ctx, make_frame


@pytest.mark.parametrize("sw", [0x00, 0x12, 0x2B, 0x34, 0xAB, 0xFF])
def test_raw_annotates_sync_word_hex(sw: int) -> None:
    decoder = RawDecoder()
    frame = make_frame(b"\x01\x02", sync_word=sw)
    ann = decoder.process(frame, make_ctx())
    assert ann is not None
    assert ann.name == "raw"
    assert ann.ok is True
    assert ann.fields == {"sync_word_hex": f"0x{sw:02X}"}


def test_raw_is_catch_all() -> None:
    decoder = RawDecoder()
    assert decoder.sync_words == ()
