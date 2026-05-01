# SPDX-License-Identifier: ISC
"""Tests for :mod:`lora.daemon.lifecycle`."""

from __future__ import annotations

import asyncio
import os
import signal

import pytest

from lora.daemon.lifecycle import Lifecycle


async def test_signal_shutdown_unblocks_wait() -> None:
    """Direct signal_shutdown() unblocks wait_for_shutdown without OS signals."""
    lc = Lifecycle()
    lc.install_handlers()
    try:
        # signal from a worker.
        async def trigger() -> None:
            await asyncio.sleep(0.01)
            lc.signal_shutdown()

        asyncio.create_task(trigger())
        await asyncio.wait_for(lc.wait_for_shutdown(), timeout=1.0)
    finally:
        lc.uninstall_handlers()


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only signal test")
async def test_sigterm_triggers_shutdown() -> None:
    """SIGTERM via os.kill drives Lifecycle to shutdown."""
    lc = Lifecycle()
    lc.install_handlers()
    try:

        async def trigger() -> None:
            await asyncio.sleep(0.01)
            os.kill(os.getpid(), signal.SIGTERM)

        asyncio.create_task(trigger())
        await asyncio.wait_for(lc.wait_for_shutdown(), timeout=1.0)
    finally:
        lc.uninstall_handlers()


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only signal test")
async def test_sighup_is_a_placeholder() -> None:
    """SIGHUP must NOT trigger shutdown — Phase 2F installs the real handler."""
    lc = Lifecycle()
    lc.install_handlers()
    try:
        os.kill(os.getpid(), signal.SIGHUP)
        # Give the loop a chance to dispatch.
        await asyncio.sleep(0.05)
        assert not lc.shutdown_requested
    finally:
        lc.uninstall_handlers()
