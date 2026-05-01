# SPDX-License-Identifier: ISC
"""Smoke tests for ``lora.viz.spectrum``.

Like ``lora.viz.waterfall`` the spectrum bar graph runs an interactive
raw-mode loop with custom ANSI escapes; per the Phase 3 spec we only
verify ``--help`` and that the module imports cleanly.

The spectrum bar graph subscribes to the lora_scan stream
(``:5557``), not lora-core (``:5558``) — out of scope per spec
Section 7 row 8.
"""

from __future__ import annotations

import subprocess
import sys


def test_module_imports() -> None:
    import lora.viz.spectrum  # noqa: F401


def test_help_exits_zero() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "lora.viz.spectrum", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout + result.stderr
    assert "spectrum" in out.lower()
    # default port is 5557 — keep this assertion as a guard against
    # accidentally repointing spectrum at lora-core.
    assert "5557" in out


def test_arg_parser_accepts_overrides() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "lora.viz.spectrum",
            "--port",
            "5557",
            "--ema",
            "0.5",
            "--help",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
