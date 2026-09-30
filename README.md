# Intelbras AMT 4010 for Home Assistant

Local integration for the **Intelbras AMT 4010** alarm panel over its Ethernet
module (TCP 9009, ISECMobile protocol `0xE9`). No cloud, no extra Python
packages, no server of its own.

It is conservative by design: an alarm integration that guesses is worse than
none.

- **Installs read-only.** Arm/disarm is off until you enable it in the options,
  and it is enforced in code, not by hiding buttons. The services refuse, and
  the client refuses to put a command on the wire.
- **An allowlist at the single point that writes to the socket.** It allows
  status (`5A`, `5B`), zone-name reads (`5C`, only inside the name area) and,
  when enabled, arm/disarm (`41`/`44`, whole panel or one partition, never
  stay). Bypass, siren, panic, PGM, programming (`0xE7`) and everything else
  are refused before a connection is opened.
- **One TCP connection per operation**, serialized. The panel serves one
  client at a time and the owner's app uses the same port, so the integration
  never holds it.
- **Commands are never retried.** When a command leaves and no valid reply comes
  back, you get "unknown result, do not repeat it, check the keypad". A
  refusal (NACK) comes with its reason, e.g. "zones are open".
- **After an acknowledged command the state is read again**, strictly. A cached
  status never counts as confirmation.
- **Contradictions become `unknown`.** Example: the "armed" bit set with no
  partition armed. The panel does not publish a guess.
- **A refused password is never sent again**, from any operation (poll, name
  read, command). Polling stops, Home Assistant asks for the new password, and
  the refusal is remembered across restarts until a reauthentication succeeds.
- **The password never reaches a log**: not in frames, errors, attributes or
  the diagnostics download.

## Requirements

- AMT 4010 with the Ethernet module and its IP address reachable from Home
  Assistant (a DHCP reservation is recommended).
- The **master or a user password** (4 or 6 digits). The remote-access
  password is not accepted by this protocol (the panel answers NACK `E1`).
- Home Assistant 2026.8 or newer. Tested on 2026.8.3.

## Installation (HACS custom repository)

1. HACS → ⋮ → *Custom repositories* → this repository, category *Integration*.
2. Download it, restart Home Assistant.
3. *Settings → Devices & services → Add integration → Intelbras AMT 4010*.
   Enter the IP address, port `9009` and the password. The flow accepts an AMT
   4010 and nothing else: it checks both the `NACK E5` to `5A` and the model
   byte of `5B`.

## Entities

| Entity | Meaning |
|---|---|
| *Panel* | whole panel; commands `41`/`44` without a partition |
| *Partition A…D* | one per partition in use (option; default A and B), only if the panel is partitioned |
| *Zone NN* (binary sensor) | **on = open now**. Attributes: violated, bypassed, tamper/short (zones 1–8), low battery (17–64). Entity id `binary_sensor.amt_4010_zona_NN`, stable when names change |
| *Siren* | general status bit 1; the two raw siren bits of status byte 46 (SDK numbering) as attributes |
| *Panel communication* | whether the state on display was read in the last cycle; stays available to say why when everything else is not |
| Problems | AC failure, panel battery, auxiliary overload, siren wiring, phone line, event reporting, keypad/receiver/expander, keypad tamper, "panel reports a problem" |
| Zone flags | violated, bypassed, tamper, short circuit, low battery; each lists the zones (all 64, entity or not) |
| *Open zones* (sensor) | count and list of open zones among all 64. Shows zones that have no entity yet |
| *Panel clock* | the panel's own clock (diagnostic) |
| *Read zone names again* (button) | names are read once and stored; this reads them again |
| *Alarm* (event) + device trigger | rising edge of siren or firing zones; the bus event `amt4010_alarm_triggered` carries `device_id` and the device trigger matches only its own panel |

Zones get an entity when a name is programmed in the panel, or when listed in
the *Zones with an entity* option (e.g. `1-8, 12`).

With a Portuguese Home Assistant the entity ids come out in Portuguese
(`alarm_control_panel.amt_4010_central`, `…_particao_a`,
`binary_sensor.amt_4010_comunicacao_com_a_central`, …).

## Not yet proven on a real panel

These come from the SDK or from another project, and are decoded as documented
but not observed:

- siren and "zones firing" bits during a real alarm (SDK and Pehesi97 agree on
  the general byte; byte 46 is kept raw because the SDK and
  andregoncalvespires disagree on its siren bit). Pehesi97 reports that
  confirmation beeps can show as "siren on", and andregoncalvespires reads the
  "firing" bit as "some zone open": either would show a false *triggered*, so
  do not automate on the alarm event before this is checked on your panel;
- whether "violated" is alarm memory (it was on the AMT 8000);
- the meaning of the general "armed" bit with only some partitions armed;
- stay bits 4/5 (single source);
- every problem bit (no capture with a real fault yet).

The diagnostics download contains the raw 54 status bytes, so a real event can
settle each point without a second connection to the panel.

## Probe

`tools/sonda.py` is a standalone read-only probe (5A, 5B, 5C in the name area).
It reads the password from stdin and never prints it:

```bash
read -rs PW && printf '%s\n' "$PW" | python3 tools/sonda.py 192.0.2.10 5B; unset PW
```

## Tests

```bash
python3 -m unittest discover -s tests -v          # no Home Assistant, loopback panel
uv venv --python 3.14 .venv-ha
VIRTUAL_ENV=.venv-ha uv pip install "pytest-homeassistant-custom-component==0.13.357"
.venv-ha/bin/python -m pytest -q                  # inside Home Assistant 2026.8.3
```

## License

MIT. See [LICENSE](LICENSE) and [NOTICE.md](NOTICE.md) for where each part comes from.
Not affiliated with Intelbras.
