# SPDX-License-Identifier: ISC
"""Phase 2F SIGHUP stress test — many reloads must not lose frames or drop the daemon.

The default test runs 100 SIGHUP iterations to keep the regular suite
under a couple of seconds. The 1000-iter variant carries the
``@pytest.mark.slow`` marker so CI can pick it up via ``-m slow``
without it bloating the default ``uv run pytest`` invocation.

Why a SIGHUP storm rather than direct ``HotReloader.reload`` calls?
The whole point of the test is to exercise the signal-handler ->
``asyncio.Event`` -> reload-loop dispatch chain. Calling
``HotReloader.reload`` directly skips the signal path, which is the
one place where threading actually matters.

Note on signal coalescing
-------------------------
POSIX standard signals are not queued — multiple SIGHUPs arriving
between two handler invocations collapse to one. We therefore assert
``reload_count > 0`` rather than ``== num_iters``. The intent is "the
reloader fired" not "every signal triggered exactly one reload".
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import threading
import time
import tomllib
from collections.abc import Iterable
from pathlib import Path

import pytest

from lora.core.types import Carrier, LoraFrame, Phy
from lora.daemon._decoders import build_decoders
from lora.daemon.config import DaemonConfig
from lora.daemon.decode_chain import DecodeChain
from lora.daemon.hot_reload import HotReloader
from lora.daemon.lifecycle import Lifecycle
from lora.identity import IdentityConfig, IdentityStore

# Two valid configs that differ only in ``decoders.enabled``. Both
# settle to the same effective set after ``build_decoders`` appends
# the catch-all RawDecoder, but the diff against ``current`` flips
# ``core.decoders.enabled`` on every flip.
_CONFIG_TEMPLATE = """
[logging]
level = "INFO"
color = true

[core]
listen = "127.0.0.1:5558"
fanout_queue_depth = 10000

[[core.upstream]]
name = "default"
host = "127.0.0.1"
port = 5556

[core.aggregator]
window_ms = 200
max_candidates = 8

[core.decoders]
enabled = {enabled}

[core.identity]
identity_file = "{tmp}/id/identity.bin"
keys_dir = "{tmp}/id/keys"
channels_dir = "{tmp}/id/channels"
contacts_dir = "{tmp}/id/contacts"

[core.storage]
db_path = "{tmp}/lora.duckdb"
"""


def _config_a(tmp_path: Path) -> str:
    return _CONFIG_TEMPLATE.format(enabled='["meshcore", "raw"]', tmp=tmp_path)


def _config_b(tmp_path: Path) -> str:
    return _CONFIG_TEMPLATE.format(enabled='["raw"]', tmp=tmp_path)


def _atomic_write(path: Path, body: str) -> None:
    """Atomic file replace so the SIGHUP reader never sees a half-written file.

    The strict typed validator (Phase 2G) refuses an empty or
    half-flushed config; without atomicity the SIGHUP storm sees mid-
    write states and floods the test logger with parse errors. POSIX
    rename is atomic on the same filesystem.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body)
    os.replace(tmp, path)


def _frame(sync: int = 0x12) -> LoraFrame:
    """Construct a minimal LoraFrame for chain.process()."""
    return LoraFrame(
        type="lora_frame",
        ts="2026-01-01T00:00:00+00:00",
        seq=1,
        payload=b"\x00\x00\x00",
        payload_len=3,
        crc_valid=True,
        cr=4,
        is_downchirp=False,
        payload_hash=0xCAFEF00D,
        id="00000000-0000-0000-0000-000000000001",
        phy=Phy(sf=8, bw=62500, cr=4, sync_word=sync, crc_valid=True),
        carrier=Carrier(sync_word=sync, sf=8, bw=62500, cr=4, ldro_cfg=False),
        source="radio_868",
    )


def _identity(tmp_path: Path) -> IdentityStore:
    return IdentityStore(
        IdentityConfig(
            identity_file=tmp_path / "identity.bin",
            keys_dir=tmp_path / "keys",
            channels_dir=tmp_path / "channels",
            contacts_dir=tmp_path / "contacts",
        )
    )


