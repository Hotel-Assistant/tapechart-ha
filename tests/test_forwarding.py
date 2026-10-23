"""Forwarding mapped entities to the PMS, and the persistent outbox."""

from __future__ import annotations

from datetime import timedelta
import json
from typing import Any
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.tapechart.const import DOMAIN

from .conftest import EVENTS_URL, U205, U206, events_posted

PIR = "binary_sensor.room_205_bathroom_pir"
CLIMATE = "climate.room_205_thermostat_climate_controller"
TEMP = "sensor.room_205_room_temp"
DOOR = "binary_sensor.room_205_door"
ALARM = "binary_sensor.room_205_leak"


def _signals(
    aioclient_mock: AiohttpClientMocker, role: str | None = None
) -> list[dict[str, Any]]:
    return [
        e
        for e in events_posted(aioclient_mock)
        if e["type"] == "signal" and (role is None or e["role"] == role)
    ]


def _states(hass: HomeAssistant) -> None:
    hass.states.async_set(PIR, "off", {"friendly_name": "Room 205 Bathroom PIR"})
    hass.states.async_set(
        CLIMATE,
        "heat",
        {
            "current_temperature": 21.0,
            "temperature": 22.0,
            "friendly_name": "Thermostat",
        },
    )
    hass.states.async_set(TEMP, "21.0")
    hass.states.async_set(DOOR, "off")
    hass.states.async_set(ALARM, "off", {"device_class": "moisture"})
    hass.states.async_set("light.room_205_main_light", "on")


OPTIONS = {
    "mapping": {
        U205: {
            "presence": PIR,
            "climate": CLIMATE,
            "temperature": TEMP,
            "door": DOOR,
            "alarm": ALARM,
            "scenario_welcome": "script.welcome",
        }
    },
    "presence_hold_minutes": 5,
    "temperature_min_delta": 0.5,
    "temperature_min_interval": 60,
}


async def test_startup_sends_health_mapping_and_current_states(
    hass: HomeAssistant, setup_entry, aioclient_mock: AiohttpClientMocker
) -> None:
    """Startup: health, the room's full mapping, one signal per mapped entity."""
    _states(hass)
    await setup_entry(options=OPTIONS)
    events = events_posted(aioclient_mock)
    assert events[0]["type"] == "health"
    assert events[0]["webhook_urls"][0].endswith("/api/webhook/hook123")
    mapping = [e for e in events if e["type"] == "mapping"]
    assert len(mapping) == 1
    assert mapping[0]["unit_id"] == U205
    assert mapping[0]["roles"]["presence"] == {
        "entity_id": PIR,
        "name": "Room 205 Bathroom PIR",
    }
    assert set(mapping[0]["roles"]) == {
        "presence",
        "climate",
        "temperature",
        "door",
        "alarm",
        "scenario_welcome",
    }
    signals = {e["role"]: e for e in _signals(aioclient_mock)}
    assert set(signals) == {"presence", "climate", "temperature", "door", "alarm"}
    assert signals["climate"]["state"] == "heat"
    assert signals["climate"]["attributes"] == {
        "current_temperature": 21.0,
        "temperature": 22.0,
    }
    assert signals["temperature"]["state"] == "21"
    assert signals["alarm"]["attributes"] == {"device_class": "moisture"}
    assert signals["door"]["attributes"] == {}
    # Nothing unmapped ever leaves HA.
    assert all("light." not in str(e) for e in events)
    # Every request is signed with the logical path.
    for _method, url, _data, headers in aioclient_mock.mock_calls:
        if str(url) == EVENTS_URL:
            assert headers["X-TapeChart-Connection"]
            assert headers["X-TapeChart-Id"]


