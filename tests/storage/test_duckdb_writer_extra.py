# SPDX-License-Identifier: ISC
"""Additional DuckDB writer tests: DatabaseLocked, _maybe_lock_conflict, lifecycle edge cases."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from lora.storage.duckdb_writer import (
    DatabaseLocked,
    DuckDBWriter,
    StorageConfig,
    _maybe_lock_conflict,
)


def test_database_locked_with_pid() -> None:
    err = DatabaseLocked(Path("/tmp/test.duckdb"), pid=1234, raw="PID 1234")
    assert "PID 1234" in str(err)
    assert err.pid == 1234
    assert err.db_path == Path("/tmp/test.duckdb")


def test_database_locked_without_pid() -> None:
    err = DatabaseLocked(Path("/tmp/test.duckdb"), pid=None, raw="some error")
    assert "another process" in str(err)
    assert err.pid is None


def test_maybe_lock_conflict_non_ioe_returns_none() -> None:
    result = _maybe_lock_conflict(ValueError("nope"), Path("/tmp/x.duckdb"))
    assert result is None


def test_maybe_lock_conflict_wrong_message_returns_none() -> None:
    e = duckdb.IOException("some other IO error")
    result = _maybe_lock_conflict(e, Path("/tmp/x.duckdb"))
    assert result is None


def test_maybe_lock_conflict_with_pid() -> None:
    e = duckdb.IOException("Conflicting lock in database directory. PID 5678")
    result = _maybe_lock_conflict(e, Path("/tmp/x.duckdb"))
    assert result is not None
    assert isinstance(result, DatabaseLocked)
    assert result.pid == 5678


def test_start_twice_raises(tmp_path: Path) -> None:
    cfg = StorageConfig(db_path=tmp_path / "lora.duckdb")
    w = DuckDBWriter(cfg)
    w.start()
    with pytest.raises(RuntimeError, match="already started"):
        w.start()
    w.stop(timeout=2.0)


def test_stop_without_start_is_noop() -> None:
    w = DuckDBWriter(StorageConfig(db_path=Path("/tmp/_none.duckdb")))
    w.stop(timeout=0.1)


def test_checkpoint_before_start_raises() -> None:
    w = DuckDBWriter(StorageConfig(db_path=Path("/tmp/_none.duckdb")))
    with pytest.raises(RuntimeError, match="not started"):
        w.checkpoint()


def test_stats_snapshot_does_not_mutate(tmp_path: Path) -> None:
    cfg = StorageConfig(db_path=tmp_path / "stats.duckdb")
    w = DuckDBWriter(cfg)
    w.start()
    try:
        s1 = w.stats
        s1.enqueued = 999
        s2 = w.stats
        assert s2.enqueued == 0
    finally:
        w.stop(timeout=2.0)
