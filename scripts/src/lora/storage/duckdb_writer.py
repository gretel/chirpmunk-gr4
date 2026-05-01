#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Thread-backed DuckDB writer for the lora-core daemon fanout.

The daemon's fanout pushes typed events (``LoraFrame``, ``Status``,
multisf / wideband telemetry) into the writer via non-blocking
``put_*`` calls. A single dedicated thread drains a bounded queue,
batches per-table INSERTs via ``executemany``, and CHECKPOINTs at
configured intervals plus on shutdown.

Lifecycle:

    >>> w = DuckDBWriter(StorageConfig(db_path=Path("data/lora.duckdb")))
    >>> w.start()
    >>> w.put(frame)            # non-blocking; oldest-drop on overflow
    >>> w.put_status(status)
    >>> w.checkpoint()          # blocking flush + CHECKPOINT
    >>> w.stop(timeout=5.0)     # drain queue, CHECKPOINT, close

Threading rules
~~~~~~~~~~~~~~~

The writer thread owns its own DuckDB cursor (``con.cursor()``). The
producer side (``put_*`` callers) only touches the queue and the stats
counters — never the connection. See the duckdb skill for the broader
"per-thread cursor" rule.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import duckdb

from lora.core.types import (
    LoraFrame,
    MultisfDetect,
    MultisfFrame,
    MultisfSync,
    Status,
    WidebandSlot,
    WidebandSweep,
)
from lora.storage._lora_frame_row import (
    LORA_FRAME_COLUMNS,
    lora_frame_row,
    parse_ts,
    status_row,
)
from lora.storage.schema import apply_schema

log = logging.getLogger("lora.storage")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DatabaseLocked(RuntimeError):
    """Raised when the DuckDB file is already locked by another process.

    DuckDB allows only a single read-write process per file. A second
    ``lora core`` instance pointed at the same ``data/lora.duckdb``
    will hit this on connect. Carries the conflicting PID when
    DuckDB's error message provides one.
    """

    def __init__(self, db_path: Path, pid: int | None, raw: str) -> None:
        self.db_path = db_path
        self.pid = pid
        self.raw = raw
        if pid is not None:
            msg = (
                f"DuckDB file {db_path} is already locked by PID {pid} "
                f"(another lora core / DuckDB writer is running)"
            )
        else:
            msg = (
                f"DuckDB file {db_path} is already locked by another process "
                f"(another lora core / DuckDB writer is running)"
            )
        super().__init__(msg)


_LOCK_PID_RE = re.compile(r"PID\s+(\d+)")


def _maybe_lock_conflict(exc: BaseException, db_path: Path) -> DatabaseLocked | None:
    """Return a :class:`DatabaseLocked` if ``exc`` is DuckDB's lock error."""
    if not isinstance(exc, duckdb.IOException):
        return None
    raw = str(exc)
    if "Conflicting lock" not in raw:
        return None
    m = _LOCK_PID_RE.search(raw)
    pid = int(m.group(1)) if m else None
    return DatabaseLocked(db_path, pid, raw)


# ---------------------------------------------------------------------------
# Config + stats
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StorageConfig:
    """Tuning knobs for :class:`DuckDBWriter`."""

    db_path: Path
    batch_rows: int = 256
    batch_ms: int = 1000
    queue_depth: int = 10_000
    checkpoint_interval_s: int = 300


@dataclass
class WriterStats:
    """Live counters exposed by :attr:`DuckDBWriter.stats`."""

    enqueued: int = 0
    written: int = 0
    dropped: int = 0
    last_flush_ms: int = 0
    last_checkpoint_ts: float = 0.0


# ---------------------------------------------------------------------------
# Internal sentinels
# ---------------------------------------------------------------------------


class _Stop:
    """Drain-and-close marker."""


@dataclass
class _Checkpoint:
    """Blocking flush + CHECKPOINT request from :meth:`DuckDBWriter.checkpoint`."""

    done: threading.Event = field(default_factory=threading.Event)


# ---------------------------------------------------------------------------
# INSERT statements per table (single source of truth, parameterised)
# ---------------------------------------------------------------------------


_INSERT_LORA_FRAMES: Final[str] = (
    "INSERT INTO lora_frames ("
    + ", ".join(LORA_FRAME_COLUMNS)
    + ") VALUES ("
    + ", ".join("?" * len(LORA_FRAME_COLUMNS))
    + ") ON CONFLICT (id) DO NOTHING"
)

