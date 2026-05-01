#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Routes a :class:`~lora.core.types.LoraFrame` through registered decoders.

Phase 2F upgrades the simple list registry to a double-buffered
atomic-swap pattern so :meth:`reload` may be called from any thread
without disturbing in-flight :meth:`process` calls. Internally the
chain holds an immutable ``tuple[Decoder, ...]`` that ``process``
captures ONCE per call (a single attribute load, atomic in CPython);
``reload`` simply rebinds the attribute to a new tuple. Readers see
either the old or the new tuple — never a torn intermediate state.

Matching rule
-------------
A decoder ``d`` matches if ``frame.phy.sync_word in d.sync_words`` OR
``d.sync_words == ()`` (catch-all). The first match in iteration order
wins, so callers should place catch-all decoders (notably
:class:`~lora.decoders.RawDecoder`) at the end of the tuple.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace

from lora.core.types import LoraFrame
from lora.decoders.base import DecodeContext, Decoder, safe_run
from lora.identity import IdentityStore

_log = logging.getLogger("lora.daemon.decode_chain")


class DecodeChain:
    """Dispatches a :class:`LoraFrame` to the first matching decoder.

    The decoder set is held as an immutable ``tuple`` in
    :attr:`_decoders`. :meth:`process` reads the attribute exactly once
    per call so a concurrent :meth:`reload` cannot make a single call
    see a partially-updated set. In CPython attribute load + tuple
    iteration on the captured reference is safe with respect to a
    concurrent rebind.
    """

    __slots__ = ("_decoders", "_ctx", "_reload_count")

    def __init__(
        self,
        decoders: Sequence[Decoder],
        identity: IdentityStore,
        *,
        log: logging.Logger | None = None,
    ) -> None:
        self._decoders: tuple[Decoder, ...] = tuple(decoders)
        self._ctx = DecodeContext(identity=identity, log=log or _log)
        self._reload_count: int = 0

    # ------------------------------------------------------------------
    # Hot path
    # ------------------------------------------------------------------

    def process(self, frame: LoraFrame) -> LoraFrame:
        """Return ``frame`` with its ``protocol`` field populated.

        If no decoder claims the frame's sync_word and no catch-all is
        registered, the frame is returned unchanged.
        """
        # Snapshot the tuple ONCE — concurrent reload() will rebind
        # the attribute but cannot alter the tuple we just captured.
        decoders = self._decoders
        sync = frame.phy.sync_word
        for dec in decoders:
            if not dec.sync_words or sync in dec.sync_words:
                annotation = safe_run(dec, frame, self._ctx)
                return replace(frame, protocol=annotation)
        return frame

    # ------------------------------------------------------------------
    # Reload (Phase 2F)
    # ------------------------------------------------------------------

    def reload(self, new_decoders: Sequence[Decoder]) -> None:
        """Atomically swap in a new decoder tuple.

        Safe to call from any thread. Readers in :meth:`process` see
        either the old or the new tuple — never a torn state.
        In-flight calls finish on whichever tuple they captured.
        """
        # Building the tuple before the swap means the rebind itself
        # is a single attribute store; CPython's GIL makes this atomic
        # with respect to the read in process().
        new_tuple: tuple[Decoder, ...] = tuple(new_decoders)
        self._decoders = new_tuple
        self._reload_count += 1

    @property
    def reload_count(self) -> int:
        """Number of times :meth:`reload` has been called."""
        return self._reload_count

    @property
    def decoders(self) -> tuple[Decoder, ...]:
        """Current decoder tuple (atomic snapshot)."""
        return self._decoders
