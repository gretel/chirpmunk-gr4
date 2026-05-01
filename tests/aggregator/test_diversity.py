# SPDX-License-Identifier: ISC
"""Tests for :mod:`lora.aggregator.diversity`.

Covers the dedup grouping, ranking, multi-source ``rx_sources``
emission, the ``max_candidates`` cap policy, the late-feed window
boundary edge, and the FNV-1a 64-bit hash known vectors.
"""

from __future__ import annotations

from lora.aggregator import Candidate, DiversityAggregator, PendingGroup
from lora.aggregator.diversity import (
    build_aggregated,
    extract_candidate,
    fnv1a64,
    format_ruling,
    is_better,
)
from lora.core.types import Carrier, LoraFrame, Phy

# ---------------------------------------------------------------------------
# Fixture builders (local to this test file — see task spec)
# ---------------------------------------------------------------------------


def _phy(
    *, snr_db: float | None = 5.0, sync_word: int = 0x12, crc_valid: bool = True
) -> Phy:
    return Phy(
        sf=8, bw=62500, cr=4, sync_word=sync_word, crc_valid=crc_valid, snr_db=snr_db
    )


def _carrier(*, sync_word: int = 0x12) -> Carrier:
    return Carrier(sync_word=sync_word, sf=8, bw=62500, cr=4, ldro_cfg=False)


def _frame(
    *,
    payload: bytes = b"hello",
    sync_word: int = 0x12,
    crc_valid: bool = True,
    snr_db: float | None = 5.0,
    rx_channel: int | None = 0,
    source: str = "radio_868",
    seq: int = 1,
    id_: str = "00000000-0000-0000-0000-000000000001",
) -> LoraFrame:
    return LoraFrame(
        type="lora_frame",
        ts="2026-01-01T00:00:00Z",
        seq=seq,
        payload=payload,
        payload_len=len(payload),
        crc_valid=crc_valid,
        cr=4,
        is_downchirp=False,
        payload_hash=fnv1a64(payload),
        id=id_,
        phy=_phy(snr_db=snr_db, sync_word=sync_word, crc_valid=crc_valid),
        carrier=_carrier(sync_word=sync_word),
        source=source,
        rx_channel=rx_channel,
    )


def _candidate(
    *,
    payload: bytes = b"hello",
    sync_word: int = 0x12,
    crc_valid: bool = True,
    snr_db: float | None = 5.0,
    rx_channel: int | None = 0,
    source: str = "radio_868",
    arrived_at: float = 0.0,
    seq: int = 1,
    id_: str = "00000000-0000-0000-0000-000000000001",
) -> Candidate:
    f = _frame(
        payload=payload,
        sync_word=sync_word,
        crc_valid=crc_valid,
        snr_db=snr_db,
        rx_channel=rx_channel,
        source=source,
        seq=seq,
        id_=id_,
    )
    cand = extract_candidate(f, arrived_at=arrived_at, source=source)
    assert cand is not None
    return cand


# ---------------------------------------------------------------------------
# fnv1a64
# ---------------------------------------------------------------------------


def test_fnv1a64_known_vector() -> None:
    """Canonical FNV-1a 64-bit test vectors."""
    assert fnv1a64(b"") == 0xCBF29CE484222325
    assert fnv1a64(b"a") == 0xAF63DC4C8601EC8C
    assert fnv1a64(b"hello") == 0xA430D84680AABD0B


# ---------------------------------------------------------------------------
# Single-candidate window emission
# ---------------------------------------------------------------------------


def test_single_candidate_emits_after_window() -> None:
    agg = DiversityAggregator(window_ms=200, max_candidates=8)
    agg.feed(_candidate(arrived_at=0.0, source="radio_868"))

    # Before window — nothing emitted.
    assert list(agg.flush_expired(now=0.1)) == []
    assert agg.pending_count == 1

    # At/after window — one frame.
    out = list(agg.flush_expired(now=0.25))
    assert len(out) == 1
    frame = out[0]
    assert frame.diversity is not None
    assert frame.diversity.n_candidates == 1
    assert tuple(frame.diversity.rx_sources) == ("radio_868",)
    assert agg.pending_count == 0


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------


