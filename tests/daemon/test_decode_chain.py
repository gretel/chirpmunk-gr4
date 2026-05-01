# SPDX-License-Identifier: ISC
"""Tests for :mod:`lora.daemon.decode_chain`."""

from __future__ import annotations

import logging
import tempfile
from collections.abc import Mapping
from pathlib import Path

from lora.core.types import (
    Carrier,
    LoraFrame,
    Phy,
    ProtocolAnnotation,
)
from lora.daemon.decode_chain import DecodeChain
from lora.decoders import (
    LoRaWANDecoder,
    MeshCoreDecoder,
    RawDecoder,
)
from lora.identity import IdentityConfig, IdentityStore


def _make_identity_store() -> IdentityStore:
    tmp = Path(tempfile.mkdtemp())
    return IdentityStore(
        IdentityConfig(
            identity_file=tmp / "identity.bin",
            keys_dir=tmp / "keys",
            channels_dir=tmp / "channels",
            contacts_dir=tmp / "contacts",
        )
    )


def _frame(sync_word: int, payload: bytes = b"hi") -> LoraFrame:
    return LoraFrame(
        type="lora_frame",
        ts="2026-01-01T00:00:00+00:00",
        seq=1,
        payload=payload,
        payload_len=len(payload),
        crc_valid=True,
        cr=4,
        is_downchirp=False,
        payload_hash=0xDEADBEEF,
        id="00000000-0000-0000-0000-000000000001",
        phy=Phy(sf=8, bw=62500, cr=4, sync_word=sync_word, crc_valid=True),
        carrier=Carrier(sync_word=sync_word, sf=8, bw=62500, cr=4, ldro_cfg=False),
        source="radio_868",
    )


def test_dispatch_picks_meshcore_for_sync_0x12() -> None:
    chain = DecodeChain(
        decoders=[
            MeshCoreDecoder(),
            LoRaWANDecoder(),
            RawDecoder(),
        ],
        identity=_make_identity_store(),
    )
    out = chain.process(_frame(0x12))
    assert out.protocol is not None
    assert out.protocol.name == "meshcore"


def test_dispatch_picks_lorawan_for_sync_0x34() -> None:
    chain = DecodeChain(
        decoders=[
            MeshCoreDecoder(),
            LoRaWANDecoder(),
            RawDecoder(),
        ],
        identity=_make_identity_store(),
    )
    out = chain.process(_frame(0x34, payload=b"\x80" + b"\x00" * 11))
    assert out.protocol is not None
    assert out.protocol.name == "lorawan"


def test_dispatch_falls_back_to_raw_for_unknown_sync_word() -> None:
    chain = DecodeChain(
        decoders=[
            MeshCoreDecoder(),
            LoRaWANDecoder(),
            RawDecoder(),
        ],
        identity=_make_identity_store(),
    )
    out = chain.process(_frame(0x99))
    assert out.protocol is not None
    assert out.protocol.name == "raw"


class _ExplodingDecoder:
    name: str = "exploding"
    sync_words: tuple[int, ...] = (0x55,)

    def process(self, frame: LoraFrame, ctx: object) -> ProtocolAnnotation | None:
        raise RuntimeError("boom")


def test_safe_run_isolates_decoder_failure() -> None:
    """A buggy decoder MUST NOT propagate. Frame gets ok=False annotation."""
    chain = DecodeChain(
        decoders=[_ExplodingDecoder(), RawDecoder()],
        identity=_make_identity_store(),
        log=logging.getLogger("test.decode_chain"),
    )
    out = chain.process(_frame(0x55))
    assert out.protocol is not None
    assert out.protocol.name == "exploding"
    assert out.protocol.ok is False
    assert out.protocol.error == "RuntimeError"


def test_decoder_returning_none_yields_declined() -> None:
    """``decoder.process`` returning ``None`` is recorded as declined.

    Mirrors the ``safe_run`` contract in :mod:`lora.decoders.base`.
    """

    class _DeclineDecoder:
        name: str = "decline"
        sync_words: tuple[int, ...] = (0x77,)

        def process(self, frame: LoraFrame, ctx: object) -> ProtocolAnnotation | None:
            del frame, ctx
            return None

    chain = DecodeChain(
        decoders=[_DeclineDecoder(), RawDecoder()],
        identity=_make_identity_store(),
    )
    out = chain.process(_frame(0x77))
    assert out.protocol is not None
    assert out.protocol.name == "decline"
    assert out.protocol.ok is False


def test_protocol_fields_attached_for_real_decoder() -> None:
    """End-to-end sanity: frame goes through with a non-empty annotation."""
    chain = DecodeChain(
        decoders=[MeshCoreDecoder(), RawDecoder()],
        identity=_make_identity_store(),
    )
    out = chain.process(_frame(0x12))
    assert out.protocol is not None
    # Even on parse failure, the annotation has a name and ok flag.
    assert isinstance(out.protocol.fields, Mapping)
