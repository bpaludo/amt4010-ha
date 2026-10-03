"""The whole panel plus one entity per partition in use."""
from __future__ import annotations

import asyncio
import logging

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
    CodeFormat,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .client import (
    AmbiguousResult,
    Amt4010Error,
    InvalidAuth,
    NotSent,
    Rejected,
)
from .const import (
    CONF_CODE_ARM_REQUIRED,
    CONF_ENABLE_COMMANDS,
    DEFAULT_CODE_ARM_REQUIRED,
    DEFAULT_ENABLE_COMMANDS,
    DOMAIN,
    SETTLE_SECONDS,
)
from .coordinator import Amt4010ConfigEntry, Amt4010Coordinator
from .entity import Amt4010Entity, remove_stale_entities
from .state import (
    ARMED_AWAY,
    ARMED_HOME,
    DISARMED,
    TRIGGERED,
    panel_state,
    partition_state,
)

_LOGGER = logging.getLogger(__name__)

# One command at a time: the panel serves one connection.
PARALLEL_UPDATES = 1

_HA_STATE = {
    DISARMED: AlarmControlPanelState.DISARMED,
    ARMED_AWAY: AlarmControlPanelState.ARMED_AWAY,
    ARMED_HOME: AlarmControlPanelState.ARMED_HOME,
    TRIGGERED: AlarmControlPanelState.TRIGGERED,
}

# NACK code -> translation key of the error shown to the user.
_REJECTION_KEYS = {
    0xE3: "not_partitioned",
    0xE4: "zones_open",
    0xE7: "no_permission",
    0xEA: "partition_without_zones",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Amt4010ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[AlarmControlPanelEntity] = [Amt4010Panel(coordinator)]
    if coordinator.data and coordinator.data.partitioned:
        entities += [
            Amt4010Partition(coordinator, letter)
            for letter in coordinator.partitions_in_use
        ]
    remove_stale_entities(
        hass,
        entry,
        "alarm_control_panel",
        "partition_",
        {entity.unique_id for entity in entities},
    )
    async_add_entities(entities)


def _error(key: str, **placeholders: str) -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders or None,
    )


class _Commandable(Amt4010Entity, AlarmControlPanelEntity):
    """Commands are opt-in and enforced here, not only by hiding buttons:
    the services can be called directly, so every method refuses too."""

    _attr_code_format = CodeFormat.NUMBER
    _partition: str | None = None

    @property
    def _commands_enabled(self) -> bool:
        return self.coordinator.config_entry.options.get(
            CONF_ENABLE_COMMANDS, DEFAULT_ENABLE_COMMANDS
        )

    @property
    def supported_features(self) -> AlarmControlPanelEntityFeature:
        if not self._commands_enabled:
            return AlarmControlPanelEntityFeature(0)
        return AlarmControlPanelEntityFeature.ARM_AWAY

    @property
    def code_arm_required(self) -> bool:
        return self.coordinator.config_entry.options.get(
            CONF_CODE_ARM_REQUIRED, DEFAULT_CODE_ARM_REQUIRED
        )

    def _check(self, code: str | None, *, code_required: bool) -> None:
        if not self._commands_enabled or not self.coordinator.client.commands_enabled:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="commands_disabled"
            )
        # Checked locally, before any connection; neither code is ever echoed.
        if code_required and not self.coordinator.client.password_matches(code):
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="invalid_code"
            )

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        self._check(code, code_required=self.code_arm_required)
        await self._run(self.coordinator.client.arm(self._partition))

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        self._check(code, code_required=True)
        await self._run(self.coordinator.client.disarm(self._partition))

    async def _run(self, command) -> None:
        """Run one command. Never retried; every outcome is stated plainly."""
        try:
            await command
        except Rejected as exc:
            key = _REJECTION_KEYS.get(exc.code)
            if key:
                raise _error(key) from exc
            raise _error("rejected", code=f"0x{exc.code:02X}") from exc
        except InvalidAuth as exc:
            self.coordinator.async_password_refused()
            raise _error("password_refused") from exc
        except AmbiguousResult as exc:
            await asyncio.sleep(SETTLE_SECONDS)
            await self.coordinator.async_reconcile()
            raise _error("ambiguous") from exc
        except NotSent as exc:
            raise _error("not_sent") from exc
        except Amt4010Error as exc:
            raise _error("ambiguous") from exc
        await asyncio.sleep(SETTLE_SECONDS)
        if not await self.coordinator.async_reconcile():
            raise _error("not_read_back")


class Amt4010Panel(_Commandable):
    """41/44 without a partition: the whole panel."""

    _attr_translation_key = "panel"

    def __init__(self, coordinator: Amt4010Coordinator) -> None:
        super().__init__(coordinator, "panel")
        self._warned: str | None = None

    @property
    def alarm_state(self) -> AlarmControlPanelState | None:
        status = self.coordinator.data
        if status is None:
            return None
        state = panel_state(
            status, self.coordinator.partitions_in_use, self.coordinator.alarm
        )
        if state is None:
            signature = f"{status.raw[27]:02x}{status.raw[28]:02x}{status.general:02x}"
            if signature != self._warned:
                self._warned = signature
                _LOGGER.warning(
                    "AMT 4010 state is not decidable (partition bytes %s %s, general "
                    "0x%02x, partitions in use %s): publishing unknown instead of "
                    "guessing; check the keypad",
                    f"0x{status.raw[27]:02x}",
                    f"0x{status.raw[28]:02x}",
                    status.general,
                    ",".join(self.coordinator.partitions_in_use),
                )
            return None
        self._warned = None
        return _HA_STATE[state]

    @property
    def extra_state_attributes(self) -> dict:
        status = self.coordinator.data
        if status is None:
            return {}
        alarm = self.coordinator.alarm
        return {
            "partitioned": status.partitioned,
            "armed_bit": status.armed_flag,
            "partitions_armed": [p for p, on in status.partitions_armed.items() if on],
            "partitions_in_use": self.coordinator.partitions_in_use,
            "partitions_alarmed": sorted(alarm.alarmed),
            "siren_confirmed": alarm.siren,
            "alarm_memory": status.alarm_memory,
            "general_byte": f"0x{status.general:02x}",
        }


class Amt4010Partition(_Commandable):
    _attr_translation_key = "partition"

    def __init__(self, coordinator: Amt4010Coordinator, letter: str) -> None:
        super().__init__(coordinator, f"partition_{letter.lower()}")
        self._partition = letter
        self._attr_translation_placeholders = {"partition": letter}

    @property
    def alarm_state(self) -> AlarmControlPanelState | None:
        status = self.coordinator.data
        if status is None:
            return None
        state = partition_state(status, self._partition, self.coordinator.alarm)
        return None if state is None else _HA_STATE[state]

    @property
    def extra_state_attributes(self) -> dict:
        status = self.coordinator.data
        if status is None or status.partitions_stay is None:
            return {}
        return {"stay": status.partitions_stay[self._partition]}
