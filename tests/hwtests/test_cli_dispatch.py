#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Tests for ``lora hwtest`` CLI dispatcher (Phase 5D).

Verifies:
* ``--help`` for every matrix-driven subcommand exits 0 (subcommand is wired).
* Passthrough subcommands (bridge-live / scan-perf / trx-perf) reach
  their own argparse rather than being eaten by the parent.
"""

from __future__ import annotations

import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "subcmd",
    [
        "decode",
        "scan",
        "tx",
        "bridge",
        "transmit",
        "bridge-live",
        "scan-perf",
        "trx-perf",
    ],
)
def test_hwtest_subcmd_help(subcmd: str) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "lora.hwtests.cli", subcmd, "--help"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, (
        f"hwtest {subcmd} --help exited {proc.returncode}\n"
        f"stdout: {proc.stdout}\n"
        f"stderr: {proc.stderr}"
    )
    assert "usage:" in proc.stdout.lower()


def test_hwtest_top_level_help() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "lora.hwtests.cli", "--help"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0
    out = proc.stdout
    for token in (
        "decode",
        "scan",
        "tx",
        "bridge",
        "transmit",
        "bridge-live",
        "scan-perf",
        "trx-perf",
    ):
        assert token in out, f"{token!r} not advertised in --help"


def test_lora_top_dispatcher_lists_hwtest() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "lora.cli", "--help"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0
    assert "hwtest" in proc.stdout
    assert "cal" not in proc.stdout


def test_lora_hwtest_decode_help_via_top() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "lora.cli", "hwtest", "decode", "--help"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0
    # Matrix flag should reach the inner argparse.
    assert "--matrix" in proc.stdout
