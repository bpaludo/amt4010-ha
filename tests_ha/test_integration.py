"""The integration inside a real Home Assistant, against a loopback AMT 4010."""
from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant import config_entries
from homeassistant.components import automation
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.amt4010 import protocol as P
from custom_components.amt4010.const import DOMAIN, EVENT_ALARM_TRIGGERED
from custom_components.amt4010.diagnostics import async_get_config_entry_diagnostics

PASSWORD = "9876"


async def setup_entry(hass: HomeAssistant, panel, **options) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="AMT 4010",
        data={"host": "127.0.0.1", "port": panel.port, "password": PASSWORD},
        options={"scan_interval": 5, **options},
        unique_id=f"127.0.0.1:{panel.port}",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    task = entry.runtime_data._name_task
    if task is not None:
        await task
    await hass.async_block_till_done()
    return entry


def eid(hass: HomeAssistant, entry, key: str) -> str:
    registry = er.async_get(hass)
    for platform in ("alarm_control_panel", "binary_sensor", "sensor", "event", "button"):
        found = registry.async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}_{key}")
        if found:
            return found
    raise AssertionError(f"no entity for {key}")


async def refresh(hass: HomeAssistant, entry) -> None:
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


async def test_entities_and_names(hass: HomeAssistant, fake_panel) -> None:
    entry = await setup_entry(hass, fake_panel)
    assert entry.state is ConfigEntryState.LOADED
    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    ids = {e.entity_id for e in entities}
    # panel + A + B, 3 named zones ("Zona NN" is the default = no name), siren,
    # link, 9 problems, 5 zone flags, 2 sensors, button, event
    assert len(entities) == 3 + 3 + 1 + 1 + 9 + 5 + 2 + 1 + 1, sorted(ids)
    assert {"binary_sensor.amt_4010_zona_01", "binary_sensor.amt_4010_zona_03"} <= ids
    assert hass.states.get("binary_sensor.amt_4010_zona_03").attributes["friendly_name"] == (
        "AMT 4010 03 Garagem"
    )
    assert hass.states.get("binary_sensor.amt_4010_zona_05").attributes["friendly_name"] == (
        "AMT 4010 05Cozinha"  # the number is already in the name: not repeated
    )
    assert "binary_sensor.amt_4010_zona_02" not in ids
    panel = hass.states.get(eid(hass, entry, "panel"))
    assert panel.state == "disarmed"
    assert panel.attributes["supported_features"] == 0
    assert hass.states.get(eid(hass, entry, "partition_a")).attributes["supported_features"] == 0
    assert hass.states.get(eid(hass, entry, "panel_link")).state == "on"
    assert hass.states.get(eid(hass, entry, "open_zones")).state == "0"
    clock = hass.states.get(eid(hass, entry, "panel_clock")).state
    assert clock.startswith("2026-09-29T")
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    assert (device.name, device.model, device.sw_version) == ("AMT 4010", "AMT 4010", "6.6")
    # 1 x 5A? no: setup reads 5B, then the 6 name blocks
    assert fake_panel.commands() == [P.CMD_STATUS] + [P.CMD_READ_EEPROM] * 6


async def test_entity_ids_in_portuguese(hass: HomeAssistant, fake_panel) -> None:
    """The client's Home Assistant runs in pt-BR: these are the ids it creates."""
    hass.config.language = "pt-BR"
    entry = await setup_entry(hass, fake_panel)
    ids = {e.entity_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)}
    for expected in (
        "alarm_control_panel.amt_4010_central",
        "alarm_control_panel.amt_4010_particao_a",
        "alarm_control_panel.amt_4010_particao_b",
        "binary_sensor.amt_4010_zona_01",
        "binary_sensor.amt_4010_sirene",
        "binary_sensor.amt_4010_comunicacao_com_a_central",
        "binary_sensor.amt_4010_falta_de_energia_ac",
        "binary_sensor.amt_4010_zonas_violadas",
        "sensor.amt_4010_zonas_abertas",
        "sensor.amt_4010_relogio_da_central",
        "button.amt_4010_reler_nomes_das_zonas",
        "event.amt_4010_disparo",
    ):
        assert expected in ids, sorted(ids)


