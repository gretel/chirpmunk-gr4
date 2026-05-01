#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Daemon configuration adapter — Phase 2G strict typed schema.

The runtime data class :class:`DaemonConfig` is the asyncio code's
view of ``apps/config.toml``. The schema-level validator and bounds
checks live in :mod:`lora.core.config_typed`; this module performs
the small projection from the typed view into the flat runtime
fields the daemon's tasks consume.

No backwards compatibility
--------------------------

The legacy ``[aggregator]`` and ``[meshcore]`` top-level sections, and
the legacy single-string ``[core].upstream = "host:port"`` form, are
all rejected at load time by :func:`lora.core.config_typed.load_typed`.
:meth:`DaemonConfig.from_legacy_toml` is now a thin wrapper around
``load_typed`` + :meth:`DaemonConfig.from_typed`. The "legacy" name is
preserved only as the public entry point used by
:mod:`lora.daemon.main` and :mod:`lora.daemon.hot_reload`; it does not
imply any aliasing of legacy keys.

Listen-port behaviour
---------------------

This dataclass holds whatever ``[core].listen`` says, verbatim. The
``--listen HOST:PORT`` CLI flag overrides it via
:mod:`lora.daemon.main`'s ``_apply_listen_override``; the data class
itself stays a pure projection of the typed config.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from lora.core.config_typed import TypedConfig, load_typed
from lora.core.udp import parse_host_port
from lora.identity import IdentityConfig
from lora.storage import StorageConfig

_DEFAULT_DB_PATH: Path = Path("data/lora.duckdb")
_EMPTY_OPTIONS: Mapping[str, Mapping[str, Any]] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class UpstreamConfig:
    """One ``lora_trx`` instance the daemon subscribes to.

    ``strict=True`` (default) drops datagrams whose source address does
    not match the configured ``host:port``. Set ``False`` only for
    test rigs where the upstream is ephemeral.
    """

    name: str
    host: str
    port: int
    strict: bool = True


