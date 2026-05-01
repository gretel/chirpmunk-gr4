# gr4-lora — Python tools

The C++ side of gr4-lora (`apps/lora_trx`, `apps/lora_scan`) is the SDR
front-end. Everything in `scripts/` is the **Python** half: a single
`lora` CLI that owns the data plane (the `lora-core` daemon),
visualisation, decoders, hardware tests, and the MeshCore
companion bridge.

Calibration / RF measurement tools live in a separate repo
(`gretel/gr4-rf-tools`, `rftools` CLI).

This README is the install-and-run guide for end users. The full
specification lives in `docs/superpowers/specs/lora-core.md` (operator
manual / wire protocol), and `CBOR-SCHEMA.md` documents every
inter-process message.

## Install

The build is `pyproject.toml`-driven and compatible with [`uv`](https://docs.astral.sh/uv/).

### Development checkout

```bash
uv sync          # creates .venv, installs gr4-lora + dev deps
uv run lora --help
```

`uv sync` reads `pyproject.toml` + `uv.lock` and materialises
`./.venv`. All the tools below are then reachable as
`uv run lora <cmd>`. The `dev` extras (pytest, pyright, ruff,
hypothesis) come along automatically.

### Stable / per-user install

```bash
uv tool install --from . gr4-lora
lora --help
```

`uv tool install` puts `lora` on `PATH` in an isolated venv under
`~/.local/share/uv/tools/`. Re-run with `--reinstall` after pulling
new code.

### Editable install (alternative)

```bash
pip install -e '.[dev]'
```

## Run

A typical operator session looks like:

```bash
# 1) C++ SDR front-end (publishes raw `lora_frame` on UDP :5556).
./build/apps/lora_trx --config apps/config.toml &

# 2) Python data plane: aggregates, annotates, persists, fans out.
lora core &                       # listens on UDP :5555 by default

# 3) Live frame viewer (subscribes to :5555).
lora mon &

# 4) Optional: spectrum / waterfall.
lora waterfall &
lora spectrum &                   # requires `lora_scan` running on :5557

# 5) MeshCore companion bridge — for phone-app pairing.
lora bridge meshcore --connect 127.0.0.1:5555 --port 7834 &
```

Stop the daemon cleanly with `kill -TERM` (or `Ctrl-C` if attached).
The lifecycle module flushes DuckDB and closes sockets gracefully.

## Subcommand reference

| Subcommand | Purpose |
|------------|---------|
| `lora core` | data-plane daemon: aggregates `lora_frame` from one or more `lora_trx` upstreams, annotates protocol payloads, persists to DuckDB, fans out CBOR over UDP, proxies `lora_tx` requests. |
| `lora mon` | live frame viewer subscribing to `lora-core` (`:5555`) — pretty-prints decoded frames + diversity / protocol annotations. |
| `lora waterfall` | RX waterfall for the narrowband decoder (`lora_trx`). |
| `lora spectrum` | scan-spectrum viewer (`lora_scan` on `:5557`). |
| `lora wav` | IQ recorder / replay tool. |
| `lora decode {meshcore,lorawan,meshtastic,raw}` | offline replay decoder; reads a captured CBOR sequence and dumps decoded frames as JSON. |
| `lora tx {advert,send,anon-req}` | one-shot MeshCore TX builder; emits a CBOR `lora_tx` request to a `lora_trx` instance. |
| `lora bridge meshcore` | MeshCore companion-protocol bridge (TCP server on `:7834`) — translates phone-app `CMD_*` ↔ wire packets via `lora-core`. |
| `lora bridge meshcore migrate-json --from <path>` | one-shot migration of a legacy `meshcore_bridge.py` `config.json` into the typed CBOR EEPROM file. |
| `lora bridge serial` | serial → TCP adapter for pre-MeshCore radios. |
| `lora hwtest` | hardware A/B test harness (`decode`, `scan`, `tx`, `bridge` modes — formerly `lora_test.py`). |

Every subcommand accepts `--help`; e.g. `lora core --help` shows the
`--config`, `--log-level`, `--listen` flags. `lora hwtest --help` lists
the matrices, attach modes, etc.

## Migration notes (upgrading from the pre-Phase-7 branch)

If you are upgrading from a branch that still had the legacy
`apps`, `lib`, and `tests` directories under `scripts/` on disk:

* **Aggregator config keys removed.** The old `[aggregator]` and
  `[meshcore]` TOML sections are gone. `lora-core` reads `[core]` (with
  nested `upstream`, `aggregator`, `decoders`, `identity`, `storage`).
  Loading a stale config fails fast with a hint pointing at the
  offending key. See `apps/config.toml` for the canonical layout.
* **Default listen port is `:5555`.** The Phase 2 parallel-run shim
  that rewrote `:5555` → `:5558` is gone. If you have a long-running
  `lora_agg` instance still bound to `:5555`, stop it first.
* **MeshCore EEPROM migration is opt-in.** Legacy
  `meshcore_bridge.py config.json` is no longer auto-converted on
  startup. Run
  `lora bridge meshcore migrate-json --from <path>` once to write the
  typed CBOR EEPROM, then start the bridge normally.
* **DuckDB file path changed.** The legacy `data/lora_frames.duckdb`
  is **not** migrated. The new daemon writes to `data/lora.duckdb`.
  The old file is still readable with `duckdb data/lora_frames.duckdb`
  if you need to query historical data.
* **The legacy `apps` and `lib` Python modules are gone.** Every tool
  has a canonical home under `lora.*`; invoke via `lora <subcmd>`. The
  legacy top-level imports (`import meshcore_tx`, `import lora_mon`)
  no longer resolve. Use `from lora.tools.meshcore_tx import ...` /
  `from lora.viz.mon import ...`.

## Hardware-side notes

The C++ front-end (`apps/lora_trx` / `apps/lora_scan`) reads
`apps/config.toml` for SDR settings and ignores the Python-only
sections. Build instructions live at the repo root (CMake) — this
README covers the Python tools only. The Heltec V3 (or any LoRa
companion) is wired through `lora bridge meshcore` over USB serial
(`/dev/cu.usbserial-*` on macOS, `/dev/ttyUSB*` on Linux).
