#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Legacy ``data/meshcore/config.json`` → CBOR EEPROM migrator.

Phase 4 deliberately drops the JSON read path from production code: the
bridge runs only off ``config.cbor``.  This one-shot tool converts an
existing JSON EEPROM into the new CBOR layout so operators can switch
without losing per-node settings.

Usage::

    lora bridge meshcore migrate-json --from data/meshcore/config.json
    lora bridge meshcore migrate-json --from data/meshcore/config.json --delete-source
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from lora.bridges.meshcore.state import EEPROM_SCHEMA_VERSION, write_eeprom

log = logging.getLogger("lora.tools.migrate_meshcore_json")


def migrate(source: Path, *, delete_source: bool = False) -> Path:
    """Read ``source`` (legacy JSON), write a sibling ``config.cbor``.

    Returns the destination path.  ``delete_source`` removes the JSON
    file after a successful write.
    """
    if not source.is_file():
        raise FileNotFoundError(f"source EEPROM JSON not found: {source}")

    raw = source.read_text()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"could not decode JSON at {source}: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"JSON EEPROM at {source} must be an object at the root")

    if data.get("version") != EEPROM_SCHEMA_VERSION:
        raise ValueError(
            f"JSON EEPROM at {source} has version "
            f"{data.get('version')!r}, expected {EEPROM_SCHEMA_VERSION}"
        )

    dest = source.with_suffix(".cbor")
    write_eeprom(dest, data)
    log.info("EEPROM migrated: %s → %s", source, dest)

    if delete_source:
        source.unlink()
        log.info("removed legacy JSON: %s", source)

    return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lora bridge meshcore migrate-json",
        description="Convert a legacy MeshCore-bridge config.json to CBOR.",
    )
    parser.add_argument(
        "--from",
        dest="source",
        type=Path,
        required=True,
        metavar="PATH",
        help="Path to the legacy config.json",
    )
    parser.add_argument(
        "--delete-source",
        action="store_true",
        help="Remove the JSON file after a successful write",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        dest = migrate(args.source, delete_source=args.delete_source)
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(dest)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
