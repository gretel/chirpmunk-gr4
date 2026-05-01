#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Single source of truth for MeshCore identity, keys, contacts, channels.

Phase 2B introduces :class:`IdentityStore` which is consumed by both
the daemon's decode pipeline (read-side, opts in to mtime watching)
and the bridge process (write-side, performs atomic mutations).
"""

from __future__ import annotations

from lora.identity.store import ContactRecord, IdentityConfig, IdentityStore

__all__ = ["ContactRecord", "IdentityConfig", "IdentityStore"]
