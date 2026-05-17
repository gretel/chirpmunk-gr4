# SPDX-License-Identifier: ISC
"""Additional lifecycle tests: watch_parent, _dump_traceback, install_sighup, _reload_loop."""

from __future__ import annotations

import asyncio
import os
import signal
from unittest.mock import MagicMock, patch

import pytest

from lora.daemon.lifecycle import Lifecycle


async def test_watch_parent_shuts_down_when_ppid_changes() -> None:
    lc = Lifecycle()
    with patch.object(os, "getppid", side_effect=[100, 1]):
        task = asyncio.create_task(
            lc.watch_parent(original_ppid=100, poll_interval_s=0.05)
        )
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    assert lc.shutdown_requested


async def test_watch_parent_original_ppid_0_returns_immediately() -> None:
    lc = Lifecycle()
    task = asyncio.create_task(lc.watch_parent(original_ppid=0, poll_interval_s=0.1))
    await asyncio.wait_for(task, timeout=1.0)
    assert not lc.shutdown_requested


async def test_watch_parent_original_ppid_1_returns_immediately() -> None:
    lc = Lifecycle()
    task = asyncio.create_task(lc.watch_parent(original_ppid=1, poll_interval_s=0.1))
    await asyncio.wait_for(task, timeout=1.0)
    assert not lc.shutdown_requested


async def test_watch_parent_no_change_stays_alive() -> None:
    real_ppid = os.getppid()
    lc = Lifecycle()
    task = asyncio.create_task(
        lc.watch_parent(original_ppid=real_ppid, poll_interval_s=0.05)
    )
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert not lc.shutdown_requested


def test_dump_traceback_runs_without_error() -> None:
    lc = Lifecycle()
    # Must not raise.
    lc._dump_traceback()


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only")
async def test_install_sighup_replaces_placeholder() -> None:
    lc = Lifecycle()
    lc.install_handlers()
    try:
        reloader = MagicMock()
        reloader.reload.return_value = MagicMock(applied=1, refused=[], errors=[])
        lc.install_sighup(reloader)
        # Give the reload loop task time to start.
        await asyncio.sleep(0.05)
        # SIGHUP should trigger reload.
        os.kill(os.getpid(), signal.SIGHUP)
        await asyncio.sleep(0.05)
        reloader.reload.assert_called_once()
    finally:
        lc.uninstall_handlers()


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only")
async def test_install_sighup_twice_cancels_prior_task() -> None:
    lc = Lifecycle()
    lc.install_handlers()
    try:
        r1 = MagicMock()
        r2 = MagicMock()
        r2.reload.return_value = MagicMock(applied=0, refused=[], errors=[])
        lc.install_sighup(r1)
        prior_task = lc._reload_task
        lc.install_sighup(r2)
        # Prior task should be cancelling or cancelled.
        assert prior_task is not None
        assert prior_task.cancelling() or prior_task.cancelled()
    finally:
        lc.uninstall_handlers()


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only")
async def test_uninstall_handlers_cancels_reload_task() -> None:
    lc = Lifecycle()
    lc.install_handlers()
    reloader = MagicMock()
    lc.install_sighup(reloader)
    assert lc._reload_task is not None and not lc._reload_task.done()
    lc.uninstall_handlers()
    assert lc._reload_task is None or lc._reload_task.done()


async def test_signal_shutdown_idempotent() -> None:
    lc = Lifecycle()
    assert not lc.shutdown_requested
    lc.signal_shutdown()
    assert lc.shutdown_requested
    lc.signal_shutdown()  # second call must not raise
    assert lc.shutdown_requested
