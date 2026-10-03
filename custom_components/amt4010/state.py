"""How a status becomes alarm states and zone lists — pure, no Home Assistant.

Rule inherited from the AMT 8000 fork: when the panel contradicts itself the
answer is "unknown", never a guess. "Disarmed while armed" leaves a property
unprotected without anyone noticing; "armed while disarmed" gives false
confidence.
"""
from __future__ import annotations

import dataclasses

from .protocol import ZONE_COUNT, Status

DISARMED = "disarmed"
ARMED_AWAY = "armed_away"
ARMED_HOME = "armed_home"
TRIGGERED = "triggered"

# Siren bit seen across reads at least this far apart before it counts:
# arm/disarm confirmation beeps can show as "siren on" (Pehesi97), and the
# strict read one second after a command lands right on them.
SIREN_CONFIRM_SECONDS = 3.0
# Key for an unpartitioned panel in the "alarmed" set.
WHOLE_PANEL = "*"


@dataclasses.dataclass(frozen=True)
class AlarmView:
    """What the tracker concluded from one status."""

    siren: bool  # siren confirmed (see SIREN_CONFIRM_SECONDS)
    alarmed: frozenset[str]  # armed partitions (or WHOLE_PANEL) that fired
    new_zones: frozenset[int]  # zones that entered the violated map now
    fire_event: bool

    @property
    def live(self) -> bool:
        return self.siren or bool(self.alarmed)


QUIET = AlarmView(siren=False, alarmed=frozenset(), new_zones=frozenset(), fire_event=False)


def _armed_keys(status: Status) -> frozenset[str]:
    if status.partitioned:
        return frozenset(p for p, on in status.partitions_armed.items() if on)
    return frozenset({WHOLE_PANEL}) if status.armed_flag else frozenset()


@dataclasses.dataclass
class AlarmTracker:
    """Alarm now versus alarm memory, which the AMT 4010 status mixes up.

    No bit of the status means "alarm now" (protocol.GENERAL_FIRING). What is
    trusted instead:
    - a zone entering the violated map, or the latched bit rising, while a
      partition is armed: those armed partitions fired, until disarmed. The
      status does not say which partition fired, so all armed ones show it;
    - the siren (any of its three candidate bits), once confirmed across reads
      (beeps are not alarms).
    Memory left from an earlier alarm — what is already in the violated map,
    the latched bit already set — never makes anything triggered. Memory that
    clears completely (no zone left in the map and the latched bit off) means
    the panel was armed again or reset: whatever fired before is over, even if
    the disarm fell between two reads. A partial clear does not count: it may
    be another partition's memory (one zone leaving, or the single latched bit
    dropping while zones remain).

    Events: one for every zone that enters the violated map, armed or not (each
    is a fact the panel recorded); one when the latched bit rises or the siren
    is confirmed with nothing reported yet in the episode. The episode ends at
    the first quiet read.

    Known gaps: a zone already in memory firing again silently is not seen (the
    map does not change); a silent alarm that records no zone (e.g. a silent
    panic, if the panel keeps it out of the map) is not seen either.
    """

    initialized: bool = False
    violated: frozenset[int] = frozenset()
    latched: bool = False
    alarmed: frozenset[str] = frozenset()
    episode: bool = False
    # Not a timestamp across restarts: only whether it was confirmed, so a
    # reload during a running siren does not drop "triggered" for one read.
    siren_since: float | None = None
    siren_was_confirmed: bool = False

    def update(self, status: Status, now: float) -> AlarmView:
        armed = _armed_keys(status)
        if self.initialized:
            new_zones = status.violated_zones - self.violated
            rose = status.trigger_latched and not self.latched
            cleared = (bool(self.violated) or self.latched) and not status.alarm_memory
        else:
            # First read ever (or after 0.1.0): what is there is memory.
            new_zones, rose, cleared = frozenset(), False, False
            self.initialized = True
        self.violated = status.violated_zones
        self.latched = status.trigger_latched
        if cleared:
            # The episode is left to the quiet-read rule below: a siren still
            # sounding is the same alarm, not a new event.
            self.alarmed = frozenset()
        if (new_zones or rose) and armed:
            self.alarmed |= armed
        self.alarmed &= armed  # a disarmed partition is no longer alarmed
        if status.siren_any:
            if self.siren_since is None:
                self.siren_since = now - (
                    SIREN_CONFIRM_SECONDS if self.siren_was_confirmed else 0.0
                )
        else:
            self.siren_since = None
        siren = self.siren_since is not None and now - self.siren_since >= SIREN_CONFIRM_SECONDS
        self.siren_was_confirmed = siren
        live = siren or bool(self.alarmed)
        fire = bool(new_zones) or ((rose or live) and not self.episode)
        if fire:
            self.episode = True
        elif not live and not status.siren_any:
            self.episode = False
        return AlarmView(siren=siren, alarmed=self.alarmed, new_zones=new_zones, fire_event=fire)

    def snapshot(self) -> dict | None:
        if not self.initialized:
            return None
        return {
            "violated": sorted(self.violated),
            "latched": self.latched,
            "alarmed": sorted(self.alarmed),
            "episode": self.episode,
            "siren": self.siren_was_confirmed,
        }

    @classmethod
    def restore(cls, data: dict | None) -> AlarmTracker:
        if not data:
            return cls()
        return cls(
            initialized=True,
            violated=frozenset(int(z) for z in data.get("violated", ())),
            latched=bool(data.get("latched")),
            alarmed=frozenset(str(p) for p in data.get("alarmed", ())),
            episode=bool(data.get("episode")),
            siren_was_confirmed=bool(data.get("siren")),
        )


