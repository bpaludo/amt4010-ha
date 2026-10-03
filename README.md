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
- **Alarm memory is not an alarm.** No single status bit means "alarm now" on
  this panel (see below), so *triggered* is derived, and what the panel keeps
  after disarming is a separate *Alarm memory* entity.
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
| *Panel* | whole panel; commands `41`/`44` without a partition. *Triggered* while the alarm is live (see *Alarm state*) |
| *Partition A…D* | one per partition in use (option; default A and B), only if the panel is partitioned. *Triggered* only while armed |
| *Zone NN* (binary sensor) | **on = open now**. Attributes: violated, bypassed, tamper/short (zones 1–8), low battery (17–64). Entity id `binary_sensor.amt_4010_zona_NN`, stable when names change |
| *Siren* | on = any of the three candidate siren bits, raw (general bit 1; byte 46 bits 2 and 3, SDK numbering — the sources disagree, see below); each bit as an attribute; `confirmed` = seen for 3 s or more (what the alarm state uses) |
| *Alarm memory* | on while the panel remembers an alarm: a zone in the violated map or the latched bit (general bit 6). Stays on after disarming, until the panel clears it |
| *Panel communication* | whether the state on display was read in the last cycle; stays available to say why when everything else is not |
| Problems | AC failure, panel battery, auxiliary overload, siren wiring, phone line, event reporting, keypad/receiver/expander, keypad tamper, "panel reports a problem" |
| Zone flags | violated, bypassed, tamper, short circuit, low battery; each lists the zones (all 64, entity or not) |
| *Open zones* (sensor) | count and list of open zones among all 64. Shows zones that have no entity yet |
| *Panel clock* | the panel's own clock (diagnostic) |
| *Read zone names again* (button) | names are read once and stored; this reads them again |
| *Alarm* (event) + device trigger | see *Alarm state*; the bus event `amt4010_alarm_triggered` carries `device_id`, `new_violated_zones` and `partitions_alarmed`, and the device trigger matches only its own panel. During startup it waits until Home Assistant has started, so automations see it |

Zones get an entity when a name is programmed in the panel, or when listed in
the *Zones with an entity* option (e.g. `1-8, 12`).

With a Portuguese Home Assistant the entity ids come out in Portuguese
(`alarm_control_panel.amt_4010_central`, `…_particao_a`,
`binary_sensor.amt_4010_comunicacao_com_a_central`, …).

## Alarm state

Version 0.1.0 took general bit 2 (the SDK's "zones firing") as the alarm. On a
real panel (firmware 6.6) that bit, together with bit 6, **stayed set for more
than a day after an alarm**, with the panel disarmed and no zone open, while the
zone kept its place in the violated map: 0.1.0 showed the panel and every
partition *triggered* all that time, and a second alarm in that window would
have raised no event. Another AMT 4010 saw bit 2 rise with open zones (Pehesi97
issue #10). Neither bit says "alarm now", so since 0.1.1:

- a partition is *triggered* when, **while it is armed**, a zone enters the
  violated map or the latched bit (general bit 6) rises; it stays so until
  disarmed. The status does not say which partition fired, so every armed
  partition shows it;
- the siren counts once seen across reads at least 3 s apart, so an arm/disarm
  confirmation beep is not an alarm. Any of the three candidate siren bits
  counts. A confirmed siren makes the panel *triggered* even when disarmed
  (24 h zone, panic);
- memory left from an earlier alarm never makes anything *triggered*. Memory
  that clears completely (no zone left in the violated map and bit 6 off) means
  the panel was armed again or reset, so an alarm still shown is over — this
  covers a disarm and re-arm between two reads. A partial clear (one zone
  leaving, or bit 6 dropping while zones remain) may be another partition's
  memory and changes nothing;
- events: one for every zone that enters the violated map, armed or not; one
  when bit 6 rises or the siren is confirmed with nothing reported yet in the
  episode. A burglar crossing three zones gives three events.

Known gaps:

- **a silent alarm that puts no zone in the violated map** (for example a
  silent panic, if the panel keeps it out of the map) is not seen. 0.1.0 saw it
  through bit 2, at the price of false alarms on every open zone;
- a zone that is already in memory and fires again silently is not seen,
  because the map does not change. With the siren it is;
- if the panel does **not** clear its memory on re-arm, a disarm and re-arm
  between two reads leaves the old alarm on display until the next disarm that
  is read. A new zone still raises its event.

The integration logs the raw status at INFO whenever a status byte changes,
except open zones (bytes 1–8: every door would be a line), the clock (31–35)
and the keypad battery icon (41) — byte numbers as in the SDK. The status
carries no credential. Home Assistant only keeps INFO for this integration when
the logger allows it (`logger:` → `logs: custom_components.amt4010: info`).
This log is the field record for what follows.

## Not yet proven on a real panel

These come from the SDK or from another project, and are decoded as documented
but not observed:

- the siren bits during a real alarm (byte 46 is kept raw because the SDK and
  andregoncalvespires disagree on its siren bit), and whether a confirmation
  beep sets general bit 1;
- whether the violated map gets a zone only when the alarm fires, or already
  when an entry or exit delay starts, or only at disarm (an entry delay that
  marks the zone would make every arrival *triggered*, with an event);
- when the memory clears: on the next arm of the same partition, of any
  partition, or never by itself;
- the meaning of general bit 2 (open zones, memory, or both);
- the meaning of the general "armed" bit with only some partitions armed;
- stay bits 4/5 (single source);
- every problem bit (no capture with a real fault yet).

Do not automate on the alarm event before these are checked on your panel.
The diagnostics download contains the raw 54 status bytes and the alarm
tracker, so a real event can settle each point without a second connection to
the panel.

## Probe

`tools/sonda.py` is a standalone read-only probe (5A, 5B, 5C in the name area).
It reads the password from stdin and never prints it:

```bash
read -rs PW && printf '%s\n' "$PW" | python3 tools/sonda.py 192.0.2.10 5B; unset PW
```

## Tests

```bash
python3 -m unittest discover -s tests -v          # Python 3.12+, no Home Assistant, loopback panel
uv venv --python 3.14 .venv-ha
VIRTUAL_ENV=.venv-ha uv pip install "pytest-homeassistant-custom-component==0.13.357"
.venv-ha/bin/python -m pytest -q                  # inside Home Assistant 2026.8.3
```

## License

MIT. See [LICENSE](LICENSE) and [NOTICE.md](NOTICE.md) for where each part comes from.
Not affiliated with Intelbras.