@dataclass(frozen=True, slots=True)
class DaemonConfig:
    """Flat runtime view of the typed ``[core.*]`` config.

    Field-by-field:

    * ``listen_host`` / ``listen_port`` — parsed from ``[core].listen``.
    * ``upstreams`` — one :class:`UpstreamConfig` per ``[[core.upstream]]``.
    * ``fanout_queue_depth`` — ``[core].fanout_queue_depth``.
    * ``aggregator_window_ms`` / ``aggregator_max_candidates`` —
      ``[core.aggregator]``.
    * ``decoders_enabled`` — tuple of decoder names from
      ``[core.decoders].enabled``.
    * ``decoders_options`` — ``{name: dict}`` from each
      ``[core.decoders.<name>]`` sub-table.
    * ``storage`` / ``identity`` — projection of ``[core.storage]`` and
      ``[core.identity]``.
    * ``log_level`` / ``log_color`` — ``[logging]`` (top-level, untyped
      by design — owned by the C++ daemon's section).
    """

    listen_host: str = "127.0.0.1"
    listen_port: int = 5555
    upstreams: tuple[UpstreamConfig, ...] = ()
    fanout_queue_depth: int = 10_000
    aggregator_window_ms: int = 200
    aggregator_max_candidates: int = 8
    decoders_enabled: tuple[str, ...] = ("meshcore", "lorawan", "raw")
    decoders_options: Mapping[str, Mapping[str, Any]] = field(
        default_factory=lambda: _EMPTY_OPTIONS
    )
    storage: StorageConfig = field(
        default_factory=lambda: StorageConfig(db_path=_DEFAULT_DB_PATH)
    )
    identity: IdentityConfig = field(default_factory=IdentityConfig.default)
    log_level: str = "INFO"
    log_color: bool = True

    @classmethod
    def from_typed(cls, typed: TypedConfig) -> DaemonConfig:
        """Project a :class:`TypedConfig` onto the flat runtime view."""
        listen_host, listen_port = parse_host_port(typed.core.listen)
        upstreams = tuple(
            UpstreamConfig(name=u.name, host=u.host, port=u.port, strict=u.strict)
            for u in typed.core.upstreams
        )
        decoders_options: dict[str, Mapping[str, Any]] = {
            name: MappingProxyType(dict(opts.options))
            for name, opts in typed.core.decoders.options.items()
        }
        storage = StorageConfig(
            db_path=typed.core.storage.db_path,
            batch_rows=typed.core.storage.batch_rows,
            batch_ms=typed.core.storage.batch_ms,
            queue_depth=typed.core.fanout_queue_depth,
            checkpoint_interval_s=typed.core.storage.checkpoint_interval_s,
        )
        identity = IdentityConfig(
            identity_file=typed.core.identity.identity_file,
            keys_dir=typed.core.identity.keys_dir,
            channels_dir=typed.core.identity.channels_dir,
            contacts_dir=typed.core.identity.contacts_dir,
            reload_interval_s=typed.core.identity.reload_interval_s,
        )
        return cls(
            listen_host=listen_host,
            listen_port=listen_port,
            upstreams=upstreams,
            fanout_queue_depth=typed.core.fanout_queue_depth,
            aggregator_window_ms=typed.core.aggregator.window_ms,
            aggregator_max_candidates=typed.core.aggregator.max_candidates,
            decoders_enabled=typed.core.decoders.enabled,
            decoders_options=MappingProxyType(decoders_options),
            storage=storage,
            identity=identity,
        )

    @classmethod
    def from_legacy_toml(cls, cfg: Mapping[str, Any]) -> DaemonConfig:
        """Validate ``cfg`` via :func:`load_typed`, then project to runtime form.

        The "legacy" name is historical — it pre-dates Phase 2G. The
        method name is kept for one release because the daemon entry
        point still calls it; new code should prefer
        :meth:`from_typed` directly when it already holds a
        :class:`TypedConfig`.

        ``[logging].level`` and ``[logging].color`` are read from the
        top-level ``[logging]`` table (owned by the C++ daemon), not
        the typed schema.
        """
        typed = load_typed(cfg)
        out = cls.from_typed(typed)
        logging_cfg = cfg.get("logging")
        if isinstance(logging_cfg, Mapping):
            log_level = (
                str(logging_cfg["level"]).upper()
                if "level" in logging_cfg
                else out.log_level
            )
            log_color = (
                bool(logging_cfg["color"]) if "color" in logging_cfg else out.log_color
            )
            return cls(
                listen_host=out.listen_host,
                listen_port=out.listen_port,
                upstreams=out.upstreams,
                fanout_queue_depth=out.fanout_queue_depth,
                aggregator_window_ms=out.aggregator_window_ms,
                aggregator_max_candidates=out.aggregator_max_candidates,
                decoders_enabled=out.decoders_enabled,
                decoders_options=out.decoders_options,
                storage=out.storage,
                identity=out.identity,
                log_level=log_level,
                log_color=log_color,
            )
        return out


@dataclass(frozen=True, slots=True)
class SupervisorConfig:
    """Runtime view of ``[startup]`` for the ``lora daemon`` supervisor.

    Independent of :class:`DaemonConfig` because the supervisor needs
    none of the ``[core.*]`` fields, and ``lora daemon`` must not
    error out on a missing ``[core]`` section (the operator may run
    a node where ``core`` is intentionally absent from
    ``[startup].services``).
    """

    services: tuple[str, ...]

    @classmethod
    def from_toml(cls, cfg: Mapping[str, Any]) -> SupervisorConfig:
        startup = cfg.get("startup")
        if not isinstance(startup, Mapping):
            raise ValueError("lora daemon: missing [startup] section in config.toml")
        services = startup.get("services")
        if services is None:
            raise ValueError(
                "lora daemon: missing [startup].services key in config.toml"
            )
        if not isinstance(services, list) or not all(
            isinstance(s, str) for s in services
        ):
            raise ValueError(
                "lora daemon: [startup].services must be a list of strings"
            )
        if not services:
            raise ValueError("lora daemon: [startup].services must be non-empty")
        return cls(services=tuple(services))
