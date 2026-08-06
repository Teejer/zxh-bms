# bms-cli

Written with the help of Qwen3.6 35b. Allows zxh-bms batteries to be monitored in Homeassistant or cli.

A Linux command-line tool for the family of Bluetooth LE "smart BMS" boards
used in many rebranded LiFePO4 batteries. The protocol was reverse engineered
from the vendor's Android app (`zxhbms`, a uni-app/DCloud hybrid app) by
reading its bundled JavaScript (`common/libs/Command/DataPacker.js`,
`common/function/crc.js`, and `pages/controlPanel/index/index.vue`).

This tool is **read-only**. It does not implement the app's parameter-write
or firmware-upgrade commands, so there's no risk of misconfiguring
protection settings or bricking the BMS.

## The protocol, briefly

- Transport: BLE GATT. The BMS exposes one of a few known service/characteristic
  pairs (Nordic UART-alike `6E400001`, or vendor UUIDs `00002760...` /
  `0003CDD0...`); the tool auto-detects which one is present, same as the app.
- Frame format is Modbus-flavored but not quite Modbus:
  - Request: `[address, function, reg_hi, reg_lo, count, crc_hi, crc_lo]` (7 bytes)
  - Response: `[address, function, len_hi, len_lo, data..., crc_hi, crc_lo]`
  - `function` 3 = read parameter/config register, 4 = read status/realtime register.
  - `len` is a 16-bit word (not the usual single Modbus byte count).
  - CRC is CRC16/XMODEM (poly 0x1021, init 0), **not** the classic Modbus CRC16.
- Each reading (pack current, cell voltages, temperatures, ...) is a specific
  `(function, register, count)` triplet the firmware expects -- see
  `bms_cli/protocol.py` for the full map and scaling factors.

Write support (changing protection thresholds, renaming the pack, firmware
upgrade) uses function codes 6/16 in the app and could be added later if
you need it, but isn't included here on purpose.

## Install

```
cd lifepo4-cli
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Requires BlueZ on the host (standard on any modern Linux) and a user with
permission to use BLE (`bluetoothd` running; if you hit permission errors,
add your user to the `bluetooth` group or run via the venv with the
capability the way you'd run any other BlueZ client).

## Usage

```
# find your battery's BLE MAC address
bms-cli scan

# one-shot identification (cell count, nominal voltage/capacity, firmware)
bms-cli info AA:BB:CC:DD:EE:FF

# one status snapshot: pack V/A/W, SOC, per-cell mV, temps, protection flags
bms-cli status AA:BB:CC:DD:EE:FF
bms-cli status AA:BB:CC:DD:EE:FF --json

# poll continuously
bms-cli watch AA:BB:CC:DD:EE:FF --interval 2

# the BMS bus address is auto-discovered on every connect (most boards ignore
# commands sent to the wrong address); override it explicitly if you ever need to
bms-cli status AA:BB:CC:DD:EE:FF --address 1

# check what address a board reports, without doing anything else
bms-cli pack-number AA:BB:CC:DD:EE:FF