def _contradiction(status: Status) -> bool:
    """Readings that no interpretation of the bits can reconcile.

    - the general "armed" bit set while no partition is armed;
    - a partition reported in stay while its armed bit is clear.
    Whether the general bit means "any" or "all" partitions armed is not
    documented, so an armed partition with the general bit clear is not one.
    """
    armed = any(status.partitions_armed.values())
    if status.partitioned and status.armed_flag and not armed:
        return True
    if not status.partitioned and armed and not status.armed_flag:
        # Unpartitioned, a partition bit says armed and the panel says it is
        # not: believing the general bit would claim "disarmed".
        return True
    stay = status.partitions_stay or {}
    return any(stay.get(p) and not status.partitions_armed[p] for p in stay)


def partition_state(status: Status, letter: str, alarm: AlarmView = QUIET) -> str | None:
    """One partition. Triggered only while armed: the status does not say
    which partition fired, so an alarm shows on every armed partition, and a
    disarmed one is never "triggered" (alarm memory is a separate entity).
    A self-contradicting status is unknown here too: a partition must never
    look disarmed when the panel says something is armed."""
    if status.partitioned and status.partitions_armed[letter] and (
        alarm.siren or letter in alarm.alarmed
    ):
        return TRIGGERED
    if _contradiction(status) or not status.partitioned:
        # A partition entity of a panel that is (no longer) partitioned has no
        # state of its own: showing "disarmed" there could contradict the panel.
        return None
    if not status.partitions_armed[letter]:
        return DISARMED
    if status.partitions_stay and status.partitions_stay[letter]:
        return ARMED_HOME
    return ARMED_AWAY


def panel_state(status: Status, in_use: list[str], alarm: AlarmView = QUIET) -> str | None:
    """The whole panel, reconciled with the general "armed" bit.

    Triggered while the alarm is live (AlarmTracker): a confirmed siren, even
    disarmed (24 h zone, panic), or an armed partition that fired.
    Unpartitioned: the general bit decides. Partitioned: the partitions in use
    decide — all armed is away (home if any is in stay), some armed is home.
    The general bit is a consistency check only where every reading of it
    agrees: set while no partition is armed is a contradiction. Whether it
    means "any" or "all" partitions armed is not documented, so a partial
    arm does not test it.
    """
    if alarm.live:
        return TRIGGERED
    if _contradiction(status):
        return None
    if not status.partitioned:
        if not status.armed_flag:
            return DISARMED
        # The panel reports stay per partition bit even unpartitioned: away
        # there would claim more protection than stay gives.
        return ARMED_HOME if any((status.partitions_stay or {}).values()) else ARMED_AWAY
    armed = {letter for letter, on in status.partitions_armed.items() if on}
    if armed - set(in_use):
        # A partition we were told is unused is armed: the option is wrong.
        return None
    if not armed:
        return DISARMED  # "armed" bit without an armed partition: _contradiction
    stay = status.partitions_stay or {}
    if armed == set(in_use):
        return ARMED_HOME if any(stay.get(p) for p in armed) else ARMED_AWAY
    return ARMED_HOME


def parse_zone_list(text: str) -> list[int]:
    """"1-8, 12" -> [1..8, 12]. Raises ValueError on anything else."""
    zones: set[int] = set()
    for part in text.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            low, high = (int(x) for x in part.split("-", 1))
            if low > high:
                raise ValueError(part)
            zones.update(range(low, high + 1))
        else:
            zones.add(int(part))
    if any(not 1 <= zone <= ZONE_COUNT for zone in zones):
        raise ValueError("zones are 1-64")
    return sorted(zones)


def override_zones(override: str | None) -> list[int]:
    """The zone option; spaces or separators alone mean "not set"."""
    return parse_zone_list(override or "")


def known_zones(names: dict[int, str | None], override: str | None) -> list[int]:
    """Zones that get an entity: the option when set, else the named ones."""
    return override_zones(override) or sorted(z for z, name in names.items() if name)


def zone_label(zone: int, names: dict[int, str | None]) -> str:
    """"05 Sala"; installers often type the number into the name already
    ("05Sala", "05 Sala"), and then it is not repeated."""
    name = names.get(zone)
    if not name:
        return f"{zone:02d}"
    digits = name[: len(name) - len(name.lstrip("0123456789"))]
    return name if digits and int(digits) == zone else f"{zone:02d} {name}"