async def test_names_are_read_once(hass: HomeAssistant, fake_panel) -> None:
    entry = await setup_entry(hass, fake_panel)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert fake_panel.commands().count(P.CMD_READ_EEPROM) == 6  # kept in storage
    await hass.services.async_call(
        "button", "press", {"entity_id": eid(hass, entry, "read_zone_names")}, blocking=True
    )
    await entry.runtime_data._name_task
    assert fake_panel.commands().count(P.CMD_READ_EEPROM) == 12


async def test_zone_option_and_open_zones(hass: HomeAssistant, fake_panel) -> None:
    entry = await setup_entry(hass, fake_panel, zones="2, 5-6")
    assert {"binary_sensor.amt_4010_zona_02", "binary_sensor.amt_4010_zona_05"} <= set(
        hass.states.async_entity_ids("binary_sensor")
    )
    assert "binary_sensor.amt_4010_zona_01" not in hass.states.async_entity_ids("binary_sensor")
    fake_panel.status[0] = 0b0001_0010  # zones 2 and 5 open
    fake_panel.status[8] = 0b0000_0010  # zone 2 violated
    await refresh(hass, entry)
    assert hass.states.get("binary_sensor.amt_4010_zona_02").state == "on"
    assert hass.states.get("binary_sensor.amt_4010_zona_02").attributes["violated"] is True
    assert hass.states.get("binary_sensor.amt_4010_zona_06").state == "off"
    open_zones = hass.states.get(eid(hass, entry, "open_zones"))
    assert open_zones.state == "2"
    assert open_zones.attributes["zones"] == ["02", "05Cozinha"]
    violated = hass.states.get(eid(hass, entry, "zones_violated"))
    assert (violated.state, violated.attributes["zones"]) == ("on", ["02"])


async def test_problems(hass: HomeAssistant, fake_panel) -> None:
    entry = await setup_entry(hass, fake_panel)
    fake_panel.status[35] = 0x01 | 0x04
    fake_panel.status[42] = 0x01
    await refresh(hass, entry)
    assert hass.states.get(eid(hass, entry, "ac_failure")).state == "on"
    battery = hass.states.get(eid(hass, entry, "panel_battery"))
    assert battery.state == "on" and battery.attributes["missing_or_reversed"] is True
    assert hass.states.get(eid(hass, entry, "siren_wiring")).state == "on"
    assert hass.states.get(eid(hass, entry, "phone_line")).state == "off"


async def test_commands_disabled_never_reach_the_panel(hass: HomeAssistant, fake_panel) -> None:
    entry = await setup_entry(hass, fake_panel)
    for entity in (eid(hass, entry, "panel"), eid(hass, entry, "partition_a")):
        for service in ("alarm_disarm", "alarm_arm_away"):
            with pytest.raises(ServiceValidationError):
                await hass.services.async_call(
                    "alarm_control_panel",
                    service,
                    {"entity_id": entity, "code": PASSWORD},
                    blocking=True,
                )
    assert P.CMD_ARM not in fake_panel.commands()
    assert P.CMD_DISARM not in fake_panel.commands()


async def test_arm_partition_sends_one_frame_then_rereads(hass: HomeAssistant, fake_panel) -> None:
    entry = await setup_entry(hass, fake_panel, enable_commands=True)
    fake_panel.on_command[(P.CMD_ARM, b"\x41")] = {27: 0x01, 29: 0x08}
    partition = eid(hass, entry, "partition_a")
    assert hass.states.get(partition).attributes["supported_features"] == 2  # ARM_AWAY
    before = len(fake_panel.frames)
    await hass.services.async_call(
        "alarm_control_panel", "alarm_arm_away", {"entity_id": partition, "code": PASSWORD},
        blocking=True,
    )
    assert fake_panel.frames[before:] == [(P.CMD_ARM, b"\x41"), (P.CMD_STATUS, b"")]
    assert hass.states.get(partition).state == "armed_away"
    assert hass.states.get(eid(hass, entry, "panel")).state == "armed_home"  # A of A+B


