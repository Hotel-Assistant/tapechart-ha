"""Room devices and entities from room_state (contract §7 + §9 H4/M2)."""

from __future__ import annotations

import copy
import json
from typing import Any

from homeassistant.components.diagnostics import REDACTED
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.tapechart.const import DOMAIN
from custom_components.tapechart.sensor import TapeChartStatusSensor

from .conftest import (
    U205,
    U206,
    U207,
    U208,
    U209,
    U210,
    UNPAIR_URL,
    WEBHOOK_ID,
    room_device,
    sign,
)


def _eid(hass: HomeAssistant, platform: str, unit: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        platform, DOMAIN, f"{unit}_{key}"
    )
    assert entity_id, (platform, unit, key)
    return entity_id


def _state(hass: HomeAssistant, platform: str, unit: str, key: str):
    return hass.states.get(_eid(hass, platform, unit, key))


async def test_devices_and_entities(hass: HomeAssistant, setup_entry) -> None:
    """One device per room, named "<property> · <number>", with every entity."""
    entry = await setup_entry()
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert sorted(d.name for d in devices) == [
        f"Villa Bozhe · {n}" for n in ("205", "206", "207", "208", "209", "210")
    ]
    device = room_device(hass, U205)
    assert device.manufacturer == "Tape Chart"
    assert device.model == "Double"
    assert device.suggested_area is None

    entities = er.async_entries_for_device(er.async_get(hass), device.id)
    assert sorted(e.unique_id.removeprefix(f"{U205}_") for e in entities) == sorted(
        [
            "status",
            "guest_checked_in",
            "guest_language",
            "adults",
            "children",
            "check_in",
            "check_out",
            "nights",
            "nights_left",
            "next_arrival",
            "stay",
        ]
    )
    # §9 H4 / M2: no reservation number and no guest-name sensor.
    reg = er.async_get(hass)
    assert (
        reg.async_get_entity_id("sensor", DOMAIN, f"{U205}_reservation_number") is None
    )
    assert reg.async_get_entity_id("sensor", DOMAIN, f"{U205}_guest") is None
    assert _eid(hass, "sensor", U205, "status") == "sensor.villa_bozhe_205_status"


async def test_occupied_room(hass: HomeAssistant, setup_entry) -> None:
    """Values of an occupied room."""
    await setup_entry()
    status = _state(hass, "sensor", U205, "status")
    assert status.state == "occupied"
    assert status.attributes["guest"] == "Ivan Petrov"
    assert status.attributes["companions"] == ["Maria Petrova"]
    assert status.attributes["business_date"] == "2026-10-23"
    assert status.attributes["options"] == [
        "vacant",
        "arriving",
        "occupied",
        "departing",
        "departed",
        "out_of_service",
    ]
    assert _state(hass, "binary_sensor", U205, "guest_checked_in").state == "on"
    assert _state(hass, "sensor", U205, "guest_language").state == "bg"
    assert _state(hass, "sensor", U205, "adults").state == "2"
    children = _state(hass, "sensor", U205, "children")
    assert children.state == "1"
    assert children.attributes["ages"] == [7]
    assert _state(hass, "sensor", U205, "check_in").state == "2026-10-22T11:00:00+00:00"
    assert (
        _state(hass, "sensor", U205, "check_out").state == "2026-10-26T10:00:00+00:00"
    )
    assert _state(hass, "sensor", U205, "nights").state == "4"
    assert _state(hass, "sensor", U205, "nights_left").state == "3"
    nxt = _state(hass, "sensor", U205, "next_arrival")
    assert nxt.state == "2026-10-27T12:00:00+00:00"
    assert "reservation_number" not in nxt.attributes


async def test_every_status(hass: HomeAssistant, setup_entry) -> None:
    """All six statuses; rooms without a stay read unknown."""
    await setup_entry()
    expected = {
        U205: "occupied",
        U206: "vacant",
        U207: "arriving",
        U208: "departing",
        U209: "departed",
        U210: "out_of_service",
    }
    for unit, status in expected.items():
        assert _state(hass, "sensor", unit, "status").state == status
    assert _state(hass, "binary_sensor", U207, "guest_checked_in").state == "off"
    assert _state(hass, "sensor", U207, "guest_language").state == "en"
    vacant = _state(hass, "sensor", U206, "status")
    assert "guest" not in vacant.attributes
    assert "companions" not in vacant.attributes
    for key in ("check_in", "next_arrival"):
        assert _state(hass, "sensor", U206, key).state == STATE_UNKNOWN
    for key in ("adults", "children", "nights", "nights_left"):
        assert _state(hass, "sensor", U206, key).state == "0"
    assert _state(hass, "sensor", U206, "guest_language").state == "none"


