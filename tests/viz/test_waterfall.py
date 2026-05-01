# SPDX-License-Identifier: ISC
"""Smoke tests for ``lora.viz.waterfall``.

The waterfall has a heavy interactive curses-y loop (raw-mode tty,
mouse-tracking escape sequences, scroll regions). Per the Phase 3
spec we only verify ``--help`` and module import — full end-to-end
testing is documented as out-of-scope (DONE_WITH_CONCERNS) because
it requires a real PTY and CRC-tagged spectrum frames.
"""

from __future__ import annotations

import subprocess
import sys


def test_module_imports() -> None:
    """Importing the module must not crash (no top-level UDP / TTY work)."""
    import lora.viz.waterfall  # noqa: F401


def test_help_exits_zero() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "lora.viz.waterfall", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout + result.stderr
    assert "waterfall" in out.lower()
    assert "--connect" in out


def test_arg_parser_accepts_connect_override() -> None:
    """--help with explicit override should still exit 0."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "lora.viz.waterfall",
            "--connect",
            "127.0.0.1:5558",
            "--help",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
