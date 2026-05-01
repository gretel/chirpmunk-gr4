#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Phase 2G typed ``[core.*]`` / ``[bridge.*]`` config schema.

Strict no-compat validator: the legacy ``[aggregator]`` and
``[meshcore]`` top-level sections, plus the legacy single-string
``[core].upstream = "host:port"`` form, all fail-fast at load time
with a hint pointing at the new schema.

See ``docs/superpowers/specs/2026-04-27-lora-core-refactor-design.md``
Section 5 for the canonical TOML layout and bounds table.

Listen-port behaviour during the parallel-run phase
---------------------------------------------------

The in-config default (``[core].listen = "127.0.0.1:5555"``) is the
canonical post-cutover state. Operators can override at the CLI with
``--listen HOST:PORT``; the parallel-run :5558 shim was retired in
Phase 7 along with the legacy ``lora_agg``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from lora.core.udp import parse_host_port

_LEGACY_HINT = (
    "no compat — see docs/superpowers/specs/"
    "2026-04-27-lora-core-refactor-design.md Section 5"
)


class TypedConfigError(ValueError):
    """Raised on legacy keys, missing required fields, or bound violations."""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TypedUpstream:
    """One ``[[core.upstream]]`` entry."""

    name: str
    host: str
    port: int
    strict: bool = True


@dataclass(frozen=True, slots=True)
class TypedCoreAggregator:
    """``[core.aggregator]`` table."""

    window_ms: int = 200
    max_candidates: int = 8


@dataclass(frozen=True, slots=True)
class TypedDecoderOptions:
    """Per-decoder option bag.

    Accepts arbitrary keys per ``[core.decoders.<name>]`` sub-table;
    the decoder itself validates them at construction time via its
    ``__init__`` signature.
    """

    options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TypedCoreDecoders:
    """``[core.decoders]`` table + per-decoder option sub-tables."""

    enabled: tuple[str, ...]
    options: Mapping[str, TypedDecoderOptions] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TypedCoreIdentity:
    """``[core.identity]`` table."""

    identity_file: Path
    keys_dir: Path
    channels_dir: Path
    contacts_dir: Path
    reload_interval_s: float = 5.0


@dataclass(frozen=True, slots=True)
class TypedCoreStorage:
    """``[core.storage]`` table."""

    backend: str = "duckdb"
    db_path: Path = Path("data/lora.duckdb")
    batch_rows: int = 256
    batch_ms: int = 1000
    checkpoint_interval_s: int = 300


@dataclass(frozen=True, slots=True)
class TypedCore:
    """``[core]`` + sub-tables (aggregator, decoders, identity, storage)."""

    listen: str  # "host:port"
    upstreams: tuple[TypedUpstream, ...]
    aggregator: TypedCoreAggregator
    decoders: TypedCoreDecoders
    identity: TypedCoreIdentity
    storage: TypedCoreStorage
    fanout_queue_depth: int = 10000


@dataclass(frozen=True, slots=True)
class TypedBridgeMeshcore:
    """``[bridge.meshcore]`` table (optional, defaults applied)."""

    listen_port: int = 7834
    tx_via_core: bool = True
    eeprom_path: Path = Path("data/meshcore/config.cbor")


@dataclass(frozen=True, slots=True)
class TypedBridgeSerial:
    """``[bridge.serial]`` table (optional, defaults applied)."""

    listen_port: int = 7835


