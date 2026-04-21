# LOST

**LoRa Open Source Transceiver (LOST)** — a C++23 LoRa PHY transceiver and wideband scanner built on [GNU Radio 4](https://github.com/fair-acc/gnuradio4), paired with a Python toolchain for monitoring, decoding, and telemetry.

> **Status:** research prototype. APIs, configuration, and on-wire formats change without notice. Not production-ready.

## Requirements

- Computer able to compile and run `gnuradio4`
- SDR:
	- UHD (SoapyUHD module)
	- PAPR (SoapyPlutoPAPR)

## Credits

The DSP pipeline draws on the EPFL TCL reference implementation ([gr-lora_sdr](https://github.com/tapparelj/gr-lora_sdr), GPL-3.0), rewritten as native GR4 blocks. Full attribution in [CREDITS](CREDITS).

## Documentation

Is currently lacking. Work in progress. Please stay tuned!

## License

[ISC](LICENSE) — SPDX identifier `ISC`.
Copyright © 2025–2026 Tom Hensel &lt;code@jitter.eu&gt;.

Third-party attribution and academic references: [CREDITS](CREDITS).
