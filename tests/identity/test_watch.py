#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Tests for the polling mtime watcher used by IdentityStore."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from lora.identity.watch import MtimeWatcher


def _wait(event: threading.Event, timeout_s: float) -> bool:
    return event.wait(timeout=timeout_s)


def test_watcher_fires_on_file_change(tmp_path: Path) -> None:
    target = tmp_path / "watched.bin"
    target.write_bytes(b"a")
    fired = threading.Event()
    watcher = MtimeWatcher([target], callback=fired.set, interval_s=0.05)
    watcher.start()
    try:
        # Mtime resolution on macOS HFS+ is 1s; use os.utime to force change.
        import os

        new_mtime = target.stat().st_mtime + 5
        target.write_bytes(b"ab")
        os.utime(target, (new_mtime, new_mtime))
        assert _wait(fired, timeout_s=2.0), "callback did not fire on file change"
    finally:
        watcher.stop()


def test_watcher_fires_on_new_file_in_dir(tmp_path: Path) -> None:
    fired = threading.Event()
    watcher = MtimeWatcher([tmp_path], callback=fired.set, interval_s=0.05)
    watcher.start()
    try:
        # Give the watcher one cycle to record the empty baseline.
        time.sleep(0.1)
        (tmp_path / "new.bin").write_bytes(b"hello")
        assert _wait(fired, timeout_s=2.0), "callback did not fire on new file in dir"
    finally:
        watcher.stop()


def test_watcher_stop_cleanly(tmp_path: Path) -> None:
    target = tmp_path / "watched.bin"
    target.write_bytes(b"x")
    watcher = MtimeWatcher([target], callback=lambda: None, interval_s=0.05)
    watcher.start()
    time.sleep(0.1)
    watcher.stop()
    assert watcher._thread is not None
    assert not watcher._thread.is_alive(), "watcher thread did not exit on stop()"
