"""How a status becomes alarm states and zone lists — pure, no Home Assistant.

Rule inherited from the AMT 8000 fork: when the panel contradicts itself the
answer is "unknown", never a guess. "Disarmed while armed" leaves a property
unprotected without anyone noticing; "armed while disarmed" gives false
confidence.
"""
from __future__ import annotations

from .protocol import ZONE_COUNT, Status

DISARMED = "disarmed"
ARMED_AWAY = "armed_away"
ARMED_HOME = "armed_home"
TRIGGERED = "triggered"


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


def partition_state(status: Status, letter: str) -> str | None:
    """One partition. A panel-wide alarm shows on every partition: the status
    does not say which partition fired, and a quiet partition while the siren
    runs would be a lie. A self-contradicting status is unknown here too: a
    partition must never look disarmed when the panel says something is armed."""
    if status.alarm_active:
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


def panel_state(status: Status, in_use: list[str]) -> str | None:
    """The whole panel, reconciled with the general "armed" bit.

    Unpartitioned: the general bit decides. Partitioned: the partitions in use
    decide — all armed is away (home if any is in stay), some armed is home.
    The general bit is a consistency check only where every reading of it
    agrees: set while no partition is armed is a contradiction. Whether it
    means "any" or "all" partitions armed is not documented, so a partial
    arm does not test it.
    """
    if status.alarm_active:
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
