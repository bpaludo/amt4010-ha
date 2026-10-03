# Changelog

## 0.1.1

- **Fix: alarm memory no longer shows as *triggered*.** General bit 2 (and
  bit 6) stay set after an alarm with the panel disarmed; 0.1.0 showed the
  panel and every partition *triggered* until the memory cleared, and a second
  alarm in that window raised no event. The alarm is now derived from the
  violated map and the latched bit while armed, plus a confirmed siren
  (README, *Alarm state*).
- Partitions are *triggered* only while armed.
- New entity *Alarm memory*. The siren entity is on for any of the three
  candidate siren bits (general bit 1, byte 46 bits 2 and 3) and gains
  `confirmed`.
- Events: one per zone entering the violated map, armed or not; one when the
  latched bit rises or the siren is confirmed with nothing reported yet.
  They carry `new_violated_zones` and `partitions_alarmed`. During startup the
  bus event waits until Home Assistant has started.
- Memory that clears completely (no violated zone, latched bit off) ends an
  alarm still on display (a disarm and re-arm between two reads).
- **Removed:** a silent alarm that records no violated zone is no longer seen
  (0.1.0 saw it through bit 2, with false alarms on every open zone).
- The raw status is logged (INFO) when it changes, except open zones, the clock
  and the battery icon.
- Upgrading from 0.1.0 takes what the panel shows at the first read as memory:
  no event. A rollback to 0.1.0 finds `alarm_active` in its old meaning.

## 0.1.0

First release: status, zones, partitions, problems, zone names, arm/disarm
(opt-in).
