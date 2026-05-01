# SPDX-License-Identifier: ISC
"""Phase 2F :meth:`DecodeChain.reload` atomic-swap coverage.

The double-buffer + atomic-tuple swap is the foundation that lets the
SIGHUP-driven hot reload mutate the decoder set without halting the
pipeline. These tests pin down two invariants:

1. After ``reload``, ``process`` dispatches to the NEW set.
2. A concurrent ``reload`` does not blow up an in-flight ``process``
   loop running on a different thread.
"""

from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path

from lora.core.types import (
    Carrier,
    LoraFrame,
    Phy,
)
from lora.daemon.decode_chain import DecodeChain
from lora.decoders import (
    MeshCoreDecoder,
    RawDecoder,
)
from lora.identity import IdentityConfig, IdentityStore


def _identity() -> IdentityStore:
    tmp = Path(tempfile.mkdtemp())
    return IdentityStore(
        IdentityConfig(
            identity_file=tmp / "identity.bin",
            keys_dir=tmp / "keys",
            channels_dir=tmp / "channels",
            contacts_dir=tmp / "contacts",
        )
    )


def _frame(sync: int, payload: bytes = b"\x00\x00\x00") -> LoraFrame:
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
        phy=Phy(sf=8, bw=62500, cr=4, sync_word=sync, crc_valid=True),
        carrier=Carrier(sync_word=sync, sf=8, bw=62500, cr=4, ldro_cfg=False),
        source="radio_868",
    )


def test_reload_swaps_atomically() -> None:
    """Build a chain with [meshcore, raw], swap to [raw], confirm dispatch."""
    chain = DecodeChain(
        decoders=[MeshCoreDecoder(), RawDecoder()],
        identity=_identity(),
    )
    # Sanity — sync 0x12 hits meshcore before reload.
    out_before = chain.process(_frame(0x12))
    assert out_before.protocol is not None
    assert out_before.protocol.name == "meshcore"

    # Reload with a single-decoder set [raw]. RawDecoder claims any
    # sync (empty sync_words tuple = catch-all).
    new_set = (RawDecoder(),)
    chain.reload(new_set)

    # The internal tuple must be exactly the one we just installed.
    assert chain.decoders == new_set
    assert chain.reload_count == 1

    out_after = chain.process(_frame(0x12))
    assert out_after.protocol is not None
    assert out_after.protocol.name == "raw"


def test_reload_does_not_block_in_flight_process() -> None:
    """A reader thread looping ``process`` must not see a torn state.

    The acceptance criteria are:
    * No exception escapes ``process``.
    * Every iteration returns a non-None LoraFrame.
    * The chain's ``reload_count`` reflects the swaps that happened.
    """
    chain = DecodeChain(
        decoders=[MeshCoreDecoder(), RawDecoder()],
        identity=_identity(),
    )
    stop = threading.Event()
    errors: list[BaseException] = []
    iterations = 0
    iter_lock = threading.Lock()

    def reader() -> None:
        nonlocal iterations
        try:
            while not stop.is_set():
                out = chain.process(_frame(0x12))
                if out is None:  # pragma: no cover - defensive
                    raise AssertionError("process returned None")
                with iter_lock:
                    iterations += 1
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def swapper() -> None:
        try:
            for i in range(50):
                if i % 2 == 0:
                    chain.reload((MeshCoreDecoder(), RawDecoder()))
                else:
                    chain.reload((RawDecoder(),))
                time.sleep(0.001)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(2)]
    for t in threads:
        t.start()
    swap_thread = threading.Thread(target=swapper)
    swap_thread.start()

    # Run for ~200ms.
    time.sleep(0.2)
    stop.set()
    swap_thread.join(timeout=2.0)
    for t in threads:
        t.join(timeout=2.0)

    assert errors == [], f"unexpected exceptions: {errors!r}"
    assert iterations > 0
    assert chain.reload_count == 50