async def test_wrong_code_never_reaches_the_panel(hass: HomeAssistant, fake_panel) -> None:
    entry = await setup_entry(hass, fake_panel, enable_commands=True)
    with pytest.raises(ServiceValidationError) as info:
        await hass.services.async_call(
            "alarm_control_panel", "alarm_disarm",
            {"entity_id": eid(hass, entry, "panel"), "code": "1234"}, blocking=True,
        )
    assert "1234" not in str(info.value) and PASSWORD not in str(info.value)
    assert P.CMD_DISARM not in fake_panel.commands()


async def test_refusal_and_ambiguity_are_never_retried(hass: HomeAssistant, fake_panel) -> None:
    entry = await setup_entry(hass, fake_panel, enable_commands=True)
    panel = eid(hass, entry, "panel")
    fake_panel.behaviour[P.CMD_ARM] = "nack:E4"
    with pytest.raises(HomeAssistantError) as info:
        await hass.services.async_call(
            "alarm_control_panel", "alarm_arm_away", {"entity_id": panel, "code": PASSWORD},
            blocking=True,
        )
    assert info.value.translation_key == "zones_open"
    assert fake_panel.commands().count(P.CMD_ARM) == 1

    fake_panel.behaviour[P.CMD_DISARM] = "drop"
    with pytest.raises(HomeAssistantError) as info:
        await hass.services.async_call(
            "alarm_control_panel", "alarm_disarm", {"entity_id": panel, "code": PASSWORD},
            blocking=True,
        )
    assert info.value.translation_key == "ambiguous"
    assert fake_panel.commands().count(P.CMD_DISARM) == 1


async def test_wrong_password_stops_polling_and_asks(hass: HomeAssistant, fake_panel) -> None:
    entry = await setup_entry(hass, fake_panel)
    fake_panel.behaviour[P.CMD_STATUS] = "nack:E1"
    await refresh(hass, entry)
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == ["reauth"]
    seen = fake_panel.connections
    for minutes in (1, 5, 30):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=minutes))
        await hass.async_block_till_done()
    assert fake_panel.connections == seen  # never repeated at the panel


async def test_stale_then_unavailable_then_back(hass: HomeAssistant, fake_panel) -> None:
    entry = await setup_entry(hass, fake_panel)
    link, zone = eid(hass, entry, "panel_link"), "binary_sensor.amt_4010_zona_01"
    fake_panel.behaviour[P.CMD_STATUS] = "drop"
    await refresh(hass, entry)
    assert hass.states.get(link).state == "off"
    assert hass.states.get(link).attributes["last_failure"] == "no_reply"
    assert hass.states.get(zone).state == "off"  # last status, tolerated
    await refresh(hass, entry)
    await refresh(hass, entry)
    assert hass.states.get(zone).state == "unavailable"
    assert hass.states.get(link).state == "off"  # still there to say why
    assert entry.runtime_data.update_interval.total_seconds() >= 40
    del fake_panel.behaviour[P.CMD_STATUS]
    await refresh(hass, entry)
    assert hass.states.get(link).state == "on"
    assert hass.states.get(zone).state == "off"
    assert entry.runtime_data.update_interval.total_seconds() == 5


