#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""``lora bridge meshcore`` — process entrypoint.

Phase 4 split from ``meshcore_bridge.py:main()``.  Loads
the typed config, wires :class:`IdentityStore` + :class:`BridgeState`,
opens the UDP subscriber + TCP listener, and runs the
selectors-based event loop.
"""

from __future__ import annotations

import argparse
import logging
import selectors
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import cbor2

try:
    import setproctitle
except ImportError:  # pragma: no cover - setproctitle in pyproject deps
    setproctitle = None  # type: ignore[assignment]

from lora.bridges.meshcore.companion import handle_command, lora_frame_to_companion_msgs
from lora.bridges.meshcore.protocol import (
    BRIDGE_PORT,
    PUSH_ACK,
    PUSH_MESSAGES_WAITING,
    FrameDecoder,
    frame_encode,
    parse_flood_scope,
)
from lora.bridges.meshcore.repeater import (
    expire_recent_tx,
    is_rx_duplicate,
    is_tx_echo,
    prepare_repeat_packet,
)
from lora.bridges.meshcore.state import (
    BridgeState,
    LegacyJsonEepromError,
    initialize_eeprom_from_settings,
    load_eeprom,
)
from lora.core.cbor_stream import apply_config, wait_for_config
from lora.core.config import load_config
from lora.core.config_typed import TypedConfigError, load_typed
from lora.core.logging import add_logging_args, setup_logging
from lora.core.meshcore_crypto import (
    DEFAULT_CHANNELS_DIR,
    DEFAULT_IDENTITY_FILE,
    DEFAULT_KEYS_DIR,
)
from lora.core.udp import create_udp_subscriber, parse_host_port
from lora.identity import IdentityConfig, IdentityStore
from lora.tools.meshcore_tx import make_cbor_tx_request

log = logging.getLogger("lora.bridges.meshcore.cli")

#: Default contacts directory (matches the legacy bridge layout).
DEFAULT_CONTACTS_DIR = (
    Path(__file__).resolve().parent.parent.parent.parent.parent
    / "scripts"
    / "apps"
    / "data"
    / "meshcore"
    / "contacts"
)


def _get_git_rev() -> str:
    """Return the short git SHA of the gr4-lora repo, or ``"dev"``."""
    try:
        here = Path(__file__).resolve().parent
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=here,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except OSError, subprocess.TimeoutExpired:
        pass
    return "dev"


def _tcp_send(sock: socket.socket, payload: bytes) -> None:
    """Send a framed companion-protocol response."""
    try:
        sock.sendall(frame_encode(payload))
    except ConnectionError, OSError:
        pass  # client disconnected — cleaned up on next recv


# ---------------------------------------------------------------------------
# Server loop
# ---------------------------------------------------------------------------


def run_bridge(
    tcp_port: int,
    udp_sock: socket.socket,
    sub_msg: bytes,
    udp_addr: tuple[str, int],
    state: BridgeState,
) -> None:
    """Run the bridge: TCP server + UDP subscriber.

    *udp_sock* is an already-subscribed UDP socket.  *sub_msg* is the
    CBOR subscribe message used for periodic keepalive.
    """
    udp_sock.setblocking(False)
    log.info("bridge running (UDP %s:%d)", udp_addr[0], udp_addr[1])

    tcp_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp_srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    tcp_srv.bind(("0.0.0.0", tcp_port))
    tcp_srv.listen(1)
    tcp_srv.setblocking(False)
    log.info("listening on TCP port %d", tcp_port)

    sel = selectors.DefaultSelector()
    sel.register(tcp_srv, selectors.EVENT_READ, data="tcp_accept")
    sel.register(udp_sock, selectors.EVENT_READ, data="udp_rx")

    client_sock: socket.socket | None = None
    client_decoder: FrameDecoder | None = None
    last_keepalive = time.monotonic()
    config_shown = False
    last_status_total = -1

    try:
        while True:
            events = sel.select(timeout=2.0)
            now = time.monotonic()
            if now - last_keepalive >= 5.0:
                udp_sock.sendto(sub_msg, udp_addr)
                last_keepalive = now

            for key, _mask in events:
                if key.data == "tcp_accept":
                    conn, addr = tcp_srv.accept()
                    conn.setblocking(False)
                    log.info("companion connected: %s:%d", addr[0], addr[1])
                    if client_sock is not None:
                        sel.unregister(client_sock)
                        client_sock.close()
                        log.info("previous client disconnected")
                    client_sock = conn
                    client_decoder = FrameDecoder()
                    sel.register(client_sock, selectors.EVENT_READ, data="tcp_rx")
                    state.msg_seq = 0

                elif key.data == "tcp_rx":
                    assert client_sock is not None
                    assert client_decoder is not None
                    try:
                        raw = client_sock.recv(4096)
                    except ConnectionError, OSError:
                        raw = b""
                    if not raw:
                        log.info("companion disconnected")
                        sel.unregister(client_sock)
                        client_sock.close()
                        client_sock = None
                        client_decoder = None
                        continue
                    frames = client_decoder.feed(raw)
                    for cmd_payload in frames:
                        responses = handle_command(
                            cmd_payload, state, udp_sock, udp_addr
                        )
                        for resp in responses:
                            _tcp_send(client_sock, resp)

                elif key.data == "udp_rx":
                    try:
                        dgram, _from = udp_sock.recvfrom(65536)
                    except BlockingIOError, OSError:
                        continue
                    try:
                        msg = cbor2.loads(dgram)
                    except cbor2.CBORDecodeError, ValueError, EOFError:
                        continue
                    if not isinstance(msg, dict):
                        continue

                    msg_type = msg.get("type")
                    if msg_type == "config":
                        phy = msg.get("phy", {})
                        if phy:
                            state.freq_mhz = phy.get("freq", state.freq_mhz * 1e6) / 1e6
                            state.bw_khz = phy.get("bw", state.bw_khz * 1e3) / 1e3
                            state.sf = int(phy.get("sf", state.sf))
                            state.cr = int(phy.get("cr", state.cr + 4)) - 4
                            state.tx_power = int(phy.get("tx_gain", state.tx_power))
                        if not config_shown:
                            log.debug(
                                "config: %.3f MHz SF%d BW %.1fk CR 4/%d",
                                state.freq_mhz,
                                state.sf,
                                state.bw_khz,
                                state.cr + 4,
                            )
                            config_shown = True
                        continue

                    if msg_type == "status":
                        frames_info = msg.get("frames", {})
                        total = frames_info.get("total", 0)
                        if total != last_status_total:
                            ok = frames_info.get("crc_ok", 0)
                            log.debug("status: %d frames (%d CRC_OK)", total, ok)
                            last_status_total = total
                        continue

                    if msg_type != "lora_frame":
                        continue

                    expire_recent_tx(state.recent_tx, state.recent_tx_ttl)
                    rx_payload = msg.get("payload", b"")
                    if rx_payload and is_tx_echo(rx_payload, state.recent_tx):
                        continue
                    if rx_payload and is_rx_duplicate(
                        rx_payload, state.recent_rx, state.recent_rx_ttl
                    ):
                        continue

                    companion_msgs, ack_packets = lora_frame_to_companion_msgs(
                        msg, state
                    )

                    if ack_packets:
                        for ack_pkt in ack_packets:
                            cbor_msg = make_cbor_tx_request(ack_pkt)
                            udp_sock.sendto(cbor_msg, udp_addr)
                            log.info("TX ACK: %s (%dB)", ack_pkt.hex(), len(ack_pkt))

                    if state.client_repeat and rx_payload:
                        fwd_pkt = prepare_repeat_packet(rx_payload, msg, state.pub_key)
                        if fwd_pkt is not None:
                            fwd_cbor = make_cbor_tx_request(fwd_pkt)
                            udp_sock.sendto(fwd_cbor, udp_addr)
                            log.info("repeat: forwarded %dB", len(fwd_pkt))

                    for companion_msg in companion_msgs:
                        if companion_msg[0] == PUSH_ACK:
                            if client_sock is not None:
                                _tcp_send(client_sock, companion_msg)
                        else:
                            state.msg_queue.append(companion_msg)
                    if companion_msgs:
                        queued = [m for m in companion_msgs if m[0] != PUSH_ACK]
                        if queued and client_sock is not None:
                            _tcp_send(client_sock, bytes([PUSH_MESSAGES_WAITING]))
                        seq = msg.get("seq", "?")
                        plen = len(rx_payload)
                        log.info(
                            "RX #%s: %dB (queue %d)",
                            seq,
                            plen,
                            len(state.msg_queue),
                        )

    except KeyboardInterrupt:
        pass
    finally:
        if client_sock is not None:
            client_sock.close()
        tcp_srv.close()
        udp_sock.close()
        sel.close()
        log.info("bridge stopped")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    if setproctitle is not None:
        setproctitle.setproctitle("meshcore-bridge")

    parser = argparse.ArgumentParser(
        prog="lora bridge meshcore",
        description="MeshCore companion-protocol bridge to lora_trx",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to apps/config.toml (default: lora.core.config search path)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"TCP listen port (default: {BRIDGE_PORT})",
    )
    parser.add_argument(
        "--connect",
        metavar="HOST:PORT",
        default=None,
        help="lora_trx UDP address (default: 127.0.0.1:5555)",
    )
    parser.add_argument(
        "--identity",
        type=Path,
        default=None,
        help=f"Identity file (default: {DEFAULT_IDENTITY_FILE})",
    )
    parser.add_argument(
        "--name",
        default=None,
        help="Node name for SELF_INFO",
    )
    parser.add_argument(
        "--keys-dir",
        type=Path,
        default=None,
        help=f"Key store directory (default: {DEFAULT_KEYS_DIR})",
    )
    parser.add_argument(
        "--channels-dir",
        type=Path,
        default=None,
        help=f"Channel store directory (default: {DEFAULT_CHANNELS_DIR})",
    )
    parser.add_argument(
        "--contacts-dir",
        type=Path,
        default=None,
        help=f"Contact store directory (default: {DEFAULT_CONTACTS_DIR})",
    )
    parser.add_argument(
        "--region-scope",
        metavar="NAME",
        default=None,
        help="Region scope for transport codes",
    )
    parser.add_argument(
        "--migrate-json",
        type=Path,
        metavar="FROM",
        default=None,
        help="Migrate a legacy config.json to CBOR EEPROM and exit",
    )
    add_logging_args(parser)
    args = parser.parse_args(argv)

    setup_logging(
        "lora.bridge.meshcore", log_level=args.log_level, no_color=args.no_color
    )

    if args.migrate_json is not None:
        from lora.tools.migrate_meshcore_json import migrate

        migrate(args.migrate_json, delete_source=False)
        return 0

    # Load typed config (best-effort).  Bridge runs even without a TOML
    # so legacy CLI invocations keep working.
    typed = None
    try:
        cfg_dict = load_config(args.config)
        typed = load_typed(cfg_dict)
    except (OSError, TypedConfigError, ValueError) as exc:
        log.warning("typed config not loaded: %s", exc)

    if args.connect:
        try:
            udp_host, udp_port = parse_host_port(args.connect)
        except ValueError as exc:
            parser.error(str(exc))
            return 2
    else:
        udp_host, udp_port = "127.0.0.1", 5555

    log.info("connecting to lora_trx at %s:%d", udp_host, udp_port)
    udp_sock, sub_msg, udp_addr = create_udp_subscriber(
        udp_host,
        udp_port,
        sync_words=[0x12],
        timeout=30.0,
    )
    config_msg = wait_for_config(udp_sock, timeout=30.0)
    raw_cfg = apply_config(config_msg)
    phy = config_msg.get("phy", {})

    freq_mhz = phy.get("freq", 869_618_000.0) / 1e6
    bw_khz = phy.get("bw", 62_500) / 1e3
    sf = int(phy.get("sf", 8))
    cr = int(phy.get("cr", 8)) - 4
    tx_power = int(phy.get("tx_gain", 14))
    log.info(
        "PHY: %.3f MHz SF%d BW %.1fk CR 4/%d TX %d",
        freq_mhz,
        sf,
        bw_khz,
        cr + 4,
        tx_power,
    )

    meshcore_cfg = raw_cfg.get("meshcore", {}) if isinstance(raw_cfg, dict) else {}
    region_scope = (
        args.region_scope
        if args.region_scope is not None
        else str(meshcore_cfg.get("region_scope", ""))
    )
    if region_scope:
        log.info("region scope: #%s", region_scope)

    lat_e6 = int(float(meshcore_cfg.get("lat", 0.0)) * 1e6)
    lon_e6 = int(float(meshcore_cfg.get("lon", 0.0)) * 1e6)

    if args.port is not None:
        tcp_port = args.port
    elif typed is not None:
        tcp_port = typed.bridge_meshcore.listen_port
    else:
        tcp_port = int(meshcore_cfg.get("port", BRIDGE_PORT))

    node_name = (
        args.name
        if args.name is not None
        else str(meshcore_cfg.get("name", "lora_trx"))
    )
    node_model = str(meshcore_cfg.get("model", "lora_trx"))

    identity_file = (
        args.identity
        if args.identity is not None
        else Path(str(meshcore_cfg.get("identity_file", "")) or DEFAULT_IDENTITY_FILE)
    )
    keys_dir = (
        args.keys_dir
        if args.keys_dir is not None
        else Path(str(meshcore_cfg.get("keys_dir", "")) or DEFAULT_KEYS_DIR)
    )
    channels_dir = (
        args.channels_dir
        if args.channels_dir is not None
        else Path(str(meshcore_cfg.get("channels_dir", "")) or DEFAULT_CHANNELS_DIR)
    )
    contacts_dir = (
        args.contacts_dir
        if args.contacts_dir is not None
        else Path(str(meshcore_cfg.get("contacts_dir", "")) or DEFAULT_CONTACTS_DIR)
    )

    eeprom_path = (
        typed.bridge_meshcore.eeprom_path
        if typed is not None
        else Path(str(meshcore_cfg.get("eeprom_path", "")))
        or _default_eeprom(contacts_dir)
    )

    startup_client_repeat = int(meshcore_cfg.get("client_repeat", 0))
    startup_path_hash_mode = int(meshcore_cfg.get("path_hash_mode", 0))
    startup_autoadd_config = int(meshcore_cfg.get("autoadd_config", 0))
    startup_autoadd_max_hops = min(int(meshcore_cfg.get("autoadd_max_hops", 0)), 64)
    flood_scope_raw = str(meshcore_cfg.get("flood_scope", ""))
    startup_send_scope = parse_flood_scope(flood_scope_raw)

    # ---- EEPROM emulation ------------------------------------------
    try:
        eeprom = load_eeprom(eeprom_path)
    except LegacyJsonEepromError as exc:
        log.error("%s", exc)
        return 2

    startup_default_scope_name: str = ""
    startup_default_scope_key: bytes = b"\x00" * 16

    if eeprom:
        log.info("EEPROM config loaded from %s", eeprom_path)
        identity_cfg = eeprom.get("identity", {})
        routing_cfg = eeprom.get("routing", {})
        contacts_cfg = eeprom.get("contacts", {})
        telemetry_cfg = eeprom.get("telemetry", {})
        security_cfg = eeprom.get("security", {})

        if args.name is None:
            node_name = identity_cfg.get("name", node_name)
        lat_e6 = identity_cfg.get("lat_e6", lat_e6)
        lon_e6 = identity_cfg.get("lon_e6", lon_e6)
        region_scope = routing_cfg.get("region_scope", region_scope)
        startup_client_repeat = routing_cfg.get("client_repeat", startup_client_repeat)
        startup_path_hash_mode = routing_cfg.get(
            "path_hash_mode", startup_path_hash_mode
        )
        startup_default_scope_name = str(
            routing_cfg.get("default_scope_name", startup_default_scope_name)
        )
        raw_default_key = routing_cfg.get(
            "default_scope_key", startup_default_scope_key
        )
        if (
            isinstance(raw_default_key, (bytes, bytearray))
            and len(raw_default_key) == 16
        ):
            startup_default_scope_key = bytes(raw_default_key)
        startup_autoadd_config = contacts_cfg.get(
            "autoadd_config", startup_autoadd_config
        )
        startup_autoadd_max_hops = contacts_cfg.get(
            "autoadd_max_hops", startup_autoadd_max_hops
        )
        startup_manual_add = int(contacts_cfg.get("manual_add_contacts", 0))
        startup_telemetry_mode = int(telemetry_cfg.get("telemetry_mode", 0))
        startup_adv_loc_policy = int(telemetry_cfg.get("adv_loc_policy", 0))
        startup_multi_acks = int(telemetry_cfg.get("multi_acks", 0))
        startup_device_pin = int(security_cfg.get("device_pin", 0))
    else:
        startup_manual_add = 0
        startup_telemetry_mode = 0
        startup_adv_loc_policy = 0
        startup_multi_acks = 0
        startup_device_pin = 0
        log.info("EEPROM not found — initializing from settings: %s", eeprom_path)
        toml_settings: dict[str, Any] = {
            "name": node_name,
            "lat": lat_e6 / 1e6,
            "lon": lon_e6 / 1e6,
            "region_scope": region_scope,
            "flood_scope": flood_scope_raw,
            "client_repeat": startup_client_repeat,
            "path_hash_mode": startup_path_hash_mode,
            "autoadd_config": startup_autoadd_config,
            "autoadd_max_hops": startup_autoadd_max_hops,
        }
        initialize_eeprom_from_settings(toml_settings, eeprom_path)

    # ---- IdentityStore -------------------------------------------------
    identity_cfg_obj = IdentityConfig(
        identity_file=identity_file,
        keys_dir=keys_dir,
        channels_dir=channels_dir,
        contacts_dir=contacts_dir,
    )
    identity = IdentityStore(identity_cfg_obj)
    identity._ensure_loaded()
    log.info("identity: %s", identity.our_pub.hex())
    log.info("name: %s", node_name)

    # ---- BridgeState ---------------------------------------------------
    state = BridgeState(
        identity,
        name=node_name,
        freq_mhz=freq_mhz,
        bw_khz=bw_khz,
        sf=sf,
        cr=cr,
        tx_power=tx_power,
        eeprom_path=eeprom_path,
        lat_e6=lat_e6,
        lon_e6=lon_e6,
        region_scope=region_scope,
        client_repeat=startup_client_repeat,
        path_hash_mode=startup_path_hash_mode,
        send_scope=startup_send_scope,
        autoadd_config=startup_autoadd_config,
        autoadd_max_hops=startup_autoadd_max_hops,
        default_scope_name=startup_default_scope_name,
        default_scope_key=startup_default_scope_key,
        model=node_model,
        contacts_dir=contacts_dir,
        git_rev=_get_git_rev(),
    )
    state.manual_add_contacts = startup_manual_add
    state.telemetry_mode = startup_telemetry_mode
    state.adv_loc_policy = startup_adv_loc_policy
    state.multi_acks = startup_multi_acks
    state.device_pin = startup_device_pin

    # ---- Pre-existing contacts ----------------------------------------
    from lora.bridges.meshcore.state import load_persisted_contacts

    persisted = load_persisted_contacts(contacts_dir)
    if persisted:
        state.contacts.update(persisted)
        state.contacts_lastmod = int(time.time())
        log.info("loaded %d persisted contacts", len(persisted))

    log.info(
        "keys: %d, channels: %d, contacts: %d",
        len(state.known_keys),
        len(state.channels),
        len(state.contacts),
    )

    run_bridge(tcp_port, udp_sock, sub_msg, udp_addr, state)
    return 0


def _default_eeprom(contacts_dir: Path) -> Path:
    """Fall back to ``<contacts_dir parent>/config.cbor`` when nothing else set."""
    return contacts_dir.parent / "config.cbor"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
