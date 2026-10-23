"""The PMS -> HA webhook (contract §5 + §9)."""

from __future__ import annotations

import copy
import json
import time
from typing import Any

from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from pytest_homeassistant_custom_component.common import async_mock_service
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.tapechart.const import DOMAIN

from .conftest import ROOMS_URL, U205, U206, WEBHOOK_ID, room_device, sign

PATH = f"/api/webhook/{WEBHOOK_ID}"
MAPPING = {
    U205: {
        "presence": "binary_sensor.room_205_bathroom_pir",
        "scenario_welcome": "script.welcome_205",
        "scenario_eco": "scene.eco_205",
    }
}


async def _post(client, message: dict[str, Any], **kw: Any):
    body = json.dumps(message).encode()
    return await client.post(PATH, data=body, headers=sign(body, **kw))


def _status_entity(hass: HomeAssistant, unit_id: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{unit_id}_status"
    )
    assert entity_id
    return entity_id


async def test_refuses_bad_signatures(
    hass: HomeAssistant, setup_entry, hass_client_no_auth
) -> None:
    """Bad key, stale t, wrong connection, missing id: 401 with a constant body."""
    await setup_entry()
    client = await hass_client_no_auth()
    ping = {"type": "ping", "nonce": "n"}
    for kwargs in (
        {"secret": "0" * 64},
        {"t": int(time.time()) - 400},
        {"t": int(time.time()) + 400},
        {"connection": "other"},
    ):
        resp = await _post(client, ping, **kwargs)
        assert resp.status == 401
        assert await resp.json() == {"ok": False, "error": "unauthorized"}
    body = json.dumps(ping).encode()
    headers = sign(body)
    headers.pop("X-TapeChart-Id")
    assert (await client.post(PATH, data=body, headers=headers)).status == 401
    # The signature covers the exact bytes.
    headers = sign(body)
    assert (await client.post(PATH, data=body + b" ", headers=headers)).status == 401


async def test_ping_and_replay(
    hass: HomeAssistant, setup_entry, hass_client_no_auth
) -> None:
    """Ping echoes the nonce; a replayed id gets the first answer again."""
    await setup_entry()
    client = await hass_client_no_auth()
    resp = await _post(client, {"type": "ping", "nonce": "abc"}, message_id="m1")
    assert resp.status == 200
    assert await resp.json() == {"ok": True, "nonce": "abc"}
    resp = await _post(client, {"type": "ping", "nonce": "zzz"}, message_id="m1")
    assert await resp.json() == {"ok": True, "nonce": "abc"}
    resp = await _post(client, {"type": "nope"})
    assert resp.status == 400


async def test_room_state_version_gate(
    hass: HomeAssistant, setup_entry, hass_client_no_auth, rooms_answer
) -> None:
    """A room_state not newer than the held version is ignored."""
    await setup_entry()
    client = await hass_client_no_auth()
    entity_id = _status_entity(hass, U206)
    assert hass.states.get(entity_id).state == "vacant"

    room = copy.deepcopy(rooms_answer["rooms"][1])
    room.update(version=11, status="occupied")  # same version as held
    resp = await _post(client, {"type": "room_state", "room": room})
    assert await resp.json() == {"ok": True}
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "vacant"

    room.update(version=12, status="arriving")
    await _post(client, {"type": "room_state", "room": room})
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "arriving"

    room.update(version=5, status="vacant")  # older
    await _post(client, {"type": "room_state", "room": room})
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "arriving"

    room.update(version=99, status="vacant", property_id="another-property")
    await _post(client, {"type": "room_state", "room": room})
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "arriving"


async def test_command_runs_only_the_mapped_scenario(
    hass: HomeAssistant, setup_entry, hass_client_no_auth
) -> None:
    """Only the entity mapped to scenario_<x> of that room runs, once per command_id."""
    scripts = async_mock_service(hass, "script", "turn_on")
    scenes = async_mock_service(hass, "scene", "turn_on")
    lights = async_mock_service(hass, "light", "turn_on")
    hass.states.async_set("script.welcome_205", "off")
    hass.states.async_set("scene.eco_205", "scening")
    await setup_entry(options={"mapping": MAPPING})
    client = await hass_client_no_auth()

    def cmd(scenario: str, unit: str = U205, command_id: str = "c1") -> dict[str, Any]:
        return {
            "type": "command",
            "command_id": command_id,
            "unit_id": unit,
            "scenario": scenario,
        }

    resp = await _post(client, cmd("welcome"))
    assert await resp.json() == {"ok": True}
    assert [c.data["entity_id"] for c in scripts] == ["script.welcome_205"]

    # Same command id again (a retry under a new message id): not run twice.
    resp = await _post(client, cmd("welcome"))
    assert await resp.json() == {"ok": True}
    assert len(scripts) == 1

    resp = await _post(client, cmd("eco", command_id="c2"))
    assert await resp.json() == {"ok": True}
    assert [c.data["entity_id"] for c in scenes] == ["scene.eco_205"]

    resp = await _post(client, cmd("off", command_id="c3"))
    assert await resp.json() == {"ok": False, "error": "not_mapped"}
    resp = await _post(client, cmd("welcome", unit=U206, command_id="c4"))
    assert await resp.json() == {"ok": False, "error": "not_mapped"}
    resp = await _post(client, cmd("welcome", unit="nope", command_id="c5"))
    assert await resp.json() == {"ok": False, "error": "unknown_room"}
    resp = await _post(client, cmd("light.turn_on", command_id="c6"))
    assert await resp.json() == {"ok": False, "error": "not_mapped"}
    assert not lights
    assert len(scripts) == 1 and len(scenes) == 1


