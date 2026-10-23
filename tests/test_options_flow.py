"""The mapping options flow: map a room, fill by pattern, settings."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tapechart.const import DOMAIN

from .conftest import (
    ENTRY_DATA,
    PROPERTY,
    U205,
    U206,
    U207,
    U208,
    U209,
    U210,
    events_posted,
)


def _esphome_room(
    hass: HomeAssistant, esphome: MockConfigEntry, number: str, prefix: str, mac: str
):
    """A room controller like the hotel's ESPHome devices."""
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=esphome.entry_id,
        identifiers={("esphome", mac)},
        name=f"Room {number}",
    )
    reg = er.async_get(hass)
    ids = {}
    for domain, key, name in (
        ("binary_sensor", "bathroom_pir", "Bathroom PIR"),
        ("climate", "thermostat_climate_controller", "Thermostat Climate Controller"),
        ("sensor", "room_temp", "Room Temp"),
    ):
        entry = reg.async_get_or_create(
            domain,
            "esphome",
            f"{mac}-{domain}-{key}",
            suggested_object_id=f"{prefix}_{key}",
            device_id=device.id,
            original_name=name,
        )
        hass.states.async_set(
            entry.entity_id, "on" if domain == "binary_sensor" else "21"
        )
        ids[domain] = entry.entity_id
    return device, ids


async def test_map_room_with_device(
    hass: HomeAssistant, setup_entry, aioclient_mock
) -> None:
    """Room → device (suggested by the number) → one entity per role."""
    esphome = MockConfigEntry(domain="esphome")
    esphome.add_to_hass(hass)
    device, ids = _esphome_room(hass, esphome, "205", "room_205", "AA:01")
    hass.states.async_set("script.welcome_205", "off")
    entry = await setup_entry()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["map_room", "fill_pattern", "settings"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "map_room"}
    )
    assert result["step_id"] == "map_room"
    labels = [
        o["label"] for o in result["data_schema"].schema["room"].config["options"]
    ]
    assert labels[0] == "205 · Double"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"room": U205}
    )
    assert result["step_id"] == "map_device"
    device_key = next(iter(result["data_schema"].schema))
    assert device_key.description == {"suggested_value": device.id}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"device": device.id}
    )
    assert result["step_id"] == "map_entities"
    schema = {str(k): v for k, v in result["data_schema"].schema.items()}
    assert schema["presence"].config["include_entities"] == [ids["binary_sensor"]]
    assert schema["climate"].config["domain"] == ["climate"]
    assert "online_from_device" in schema
    before = len(events_posted(aioclient_mock))
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "presence": ids["binary_sensor"],
            "climate": ids["climate"],
            "scenario_welcome": "script.welcome_205",
            "online_from_device": True,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["mapping"][U205] == {
        "presence": ids["binary_sensor"],
        "climate": ids["climate"],
        "scenario_welcome": "script.welcome_205",
        "online": f"device:{device.id}",
    }
    assert entry.options["devices"] == {U205: device.id}
    assert entry.options["presence_hold_minutes"] == 5
    await hass.async_block_till_done()
    sent = events_posted(aioclient_mock)[before:]
    mapping = [e for e in sent if e["type"] == "mapping"]
    assert len(mapping) == 1 and mapping[0]["unit_id"] == U205
    assert {e["role"] for e in sent if e["type"] == "signal"} == {
        "presence",
        "climate",
        "online",
    }


async def test_clear_a_room(hass: HomeAssistant, setup_entry) -> None:
    """Saving a room with nothing chosen removes its mapping."""
    entry = await setup_entry(
        options={"mapping": {U205: {"presence": "binary_sensor.x"}}}
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "map_room"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"room": U205}
    )
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    schema = {str(k): k for k in result["data_schema"].schema}
    assert schema["presence"].description == {"suggested_value": "binary_sensor.x"}
    assert "online_from_device" not in schema
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["mapping"] == {}


async def test_fill_by_pattern(hass: HomeAssistant, setup_entry) -> None:
    """Devices are found by number; ids that differ (room 108 style) still match."""
    esphome = MockConfigEntry(domain="esphome")
    esphome.add_to_hass(hass)
    d205, ids205 = _esphome_room(hass, esphome, "205", "room_205", "AA:05")
    d206, ids206 = _esphome_room(hass, esphome, "206", "engo_test", "AA:06")
    # Room 207: no device, but entity ids follow the number.
    hass.states.async_set("binary_sensor.room_207_bathroom_pir", "off")
    entry = await setup_entry(
        options={
            "mapping": {
                U205: {
                    "presence": ids205["binary_sensor"],
                    "climate": ids205["climate"],
                    "temperature": ids205["sensor"],
                    "online": f"device:{d205.id}",
                }
            },
            "devices": {U205: d205.id},
        }
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "fill_pattern"}
    )
    assert result["step_id"] == "fill_pattern"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"source": U205}
    )
    assert result["step_id"] == "fill_confirm"
    assert result["description_placeholders"]["rooms"] == "2"
    text = result["description_placeholders"]["result"]
    assert "engo_test_bathroom_pir" in text
    assert "room_207_bathroom_pir" in text
    assert "✗ climate, temperature" in text
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    mapping = entry.options["mapping"]
    assert mapping[U206] == {
        "presence": ids206["binary_sensor"],
        "climate": ids206["climate"],
        "temperature": ids206["sensor"],
        "online": f"device:{d206.id}",
    }
    assert mapping[U207] == {"presence": "binary_sensor.room_207_bathroom_pir"}
    for unit in (U208, U209, U210):
        assert unit not in mapping
    assert entry.options["devices"] == {U205: d205.id, U206: d206.id}


async def test_fill_nothing_found_and_aborts(hass: HomeAssistant, setup_entry) -> None:
    """No source room → abort; nothing to propose → error."""
    entry = await setup_entry()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "fill_pattern"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_mapped_rooms"

    hass.config_entries.async_update_entry(
        entry, options={"mapping": {U205: {"presence": "binary_sensor.lobby"}}}
    )
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "fill_pattern"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"source": U205}
    )
    assert result["errors"] == {"base": "nothing_found"}


async def test_settings(hass: HomeAssistant, setup_entry) -> None:
    """Settings are stored and applied to the forwarder."""
    entry = await setup_entry()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "presence_hold_minutes": 2,
            "temperature_min_delta": 1.0,
            "temperature_min_interval": 30,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    forwarder = entry.runtime_data.forwarder
    assert forwarder.presence_hold == 120
    assert forwarder.temp_delta == 1.0
    assert forwarder.temp_interval == 30


async def test_not_loaded(hass: HomeAssistant) -> None:
    """The options need the rooms, so a stopped entry aborts."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=PROPERTY, data=dict(ENTRY_DATA))
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_loaded"
    assert U206
