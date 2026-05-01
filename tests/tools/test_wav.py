# SPDX-License-Identifier: ISC
"""Smoke tests for ``lora.tools.wav``.

The recorder spawns sounddevice + worker threads; we don't drive it
end-to-end here. Per Phase 3 spec we verify ``--help``, parse args,
and module import.
"""

from __future__ import annotations

import subprocess
import sys


def test_module_imports() -> None:
    import lora.tools.wav  # noqa: F401


def test_help_exits_zero() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "lora.tools.wav", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout + result.stderr
    assert "audio" in out.lower() or "wav" in out.lower()
    assert "--audio-dir" in out
    assert "--no-play" in out