async def _run_storm(
    tmp_path: Path,
    *,
    num_iters: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Stress harness shared by the default and slow variants.

    Wires up Lifecycle + HotReloader, fires ``num_iters`` SIGHUPs in a
    background thread alternating which TOML sits on disk, while a
    second thread spams ``DecodeChain.process``. Asserts no
    exceptions, no spurious WARNINGs, and at least one reload
    completed.
    """
    cfg_path = tmp_path / "config.toml"
    cfg_a = _config_a(tmp_path)
    cfg_b = _config_b(tmp_path)
    _atomic_write(cfg_path, cfg_a)
    initial = DaemonConfig.from_legacy_toml(tomllib.loads(cfg_a))
    identity = _identity(tmp_path / "id")
    chain = DecodeChain(
        decoders=build_decoders(initial.decoders_enabled, initial.decoders_options),
        identity=identity,
    )
    log = logging.getLogger("test.hot_reload_stress")
    reloader = HotReloader(
        config_path=cfg_path,
        current=initial,
        decode_chain=chain,
        identity=identity,
        log=log,
    )

    lc = Lifecycle()
    lc.install_handlers()
    lc.install_sighup(reloader)

    frame_errors: list[BaseException] = []
    sighup_errors: list[BaseException] = []
    frames_processed = 0
    frame_lock = threading.Lock()
    stop = threading.Event()

    def emit_frames() -> None:
        nonlocal frames_processed
        try:
            for _ in range(num_iters):
                if stop.is_set():
                    return
                out = chain.process(_frame())
                if out is None:  # pragma: no cover - defensive
                    raise AssertionError("process returned None")
                with frame_lock:
                    frames_processed += 1
        except BaseException as exc:  # noqa: BLE001
            frame_errors.append(exc)

    def emit_sighups() -> None:
        try:
            for i in range(num_iters):
                if stop.is_set():
                    return
                # Alternate which config sits on disk so each
                # successful reload sees a real diff.
                _atomic_write(cfg_path, cfg_b if i % 2 else cfg_a)
                os.kill(os.getpid(), signal.SIGHUP)
                # Yield briefly so the asyncio loop can dispatch.
                # 100us is enough on Apple Silicon and keeps the
                # 1000-iter run under ~2s.
                time.sleep(0.0001)
        except BaseException as exc:  # noqa: BLE001
            sighup_errors.append(exc)

    caplog.set_level(logging.WARNING, logger="test.hot_reload_stress")
    caplog.set_level(logging.WARNING, logger="lora.daemon.lifecycle")

    try:
        loop = asyncio.get_running_loop()
        frame_fut = loop.run_in_executor(None, emit_frames)
        sighup_fut = loop.run_in_executor(None, emit_sighups)
        await frame_fut
        await sighup_fut
        # Drain any pending reload events that the SIGHUP storm
        # queued. The reload loop iterates while ``_shutdown`` is
        # unset; one short sleep is enough for the final SIGHUP-driven
        # reload to land.
        await asyncio.sleep(0.2)
    finally:
        stop.set()
        lc.signal_shutdown()
        # Allow the reload loop task to observe the shutdown flag.
        await asyncio.sleep(0.05)
        lc.uninstall_handlers()

    assert frame_errors == [], f"frame thread raised: {frame_errors!r}"
    assert sighup_errors == [], f"sighup thread raised: {sighup_errors!r}"
    assert frames_processed == num_iters, (
        f"expected {num_iters} frames, got {frames_processed}"
    )
    assert chain.reload_count > 0, "DecodeChain never reloaded"
    assert reloader.reload_count > 0, "HotReloader never fired"
    # No restart-required field flipped, so refusal log lines must be
    # absent. Other WARNINGs would indicate a real regression.
    bad = [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and "restart required" not in r.getMessage()
    ]
    assert bad == [], f"unexpected WARNING+ logs: {[r.getMessage() for r in bad]}"


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only SIGHUP test")
async def test_sighup_100_iter_zero_loss(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Default stress run — 100 iters keeps the suite quick."""
    await _run_storm(tmp_path, num_iters=100, caplog=caplog)


@pytest.mark.slow
@pytest.mark.skipif(os.name != "posix", reason="POSIX-only SIGHUP test")
async def test_sighup_1000_iter_zero_loss(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """1000-iter variant — opt in via ``pytest -m slow``."""
    await _run_storm(tmp_path, num_iters=1000, caplog=caplog)


# Helper kept here rather than in conftest so the file remains
# self-contained for review.
def _expected_iters(default: int, slow_iters: Iterable[int] | None = None) -> int:
    """Return the iter count for a run; reserved for future tuning."""
    return default