_INSERT_STATUS: Final[str] = (
    "INSERT INTO status_heartbeats "
    "(ts, rx_gain, tx_gain, frames_total, frames_crc_ok, frames_crc_fail, "
    "rx_overflows, writer_dropped) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)

_INSERT_MULTISF_DETECT: Final[str] = (
    "INSERT INTO multisf_detect (ts, sf, bin, source) VALUES (?, ?, ?, ?)"
)

_INSERT_MULTISF_SYNC: Final[str] = (
    "INSERT INTO multisf_sync (ts, sf, cfo_int, cfo_frac, snr_db, source) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)

_INSERT_MULTISF_FRAME: Final[str] = (
    "INSERT INTO multisf_frame (ts, sf, crc_ok, len, cr, snr_db, source) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)

_INSERT_WIDEBAND_SWEEP: Final[str] = (
    "INSERT INTO wideband_sweep (ts, sweep, tainted, overflows, zero_calls, "
    "total_calls, duration_ms, n_snapshots, n_hot, n_active, max_slots, source) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

_INSERT_WIDEBAND_SLOT: Final[str] = (
    "INSERT INTO wideband_slot (ts, action, slot, channel, freq, bw, source) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)


_INSERT_SQL: Final[dict[str, str]] = {
    "lora_frame": _INSERT_LORA_FRAMES,
    "status": _INSERT_STATUS,
    "multisf_detect": _INSERT_MULTISF_DETECT,
    "multisf_sync": _INSERT_MULTISF_SYNC,
    "multisf_frame": _INSERT_MULTISF_FRAME,
    "wideband_sweep": _INSERT_WIDEBAND_SWEEP,
    "wideband_slot": _INSERT_WIDEBAND_SLOT,
}


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


# Queue items: ``(kind, row)`` tuples, or ``_Stop`` / ``_Checkpoint``
# sentinel instances. Typed loosely because Python's queue.Queue is
# variadic.
_Item = Any


class DuckDBWriter:
    """Bounded-queue writer with oldest-drop overflow policy."""

    def __init__(self, config: StorageConfig) -> None:
        self._config = config
        self._q: queue.Queue[_Item] = queue.Queue(maxsize=config.queue_depth)
        self._stats = WriterStats()
        self._stats_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Open the database, install the schema, and spin up the thread."""
        if self._thread is not None:
            raise RuntimeError("DuckDBWriter already started")
        # Open the connection on the writer thread itself so the cursor()
        # rule (per-thread handles) is trivially satisfied. We do schema
        # install on the same thread.
        ready = threading.Event()
        startup_error: list[BaseException] = []

        def _bootstrap() -> None:
            try:
                con = duckdb.connect(str(self._config.db_path))
                apply_schema(con)
            except BaseException as exc:  # pragma: no cover - propagated
                locked = _maybe_lock_conflict(exc, self._config.db_path)
                startup_error.append(locked if locked is not None else exc)
                ready.set()
                return
            log.info(
                "storage: backend=duckdb path=%s queue_depth=%d "
                "batch_rows=%d batch_ms=%d checkpoint_s=%d",
                self._config.db_path,
                self._config.queue_depth,
                self._config.batch_rows,
                self._config.batch_ms,
                self._config.checkpoint_interval_s,
            )
            ready.set()
            try:
                self._run(con)
            finally:
                try:
                    con.close()
                except Exception:  # pragma: no cover - best effort
                    log.exception("storage: close failed")

        t = threading.Thread(target=_bootstrap, name="duckdb-writer", daemon=True)
        t.start()
        ready.wait()
        if startup_error:
            raise startup_error[0]
        self._thread = t

    def stop(self, timeout: float = 5.0) -> None:
        """Drain the queue, CHECKPOINT, close the database, join the thread."""
        if self._thread is None:
            return
        self._q.put(_Stop())
        self._thread.join(timeout)
        if self._thread.is_alive():  # pragma: no cover - timeout path
            log.warning("storage: writer thread did not exit within %.1fs", timeout)
        self._thread = None

    def checkpoint(self) -> None:
        """Blocking flush + CHECKPOINT round-trip."""
        if self._thread is None:
            raise RuntimeError("DuckDBWriter not started")
        sentinel = _Checkpoint()
        self._q.put(sentinel)
        sentinel.done.wait()

    # -- producer-side put_* ------------------------------------------------

    def put(self, frame: LoraFrame) -> bool:
        return self._enqueue("lora_frame", lora_frame_row(frame))

    def put_status(self, status: Status) -> bool:
        with self._stats_lock:
            dropped = self._stats.dropped
        return self._enqueue("status", status_row(status, dropped))

    def put_multisf_detect(self, e: MultisfDetect, source: str | None = None) -> bool:
        return self._enqueue("multisf_detect", (parse_ts(e.ts), e.sf, e.bin, source))

    def put_multisf_sync(self, e: MultisfSync, source: str | None = None) -> bool:
        return self._enqueue(
            "multisf_sync",
            (parse_ts(e.ts), e.sf, e.cfo_int, e.cfo_frac, e.snr_db, source),
        )

    def put_multisf_frame(self, e: MultisfFrame, source: str | None = None) -> bool:
        return self._enqueue(
            "multisf_frame",
            (parse_ts(e.ts), e.sf, e.crc_ok, e.len, e.cr, e.snr_db, source),
        )

    def put_wideband_sweep(self, e: WidebandSweep, source: str | None = None) -> bool:
        return self._enqueue(
            "wideband_sweep",
            (
                parse_ts(e.ts),
                e.sweep,
                e.tainted,
                e.overflows,
                e.zero_calls,
                e.total_calls,
                e.duration_ms,
                e.n_snapshots,
                e.n_hot,
                e.n_active,
                e.max_slots,
                source,
            ),
        )

    def put_wideband_slot(self, e: WidebandSlot, source: str | None = None) -> bool:
        return self._enqueue(
            "wideband_slot",
            (
                parse_ts(e.ts),
                e.action,
                e.slot,
                e.channel,
                e.freq,
                e.bw,
                source,
            ),
        )

    # -- stats --------------------------------------------------------------

    @property
    def stats(self) -> WriterStats:
        """Snapshot copy of the live counters."""
        with self._stats_lock:
            return WriterStats(
                enqueued=self._stats.enqueued,
                written=self._stats.written,
                dropped=self._stats.dropped,
                last_flush_ms=self._stats.last_flush_ms,
                last_checkpoint_ts=self._stats.last_checkpoint_ts,
            )

    # -- internals ----------------------------------------------------------

    def _enqueue(self, kind: str, row: tuple[Any, ...]) -> bool:
        """Push (kind, row) with oldest-drop overflow.

        Returns True on clean enqueue, False when an oldest item was
        evicted to make room.
        """
        item = (kind, row)
        try:
            self._q.put_nowait(item)
        except queue.Full:
            try:
                self._q.get_nowait()
            except queue.Empty:  # pragma: no cover - racy edge
                pass
            else:
                with self._stats_lock:
                    self._stats.dropped += 1
            try:
                self._q.put_nowait(item)
                with self._stats_lock:
                    self._stats.enqueued += 1
            except queue.Full:  # pragma: no cover - racy edge
                with self._stats_lock:
                    self._stats.dropped += 1
            return False
        with self._stats_lock:
            self._stats.enqueued += 1
        return True

    def _run(self, con: duckdb.DuckDBPyConnection) -> None:
        """Writer thread main loop. Owns ``con`` end to end."""
        cur = con.cursor()
        buffers: dict[str, list[tuple[Any, ...]]] = defaultdict(list)
        timeout_s = max(self._config.batch_ms / 1000.0, 0.001)
        last_checkpoint = time.monotonic()

        while True:
            try:
                item = self._q.get(timeout=timeout_s)
            except queue.Empty:
                if any(buffers.values()):
                    self._flush(cur, buffers)
                if self._should_checkpoint(last_checkpoint):
                    self._do_checkpoint(cur)
                    last_checkpoint = time.monotonic()
                continue

            if isinstance(item, _Stop):
                self._drain_remaining(buffers)
                self._flush(cur, buffers)
                self._do_checkpoint(cur)
                cur.close()
                return

            if isinstance(item, _Checkpoint):
                self._flush(cur, buffers)
                self._do_checkpoint(cur)
                last_checkpoint = time.monotonic()
                item.done.set()
                continue

            kind, row = item
            buffers[kind].append(row)
            total = sum(len(b) for b in buffers.values())
            if total >= self._config.batch_rows:
                self._flush(cur, buffers)

            if self._should_checkpoint(last_checkpoint):
                self._do_checkpoint(cur)
                last_checkpoint = time.monotonic()

    def _drain_remaining(self, buffers: dict[str, list[tuple[Any, ...]]]) -> None:
        """Drain any items still queued after a stop/checkpoint sentinel."""
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                return
            if isinstance(item, _Stop):
                continue
            if isinstance(item, _Checkpoint):
                # Honour an already-queued checkpoint by signalling — we
                # are about to checkpoint anyway as part of stop.
                item.done.set()
                continue
            kind, row = item
            buffers[kind].append(row)

    def _flush(
        self,
        cur: duckdb.DuckDBPyConnection,
        buffers: dict[str, list[tuple[Any, ...]]],
    ) -> None:
        """INSERT every buffered table via ``executemany``."""
        if not any(buffers.values()):
            return
        t0 = time.monotonic()
        written = 0
        for kind, rows in buffers.items():
            if not rows:
                continue
            sql = _INSERT_SQL[kind]
            try:
                cur.executemany(sql, rows)
            except Exception:
                log.exception(
                    "storage: executemany failed for %s (%d rows)", kind, len(rows)
                )
                # Drop the batch so a single poison row cannot wedge the
                # writer indefinitely. Future work: per-row retry.
                rows.clear()
                continue
            written += len(rows)
            rows.clear()
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        with self._stats_lock:
            self._stats.written += written
            self._stats.last_flush_ms = elapsed_ms

    def _should_checkpoint(self, last_checkpoint: float) -> bool:
        return time.monotonic() - last_checkpoint >= self._config.checkpoint_interval_s

    def _do_checkpoint(self, cur: duckdb.DuckDBPyConnection) -> None:
        try:
            cur.execute("CHECKPOINT")
        except Exception:  # pragma: no cover - logged
            log.exception("storage: CHECKPOINT failed")
            return
        with self._stats_lock:
            self._stats.last_checkpoint_ts = time.time()
