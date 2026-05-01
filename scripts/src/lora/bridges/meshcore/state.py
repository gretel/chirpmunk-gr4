#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""MeshCore bridge runtime state + CBOR EEPROM persistence.

Phase 4 split from ``meshcore_bridge.py``: this module owns
the long-lived per-process state of the bridge (identity material,
contacts, message queue, packet counters, send-scope, repeat mode) plus
the CBOR-encoded EEPROM file at ``data/meshcore/config.cbor``.

EEPROM format
-------------

CBOR map with the same nested layout as the legacy JSON file but with
typed integer keys preserved.  Atomic write via tempfile + ``os.replace``;
no JSON read path in production code (use the dedicated migrator
:mod:`lora.tools.migrate_meshcore_json` for legacy installs).
"""

from __future__ import annotations

import collections
import logging
import os
import struct
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import cbor2

from lora.bridges.meshcore.protocol import (
    CONTACT_RECORD_SIZE,
    REPEAT_FREQ_RANGES,
    RESP_ALLOWED_REPEAT_FREQ,
    RESP_BATTERY,
    RESP_CONTACT,
    RESP_CONTACT_END,
    RESP_CONTACT_START,
    RESP_CURRENT_TIME,
    RESP_DEVICE_INFO,
    RESP_ERROR,
    RESP_MSG_SENT,
    RESP_NO_MORE_MSGS,
    RESP_OK,
    RESP_SELF_INFO,
    RESP_STATS,
    build_channel_info,
    parse_flood_scope,
)
from lora.core.formatters import utf8_truncate
from lora.core.meshcore_crypto import GroupChannel
from lora.identity import IdentityStore

log = logging.getLogger("lora.bridges.meshcore.state")

#: EEPROM CBOR schema version — bump when the on-disk layout changes.
EEPROM_SCHEMA_VERSION: Final[int] = 1

#: Default EEPROM file (relative to the legacy ```` data dir).
#: The CLI overrides this via ``[bridge.meshcore].eeprom_path``.
DEFAULT_EEPROM_PATH: Final[Path] = (
    Path(__file__).resolve().parent.parent.parent.parent.parent
    / "scripts"
    / "apps"
    / "data"
    / "meshcore"
    / "config.cbor"
)


# ---------------------------------------------------------------------------
# EEPROM atomic I/O
# ---------------------------------------------------------------------------


class LegacyJsonEepromError(RuntimeError):
    """Raised when a legacy JSON EEPROM file is found in production.

    The bridge refuses to silently read JSON config and instructs the
    operator to invoke the explicit one-shot migrator.
    """


def _atomic_write_bytes(dest: Path, data: bytes) -> None:
    tmp = dest.with_suffix(dest.suffix + f".tmp.{os.getpid()}")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
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


def load_eeprom(path: Path) -> dict[str, Any]:
    """Read the EEPROM CBOR file at ``path``.

    Returns ``{}`` when the file is missing.  Raises
    :class:`LegacyJsonEepromError` when a sibling ``config.json`` exists
    but ``config.cbor`` is absent — the operator must run the explicit
    migrator.  Raises :class:`OSError` / ``cbor2.CBORDecodeError`` on
    other I/O or decode failures.
    """
    if path.is_file():
        try:
            data = cbor2.loads(path.read_bytes())
        except (cbor2.CBORDecodeError, ValueError) as exc:
            log.warning("EEPROM CBOR decode failed (%s): %s", path, exc)
            return {}
        if isinstance(data, dict) and data.get("version") == EEPROM_SCHEMA_VERSION:
            return data
        log.warning("EEPROM at %s has unexpected schema; ignoring", path)
        return {}
    legacy_json = path.with_suffix(".json")
    if legacy_json.is_file():
        raise LegacyJsonEepromError(
            f"legacy JSON EEPROM at {legacy_json}; CBOR not found at {path}. "
            f"Run: lora bridge meshcore migrate-json --from {legacy_json}"
        )
    return {}


def write_eeprom(path: Path, config: dict[str, Any]) -> None:
    """Atomically write ``config`` as CBOR to ``path``."""
    payload = cbor2.dumps(config)
    _atomic_write_bytes(path, payload)


def reset_eeprom(path: Path) -> None:
    """Delete the EEPROM file (factory-reset)."""
    try:
        if path.exists():
            path.unlink()
            log.info("EEPROM deleted (factory reset): %s", path)
    except OSError as exc:
        log.warning("could not delete EEPROM %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Contact persistence (147-byte raw records under ``contacts_dir``)
# ---------------------------------------------------------------------------


def load_persisted_contacts(contacts_dir: Path) -> dict[str, bytes]:
    """Load every ``*.contact`` in ``contacts_dir`` as a 147-byte record.

    Corrupt or wrong-size files are skipped with a warning.
    """
    out: dict[str, bytes] = {}
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
        out[data[:32].hex()] = data
    return out


def persist_contacts(contacts_dir: Path, contacts: dict[str, bytes]) -> None:
    """Atomically write each contact record to ``<pk_hex>.contact``."""
    contacts_dir.mkdir(parents=True, exist_ok=True)
    for pk_hex, record in contacts.items():
        _atomic_write_bytes(contacts_dir / f"{pk_hex}.contact", record)


def delete_persisted_contact(contacts_dir: Path, pk_hex: str) -> None:
    """Remove ``<pk_hex>.contact`` if present."""
    path = contacts_dir / f"{pk_hex}.contact"
    try:
        if path.exists():
            path.unlink()
    except OSError as exc:
        log.warning("could not delete contact file %s: %s", path, exc)


# ---------------------------------------------------------------------------
# EEPROM <-> BridgeState helpers
# ---------------------------------------------------------------------------


def initialize_eeprom_from_settings(
    settings: dict[str, Any], path: Path
) -> dict[str, Any]:
    """Build a fresh EEPROM payload from ``settings`` and persist it.

    Used on first boot when no EEPROM exists.  ``settings`` mirrors the
    legacy ``[meshcore]`` section of ``apps/config.toml``.
    """
    config: dict[str, Any] = {
        "version": EEPROM_SCHEMA_VERSION,
        "identity": {
            "name": str(settings.get("name", "lora_trx")),
            "lat_e6": int(float(settings.get("lat", 0.0)) * 1e6),
            "lon_e6": int(float(settings.get("lon", 0.0)) * 1e6),
        },
        "routing": {
            "region_scope": str(settings.get("region_scope", "")),
            "client_repeat": int(settings.get("client_repeat", 0)),
            "path_hash_mode": int(settings.get("path_hash_mode", 0)),
            "default_scope_name": "",
            "default_scope_key": b"\x00" * 16,
        },
        "contacts": {
            "autoadd_config": int(settings.get("autoadd_config", 0)),
            "autoadd_max_hops": min(int(settings.get("autoadd_max_hops", 0)), 64),
            "manual_add_contacts": 0,
        },
        "telemetry": {
            "telemetry_mode": 0,
            "adv_loc_policy": 0,
            "multi_acks": 0,
        },
        "security": {
            "device_pin": 0,
        },
    }
    flood_scope_raw = str(settings.get("flood_scope", ""))
    if flood_scope_raw and any(parse_flood_scope(flood_scope_raw)):
        config["routing"]["region_scope"] = flood_scope_raw
    write_eeprom(path, config)
    log.info("EEPROM initialized from settings: %s", path)
    return config


# ---------------------------------------------------------------------------
# BridgeState
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BridgeStartupConfig:
    """Snapshot of EEPROM-restored values at process start.

    Re-applied on every ``CMD_APP_START`` so a companion-session mutation
    does not survive a reconnect (mirrors the firmware behaviour).
    """

    name: str
    lat_e6: int
    lon_e6: int
    send_scope: bytes
    client_repeat: int
    path_hash_mode: int
    autoadd_config: int
    autoadd_max_hops: int
    default_scope_name: str
    default_scope_key: bytes


class BridgeState:
    """Long-lived runtime state for a MeshCore companion bridge process.

    Identity material (pubkey / expanded private / seed / known keys /
    channels) is sourced from a shared :class:`IdentityStore`.  The
    bridge owns its own 147-byte contact dictionary because the
    companion wire protocol round-trips raw records, not the structured
    :class:`lora.identity.ContactRecord` view.
    """

    def __init__(
        self,
        identity: IdentityStore,
        *,
        name: str,
        freq_mhz: float,
        bw_khz: float,
        sf: int,
        cr: int,
        tx_power: int,
        eeprom_path: Path = DEFAULT_EEPROM_PATH,
        lat_e6: int = 0,
        lon_e6: int = 0,
        region_scope: str = "",
        client_repeat: int = 0,
        path_hash_mode: int = 0,
        send_scope: bytes = b"\x00" * 16,
        autoadd_config: int = 0,
        autoadd_max_hops: int = 0,
        default_scope_name: str = "",
        default_scope_key: bytes = b"\x00" * 16,
        model: str = "lora_trx",
        contacts_dir: Path | None = None,
        git_rev: str = "dev",
    ) -> None:
        self.identity = identity
        self.eeprom_path = eeprom_path
        self.name = name
        self.freq_mhz = freq_mhz
        self.bw_khz = bw_khz
        self.sf = sf
        self.cr = cr
        self.tx_power = tx_power
        self.model = model
        self.git_rev = git_rev

        self.lat_e6: int = lat_e6
        self.lon_e6: int = lon_e6

        # Real radio stats — updated from received lora_frame messages.
        self.noise_floor_dbm: int = -120
        self.last_rssi_dbm: int = -128
        self.last_snr_db: float = 0.0

        # Inbound companion-protocol message queue.
        self.msg_queue: collections.deque[bytes] = collections.deque(maxlen=256)
        self.msg_seq = 0

        # Packet counters for STATS_TYPE_PACKETS.
        self.pkt_recv: int = 0
        self.pkt_sent: int = 0
        self.pkt_flood_tx: int = 0
        self.pkt_direct_tx: int = 0
        self.pkt_flood_rx: int = 0
        self.pkt_direct_rx: int = 0

        # Bridge-local contact store: pubkey_hex -> 147-byte record.
        self.contacts: dict[str, bytes] = {}
        self.contacts_lastmod = 0

        # TX echo / RX dedup tracking.
        self.recent_tx: dict[bytes, float] = {}
        self.recent_tx_ttl = 30.0
        self.pending_acks: dict[bytes, tuple[float, bytes]] = {}
        self.pending_ack_ttl = 60.0
        self.recent_rx: dict[bytes, float] = {}
        self.recent_rx_ttl = 5.0

        # Region scope (long-lived) and per-session send-scope (volatile).
        self.region_scope = region_scope
        self.region_key: bytes = (
            parse_flood_scope(region_scope) if region_scope else b""
        )
        self.send_scope: bytes = send_scope if len(send_scope) == 16 else b"\x00" * 16

        # Persistent default flood scope (CMD_SET_DEFAULT_FLOOD_SCOPE).
        # Restored onto send_scope by apply_startup_config() when non-zero.
        self.default_scope_name: str = default_scope_name
        self.default_scope_key: bytes = (
            default_scope_key if len(default_scope_key) == 16 else b"\x00" * 16
        )

        # Repeat mode + auto-add config (EEPROM-bound).
        self.client_repeat: int = client_repeat
        self.path_hash_mode: int = path_hash_mode
        self.autoadd_config: int = autoadd_config
        self.autoadd_max_hops: int = autoadd_max_hops

        # CMD_SET_OTHER_PARAMS in-memory state (also EEPROM-bound).
        self.manual_add_contacts: int = 0
        self.telemetry_mode: int = 0
        self.adv_loc_policy: int = 0
        self.multi_acks: int = 0
        self.device_pin: int = 0

        # Pending request tracking for RESP payload matching.
        self._pending_status: bytes = b""
        self._pending_discovery: bytes = b""

        # Contacts directory (override; defaults to identity store).
        self._contacts_dir_override: Path | None = contacts_dir

        # Snapshot for apply_startup_config().
        self._startup = BridgeStartupConfig(
            name=name,
            lat_e6=lat_e6,
            lon_e6=lon_e6,
            send_scope=self.send_scope,
            client_repeat=client_repeat,
            path_hash_mode=path_hash_mode,
            autoadd_config=autoadd_config,
            autoadd_max_hops=autoadd_max_hops,
            default_scope_name=self.default_scope_name,
            default_scope_key=self.default_scope_key,
        )

    # ------------------------------------------------------------------
    # Identity / store passthroughs
    # ------------------------------------------------------------------

    @property
    def pub_key(self) -> bytes:
        return self.identity.our_pub

    @property
    def expanded_prv(self) -> bytes:
        return self.identity.our_prv

    @property
    def seed(self) -> bytes:
        return self.identity.get_self_seed()

    @property
    def known_keys(self) -> dict[str, bytes]:
        return {hex_pk: bytes(pk) for hex_pk, pk in self.identity.known_keys.items()}

    @property
    def channels(self) -> list[GroupChannel]:
        return list(self.identity.channels)

    @property
    def keys_dir(self) -> Path:
        return self.identity._config.keys_dir

    @property
    def channels_dir(self) -> Path:
        return self.identity._config.channels_dir

    @property
    def contacts_dir(self) -> Path:
        return (
            self._contacts_dir_override
            if self._contacts_dir_override is not None
            else self.identity._config.contacts_dir
        )

    # ------------------------------------------------------------------
    # Identity-mutating helpers
    # ------------------------------------------------------------------

    def learn_pubkey(self, pubkey: bytes) -> bool:
        """Persist ``pubkey`` to the shared identity store; return True if new."""
        return self.identity.add_learned_pubkey(pubkey)

    def reload_identity(self) -> None:
        """Re-read the shared :class:`IdentityStore` from disk."""
        self.identity.reload()

    # ------------------------------------------------------------------
    # Channels (kept here so SET_CHANNEL stays fully synchronous)
    # ------------------------------------------------------------------

    def replace_channels(self, channels: Sequence[GroupChannel]) -> None:
        """Override the in-memory channel list (test / restore)."""
        # Identity-store-backed channels are append-only; for explicit
        # replacement we re-load.  Bridge-side mutations go through
        # save_channel() in the identity layer.
        self.identity.reload()

    # ------------------------------------------------------------------
    # Startup snapshot
    # ------------------------------------------------------------------

    def apply_startup_config(self) -> None:
        """Restore EEPROM-snapshot values onto the live state."""
        s = self._startup
        log.info(
            "apply_startup_config: name=%s send_scope=%s..",
            s.name,
            s.send_scope.hex()[:16] if s.send_scope else "empty",
        )
        self.name = s.name
        self.lat_e6 = s.lat_e6
        self.lon_e6 = s.lon_e6
        self.send_scope = s.send_scope
        self.client_repeat = s.client_repeat
        self.path_hash_mode = s.path_hash_mode
        self.autoadd_config = s.autoadd_config
        self.autoadd_max_hops = s.autoadd_max_hops
        self.default_scope_name = s.default_scope_name
        self.default_scope_key = s.default_scope_key
        # Firmware semantic: per-session send_scope reverts to the persistent
        # default on every CMD_APP_START / CMD_REBOOT when a default is set.
        # A subsequent CMD_SET_FLOOD_SCOPE will then override until reconnect.
        if any(s.default_scope_key):
            self.send_scope = s.default_scope_key

    def update_startup_mirrors(self) -> None:
        """Refresh the startup snapshot after an EEPROM-persisted change."""
        self._startup = BridgeStartupConfig(
            name=self.name,
            lat_e6=self.lat_e6,
            lon_e6=self.lon_e6,
            send_scope=self._startup.send_scope,  # CLI controls scope per-session
            client_repeat=self.client_repeat,
            path_hash_mode=self.path_hash_mode,
            autoadd_config=self.autoadd_config,
            autoadd_max_hops=self.autoadd_max_hops,
            default_scope_name=self.default_scope_name,
            default_scope_key=self.default_scope_key,
        )

    @property
    def startup(self) -> BridgeStartupConfig:
        return self._startup

    # ------------------------------------------------------------------
    # Companion-protocol response builders
    # ------------------------------------------------------------------

    def build_self_info(self) -> bytes:
        """Build ``RESP_SELF_INFO`` (0x05) payload."""
        freq_field = int(self.freq_mhz * 1000)
        bw_field = int(self.bw_khz * 1000)
        buf = bytearray()
        buf.append(RESP_SELF_INFO)
        buf.append(0x01)  # adv_type: chat
        buf.append(self.tx_power & 0xFF)
        buf.append(22)  # max_tx_power dBm
        buf.extend(self.pub_key)
        buf.extend(struct.pack("<i", self.lat_e6))
        buf.extend(struct.pack("<i", self.lon_e6))
        buf.append(self.multi_acks)
        buf.append(self.adv_loc_policy)
        buf.append(self.telemetry_mode)
        buf.append(self.manual_add_contacts)
        buf.extend(struct.pack("<I", freq_field))
        buf.extend(struct.pack("<I", bw_field))
        buf.append(self.sf)
        buf.append(self.cr)
        buf.extend(utf8_truncate(self.name, 32))
        return bytes(buf)

    def build_device_info(self) -> bytes:
        """Build ``RESP_DEVICE_INFO`` (0x0D) payload (82 bytes total)."""
        fw_build = utf8_truncate("lora_trx", 12).ljust(12, b"\x00")
        model_b = utf8_truncate(self.model, 40).ljust(40, b"\x00")
        version_b = utf8_truncate(self.git_rev, 20).ljust(20, b"\x00")
        buf = bytearray()
        buf.append(RESP_DEVICE_INFO)
        buf.append(9)  # fw_ver (protocol version 9)
        buf.append(64)  # max_contacts / 2
        buf.append(8)  # max_channels
        buf.extend(struct.pack("<I", 0))  # ble_pin
        buf.extend(fw_build)
        buf.extend(model_b)
        buf.extend(version_b)
        buf.append(self.client_repeat)
        buf.append(self.path_hash_mode)
        return bytes(buf)

    def build_battery(self) -> bytes:
        buf = bytearray()
        buf.append(RESP_BATTERY)
        buf.extend(struct.pack("<H", 4200))
        buf.extend(struct.pack("<I", 0))
        buf.extend(struct.pack("<I", 1024))
        return bytes(buf)

    def build_ok(self, value: int = 0) -> bytes:
        return bytes([RESP_OK]) + struct.pack("<I", value)

    def build_error(self, code: int = 1) -> bytes:
        return bytes([RESP_ERROR, code])

    def build_current_time(self) -> bytes:
        return bytes([RESP_CURRENT_TIME]) + struct.pack("<I", int(time.time()))

    def build_no_more_msgs(self) -> bytes:
        return bytes([RESP_NO_MORE_MSGS])

    def build_contacts_response(self) -> list[bytes]:
        frames: list[bytes] = []
        count = len(self.contacts)
        frames.append(bytes([RESP_CONTACT_START]) + struct.pack("<I", count))
        for record in self.contacts.values():
            frames.append(bytes([RESP_CONTACT]) + record)
        frames.append(
            bytes([RESP_CONTACT_END]) + struct.pack("<I", self.contacts_lastmod)
        )
        return frames

    def build_msg_sent(
        self, flood: bool = False, ack_hash: bytes | None = None
    ) -> bytes:
        if ack_hash is not None:
            ack_tag = ack_hash[:4]
        else:
            self.msg_seq += 1
            ack_tag = struct.pack("<I", self.msg_seq)
        timeout = struct.pack("<I", 30000)
        return bytes([RESP_MSG_SENT, 1 if flood else 0]) + ack_tag + timeout

    def build_channel_info(self, idx: int) -> bytes:
        ch_list = self.channels
        ch = ch_list[idx] if idx < len(ch_list) else None
        return build_channel_info(idx, ch)

    def build_stats(self, stats_type: int) -> bytes:
        buf = bytearray()
        buf.append(RESP_STATS)
        buf.append(stats_type)
        if stats_type == 0:
            buf.extend(struct.pack("<H", 5000))
            buf.extend(struct.pack("<I", 0))
            buf.extend(struct.pack("<H", 0))
            buf.append(len(self.msg_queue))
        elif stats_type == 1:
            buf.extend(struct.pack("<h", self.noise_floor_dbm))
            buf.extend(struct.pack("<b", max(-128, min(127, self.last_rssi_dbm))))
            snr_i8 = max(-128, min(127, int(self.last_snr_db * 4)))
            buf.extend(struct.pack("<b", snr_i8))
            buf.extend(struct.pack("<I", 0))
            buf.extend(struct.pack("<I", 0))
        elif stats_type == 2:
            buf.extend(
                struct.pack(
                    "<IIIIII",
                    self.pkt_recv,
                    self.pkt_sent,
                    self.pkt_flood_tx,
                    self.pkt_direct_tx,
                    self.pkt_flood_rx,
                    self.pkt_direct_rx,
                )
            )
        return bytes(buf)

    def build_allowed_repeat_freq(self) -> bytes:
        buf = bytearray([RESP_ALLOWED_REPEAT_FREQ])
        for lo, hi in REPEAT_FREQ_RANGES:
            buf += struct.pack("<II", lo, hi)
        return bytes(buf)

    # ------------------------------------------------------------------
    # EEPROM persistence
    # ------------------------------------------------------------------

    def persist_config(self) -> None:
        """Write the EEPROM-bound subset of state to ``self.eeprom_path``."""
        config: dict[str, Any] = {
            "version": EEPROM_SCHEMA_VERSION,
            "identity": {
                "name": self.name,
                "lat_e6": self.lat_e6,
                "lon_e6": self.lon_e6,
            },
            "routing": {
                "region_scope": self.region_scope,
                "client_repeat": self.client_repeat,
                "path_hash_mode": self.path_hash_mode,
                "default_scope_name": self.default_scope_name,
                "default_scope_key": self.default_scope_key,
            },
            "contacts": {
                "autoadd_config": self.autoadd_config,
                "autoadd_max_hops": self.autoadd_max_hops,
                "manual_add_contacts": self.manual_add_contacts,
            },
            "telemetry": {
                "telemetry_mode": self.telemetry_mode,
                "adv_loc_policy": self.adv_loc_policy,
                "multi_acks": self.multi_acks,
            },
            "security": {
                "device_pin": self.device_pin,
            },
        }
        try:
            write_eeprom(self.eeprom_path, config)
        except OSError as exc:
            log.warning("could not persist EEPROM to %s: %s", self.eeprom_path, exc)
            return
        self.update_startup_mirrors()
        log.debug("EEPROM saved to %s", self.eeprom_path)

    def reset_config(self) -> None:
        """Delete the EEPROM file (factory reset)."""
        reset_eeprom(self.eeprom_path)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "BridgeStartupConfig",
    "BridgeState",
    "DEFAULT_EEPROM_PATH",
    "EEPROM_SCHEMA_VERSION",
    "LegacyJsonEepromError",
    "delete_persisted_contact",
    "initialize_eeprom_from_settings",
    "load_eeprom",
    "load_persisted_contacts",
    "persist_contacts",
    "reset_eeprom",
    "write_eeprom",
]
