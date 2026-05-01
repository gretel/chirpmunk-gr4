#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Bridge-mode hardware test (4-phase MeshCore companion round-trip).

Phase 5B port of the bridge mode. Process stack (inner to outer):

    lora_trx:5556 ← lora_agg:5555 ← meshcore_bridge:7834

``lora_agg`` deduplicates TX echoes (FNV-1a64 hash matching, 10 s window)
so Phase D doesn't see the bridge's own Phase C packets. The config
socket talks direct to ``lora_trx:5556`` (``lora_agg`` doesn't forward
``lora_config``).
"""

from __future__ import annotations

import asyncio
import os
import random
import re
import socket
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

import cbor2

from lora.bridges.meshcore.driver import CompanionDriver
from lora.hwtests.harness import (
    AGG_PORT,
    FLUSH_BRIDGE_S,
    FLUSH_TX_S,
    MESHCORE_BRIDGE_PORT,
    SETTLE_S,
    TRX_PORT,
    MeshCoreCompanion,
    assert_all_alive,
    spawn_lora_core,
    spawn_meshcore_bridge,
    spawn_sdr_binary,
    stop_process,
)
from lora.hwtests.matrix import (
    MATRICES,
    ConfigPoint,
    lora_airtime_s,
    point_label,
)
from lora.hwtests.report import (
    err,
    info,
    write_bridge_results,
)


def bridge_payload(point: ConfigPoint) -> str:
    """Build a unique payload string for bridge mode.

    Format: ``sf{sf}bw{bw_khz}f{freq_khz}_{nonce:02x}``
    Example: ``sf8bw62f869618_a7``.
    """
    bw_khz = int(point.bw / 1000)
    freq_khz = int(point.freq_mhz * 1000)
    nonce = random.randint(0, 255)
    return f"sf{point.sf}bw{bw_khz}f{freq_khz}_{nonce:02x}"


def extract_pubkey_from_card(card_output: str) -> str:
    """Pull the 64-char hex pubkey out of meshcore-cli card output.

    Accepts either the URI form
    ``meshcore://contact/add?...&public_key=<64hex>`` or a raw bizcard
    ``meshcore://<hex>`` (ADVERT wire format: header(1) + path_len(1) +
    pubkey(32) + ...; pubkey starts at hex offset 4).
    """
    m = re.search(r"public_key=([0-9a-fA-F]{64})", card_output)
    if m:
        return m.group(1).lower()
    m = re.search(r"meshcore://([0-9a-fA-F]+)", card_output)
    if m:
        hexdata = m.group(1)
        if len(hexdata) >= 68:
            return hexdata[4:68].lower()
    return ""


def collect_bridge(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the bridge-mode summary dict from per-point results."""
    n = len(results)
    advert_rx = sum(1 for r in results if r.get("advert_rx"))
    advert_tx = sum(1 for r in results if r.get("advert_tx"))
    msg_tx = sum(1 for r in results if r.get("msg_tx"))
    msg_tx_acked = sum(1 for r in results if r.get("msg_tx_acked"))
    msg_rx = sum(1 for r in results if r.get("msg_rx"))
    chan_tx = sum(1 for r in results if r.get("chan_tx"))
    return {
        "mode": "bridge",
        "points": n,
        "advert_rx": advert_rx,
        "advert_tx": advert_tx,
        "msg_tx": msg_tx,
        "msg_tx_acked": msg_tx_acked,
        "msg_rx": msg_rx,
        "chan_tx": chan_tx,
        "pass_rate": round(
            (advert_rx + advert_tx + msg_tx + msg_rx) / max(1, 4 * n), 3
        ),
    }


