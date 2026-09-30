"""Polling, health, zone names and the alarm edge."""
from __future__ import annotations

import logging
import random
from collections.abc import Callable
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .client import (
    Amt4010Client,
    Amt4010Error,
    CannotConnect,
    InvalidAuth,
    MAX_BACKOFF_SECONDS,
    backoff_seconds,
    failure_reason,
)
from .const import (
    CONF_PARTITIONS,
    CONF_ZONES,
    DATA_STATE,
    DEFAULT_PARTITIONS,
    DOMAIN,
    EVENT_ALARM_TRIGGERED,
    FAILURE_CYCLES_BEFORE_UNAVAILABLE,
    NAME_MAX_ATTEMPTS,
    NAME_RETRY_SECONDS,
)
from .protocol import Status
from .state import known_zones, zone_label

_LOGGER = logging.getLogger(__name__)

type Amt4010ConfigEntry = ConfigEntry[Amt4010Coordinator]

STORE_VERSION = 1


def zone_name_store(hass: HomeAssistant, entry_id: str) -> Store:
    return Store(hass, STORE_VERSION, f"{DOMAIN}.{entry_id}.zone_names")


def state_store(hass: HomeAssistant, entry_id: str) -> Store:
    """What must survive a restart: a refused password and the alarm edge."""
    return Store(hass, STORE_VERSION, f"{DOMAIN}.{entry_id}.state")


async def async_clear_refusal(hass: HomeAssistant, entry_id: str) -> None:
    """Only a successful reauthentication calls this."""
    cached = hass.data.get(DATA_STATE, {}).get(entry_id)
    if cached is not None:
        cached["password_refused"] = False
    store = state_store(hass, entry_id)
    saved = await store.async_load() or {}
    await store.async_save({**saved, "password_refused": False})