async def test_names_off(hass: HomeAssistant, setup_entry, rooms_answer) -> None:
    """With "Send guest names" off the PMS sends guest null and no companions."""
    rooms = copy.deepcopy(rooms_answer)
    rooms["rooms"][0]["stay"]["guest"] = None
    rooms["rooms"][0]["stay"]["companions"] = []
    await setup_entry(rooms=rooms)
    status = _state(hass, "sensor", U205, "status")
    assert status.state == "occupied"
    assert "guest" not in status.attributes
    assert "companions" not in status.attributes
    assert _state(hass, "sensor", U205, "guest_language").state == STATE_UNKNOWN
    assert _state(hass, "sensor", U205, "adults").state == "2"


def test_names_are_never_recorded() -> None:
    """Guest names are excluded from the recorder."""
    assert {"guest", "companions"} <= TapeChartStatusSensor._unrecorded_attributes


async def _push(client, room: dict[str, Any]) -> None:
    body = json.dumps({"type": "room_state", "room": room}).encode()
    resp = await client.post(
        f"/api/webhook/{WEBHOOK_ID}", data=body, headers=sign(body)
    )
    assert resp.status == 200


async def test_stay_events(
    hass: HomeAssistant, setup_entry, hass_client_no_auth, rooms_answer
) -> None:
    """Named events fire; a status transition fires checked_in / checked_out."""
    await setup_entry()
    client = await hass_client_no_auth()
    event_id = _eid(hass, "event", U207, "stay")
    assert hass.states.get(event_id).state == STATE_UNKNOWN

    room = copy.deepcopy(rooms_answer["rooms"][2])
    room.update(version=100, status="occupied")
    room["stay"]["checked_in"] = True
    await _push(client, room)
    await hass.async_block_till_done()
    state = hass.states.get(event_id)
    assert state.attributes["event_type"] == "checked_in"
    assert state.attributes["event_types"] == [
        "checked_in",
        "checked_out",
        "moved_in",
        "moved_out",
        "stay_changed",
        "cancelled",
    ]

    room.update(version=101, events=["stay_changed", "bogus"])
    await _push(client, room)
    await hass.async_block_till_done()
    assert hass.states.get(event_id).attributes["event_type"] == "stay_changed"

    room.update(version=102, status="departed", stay=None, events=[])
    await _push(client, room)
    await hass.async_block_till_done()
    assert hass.states.get(event_id).attributes["event_type"] == "checked_out"
    assert _state(hass, "sensor", U207, "status").state == "departed"
    assert _state(hass, "sensor", U207, "adults").state == "0"


async def test_diagnostics_redact(
    hass: HomeAssistant, setup_entry, hass_client
) -> None:
    """No secret, no webhook id, no guest names in diagnostics."""
    entry = await setup_entry()
    diag = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    text = json.dumps(diag)
    assert "Ivan" not in text and "Maria" not in text
    assert entry.data["secret"] not in text
    assert WEBHOOK_ID not in text
    assert diag["entry"]["data"]["secret"] == REDACTED
    assert len(diag["rooms"]) == 6


async def test_unload_and_remove(
    hass: HomeAssistant, setup_entry, aioclient_mock: AiohttpClientMocker
) -> None:
    """Unload stops the webhook; removal tells the PMS through /unpair."""
    entry = await setup_entry()
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED
    aioclient_mock.post(UNPAIR_URL, json={"ok": True})
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    calls = [c for c in aioclient_mock.mock_calls if str(c[1]) == UNPAIR_URL]
    assert len(calls) == 1
    headers = calls[0][3]
    assert headers["X-TapeChart-Connection"] == entry.data["connection_id"]
    assert headers["X-TapeChart-Signature"].startswith("t=")


async def test_setup_auth_failure_starts_reauth(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, ha_urls: None
) -> None:
    """A 401 at startup is ConfigEntryAuthFailed (reauth)."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from .conftest import ENTRY_DATA, PROPERTY, ROOMS_URL

    aioclient_mock.get(
        ROOMS_URL, status=401, json={"ok": False, "error": "unauthorized"}
    )
    entry = MockConfigEntry(domain=DOMAIN, unique_id=PROPERTY, data=dict(ENTRY_DATA))
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(
        f["context"]["source"] == "reauth"
        for f in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    )


async def test_setup_not_ready(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, ha_urls: None
) -> None:
    """An unreachable PMS at startup retries later."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from .conftest import ENTRY_DATA, PROPERTY, ROOMS_URL

    aioclient_mock.get(ROOMS_URL, exc=TimeoutError())
    entry = MockConfigEntry(domain=DOMAIN, unique_id=PROPERTY, data=dict(ENTRY_DATA))
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY
