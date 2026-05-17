# chirpmunk-gr4

**chirpmunk-gr4** — a C++23 LoRa PHY transceiver and wideband scanner built on [GNU Radio 4](https://github.com/fair-acc/gnuradio4), paired with a Python toolchain for monitoring, decoding, and telemetry.

> **Status:** research prototype. APIs, configuration, and on-wire formats change without notice. Not production-ready.

## Requirements

- Computer able to compile and run `gnuradio4`
- SDR:
  - UHD (SoapyUHD)
  - ~~PAPR (SoapyPlutoPAPR)~~ (not yet)

## Build & run

```sh
git submodule update --init --recursive
cmake -B build -S . -DGIT_REV=$(git rev-parse --short HEAD)
cmake --build build --target lora_trx lora_scan -j4
```

Binaries land in `build/apps/`. `GIT_REV` bakes into the startup banner; reconfigure on every commit before hardware verification.

Python toolchain registers the `lora` console-script:

```sh
uv sync
```

UDP bus is configured in `apps/config.toml`. Defaults:

| Port | Endpoint    | Direction            |
| ---- | ----------- | -------------------- |
| 5555 | `lora core` | publish to consumers |
| 5556 | `lora_trx`  | producer → core      |
| 5557 | `lora_scan` | producer → core      |

Start order (each in its own shell):

```sh
# data-plane daemon (start before producers connect)
lora core --config apps/config.toml

# transceiver
./build/apps/lora_trx --config apps/config.toml

# live frame viewer (optional)
lora mon
```

`lora daemon --config apps/config.toml` foregrounds the entire stack under the supervisor defined in the `[startup]` block.

## Credits

The DSP pipeline draws on the EPFL TCL reference implementation ([gr-lora_sdr](https://github.com/tapparelj/gr-lora_sdr), GPL-3.0), rewritten as native GR4 blocks. Full attribution in [CREDITS](CREDITS).

## Documentation

Is currently lacking. Work in progress. Please stay tuned!

## License

[ISC](LICENSE) — SPDX identifier `ISC`.
Copyright © 2025–2026 Tom Hensel &lt;code@jitter.eu&gt;.

Third-party attribution and academic references: [CREDITS](CREDITS).
