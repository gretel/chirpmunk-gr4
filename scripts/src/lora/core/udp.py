#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""UDP subscriber helpers shared by the legacy lora_trx / lora_agg clients.

The lora_trx C++ daemon and the lora_agg Python aggregator both speak a
small CBOR-over-UDP protocol: clients send a ``subscribe`` map (optionally
filtered by sync_word) and the server broadcasts ``lora_frame`` /
``status`` / ``spectrum`` events. Re-sending ``subscribe`` acts as a
keepalive.

Phase 2 reimplements this for the new daemon, but the helpers here are
small enough to share verbatim with old viz tools during the transition.
"""

from __future__ import annotations

import socket
from typing import Any

import cbor2

#: Keepalive interval for UDP subscriber registration (seconds).
KEEPALIVE_INTERVAL = 5.0

#: Default receive timeout for UDP sockets (seconds).
RECV_TIMEOUT = 10.0

#: Default lora_trx host.
DEFAULT_HOST = "127.0.0.1"

#: Default lora_trx port.
DEFAULT_PORT = 5555


def parse_host_port(
    spec: str,
    default_host: str = DEFAULT_HOST,
    default_port: int = DEFAULT_PORT,
) -> tuple[str, int]:
    """Parse a HOST:PORT string, supporting IPv6 bracket notation.

    Formats:

      - ``"host:port"``  -> ``("host", port)``
      - ``"[::1]:5555"`` -> ``("::1", 5555)``
      - ``":5555"``      -> ``(default_host, 5555)``

    Raises ``ValueError`` if the format is invalid.
    """
    if spec.startswith("["):
        bracket = spec.index("]")
        host = spec[1:bracket]
        rest = spec[bracket + 1 :]
        if rest.startswith(":"):
            port = int(rest[1:])
        else:
            port = default_port
        return host, port

    colon = spec.rfind(":")
    if colon < 0:
        raise ValueError(
            f"invalid HOST:PORT spec '{spec}' (expected host:port, e.g. 127.0.0.1:5555)"
        )
    return spec[:colon], int(spec[colon + 1 :])


def resolve_udp_address(
    connect_arg: str | None,
    cfg: dict[str, Any],
) -> tuple[str, int]:
    """Resolve consumer UDP address from --connect arg or config.toml.

    Priority: explicit ``connect_arg`` > ``[aggregator].listen``
    (if section present) > ``[network].udp_port`` > defaults
    (``127.0.0.1:5555``).

    When lora_agg is configured, clients should connect to the
    aggregator (5555) rather than directly to lora_trx (5556). An empty
    ``[aggregator]`` section (``{}``) still counts as present — the C++
    side fills defaults from missing keys, and Python mirrors that
    semantic with ``is not None`` rather than a truthy check (which
    would reject ``{}``).
    """
    # Local import to avoid a circular dependency between config.py and
    # udp.py (config.py is small and doesn't reach back into udp).
    from .config import config_agg_listen, config_udp_host, config_udp_port

    if connect_arg:
        return parse_host_port(connect_arg)
    if cfg.get("aggregator") is not None:
        return parse_host_port(config_agg_listen(cfg))
    return config_udp_host(cfg), config_udp_port(cfg)


def create_udp_subscriber(
    host: str,
    port: int,
    *,
    sync_words: list[int] | None = None,
    timeout: float = RECV_TIMEOUT,
    blocking: bool = True,
) -> tuple[socket.socket, bytes, tuple[str, int]]:
    """Create a UDP socket, subscribe to lora_trx / lora_agg.

    Returns ``(sock, sub_msg, addr)`` where ``sub_msg`` can be
    re-sent as a keepalive.

    Args:
        host: Server host address.
        port: Server UDP port.
        sync_words: Optional sync_word filter (e.g. ``[0x12]`` for
            MeshCore).
        timeout: Socket receive timeout (seconds). Ignored when
            ``blocking`` is ``False``.
        blocking: If ``False``, set socket to non-blocking mode.
    """
    af = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(af, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("" if af == socket.AF_INET else "::", 0))
    if blocking:
        sock.settimeout(timeout)
    else:
        sock.setblocking(False)

    sub: dict[str, Any] = {"type": "subscribe"}
    if sync_words:
        sub["sync_word"] = sync_words
    sub_msg = cbor2.dumps(sub)
    addr = (host, port)
    sock.sendto(sub_msg, addr)
    return sock, sub_msg, addr
