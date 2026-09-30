"""Base entity: one device per panel."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import Amt4010Coordinator


class Amt4010Entity(CoordinatorEntity[Amt4010Coordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: Amt4010Coordinator, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        status = coordinator.data
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="AMT 4010",
            manufacturer="Intelbras",
            model=status.model_name if status else "AMT 4010",
            sw_version=status.firmware_version if status else None,
        )


def remove_stale_entities(
    hass: HomeAssistant, entry: ConfigEntry, platform: str, prefix: str, keep: set[str]
) -> None:
    """Drop registry entries of this entry, under ``prefix``, not in ``keep``.

    A partition taken out of use, or a zone taken out of the list, would
    otherwise linger as "unavailable" forever.
    """
    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (
            entity.domain == platform
            and entity.unique_id.startswith(f"{entry.entry_id}_{prefix}")
            and entity.unique_id not in keep
        ):
            registry.async_remove(entity.entity_id)
