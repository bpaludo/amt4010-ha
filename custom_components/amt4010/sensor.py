"""Open zones (all 64, entity or not) and the panel clock."""
from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import Amt4010ConfigEntry, Amt4010Coordinator
from .entity import Amt4010Entity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Amt4010ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities([Amt4010OpenZones(coordinator), Amt4010Clock(coordinator)])


class Amt4010OpenZones(Amt4010Entity, SensorEntity):
    """How many zones are open, and which: also reveals zones with no entity."""

    _attr_translation_key = "open_zones"

    def __init__(self, coordinator: Amt4010Coordinator) -> None:
        super().__init__(coordinator, "open_zones")

    @property
    def native_value(self) -> int | None:
        status = self.coordinator.data
        return None if status is None else len(status.open_zones)

    @property
    def extra_state_attributes(self) -> dict:
        status = self.coordinator.data
        if status is None:
            return {}
        zones = sorted(status.open_zones)
        return {
            "zones": [self.coordinator.zone_label(z) for z in zones],
            "zone_numbers": zones,
        }


class Amt4010Clock(Amt4010Entity, SensorEntity):
    """The panel's own clock (minute precision), in the Home Assistant time zone."""

    _attr_translation_key = "panel_clock"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: Amt4010Coordinator) -> None:
        super().__init__(coordinator, "panel_clock")

    @property
    def native_value(self) -> datetime | None:
        status = self.coordinator.data
        if status is None or status.clock is None:
            return None
        return status.clock.replace(tzinfo=dt_util.get_default_time_zone())
