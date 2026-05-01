#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Decoder protocol contract for the lora-core daemon's protocol layer.

The daemon's aggregator emits :class:`lora.core.types.LoraFrame` objects.
The decoder layer attaches a :class:`lora.core.types.ProtocolAnnotation`
sub-map to each one based on the frame's sync_word, by running every
registered :class:`Decoder` whose ``sync_words`` matches (or whose
``sync_words`` is empty — catch-all decoders run last).

Invariants
----------
- Decoders are STATELESS per call. All mutable state (key material,
  channel PSKs, learned-pubkey cache) flows in via :class:`DecodeContext`.
- Decoders MUST NOT mutate the input :class:`LoraFrame`. They return a
  fresh :class:`ProtocolAnnotation` instance.
- Decoders SHOULD never raise. The daemon wraps every call in
  :func:`safe_run` as defence-in-depth, but each decoder should still
  catch its own internal parsing errors and return ``ok=False`` with
  a short ``error`` tag.

The ``DecodeContext.identity`` field is a placeholder for Phase 2A.
Phase 2B introduces the real ``lora.identity.IdentityStore``. Decoders
that need keys (currently only :mod:`lora.decoders.meshcore`) define a
narrow Protocol describing exactly which attributes they read from the
identity object, and duck-type against it. This keeps the Phase 2A
boundary small and lets Phase 2B swap in a richer identity store
without touching the decoder Protocol itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from lora.core.types import LoraFrame, ProtocolAnnotation


@dataclass(frozen=True, slots=True)
class DecodeContext:
    """Per-decode-call context passed to every :class:`Decoder`.

    ``identity`` is intentionally typed as :class:`object` for Phase 2A.
    Decoders that need key material (e.g. :class:`MeshCoreDecoder`)
    define their own narrow Protocol describing the attributes they
    require and duck-type against it. Phase 2B replaces this with a
    typed ``IdentityStore`` reference.
    """

    identity: object
    log: logging.Logger


@runtime_checkable
class Decoder(Protocol):
    """Protocol every protocol-decoder must implement.

    ``name`` matches the REGISTRY key (see :mod:`lora.decoders`) and is
    embedded in the resulting :class:`ProtocolAnnotation.name`.

    ``sync_words`` lists the sync words this decoder claims. The daemon
    dispatches a frame to every decoder whose tuple contains
    ``frame.phy.sync_word``. An empty tuple is a catch-all that runs
    last (only :class:`RawDecoder` should do this).
    """

    name: str
    sync_words: tuple[int, ...]

    def process(
        self, frame: LoraFrame, ctx: DecodeContext
    ) -> ProtocolAnnotation | None:
        """Decode ``frame`` and return an annotation, or ``None`` to skip.

        Returning ``None`` means "this decoder declines to annotate this
        frame" — the daemon will try the next matching decoder.
        Returning ``ProtocolAnnotation(ok=False, error=...)`` records a
        recognised-but-malformed frame and stops further dispatch.
        """
        ...


def safe_run(
    decoder: Decoder, frame: LoraFrame, ctx: DecodeContext
) -> ProtocolAnnotation:
    """Run ``decoder.process`` with a try/except guard.

    Decoders are expected to catch their own parsing errors. This
    wrapper exists so a buggy decoder cannot bring down the daemon's
    decode pipeline. On exception we return a failure annotation
    tagged with the exception's class name (no traceback in the
    annotation — the daemon's logger captures that separately).
    """
    try:
        result = decoder.process(frame, ctx)
    except Exception as exc:  # noqa: BLE001 — defence-in-depth wrapper
        ctx.log.exception("decoder %r raised on frame seq=%d", decoder.name, frame.seq)
        return ProtocolAnnotation(name=decoder.name, ok=False, error=type(exc).__name__)
    if result is None:
        # A decoder that declines to annotate is recorded as a no-op;
        # the daemon treats this distinctly from a real failure.
        return ProtocolAnnotation(name=decoder.name, ok=False, error="declined")
    return result
