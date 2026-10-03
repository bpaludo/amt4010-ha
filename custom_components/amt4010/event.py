"""Event entity: one per alarm episode (state.AlarmTracker decides)."""
from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import Amt4010ConfigEntry, Amt4010Coordinator
from .entity import Amt4010Entity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Amt4010ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([Amt4010AlarmEvent(entry.runtime_data)])


class Amt4010AlarmEvent(Amt4010Entity, EventEntity):
    _attr_translation_key = "alarm"
    _attr_event_types = ["alarm_triggered"]

    def __init__(self, coordinator: Amt4010Coordinator) -> None:
        super().__init__(coordinator, "alarm_event")
        # The edge is detected once, in the coordinator; this only follows it.
        # From zero: an alarm seen by the first read after setup (it began while
        # Home Assistant was down) happened before this entity existed.
        self._seen_edges = 0

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._follow_edge():
            self.async_write_ha_state()

    def _follow_edge(self) -> bool:
        if self.coordinator.alarm_edges == self._seen_edges:
            return False
        self._seen_edges = self.coordinator.alarm_edges
        self._trigger_event("alarm_triggered", self.coordinator.last_alarm_data)
        return True

    @callback
    def _handle_coordinator_update(self) -> None:
        self._follow_edge()
        super()._handle_coordinator_update()
