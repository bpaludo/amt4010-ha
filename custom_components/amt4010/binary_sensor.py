"""Zones, siren, panel problems and the health of the link."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_ZONES
from .coordinator import Amt4010ConfigEntry, Amt4010Coordinator
from .entity import Amt4010Entity, remove_stale_entities
from .protocol import Status
from .state import override_zones


@dataclass(frozen=True, kw_only=True)
class _Problem:
    key: str
    is_on: Callable[[Status], bool]
    attributes: Callable[[Status], dict] | None = None


# One entity per problem the status reports (SDK rows 368-428).
PROBLEMS = (
    _Problem(key="ac_failure", is_on=lambda s: s.ac_failure),
    _Problem(
        key="panel_battery",
        is_on=lambda s: s.battery_problem,
        attributes=lambda s: s.battery_details(),
    ),
    _Problem(key="aux_overload", is_on=lambda s: s.aux_overload),
    _Problem(
        key="siren_wiring",
        is_on=lambda s: s.siren_wiring_problem,
        attributes=lambda s: {
            "wire_cut": bool(s.system & 0x01),
            "short_circuit": bool(s.system & 0x02),
        },
    ),
    _Problem(key="phone_line", is_on=lambda s: s.phone_line_cut),
    _Problem(key="event_communication", is_on=lambda s: s.event_comm_failure),
    _Problem(
        key="bus_problem",
        is_on=lambda s: s.bus_problem,
        attributes=lambda s: {k: list(v) for k, v in s.bus_problems().items()},
    ),
    _Problem(
        key="keypad_tamper",
        is_on=lambda s: bool(s.keypads_tampered),
        attributes=lambda s: {"keypads": list(s.keypads_tampered)},
    ),
    _Problem(key="panel_problem", is_on=lambda s: s.problem_flag),
)

# Zone flags aggregated into one entity each, listing the zones: 64 zones x
# 5 flags would be 320 entities for what is almost always "none".
ZONE_FLAGS = {
    "zones_violated": "violated_zones",
    "zones_bypassed": "bypassed_zones",
    "zones_tamper": "tamper_zones",
    "zones_short_circuit": "short_zones",
    "zones_low_battery": "low_battery_zones",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Amt4010ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    known: set[int] = set()

    def settled() -> bool:
        # The zone list is authoritative once names were read or it was set by
        # hand. Before the first name read nothing is known: nothing goes.
        if coordinator.names_read_at:
            return True
        try:
            return bool(override_zones(entry.options.get(CONF_ZONES)))
        except ValueError:
            return False

    @callback
    def sync_zones() -> None:
        current = set(coordinator.zones)
        if settled():
            gone = known - current
            if gone:
                remove_stale_entities(
                    hass,
                    entry,
                    "binary_sensor",
                    "zone_",
                    {f"{entry.entry_id}_zone_{zone}" for zone in current},
                )
                known.difference_update(gone)
        new = [Amt4010Zone(coordinator, z) for z in sorted(current - known)]
        known.update(entity.zone for entity in new)
        if new:
            async_add_entities(new)

    async_add_entities(
        [
            Amt4010Siren(coordinator),
            Amt4010Link(coordinator),
            *(Amt4010Problem(coordinator, problem) for problem in PROBLEMS),
            *(Amt4010ZoneFlag(coordinator, key, attr) for key, attr in ZONE_FLAGS.items()),
        ]
    )
    if settled():
        # Zones that left the list since the last run go from the registry.
        remove_stale_entities(
            hass,
            entry,
            "binary_sensor",
            "zone_",
            {f"{entry.entry_id}_zone_{zone}" for zone in coordinator.zones},
        )
    sync_zones()
    # Names read again (button) add and remove zones without a reload.
    entry.async_on_unload(coordinator.async_add_listener(sync_zones))


class Amt4010Zone(Amt4010Entity, BinarySensorEntity):
    """On = the zone is open now (SDK bytes 1-8). Nothing else turns it on."""

    _attr_translation_key = "zone"

    def __init__(self, coordinator: Amt4010Coordinator, zone: int) -> None:
        super().__init__(coordinator, f"zone_{zone}")
        self.zone = zone
        self._attr_translation_placeholders = {"zone": f"{zone:02d}"}
        # Stable id by number: names arrive later and may change.
        self.entity_id = f"binary_sensor.amt_4010_zona_{zone:02d}"

    @property
    def name(self):
        if self.coordinator.zone_names.get(self.zone):
            return self.coordinator.zone_label(self.zone)
        return super().name

    @property
    def device_class(self) -> BinarySensorDeviceClass | None:
        value = self.coordinator.config_entry.options.get(
            f"zone_{self.zone}_device_class", "none"
        )
        return None if value in ("", "none") else BinarySensorDeviceClass(value)

    @property
    def is_on(self) -> bool | None:
        status = self.coordinator.data
        return None if status is None else self.zone in status.open_zones

    @property
    def extra_state_attributes(self) -> dict:
        status = self.coordinator.data
        if status is None:
            return {}
        attributes = {
            # "Violada" in the SDK. Whether it is alarm memory (as the AMT 8000's
            # "in alarm" turned out to be) is not proven for this panel.
            "violated": self.zone in status.violated_zones,
            "bypassed": self.zone in status.bypassed_zones,
        }
        if self.zone <= 8:  # tamper and short are reported for zones 1-8 only
            attributes["tamper"] = self.zone in status.tamper_zones
            attributes["short_circuit"] = self.zone in status.short_zones
        if self.zone >= 17:  # low battery: wireless zones 17-64 only
            attributes["low_battery"] = self.zone in status.low_battery_zones
        return attributes


class Amt4010Siren(Amt4010Entity, BinarySensorEntity):
    _attr_translation_key = "siren"
    _attr_device_class = BinarySensorDeviceClass.SOUND

    def __init__(self, coordinator: Amt4010Coordinator) -> None:
        super().__init__(coordinator, "siren")

    @property
    def is_on(self) -> bool | None:
        status = self.coordinator.data
        return None if status is None else status.siren_on

    @property
    def extra_state_attributes(self) -> dict:
        status = self.coordinator.data
        return {} if status is None else status.siren_bits()


class Amt4010Link(Amt4010Entity, BinarySensorEntity):
    """Whether what is on display was read in the last cycle.

    Stays available when everything else is not: it is what explains why.
    """

    _attr_translation_key = "panel_link"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: Amt4010Coordinator) -> None:
        super().__init__(coordinator, "panel_link")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_health_listener(self.async_write_ha_state)
        )

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.data_is_fresh

    @property
    def extra_state_attributes(self) -> dict:
        stale = self.coordinator.stale_since
        return {
            "consecutive_failures": self.coordinator.consecutive_failures,
            "last_failure": self.coordinator.last_failure,
            "stale_since": stale.isoformat() if stale else None,
        }


class Amt4010Problem(Amt4010Entity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: Amt4010Coordinator, problem: _Problem) -> None:
        super().__init__(coordinator, problem.key)
        self._problem = problem
        self._attr_translation_key = problem.key

    @property
    def is_on(self) -> bool | None:
        status = self.coordinator.data
        return None if status is None else self._problem.is_on(status)

    @property
    def extra_state_attributes(self) -> dict:
        status = self.coordinator.data
        if status is None or self._problem.attributes is None:
            return {}
        return self._problem.attributes(status)


class Amt4010ZoneFlag(Amt4010Entity, BinarySensorEntity):
    """On while any of the 64 zones carries the flag, known zone or not."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: Amt4010Coordinator, key: str, attr: str) -> None:
        super().__init__(coordinator, key)
        self._attr = attr
        self._attr_translation_key = key

    def _zones(self) -> list[int]:
        status = self.coordinator.data
        return [] if status is None else sorted(getattr(status, self._attr))

    @property
    def is_on(self) -> bool | None:
        return None if self.coordinator.data is None else bool(self._zones())

    @property
    def extra_state_attributes(self) -> dict:
        return {"zones": [self.coordinator.zone_label(z) for z in self._zones()]}