def test_two_candidates_same_hash_dedup_into_one_group() -> None:
    agg = DiversityAggregator(window_ms=200)
    agg.feed(_candidate(arrived_at=0.0, source="radio_868"))
    agg.feed(_candidate(arrived_at=0.05, source="radio_868"))

    assert agg.pending_count == 1
    out = list(agg.flush_expired(now=0.25))
    assert len(out) == 1
    div = out[0].diversity
    assert div is not None
    assert div.n_candidates == 2


def test_distinct_payload_hashes_form_separate_groups() -> None:
    agg = DiversityAggregator(window_ms=200)
    agg.feed(_candidate(arrived_at=0.0, payload=b"hello"))
    agg.feed(_candidate(arrived_at=0.0, payload=b"world"))

    assert agg.pending_count == 2
    out = list(agg.flush_expired(now=0.25))
    assert len(out) == 2
    assert {f.payload for f in out} == {b"hello", b"world"}


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def test_crc_ok_wins_over_crc_fail() -> None:
    agg = DiversityAggregator(window_ms=200)
    # CRC_OK at low SNR vs CRC_FAIL at high SNR — CRC_OK must win.
    agg.feed(
        _candidate(
            arrived_at=0.0,
            crc_valid=True,
            snr_db=-5.0,
            rx_channel=1,
            id_="00000000-0000-0000-0000-00000000000a",
        )
    )
    agg.feed(
        _candidate(
            arrived_at=0.05,
            crc_valid=False,
            snr_db=20.0,
            rx_channel=2,
            id_="00000000-0000-0000-0000-00000000000b",
        )
    )

    out = list(agg.flush_expired(now=0.25))
    assert len(out) == 1
    winner = out[0]
    assert winner.crc_valid is True
    assert winner.rx_channel == 1
    assert winner.diversity is not None
    assert winner.diversity.decoded_channel == 1
    # crc_mask: candidate 0 OK, candidate 1 FAIL → 0b01.
    assert winner.diversity.crc_mask == 0b01


def test_crc_tie_higher_snr_wins() -> None:
    agg = DiversityAggregator(window_ms=200)
    agg.feed(
        _candidate(
            arrived_at=0.0,
            crc_valid=True,
            snr_db=3.0,
            rx_channel=1,
            id_="00000000-0000-0000-0000-00000000000a",
        )
    )
    agg.feed(
        _candidate(
            arrived_at=0.05,
            crc_valid=True,
            snr_db=15.0,
            rx_channel=2,
            id_="00000000-0000-0000-0000-00000000000b",
        )
    )

    out = list(agg.flush_expired(now=0.25))
    assert len(out) == 1
    winner = out[0]
    assert winner.rx_channel == 2
    assert winner.diversity is not None
    assert winner.diversity.decoded_channel == 2


# ---------------------------------------------------------------------------
# Window boundary (late-feed edge)
# ---------------------------------------------------------------------------


def test_window_boundary_emits_separate_groups() -> None:
    agg = DiversityAggregator(window_ms=200)
    agg.feed(_candidate(arrived_at=0.0, payload=b"hello"))
    # Same hash at t=window+1ms — feed() must close the stale group and
    # start a fresh one for the incoming candidate.
    agg.feed(_candidate(arrived_at=0.201, payload=b"hello"))

    # flush_expired drains the closed group; the fresh one is still
    # pending because its window has not yet elapsed.
    out = list(agg.flush_expired(now=0.202))
    assert len(out) == 1
    assert out[0].diversity is not None
    assert out[0].diversity.n_candidates == 1
    assert agg.pending_count == 1


# ---------------------------------------------------------------------------
# Multi-source dedup
# ---------------------------------------------------------------------------


def test_multi_source_dedup() -> None:
    agg = DiversityAggregator(window_ms=200)
    agg.feed(_candidate(arrived_at=0.0, source="radio_868"))
    agg.feed(_candidate(arrived_at=0.05, source="radio_2g4"))

    assert agg.pending_count == 1
    out = list(agg.flush_expired(now=0.25))
    assert len(out) == 1
    div = out[0].diversity
    assert div is not None
    assert div.n_candidates == 2
    assert tuple(div.rx_sources) == ("radio_868", "radio_2g4")


