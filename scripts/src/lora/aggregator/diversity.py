#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Diversity-combining aggregator for typed :class:`LoraFrame` records.

This is the library-shaped successor to :mod:`scripts.apps.lora_agg` —
the legacy aggregator service is socket+selectors plumbing wrapped
around the same core dedup/ranking logic. This module exports the core
logic as a pure, sync, clock-injected component so the new daemon
(Phase 2 Task 2E) can wire it into its asyncio pipeline without
inheriting any I/O or signal handling.

Behaviour ported from legacy ``lora_agg.py``
--------------------------------------------

* Candidates group by ``(payload_hash, sync_word)``. SF is intentionally
  excluded from the dedup key — within a single OTA event all diversity
  paths see the same SF, and cross-SF same-payload transmissions are
  logically one message (ADR retries).
* The window starts at the first candidate's ``arrived_at`` and the
  group emits when ``now - window_start >= window_ms / 1000``.
* Ranking on emit: ``CRC_OK`` beats ``CRC_FAIL``, then highest
  ``snr_db`` wins. Frames with missing ``snr_db`` lose tie-breaks (the
  legacy ``-999.0`` sentinel is preserved for ranking only).

Multi-source addition (spec Section 3): each candidate carries a
``source`` string (the name from ``[[core.upstream]]`` — empty string
for legacy single-source flows). Candidates from different sources for
the same ``(payload_hash, sync_word)`` deduplicate into one group, and
the per-candidate origin is recorded in ``Diversity.rx_sources``.

``max_candidates`` cap policy
-----------------------------

Legacy ``lora_agg.py`` does not cap group sizes. This implementation
adds a cap (default 8) for memory bounding under burst load with a
**keep-best** policy: when an incoming candidate arrives at a full
group it is kept only if it strictly improves on the current worst
candidate (per :func:`is_better`); otherwise it is dropped. This
prioritises information quality over arrival order so the diversity
stats stay representative of the best receptions seen.

Late-feed edge
--------------

Callers normally interleave :meth:`DiversityAggregator.feed` and
:meth:`DiversityAggregator.flush_expired` from the same loop, so by
the time a "next-window" candidate arrives the previous group has
already been drained. If a candidate arrives whose ``arrived_at`` is
already past the matching pending group's window, :meth:`feed` closes
the stale group into an internal ready queue and starts a fresh group
for the incoming candidate. The next :meth:`flush_expired` /
:meth:`flush_all` drains the ready queue first.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from typing import Final

from lora.core.types import Diversity, LoraFrame

# ---------------------------------------------------------------------------
# FNV-1a 64-bit
# ---------------------------------------------------------------------------

# Constants match FrameSink::fnv1a64 in the C++ side and the helper in
# legacy ``lora_agg.py``. Pure function — independent of
# the Python interpreter's hash randomisation.
_FNV_OFFSET_BASIS: Final[int] = 14695981039346656037
_FNV_PRIME: Final[int] = 1099511628211
_FNV_MASK: Final[int] = 0xFFFFFFFFFFFFFFFF


def fnv1a64(data: bytes | bytearray) -> int:
    """Return the FNV-1a 64-bit hash of ``data``."""
    h = _FNV_OFFSET_BASIS
    for b in data:
        h ^= b
        h = (h * _FNV_PRIME) & _FNV_MASK
    return h


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

# Dedup key: (payload_hash, sync_word). See module docstring for rationale.
DedupKey = tuple[int, int]


@dataclass(frozen=True, slots=True)
class Candidate:
    """One decode of a frame from one chain on one source.

    ``payload_hash`` is the FNV-1a 64-bit hash of the payload bytes.
    ``arrived_at`` is monotonic seconds from a clock owned by the
    caller — the aggregator does not own a clock.
    ``source`` is the upstream name from ``[[core.upstream]]`` (empty
    string for legacy single-source flows).
    """

    payload_hash: int
    sync_word: int
    arrived_at: float
    source: str
    frame: LoraFrame


@dataclass(slots=True)
class PendingGroup:
    """Mutable group of :class:`Candidate` values sharing a :data:`DedupKey`.

    The group's window starts at ``created_at`` (= the first
    candidate's ``arrived_at``). It emits when
    ``now - created_at >= window_ms / 1000``.
    """

    key: DedupKey
    created_at: float
    candidates: list[Candidate] = field(default_factory=list)
    best_idx: int = 0

    @property
    def payload_hash(self) -> int:
        return self.key[0]

    @property
    def sync_word(self) -> int:
        return self.key[1]

    @property
    def winner(self) -> Candidate:
        return self.candidates[self.best_idx]


