# SPDX-License-Identifier: ISC
"""Phase 2F :class:`HotReloader` config-diff coverage.

These tests exercise the diff & apply path WITHOUT a running daemon.
We construct the same dependencies (``DecodeChain``, ``IdentityStore``)
that ``run_daemon`` builds, then mutate a ``config.toml`` on disk and
call :meth:`HotReloader.reload` directly.

The 1000-iter signal-storm coverage lives in
``test_hot_reload_stress.py`` so this file stays focused on the
diff logic.
"""

from __future__ import annotations

import logging
from pathlib import Path

from lora.daemon._decoders import build_decoders
from lora.daemon.config import DaemonConfig, UpstreamConfig
from lora.daemon.decode_chain import DecodeChain
from lora.daemon.hot_reload import HotReloader
from lora.decoders import MeshCoreDecoder
from lora.identity import IdentityConfig, IdentityStore
from lora.storage import StorageConfig

# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


def _identity_store(tmp_path: Path) -> IdentityStore:
    return IdentityStore(
        IdentityConfig(
            identity_file=tmp_path / "identity.bin",
            keys_dir=tmp_path / "keys",
            channels_dir=tmp_path / "channels",
            contacts_dir=tmp_path / "contacts",
        )
    )


def _initial_config(tmp_path: Path) -> DaemonConfig:
    return DaemonConfig(
        listen_host="127.0.0.1",
        listen_port=5558,
        upstreams=(UpstreamConfig(name="default", host="127.0.0.1", port=5556),),
        fanout_queue_depth=10_000,
        aggregator_window_ms=200,
        aggregator_max_candidates=8,
        decoders_enabled=("meshcore", "raw"),
        decoders_options={},
        storage=StorageConfig(db_path=tmp_path / "lora.duckdb"),
        identity=IdentityConfig(
            identity_file=tmp_path / "id" / "identity.bin",
            keys_dir=tmp_path / "id" / "keys",
            channels_dir=tmp_path / "id" / "channels",
            contacts_dir=tmp_path / "id" / "contacts",
        ),
        log_level="INFO",
    )


def _write_toml(path: Path, body: str) -> None:
    path.write_text(body)


# Base TOML — every test starts from this and mutates one field.
_BASE_TOML = """
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
enabled = ["meshcore", "raw"]

[core.storage]
db_path = "{db}"

[core.identity]
identity_file = "{identity}"
keys_dir = "{keys}"
channels_dir = "{channels}"
contacts_dir = "{contacts}"
"""


def _toml(tmp_path: Path, **overrides: str) -> str:
    """Render the base TOML with absolute paths to ``tmp_path``."""
    body = _BASE_TOML.format(
        db=tmp_path / "lora.duckdb",
        identity=tmp_path / "id" / "identity.bin",
        keys=tmp_path / "id" / "keys",
        channels=tmp_path / "id" / "channels",
        contacts=tmp_path / "id" / "contacts",
    )
    for k, v in overrides.items():
        body = body.replace(k, v)
    return body


def _make_reloader(tmp_path: Path) -> tuple[HotReloader, DecodeChain, Path]:
    """Build a HotReloader wired up to disk-resident config.toml."""
    config_path = tmp_path / "config.toml"
    _write_toml(config_path, _toml(tmp_path))
    config = _initial_config(tmp_path)
    identity = _identity_store(tmp_path / "id")
    decode_chain = DecodeChain(
        decoders=build_decoders(config.decoders_enabled, config.decoders_options),
        identity=identity,
    )
    log = logging.getLogger("test.hot_reload")
    reloader = HotReloader(
        config_path=config_path,
        current=config,
        decode_chain=decode_chain,
        identity=identity,
        log=log,
    )
    return reloader, decode_chain, config_path


# ----------------------------------------------------------------------
# Hot-reloadable apply tests
# ----------------------------------------------------------------------


def test_reload_picks_up_new_decoder_enabled_list(tmp_path: Path) -> None:
    reloader, chain, path = _make_reloader(tmp_path)
    # Sanity — chain has [meshcore, raw] before reload.
    assert [type(d).__name__ for d in chain.decoders] == [
        "MeshCoreDecoder",
        "RawDecoder",
    ]

    # Mutate config: drop meshcore, leave only raw.
    body = path.read_text()
    body = body.replace('enabled = ["meshcore", "raw"]', 'enabled = ["raw"]')
    path.write_text(body)

    res = reloader.reload()
    assert "core.decoders.enabled" in res.applied
    assert res.refused == []
    assert res.errors == []
    # build_decoders auto-appends RawDecoder if absent — here it's
    # already in `enabled`, so the chain is exactly [raw].
    assert [type(d).__name__ for d in chain.decoders] == ["RawDecoder"]


def test_reload_per_decoder_option_change_rebuilds_instance(
    tmp_path: Path,
) -> None:
    """A change in [core.decoders.<name>] options rebuilds the instance.

    Verifies:
    * The DecodeChain's decoder for that name is a NEW object (id differs).
    * The new instance carries the new option (decrypt=False).
    """
    reloader, chain, path = _make_reloader(tmp_path)
    # Add a [core.decoders.meshcore] table with decrypt=true.
    body = path.read_text()
    body += "\n[core.decoders.meshcore]\ndecrypt = true\n"
    path.write_text(body)
    res = reloader.reload()
    assert res.errors == []
    old_meshcore = next(
        (d for d in chain.decoders if isinstance(d, MeshCoreDecoder)), None
    )
    assert old_meshcore is not None
    assert old_meshcore.decrypt is True

    # Now flip to decrypt=false and reload again.
    body = path.read_text().replace("decrypt = true", "decrypt = false")
    path.write_text(body)
    res = reloader.reload()
    assert "core.decoders.options" in res.applied
    assert res.refused == []
    assert res.errors == []

    new_meshcore = next(
        (d for d in chain.decoders if isinstance(d, MeshCoreDecoder)), None
    )
    assert new_meshcore is not None
    assert new_meshcore is not old_meshcore  # rebuilt
    assert new_meshcore.decrypt is False


def test_reload_logging_level_changes_root_logger(tmp_path: Path) -> None:
    # Save+restore the root logger level to keep the test isolated.
    root = logging.getLogger()
    saved = root.level
    try:
        root.setLevel(logging.INFO)
        reloader, _, path = _make_reloader(tmp_path)
        body = path.read_text().replace('level = "INFO"', 'level = "DEBUG"')
        path.write_text(body)
        res = reloader.reload()
        assert "logging.level" in res.applied
        assert root.level == logging.DEBUG
    finally:
        root.setLevel(saved)


# ----------------------------------------------------------------------
# Restart-required refusal tests
# ----------------------------------------------------------------------


def test_reload_refuses_listen_change(tmp_path: Path) -> None:
    reloader, _, path = _make_reloader(tmp_path)
    body = path.read_text().replace(
        'listen = "127.0.0.1:5558"', 'listen = "127.0.0.1:5559"'
    )
    path.write_text(body)
    res = reloader.reload()
    fields = [f for f, _ in res.refused]
    assert "core.listen" in fields
    # Daemon kept the old listen address.
    assert reloader.current.listen_port == 5558


def test_reload_refuses_upstream_change(tmp_path: Path) -> None:
    reloader, _, path = _make_reloader(tmp_path)
    body = path.read_text() + (
        '\n[[core.upstream]]\nname = "second"\nhost = "127.0.0.1"\nport = 5557\n'
    )
    path.write_text(body)
    res = reloader.reload()
    fields = [f for f, _ in res.refused]
    assert "core.upstream" in fields
    # Daemon still has the original single upstream.
    assert len(reloader.current.upstreams) == 1


def test_reload_refuses_storage_change(tmp_path: Path) -> None:
    reloader, _, path = _make_reloader(tmp_path)
    new_db = tmp_path / "other.duckdb"
    body = path.read_text().replace(
        f'db_path = "{tmp_path / "lora.duckdb"}"',
        f'db_path = "{new_db}"',
    )
    path.write_text(body)
    res = reloader.reload()
    fields = [f for f, _ in res.refused]
    assert "core.storage" in fields
    # Daemon kept the old db_path.
    assert reloader.current.storage.db_path == tmp_path / "lora.duckdb"


def test_reload_refuses_aggregator_change(tmp_path: Path) -> None:
    reloader, _, path = _make_reloader(tmp_path)
    body = path.read_text().replace("window_ms = 200", "window_ms = 500")
    path.write_text(body)
    res = reloader.reload()
    fields = [f for f, _ in res.refused]
    assert "core.aggregator" in fields
    assert reloader.current.aggregator_window_ms == 200


def test_reload_refuses_fanout_queue_depth_change(tmp_path: Path) -> None:
    reloader, _, path = _make_reloader(tmp_path)
    body = path.read_text().replace(
        "fanout_queue_depth = 10000", "fanout_queue_depth = 50000"
    )
    path.write_text(body)
    res = reloader.reload()
    fields = [f for f, _ in res.refused]
    assert "core.fanout_queue_depth" in fields
    assert reloader.current.fanout_queue_depth == 10_000


# ----------------------------------------------------------------------
# Error / no-op paths
# ----------------------------------------------------------------------


def test_reload_handles_malformed_toml(tmp_path: Path) -> None:
    reloader, chain, path = _make_reloader(tmp_path)
    path.write_text("this is { not [ valid toml")
    res = reloader.reload()
    assert res.errors  # at least one parse error
    assert res.applied == []
    assert res.refused == []
    # Daemon kept its previous decoder set.
    assert [type(d).__name__ for d in chain.decoders] == [
        "MeshCoreDecoder",
        "RawDecoder",
    ]


def test_reload_idempotent(tmp_path: Path) -> None:
    reloader, _, _ = _make_reloader(tmp_path)
    # First call may apply the on-disk config-vs-defaults delta if any
    # — drain that. After the first call, current == effective.
    reloader.reload()

    res1 = reloader.reload()
    assert res1.applied == []
    assert res1.refused == []
    assert res1.errors == []

    res2 = reloader.reload()
    assert res2.applied == []
    assert res2.refused == []
    assert res2.errors == []
