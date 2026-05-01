#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Hardware-test config matrices and pure helpers.

Sole source of truth for ``MATRICES``, :class:`ConfigPoint`, the LoRa
airtime model, and :func:`point_label`. No I/O, no logging, no
hardware. Imported by every ``lora.hwtests.*`` test runner and by the
``lora hwtest`` CLI. Carved out of the legacy
``lora_test.py`` in Phase 5.

All matrices lock to :data:`FREQ_MHZ` (869.618 MHz, MeshCore EU
primary). The SDR does NOT retune per-point; only the Heltec TX does.
Matrices that vary frequency are broken by design — don't add them.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Center frequency for every matrix point and for the SDR receiver.
FREQ_MHZ: float = 869.618


@dataclass
class ConfigPoint:
    """One point in an experiment matrix. Each gets exactly one TX.

    Defaults match MeshCore EU primary: 869.618 MHz / BW 62.5 kHz.
    Always supply ``sf`` explicitly; the rest are sensible no-thought
    fallbacks for ad-hoc experiments.
    """

    sf: int
    bw: int = 62500  # Hz — MeshCore EU default
    freq_mhz: float = FREQ_MHZ  # MeshCore EU primary
    tx_power: int = 2  # dBm
    cr: int = 8  # coding rate denominator (5..8 → 4/5..4/8)


