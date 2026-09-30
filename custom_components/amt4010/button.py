"""Re-read the zone names from the panel (they are read once and kept)."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import Amt4010ConfigEntry, Amt4010Coordinator
from .entity import Amt4010Entity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Amt4010ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([Amt4010ReadNames(entry.runtime_data)])


class Amt4010ReadNames(Amt4010Entity, ButtonEntity):
    """Only reads (0x5C in the zone-name area); never changes the panel."""

    _attr_translation_key = "read_zone_names"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: Amt4010Coordinator) -> None:
        super().__init__(coordinator, "read_zone_names")

    @property
    def available(self) -> bool:
        return True

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "read_at": self.coordinator.names_read_at,
            "last_error": self.coordinator.names_last_error,
            "named_zones": sum(1 for n in self.coordinator.zone_names.values() if n),
        }

    async def async_press(self) -> None:
        self.coordinator.async_request_name_read()
