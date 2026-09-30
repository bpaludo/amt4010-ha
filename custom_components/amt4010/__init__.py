"""Intelbras AMT 4010 — local polling over ISECMobile (0xE9)."""
from __future__ import annotations

from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed

from .client import Amt4010Client
from .const import (
    CONF_ENABLE_COMMANDS,
    DATA_STATE,
    CONF_SCAN_INTERVAL,
    DEFAULT_ENABLE_COMMANDS,
    DEFAULT_SCAN_INTERVAL,
)
from .coordinator import (
    Amt4010ConfigEntry,
    Amt4010Coordinator,
    state_store,
    zone_name_store,
)

PLATFORMS = [
    Platform.ALARM_CONTROL_PANEL,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.EVENT,
    Platform.SENSOR,
]


async def async_setup_entry(hass: HomeAssistant, entry: Amt4010ConfigEntry) -> bool:
    client = Amt4010Client(
        entry.data[CONF_HOST],
        entry.data[CONF_PORT],
        entry.data[CONF_PASSWORD],
        commands_enabled=entry.options.get(CONF_ENABLE_COMMANDS, DEFAULT_ENABLE_COMMANDS),
    )
    coordinator = Amt4010Coordinator(
        hass,
        entry,
        client,
        scan_interval=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
    )
    if await coordinator.async_load_state():
        # Refused before this restart: ask again, send nothing.
        raise ConfigEntryAuthFailed("The panel refused this password before")
    await coordinator.async_load_names()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.async_start_name_read_if_needed()
    entry.async_on_unload(coordinator.async_shutdown_names)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: Amt4010ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: Amt4010ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: Amt4010ConfigEntry) -> None:
    await zone_name_store(hass, entry.entry_id).async_remove()
    await state_store(hass, entry.entry_id).async_remove()
    hass.data.get(DATA_STATE, {}).pop(entry.entry_id, None)