class Amt4010Coordinator(DataUpdateCoordinator[Status]):
    config_entry: Amt4010ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: Amt4010ConfigEntry,
        client: Amt4010Client,
        scan_interval: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        self.client = client
        self._base_interval = scan_interval
        self._last_status: Status | None = None
        self._consecutive_failures = 0
        # Health, published by the connectivity entity: while failures are
        # tolerated the last status is republished, and only this says it is old.
        self.last_failure: str | None = None
        self.stale_since: datetime | None = None
        self._health_listeners: list[Callable[[], None]] = []
        # Alarm edge, shared by the bus event and the event entity.
        self._alarm_active = False  # loaded from state_store at setup
        self.alarm_edges = 0
        self.last_alarm_data: dict | None = None
        # Zone names: read from the panel once, kept in storage.
        self._store = zone_name_store(hass, entry.entry_id)
        self._state_store = state_store(hass, entry.entry_id)
        self._state: dict = {"password_refused": False, "alarm_active": False}
        self.zone_names: dict[int, str | None] = {}
        self.names_read_at: str | None = None
        self.names_last_error: str | None = None
        self._name_attempts = 0
        self._name_retry: CALLBACK_TYPE | None = None
        self._name_task = None

    @property
    def auth_failed(self) -> bool:
        """The client latched a refused password: nothing goes to the panel."""
        return self.client.password_refused

    @callback
    def async_password_refused(self) -> None:
        """E1 outside the poll (names, command, strict read): stop and ask."""
        self._remember_refusal()
        self.config_entry.async_start_reauth(self.hass)

    @callback
    def _remember_refusal(self) -> None:
        """Kept across restarts: a new client would otherwise send the refused
        password again after every reload (the 04:02 restart included). Only a
        successful reauthentication clears it (config_flow)."""
        self.async_shutdown_names()
        self._save_state(password_refused=True)

    @callback
    def _save_state(self, **changes) -> None:
        if all(self._state.get(k) == v for k, v in changes.items()):
            return
        self._state.update(changes)
        self._state_store.async_delay_save(lambda: dict(self._state), 0)

    async def async_load_state(self) -> bool:
        """Load what survives restarts. True when the password was refused before.

        Within one Home Assistant run the dict lives in hass.data, shared by
        every coordinator of the entry (a reload must not wait for a delayed
        write); the Store carries it across restarts.
        """
        cache = self.hass.data.setdefault(DATA_STATE, {})
        entry_id = self.config_entry.entry_id
        if entry_id not in cache:
            stored = await self._state_store.async_load() or {}
            cache[entry_id] = {
                "password_refused": bool(stored.get("password_refused")),
                "alarm_active": bool(stored.get("alarm_active")),
            }
        self._state = cache[entry_id]
        # An alarm already active before the restart is not a new alarm.
        self._alarm_active = self._state["alarm_active"]
        return self._state["password_refused"]

    # -- health -----------------------------------------------------------
    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def data_is_fresh(self) -> bool:
        return self.last_update_success and self._consecutive_failures == 0

    @callback
    def async_add_health_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        self._health_listeners.append(listener)

        @callback
        def remove() -> None:
            self._health_listeners.remove(listener)

        return remove

    @callback
    def _async_refresh_finished(self) -> None:
        # The base class says nothing while failures follow failures.
        if not self.last_update_success:
            for listener in list(self._health_listeners):
                listener()

    # -- polling ----------------------------------------------------------
    async def _async_update_data(self) -> Status:
        try:
            status = await self.client.get_status()
        except InvalidAuth as exc:
            # Polling stops here (Home Assistant does not reschedule after an
            # auth failure), and so does anything else that would send the same
            # password: repeating a wrong password at an alarm panel is exactly
            # what must not happen.
            self._note_failure(exc)
            self._remember_refusal()
            raise ConfigEntryAuthFailed("The panel refused the password") from exc
        except Amt4010Error as exc:
            return self._tolerate(exc)
        self._accept(status)
        return status

    def _note_failure(self, exc: Amt4010Error) -> None:
        self._consecutive_failures += 1
        self.last_failure = failure_reason(exc)
        if self.stale_since is None:
            self.stale_since = dt_util.utcnow()

    def _tolerate(self, exc: Amt4010Error) -> Status:
        self._note_failure(exc)
        self.update_interval = timedelta(
            seconds=min(
                backoff_seconds(self._base_interval, self._consecutive_failures)
                + random.uniform(0.0, 1.0),
                MAX_BACKOFF_SECONDS,
            )
        )
        _LOGGER.debug(
            "AMT 4010 %s, cycle %d/%d",
            self.last_failure,
            self._consecutive_failures,
            FAILURE_CYCLES_BEFORE_UNAVAILABLE,
        )
        if (
            self._last_status is not None
            and self._consecutive_failures < FAILURE_CYCLES_BEFORE_UNAVAILABLE
        ):
            return self._last_status
        raise UpdateFailed(f"AMT 4010 unavailable ({self.last_failure})") from exc

    def _accept(self, status: Status) -> None:
        if self._consecutive_failures:
            _LOGGER.info(
                "AMT 4010 answered again after %d failed cycle(s), last: %s",
                self._consecutive_failures,
                self.last_failure,
            )
        self._consecutive_failures = 0
        self.last_failure = None
        self.stale_since = None
        self.update_interval = timedelta(seconds=self._base_interval)
        self._last_status = status
        self._track_alarm(status)

    async def async_reconcile(self) -> bool:
        """Strict read after a command: the cached status never confirms one."""
        try:
            status = await self.client.get_status()
        except InvalidAuth:
            self.async_password_refused()
            return False
        except Amt4010Error:
            return False
        self._accept(status)
        self.async_set_updated_data(status)
        return True

    # -- alarm edge -------------------------------------------------------
    @property
    def device_id(self) -> str | None:
        device = dr.async_get(self.hass).async_get_device(
            identifiers={(DOMAIN, self.config_entry.entry_id)}
        )
        return device.id if device else None

    def _track_alarm(self, status: Status) -> None:
        if status.alarm_active and not self._alarm_active:
            self.alarm_edges += 1
            self.last_alarm_data = {
                "open_zones": [self.zone_label(z) for z in sorted(status.open_zones)],
                "violated_zones": [
                    self.zone_label(z) for z in sorted(status.violated_zones)
                ],
                "siren": status.siren_on,
                "zones_firing": status.zones_firing,
                "partitions_armed": [
                    p for p, armed in status.partitions_armed.items() if armed
                ],
            }
            self.hass.bus.async_fire(
                EVENT_ALARM_TRIGGERED,
                {
                    "device_id": self.device_id,
                    "entry_id": self.config_entry.entry_id,
                    **self.last_alarm_data,
                },
            )
        self._alarm_active = status.alarm_active
        self._save_state(alarm_active=status.alarm_active)

    # -- options-derived --------------------------------------------------
    @property
    def partitions_in_use(self) -> list[str]:
        return list(self.config_entry.options.get(CONF_PARTITIONS, DEFAULT_PARTITIONS))

    @property
    def zones(self) -> list[int]:
        """Zones with an entity; a bad option falls back to the named zones."""
        override = self.config_entry.options.get(CONF_ZONES, "")
        try:
            return known_zones(self.zone_names, override)
        except ValueError:
            return known_zones(self.zone_names, None)

    def zone_label(self, zone: int) -> str:
        return zone_label(zone, self.zone_names)

    # -- zone names -------------------------------------------------------
    async def async_load_names(self) -> None:
        stored = await self._store.async_load()
        if stored:
            self.zone_names = {int(k): v for k, v in stored["names"].items()}
            self.names_read_at = stored.get("read_at")

    @callback
    def async_start_name_read_if_needed(self) -> None:
        """Names are read once and kept: every read is traffic on a single-client
        port, and the panel may log remote EEPROM reads."""
        if self.names_read_at is None:
            self._schedule_name_read(0)

    @callback
    def async_request_name_read(self) -> None:
        """Explicit re-read (button)."""
        self._name_attempts = 0
        self._schedule_name_read(0)

    @callback
    def _schedule_name_read(self, delay: float) -> None:
        if self._name_retry:
            self._name_retry()
            self._name_retry = None
        if delay:
            self._name_retry = async_call_later(self.hass, delay, self._name_timer)
        else:
            self._start_name_task()

    @callback
    def _name_timer(self, _now) -> None:
        self._name_retry = None
        self._start_name_task()

    @callback
    def _start_name_task(self) -> None:
        if self.auth_failed:
            return
        if self._name_task and not self._name_task.done():
            return
        self._name_task = self.config_entry.async_create_background_task(
            self.hass, self._async_read_names(), f"{DOMAIN} zone names"
        )

    async def _async_read_names(self) -> None:
        self._name_attempts += 1
        try:
            names = await self.client.read_zone_names()
        except Amt4010Error as exc:
            self.names_last_error = failure_reason(exc)
            if isinstance(exc, InvalidAuth):
                self.async_password_refused()
            _LOGGER.debug(
                "Zone names not read (%s), attempt %d/%d",
                self.names_last_error,
                self._name_attempts,
                NAME_MAX_ATTEMPTS,
            )
            # Only a lost connection is worth another try: a NACK (e.g. a
            # firmware without 0x5C) or a refused password will not change.
            if self._name_attempts < NAME_MAX_ATTEMPTS and isinstance(exc, CannotConnect):
                self._schedule_name_read(NAME_RETRY_SECONDS)
            self.async_update_listeners()
            return
        if not any(names.values()) and any(self.zone_names.values()):
            # Named before, none now (e.g. a panel reset): do not let one read
            # delete every zone entity. The option "Zones" still decides by hand.
            self.names_last_error = "no_names"
            _LOGGER.warning(
                "AMT 4010 returned no programmed zone name; keeping the %d names read before",
                sum(1 for name in self.zone_names.values() if name),
            )
            self.async_update_listeners()
            return
        self.zone_names = names
        self.names_read_at = dt_util.utcnow().isoformat()
        self.names_last_error = None
        await self._store.async_save(
            {"names": {str(k): v for k, v in names.items()}, "read_at": self.names_read_at}
        )
        _LOGGER.info(
            "AMT 4010 zone names read: %d named zone(s)",
            sum(1 for name in names.values() if name),
        )
        self.async_update_listeners()

    @callback
    def async_shutdown_names(self) -> None:
        if self._name_retry:
            self._name_retry()
            self._name_retry = None