async def test_presence_hold(
    hass: HomeAssistant,
    setup_entry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """On goes out at once; off only after the hold; an on inside the hold voids it."""
    _states(hass)
    await setup_entry(options=OPTIONS)
    start = len(_signals(aioclient_mock, "presence"))

    hass.states.async_set(PIR, "on")
    await hass.async_block_till_done()
    assert [e["state"] for e in _signals(aioclient_mock, "presence")[start:]] == ["on"]

    hass.states.async_set(PIR, "off")
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=3))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    hass.states.async_set(PIR, "on")  # back before the hold ran out
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=10))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert [e["state"] for e in _signals(aioclient_mock, "presence")[start:]] == ["on"]

    hass.states.async_set(PIR, "off")
    await hass.async_block_till_done()
    off_at = hass.states.get(PIR).last_updated
    freezer.tick(timedelta(minutes=4))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert [e["state"] for e in _signals(aioclient_mock, "presence")[start:]] == ["on"]
    freezer.tick(timedelta(minutes=2))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    sent = _signals(aioclient_mock, "presence")[start:]
    assert [e["state"] for e in sent] == ["on", "off"]
    # The off keeps the time it really happened.
    assert sent[-1]["changed_at"] == off_at.isoformat()


async def test_temperature_debounce(
    hass: HomeAssistant,
    setup_entry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Small changes are dropped, fast ones are combined, unavailable is immediate."""
    _states(hass)
    await setup_entry(options=OPTIONS)
    freezer.tick(timedelta(seconds=120))

    def temps() -> list[str]:
        return [e["state"] for e in _signals(aioclient_mock, "temperature")]

    assert temps() == ["21"]
    hass.states.async_set(TEMP, "21.3")  # below the 0.5 delta
    await hass.async_block_till_done()
    assert temps() == ["21"]
    hass.states.async_set(TEMP, "22.0")  # interval passed: at once
    await hass.async_block_till_done()
    assert temps() == ["21", "22"]
    freezer.tick(timedelta(seconds=10))
    hass.states.async_set(TEMP, "23.0")  # inside the interval: trailing send
    await hass.async_block_till_done()
    hass.states.async_set(TEMP, "23.5")
    await hass.async_block_till_done()
    assert temps() == ["21", "22"]
    freezer.tick(timedelta(seconds=55))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert temps() == ["21", "22", "23.5"]
    hass.states.async_set(TEMP, "unavailable")
    await hass.async_block_till_done()
    assert temps()[-1] == "unavailable"


async def test_climate_mode_and_setpoint_immediate(
    hass: HomeAssistant,
    setup_entry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Mode and setpoint changes go at once; the current temperature is debounced."""
    _states(hass)
    await setup_entry(options=OPTIONS)

    def climate() -> list[tuple[str, Any, Any]]:
        return [
            (
                e["state"],
                e["attributes"]["temperature"],
                e["attributes"]["current_temperature"],
            )
            for e in _signals(aioclient_mock, "climate")
        ]

    hass.states.async_set(
        CLIMATE, "heat", {"current_temperature": 21.2, "temperature": 22.0}
    )
    await hass.async_block_till_done()
    assert climate() == [("heat", 22.0, 21.0)]
    hass.states.async_set(
        CLIMATE, "heat", {"current_temperature": 21.2, "temperature": 24.0}
    )
    await hass.async_block_till_done()
    hass.states.async_set(
        CLIMATE, "off", {"current_temperature": 21.2, "temperature": 24.0}
    )
    await hass.async_block_till_done()
    assert climate()[1:] == [("heat", 24.0, 21.2), ("off", 24.0, 21.2)]


async def test_outbox_survives_restart_and_drains(
    hass: HomeAssistant,
    setup_entry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    """A PMS outage keeps events on disk; they go out in order when it is back."""
    _states(hass)
    aioclient_mock.post(EVENTS_URL, status=503, json={"ok": False, "error": "timeout"})
    entry = await setup_entry(options=OPTIONS, mock_events=False)
    hass.states.async_set(DOOR, "on")
    await hass.async_block_till_done()
    hub = entry.runtime_data
    assert hub.outbox.events
    pending = [e["id"] for e in hub.outbox.events if e["type"] == "signal"]
    assert len(pending) == 6  # five at startup + the door

    assert await hass.config_entries.async_unload(entry.entry_id)
    stored = hass_storage[f"{DOMAIN}.outbox.{entry.entry_id}"]["data"]["events"]
    assert [e["id"] for e in stored if e["type"] == "signal"] == pending

    aioclient_mock.clear_requests()
    aioclient_mock.get(
        "https://pms.example.com/api/home-assistant/rooms",
        json={"ok": True, "rooms": list(hub.rooms.values())},
    )
    aioclient_mock.post(EVENTS_URL, json={"ok": True, "accepted": 1})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # The old signals go out first, in order; the old health/mapping were
    # replaced by the fresh ones (replace-all semantics).
    ids = [e["id"] for e in events_posted(aioclient_mock) if e["type"] == "signal"]
    assert ids[: len(pending)] == pending
    assert not entry.runtime_data.outbox.events


async def test_outbox_backoff_and_batches(
    hass: HomeAssistant,
    setup_entry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Retries back off; batches hold at most 100 events."""
    aioclient_mock.post(
        EVENTS_URL, status=429, json={"ok": False, "error": "rate_limited"}
    )
    entry = await setup_entry(mock_events=False)
    hub = entry.runtime_data
    hub.outbox.async_add([{"id": str(i), "type": "health"} for i in range(1)])
    many = [
        {
            "id": f"s{i}",
            "type": "signal",
            "unit_id": U205,
            "role": "door",
            "state": "on",
            "attributes": {},
            "changed_at": "2026-10-23T12:00:00+00:00",
        }
        for i in range(250)
    ]
    hub.outbox.async_add(many)
    await hass.async_block_till_done()
    calls = len(aioclient_mock.mock_calls)
    await hass.async_block_till_done()
    assert len(aioclient_mock.mock_calls) == calls  # waiting for the backoff

    aioclient_mock.clear_requests()
    aioclient_mock.post(EVENTS_URL, json={"ok": True, "accepted": 100})
    freezer.tick(timedelta(seconds=20))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    sizes = [len(json.loads(c[2])["events"]) for c in aioclient_mock.mock_calls]
    assert sizes and max(sizes) <= 100
    assert sum(sizes) == len(many) + 1
    assert not hub.outbox.events


async def test_rejected_event_is_isolated(
    hass: HomeAssistant, setup_entry, aioclient_mock: AiohttpClientMocker
) -> None:
    """The PMS refuses a whole batch for one bad event: only that one is dropped."""

    async def _answer(method, url, data):
        from pytest_homeassistant_custom_component.test_util.aiohttp import (
            AiohttpClientMockResponse,
        )

        events = json.loads(data)["events"]
        if any(e.get("state") == "bad" for e in events):
            return AiohttpClientMockResponse(
                "post", url, status=400, json={"ok": False, "error": "bad_request"}
            )
        return AiohttpClientMockResponse(
            "post", url, json={"ok": True, "accepted": len(events)}
        )

    aioclient_mock.post(EVENTS_URL, side_effect=_answer)
    entry = await setup_entry(mock_events=False)
    hub = entry.runtime_data
    aioclient_mock.clear_requests()
    aioclient_mock.post(EVENTS_URL, side_effect=_answer)
    batch = [
        {"id": f"e{i}", "type": "signal", "state": "bad" if i == 2 else "on"}
        for i in range(5)
    ]
    hub.outbox.async_add(batch)
    await hass.async_block_till_done()
    accepted = [
        e["id"]
        for c in aioclient_mock.mock_calls
        for e in json.loads(c[2])["events"]
        if e.get("state") != "bad"
    ]
    assert sorted(set(accepted)) == ["e0", "e1", "e3", "e4"]
    assert not hub.outbox.events


async def test_auth_failure_pauses(
    hass: HomeAssistant, setup_entry, aioclient_mock: AiohttpClientMocker
) -> None:
    """A 401 on /events stops sending and starts a reauth; events are kept."""
    aioclient_mock.post(
        EVENTS_URL, status=401, json={"ok": False, "error": "unauthorized"}
    )
    entry = await setup_entry(mock_events=False)
    await hass.async_block_till_done()
    hub = entry.runtime_data
    assert hub.disconnected
    assert hub.outbox.events
    assert any(
        f["context"]["source"] == "reauth"
        for f in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    )


async def test_mapping_change_sends_mapping_and_state(
    hass: HomeAssistant, setup_entry, aioclient_mock: AiohttpClientMocker
) -> None:
    """Options change: a mapping event for the touched room, a signal for the new entity."""
    _states(hass)
    hass.states.async_set("binary_sensor.room_206_bathroom_pir", "on")
    entry = await setup_entry(options=OPTIONS)
    before = len(events_posted(aioclient_mock))
    new_options = {
        **OPTIONS,
        "mapping": {
            **OPTIONS["mapping"],
            U206: {"presence": "binary_sensor.room_206_bathroom_pir"},
        },
    }
    hass.config_entries.async_update_entry(entry, options=new_options)
    await hass.async_block_till_done()
    new = events_posted(aioclient_mock)[before:]
    assert [e["type"] for e in new] == ["mapping", "signal"]
    assert new[0]["unit_id"] == U206
    assert new[1]["state"] == "on"

    before = len(events_posted(aioclient_mock))
    hass.config_entries.async_update_entry(
        entry, options={**new_options, "mapping": {U205: OPTIONS["mapping"][U205]}}
    )
    await hass.async_block_till_done()
    new = events_posted(aioclient_mock)[before:]
    assert new == [
        {"id": new[0]["id"], "type": "mapping", "unit_id": U206, "roles": {}}
    ]
    hass.states.async_set("binary_sensor.room_206_bathroom_pir", "off")
    await hass.async_block_till_done()
    assert len(events_posted(aioclient_mock)) == before + 1  # no longer forwarded


async def test_online_from_device_availability(
    hass: HomeAssistant, setup_entry, aioclient_mock: AiohttpClientMocker
) -> None:
    """`online` derived from a device: off when every entity is unavailable."""
    other = MockConfigEntry(domain="esphome")
    other.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id,
        identifiers={("esphome", "room205")},
        name="Room 205",
    )
    reg = er.async_get(hass)
    for object_id in ("bathroom_pir", "thermostat_control_input"):
        reg.async_get_or_create(
            "binary_sensor",
            "esphome",
            f"mac-{object_id}",
            suggested_object_id=f"room_205_{object_id}",
            device_id=device.id,
        )
    hass.states.async_set("binary_sensor.room_205_bathroom_pir", "off")
    hass.states.async_set("binary_sensor.room_205_thermostat_control_input", "on")
    await setup_entry(options={"mapping": {U205: {"online": f"device:{device.id}"}}})

    online = _signals(aioclient_mock, "online")
    assert [e["state"] for e in online] == ["on"]
    mapping = next(e for e in events_posted(aioclient_mock) if e["type"] == "mapping")
    assert (
        mapping["roles"]["online"]["entity_id"] == "binary_sensor.room_205_bathroom_pir"
    )
    assert mapping["roles"]["online"]["name"] == "Room 205 (availability)"

    hass.states.async_set("binary_sensor.room_205_bathroom_pir", "unavailable")
    await hass.async_block_till_done()
    assert [e["state"] for e in _signals(aioclient_mock, "online")] == ["on"]
    hass.states.async_set(
        "binary_sensor.room_205_thermostat_control_input", "unavailable"
    )
    await hass.async_block_till_done()
    assert [e["state"] for e in _signals(aioclient_mock, "online")] == ["on", "off"]
    hass.states.async_set("binary_sensor.room_205_bathroom_pir", "off")
    await hass.async_block_till_done()
    assert [e["state"] for e in _signals(aioclient_mock, "online")] == [
        "on",
        "off",
        "on",
    ]


async def test_invalid_mapping_entries_not_sent(
    hass: HomeAssistant, setup_entry, aioclient_mock: AiohttpClientMocker
) -> None:
    """A role mapped to a domain it does not allow never reaches the wire (§9 H1)."""
    hass.states.async_set("light.x", "on")
    await setup_entry(options={"mapping": {U205: {"door": "light.x", "presence": PIR}}})
    mapping = next(e for e in events_posted(aioclient_mock) if e["type"] == "mapping")
    assert set(mapping["roles"]) == {"presence"}


async def test_unrelated_entities_do_not_reach_the_pms(
    hass: HomeAssistant, setup_entry, aioclient_mock: AiohttpClientMocker
) -> None:
    """Changes of unmapped entities send nothing."""
    _states(hass)
    await setup_entry(options=OPTIONS)
    before = len(aioclient_mock.mock_calls)
    with patch("custom_components.tapechart.outbox.Outbox.async_add") as add:
        hass.states.async_set("light.room_205_main_light", "off")
        await hass.async_block_till_done()
        add.assert_not_called()
    assert len(aioclient_mock.mock_calls) == before
    assert U206  # room 206 has no mapping at all
