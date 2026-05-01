# SPDX-License-Identifier: ISC
"""Tests for :mod:`lora.core.udp` parsing + subscriber helpers.

The legacy ``resolve_udp_address`` priority chain (``--connect`` >
``[aggregator].listen`` > ``[network].udp_*`` > defaults) is exercised
end-to-end here. The strict typed-config path doesn't use these
accessors but the legacy ``lora_mon`` / ``lora_agg`` scripts still do.
"""

from __future__ import annotations

import socket

import cbor2
import pytest

from lora.core.udp import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    create_udp_subscriber,
    parse_host_port,
    resolve_udp_address,
)

# ---------------------------------------------------------------------------
# parse_host_port
# ---------------------------------------------------------------------------


def test_parse_host_port_basic() -> None:
    assert parse_host_port("127.0.0.1:5556") == ("127.0.0.1", 5556)


def test_parse_host_port_default_host() -> None:
    assert parse_host_port(":5556", default_host="example") == ("", 5556)


def test_parse_host_port_ipv6_bracket() -> None:
    assert parse_host_port("[::1]:5556") == ("::1", 5556)


def test_parse_host_port_ipv6_bracket_no_port_uses_default() -> None:
    assert parse_host_port("[::1]", default_port=5557) == ("::1", 5557)


def test_parse_host_port_no_colon_raises() -> None:
    with pytest.raises(ValueError, match=r"invalid HOST:PORT"):
        parse_host_port("just-a-host")


# ---------------------------------------------------------------------------
# resolve_udp_address — legacy priority chain
# ---------------------------------------------------------------------------


def test_resolve_explicit_arg_wins() -> None:
    assert resolve_udp_address("10.0.0.1:7000", {}) == ("10.0.0.1", 7000)


def test_resolve_aggregator_section_present_uses_listen() -> None:
    cfg = {"aggregator": {"listen": "127.0.0.1:5555"}}
    assert resolve_udp_address(None, cfg) == ("127.0.0.1", 5555)


def test_resolve_aggregator_empty_dict_still_counts_as_present() -> None:
    """Empty ``[aggregator] = {}`` triggers the listen-default branch."""
    # The legacy accessor returns 127.0.0.1:5555 by default for an
    # empty aggregator section.
    cfg: dict[str, object] = {"aggregator": {}}
    host, port = resolve_udp_address(None, cfg)
    assert host == "127.0.0.1"
    assert port == 5555


def test_resolve_falls_back_to_network_section() -> None:
    cfg = {"network": {"udp_listen": "127.0.0.1", "udp_port": 5556}}
    host, port = resolve_udp_address(None, cfg)
    assert host == "127.0.0.1"
    assert port == 5556


def test_resolve_defaults_when_nothing_set() -> None:
    host, port = resolve_udp_address(None, {})
    assert (host, port) == (DEFAULT_HOST, DEFAULT_PORT)


# ---------------------------------------------------------------------------
# create_udp_subscriber — round-trip the subscribe message
# ---------------------------------------------------------------------------


def test_create_udp_subscriber_round_trip() -> None:
    """Server-side: receive the subscribe sent by the helper, decode it."""
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    server.settimeout(2.0)
    server_host, server_port = server.getsockname()
    try:
        sock, sub_msg, addr = create_udp_subscriber(
            server_host, server_port, sync_words=[0x12, 0x34], timeout=1.0
        )
        try:
            data, _ = server.recvfrom(65536)
            assert data == sub_msg
            decoded = cbor2.loads(data)
            assert decoded["type"] == "subscribe"
            assert decoded["sync_word"] == [0x12, 0x34]
            assert addr == (server_host, server_port)
        finally:
            sock.close()
    finally:
        server.close()


def test_create_udp_subscriber_non_blocking() -> None:
    """Non-blocking mode produces a socket whose ``recvfrom`` does not block."""
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    try:
        host, port = server.getsockname()
        sock, _sub, _addr = create_udp_subscriber(host, port, blocking=False)
        try:
            with pytest.raises((BlockingIOError, OSError)):
                sock.recvfrom(1)
        finally:
            sock.close()
    finally:
        server.close()
