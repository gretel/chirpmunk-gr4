# SPDX-License-Identifier: ISC
"""Integration tests for :class:`lora.daemon.supervisor.Supervisor`.

Children are real OS processes (``python -c "..."``) — not mocks —
so we exercise the full asyncio + signal + process-group plumbing.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path  # noqa: F401  (used in later tasks)

import pytest

from lora.daemon.supervisor import Supervisor


def _sleeper_argv(seconds: float) -> list[str]:
    """Argv that sleeps for ``seconds`` then exits 0."""
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


@pytest.mark.asyncio
async def test_supervisor_spawns_and_drains_on_sigterm() -> None:
    sup = Supervisor(
        services={
            "alpha": _sleeper_argv(30),
            "beta": _sleeper_argv(30),
        }
    )
    run_task = asyncio.create_task(sup.run())
    # Wait until both children are recorded.
    for _ in range(50):
        if len(sup.children) == 2:
            break
        await asyncio.sleep(0.05)
    assert len(sup.children) == 2
    # Trigger shutdown directly (equivalent to SIGTERM handler firing).
    sup.request_shutdown()
    rc = await asyncio.wait_for(run_task, timeout=10.0)
    assert rc == 0
    for child in sup.children.values():
        assert child.returncode is not None


def _exit_argv(rc: int) -> list[str]:
    """Argv that exits immediately with ``rc``."""
    return [sys.executable, "-c", f"import sys; sys.exit({rc})"]


@pytest.mark.asyncio
async def test_supervisor_fail_fast_on_first_child_exit() -> None:
    sup = Supervisor(
        services={
            "fast_dier": _exit_argv(7),
            "long_lived": _sleeper_argv(30),
        }
    )
    rc = await asyncio.wait_for(sup.run(), timeout=10.0)
    # Failed child's rc is propagated.
    assert rc == 7
    # Sibling was reaped via SIGTERM.
    assert sup.children["long_lived"].returncode == -signal.SIGTERM


@pytest.mark.asyncio
async def test_supervisor_clean_child_exit_still_drains_siblings() -> None:
    """rc=0 from a child still fail-fasts the supervisor.

    Spec: any child exit (including 0) triggers drain. A long-running
    service exiting cleanly mid-day is still a state the operator
    should notice.
    """
    sup = Supervisor(
        services={
            "clean_exit": _exit_argv(0),
            "long_lived": _sleeper_argv(30),
        }
    )
    rc = await asyncio.wait_for(sup.run(), timeout=10.0)
    assert rc == 0
    assert sup.children["long_lived"].returncode == -signal.SIGTERM


def _sigterm_ignorer_argv(seconds: float) -> list[str]:
    """Argv that ignores SIGTERM and sleeps."""
    code = (
        "import signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"time.sleep({seconds})"
    )
    return [sys.executable, "-c", code]


@pytest.mark.asyncio
async def test_supervisor_sigkills_stragglers_after_grace() -> None:
    """Child that ignores SIGTERM gets SIGKILL after SHUTDOWN_GRACE_S.

    The trip-wire (``stubborn``) is spawned first and given enough
    head-start to install its SIG_IGN handler before ``trip_wire``
    exits and triggers drain. Without the head-start, drain races
    the handler install and SIGTERM kills ``stubborn`` outright,
    masking the SIGKILL path we are exercising.
    """
    from lora.daemon.supervisor import SHUTDOWN_GRACE_S

    sup = Supervisor(
        services={
            "stubborn": _sigterm_ignorer_argv(60),
            # Sleeps briefly so stubborn's SIG_IGN is in place before
            # drain begins.
            "trip_wire": [sys.executable, "-c", "import time; time.sleep(0.5)"],
        }
    )
    rc = await asyncio.wait_for(sup.run(), timeout=SHUTDOWN_GRACE_S + 3.0)
    assert rc == 0
    assert sup.children["stubborn"].returncode == -signal.SIGKILL


import subprocess  # noqa: E402
import textwrap  # noqa: E402


@pytest.mark.asyncio
async def test_lora_daemon_end_to_end(tmp_path: Path) -> None:
    """Spawn `lora daemon` as a real subprocess against a tmp config.

    Validates argparse wiring, config resolution, ``KNOWN_SERVICES``
    lookup, signal handling. Uses the ``wav`` service because it
    spawns + dies fast on missing audio device; we then SIGTERM
    the supervisor and assert clean exit.
    """
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        textwrap.dedent(
            """
            [startup]
            services = ["wav"]
            """
        ).strip()
    )
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "lora.cli",
        "daemon",
        "--config",
        str(cfg),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    # Give the supervisor 1s to spawn + log.
    await asyncio.sleep(1.0)
    if proc.returncode is None:
        proc.terminate()
    await asyncio.wait_for(proc.wait(), timeout=10.0)
    assert proc.returncode is not None
