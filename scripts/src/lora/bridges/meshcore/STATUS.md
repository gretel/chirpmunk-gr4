# meshcore_bridge.py — implementation status

Last updated: 2026-03-30

## companion protocol commands

| cmd | hex | status | manual test |
|-----|-----|--------|-------------|
| APP_START | 0x01 | done | connect meshcore-cli, verify prompt shows name + radio params |
| SEND_TXT_MSG | 0x02 | done | `msg <name> hello` — verify "acked" within 30s |
| SEND_CHAN_TXT_MSG | 0x03 | done | `chan 0 test` — verify on second node or observer |
| GET_CONTACTS | 0x04 | done | `contacts` — verify list matches known peers |
| GET_DEVICE_TIME | 0x05 | done | implicit (mccli queries on connect) |
| SET_DEVICE_TIME | 0x06 | stub (ignored) | n/a — bridge uses system clock |
| SEND_SELF_ADVERT | 0x07 | done | `advert` — verify peer receives it |
| SET_ADVERT_NAME | 0x08 | done + eeprom | `set name Foo` → restart bridge → verify name persists |
| ADD_UPDATE_CONTACT | 0x09 | done | `import_contact meshcore://...` then `contacts` |
| SYNC_NEXT_MESSAGE | 0x0A | done | send msg from peer → verify it appears in mccli |
| SET_RADIO_PARAMS | 0x0B | partial | `set radio 869.618,62.5,8,8` — params stored in-memory only; **no live retune** |
| SET_RADIO_TX_POWER | 0x0C | done (in-memory) | `set txpower 10` — not eeprom-persisted |
| RESET_PATH | 0x0D | done | `resetpath` → `contacts` should show all paths reset |
| SET_ADVERT_LATLON | 0x0E | done + eeprom | set via companion → restart → verify lat/lon persists |
| REMOVE_CONTACT | 0x0F | done (6B prefix) | remove contact in companion app → verify gone from `contacts` |
| SHARE_CONTACT | 0x10 | done | `share <name>` — verify URI printed |
| EXPORT_CONTACT | 0x11 | done | `export <name>` — verify `meshcore://` URI |
| IMPORT_CONTACT | 0x12 | done | `import_contact meshcore://...` → `contacts` |
| REBOOT | 0x13 | simulated | `reboot` — verify startup config restored (name, scope) |
| GET_BATT_AND_STORAGE | 0x14 | stub (fake) | `battery` — shows 4200 mV / 0 KB used |
| SET_TUNING_PARAMS | 0x15 | stub | n/a |
| DEVICE_QUERY | 0x16 | done | `infos` — verify model, fw_ver=9, git rev |
| EXPORT_PRIVATE_KEY | 0x17 | done | via companion — **no PIN check** |
| IMPORT_PRIVATE_KEY | 0x18 | partial | imports to memory — **not saved to identity.bin** |
| LOGIN | 0x1A | done (CLI proxy) | `login <node> <pwd>` — sends TXT_TYPE_CLI |
| STATUS_REQ | 0x1B | done (CLI proxy) | `status <node>` |
| HAS_CONNECTION | 0x1C | stub (NOT_FOUND) | n/a |
| LOGOUT | 0x1D | done (CLI proxy) | `logout <node>` |
| GET_CONTACT_BY_KEY | 0x1E | done | implicit (companion app lookups) |
| GET_CHANNEL | 0x1F | done | `channels` — verify all 8 slots |
| SET_CHANNEL | 0x20 | done | set channel in companion app → `channels` |
| TRACE | 0x24 | done | `trace <node>` |
| SET_DEVICE_PIN | 0x25 | done + eeprom | set via companion → restart → verify persists |
| SET_OTHER_PARAMS | 0x26 | done + eeprom | set manual_add / telemetry / adv_loc via companion |
| SEND_TELEMETRY_REQ | 0x27 | done (CLI proxy) | `telemetry <node>` |
| GET_CUSTOM_VARS | 0x28 | stub (empty) | n/a — no hardware sensors |
| SET_CUSTOM_VAR | 0x29 | stub (error) | n/a |
| GET_ADVERT_PATH | 0x2A | done | implicit (companion path display) |
| GET_TUNING_PARAMS | 0x2B | stub (defaults) | n/a |
| BINARY_REQ | 0x32 | done | via companion binary request |
| FACTORY_RESET | 0x33 | done | `factory_reset` — **destructive**, verify contacts/keys wiped |
| PATH_DISCOVERY | 0x34 | partial | `nd` — sends CLI "status", not CTRL discover_req |
| SET_FLOOD_SCOPE | 0x36 | done + guard | `/scope de-hh` — verify scope active; all-zero from mccli ignored when config scope set |
| SEND_CONTROL_DATA | 0x37 | done | `node_discover` — verify CTRL flood TX with transport codes |
| GET_STATS | 0x38 | done (mixed) | `stats` — real packet counters; fake battery/uptime/air_secs |
| SEND_ANON_REQ | 0x39 | done | via companion anonymous request |
| SET_AUTOADD_CONFIG | 0x3A | done + eeprom | set via companion → restart → verify persists |
| GET_AUTOADD_CONFIG | 0x3B | done | implicit |
| GET_ALLOWED_REPEAT_FREQ | 0x3C | done | implicit (companion queries) |
| SET_PATH_HASH_MODE | 0x3D | done + eeprom | set via companion → restart → verify persists |

