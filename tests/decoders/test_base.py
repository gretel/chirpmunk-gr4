# SPDX-License-Identifier: ISC
"""Tests for the Decoder Protocol contract and :func:`safe_run`."""

from __future__ import annotations

from lora.core.types import LoraFrame, ProtocolAnnotation
from lora.decoders.base import DecodeContext, Decoder, safe_run

from .conftest import make_ctx, make_frame


class _OkDecoder:
    name = "ok"
    sync_words: tuple[int, ...] = (0x12,)

    def process(
        self, frame: LoraFrame, ctx: DecodeContext
    ) -> ProtocolAnnotation | None:
        return ProtocolAnnotation(name=self.name, ok=True, fields={"x": 1})


class _RaisingDecoder:
    name = "boom"
    sync_words: tuple[int, ...] = (0x12,)

    def process(
        self, frame: LoraFrame, ctx: DecodeContext
    ) -> ProtocolAnnotation | None:
        raise ValueError("synthetic")


class _DecliningDecoder:
    name = "decline"
    sync_words: tuple[int, ...] = (0x12,)

    def process(
        self, frame: LoraFrame, ctx: DecodeContext
    ) -> ProtocolAnnotation | None:
        return None


def test_protocol_runtime_check() -> None:
    assert isinstance(_OkDecoder(), Decoder)
    assert isinstance(_RaisingDecoder(), Decoder)


def test_safe_run_passes_through_normal_return() -> None:
    frame = make_frame(b"\x00")
    ctx = make_ctx()
    ann = safe_run(_OkDecoder(), frame, ctx)
    assert ann.name == "ok"
    assert ann.ok is True
    assert ann.fields == {"x": 1}
    assert ann.error is None


def test_safe_run_wraps_exception_into_failure_annotation() -> None:
    frame = make_frame(b"\x00")
    ctx = make_ctx()
    ann = safe_run(_RaisingDecoder(), frame, ctx)
    assert ann.name == "boom"
    assert ann.ok is False
    assert ann.error == "ValueError"


def test_safe_run_treats_none_as_declined() -> None:
    frame = make_frame(b"\x00")
    ctx = make_ctx()
    ann = safe_run(_DecliningDecoder(), frame, ctx)
    assert ann.name == "decline"
    assert ann.ok is False
    assert ann.error == "declined"