# ---------------------------------------------------------------------------
# Ranking + extraction helpers
# ---------------------------------------------------------------------------


def _snr_db(frame: LoraFrame) -> float:
    """Read SNR from the typed frame; missing → sentinel that loses tie-breaks.

    Legacy ``lora_agg`` used ``-999.0`` so missing-SNR frames consistently
    lose ranking ties. Preserved here.
    """
    snr = frame.phy.snr_db
    return snr if snr is not None else -999.0


def _snr_db_for_emit(frame: LoraFrame) -> float:
    """Read SNR for the emitted ``Diversity.snr_db`` array; missing → 0.0.

    Distinct from :func:`_snr_db` because the per-candidate emit array
    documents the receiver's reported SNR, not a sort key. The task
    spec defines ``c.frame.phy.snr_db or 0.0`` so consumers see 0.0
    (not -999) for absent-SNR candidates.
    """
    snr = frame.phy.snr_db
    return snr if snr is not None else 0.0


def is_better(a: Candidate, b: Candidate) -> bool:
    """Return ``True`` iff candidate ``a`` outranks ``b``.

    Ranking: ``crc_valid`` beats ``not crc_valid``, then highest
    ``snr_db`` wins. Mirrors legacy ``lora_agg.is_better``.
    """
    a_crc = a.frame.crc_valid
    b_crc = b.frame.crc_valid
    if a_crc != b_crc:
        return a_crc
    return _snr_db(a.frame) > _snr_db(b.frame)


def extract_candidate(
    frame: LoraFrame, arrived_at: float, source: str
) -> Candidate | None:
    """Lift a typed :class:`LoraFrame` into a :class:`Candidate`.

    Returns ``None`` only if the frame is missing the dedup fields.
    Typed frames produced by :mod:`lora.core.schema` always have
    ``payload_hash`` and ``carrier.sync_word``; the ``None`` branch
    exists for parity with the legacy dict-walking version.
    """
    return Candidate(
        payload_hash=frame.payload_hash,
        sync_word=frame.carrier.sync_word,
        arrived_at=arrived_at,
        source=source,
        frame=frame,
    )


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------


def build_aggregated(group: PendingGroup) -> LoraFrame:
    """Return the winning candidate's frame with the diversity sub-map filled.

    Diversity layout matches the legacy aggregator output and the
    Phase 2A :class:`lora.core.types.Diversity` schema (including the
    new ``rx_sources`` field).
    """
    winner = group.winner
    cands = group.candidates
    rx_channels = tuple(
        c.frame.rx_channel if c.frame.rx_channel is not None else -1 for c in cands
    )
    snr_values = tuple(_snr_db_for_emit(c.frame) for c in cands)
    crc_mask = sum(1 << i for i, c in enumerate(cands) if c.frame.crc_valid)
    gap_us = int((cands[-1].arrived_at - cands[0].arrived_at) * 1_000_000)
    source_ids = tuple(c.frame.id for c in cands)
    rx_sources = tuple(c.source for c in cands)

    diversity = Diversity(
        n_candidates=len(cands),
        decoded_channel=winner.frame.rx_channel,
        rx_channels=rx_channels,
        snr_db=snr_values,
        crc_mask=crc_mask,
        gap_us=max(0, gap_us),
        source_ids=source_ids,
        rx_sources=rx_sources,
    )
    return replace(winner.frame, diversity=diversity)


def format_ruling(group: PendingGroup) -> str:
    """One-line ruling summary for log output (legacy parity).

    Multi-source extension: when a group has more than one candidate,
    the per-candidate ``source`` strings are appended so site logs
    show which upstream radios contributed to each merged frame.
    """
    winner = group.winner
    f = winner.frame
    crc_str = "CRC_OK" if f.crc_valid else "CRC_FAIL"
    n = len(group.candidates)
    n_ok = sum(1 for c in group.candidates if c.frame.crc_valid)
    parts = [f"FWD #{f.seq} {f.payload_len}B {crc_str}"]
    snr = f.phy.snr_db
    if snr is not None:
        parts.append(f"SNR={snr:.1f}dB")
    if f.rx_channel is not None:
        parts.append(f"ch={f.rx_channel}")
    parts.append(f"sync=0x{f.carrier.sync_word:02X}")
    if f.decode_label:
        parts.append(f"[{f.decode_label}]")
    parts.append(f"hash={group.payload_hash:016x}")
    if n > 1:
        parts.append(f"[best of {n}, {n_ok} CRC_OK]")
        sources = ",".join(c.source for c in group.candidates)
        parts.append(f"src={sources}")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# DiversityAggregator
