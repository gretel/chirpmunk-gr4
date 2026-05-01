#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""TOML configuration loader for the lora package.

Phase 1 keeps the legacy ``apps/config.toml`` schema (the one consumed
by the C++ ``lora_trx`` / ``lora_scan`` binaries today). The validated
typed-dataclass loader for the new ``[core.*]`` / ``[bridge.*]``
sections lands in Phase 2 alongside the daemon. Until then this module
exposes the same accessor surface old scripts have always used.
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from typing import Any

# Search path for config.toml (first match wins):
#   1. ./config.toml                         (CWD, e.g. when running from apps/)
#   2. ./apps/config.toml                    (CWD = project root)
#   3. <package-root>/../../apps/config.toml (relative to scripts/src/lora/core/)
#   4. ~/.config/gr4-lora/config.toml        (user config)
_CONFIG_SEARCH = [
    Path("config.toml"),
    Path("apps/config.toml"),
    Path(__file__).resolve().parents[3] / "apps" / "config.toml",
    Path.home() / ".config" / "gr4-lora" / "config.toml",
]

_log = logging.getLogger(__name__)


def find_config(explicit: str | Path | None = None) -> Path | None:
    """Find config.toml by explicit path or search path. Returns None if not found."""
    if explicit is not None:
        p = Path(explicit)
        return p if p.is_file() else None
    for p in _CONFIG_SEARCH:
        if p.is_file():
            return p
    return None


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load config.toml and return the raw dict. Returns ``{}`` if not found.

    Logs a warning when no config file is found so the failure is
    visible. An explicit path that doesn't exist is also warned about.
    """
    if path is not None:
        p = Path(path)
        if not p.is_file():
            _log.warning("config file not found: %s — using defaults", p)
            return {}
    p = find_config(path)
    if p is None:
        _log.warning(
            "config.toml not found (searched: %s) — using defaults",
            ", ".join(str(s) for s in _CONFIG_SEARCH),
        )
        return {}
    _log.debug("loaded config: %s", p)
    with open(p, "rb") as f:
        return tomllib.load(f)


# ---- Legacy accessor functions (consumed by old *.py) -------
# Phase 2 introduces a typed `[core.*]` / `[bridge.*]` schema and replaces
# these with dataclass-based accessors that fail-fast on legacy `[aggregator]`
# / `[meshcore]` keys (per the spec's no-compat rule).


def config_udp_host(cfg: dict[str, Any]) -> str:
    """Extract UDP host from config.

    Looks under the ``[network]`` section first, then falls back to the
    flat top-level key. Default: ``127.0.0.1``.
    """
    net = cfg.get("network", {})
    return str(net.get("udp_listen", cfg.get("udp_listen", "127.0.0.1")))


def config_udp_port(cfg: dict[str, Any]) -> int:
    """Extract UDP port from config.

    Returns the lora_trx port (default 5555 when no ``[network]`` section,
    5556 when one is present). Scripts that want the aggregated consumer
    stream should use ``config_agg_listen`` instead.
    """
    net = cfg.get("network", {})
    return int(net.get("udp_port", cfg.get("udp_port", 5555)))


def config_agg_upstream(cfg: dict[str, Any]) -> str:
    """Extract lora_agg upstream address (lora_trx raw port) from ``[aggregator]``."""
    agg = cfg.get("aggregator", {})
    return str(agg.get("upstream", cfg.get("agg_upstream", "127.0.0.1:5556")))


def config_agg_listen(cfg: dict[str, Any]) -> str:
    """Extract lora_agg consumer listen address from ``[aggregator]``."""
    agg = cfg.get("aggregator", {})
    return str(agg.get("listen", cfg.get("agg_listen", "127.0.0.1:5555")))


def config_agg_window_ms(cfg: dict[str, Any]) -> int:
    """Extract lora_agg diversity combining window (ms) from ``[aggregator]``."""
    agg = cfg.get("aggregator", {})
    return int(agg.get("window_ms", cfg.get("agg_window_ms", 200)))


def config_region_scope(cfg: dict[str, Any]) -> str:
    """Extract region_scope from the ``[meshcore]`` section, defaulting to empty."""
    return str(cfg.get("meshcore", {}).get("region_scope", ""))
