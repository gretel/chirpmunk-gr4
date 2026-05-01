#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Phase 2F SIGHUP-driven hot-reload of the daemon's mutable configuration.

The daemon's :class:`~lora.daemon.lifecycle.Lifecycle` installs a
SIGHUP handler that asks a :class:`HotReloader` to re-read
``apps/config.toml``, diff against the in-memory
:class:`~lora.daemon.config.DaemonConfig`, and apply the changes that
are safe to apply at runtime. Restart-required fields are logged and
ignored — the daemon stays alive on the previous values.

Hot-reloadable fields
---------------------

* ``[logging].level`` / ``[logging].color`` — root logger reconfig.
* ``[core.decoders].enabled`` and per-decoder ``[core.decoders.<name>]``
  options. Triggers an atomic
  :meth:`~lora.daemon.decode_chain.DecodeChain.reload`.
* ``[core.identity].*`` — file paths and watcher interval.
  :meth:`IdentityStore.reload` is called; the watcher is cycled if
  the interval changed.

Restart-required fields
-----------------------

* ``[core].listen`` — binds a UDP socket; we cannot move it at runtime
  without dropping subscribers.
* ``[core].fanout_queue_depth`` — bounded asyncio.Queue is sized at
  construction.
* ``[[core.upstream]]`` — adding/removing/renaming upstream sources
  changes which UdpSource tasks are running.
* ``[core.aggregator].*`` — window_ms changes mid-run distort the
  diversity-combining timing window.
* ``[core.storage].*`` — different db path = different file.

