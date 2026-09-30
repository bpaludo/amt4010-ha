"""Config flow: host, port and password; accepts an AMT 4010 and nothing else."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers import selector

from .client import Amt4010Client, CannotConnect, InvalidAuth, NotAmt4010
from .const import (
    CONF_CODE_ARM_REQUIRED,
    CONF_ENABLE_COMMANDS,
    CONF_PARTITIONS,
    CONF_SCAN_INTERVAL,
    CONF_ZONES,
    DEFAULT_CODE_ARM_REQUIRED,
    DEFAULT_ENABLE_COMMANDS,
    DEFAULT_PARTITIONS,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .coordinator import Amt4010ConfigEntry, async_clear_refusal
from .protocol import MODEL_AMT4010, PARTITIONS, valid_password
from .state import parse_zone_list

_LOGGER = logging.getLogger(__name__)

# No default: the password is typed by a person every time.
_PASSWORD = selector.TextSelector(
    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
)

_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=65535)
        ),
        vol.Required(CONF_PASSWORD): _PASSWORD,
    }
)
_REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): _PASSWORD})

_DEVICE_CLASSES = ["none", "door", "window", "motion", "smoke", "vibration", "garage_door"]


async def _validate(host: str, port: int, password: str) -> str | None:
    """Error key, or None when the panel is an AMT 4010 that took the password."""
    if not valid_password(password):
        return "invalid_password_format"
    client = Amt4010Client(host, port, password)
    try:
        await client.detect()
        status = await client.get_status()
    except InvalidAuth:
        return "invalid_auth"
    except NotAmt4010:
        return "not_amt4010"
    except CannotConnect:
        return "cannot_connect"
    except Exception:  # noqa: BLE001 - shown as "unknown", logged without data
        _LOGGER.exception("Unexpected error while validating the AMT 4010")
        return "unknown"
    if status.model != MODEL_AMT4010:
        return "not_amt4010"
    return None


class Amt4010ConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: Amt4010ConfigEntry) -> OptionsFlow:
        return Amt4010OptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = user_input[CONF_PORT]
            password = user_input[CONF_PASSWORD].strip()
            # The protocol exposes no MAC or serial; the address has a DHCP
            # reservation.
            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()
            error = await _validate(host, port, password)
            if error is None:
                return self.async_create_entry(
                    title="AMT 4010",
                    data={CONF_HOST: host, CONF_PORT: port, CONF_PASSWORD: password},
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="user", data_schema=_USER_SCHEMA, errors=errors
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            password = user_input[CONF_PASSWORD].strip()
            error = await _validate(entry.data[CONF_HOST], entry.data[CONF_PORT], password)
            if error is None:
                await async_clear_refusal(self.hass, entry.entry_id)
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: password}
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=_REAUTH_SCHEMA, errors=errors
        )


class Amt4010OptionsFlow(OptionsFlow):
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        options = self.config_entry.options
        if user_input is not None:
            try:
                parse_zone_list(user_input.get(CONF_ZONES, ""))
            except ValueError:
                errors[CONF_ZONES] = "invalid_zones"
            if not user_input.get(CONF_PARTITIONS):
                errors[CONF_PARTITIONS] = "no_partitions"
            if not errors:
                # Merge: with the panel offline the zone list is empty, and
                # saving user_input alone would drop the zone device classes.
                # An emptied text field is absent from user_input: clear it.
                return self.async_create_entry(
                    data={**options, CONF_ZONES: "", **user_input}
                )

        coordinator = getattr(self.config_entry, "runtime_data", None)
        zones = coordinator.zones if coordinator else []
        fields: dict = {
            vol.Required(
                CONF_SCAN_INTERVAL,
                default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            ): vol.All(
                vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL)
            ),
            vol.Required(
                CONF_ENABLE_COMMANDS,
                default=options.get(CONF_ENABLE_COMMANDS, DEFAULT_ENABLE_COMMANDS),
            ): bool,
            vol.Required(
                CONF_CODE_ARM_REQUIRED,
                default=options.get(CONF_CODE_ARM_REQUIRED, DEFAULT_CODE_ARM_REQUIRED),
            ): bool,
            vol.Required(
                CONF_PARTITIONS,
                default=options.get(CONF_PARTITIONS, DEFAULT_PARTITIONS),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(options=list(PARTITIONS), multiple=True)
            ),
            vol.Optional(
                CONF_ZONES,
                description={"suggested_value": options.get(CONF_ZONES, "")},
            ): str,
        }
        for zone in zones:
            key = f"zone_{zone}_device_class"
            fields[vol.Optional(key, default=options.get(key, "none"))] = (
                selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=_DEVICE_CLASSES, translation_key="zone_device_class"
                    )
                )
            )
        return self.async_show_form(
            step_id="init", data_schema=vol.Schema(fields), errors=errors
        )