async def _run_bridge_point(  # pragma: no cover  # hw-only path
    point: ConfigPoint,
    companion: MeshCoreCompanion,
    driver: CompanionDriver,
    config_sock: socket.socket,
    sdr_pubkey: str,
    heltec_name: str,
    contact_exchanged: bool,
) -> dict[str, Any]:
    """Run the four-phase bridge test (A: ADVERT RX, B: ADVERT TX,
    C: TXT_MSG TX, D: TXT_MSG RX, F: GRP_TXT TX)."""
    result: dict[str, Any] = {
        "config": asdict(point),
        "advert_rx": None,
        "advert_tx": False,
        "advert_tx_snr": None,
        "msg_tx": False,
        "msg_tx_payload": "",
        "msg_tx_acked": None,
        "msg_rx": False,
        "msg_rx_payload": "",
        "phases_passed": 0,
        "phases_total": 4,
    }

    # Crashed children (e.g. lora_trx libusb fault) must not silently
    # turn into a sea of empty-result phases. Bail loud at every flow
    # boundary instead.
    assert_all_alive()

    # Drain stale spectrum/status so the bridge set_radio ack isn't
    # buried under lora_trx broadcast traffic.
    config_sock.setblocking(False)
    try:
        while True:
            config_sock.recvfrom(65536)
    except BlockingIOError:
        pass
    config_sock.setblocking(True)

    bw_khz = point.bw / 1000.0
    driver.set_radio(point.freq_mhz, bw_khz, point.sf, cr=point.cr)
    if not await companion.set_radio(point.freq_mhz, bw_khz, point.sf, cr=point.cr):
        err(f"  set_radio failed for {point_label(point)}")
        return result
    await asyncio.sleep(SETTLE_S)

    # Phase A: Heltec ADVERT → Bridge RX
    if not contact_exchanged:
        info("  Phase A: Heltec ADVERT → Bridge RX")
        await companion.send_advert()
        tx_air_a = lora_airtime_s(point.sf, point.bw)
        await asyncio.sleep(max(FLUSH_BRIDGE_S, tx_air_a + 2.0))
        assert_all_alive()
        contacts_out = driver.get_contacts()
        heltec_pk = companion.pubkey.lower()
        advert_rx = heltec_pk[:12] in contacts_out.lower() if heltec_pk else False
        result["advert_rx"] = advert_rx
        if advert_rx:
            result["phases_passed"] += 1
            info("  Phase A: PASS (contact seen)")
        else:
            info(f"  Phase A: FAIL (pubkey {heltec_pk[:12]} not in contacts)")
    else:
        result["advert_rx"] = True
        result["phases_passed"] += 1
        info("  Phase A: SKIP (contact already exchanged)")

    await asyncio.sleep(1.0)

    # Phase B: Bridge ADVERT → Heltec RX
    info("  Phase B: Bridge ADVERT → Heltec RX")
    companion.drain_adverts()
    driver.send_advert()
    tx_air = lora_airtime_s(point.sf, point.bw)
    await asyncio.sleep(max(FLUSH_TX_S, tx_air + 1.0))
    # Phase B's TX path is the documented B210 1.5 s libusb risk; check
    # before reading adverts so a crashed lora_trx surfaces here, not as
    # a silent "0 adverts seen" three phases later.
    assert_all_alive()
    adverts = companion.drain_adverts()
    advert_tx = any(
        a.get("adv_key", "").lower().startswith(sdr_pubkey[:12].lower())
        for a in adverts
    )
    result["advert_tx"] = advert_tx
    if advert_tx:
        result["phases_passed"] += 1
        snr = next(
            (
                a["snr"]
                for a in adverts
                if a.get("adv_key", "").lower().startswith(sdr_pubkey[:12].lower())
            ),
            None,
        )
        result["advert_tx_snr"] = snr
        info(f"  Phase B: PASS (SNR={snr})")
    else:
        keys = [a.get("adv_key", "")[:12] for a in adverts]
        info(f"  Phase B: FAIL (saw {len(adverts)} adverts: {keys})")

    await asyncio.sleep(1.0)

    # Phase C: Bridge TXT_MSG → Heltec RX
    info("  Phase C: Bridge TXT_MSG → Heltec RX")
    companion.drain_messages()
    payload_c = bridge_payload(point)
    result["msg_tx_payload"] = payload_c
    msg_ok, msg_output = driver.send_msg(heltec_name, payload_c)
    msg_acked = "acked" in msg_output.lower() if msg_ok else None
    result["msg_tx_acked"] = msg_acked
    tx_air_c = lora_airtime_s(point.sf, point.bw, n_bytes=40)
    await asyncio.sleep(max(FLUSH_TX_S, tx_air_c + 1.0))
    assert_all_alive()
    msgs = companion.drain_messages()
    msg_tx = any(payload_c in m.get("text", "") for m in msgs)
    result["msg_tx"] = msg_tx
    if msg_tx:
        result["phases_passed"] += 1
        ack_str = f" acked={msg_acked}" if msg_acked is not None else ""
        info(f"  Phase C: PASS ('{payload_c}'{ack_str})")
    else:
        texts = [m.get("text", "")[:40] for m in msgs]
        info(f"  Phase C: FAIL (sent '{payload_c}', saw {len(msgs)} msgs: {texts})")

    await asyncio.sleep(1.0)

    # Phase D: Heltec TXT_MSG → Bridge RX
    info("  Phase D: Heltec TXT_MSG → Bridge RX")
    payload_d = bridge_payload(point)
    result["msg_rx_payload"] = payload_d
    sdr_pubkey_bytes = bytes.fromhex(sdr_pubkey)
    await companion.send_message(sdr_pubkey_bytes, payload_d)
    tx_air_d = lora_airtime_s(point.sf, point.bw, n_bytes=40)
    deadline = asyncio.get_event_loop().time() + max(FLUSH_BRIDGE_S, tx_air_d + 3.0)
    all_recv: list[str] = []
    msg_rx = False
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(1.0)
        # Poll loop runs for several seconds; if a child died during the
        # sleep, fail fast rather than burn the whole flush window.
        assert_all_alive()
        for _ in range(10):
            recv_out = driver.recv_msg(timeout=1.0)
            if not recv_out:
                break
            all_recv.append(recv_out)
            if payload_d in recv_out:
                msg_rx = True
        if msg_rx:
            break
    result["msg_rx"] = msg_rx
    if msg_rx:
        result["phases_passed"] += 1
        info(f"  Phase D: PASS ('{payload_d}')")
    else:
        shown = [r[-40:] for r in all_recv[:3]]
        info(f"  Phase D: FAIL (sent '{payload_d}', got {len(all_recv)} msgs: {shown})")

    await asyncio.sleep(1.0)

    # Phase F: Bridge GRP_TXT → Heltec RX (public channel)
    info("  Phase F: Bridge GRP_TXT → Heltec RX (public channel)")
    companion.drain_messages()
    payload_f = bridge_payload(point)
    chan_ok = driver.chan_msg("public", payload_f)
    result["chan_tx"] = False
    result["chan_tx_payload"] = payload_f
    if chan_ok:
        tx_air_f = lora_airtime_s(point.sf, point.bw, n_bytes=40)
        await asyncio.sleep(max(FLUSH_TX_S, tx_air_f + 1.0))
        chan_msgs = companion.drain_messages()
        chan_rx = any(payload_f in m.get("text", "") for m in chan_msgs)
        result["chan_tx"] = chan_rx
        if chan_rx:
            info(f"  Phase F: PASS ('{payload_f}')")
        else:
            texts = [m.get("text", "")[:40] for m in chan_msgs]
            info(
                f"  Phase F: FAIL (sent '{payload_f}', "
                f"saw {len(chan_msgs)} msgs: {texts})"
            )
    else:
        info("  Phase F: FAIL (chan_msg command failed)")

    return result