async def test_alarm_event_and_device_trigger_per_device(
    hass: HomeAssistant, fake_panel, second_panel
) -> None:
    first = await setup_entry(hass, fake_panel)
    second = await setup_entry(hass, second_panel)
    devices = dr.async_get(hass)
    first_device = devices.async_get_device(identifiers={(DOMAIN, first.entry_id)})
    assert await async_setup_component(
        hass,
        automation.DOMAIN,
        {
            automation.DOMAIN: {
                "trigger": {
                    "platform": "device", "domain": DOMAIN,
                    "device_id": first_device.id, "type": "alarm_triggered",
                },
                "action": {"event": "first_panel_fired"},
            }
        },
    )
    fired = async_capture_events(hass, "first_panel_fired")
    bus = async_capture_events(hass, EVENT_ALARM_TRIGGERED)

    second_panel.status[29] = 0x02  # siren on the second panel
    await refresh(hass, second)
    assert len(bus) == 1 and len(fired) == 0

    fake_panel.status[29] = 0x04  # silent alarm on the first
    fake_panel.status[8] = 0x04  # zone 3 violated
    await refresh(hass, first)
    await refresh(hass, first)  # still active: no second edge
    assert len(fired) == 1 and len(bus) == 2
    assert bus[1].data["device_id"] == first_device.id
    assert bus[1].data["violated_zones"] == ["03 Garagem"]
    event = hass.states.get(eid(hass, first, "alarm_event"))
    assert event.attributes["event_type"] == "alarm_triggered"
    assert hass.states.get(eid(hass, first, "panel")).state == "triggered"


async def test_diagnostics_without_password(hass: HomeAssistant, fake_panel) -> None:
    entry = await setup_entry(hass, fake_panel)
    text = str(await async_get_config_entry_diagnostics(hass, entry))
    assert PASSWORD not in text and "39 38 37 36" not in text
    assert "41 66 01" in text  # the raw status is there


async def test_config_flow(hass: HomeAssistant, fake_panel) -> None:
    flow = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    async def submit(**data):
        return await hass.config_entries.flow.async_configure(
            flow["flow_id"],
            {"host": "127.0.0.1", "port": fake_panel.port, "password": PASSWORD, **data},
        )

    result = await submit(password="12345")
    assert result["errors"] == {"base": "invalid_password_format"}
    assert fake_panel.connections == 0

    fake_panel.behaviour[P.CMD_PARTIAL_STATUS] = "nack:E1"
    assert (await submit())["errors"] == {"base": "invalid_auth"}
    fake_panel.behaviour[P.CMD_PARTIAL_STATUS] = "nack:E2"
    assert (await submit())["errors"] == {"base": "not_amt4010"}
    del fake_panel.behaviour[P.CMD_PARTIAL_STATUS]
    fake_panel.status[24] = 0x1E  # an AMT 2018 status
    assert (await submit())["errors"] == {"base": "not_amt4010"}
    fake_panel.status[24] = 0x41

    result = await submit()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "AMT 4010"
    assert result["result"].unique_id == f"127.0.0.1:{fake_panel.port}"
    await hass.async_block_till_done()
    again = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    again = await hass.config_entries.flow.async_configure(
        again["flow_id"],
        {"host": "127.0.0.1", "port": fake_panel.port, "password": PASSWORD},
    )
    assert again["reason"] == "already_configured"


async def test_reauth(hass: HomeAssistant, fake_panel) -> None:
    entry = await setup_entry(hass, fake_panel)
    result = await entry.start_reauth_flow(hass)
    fake_panel.behaviour[P.CMD_PARTIAL_STATUS] = "nack:E1"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"password": "1111"}
    )
    assert result["errors"] == {"base": "invalid_auth"}
    del fake_panel.behaviour[P.CMD_PARTIAL_STATUS]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"password": "2222"}
    )
    assert result["reason"] == "reauth_successful"
    assert entry.data["password"] == "2222"