# ---------------------------------------------------------------------------


class DiversityAggregator:
    """Stateful aggregator over typed :class:`LoraFrame` records.

    Push candidates with :meth:`feed`. Call :meth:`flush_expired`
    periodically (or :meth:`flush_all` on shutdown) to drain mature
    groups. The aggregator does not own a clock — pass monotonic
    seconds in via ``Candidate.arrived_at`` and ``flush_expired``'s
    ``now`` parameter.
    """

    __slots__ = ("_window_s", "_max_candidates", "_pending", "_ready")

    def __init__(self, *, window_ms: int = 200, max_candidates: int = 8) -> None:
        if window_ms <= 0:
            raise ValueError(f"window_ms must be positive, got {window_ms}")
        if max_candidates <= 0:
            raise ValueError(f"max_candidates must be positive, got {max_candidates}")
        self._window_s: float = window_ms / 1000.0
        self._max_candidates: int = max_candidates
        self._pending: dict[DedupKey, PendingGroup] = {}
        # Late-feed edge: groups closed by feed() because a new candidate
        # arrived past their window — drained next flush.
        self._ready: list[PendingGroup] = []

    @property
    def pending_count(self) -> int:
        """Total groups awaiting emission (active pending + ready-to-drain)."""
        return len(self._pending) + len(self._ready)

    def feed(self, candidate: Candidate) -> None:
        """Insert a candidate into the matching pending group.

        Creates a new group if none exists for ``(payload_hash,
        sync_word)``. When the group is at ``max_candidates`` capacity,
        the new candidate is kept only if it strictly outranks the
        current worst (keep-best policy).

        Late-feed edge: if the existing group's window has already
        elapsed by ``candidate.arrived_at``, the stale group is moved
        to the ready queue and a fresh group is started for the
        incoming candidate.
        """
        key: DedupKey = (candidate.payload_hash, candidate.sync_word)
        group = self._pending.get(key)
        if group is not None and (
            candidate.arrived_at - group.created_at >= self._window_s
        ):
            self._ready.append(group)
            del self._pending[key]
            group = None

        if group is None:
            new_group = PendingGroup(key=key, created_at=candidate.arrived_at)
            new_group.candidates.append(candidate)
            new_group.best_idx = 0
            self._pending[key] = new_group
            return

        if len(group.candidates) < self._max_candidates:
            group.candidates.append(candidate)
            if is_better(candidate, group.candidates[group.best_idx]):
                group.best_idx = len(group.candidates) - 1
            return

        # Cap reached — keep-best policy.
        worst_idx = self._worst_index(group)
        if is_better(candidate, group.candidates[worst_idx]):
            group.candidates[worst_idx] = candidate
            self._recompute_best(group)

    def flush_expired(self, now: float) -> Iterator[LoraFrame]:
        """Yield aggregated frames for groups whose window has elapsed.

        ``now`` is monotonic seconds (the same clock the caller used
        for ``Candidate.arrived_at``).
        """
        # Drain anything closed by feed() during late-arrival handling.
        while self._ready:
            yield build_aggregated(self._ready.pop(0))
        # Then normally expired pending groups.
        expired = [
            k
            for k, g in self._pending.items()
            if (now - g.created_at) >= self._window_s
        ]
        for k in expired:
            yield build_aggregated(self._pending.pop(k))

    def flush_all(self) -> Iterator[LoraFrame]:
        """Drain every pending group regardless of window — for shutdown."""
        while self._ready:
            yield build_aggregated(self._ready.pop(0))
        for k in list(self._pending.keys()):
            yield build_aggregated(self._pending.pop(k))

    @staticmethod
    def _worst_index(group: PendingGroup) -> int:
        """Index of the lowest-ranked candidate per :func:`is_better`."""
        worst_idx = 0
        for i in range(1, len(group.candidates)):
            if is_better(group.candidates[worst_idx], group.candidates[i]):
                worst_idx = i
        return worst_idx

    @staticmethod
    def _recompute_best(group: PendingGroup) -> None:
        best_idx = 0
        for i in range(1, len(group.candidates)):
            if is_better(group.candidates[i], group.candidates[best_idx]):
                best_idx = i
        group.best_idx = best_idx
