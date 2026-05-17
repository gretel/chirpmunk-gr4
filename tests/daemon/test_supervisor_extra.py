# SPDX-License-Identifier: ISC
"""Additional supervisor tests: argument parsing, edge cases."""

from __future__ import annotations

import asyncio
import signal
import sys

import pytest

from lora.daemon.supervisor import Supervisor, validate_services


def test_supervisor_empty_services_raises() -> None:
    with pytest.raises(ValueError, match="at least one service"):
        Supervisor(services={})


def test_validate_services_all_known() -> None:
    validate_services(("core", "bridge.meshcore", "bridge.serial", "wav"))


def test_validate_services_unknown_raises() -> None:
    with pytest.raises(ValueError, match="unknown service"):
        validate_services(("core", "nonexistent"))


def test_validate_services_mixed_known_and_unknown_raises() -> None:
    with pytest.raises(ValueError, match="unknown service 'bad'"):
        validate_services(("core", "bad"))


@pytest.mark.asyncio
async def test_supervisor_single_child_exits_0() -> None:
    sup = Supervisor(
        services={
            "fast": [sys.executable, "-c", "import sys; sys.exit(0)"],
        }
    )
    rc = await asyncio.wait_for(sup.run(), timeout=10.0)
    assert rc == 0


@pytest.mark.asyncio
async def test_supervisor_single_child_exits_nonzero() -> None:
    sup = Supervisor(
        services={
            "fails": [sys.executable, "-c", "import sys; sys.exit(3)"],
        }
    )
    rc = await asyncio.wait_for(sup.run(), timeout=10.0)
    assert rc == 3


@pytest.mark.asyncio
async def test_supervisor_spawns_one_and_drains_via_shutdown_event() -> None:
    sup = Supervisor(
        services={
            "sleeper": [sys.executable, "-c", "import time; time.sleep(30)"],
        }
    )
    run_task = asyncio.create_task(sup.run())
    for _ in range(50):
        if len(sup.children) == 1:
            break
        await asyncio.sleep(0.05)
    assert len(sup.children) == 1
    sup.request_shutdown()
    rc = await asyncio.wait_for(run_task, timeout=10.0)
    assert rc == 0
    child = sup.children["sleeper"]
    assert child.returncode == -signal.SIGTERM


@pytest.mark.asyncio
async def test_supervisor_drain_siblings_after_one_exits() -> None:
    sup = Supervisor(
        services={
            "dier": [sys.executable, "-c", "import sys; sys.exit(42)"],
            "sleeper": [sys.executable, "-c", "import time; time.sleep(30)"],
        }
    )
    rc = await asyncio.wait_for(sup.run(), timeout=10.0)
    assert rc == 42
    assert sup.children["sleeper"].returncode == -signal.SIGTERM