# explore/verify an arbitrary register (for extending the protocol map)
bms-cli raw AA:BB:CC:DD:EE:FF --function 4 --register 3000 --count 5
```

### Scanning and flaky connections

Cheap BLE modules routinely fail the first connect attempt or take a while to
show up in a scan. Every command that connects retries the whole
connect+GATT-discovery sequence automatically:

```
--connect-retries 3     # how many times to retry (default 3)
--retry-delay 2         # seconds between retries (default 2)
--connect-timeout 10    # seconds to wait per individual attempt (default 10)
```

`scan` takes longer to find some devices; increase its window with:

```
bms-cli scan --timeout 20
```

`scan` also retries automatically (`--retries 3`/`--retry-delay 2`, same
defaults as connect) if BlueZ reports `org.bluez.Error.InProgress` --
BlueZ's discovery state is a single resource shared by the whole adapter, so
this shows up occasionally whenever something else (a stuck previous scan,
another BLE client, an `mqtt-bridge` instance reconnecting right now) is
using it at the same moment. It's usually transient and clears on retry.

For the same reason, `mqtt-bridge` serializes BLE *connects* across multiple
devices in one process (GATT reads/writes on already-connected devices still
run concurrently) -- letting every device try to connect at the same instant
was a real way to trigger this error.

Add `--debug` to any command to see the discovered GATT services/characteristics,
every byte written, and every notification received -- useful when a connect or
a read is failing and you need to see what's actually happening on the wire.

## Known register map

| Reading | function | register | count |
|---|---|---|---|
| Label/identification (cell count, temp probe count, nominal V/Ah, full Ah) | 3 | 4000 | 5 |
| Instrument (current, SOC, MOS temp, balance bits, protection bits) | 4 | 3000 | 5 |
| Cell voltages | 4 | 3012 + 14×page | 7 (or remainder) |
| Basic info (capacity, cycles, health, charge/discharge counts) | 4 | 3076 | 7 |
| Temperature probes (up to 4) | 4 | 3087 | 4 |
| Protection event counters, part 1 | 4 | 3095 | 7 |
| Protection event counters, part 2 | 4 | 3109 | 7 |
| Pack bus address discovery (broadcast) | 3 | 4008 | 1 |
| Manufacture date + firmware version | 3 | 4009 | 3 |
| Serial number (2 chunks) | 3 | 4023, 4037 | 1, 1 |
| Manufacturer name (2 chunks) | 3 | 4051, 4065 | 1, 1 |

Note the address quirk: these boards follow real Modbus addressing, where `0`
is the broadcast address slaves never reply to except for the one pack-number
discovery read above. Every other command must go to the pack's real address,
which is why `--address` auto-discovers it via that read on every connect
(see `bms-cli pack-number` to check it standalone).

Protection bitmask (16 bits, MSB-first, bit 0 = LSB) is decoded in
`protocol.PROTECTION_BITS`.

## Home Assistant integration

The recommended path is `bms-cli mqtt-bridge`: a persistent daemon that
connects once, keeps polling over that same BLE connection, and publishes
readings to MQTT with [Home Assistant MQTT
Discovery](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery)
-- entities just appear under Settings > Devices & Services > MQTT, no HA-side
YAML required. It publishes one retained discovery config per sensor (pack
voltage/current/power/SOC, every individual cell voltage, up to 4 temp
probes, cycle count, health, a "Protection Active" binary sensor, etc.), then
one JSON state update per poll.

A transient BLE drop does **not** mark entities "unavailable" -- it just
stops updating them, so HA keeps showing the last known values (frozen, not
blank) until the reconnect succeeds. A "Last Updated" timestamp sensor is
published alongside the rest (`device_class: timestamp`) so you can tell how
stale the frozen values are, or alert if it stops advancing. Only a genuine
bridge shutdown (a clean stop, or the process dying and the MQTT broker
firing its last-will) marks the device offline. If you'd rather see
"unavailable" during outages instead of stale values, that's a one-line
change in `_run_single_bridge`'s
exception handler in `bms_cli/mqtt_bridge.py`.

```
pip install -e ".[mqtt]"   # pulls in paho-mqtt

bms-cli mqtt-bridge AA:BB:CC:DD:EE:FF \
  --mqtt-host 192.168.1.10 \
  --mqtt-username homeassistant \
  --mqtt-password-env MQTT_PASSWORD \
  --interval 5
```

Prefer `--mqtt-password-env` (reads from an environment variable) over
`--mqtt-password` so the credential doesn't show up in `ps` output or shell
history.

### Multiple batteries

Pass more than one MAC address to monitor several packs from a single
process/service:

```
bms-cli mqtt-bridge AA:BB:CC:DD:EE:FF 11:22:33:44:55:66 \
  --mqtt-host 192.168.1.10 --mqtt-username homeassistant \
  --mqtt-password-env MQTT_PASSWORD
```

Each device gets its own BLE connection, its own topics (`bms/<mac>/...`,
so no collisions), its own Home Assistant device entry, and its own MQTT
connection (so a dropped/reconnecting device's availability doesn't affect
the others) -- but they all run as one process/service, so one `systemctl
restart` (or crash/`Restart=always`) covers every pack. `--address` can only
be used with a single device, since each pack's bus address is auto-discovered
independently; just omit it for multi-device mode.

To run it persistently, a systemd unit like this works well
(`/etc/systemd/system/bms-mqtt-bridge.service`):

```ini
[Unit]
Description=BMS to MQTT bridge for Home Assistant
After=bluetooth.service network-online.target
Wants=bluetooth.service

[Service]
Type=simple
EnvironmentFile=/etc/bms-mqtt-bridge.env
ExecStart=/home/youruser/lifepo4-cli/.venv/bin/bms-cli mqtt-bridge AA:BB:CC:DD:EE:FF \
  --mqtt-host 192.168.1.10 --mqtt-username homeassistant \
  --mqtt-password-env MQTT_PASSWORD --interval 5
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

with `/etc/bms-mqtt-bridge.env` containing `MQTT_PASSWORD=...` (root-readable
only: `chmod 600`). Then `sudo systemctl enable --now bms-mqtt-bridge`.

If you'd rather avoid a persistent daemon, a `command_line` sensor pointed at `bms-cli status --json`
works too.

## Caveats

- Byte offsets and scaling factors were derived by tracing the exact
  arithmetic in the app's minified JS, and have since been validated against
  a real 16S/51.2V pack (`info`/`status` output matches expected values:
  correct cell count, sane cell voltages/balance, temps, SOC, capacity).
  Fields not yet exercised against real hardware: protection event counters
  (`--protect-counters`), serial number, and manufacturer name.
- Some fields (protection counter labels, decay/aging counters) are best-effort
  translations of the original Chinese variable names.