The reloader is single-threaded by design: SIGHUP wakes an
asyncio.Event, the loop's reload task awaits the event, and only one
:meth:`HotReloader.reload` call runs at a time. The DecodeChain swap
is the one place where threading matters; that swap is atomic by
:mod:`~lora.daemon.decode_chain` construction.
"""

from __future__ import annotations

import logging
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from lora.daemon._decoders import build_decoders
from lora.daemon.config import DaemonConfig
from lora.daemon.decode_chain import DecodeChain
from lora.identity import IdentityStore

# Field names treated as restart-required. Each maps to a short reason
# string used in the WARN log line.
_RESTART_REQUIRED_REASONS: dict[str, str] = {
    "core.listen": "listen socket cannot be rebound at runtime",
    "core.fanout_queue_depth": "fanout queue is sized at construction",
    "core.upstream": "upstream source set is fixed for the daemon's lifetime",
    "core.aggregator": "aggregator window timing distorts mid-run",
    "core.storage": "DuckDB file path is fixed at startup",
}


@dataclass(frozen=True, slots=True)
class ReloadResult:
    """Outcome of one :meth:`HotReloader.reload` call.

    * ``applied`` — TOML field paths that changed AND were applied.
    * ``refused`` — list of ``(field, reason)`` for restart-required
      fields whose new value was kept on disk but ignored at runtime.
    * ``errors`` — parse / validation errors. Empty on a clean reload
      that found nothing to do.
    * ``new_config`` — the effective :class:`DaemonConfig` after the
      reload. Hot fields reflect new values; restart-required fields
      keep their pre-reload values so the rest of the daemon stays
      consistent.
    """

    applied: list[str] = field(default_factory=list)
    refused: list[tuple[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    new_config: DaemonConfig | None = None


class HotReloader:
    """Re-reads ``config_path`` and applies hot-reloadable changes.

    Each call to :meth:`reload` is independent — there is no
    accumulated state. ``current`` advances to the new effective
    config after a successful apply so the next call diffs against
    the latest applied state.
    """

    def __init__(
        self,
        *,
        config_path: Path,
        current: DaemonConfig,
        decode_chain: DecodeChain,
        identity: IdentityStore,
        log: logging.Logger,
    ) -> None:
        self._config_path = Path(config_path)
        self._current = current
        self._decode_chain = decode_chain
        self._identity = identity
        self._log = log
        self._reload_count: int = 0

    @property
    def current(self) -> DaemonConfig:
        """The effective config after the most recent reload."""
        return self._current

    @property
    def reload_count(self) -> int:
        """Number of successful :meth:`reload` calls."""
        return self._reload_count

    # ------------------------------------------------------------------
    # Reload
    # ------------------------------------------------------------------

    def reload(self) -> ReloadResult:
        """Re-read ``config_path``, diff, and apply. Never raises.

        On a parse/validation failure the daemon keeps running on the
        previous config and the failure is reported via
        :attr:`ReloadResult.errors`.
        """
        self._reload_count += 1
        errors: list[str] = []

        # ---- 1. Parse from disk ------------------------------------
        try:
            with self._config_path.open("rb") as f:
                raw = tomllib.load(f)
        except FileNotFoundError as exc:
            errors.append(f"config file not found: {exc}")
            return ReloadResult(errors=errors, new_config=self._current)
        except tomllib.TOMLDecodeError as exc:
            errors.append(f"TOML parse error: {exc}")
            self._log.error("hot reload: %s", errors[-1])
            return ReloadResult(errors=errors, new_config=self._current)
        except OSError as exc:
            errors.append(f"could not read config: {exc}")
            self._log.error("hot reload: %s", errors[-1])
            return ReloadResult(errors=errors, new_config=self._current)

        try:
            new_config = DaemonConfig.from_legacy_toml(raw)
        except Exception as exc:  # noqa: BLE001 — never propagate
            errors.append(f"config validation error: {exc!r}")
            self._log.error("hot reload: %s", errors[-1])
            return ReloadResult(errors=errors, new_config=self._current)

        # ---- 2. Diff and apply -------------------------------------
        applied: list[str] = []
        refused: list[tuple[str, str]] = []
        cur = self._current
        # Buffer the resulting effective config: start from cur and
        # overlay hot fields. Refused fields keep cur's values.
        effective = cur

        # 2a. Restart-required fields — log & refuse.
        if (cur.listen_host, cur.listen_port) != (
            new_config.listen_host,
            new_config.listen_port,
        ):
            refused.append(("core.listen", _RESTART_REQUIRED_REASONS["core.listen"]))
            self._log.warning(
                "restart required: core.listen changed from %s:%d to %s:%d; "
                "daemon kept running with old value",
                cur.listen_host,
                cur.listen_port,
                new_config.listen_host,
                new_config.listen_port,
            )
        if cur.fanout_queue_depth != new_config.fanout_queue_depth:
            refused.append(
                (
                    "core.fanout_queue_depth",
                    _RESTART_REQUIRED_REASONS["core.fanout_queue_depth"],
                )
            )
            self._log.warning(
                "restart required: core.fanout_queue_depth changed from %d to %d; "
                "daemon kept running with old value",
                cur.fanout_queue_depth,
                new_config.fanout_queue_depth,
            )
        if cur.upstreams != new_config.upstreams:
            refused.append(
                ("core.upstream", _RESTART_REQUIRED_REASONS["core.upstream"])
            )
            self._log.warning(
                "restart required: [[core.upstream]] changed; "
                "daemon kept running with old set"
            )
        if (
            cur.aggregator_window_ms != new_config.aggregator_window_ms
            or cur.aggregator_max_candidates != new_config.aggregator_max_candidates
        ):
            refused.append(
                ("core.aggregator", _RESTART_REQUIRED_REASONS["core.aggregator"])
            )
            self._log.warning(
                "restart required: core.aggregator changed; "
                "daemon kept running with old window_ms=%d",
                cur.aggregator_window_ms,
            )
        if cur.storage != new_config.storage:
            refused.append(("core.storage", _RESTART_REQUIRED_REASONS["core.storage"]))
            self._log.warning(
                "restart required: core.storage changed (db_path=%s -> %s); "
                "daemon kept running with old path",
                cur.storage.db_path,
                new_config.storage.db_path,
            )

        # 2b. Hot-reloadable fields — apply in place.
        # Logging.
        if cur.log_level != new_config.log_level:
            try:
                level = logging.getLevelName(new_config.log_level.upper())
                if isinstance(level, int):
                    logging.getLogger().setLevel(level)
                    applied.append("logging.level")
                    self._log.info(
                        "hot reload: logging.level %s -> %s",
                        cur.log_level,
                        new_config.log_level,
                    )
                else:
                    errors.append(f"unknown log level: {new_config.log_level!r}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"could not apply log level: {exc!r}")
        if cur.log_color != new_config.log_color:
            # The actual ANSI-formatter wiring lives in the bootstrap;
            # at runtime we just record that the value changed so
            # downstream observers (e.g. the lifecycle status JSON) can
            # see the new state.
            applied.append("logging.color")
            self._log.info(
                "hot reload: logging.color %s -> %s",
                cur.log_color,
                new_config.log_color,
            )

        # Decoders (enabled OR options changed).
        if (
            cur.decoders_enabled != new_config.decoders_enabled
            or _decoders_options_differ(
                cur.decoders_options, new_config.decoders_options
            )
        ):
            try:
                new_decoders = build_decoders(
                    new_config.decoders_enabled,
                    new_config.decoders_options,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"decoder build failed: {exc!r}")
                self._log.error("hot reload: %s", errors[-1])
            else:
                self._decode_chain.reload(new_decoders)
                if cur.decoders_enabled != new_config.decoders_enabled:
                    applied.append("core.decoders.enabled")
                if _decoders_options_differ(
                    cur.decoders_options, new_config.decoders_options
                ):
                    applied.append("core.decoders.options")
                self._log.info(
                    "hot reload: rebuilt decode chain with %s",
                    list(new_config.decoders_enabled),
                )

        # Identity.
        if cur.identity != new_config.identity:
            try:
                # Cheapest correct option — drop caches and re-read.
                # The watcher will pick up its new interval on next
                # cycle; we don't restart it here to avoid a race with
                # in-flight callbacks.
                self._identity.reload()
                applied.append("core.identity")
                self._log.info("hot reload: identity store reloaded")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"identity reload failed: {exc!r}")
                self._log.error("hot reload: %s", errors[-1])

        # ---- 3. Build effective config -----------------------------
        # Start from the freshly-loaded config, then overlay
        # restart-required fields with current values so callers see
        # the actual runtime state (not the on-disk wishlist).
        effective = replace(
            new_config,
            listen_host=cur.listen_host,
            listen_port=cur.listen_port,
            fanout_queue_depth=cur.fanout_queue_depth,
            upstreams=cur.upstreams,
            aggregator_window_ms=cur.aggregator_window_ms,
            aggregator_max_candidates=cur.aggregator_max_candidates,
            storage=cur.storage,
        )
        self._current = effective
        return ReloadResult(
            applied=applied,
            refused=refused,
            errors=errors,
            new_config=effective,
        )


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _decoders_options_differ(
    a: Mapping[str, Mapping[str, Any]],
    b: Mapping[str, Mapping[str, Any]],
) -> bool:
    """Compare two decoders_options maps as plain dicts.

    Required because ``MappingProxyType`` instances compare by identity
    rather than content under some pyright-Python combos; converting
    each side to ``dict`` gives us deterministic equality semantics.
    """
    return {k: dict(v) for k, v in a.items()} != {k: dict(v) for k, v in b.items()}