## RX packet handling

| payload type | hex | handled | companion push | RF response | manual test |
|-------------|-----|---------|----------------|-------------|-------------|
| ADVERT | 0x04 | done | PUSH_NEW_ADVERT (147B record) | — | peer sends advert → verify contact appears |
| TXT_MSG | 0x02 | done | CONTACT_MSG_RECV_V3 (SNR) | bare ACK (direct) | peer sends msg → verify text + "acked" on peer |
| GRP_TXT | 0x05 | done | CHANNEL_MSG_RECV_V3 (SNR) | — | peer sends channel msg → verify in mccli |
| ACK | 0x03 | done | PUSH_ACK if pending match | — | send msg → verify "acked" |
| PATH | 0x08 | done | PUSH_PATH_UPDATE + bundled ACK | — | send flood msg → verify path learned + ACK |
| ANON_REQ | 0x07 | done | CONTACT_MSG_RECV_V3 | encrypted RESP (direct) | peer sends anon_req → verify login response |
| RESP | 0x01 | done | LOGIN_SUCCESS/FAIL or STATUS_RESPONSE | — | after login → verify push |
| CTRL | 0x0B | done | PUSH_CONTROL_DATA | — | peer sends node_discover → verify push |
| TRACE | 0x09 | **not handled** | — | — | — |
| MULTI | 0x0A | **not handled** | — | — | — |
| GRP_DATA | 0x06 | **not handled** | — | — | — |
| REQ | 0x00 | **not handled** | — | — | — |
| RAW_CUSTOM | 0x0F | **not handled** | — | — | — |

## TX packet building

| payload type | hex | can TX | routing | manual test |
|-------------|-----|--------|---------|-------------|
| ADVERT | 0x04 | done | FLOOD / T_FLOOD (scope) | `advert` → peer sees it |
| TXT_MSG | 0x02 | done | FLOOD when out_path=-1, DIRECT when path known | `msg <name> hello` |
| GRP_TXT | 0x05 | done | FLOOD / T_FLOOD (scope) | `chan 0 test` |
| ACK | 0x03 | done | DIRECT (no transport codes) | automatic on direct TXT_MSG RX |
| PATH+ACK | 0x08 | done | FLOOD / T_FLOOD (scope) | automatic on flood TXT_MSG RX — return path + bundled ACK |
| RESP | 0x01 | done | DIRECT (no transport codes) | automatic on ANON_REQ RX |
| CTRL | 0x0B | done | FLOOD / T_FLOOD (scope) | `node_discover` |
| TRACE | 0x09 | done | DIRECT | `trace <node>` |
| ANON_REQ | 0x07 | done | DIRECT | via companion |
| PATH | 0x08 | done | FLOOD / T_FLOOD (scope) | auto on flood TXT_MSG RX — carries return path + bundled ACK |
| GRP_DATA | 0x06 | **no** | — | — |
| MULTI | 0x0A | **no** | — | — |

## EEPROM persistence (config.json)