async def test_options(hass: HomeAssistant, fake_panel) -> None:
    entry = await setup_entry(hass, fake_panel)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"scan_interval": 5, "enable_commands": False, "code_arm_required": True,
         "partitions": ["A"], "zones": "99"},
    )
    assert result["errors"] == {"zones": "invalid_zones"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"scan_interval": 5, "enable_commands": False, "code_arm_required": True,
         "partitions": ["A"], "zone_1_device_class": "door"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["zones"] == ""
    assert hass.states.get("binary_sensor.amt_4010_zona_01").attributes["device_class"] == "door"
    assert hass.states.get(eid(hass, entry, "partition_a"))
    assert not er.async_get(hass).async_get_entity_id(
        "alarm_control_panel", DOMAIN, f"{entry.entry_id}_partition_b"
    )  # gone from the registry, not left "unavailable"
    assert hass.states.get("alarm_control_panel.amt_4010_partition_b") is None


async def test_zone_names_that_fail_are_not_retried_on_nack(
    hass: HomeAssistant, fake_panel
) -> None:
    fake_panel.behaviour[P.CMD_READ_EEPROM] = "nack:E5"
    entry = await setup_entry(hass, fake_panel)
    assert fake_panel.commands().count(P.CMD_READ_EEPROM) == 1
    assert entry.runtime_data._name_retry is None
    button = hass.states.get(eid(hass, entry, "read_zone_names"))
    assert button.attributes["last_error"] == "rejected"
    # no named zone and no option: no zone entity, but open zones still show
    assert not [e for e in hass.states.async_entity_ids("binary_sensor") if "_zona_" in e]
    fake_panel.status[0] = 0x04
    await refresh(hass, entry)
    assert hass.states.get(eid(hass, entry, "open_zones")).attributes["zone_numbers"] == [3]


async def test_zone_list_change_removes_old_zones(hass: HomeAssistant, fake_panel) -> None:
    entry = await setup_entry(hass, fake_panel, zones="1-3")
    hass.config_entries.async_update_entry(entry, options={**entry.options, "zones": "1"})
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    assert registry.async_get("binary_sensor.amt_4010_zona_01")
    assert registry.async_get("binary_sensor.amt_4010_zona_02") is None
    assert registry.async_get("binary_sensor.amt_4010_zona_03") is None


async def test_refused_password_also_stops_a_pending_name_retry(
    hass: HomeAssistant, fake_panel
) -> None:
    fake_panel.behaviour[P.CMD_READ_EEPROM] = "drop"
    entry = await setup_entry(hass, fake_panel)
    coordinator = entry.runtime_data
    assert coordinator._name_retry is not None  # a lost connection is retried
    del fake_panel.behaviour[P.CMD_READ_EEPROM]
    fake_panel.behaviour[P.CMD_STATUS] = "nack:E1"
    await refresh(hass, entry)
    assert coordinator._name_retry is None
    seen = fake_panel.connections
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=400))
    await hass.async_block_till_done()
    coordinator.async_request_name_read()  # even the button stays quiet
    await hass.async_block_till_done()
    assert fake_panel.connections == seen


async def test_reading_names_again_removes_erased_zones(hass: HomeAssistant, fake_panel) -> None:
    entry = await setup_entry(hass, fake_panel)
    assert hass.states.get("binary_sensor.amt_4010_zona_03")
    del fake_panel.names[3]
    await hass.services.async_call(
        "button", "press", {"entity_id": eid(hass, entry, "read_zone_names")}, blocking=True
    )
    await entry.runtime_data._name_task
    await hass.async_block_till_done()
    assert er.async_get(hass).async_get("binary_sensor.amt_4010_zona_03") is None
    assert hass.states.get("binary_sensor.amt_4010_zona_03") is None
    assert hass.states.get("binary_sensor.amt_4010_zona_01").state == "off"


async def test_backoff_never_passes_60_seconds(hass: HomeAssistant, fake_panel) -> None:
    entry = await setup_entry(hass, fake_panel)
    fake_panel.behaviour[P.CMD_STATUS] = "drop"
    for _ in range(8):
        await refresh(hass, entry)
        assert entry.runtime_data.update_interval.total_seconds() <= 60


async def test_contradiction_is_unknown_on_every_panel(hass: HomeAssistant, fake_panel) -> None:
    entry = await setup_entry(hass, fake_panel)
    fake_panel.status[29] = 0x08  # "armed" with no partition armed
    await refresh(hass, entry)
    for key in ("panel", "partition_a", "partition_b"):
        assert hass.states.get(eid(hass, entry, key)).state == "unknown"


