#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Helper that turns ``decoders_enabled`` + ``decoders_options`` into a
ready-to-run tuple of decoder instances.

Lives in its own module so :mod:`lora.daemon.main` (initial daemon
construction) and :mod:`lora.daemon.hot_reload` (Phase 2F SIGHUP) share
exactly the same instantiation logic. The catch-all
:class:`~lora.decoders.RawDecoder` is appended automatically when the
configured set doesn't include it, so unknown sync words always reach
*some* decoder.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from lora.decoders import REGISTRY, RawDecoder
from lora.decoders.base import Decoder

_log = logging.getLogger("lora.daemon._decoders")


def build_decoders(
    enabled: tuple[str, ...] | list[str],
    options: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[Decoder, ...]:
    """Instantiate decoders in the order specified by ``enabled``.

    ``options`` (a mapping of decoder name -> kwargs dict) is applied
    via ``cls(**opts)``. Decoders accepting ``**options`` (added in
    Phase 2F) consume any unknown keys and stash them on
    ``self.options``. If a class refuses kwargs (e.g. a third-party
    decoder predating Phase 2F) we fall back to ``cls()`` and emit a
    WARN.

    Unknown names in ``enabled`` are skipped with a WARN. The catch-all
    :class:`RawDecoder` is appended if absent so unknown sync words
    still reach a decoder.
    """
    opts_map: Mapping[str, Mapping[str, Any]] = options or {}
    out: list[Decoder] = []
    for name in enabled:
        cls = REGISTRY.get(name)
        if cls is None:
            _log.warning("unknown decoder %r — skipped", name)
            continue
        kwargs: Mapping[str, Any] = opts_map.get(name, {})
        try:
            inst = cls(**dict(kwargs))
        except TypeError as exc:
            _log.warning(
                "decoder %r rejected options %r (%s); falling back to no-args",
                name,
                dict(kwargs),
                exc,
            )
            inst = cls()
        out.append(inst)
    if not any(isinstance(d, RawDecoder) for d in out):
        out.append(RawDecoder())
    _log.info(
        "decoders enabled: [%s]",
        ", ".join(type(d).__name__ for d in out),
    )
    return tuple(out)