| field | written on change | restored on boot | startup mirror | manual test |
|-------|-------------------|-------------------|----------------|-------------|
| name | yes | yes | _startup_name | `set name X` → restart → `infos` |
| lat_e6 | yes | yes | _startup_lat_e6 | set latlon → restart → `infos` |
| lon_e6 | yes | yes | _startup_lon_e6 | set latlon → restart → `infos` |
| region_scope | yes | yes | — (derived) | config.toml only |
| client_repeat | yes | yes | _startup_client_repeat | `set radio ...,on` → restart → `infos` |
| path_hash_mode | yes | yes | _startup_path_hash_mode | set mode → restart → `infos` |
| autoadd_config | yes | yes | _startup_autoadd_config | set → restart → `get autoadd` |
| autoadd_max_hops | yes | yes | _startup_autoadd_max_hops | set → restart → `get autoadd` |
| manual_add_contacts | yes | yes | — | set other_params → restart → verify |
| telemetry_mode | yes | yes | — | set other_params → restart → verify |
| adv_loc_policy | yes | yes | — | set other_params → restart → verify |
| multi_acks | yes | yes | — | set other_params → restart → verify |
| device_pin | yes | yes | — | set pin → restart → verify |
| send_scope | **no** (per-session) | no | _startup_send_scope (from config.toml) | `/scope de-hh` → disconnect → reconnect → verify config scope restored |
| freq/bw/sf/cr | **no** | no | — | lora_trx config.toml is authoritative |
| tx_power | **no** | no | — | in-memory only |

## repeater mode (client_repeat)

| check | implemented | firmware equivalent | manual test |
|-------|-------------|---------------------|-------------|
| flood-only forwarding | done | `isRouteFlood()` | enable repeat → send direct msg from peer → verify NOT forwarded |
| CRC validation | done | hardware CRC | — (hard to test manually) |
| payload type filter (PR #1810) | done | denies GRP_TXT/GRP_DATA/ADVERT | enable repeat → peer sends advert → verify NOT forwarded; peer sends msg → verify forwarded |
| path room (MAX_PATH_SIZE=64) | done | `(n+1)*hashSize <= 64` | need 64-hop packet (impractical manually) |
| loop detection | done | `isLooped()` | need looping topology (2+ repeaters) |
| path hash append | done | `copyHashTo()` + `setPathHashCount(n+1)` | enable repeat → forward a flood → check path on observer |
| echo filter | done | `MeshTables::hasSeen()` | verify bridge doesn't re-forward its own TX |
| RX dedup | done | implicit | dual-channel RX produces no duplicates |
| DoNotRetransmit flag | **no** | `isMarkedDoNotRetransmit()` | — |
| retransmit delay | **no** | `getRetransmitDelay()` | — |
| airtime budget / LBT | **no** | duty-cycle limits | — |
| deny_flood_broadcast config knob | **no** (hardcoded true) | region-based | TODO |

## known limitations

| # | area | issue | severity | workaround |
|---|------|-------|----------|------------|
| ~~1~~ | ~~TX~~ | ~~bare ACK for flood TXT_MSG~~ | ~~high~~ | fixed: PATH+ACK (ptype 0x08) for flood, bare ACK for direct |
| 2 | TX | SET_RADIO_PARAMS doesn't retune lora_trx | medium | restart lora_trx after radio param change |
| 3 | TX | PATH_DISCOVERY sends CLI "status" not CTRL discover_req | low | still triggers a response from target |
| 4 | RX | TRACE/MULTI/GRP_DATA/REQ not handled | low | uncommon packet types |
| 5 | identity | IMPORT_PRIVATE_KEY doesn't persist to identity.bin | medium | manually copy seed file |
| 6 | security | EXPORT_PRIVATE_KEY has no PIN check | medium | — |
| 7 | factory reset | channels_dir not wiped | low | manually delete |
| 8 | factory reset | identity not regenerated (firmware does) | low | delete identity.bin + restart |
| 9 | repeater | no retransmit delay — immediate forward | medium | single-repeater setups unaffected |
| 10 | repeater | no airtime budget | medium | low-traffic mesh unaffected |
| 11 | repeater | deny_flood_broadcast not configurable | low | hardcoded true matches PR #1810 default |
| 12 | stats | battery/uptime/air_secs are fake | low | packet counters are real |
| 13 | scope | meshcore-cli `/scope` key derivation differs from firmware | medium | use config.toml `flood_scope` for repeater-compatible scoping |
| 14 | ANON_REQ TX | path routing from companion frame ignored | low | — |

## test matrix for manual validation

priority 0 = must pass, 1 = should pass, 2 = nice to have.

| # | p | test | steps | expected | checks |
|---|---|------|-------|----------|--------|
| 1 | 0 | connect | `meshcore-cli -t <ip> -p 7834` | prompt with node name | APP_START → SELF_INFO |
| 2 | 0 | contacts | `contacts` | known peers listed | GET_CONTACTS round-trip |
| 3 | 0 | send advert | `advert` | "OK", peer sees advert | ADVERT TX + Ed25519 sig |
| 4 | 0 | send direct msg | `msg <contact> hello` | "acked" within 30s | TXT_MSG TX + ACK RX |
| 5 | 0 | receive msg | peer sends msg to bridge | message appears in mccli | TXT_MSG RX + decrypt |
| 6 | 0 | channel TX | `chan 0 test message` | "OK" | GRP_TXT TX |
| 7 | 0 | channel RX | peer sends to public channel | message appears with sender | GRP_TXT RX + decrypt |
| 8 | 0 | device info | `infos` | model, freq, sf, bw, cr | DEVICE_QUERY → DEVICE_INFO |
| 9 | 0 | reconnect | disconnect + reconnect mccli | contacts + channels preserved | state persistence |
| 10 | 1 | set name + persist | `set name Foo` → restart bridge → connect | name shows "Foo" | EEPROM write + restore |
| 11 | 1 | set radio | `set radio 869.618,62.5,8,8` | "OK" | SET_RADIO_PARAMS |
| 12 | 1 | set scope | `/scope de-hh` | prompt shows scope | SET_FLOOD_SCOPE |
| 13 | 1 | scope persists over reconnect | set `flood_scope` in config.toml → connect mccli | scope active (log shows "ignoring all-zero") | scope guard |
| 14 | 1 | export contact | `export <name>` | `meshcore://` URI | EXPORT_CONTACT |
| 15 | 1 | import contact | `import_contact meshcore://...` → `contacts` | contact in list | IMPORT_CONTACT |
| 16 | 1 | set channel | set via companion | `channels` shows it | SET_CHANNEL |
| 17 | 1 | stats | `stats` | packet counters, queue len | GET_STATS (3 sub-types) |
| 18 | 1 | node discover | `nd` | nearby nodes respond | CTRL flood TX |
| 19 | 1 | remove contact | remove via companion | gone from `contacts` | REMOVE_CONTACT (6B prefix) |
| 20 | 1 | reset paths | `resetpath` → `contacts` | all paths show -1 | RESET_PATH |
| 21 | 2 | flood routing | send to contact with out_path=-1 | log shows "(flood)" | out_path_len routing |
| 22 | 2 | direct routing | send to contact with known path | log shows "(direct, path_len=N)" | PATH learning → DIRECT TX |
| 23 | 2 | repeater forward | enable repeat → peer floods msg → observer sees forwarded | path has bridge hash appended | _prepare_repeat_packet |
| 24 | 2 | repeater blocks advert | enable repeat → peer sends advert → observer does NOT see forwarded | PR #1810 filter | _DENYF_PAYLOAD_TYPES |
| 25 | 2 | repeater blocks channel | enable repeat → peer sends chan msg → observer does NOT see forwarded | PR #1810 filter | _DENYF_PAYLOAD_TYPES |
| 26 | 2 | factory reset | `factory_reset` → restart | contacts/keys gone, defaults restored | FACTORY_RESET |
| 27 | 2 | reboot | `reboot` | startup config restored | apply_startup_config |
| 28 | 2 | autoadd config | set autoadd → restart → verify | persisted | EEPROM round-trip |
| 29 | 2 | path hash mode | set mode → restart → verify | persisted | EEPROM round-trip |
| 30 | 2 | multi-hop via repeater | send to out-of-range contact with repeater between | message delivered + acked | end-to-end mesh routing |