@dataclass(frozen=True, slots=True)
class TypedConfig:
    """Top-level typed view of ``apps/config.toml``."""

    core: TypedCore
    bridge_meshcore: TypedBridgeMeshcore
    bridge_serial: TypedBridgeSerial


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_typed(cfg: Mapping[str, Any]) -> TypedConfig:
    """Validate ``cfg`` against the Phase 2G schema and return a ``TypedConfig``.

    Raises :class:`TypedConfigError` on:

    * Legacy ``[aggregator]`` / ``[meshcore]`` top-level keys.
    * Legacy string-form ``[core].upstream = "host:port"``.
    * Missing required sections / fields.
    * Bound violations (see Section 5 of the spec).
    * Unknown decoder name in ``[core.decoders].enabled``.
    """
    # ---- 0. Legacy fail-fast ---------------------------------------
    if "aggregator" in cfg:
        raise TypedConfigError(
            f"legacy [aggregator] key found; move to [core.aggregator] ({_LEGACY_HINT})"
        )
    if "meshcore" in cfg:
        raise TypedConfigError(
            "legacy [meshcore] key found; bridge keys move to [bridge.meshcore], "
            f"identity / advert fields are EEPROM-persisted ({_LEGACY_HINT})"
        )

    core_raw = cfg.get("core")
    if not isinstance(core_raw, Mapping):
        raise TypedConfigError(f"missing required [core] section ({_LEGACY_HINT})")

    # ---- 1. [[core.upstream]] -------------------------------------
    upstream_raw = core_raw.get("upstream")
    if isinstance(upstream_raw, str):
        raise TypedConfigError(
            "core.upstream must be array of [[core.upstream]] tables; "
            f"string form not supported ({_LEGACY_HINT})"
        )
    if not isinstance(upstream_raw, list) or not upstream_raw:
        raise TypedConfigError(
            f"[[core.upstream]] must be a non-empty array of tables ({_LEGACY_HINT})"
        )
    upstreams: list[TypedUpstream] = []
    seen_names: set[str] = set()
    for i, item in enumerate(upstream_raw):
        if not isinstance(item, Mapping):
            raise TypedConfigError(
                f"[[core.upstream]][{i}] must be a table, got {type(item).__name__}"
            )
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise TypedConfigError(
                f"[[core.upstream]][{i}].name must be a non-empty string"
            )
        if name in seen_names:
            raise TypedConfigError(
                f"[[core.upstream]][{i}].name {name!r} duplicates an earlier entry"
            )
        seen_names.add(name)
        host = item.get("host")
        if not isinstance(host, str) or not host:
            raise TypedConfigError(
                f"[[core.upstream]][{i}].host must be a non-empty string"
            )
        port = _require_int(item.get("port"), f"[[core.upstream]][{i}].port")
        if not 1 <= port <= 65535:
            raise TypedConfigError(
                f"[[core.upstream]][{i}].port must be 1..65535, got {port}"
            )
        strict_raw = item.get("strict", True)
        if not isinstance(strict_raw, bool):
            raise TypedConfigError(
                f"[[core.upstream]][{i}].strict must be a bool, got "
                f"{type(strict_raw).__name__}"
            )
        upstreams.append(
            TypedUpstream(name=name, host=host, port=port, strict=strict_raw)
        )

    # ---- 2. [core] scalars ----------------------------------------
    listen_raw = core_raw.get("listen")
    if not isinstance(listen_raw, str) or not listen_raw:
        raise TypedConfigError("[core].listen must be a non-empty 'host:port' string")
    # Validate parseability now so a bad string fails at validation time.
    try:
        parse_host_port(listen_raw)
    except ValueError as exc:
        raise TypedConfigError(f"[core].listen: {exc}") from exc

    fanout_queue_depth = _require_int(
        core_raw.get("fanout_queue_depth", 10000),
        "[core].fanout_queue_depth",
    )
    if not 256 <= fanout_queue_depth <= 1_000_000:
        raise TypedConfigError(
            "[core].fanout_queue_depth must be 256..1_000_000, "
            f"got {fanout_queue_depth}"
        )

    # ---- 3. [core.aggregator] -------------------------------------
    agg_raw = core_raw.get("aggregator", {})
    if not isinstance(agg_raw, Mapping):
        raise TypedConfigError("[core.aggregator] must be a table")
    window_ms = _require_int(
        agg_raw.get("window_ms", 200), "[core.aggregator].window_ms"
    )
    if not 0 <= window_ms <= 10000:
        raise TypedConfigError(
            f"[core.aggregator].window_ms must be 0..10000, got {window_ms}"
        )
    max_candidates = _require_int(
        agg_raw.get("max_candidates", 8), "[core.aggregator].max_candidates"
    )
    if max_candidates < 1:
        raise TypedConfigError(
            f"[core.aggregator].max_candidates must be >= 1, got {max_candidates}"
        )
    aggregator = TypedCoreAggregator(window_ms=window_ms, max_candidates=max_candidates)

    # ---- 4. [core.decoders] + sub-tables --------------------------
    decoders_raw = core_raw.get("decoders")
    if not isinstance(decoders_raw, Mapping):
        raise TypedConfigError("missing required [core.decoders] section")
    enabled_raw = decoders_raw.get("enabled")
    if not isinstance(enabled_raw, list) or not enabled_raw:
        raise TypedConfigError(
            "[core.decoders].enabled must be a non-empty list of decoder names"
        )
    # REGISTRY import here (not at module top) so the import graph stays
    # acyclic even if a decoder ends up importing config helpers.
    from lora.decoders import REGISTRY

    enabled_names: list[str] = []
    for n in enabled_raw:
        if not isinstance(n, str) or not n:
            raise TypedConfigError(
                "[core.decoders].enabled entries must be non-empty strings"
            )
        if n not in REGISTRY:
            raise TypedConfigError(
                f"[core.decoders].enabled contains unknown plugin {n!r}; "
                f"known: {sorted(REGISTRY)}"
            )
        enabled_names.append(n)

    options: dict[str, TypedDecoderOptions] = {}
    for k, v in decoders_raw.items():
        if k == "enabled":
            continue
        if not isinstance(v, Mapping):
            # Tolerate scalars at decoder-options level only if they are
            # a typo — but per the strict policy we reject.
            raise TypedConfigError(
                f"[core.decoders.{k}] must be a table, got {type(v).__name__}"
            )
        options[str(k)] = TypedDecoderOptions(
            options=MappingProxyType(dict(v)),
        )
    decoders = TypedCoreDecoders(
        enabled=tuple(enabled_names),
        options=MappingProxyType(options),
    )

    # ---- 5. [core.identity] ---------------------------------------
    identity_raw = core_raw.get("identity")
    if not isinstance(identity_raw, Mapping):
        raise TypedConfigError("missing required [core.identity] section")
    identity = _build_identity(identity_raw)

    # ---- 6. [core.storage] ----------------------------------------
    storage_raw = core_raw.get("storage", {})
    if not isinstance(storage_raw, Mapping):
        raise TypedConfigError("[core.storage] must be a table")
    storage = _build_storage(storage_raw)

    core = TypedCore(
        listen=listen_raw,
        upstreams=tuple(upstreams),
        aggregator=aggregator,
        decoders=decoders,
        identity=identity,
        storage=storage,
        fanout_queue_depth=fanout_queue_depth,
    )

    # ---- 7. [bridge.meshcore] / [bridge.serial] -------------------
    bridge_raw = cfg.get("bridge", {})
    if not isinstance(bridge_raw, Mapping):
        raise TypedConfigError("[bridge] must be a table")

    bm_raw = bridge_raw.get("meshcore", {})
    if not isinstance(bm_raw, Mapping):
        raise TypedConfigError("[bridge.meshcore] must be a table")
    bm_listen = _require_int(
        bm_raw.get("listen_port", 7834), "[bridge.meshcore].listen_port"
    )
    if not 1 <= bm_listen <= 65535:
        raise TypedConfigError(
            f"[bridge.meshcore].listen_port must be 1..65535, got {bm_listen}"
        )
    bm_tx_via = bm_raw.get("tx_via_core", True)
    if not isinstance(bm_tx_via, bool):
        raise TypedConfigError(
            "[bridge.meshcore].tx_via_core must be a bool, got "
            f"{type(bm_tx_via).__name__}"
        )
    bm_eeprom = Path(str(bm_raw.get("eeprom_path", "data/meshcore/config.cbor")))
    bridge_meshcore = TypedBridgeMeshcore(
        listen_port=bm_listen,
        tx_via_core=bm_tx_via,
        eeprom_path=bm_eeprom,
    )

    bs_raw = bridge_raw.get("serial", {})
    if not isinstance(bs_raw, Mapping):
        raise TypedConfigError("[bridge.serial] must be a table")
    bs_listen = _require_int(
        bs_raw.get("listen_port", 7835), "[bridge.serial].listen_port"
    )
    if not 1 <= bs_listen <= 65535:
        raise TypedConfigError(
            f"[bridge.serial].listen_port must be 1..65535, got {bs_listen}"
        )
    bridge_serial = TypedBridgeSerial(listen_port=bs_listen)

    return TypedConfig(
        core=core,
        bridge_meshcore=bridge_meshcore,
        bridge_serial=bridge_serial,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_int(value: Any, label: str) -> int:
    """Coerce ``value`` to ``int`` or raise :class:`TypedConfigError`."""
    if isinstance(value, bool):  # bool is a subclass of int in Python
        raise TypedConfigError(f"{label} must be an int, got bool")
    if isinstance(value, int):
        return value
    raise TypedConfigError(f"{label} must be an int, got {type(value).__name__}")


def _build_identity(table: Mapping[str, Any]) -> TypedCoreIdentity:
    """Build a :class:`TypedCoreIdentity` from a raw ``[core.identity]`` table."""
    keys: tuple[str, ...] = (
        "identity_file",
        "keys_dir",
        "channels_dir",
        "contacts_dir",
    )
    paths: dict[str, Path] = {}
    for k in keys:
        v = table.get(k)
        if v is None:
            raise TypedConfigError(f"[core.identity].{k} is required")
        if not isinstance(v, str) or not v:
            raise TypedConfigError(f"[core.identity].{k} must be a non-empty string")
        p = Path(v)
        if not _path_creatable(p):
            raise TypedConfigError(
                f"[core.identity].{k}={v!r} is not creatable "
                "(no existing writable ancestor directory)"
            )
        paths[k] = p

    reload_interval_s_raw = table.get("reload_interval_s", 5.0)
    if isinstance(reload_interval_s_raw, bool) or not isinstance(
        reload_interval_s_raw, (int, float)
    ):
        raise TypedConfigError(
            "[core.identity].reload_interval_s must be a number, got "
            f"{type(reload_interval_s_raw).__name__}"
        )
    reload_interval_s = float(reload_interval_s_raw)
    if reload_interval_s <= 0:
        raise TypedConfigError(
            f"[core.identity].reload_interval_s must be > 0, got {reload_interval_s}"
        )

    return TypedCoreIdentity(
        identity_file=paths["identity_file"],
        keys_dir=paths["keys_dir"],
        channels_dir=paths["channels_dir"],
        contacts_dir=paths["contacts_dir"],
        reload_interval_s=reload_interval_s,
    )


def _build_storage(table: Mapping[str, Any]) -> TypedCoreStorage:
    """Build a :class:`TypedCoreStorage` from a raw ``[core.storage]`` table."""
    backend_raw = table.get("backend", "duckdb")
    if not isinstance(backend_raw, str) or not backend_raw:
        raise TypedConfigError("[core.storage].backend must be a non-empty string")
    db_path_raw = table.get("db_path", "data/lora.duckdb")
    if not isinstance(db_path_raw, str) or not db_path_raw:
        raise TypedConfigError("[core.storage].db_path must be a non-empty string")
    db_path = Path(db_path_raw)

    batch_rows = _require_int(table.get("batch_rows", 256), "[core.storage].batch_rows")
    if not 1 <= batch_rows <= 65536:
        raise TypedConfigError(
            f"[core.storage].batch_rows must be 1..65536, got {batch_rows}"
        )
    batch_ms = _require_int(table.get("batch_ms", 1000), "[core.storage].batch_ms")
    if not 1 <= batch_ms <= 60000:
        raise TypedConfigError(
            f"[core.storage].batch_ms must be 1..60000, got {batch_ms}"
        )
    checkpoint_interval_s = _require_int(
        table.get("checkpoint_interval_s", 300),
        "[core.storage].checkpoint_interval_s",
    )
    if checkpoint_interval_s < 1:
        raise TypedConfigError(
            "[core.storage].checkpoint_interval_s must be >= 1, "
            f"got {checkpoint_interval_s}"
        )
    return TypedCoreStorage(
        backend=backend_raw,
        db_path=db_path,
        batch_rows=batch_rows,
        batch_ms=batch_ms,
        checkpoint_interval_s=checkpoint_interval_s,
    )


def _path_creatable(p: Path) -> bool:
    """Return ``True`` if ``p`` exists OR an ancestor is a writable directory.

    "Creatable" is interpreted as "we could ``mkdir -p`` to ``p`` later":
    we walk up ``p``'s ancestors until we find an existing entry, and
    require that entry to be a writable directory.
    """
    try:
        absolute = p if p.is_absolute() else (Path.cwd() / p)
    except OSError:
        return False
    cur = absolute
    while True:
        if cur.exists():
            if cur.is_dir():
                return os.access(cur, os.W_OK)
            # ``cur`` exists as a non-directory (file/symlink/etc.).
            # Treat as creatable iff the containing directory is writable
            # — we can overwrite the file in place.
            parent = cur.parent
            return parent.is_dir() and os.access(parent, os.W_OK)
        parent = cur.parent
        if parent == cur:
            return False
        cur = parent


__all__ = [
    "TypedBridgeMeshcore",
    "TypedBridgeSerial",
    "TypedConfig",
    "TypedConfigError",
    "TypedCore",
    "TypedCoreAggregator",
    "TypedCoreDecoders",
    "TypedCoreIdentity",
    "TypedCoreStorage",
    "TypedDecoderOptions",
    "TypedUpstream",
    "load_typed",
]
