# SPDX-License-Identifier: ISC
"""Tests for :mod:`lora.storage.duckdb_writer`.

Covers persistence (lora_frames, status, telemetry), protocol + diversity
flattening, oldest-drop overflow policy, checkpoint visibility, and graceful
ts-parse fallback.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb

from lora.core.types import (
    Carrier,
    Diversity,
    LoraFrame,
    MultisfDetect,
    MultisfFrame,
    MultisfSync,
    Phy,
    ProtocolAnnotation,
    Status,
    StatusFrames,
    StatusPhy,
    WidebandSlot,
    WidebandSweep,
)
from lora.storage import DuckDBWriter, StorageConfig

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _phy(**overrides: object) -> Phy:
    base: dict[str, object] = dict(
        sf=8,
        bw=62500,
        cr=4,
        sync_word=0x12,
        crc_valid=True,
        snr_db=5.0,
        noise_floor_db=-110.0,
        peak_db=-90.0,
        snr_db_td=4.5,
        cfo_int=1.0,
        cfo_frac=0.01,
        sfo_hat=1e-6,
        sample_rate=125000.0,
        channel_freq=8.69618e8,
        ppm_error=0.5,
    )
    base.update(overrides)
    return Phy(**base)  # type: ignore[arg-type]


def _carrier(**overrides: object) -> Carrier:
    base: dict[str, object] = dict(sync_word=0x12, sf=8, bw=62500, cr=4, ldro_cfg=False)
    base.update(overrides)
    return Carrier(**base)  # type: ignore[arg-type]


def _frame(
    *,
    seq: int = 1,
    id_: str | None = None,
    payload: bytes = b"hello",
    ts: str = "2026-01-01T00:00:00+00:00",
    source: str = "radio_868",
    diversity: Diversity | None = None,
    protocol: ProtocolAnnotation | None = None,
    crc_valid: bool = True,
) -> LoraFrame:
    if id_ is None:
        id_ = f"frame-{seq:08d}"
    return LoraFrame(
        type="lora_frame",
        ts=ts,
        seq=seq,
        payload=payload,
        payload_len=len(payload),
        crc_valid=crc_valid,
        cr=4,
        is_downchirp=False,
        payload_hash=0xDEADBEEFCAFEBABE,
        id=id_,
        phy=_phy(crc_valid=crc_valid),
        carrier=_carrier(),
        source=source,
        rx_channel=0,
        diversity=diversity,
        protocol=protocol,
    )


def _config(tmp_path: Path, **overrides: object) -> StorageConfig:
    base: dict[str, object] = dict(
        db_path=tmp_path / "lora.duckdb",
        batch_rows=8,
        batch_ms=50,
        queue_depth=1024,
        checkpoint_interval_s=300,
    )
    base.update(overrides)
    return StorageConfig(**base)  # type: ignore[arg-type]


def _read(db: Path, sql: str) -> list[tuple[object, ...]]:
    # Note: same-process re-open uses the same default (rw) configuration so
    # DuckDB lets us share the underlying instance via MVCC. Casting any
    # TIMESTAMPTZ column with `strftime(...)` in SQL avoids the optional pytz
    # dependency the duckdb client otherwise needs to materialise tz-aware
    # datetimes — see the duckdb skill notes.
    con = duckdb.connect(str(db))
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_writer_persists_lora_frame(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    w = DuckDBWriter(cfg)
    w.start()
    try:
        for i in range(5):
            assert w.put(_frame(seq=i, id_=f"f-{i}", payload=bytes([i, i + 1, i + 2])))
    finally:
        w.stop(timeout=5.0)

    rows = _read(
        cfg.db_path, "SELECT seq, payload, payload_len FROM lora_frames ORDER BY seq"
    )
    assert len(rows) == 5
    assert rows[0][1] == b"\x00\x01\x02"
    assert rows[0][2] == 3


def test_writer_persists_lora_frame_with_protocol_meshcore(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    w = DuckDBWriter(cfg)
    w.start()
    proto = ProtocolAnnotation(
        name="meshcore",
        ok=True,
        fields={
            "route": "FLOOD",
            "payload_type": "TXT",
            "decrypted": True,
            "decrypted_text": "Hi",
        },
    )
    try:
        assert w.put(_frame(id_="mc-1", protocol=proto))
    finally:
        w.stop(timeout=5.0)

    rows = _read(
        cfg.db_path,
        "SELECT protocol_name, protocol_ok, mc_route, mc_payload_type, "
        "mc_decrypted, mc_decrypted_text FROM lora_frames",
    )
    assert rows == [("meshcore", True, "FLOOD", "TXT", True, "Hi")]


def test_writer_persists_lora_frame_with_protocol_lorawan(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    w = DuckDBWriter(cfg)
    w.start()
    proto = ProtocolAnnotation(
        name="lorawan",
        ok=True,
        fields={
            "mtype": "UnconfirmedDataUp",
            "dev_addr": "26011BDA",
            "fcnt": 42,
            "fport": 1,
            "is_uplink": True,
        },
    )
    try:
        assert w.put(_frame(id_="lw-1", protocol=proto))
    finally:
        w.stop(timeout=5.0)

    rows = _read(
        cfg.db_path,
        "SELECT lorawan_mtype, lorawan_dev_addr, lorawan_fcnt, "
        "lorawan_fport, lorawan_is_uplink FROM lora_frames",
    )
    assert rows == [("UnconfirmedDataUp", "26011BDA", 42, 1, True)]


def test_writer_persists_lora_frame_with_diversity(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    w = DuckDBWriter(cfg)
    w.start()
    div = Diversity(
        n_candidates=3,
        decoded_channel=1,
        rx_channels=(0, 1, 2),
        snr_db=(3.0, 5.0, 4.0),
        crc_mask=0b101,
        gap_us=120,
        source_ids=("a", "b", "c"),
        rx_sources=("radio_868", "radio_870", "radio_915"),
    )
    try:
        assert w.put(_frame(id_="div-1", diversity=div))
    finally:
        w.stop(timeout=5.0)

    rows = _read(
        cfg.db_path,
        "SELECT diversity_n, diversity_gap_us, diversity_crc_mask, "
        "diversity_decoded_channel, diversity_rx_sources FROM lora_frames",
    )
    assert len(rows) == 1
    n, gap, mask, decoded, rx_sources = rows[0]
    assert n == 3
    assert gap == 120
    assert mask == 0b101
    assert decoded == 1
    assert list(rx_sources) == ["radio_868", "radio_870", "radio_915"]  # type: ignore[arg-type]


def test_writer_drop_oldest_on_overflow(tmp_path: Path) -> None:
    # Tiny queue + huge batch threshold so the writer thread cannot drain
    # in time. The producer fills 4 slots and drops the next 6.
    cfg = _config(tmp_path, queue_depth=4, batch_rows=999, batch_ms=60_000)
    w = DuckDBWriter(cfg)
    w.start()
    try:
        for i in range(10):
            w.put(_frame(seq=i, id_=f"drop-{i}"))
        # Producer thread released GIL only at IO/sleep — the writer
        # has been parked on q.get(timeout=60s) the whole time.
        time.sleep(0.05)  # let any racing transitions settle
        assert w.stats.dropped == 6, w.stats
    finally:
        w.stop(timeout=5.0)

    rows = _read(cfg.db_path, "SELECT count(*) FROM lora_frames")
    assert rows == [(4,)]


def test_writer_checkpoint_makes_row_visible(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    w = DuckDBWriter(cfg)
    w.start()
    try:
        assert w.put(_frame(id_="cp-1"))
        w.checkpoint()
        rows = _read(cfg.db_path, "SELECT count(*) FROM lora_frames")
        assert rows == [(1,)]
    finally:
        w.stop(timeout=5.0)


def test_writer_stop_drains_queue(tmp_path: Path) -> None:
    cfg = _config(tmp_path, batch_rows=32, batch_ms=200, queue_depth=200)
    w = DuckDBWriter(cfg)
    w.start()
    try:
        for i in range(100):
            assert w.put(_frame(seq=i, id_=f"drain-{i}"))
    finally:
        w.stop(timeout=10.0)

    rows = _read(cfg.db_path, "SELECT count(*) FROM lora_frames")
    assert rows == [(100,)]
    assert w.stats.dropped == 0


def test_writer_telemetry_tables(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    w = DuckDBWriter(cfg)
    w.start()
    ts = "2026-01-01T00:00:00+00:00"
    try:
        assert w.put_multisf_detect(
            MultisfDetect(type="multisf_detect", ts=ts, sf=8, bin=42)
        )
        assert w.put_multisf_sync(
            MultisfSync(
                type="multisf_sync", ts=ts, sf=8, cfo_int=1, cfo_frac=0.1, snr_db=4.0
            )
        )
        assert w.put_multisf_frame(
            MultisfFrame(
                type="multisf_frame", ts=ts, sf=8, crc_ok=True, len=12, cr=4, snr_db=4.0
            )
        )
        assert w.put_wideband_sweep(
            WidebandSweep(
                type="wideband_sweep",
                ts=ts,
                sweep=1,
                tainted=False,
                overflows=0,
                zero_calls=0,
                total_calls=10,
                duration_ms=42,
                n_snapshots=8,
                n_hot=2,
                n_active=3,
                max_slots=4,
            )
        )
        assert w.put_wideband_slot(
            WidebandSlot(
                type="wideband_slot",
                ts=ts,
                action="activate",
                slot=0,
                channel=2,
                freq=8.69618e8,
                bw=62500,
            )
        )
    finally:
        w.stop(timeout=5.0)

    for table, expected in [
        ("multisf_detect", 1),
        ("multisf_sync", 1),
        ("multisf_frame", 1),
        ("wideband_sweep", 1),
        ("wideband_slot", 1),
    ]:
        rows = _read(cfg.db_path, f"SELECT count(*) FROM {table}")
        assert rows == [(expected,)], table


def test_writer_status_heartbeat(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    w = DuckDBWriter(cfg)
    w.start()
    try:
        s = Status(
            type="status",
            ts="2026-01-01T00:00:00+00:00",
            phy=StatusPhy(rx_gain=40.0, tx_gain=70.0),
            frames=StatusFrames(total=100, crc_ok=99, crc_fail=1),
            rx_overflows=0,
        )
        assert w.put_status(s)
    finally:
        w.stop(timeout=5.0)

    rows = _read(
        cfg.db_path,
        "SELECT rx_gain, tx_gain, frames_total, frames_crc_ok, "
        "frames_crc_fail, rx_overflows, writer_dropped FROM status_heartbeats",
    )
    assert rows == [(40.0, 70.0, 100, 99, 1, 0, 0)]


def test_writer_handles_malformed_ts_gracefully(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    w = DuckDBWriter(cfg)
    w.start()
    before = datetime.now(timezone.utc)
    try:
        assert w.put(_frame(id_="bad-ts", ts="not-a-date"))
    finally:
        w.stop(timeout=5.0)
    after = datetime.now(timezone.utc)

    rows = _read(
        cfg.db_path,
        "SELECT strftime(ts AT TIME ZONE 'UTC', '%Y-%m-%dT%H:%M:%S') FROM lora_frames",
    )
    assert len(rows) == 1
    ts_str = rows[0][0]
    assert isinstance(ts_str, str)
    ts = datetime.fromisoformat(ts_str).replace(tzinfo=timezone.utc)
    # ts is "now" within a small slack window
    assert before - timedelta(seconds=1) <= ts <= after + timedelta(seconds=1)


def test_writer_iso_z_suffix_parsed(tmp_path: Path) -> None:
    """Frames with trailing 'Z' (Zulu) must round-trip as UTC datetimes."""
    cfg = _config(tmp_path)
    w = DuckDBWriter(cfg)
    w.start()
    try:
        assert w.put(_frame(id_="zts-1", ts="2026-04-27T12:34:56Z"))
    finally:
        w.stop(timeout=5.0)

    rows = _read(
        cfg.db_path,
        "SELECT strftime(ts AT TIME ZONE 'UTC', '%Y-%m-%dT%H:%M:%S') FROM lora_frames",
    )
    assert rows == [("2026-04-27T12:34:56",)]
