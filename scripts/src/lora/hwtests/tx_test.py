#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""TX-mode hardware test (SDR TX → Heltec RX, ADVERT + TXT_MSG).

Phase 5B port. Unlike the legacy harness — which shelled out to
``meshcore_tx.py`` over five subprocess hops per point — this runner
imports the :mod:`lora.tools.meshcore_tx` library directly. One process,
no fork-per-packet.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import asdict
from datetime import datetime, timezone

from lora.hwtests.harness import (
    FLUSH_TX_S,
    SETTLE_S,
    TRX_PORT,
    MeshCoreCompanion,
    assert_all_alive,
    spawn_sdr_binary,
    stop_process,
)
from lora.hwtests.matrix import (
    MATRICES,
    ConfigPoint,
    point_label,
)
from lora.hwtests.report import (
    PointResult,
    err,
    info,
    write_results,
)
from lora.tools.meshcore_tx import (
    _load_identity,
    build_advert,
    build_txt_msg,
    send_via_udp,
)


def get_sdr_pubkey() -> str:
    """Return the local SDR identity's 64-char hex public key.

    Loads (or creates) the default identity file via
    :func:`lora.tools.meshcore_tx._load_identity`. Replaces the legacy
    ``meshcore_tx.py --show-key`` subprocess hop.
    """
    _expanded, pub_key, _seed = _load_identity(None)
    return pub_key.hex()


def sdr_tx_advert(
    *,
    host: str,
    port: int,
    freq_mhz: float,
    sf: int,
    bw: int,
) -> bool:
    """Build an ADVERT and send it via lora_trx UDP. Returns True on send."""
    try:
        packet = build_advert(name="lora_test")
        send_via_udp(
            packet,
            host=host,
            port=port,
            freq=int(freq_mhz * 1e6),
            sf=sf,
            bw=bw,
        )
        return True
    except Exception as e:
        info(f"  sdr_tx_advert failed: {e}")
        return False


def sdr_tx_msg(
    *,
    host: str,
    port: int,
    dest_pubkey: str,
    text: str,
    freq_mhz: float,
    sf: int,
    bw: int,
) -> bool:
    """Build an encrypted TXT_MSG and send it via lora_trx UDP."""
    try:
        recipient = bytes.fromhex(dest_pubkey)
        packet = build_txt_msg(text, recipient)
        send_via_udp(
            packet,
            host=host,
            port=port,
            freq=int(freq_mhz * 1e6),
            sf=sf,
            bw=bw,
        )
        return True
    except Exception as e:
        info(f"  sdr_tx_msg failed: {e}")
        return False