async def _run_async(  # pragma: no cover  # hw-only path
    *,
    serial_port: str | None,
    tcp_host: str | None,
    tcp_port: int | None,
    matrix_name: str,
    config_file: str,
    binary: str,
    label: str,
    hypothesis: str,
    output_path: str,
    attach: bool,
    host: str,
) -> int:
    matrix = list(MATRICES[matrix_name])

    binary_proc = spawn_sdr_binary(
        binary,
        config_file=config_file,
        udp_port=TRX_PORT,
        log_path="tmp/bridge_trx.log",
        attach=attach,
    )
    agg_proc = spawn_lora_core(
        listen=f"127.0.0.1:{AGG_PORT}",
        log_path="tmp/lora_core.log",
        attach=attach,
    )
    bridge_proc = spawn_meshcore_bridge(
        connect=f"127.0.0.1:{AGG_PORT}",
        port=MESHCORE_BRIDGE_PORT,
        log_path="tmp/meshcore_bridge.log",
        attach=attach,
    )

    companion = MeshCoreCompanion()
    if tcp_host is not None:
        info(f"Connecting to companion via TCP {tcp_host}:{tcp_port}")
        connected = await companion.connect(tcp_host=tcp_host, tcp_port=tcp_port)
    else:
        if serial_port is None:
            err("bridge: --serial or --tcp required")
            stop_process(bridge_proc)
            stop_process(agg_proc)
            stop_process(binary_proc)
            return 2
        info(f"Connecting to companion on {serial_port}")
        connected = await companion.connect(serial_port)
    if not connected:
        err("meshcore_py: cannot connect to companion")
        stop_process(bridge_proc)
        stop_process(agg_proc)
        stop_process(binary_proc)
        return 1
    heltec_pubkey = companion.pubkey
    info(f"Heltec pubkey: {heltec_pubkey[:16]}...")

    driver = CompanionDriver(bridge_host="127.0.0.1", bridge_port=MESHCORE_BRIDGE_PORT)
    card_out = driver.get_card()
    sdr_pubkey = extract_pubkey_from_card(card_out)
    if not sdr_pubkey:
        err(f"cannot extract SDR pubkey from card output: {card_out[:80]}")
        await companion.close()
        stop_process(bridge_proc)
        stop_process(agg_proc)
        stop_process(binary_proc)
        return 1
    info(f"SDR pubkey: {sdr_pubkey[:16]}...")

    heltec_name = heltec_pubkey[:8]

    # Config socket → direct to lora_trx:5556 (lora_agg doesn't forward
    # lora_config). Pre-subscribe + drain so set_radio acks aren't buried
    # under spectrum broadcasts.
    config_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    config_sock.bind(("0.0.0.0", 0))
    config_sock.sendto(cbor2.dumps({"type": "subscribe"}), (host, TRX_PORT))
    await asyncio.sleep(1.0)
    config_sock.settimeout(0.1)
    try:
        while True:
            config_sock.recvfrom(65536)
    except TimeoutError, BlockingIOError:
        pass
    config_sock.settimeout(None)

    info(f"\n--- bridge: {len(matrix)} points, matrix={matrix_name} ---")
    if hypothesis:
        info(f"H: {hypothesis}")
    info("")

    bridge_results: list[dict[str, Any]] = []
    contact_exchanged = False
    try:
        for i, point in enumerate(matrix):
            info(f"[{i + 1}/{len(matrix)}] {point_label(point)}")
            assert_all_alive()
            r = await _run_bridge_point(
                point,
                companion,
                driver,
                config_sock,
                sdr_pubkey,
                heltec_name,
                contact_exchanged,
            )
            bridge_results.append(r)
            info(f"  => {r['phases_passed']}/{r['phases_total']} phases passed")
            if r.get("advert_rx"):
                contact_exchanged = True
    except KeyboardInterrupt:
        info("\nInterrupted")
    finally:
        config_sock.close()
        await companion.set_radio(869.618, 62.5, 8, cr=8)
        await companion.close()
        stop_process(bridge_proc)
        stop_process(agg_proc)
        stop_process(binary_proc)

    summary = collect_bridge(bridge_results)
    write_bridge_results(
        output_path=output_path,
        label=label,
        hypothesis=hypothesis,
        binary=binary,
        config_file=config_file,
        matrix_name=matrix_name,
        bridge_results=bridge_results,
        summary=summary,
    )
    n = summary["points"]
    info(
        f"BRIDGE {n}/{n} points: "
        f"advert_rx={summary['advert_rx']}/{n} "
        f"advert_tx={summary['advert_tx']}/{n} "
        f"msg_tx={summary['msg_tx']}/{n} "
        f"msg_tx_acked={summary['msg_tx_acked']}/{n} "
        f"msg_rx={summary['msg_rx']}/{n}"
    )
    return 0


def run(
    *,
    serial_port: str | None,
    tcp_host: str | None,
    tcp_port: int | None,
    matrix_name: str,
    config_file: str = "apps/config.toml",
    binary: str = "",
    label: str = "",
    hypothesis: str = "",
    output_dir: str = "data/testing",
    attach: bool = False,
    host: str = "127.0.0.1",
) -> int:
    binary_path = binary or "./build/apps/lora_trx"
    label = label or f"bridge_{matrix_name}"
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_path = os.path.join(output_dir, f"lora_test_{label}_{ts}.json")
    return asyncio.run(
        _run_async(
            serial_port=serial_port,
            tcp_host=tcp_host,
            tcp_port=tcp_port,
            matrix_name=matrix_name,
            config_file=config_file,
            binary=binary_path,
            label=label,
            hypothesis=hypothesis,
            output_path=output_path,
            attach=attach,
            host=host,
        )
    )


__all__ = [
    "_run_bridge_point",
    "bridge_payload",
    "collect_bridge",
    "extract_pubkey_from_card",
    "run",
]
