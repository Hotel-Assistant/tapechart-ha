"""Regression tests for the 2026-10-24 review (M1-M4, L1-L11)."""

from __future__ import annotations

import copy
from datetime import timedelta
import json
from typing import Any
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, STATE_UNKNOWN
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.tapechart.const import DOMAIN

from .conftest import (
    ENTRY_DATA,
    EVENTS_URL,
    PROPERTY,
    ROOMS_URL,
    U205,
    U206,
    U207,
    WEBHOOK_ID,
    events_posted,
    room_device,
    sign,
)

PIR = "binary_sensor.room_205_bathroom_pir"


async def test_m2_devices_of_rooms_archived_while_down_are_removed(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    ha_urls: None,
    rooms_answer,
) -> None:
    """After the first /rooms, devices of rooms the PMS no longer lists go."""
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=PROPERTY, title="Villa Bozhe", data=dict(ENTRY_DATA)
    )
    entry.add_to_hass(hass)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "archived-unit")},
        name="old",
    )
    aioclient_mock.get(ROOMS_URL, json=rooms_answer)
    aioclient_mock.post(EVENTS_URL, json={"ok": True})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert room_device(hass, "archived-unit") is None
    assert room_device(hass, U205) is not None


async def test_m3_online_from_a_device_without_binary_sensors(
    hass: HomeAssistant, setup_entry, aioclient_mock: AiohttpClientMocker
) -> None:
    """Any member entity names the derived `online` role in the mapping event."""
    other = MockConfigEntry(domain="esphome")
    other.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("esphome", "trv")}, name="TRV 205"
    )
    er.async_get(hass).async_get_or_create(
        "climate",
        "esphome",
        "trv-climate",
        suggested_object_id="trv_205",
        device_id=device.id,
    )
    hass.states.async_set("climate.trv_205", "heat")
    await setup_entry(options={"mapping": {U205: {"online": f"device:{device.id}"}}})
    mapping = next(e for e in events_posted(aioclient_mock) if e["type"] == "mapping")
    assert mapping["roles"]["online"] == {
        "entity_id": "climate.trv_205",
        "name": "TRV 205 (availability)",
    }


async def test_m4_current_states_only_after_start(
    hass: HomeAssistant, setup_entry, aioclient_mock: AiohttpClientMocker
) -> None:
    """While HA starts nothing goes out as `unavailable`; missing entities wait."""
    hass.set_state(CoreState.starting)
    hass.states.async_set("binary_sensor.room_205_door", "off")
    await setup_entry(
        options={
            "mapping": {U205: {"presence": PIR, "door": "binary_sensor.room_205_door"}}
        }
    )
    assert not [e for e in events_posted(aioclient_mock) if e["type"] == "signal"]
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    signals = [e for e in events_posted(aioclient_mock) if e["type"] == "signal"]
    assert [(e["role"], e["state"]) for e in signals] == [("door", "off")]
    # The presence sensor appears later, already off: reported at once, not held.
    hass.states.async_set(PIR, "off")
    await hass.async_block_till_done()
    signals = [e for e in events_posted(aioclient_mock) if e["type"] == "signal"]
    assert ("presence", "off") in [(e["role"], e["state"]) for e in signals]


async def test_m4_paced_under_the_pms_limit(
    hass: HomeAssistant,
    setup_entry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """At most 500 events go out per minute."""
    entry = await setup_entry()
    aioclient_mock.clear_requests()
    aioclient_mock.post(EVENTS_URL, json={"ok": True})
    entry.runtime_data.outbox.async_add(
        [{"id": f"e{i}", "type": "signal"} for i in range(1200)]
    )
    await hass.async_block_till_done()
    assert 0 < len(events_posted(aioclient_mock)) <= 500
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(events_posted(aioclient_mock)) <= 1000
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(events_posted(aioclient_mock)) == 1200


async def test_l2_diagnostics_redact_cloudhook(
    hass: HomeAssistant, setup_entry, hass_client
) -> None:
    """The cloudhook URL and the connection id are redacted too."""
    entry = await setup_entry()
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "cloudhook_url": "https://hooks.nabu.casa/secret"}
    )
    text = json.dumps(await get_diagnostics_for_config_entry(hass, hass_client, entry))
    assert "hooks.nabu.casa" not in text
    assert entry.data["connection_id"] not in text


