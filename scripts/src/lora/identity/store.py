#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Unified MeshCore identity / keys / contacts / channels store.

Single source of truth consumed by:

- the daemon's decode pipeline (read-side; opts in to mtime watching);
- the bridge process (write-side; performs atomic mutations).

Crypto / parser primitives are reused from
:mod:`lora.core.meshcore_crypto`; this module only adds the file-store
+ caching + atomic-write layer plus a richer contact wrapper. The
147-byte contact serialization itself stays inside
``meshcore_bridge.py`` until Phase 4 lifts it out.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from lora.core.meshcore_crypto import (
    DEFAULT_DATA_DIR,
    PUB_KEY_SIZE,
    GroupChannel,
    load_channels,
    load_known_keys,
    load_or_create_identity,
)
from lora.identity.watch import MtimeWatcher

log = logging.getLogger("lora.identity")

# ---------------------------------------------------------------------------
# Constants — MeshCore contact record (147 bytes, layout per
# meshcore_bridge.py::_build_contact_record)
# ---------------------------------------------------------------------------

CONTACT_RECORD_SIZE: Final[int] = 147
_CONTACT_NAME_OFFSET: Final[int] = 99  # pubkey(32)+type+flags+path_len+path(64) = 99
_CONTACT_NAME_LEN: Final[int] = 32


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IdentityConfig:
    """File-system layout for the IdentityStore.

    Defaults align with :data:`lora.core.meshcore_crypto.DEFAULT_DATA_DIR`
    so a process started without TOML wiring still finds the bundled
    seed channels.  Phase 2G will populate this from
    ``[core.identity]`` in ``apps/config.toml``.
    """

    identity_file: Path
    keys_dir: Path
    channels_dir: Path
    contacts_dir: Path
    reload_interval_s: float = 5.0

    @classmethod
    def default(cls) -> IdentityConfig:
        base = DEFAULT_DATA_DIR
        return cls(
            identity_file=base / "identity.bin",
            keys_dir=base / "keys",
            channels_dir=base / "channels",
            contacts_dir=base / "contacts",
        )


@dataclass(frozen=True, slots=True)
class ContactRecord:
    """Wrapper around a MeshCore 147-byte contact serialization.

    The identity layer treats ``raw`` as opaque — only ``name`` and
    ``pub_key`` are interpreted here. Phase 4 (bridge split) will move
    the full serialization into this module; for now the bridge
    process is the authoritative writer.
    """

    name: str
    pub_key: bytes
    raw: bytes

    @classmethod
    def from_bytes(cls, raw: bytes) -> ContactRecord:
        if len(raw) != CONTACT_RECORD_SIZE:
            raise ValueError(
                f"contact record must be {CONTACT_RECORD_SIZE} bytes, got {len(raw)}"
            )
        pub_key = bytes(raw[:PUB_KEY_SIZE])
        name_bytes = raw[
            _CONTACT_NAME_OFFSET : _CONTACT_NAME_OFFSET + _CONTACT_NAME_LEN
        ]
        name = name_bytes.rstrip(b"\x00").decode("utf-8", errors="replace")
        return cls(name=name, pub_key=pub_key, raw=bytes(raw))


# ---------------------------------------------------------------------------
# IdentityStore
# ---------------------------------------------------------------------------