async def test_command_failure(
    hass: HomeAssistant, setup_entry, hass_client_no_auth
) -> None:
    """A mapped script that is gone, or that raises, answers failed."""
    await setup_entry(options={"mapping": MAPPING})
    client = await hass_client_no_auth()
    resp = await _post(
        client,
        {"type": "command", "command_id": "c9", "unit_id": U205, "scenario": "welcome"},
    )
    assert await resp.json() == {"ok": False, "error": "failed"}


async def test_rooms_changed_refetches(
    hass: HomeAssistant,
    setup_entry,
    hass_client_no_auth,
    aioclient_mock: AiohttpClientMocker,
    rooms_answer,
) -> None:
    """rooms_changed: /rooms again; new rooms get devices, archived ones lose them."""
    entry = await setup_entry()
    client = await hass_client_no_auth()
    changed = copy.deepcopy(rooms_answer)
    changed["rooms"] = [r for r in changed["rooms"] if r["unit_id"] != U206]
    new = copy.deepcopy(rooms_answer["rooms"][1])
    new.update(unit_id="22222222-2222-4222-8222-222222222301", number="301", version=1)
    changed["rooms"].append(new)
    changed["rooms"][0]["number"] = "205A"
    changed["rooms"][0]["version"] = 50
    changed["property"] = {"id": "x", "name": "Villa Bozhe Spa"}
    aioclient_mock.clear_requests()
    aioclient_mock.get(ROOMS_URL, json=changed)
    aioclient_mock.post(
        "https://pms.example.com/api/home-assistant/events", json={"ok": True}
    )

    resp = await _post(client, {"type": "rooms_changed"})
    assert await resp.json() == {"ok": True}
    await hass.async_block_till_done()

    hub = entry.runtime_data
    assert U206 not in hub.rooms
    assert "22222222-2222-4222-8222-222222222301" in hub.rooms
    reg = er.async_get(hass)
    assert reg.async_get_entity_id(
        "sensor", DOMAIN, "22222222-2222-4222-8222-222222222301_status"
    )
    assert reg.async_get_entity_id("sensor", DOMAIN, f"{U206}_status") is None

    device = room_device(hass, U205)
    assert device.name == "Villa Bozhe Spa · 205A"
    assert entry.title == "Villa Bozhe Spa"


async def test_disconnected_starts_reauth(
    hass: HomeAssistant, setup_entry, hass_client_no_auth
) -> None:
    """`disconnected` raises a repair issue and a reauth flow, and stops sending."""
    entry = await setup_entry()
    client = await hass_client_no_auth()
    resp = await _post(client, {"type": "disconnected"})
    assert await resp.json() == {"ok": True}
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert any(f["context"]["source"] == SOURCE_REAUTH for f in flows)
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"connection_lost_{entry.entry_id}"
    )
    assert issue is not None
    assert entry.runtime_data.outbox.paused


async def test_handler_error_is_not_success(
    hass: HomeAssistant, setup_entry, hass_client_no_auth
) -> None:
    """A crash answers 500 (HA itself would answer 200)."""
    entry = await setup_entry()
    client = await hass_client_no_auth()

    def boom(*_a: Any, **_k: Any) -> bool:
        raise RuntimeError("boom")

    entry.runtime_data.async_update_room = boom
    resp = await _post(client, {"type": "room_state", "room": {"unit_id": U205}})
    assert resp.status == 500


async def test_cloudhook_path_is_logical(hass: HomeAssistant, setup_entry) -> None:
    """A cloudhook MockRequest (no URL path) verifies against /tapechart/webhook."""
    from homeassistant.components import webhook
    from homeassistant.util.aiohttp import MockRequest

    await setup_entry()
    body = json.dumps({"type": "ping", "nonce": "c"}).encode()
    request = MockRequest(
        content=body, mock_source="cloud", method="POST", headers=sign(body)
    )
    resp = await webhook.async_handle_webhook(hass, WEBHOOK_ID, request)
    assert resp.status == 200
    assert json.loads(resp.body) == {"ok": True, "nonce": "c"}