# ---------------------------------------------------------------------------
# max_candidates cap (keep-best policy)
# ---------------------------------------------------------------------------


def test_max_candidates_caps_group_size() -> None:
    """Cap policy: when full, replace worst iff new candidate strictly better.

    Feed cap+3 candidates with strictly increasing SNR. The keep-best
    policy keeps the highest-SNR ``cap`` candidates; the winner is the
    last (highest-SNR) one fed.
    """
    cap = 4
    agg = DiversityAggregator(window_ms=200, max_candidates=cap)
    n = cap + 3
    for i in range(n):
        agg.feed(
            _candidate(
                arrived_at=i * 0.001,  # all within window
                snr_db=float(i),
                rx_channel=i,
                id_=f"00000000-0000-0000-0000-{i:012x}",
            )
        )

    assert agg.pending_count == 1
    out = list(agg.flush_expired(now=0.5))
    assert len(out) == 1
    div = out[0].diversity
    assert div is not None
    assert div.n_candidates == cap
    # Winner is the highest-SNR candidate (the last one fed).
    assert out[0].rx_channel == n - 1


# ---------------------------------------------------------------------------
# flush_all
# ---------------------------------------------------------------------------


def test_flush_all_drains_pending() -> None:
    agg = DiversityAggregator(window_ms=10_000)  # huge window
    agg.feed(_candidate(arrived_at=0.0, payload=b"hello"))
    agg.feed(_candidate(arrived_at=0.0, payload=b"world"))

    # No window elapsed — flush_expired emits nothing.
    assert list(agg.flush_expired(now=0.1)) == []
    out = list(agg.flush_all())
    assert len(out) == 2
    assert agg.pending_count == 0


# ---------------------------------------------------------------------------
# format_ruling smoke (mentions both source names)
# ---------------------------------------------------------------------------


def test_format_ruling_shape() -> None:
    g = PendingGroup(key=(0xABCD, 0x12), created_at=0.0)
    g.candidates.append(
        _candidate(
            arrived_at=0.0,
            source="radio_868",
            id_="00000000-0000-0000-0000-00000000000a",
        )
    )
    g.candidates.append(
        _candidate(
            arrived_at=0.05,
            source="radio_2g4",
            id_="00000000-0000-0000-0000-00000000000b",
        )
    )
    s = format_ruling(g)
    assert s
    assert "radio_868" in s
    assert "radio_2g4" in s
    assert "best of 2" in s


# ---------------------------------------------------------------------------
# build_aggregated metadata (extra coverage)
# ---------------------------------------------------------------------------


def test_build_aggregated_diversity_metadata() -> None:
    g = PendingGroup(key=(0xABCD, 0x12), created_at=0.0)
    g.candidates.append(
        _candidate(
            arrived_at=0.0,
            source="radio_868",
            rx_channel=3,
            snr_db=5.0,
            crc_valid=True,
        )
    )
    g.candidates.append(
        _candidate(
            arrived_at=0.1,
            source="radio_2g4",
            rx_channel=7,
            snr_db=10.0,
            crc_valid=True,
        )
    )
    g.best_idx = 1
    out = build_aggregated(g)
    div = out.diversity
    assert div is not None
    assert tuple(div.rx_channels) == (3, 7)
    assert tuple(div.snr_db) == (5.0, 10.0)
    assert div.crc_mask == 0b11
    assert div.gap_us == 100_000
    assert div.decoded_channel == 7
    assert tuple(div.source_ids) == (
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000001",
    )
    assert tuple(div.rx_sources) == ("radio_868", "radio_2g4")


# ---------------------------------------------------------------------------
# is_better edge: missing snr loses to present snr
# ---------------------------------------------------------------------------


def test_is_better_missing_snr_loses() -> None:
    a = _candidate(snr_db=None, crc_valid=True)
    b = _candidate(snr_db=-50.0, crc_valid=True)
    assert is_better(b, a) is True
    assert is_better(a, b) is False
