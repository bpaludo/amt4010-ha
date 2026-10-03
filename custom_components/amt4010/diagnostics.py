"""Diagnostics download: decoded status plus the raw 54 bytes. Password redacted.

The panel's port serves one client, so a separate probe competes with the
integration; this reuses the status the integration already read. The status
bytes carry no credential (the password only exists in frames we send, which
are never kept).
"""
from __future__ import annotations

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant

from .coordinator import Amt4010ConfigEntry, Amt4010Coordinator

TO_REDACT = {CONF_PASSWORD}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: Amt4010ConfigEntry
) -> dict:
    info = {
        "state": entry.state.value,
        "data": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
    }
    coordinator: Amt4010Coordinator | None = getattr(entry, "runtime_data", None)
    if coordinator is None:
        # Not loaded (e.g. retrying setup): exactly when this download matters.
        return {"entry": info, "health": None, "status": None}
    status = coordinator.data
    stale = coordinator.stale_since
    return {
        "entry": info,
        "health": {
            "data_is_fresh": coordinator.data_is_fresh,
            "consecutive_failures": coordinator.consecutive_failures,
            "last_failure": coordinator.last_failure,
            "stale_since": stale.isoformat() if stale else None,
        },
        "zone_names": {
            "read_at": coordinator.names_read_at,
            "last_error": coordinator.names_last_error,
            # Names describe the client's house; a diagnostics file may end up
            # attached to a public issue. Count only.
            "named_zones": sum(1 for name in coordinator.zone_names.values() if name),
        },
        "status": None
        if status is None
        else {
            "raw": status.raw.hex(" "),
            "model": status.model_name,
            "firmware": status.firmware_version,
            "partitioned": status.partitioned,
            "partitions_armed": status.partitions_armed,
            "partitions_stay": status.partitions_stay,
            "general": f"0x{status.general:02x}",
            "alarm": {
                "siren_confirmed": coordinator.alarm.siren,
                "partitions_alarmed": sorted(coordinator.alarm.alarmed),
                "alarm_memory": status.alarm_memory,
                "tracker": coordinator._tracker.snapshot(),
            },
            "clock": status.clock.isoformat() if status.clock else None,
            "siren_bits": status.siren_bits(),
            "battery": status.battery_details(),
            "bus": status.bus_problems(),
            "open_zones": sorted(status.open_zones),
            "violated_zones": sorted(status.violated_zones),
            "bypassed_zones": sorted(status.bypassed_zones),
            "pgm_on": sorted(status.pgm_on),
        },
    }