def lora_airtime_s(
    sf: int,
    bw: int,
    n_bytes: int = 126,
    cr: int = 4,
    preamble: int = 8,
    explicit_header: bool = True,
) -> float:
    """Estimate LoRa packet airtime in seconds (Semtech AN1200.13).

    Default ``n_bytes`` = 126 (typical MeshCore ADVERT with name +
    location). The ``cr`` parameter here is the *coding-rate index*
    (4 → 4/8, equivalent to ``ConfigPoint.cr - 4``); kept at the
    legacy default to preserve byte-exact parity with the old harness
    estimates.
    """
    t_sym = (1 << sf) / bw
    n_preamble = (preamble + 4.25) * t_sym
    de = 1 if (sf >= 11 and bw == 125000) or (sf == 12 and bw <= 125000) else 0
    ih = 0 if explicit_header else 1
    payload_bits = 8 * n_bytes - 4 * sf + 28 + 16 - 20 * ih
    n_payload = 8 + max(
        0, ((payload_bits + 4 * (sf - 2 * de) - 1) // (4 * (sf - 2 * de)))
    ) * (cr + 4)
    return n_preamble + n_payload * t_sym


def point_label(p: ConfigPoint) -> str:
    """Human-readable label for a config point.

    Format: ``SF{sf}/BW{bw_k:g}k@{freq_mhz:.3f} {tx_power}dBm[ CR{cr}]``.
    The ``CR`` suffix is only included when ``cr != 8`` (the default).
    """
    bw_k = p.bw / 1000
    pwr = f" {p.tx_power}dBm"
    cr = f" CR{p.cr}" if p.cr != 8 else ""
    return f"SF{p.sf}/BW{bw_k:g}k@{p.freq_mhz:.3f}{pwr}{cr}"


#: Predefined experiment matrices keyed by ``--matrix`` CLI argument.
#:
#: All points lock to :data:`FREQ_MHZ`. Sweeps cover SF/BW/CR/tx_power
#: while leaving the SDR center frequency untouched.
MATRICES: dict[str, list[ConfigPoint]] = {
    "basic": [
        ConfigPoint(sf=7, bw=62500, freq_mhz=FREQ_MHZ),
        ConfigPoint(sf=8, bw=62500, freq_mhz=FREQ_MHZ),
        ConfigPoint(sf=12, bw=62500, freq_mhz=FREQ_MHZ),
    ],
    "sf_sweep": [
        ConfigPoint(sf=sf, bw=62500, freq_mhz=FREQ_MHZ) for sf in (7, 8, 9, 10, 11, 12)
    ],
    "full": [
        ConfigPoint(sf=7, bw=62500, freq_mhz=FREQ_MHZ),
        ConfigPoint(sf=8, bw=62500, freq_mhz=FREQ_MHZ),
        ConfigPoint(sf=9, bw=62500, freq_mhz=FREQ_MHZ),
        ConfigPoint(sf=10, bw=62500, freq_mhz=FREQ_MHZ),
        ConfigPoint(sf=12, bw=62500, freq_mhz=FREQ_MHZ),
        ConfigPoint(sf=7, bw=125000, freq_mhz=FREQ_MHZ),
        ConfigPoint(sf=8, bw=125000, freq_mhz=FREQ_MHZ),
        ConfigPoint(sf=8, bw=250000, freq_mhz=FREQ_MHZ),
    ],
    # Bridge-optimised: mixed BW to keep airtime under B210 TX limit (~1.5 s).
    "bridge_full": [
        ConfigPoint(sf=7, bw=62500, freq_mhz=FREQ_MHZ),  # 652 ms
        ConfigPoint(sf=8, bw=62500, freq_mhz=FREQ_MHZ),  # 1140 ms
        ConfigPoint(sf=9, bw=125000, freq_mhz=FREQ_MHZ),  # 1042 ms
        ConfigPoint(sf=10, bw=250000, freq_mhz=FREQ_MHZ),  # 1042 ms
    ],
    # Official MeshCore regional presets (SF/BW/CR vary, freq locked).
    "meshcore_presets": [
        ConfigPoint(sf=8, bw=62500, freq_mhz=FREQ_MHZ, cr=8, tx_power=10),
        ConfigPoint(sf=11, bw=250000, freq_mhz=FREQ_MHZ, cr=5, tx_power=10),
        ConfigPoint(sf=10, bw=250000, freq_mhz=FREQ_MHZ, cr=5, tx_power=10),
        ConfigPoint(sf=7, bw=62500, freq_mhz=FREQ_MHZ, cr=5, tx_power=10),
    ],
    # Comprehensive baseline: SF7-12 × BW62.5k/125k/250k. tx_power=10.
    "baseline": [
        # BW62.5k: all SFs
        ConfigPoint(sf=7, bw=62500, freq_mhz=FREQ_MHZ, tx_power=10),
        ConfigPoint(sf=8, bw=62500, freq_mhz=FREQ_MHZ, tx_power=10),
        ConfigPoint(sf=9, bw=62500, freq_mhz=FREQ_MHZ, tx_power=10),
        ConfigPoint(sf=10, bw=62500, freq_mhz=FREQ_MHZ, tx_power=10),
        ConfigPoint(sf=11, bw=62500, freq_mhz=FREQ_MHZ, tx_power=10),
        ConfigPoint(sf=12, bw=62500, freq_mhz=FREQ_MHZ, tx_power=10),
        # BW125k: SF7-10
        ConfigPoint(sf=7, bw=125000, freq_mhz=FREQ_MHZ, tx_power=10),
        ConfigPoint(sf=8, bw=125000, freq_mhz=FREQ_MHZ, tx_power=10),
        ConfigPoint(sf=9, bw=125000, freq_mhz=FREQ_MHZ, tx_power=10),
        ConfigPoint(sf=10, bw=125000, freq_mhz=FREQ_MHZ, tx_power=10),
        # BW250k: SF7-9
        ConfigPoint(sf=7, bw=250000, freq_mhz=FREQ_MHZ, tx_power=10),
        ConfigPoint(sf=8, bw=250000, freq_mhz=FREQ_MHZ, tx_power=10),
        ConfigPoint(sf=9, bw=250000, freq_mhz=FREQ_MHZ, tx_power=10),
    ],
}


__all__ = [
    "FREQ_MHZ",
    "MATRICES",
    "ConfigPoint",
    "lora_airtime_s",
    "point_label",
]
