#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Tests for ``lora.hwtests.tx_test`` helpers.

End-to-end TX requires real hardware. These tests verify the library
plumbing replacing the legacy ``meshcore_tx.py`` subprocess hops:

* :func:`get_sdr_pubkey` returns 64-char hex from the local identity.
* :func:`sdr_tx_advert` / :func:`sdr_tx_msg` send a CBOR ``lora_tx``
  datagram to the configured UDP host:port.
"""

from __future__ import annotations

import socket

import cbor2

from lora.hwtests.tx_test import get_sdr_pubkey, sdr_tx_advert, sdr_tx_msg


def _free_udp_port() -> tuple[socket.socket, int]:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    return s, s.getsockname()[1]


class TestGetSdrPubkey:
    def test_returns_64_char_hex(self) -> None:
        pk = get_sdr_pubkey()
        assert len(pk) == 64
        assert all(c in "0123456789abcdef" for c in pk)


class TestSdrTxAdvert:
    def test_sends_lora_tx_cbor_with_phy_overrides(self) -> None:
        sock, port = _free_udp_port()
        sock.settimeout(2.0)
        try:
            assert sdr_tx_advert(
                host="127.0.0.1", port=port, freq_mhz=869.618, sf=8, bw=62500
            )
            data, _ = sock.recvfrom(65536)
        finally:
            sock.close()
        msg = cbor2.loads(data)
        assert msg["type"] == "lora_tx"
        assert msg["sf"] == 8
        assert msg["bw"] == 62500
        assert msg["freq"] == 869_618_000
        assert isinstance(msg["payload"], bytes)
        assert len(msg["payload"]) > 0


class TestSdrTxMsg:
    def test_sends_encrypted_txt_msg(self) -> None:
        sock, port = _free_udp_port()
        sock.settimeout(2.0)
        # Use the local identity's own pubkey as a syntactically-valid
        # destination — encryption succeeds, no hardware needed.
        dest = get_sdr_pubkey()
        try:
            assert sdr_tx_msg(
                host="127.0.0.1",
                port=port,
                dest_pubkey=dest,
                text="hi",
                freq_mhz=869.618,
                sf=8,
                bw=62500,
            )
            data, _ = sock.recvfrom(65536)
        finally:
            sock.close()
        msg = cbor2.loads(data)
        assert msg["type"] == "lora_tx"
        assert msg["sf"] == 8
        assert msg["bw"] == 62500
        assert isinstance(msg["payload"], bytes)

    def test_returns_false_on_bad_pubkey(self) -> None:
        # Non-hex; encryption raises → wrapper returns False.
        assert not sdr_tx_msg(
            host="127.0.0.1",
            port=1,
            dest_pubkey="zz",
            text="hi",
            freq_mhz=869.618,
            sf=8,
            bw=62500,
        )