async def test_refused_password_on_a_command_asks_and_stops(
    hass: HomeAssistant, fake_panel
) -> None:
    entry = await setup_entry(hass, fake_panel, enable_commands=True)
    panel = eid(hass, entry, "panel")
    fake_panel.behaviour[P.CMD_ARM] = "nack:E1"
    for _ in range(2):
        with pytest.raises(HomeAssistantError) as info:
            await hass.services.async_call(
                "alarm_control_panel", "alarm_arm_away", {"entity_id": panel, "code": PASSWORD},
                blocking=True,
            )
        assert info.value.translation_key == "password_refused"
    await hass.async_block_till_done()
    assert fake_panel.commands().count(P.CMD_ARM) == 1  # the second never left
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == ["reauth"]
    seen = fake_panel.connections
    await refresh(hass, entry)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=10))
    await hass.async_block_till_done()
    assert fake_panel.connections == seen


async def test_partitions_become_unknown_if_the_panel_is_unpartitioned(
    hass: HomeAssistant, fake_panel
) -> None:
    entry = await setup_entry(hass, fake_panel)
    fake_panel.status[26], fake_panel.status[29] = 0x00, 0x08
    await refresh(hass, entry)
    assert hass.states.get(eid(hass, entry, "panel")).state == "armed_away"
    for key in ("partition_a", "partition_b"):
        assert hass.states.get(eid(hass, entry, key)).state == "unknown"
    fake_panel.status[26] = 0x02  # undocumented: the status is refused
    await refresh(hass, entry)
    assert hass.states.get(eid(hass, entry, "panel_link")).attributes["last_failure"] == "bad_reply"


async def test_refused_password_survives_a_restart(hass: HomeAssistant, fake_panel) -> None:
    """Contra-assinatura, achado 4: a new client must not resend it after reload."""
    entry = await setup_entry(hass, fake_panel)
    fake_panel.behaviour[P.CMD_STATUS] = "nack:E1"
    await refresh(hass, entry)
    await hass.async_block_till_done()
    seen = fake_panel.connections
    for _ in range(2):  # the 04:02 restart, twice
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    assert fake_panel.connections == seen
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flow = hass.config_entries.flow.async_progress_by_handler(DOMAIN)[0]
    del fake_panel.behaviour[P.CMD_STATUS]
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {"password": "2222"})
    assert result["reason"] == "reauth_successful"
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert fake_panel.connections > seen


async def test_alarm_already_active_is_not_a_new_alarm_after_restart(
    hass: HomeAssistant, fake_panel
) -> None:
    """Contra-assinatura, achado 2: one event per alarm, not one per reload."""
    entry = await setup_entry(hass, fake_panel)
    bus = async_capture_events(hass, EVENT_ALARM_TRIGGERED)
    fake_panel.status[29] = 0x02
    await refresh(hass, entry)
    await hass.async_block_till_done()
    for _ in range(3):
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    assert len(bus) == 1
    fake_panel.status[29] = 0x00
    await refresh(hass, entry)
    fake_panel.status[29] = 0x02  # a second, real alarm
    await refresh(hass, entry)
    assert len(bus) == 2


async def test_a_read_with_no_names_keeps_the_zones(hass: HomeAssistant, fake_panel) -> None:
    entry = await setup_entry(hass, fake_panel)
    fake_panel.names.clear()  # e.g. a panel reset: every zone back to "Zona NN"
    await hass.services.async_call(
        "button", "press", {"entity_id": eid(hass, entry, "read_zone_names")}, blocking=True
    )
    await entry.runtime_data._name_task
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.amt_4010_zona_03")
    assert hass.states.get(eid(hass, entry, "read_zone_names")).attributes["last_error"] == "no_names"


async def test_alarm_that_began_while_down_reaches_the_event_entity(
    hass: HomeAssistant, fake_panel
) -> None:
    """Contra-assinatura, rechecagem N1: bus, device trigger and entity agree."""
    entry = await setup_entry(hass, fake_panel)
    event_id = eid(hass, entry, "alarm_event")
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    fake_panel.status[29] = 0x02  # the alarm starts with the integration down
    bus = async_capture_events(hass, EVENT_ALARM_TRIGGERED)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert len(bus) == 1
    assert hass.states.get(event_id).attributes["event_type"] == "alarm_triggered"
    # and a plain reload during the same alarm adds nothing
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert len(bus) == 1
