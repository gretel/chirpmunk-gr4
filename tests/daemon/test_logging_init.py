# SPDX-License-Identifier: ISC
"""Boot-time logging smoke test for :func:`lora.daemon.main.main`.

Asserts that the synchronous part of ``main()`` (everything before
``asyncio.run``) emits the operator-visible INFO records the
"lora-core logging" spec requires: a startup banner, the resolved
config path, and (at DEBUG) the chosen log level.

We monkey-patch :func:`asyncio.run` to a no-op so the actual daemon
loop never starts — this keeps the test hermetic (no sockets, no
DuckDB).
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

# ``lora.daemon`` re-exports ``main`` as a function, so ``import lora.daemon.main``
# does not bind the module object — use importlib to grab the module itself,
# which we need to monkey-patch ``asyncio.run`` on.
main_module = importlib.import_module("lora.daemon.main")


_MIN_CONFIG_TOML = """\
[core]
listen = "127.0.0.1:5555"

[[core.upstream]]
name = "default"
host = "127.0.0.1"
port = 5556

[core.aggregator]
window_ms = 200
max_candidates = 8

[core.decoders]
enabled = ["raw"]

[core.identity]
identity_file     = "{root}/identity.bin"
keys_dir          = "{root}/keys"
channels_dir      = "{root}/channels"
contacts_dir      = "{root}/contacts"
reload_interval_s = 5

[core.storage]
backend = "duckdb"
db_path = "{root}/lora.duckdb"

[bridge.meshcore]
listen_port = 7834

[bridge.serial]
listen_port = 7835
"""


def test_main_emits_boot_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``main()`` should INFO-log start banner + resolved config path.

    ``setup_logging`` replaces root handlers, so the pytest ``caplog``
    fixture's handler is wiped before ``main()`` writes its records —
    we capture the daemon's own stderr stream instead. ``NO_COLOR``
    forces plain text so substring matching is reliable.
    """
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(_MIN_CONFIG_TOML.format(root=tmp_path))

    # Short-circuit the event loop — we only care about pre-asyncio logs.
    # Close the unawaited coroutine to avoid a RuntimeWarning.
    def _no_run(coro: object, *_a: object, **_k: object) -> int:
        if hasattr(coro, "close"):
            coro.close()  # type: ignore[union-attr]
        return 0

    monkeypatch.setattr(main_module.asyncio, "run", _no_run)
    monkeypatch.setenv("NO_COLOR", "1")

    rc = main_module.main(["--config", str(cfg_path), "--log-level", "DEBUG"])

    assert rc == 0
    err = capsys.readouterr().err
    assert "lora-core starting" in err, err
    assert "config:" in err, err
    assert "log level:" in err, err
