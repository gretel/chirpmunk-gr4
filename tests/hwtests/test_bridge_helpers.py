#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Tests for ``lora.hwtests.bridge_test`` pure helpers (Phase 5B)."""

from __future__ import annotations

import re
from typing import Any

from lora.hwtests.bridge_test import (
    bridge_payload,
    collect_bridge,
    extract_pubkey_from_card,
)
from lora.hwtests.matrix import ConfigPoint


class TestBridgePayload:
    def test_format_matches_legacy(self) -> None:
        p = ConfigPoint(sf=8, bw=62500, freq_mhz=869.618)
        s = bridge_payload(p)
        # Format: sf{sf}bw{bw_khz}f{freq_khz}_{nonce:02x}
        m = re.fullmatch(r"sf8bw62f869618_[0-9a-f]{2}", s)
        assert m is not None, f"unexpected format: {s!r}"

    def test_unique_nonce_across_calls(self) -> None:
        p = ConfigPoint(sf=8, bw=62500, freq_mhz=869.618)
        # Not strictly unique per the API, but the nonce-portion should
        # vary across many calls. Check there are at least 8 distinct
        # values in 256 invocations.
        suffixes = {bridge_payload(p)[-2:] for _ in range(256)}
        assert len(suffixes) >= 8


class TestExtractPubkeyFromCard:
    def test_parses_uri_form(self) -> None:
        pubkey = "deadbeef" * 8
        uri = f"meshcore://contact/add?name=alice&public_key={pubkey}&type=1"
        pk = extract_pubkey_from_card(uri)
        assert pk == pubkey

    def test_parses_raw_bizcard_form(self) -> None:
        # ADVERT wire format: header(1) + path_len(1) + pubkey(32) + extra
        header_path = "0102"  # 4 hex chars
        pubkey = "ab" * 32  # 64 hex chars
        rest = "ff" * 16
        uri = f"meshcore://{header_path}{pubkey}{rest}"
        pk = extract_pubkey_from_card(uri)
        assert pk == "ab" * 32

    def test_returns_empty_when_no_match(self) -> None:
        assert extract_pubkey_from_card("nothing here") == ""
        assert extract_pubkey_from_card("meshcore://short") == ""

    def test_lowercases_hex(self) -> None:
        upper = "ABCDEF" + "00" * 29
        uri = f"meshcore://contact/add?public_key={upper}"
        assert extract_pubkey_from_card(uri) == upper.lower()


class TestCollectBridge:
    def _row(self, **kw: bool | None) -> dict[str, Any]:
        base: dict[str, Any] = {
            "advert_rx": False,
            "advert_tx": False,
            "msg_tx": False,
            "msg_tx_acked": False,
            "msg_rx": False,
            "chan_tx": False,
        }
        base.update(kw)
        return base

    def test_empty_list(self) -> None:
        s = collect_bridge([])
        assert s["mode"] == "bridge"
        assert s["points"] == 0
        assert s["pass_rate"] == 0
        assert s["advert_rx"] == 0

    def test_aggregates_per_phase_counts(self) -> None:
        rows = [
            self._row(
                advert_rx=True,
                advert_tx=True,
                msg_tx=True,
                msg_rx=True,
                msg_tx_acked=True,
                chan_tx=True,
            ),
            self._row(advert_rx=True),
            self._row(advert_tx=True, msg_tx=True),
            self._row(),
        ]
        s = collect_bridge(rows)
        assert s["points"] == 4
        assert s["advert_rx"] == 2
        assert s["advert_tx"] == 2
        assert s["msg_tx"] == 2
        assert s["msg_tx_acked"] == 1
        assert s["msg_rx"] == 1
        assert s["chan_tx"] == 1

    def test_pass_rate_uses_4_phases_per_point(self) -> None:
        # 4 points × 4 phases = 16 max; 4 advert_rx + 4 advert_tx +
        # 4 msg_tx + 4 msg_rx = 16 → 1.0.
        rows = [
            self._row(advert_rx=True, advert_tx=True, msg_tx=True, msg_rx=True)
            for _ in range(4)
        ]
        s = collect_bridge(rows)
        assert s["pass_rate"] == 1.0

    def test_pass_rate_zero(self) -> None:
        s = collect_bridge([self._row(), self._row()])
        assert s["pass_rate"] == 0.0
