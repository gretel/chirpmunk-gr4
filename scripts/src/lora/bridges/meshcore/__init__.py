#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""MeshCore companion-protocol bridge — Phase 4 split package.

Public re-exports keep import paths stable for downstream callers
(daemon, tests, CLI dispatcher).  The full surface lives in the
sub-modules:

* :mod:`lora.bridges.meshcore.protocol` — wire codec + constants
* :mod:`lora.bridges.meshcore.state` — :class:`BridgeState` + EEPROM CBOR
* :mod:`lora.bridges.meshcore.repeater` — flood-forwarding (PR #1810)
* :mod:`lora.bridges.meshcore.companion` — CMD/RESP/PUSH dispatchers
* :mod:`lora.bridges.meshcore.cli` — process entrypoint
"""

from __future__ import annotations

from lora.bridges.meshcore.companion import (
    handle_command,
    lora_frame_to_companion_msgs,
)
from lora.bridges.meshcore.protocol import (
    BRIDGE_PORT,
    CONTACT_RECORD_SIZE,
    FrameDecoder,
    frame_encode,
    parse_flood_scope,
)
from lora.bridges.meshcore.repeater import (
    DENYF_PAYLOAD_TYPES,
    MAX_PATH_SIZE,
    prepare_repeat_packet,
)
from lora.bridges.meshcore.state import (
    DEFAULT_EEPROM_PATH,
    EEPROM_SCHEMA_VERSION,
    BridgeState,
    LegacyJsonEepromError,
    initialize_eeprom_from_settings,
    load_eeprom,
    write_eeprom,
)

__all__ = [
    "BRIDGE_PORT",
    "BridgeState",
    "CONTACT_RECORD_SIZE",
    "DEFAULT_EEPROM_PATH",
    "DENYF_PAYLOAD_TYPES",
    "EEPROM_SCHEMA_VERSION",
    "FrameDecoder",
    "LegacyJsonEepromError",
    "MAX_PATH_SIZE",
    "frame_encode",
    "handle_command",
    "initialize_eeprom_from_settings",
    "load_eeprom",
    "lora_frame_to_companion_msgs",
    "parse_flood_scope",
    "prepare_repeat_packet",
    "write_eeprom",
]