class IdentityStore:
    """Single source of truth for MeshCore key / channel / contact state.

    Lazy-loads on first read.  All mutating operations write atomically
    (tempfile + ``os.replace``).  The optional mtime watcher is
    daemon-only and **must** be started explicitly via :meth:`start_watch`.
    """

    def __init__(self, config: IdentityConfig) -> None:
        self._config = config
        self._lock = threading.RLock()
        self._loaded = False
        self._our_prv: bytes = b""
        self._our_pub: bytes = b""
        self._our_seed: bytes = b""
        self._known_keys: dict[str, bytes] = {}
        self._channels: list[GroupChannel] = []
        self._contacts: list[ContactRecord] = []
        self._watcher: MtimeWatcher | None = None

    # -- properties (MeshCoreKeys Protocol surface) -----------------------

    @property
    def our_prv(self) -> bytes:
        self._ensure_loaded()
        return self._our_prv

    @property
    def our_pub(self) -> bytes:
        self._ensure_loaded()
        return self._our_pub

    @property
    def known_keys(self) -> Mapping[str, bytes]:
        self._ensure_loaded()
        return self._known_keys

    @property
    def channels(self) -> Sequence[GroupChannel]:
        self._ensure_loaded()
        return self._channels

    # -- self-identity accessors ------------------------------------------

    def get_self_pubkey(self) -> bytes:
        self._ensure_loaded()
        return self._our_pub

    def get_self_seed(self) -> bytes:
        self._ensure_loaded()
        return self._our_seed

    def get_self_expanded_prv(self) -> bytes:
        self._ensure_loaded()
        return self._our_prv

    # -- key / contact mutators -------------------------------------------

    def add_learned_pubkey(self, pubkey: bytes, name: str | None = None) -> bool:
        """Atomically persist `pubkey` to ``keys_dir``.

        Returns ``True`` if newly added, ``False`` if already present.
        ``name`` is currently advisory — the on-disk filename is the
        hex pubkey, matching :func:`lora.core.meshcore_crypto.save_pubkey`.
        """
        del name  # accepted for API parity; on-disk format is fixed
        if len(pubkey) != PUB_KEY_SIZE:
            raise ValueError(f"pubkey must be {PUB_KEY_SIZE} bytes, got {len(pubkey)}")
        with self._lock:
            self._ensure_loaded()
            hex_pk = pubkey.hex()
            if hex_pk in self._known_keys:
                return False
            self._config.keys_dir.mkdir(parents=True, exist_ok=True)
            dest = self._config.keys_dir / f"{hex_pk}.key"
            _atomic_write_bytes(dest, bytes(pubkey))
            self._known_keys[hex_pk] = bytes(pubkey)
            return True

    def add_contact(self, record: ContactRecord) -> bool:
        """Atomically persist a contact under its 6-byte hex prefix.

        Returns ``True`` if newly added, ``False`` if already present.
        Bridge-side persistence uses ``<full_hex>.contact`` so we do
        the same for compat.
        """
        if len(record.pub_key) != PUB_KEY_SIZE:
            raise ValueError(
                f"contact pub_key must be {PUB_KEY_SIZE} bytes, got "
                f"{len(record.pub_key)}"
            )
        if len(record.raw) != CONTACT_RECORD_SIZE:
            raise ValueError(
                f"contact raw must be {CONTACT_RECORD_SIZE} bytes, got "
                f"{len(record.raw)}"
            )
        with self._lock:
            self._ensure_loaded()
            already = any(c.pub_key == record.pub_key for c in self._contacts)
            if already:
                return False
            self._config.contacts_dir.mkdir(parents=True, exist_ok=True)
            dest = self._config.contacts_dir / f"{record.pub_key.hex()}.contact"
            _atomic_write_bytes(dest, record.raw)
            self._contacts.append(record)
            return True

    def remove_contact(self, pub_key_prefix: bytes) -> bool:
        """Delete a contact by 6-byte (or longer) pubkey prefix.

        Returns ``True`` on success, ``False`` if no match.
        """
        if len(pub_key_prefix) < 1:
            return False
        with self._lock:
            self._ensure_loaded()
            for i, c in enumerate(self._contacts):
                if c.pub_key.startswith(pub_key_prefix):
                    path = self._config.contacts_dir / f"{c.pub_key.hex()}.contact"
                    try:
                        path.unlink(missing_ok=True)
                    except OSError as exc:
                        log.warning("could not unlink contact %s: %s", path, exc)
                        return False
                    del self._contacts[i]
                    return True
        return False

    # -- lookups ----------------------------------------------------------

    def lookup_pubkey(self, name_or_hex_prefix: str) -> bytes | None:
        """Resolve a pubkey by contact name OR hex-pubkey prefix.

        Hex prefix wins on ambiguity (it is the more specific query).
        Returns ``None`` if nothing matches.
        """
        with self._lock:
            self._ensure_loaded()
            needle = name_or_hex_prefix.lower()
            try:
                bytes.fromhex(needle)
                is_hex = True
            except ValueError:
                is_hex = False
            if is_hex:
                for hex_pk, raw in self._known_keys.items():
                    if hex_pk.startswith(needle):
                        return raw
                for c in self._contacts:
                    if c.pub_key.hex().startswith(needle):
                        return c.pub_key
            for c in self._contacts:
                if c.name == name_or_hex_prefix:
                    return c.pub_key
            return None

    def lookup_name(self, pubkey: bytes) -> str | None:
        """Return the contact name for `pubkey`, or ``None`` if unknown."""
        with self._lock:
            self._ensure_loaded()
            for c in self._contacts:
                if c.pub_key == pubkey:
                    return c.name
            return None

    # -- collection accessors (cached) ------------------------------------

    def contacts(self) -> Sequence[ContactRecord]:
        with self._lock:
            self._ensure_loaded()
            return tuple(self._contacts)

    # -- cache lifecycle --------------------------------------------------

    def reload(self) -> None:
        """Drop in-memory caches and re-read every store from disk."""
        with self._lock:
            self._loaded = False
            self._do_load()

    def warm_up(self) -> None:
        """Eagerly load the on-disk stores and emit an INFO summary.

        Called from the daemon boot path so operators see how many
        keys / channels / contacts the daemon picked up before the
        first packet arrives. Subsequent reads short-circuit on
        :attr:`_loaded`.
        """
        with self._lock:
            self._ensure_loaded()
            log.info(
                "identity: identity_file=%s keys=%d channels=%d contacts=%d "
                "reload_s=%.1f",
                self._config.identity_file,
                len(self._known_keys),
                len(self._channels),
                len(self._contacts),
                self._config.reload_interval_s,
            )

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            self._do_load()

    def _do_load(self) -> None:
        cfg = self._config
        cfg.identity_file.parent.mkdir(parents=True, exist_ok=True)
        prv, pub, seed = load_or_create_identity(cfg.identity_file)
        self._our_prv = prv
        self._our_pub = pub
        self._our_seed = seed
        self._known_keys = load_known_keys(cfg.keys_dir)
        self._channels = load_channels(cfg.channels_dir)
        self._contacts = _load_contacts(cfg.contacts_dir)
        self._loaded = True

    # -- mtime watcher ----------------------------------------------------

    def start_watch(self) -> None:
        """Begin polling the on-disk stores for mutations.

        Daemon-side only.  Idempotent — calling twice is a no-op.
        """
        with self._lock:
            if self._watcher is not None:
                return
            cfg = self._config
            self._watcher = MtimeWatcher(
                paths=[
                    cfg.identity_file,
                    cfg.keys_dir,
                    cfg.channels_dir,
                    cfg.contacts_dir,
                ],
                callback=self.reload,
                interval_s=cfg.reload_interval_s,
            )
            self._watcher.start()

    def stop_watch(self) -> None:
        with self._lock:
            w = self._watcher
            self._watcher = None
        if w is not None:
            w.stop()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _atomic_write_bytes(dest: Path, data: bytes) -> None:
    """Write `data` to `dest` atomically (tempfile + ``os.replace``).

    A failure during ``os.replace`` leaves the destination either at
    its previous content (if any) or absent — never partial.  The
    tempfile is best-effort cleaned up on failure.
    """
    tmp = dest.with_suffix(dest.suffix + f".tmp.{os.getpid()}")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dest)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _load_contacts(contacts_dir: Path) -> list[ContactRecord]:
    """Read every ``*.contact`` in `contacts_dir` as a ContactRecord.

    Returns an empty list if the directory is missing or empty.
    Corrupt records (wrong size) are skipped with a warning — same
    policy as the bridge's ``_load_persisted_contacts``.
    """
    out: list[ContactRecord] = []
    if not contacts_dir.is_dir():
        return out
    for f in sorted(contacts_dir.glob("*.contact")):
        try:
            data = f.read_bytes()
        except OSError as exc:
            log.warning("could not read contact %s: %s", f, exc)
            continue
        if len(data) != CONTACT_RECORD_SIZE:
            log.warning(
                "skipping corrupt contact %s (%d bytes, expected %d)",
                f,
                len(data),
                CONTACT_RECORD_SIZE,
            )
            continue
        out.append(ContactRecord.from_bytes(data))
    return out