async def _run_tx_point(  # pragma: no cover  # hw-only path
    point: ConfigPoint,
    companion: MeshCoreCompanion,
    sdr_pubkey: str,
    *,
    host: str = "127.0.0.1",
    port: int = TRX_PORT,
) -> PointResult:
    """Run one TX point: SDR sends ADVERT + TXT_MSG, verify Heltec RX."""
    result = PointResult(config=asdict(point))

    # Bail loud if a tracked child (lora_trx) died between points.
    assert_all_alive()

    bw_khz = point.bw / 1000.0
    if not await companion.set_radio(point.freq_mhz, bw_khz, point.sf, cr=point.cr):
        err(f"set_radio failed for {point_label(point)}")
        return result

    await asyncio.sleep(SETTLE_S)
    companion.drain_adverts()

    # ---- ADVERT test ----
    ok = sdr_tx_advert(
        host=host, port=port, freq_mhz=point.freq_mhz, sf=point.sf, bw=point.bw
    )
    result.tx_ok = ok
    info(f"  ADVERT TX {'ok' if ok else 'FAIL'}")
    if not ok:
        return result
    await asyncio.sleep(FLUSH_TX_S)
    assert_all_alive()

    adverts = companion.drain_adverts()
    advert_ok = any(a.get("adv_key", "").lower() == sdr_pubkey.lower() for a in adverts)
    result.frames.append(
        {
            "test": "advert_rx",
            "passed": advert_ok,
            "adverts_seen": len(adverts),
            "snr": next(
                (
                    a["snr"]
                    for a in adverts
                    if a.get("adv_key", "").lower() == sdr_pubkey.lower()
                ),
                None,
            ),
        }
    )
    if advert_ok:
        result.crc_ok += 1
        snr = result.frames[-1]["snr"]
        info(f"  ADVERT RX: OK (SNR={snr})")
    else:
        result.crc_fail += 1
        keys = [a.get("adv_key", "")[:12] for a in adverts]
        info(f"  ADVERT RX: NOT received (saw {len(adverts)} adverts: {keys})")

    # ---- TXT_MSG test ----
    heltec_pubkey = companion.pubkey
    if heltec_pubkey:
        companion.drain_messages()
        ts = int(time.time())
        msg_text = f"test_{ts}"
        ok = sdr_tx_msg(
            host=host,
            port=port,
            dest_pubkey=heltec_pubkey,
            text=msg_text,
            freq_mhz=point.freq_mhz,
            sf=point.sf,
            bw=point.bw,
        )
        info(f"  TXT_MSG TX {'ok' if ok else 'FAIL'}")
        if ok:
            await asyncio.sleep(FLUSH_TX_S)
            assert_all_alive()
            msgs = companion.drain_messages()
            msg_ok = any(msg_text in m.get("text", "") for m in msgs)
            result.frames.append(
                {
                    "test": "txt_msg_rx",
                    "passed": msg_ok,
                    "sent": msg_text,
                    "msgs_seen": len(msgs),
                }
            )
            if msg_ok:
                result.crc_ok += 1
                info(f"  TXT_MSG RX: OK '{msg_text}'")
            else:
                result.crc_fail += 1
                texts = [m.get("text", "")[:40] for m in msgs]
                info(f"  TXT_MSG RX: NOT received (saw {len(msgs)} msgs: {texts})")
        else:
            result.crc_fail += 1
    else:
        info("  TXT_MSG: skipped (no Heltec pubkey)")

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
        log_path="tmp/tx.log",
        attach=attach,
    )

    companion = MeshCoreCompanion()
    if tcp_host is not None:
        info(f"Connecting to companion via TCP {tcp_host}:{tcp_port}")
        connected = await companion.connect(tcp_host=tcp_host, tcp_port=tcp_port)
    else:
        if serial_port is None:
            err("tx: --serial or --tcp required")
            stop_process(binary_proc)
            return 2
        info(f"Connecting to companion on {serial_port}")
        connected = await companion.connect(serial_port)
    if not connected:
        err("meshcore_py: cannot connect to companion")
        stop_process(binary_proc)
        return 1
    info(f"companion: {companion.pubkey[:16]}...")

    sdr_pubkey = get_sdr_pubkey()
    if not sdr_pubkey:
        err("cannot get SDR pubkey")
        await companion.close()
        stop_process(binary_proc)
        return 1
    info(f"SDR pubkey: {sdr_pubkey[:16]}...")

    info(f"\n--- tx: {len(matrix)} points, matrix={matrix_name} ---")
    if hypothesis:
        info(f"H: {hypothesis}")
    info("")

    results: list[PointResult] = []
    try:
        for i, point in enumerate(matrix):
            info(f"[{i + 1}/{len(matrix)}] {point_label(point)}")
            result = await _run_tx_point(
                point, companion, sdr_pubkey, host=host, port=TRX_PORT
            )
            results.append(result)
            info(f"  => {result.crc_ok} pass, {result.crc_fail} fail")
    except KeyboardInterrupt:
        info("\nInterrupted")
    finally:
        await companion.set_radio(869.618, 62.5, 8, cr=8)
        await companion.close()
        stop_process(binary_proc)

    write_results(
        output_path=output_path,
        label=label,
        hypothesis=hypothesis,
        mode="tx",
        binary=binary,
        config_file=config_file,
        matrix_name=matrix_name,
        results=results,
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
    label = label or f"tx_{matrix_name}"
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
    "_run_tx_point",
    "get_sdr_pubkey",
    "run",
    "sdr_tx_advert",
    "sdr_tx_msg",
]