async def test_l3_oversized_body(
    hass: HomeAssistant, setup_entry, hass_client_no_auth
) -> None:
    """A body over 256 KB is refused before it is read."""
    await setup_entry()
    client = await hass_client_no_auth()
    body = b"{" + b" " * (300 * 1024) + b"}"
    resp = await client.post(
        f"/api/webhook/{WEBHOOK_ID}", data=body, headers=sign(body)
    )
    assert resp.status == 413


async def test_l5_command_saved_at_once(
    hass: HomeAssistant, setup_entry, hass_client_no_auth, hass_storage: dict[str, Any]
) -> None:
    """A command that ran is on disk before the answer goes back."""
    async_mock_service(hass, "script", "turn_on")
    hass.states.async_set("script.w", "off")
    entry = await setup_entry(
        options={"mapping": {U205: {"scenario_welcome": "script.w"}}}
    )
    client = await hass_client_no_auth()
    body = json.dumps(
        {
            "type": "command",
            "command_id": "cmd-1",
            "unit_id": U205,
            "scenario": "welcome",
        }
    ).encode()
    resp = await client.post(
        f"/api/webhook/{WEBHOOK_ID}", data=body, headers=sign(body)
    )
    assert await resp.json() == {"ok": True}
    stored = hass_storage[f"{DOMAIN}.seen.{entry.entry_id}"]["data"]["commands"]
    assert "cmd-1" in stored


async def test_l6_naive_timestamp(
    hass: HomeAssistant, setup_entry, rooms_answer
) -> None:
    """A timestamp without a time zone reads unknown instead of failing."""
    rooms = copy.deepcopy(rooms_answer)
    rooms["rooms"][0]["stay"]["check_in_at"] = "2026-10-22T14:00:00"
    await setup_entry(rooms=rooms)
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{U205}_check_in"
    )
    assert hass.states.get(entity_id).state == STATE_UNKNOWN


async def test_l7_turnover_day_check_out(
    hass: HomeAssistant, setup_entry, hass_client_no_auth, rooms_answer
) -> None:
    """departing -> arriving (the next guest arrives today) fires checked_out."""
    rooms = copy.deepcopy(rooms_answer)
    rooms["rooms"][2]["status"] = "departing"
    await setup_entry(rooms=rooms)
    client = await hass_client_no_auth()
    room = copy.deepcopy(rooms_answer["rooms"][2])
    room["version"] = 500
    body = json.dumps({"type": "room_state", "room": room}).encode()
    await client.post(f"/api/webhook/{WEBHOOK_ID}", data=body, headers=sign(body))
    await hass.async_block_till_done()
    event_id = er.async_get(hass).async_get_entity_id("event", DOMAIN, f"{U207}_stay")
    assert hass.states.get(event_id).attributes["event_type"] == "checked_out"


async def test_l8_setup_retry_after_a_failure(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    ha_urls: None,
    rooms_answer,
) -> None:
    """A setup that fails after registering the webhook can be retried."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=PROPERTY, data=dict(ENTRY_DATA))
    entry.add_to_hass(hass)
    aioclient_mock.get(ROOMS_URL, json=rooms_answer)
    aioclient_mock.post(EVENTS_URL, json={"ok": True})
    with patch(
        "custom_components.tapechart.hub.async_webhook_urls", side_effect=RuntimeError
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    await hass.config_entries.async_unload(entry.entry_id)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


async def test_l9_any_entity_beside_the_device(
    hass: HomeAssistant, setup_entry
) -> None:
    """With "only this device" off, the pickers are not limited to the device."""
    other = MockConfigEntry(domain="esphome")
    other.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id,
        identifiers={("esphome", "r206")},
        name="Room 206",
    )
    er.async_get(hass).async_get_or_create(
        "binary_sensor", "esphome", "r206-pir", device_id=device.id
    )
    entry = await setup_entry()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "map_room"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"room": U206}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"device": device.id, "only_device": False}
    )
    schema = {str(k): v for k, v in result["data_schema"].schema.items()}
    assert "include_entities" not in schema["presence"].config
    assert "online_from_device" in schema


async def test_l11_missing_endpoint_stops_and_asks_to_repair(
    hass: HomeAssistant, setup_entry, aioclient_mock: AiohttpClientMocker
) -> None:
    """403/404 on /events: no endless retries; a repair + reauth instead."""
    aioclient_mock.post(EVENTS_URL, status=404, text="not here")
    entry = await setup_entry(mock_events=False)
    await hass.async_block_till_done()
    assert entry.runtime_data.outbox.paused
    assert any(
        f["context"]["source"] == "reauth"
        for f in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    )
