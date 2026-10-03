# NOTICE — where every part comes from

This integration was written for the Intelbras AMT 4010 by Palalab. It is not a
fork of any single project: the protocol layer was written from the Intelbras
SDK spreadsheet, and the Home Assistant layer follows Palalab's private AMT 8000
integration, itself built on `fdaneluzzi/homeassistant-amt8000`. No code was
taken from projects with restrictive or missing licenses.

## Sources

| Source | License | Used for |
|---|---|---|
| Intelbras, *SDKCentraisDeAlarmeIntelbras-v1.0.1.xlsx*, sheet "4-Comandar central via APP" (as published at `jabenetti/intelbras-amt-hass-integration`, `doc/`) | vendor document | frame format, commands 5A/5B/5C/41/44, ACK/NACK table, the 54-byte status layout, model codes. Cited by row in `protocol.py` and in the tests |
| `andregoncalvespires/intelbras_alarm` @ `5d60e1b` (v2.1.5) | MIT, © 2026 André Gonçalves Pires | cross-check of the status layout (`parse_status_4010`); facts not in the SDK: stay bits 4/5 from firmware 5.7, zone names at EEPROM `0x0800` in 16-byte records with at most `0xC0` bytes per read, the "consecutive ASCII" factory pattern, clock bytes in binary rather than BCD, general bit 6 as the trigger latched until re-armed. Re-implemented, not copied |
| `Pehesi97/intelbras-amt-home-assistant` @ `9e1d060` | MIT, © 2026 Pedro (Pehesi97) | second source for the general status byte (siren = bit 1); its issue #10 (bit 2 rising with open zones on an AMT 4010) is one of the reasons bit 2 is not taken as the alarm since 0.1.1 |
| `fdaneluzzi/homeassistant-amt8000` @ `ac0cc470`, through Palalab's private AMT 8000 fork (0.6.0, `60b0f5e`) | README states MIT; no LICENSE file, no copyright line | shape of the Home Assistant layer: coordinator with tolerated failures and a health listener, command gate, device trigger, config/options/reauth flow. Adapted to this panel |
| Field captures, AMT 4010 firmware 6.6, 2026-09-29 and 2026-10-01 | — | model `0x41`, firmware `0x66`, `NACK E5` to `0x5A`, the clock bytes; general bits 2 and 6 and the violated map kept after an alarm with the panel disarmed. No client data is kept in this repository |

The AMT 8000 fork's protocol client (ISECNet2) is **not** used here.

## Per file

| File | Origin |
|---|---|
| `protocol.py` | Palalab, from the SDK; facts listed above credited inline |
| `client.py` | Palalab (one connection per operation, allowlist at the single write point) |
| `state.py` | Palalab; the "contradiction → unknown" rule comes from the AMT 8000 fork; `AlarmTracker` (alarm now versus memory) is Palalab's, from field captures |
| `coordinator.py`, `binary_sensor.py` (link sensor) | adapted from the AMT 8000 fork (fdaneluzzi base + Palalab) |
| `alarm_control_panel.py`, `config_flow.py`, `device_trigger.py` | adapted from the AMT 8000 fork; the device trigger now filters by device |
| `sensor.py`, `button.py`, `event.py`, `diagnostics.py`, `entity.py` | Palalab |
| `tools/sonda.py` | Palalab (standalone read-only probe) |
| tests | Palalab |
