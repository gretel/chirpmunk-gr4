# SPDX-License-Identifier: ISC
"""lora-core daemon package — Phase 2 entry point for ``lora core``.

Wires the pieces shipped in Phase 2A-2D into a runnable asyncio process:

* multi-source UDP receivers (:mod:`lora.daemon.source`),
* L1+L2+L3 input validation (delegated to :mod:`lora.core.schema`),
* :class:`~lora.aggregator.DiversityAggregator` for diversity combining,
* :class:`~lora.daemon.decode_chain.DecodeChain` for protocol annotation,
* :class:`~lora.daemon.fanout.FanOut` for DuckDB persistence + UDP broadcast,
* :class:`~lora.daemon.tx_proxy.TxProxy` for relaying ``lora_tx`` to the
  named upstream,
* :class:`~lora.daemon.lifecycle.Lifecycle` for signal-driven shutdown.

The full typed ``[core.*]`` validator lands in Phase 2G; Phase 2E reads
the legacy ``apps/config.toml`` schema with sensible defaults so the
daemon can run alongside the existing ``lora_agg.py`` on a separate
listen port (5555).
"""

from __future__ import annotations

from lora.daemon.config import DaemonConfig, UpstreamConfig
from lora.daemon.main import main, run_daemon

__all__ = ["DaemonConfig", "UpstreamConfig", "main", "run_daemon"]
