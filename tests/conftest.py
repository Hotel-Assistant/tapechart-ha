"""Fixtures for the Tape Chart tests."""

from __future__ import annotations

from collections.abc import Callable
import copy
import json
from pathlib import Path
import time
from typing import Any
import uuid

from homeassistant.core import HomeAssistant
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.tapechart.const import DOMAIN
from custom_components.tapechart.signing import PATH_WEBHOOK, signature_header

FIXTURES = Path(__file__).parent / "fixtures"

PMS = "https://pms.example.com"
CONN = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
SECRET = "9f2c4e6a8b0d1f3e5a7c9b1d3f5e7a9c0b2d4f6e8a1c3e5b7d9f0a2c4e6b8d0f"
PROPERTY = "99999999-9999-4999-8999-999999999999"
WEBHOOK_ID = "hook123"
U205 = "11111111-1111-4111-8111-111111111205"
U206 = "11111111-1111-4111-8111-111111111206"
U207 = "11111111-1111-4111-8111-111111111207"
U208 = "11111111-1111-4111-8111-111111111208"
U209 = "11111111-1111-4111-8111-111111111209"
U210 = "11111111-1111-4111-8111-111111111210"

ENTRY_DATA = {
    "pms_url": PMS,
    "connection_id": CONN,
    "secret": SECRET,
    "webhook_id": WEBHOOK_ID,
    "webhook_url": "http://192.168.1.10:8123/api/webhook/hook123",
    "cloudhook_url": None,
    "property_id": PROPERTY,
    "property_name": "Villa Bozhe",
    "property_timezone": "Europe/Sofia",
}

ROOMS_URL = PMS + "/api/home-assistant/rooms"
EVENTS_URL = PMS + "/api/home-assistant/events"
UNPAIR_URL = PMS + "/api/home-assistant/unpair"
PAIR_URL = PMS + "/api/home-assistant/pair"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/tapechart."""


@pytest.fixture
def rooms_answer() -> dict[str, Any]:
    """A /rooms answer with every status."""
    return json.loads((FIXTURES / "rooms.json").read_text(encoding="utf-8"))


@pytest.fixture
async def ha_urls(hass: HomeAssistant) -> None:
    """Give HA an internal and an external URL."""
    await async_process_ha_core_config(
        hass,
        {
            "internal_url": "http://192.168.1.10:8123",
            "external_url": "https://ha.example.com",
        },
    )
    await async_setup_component(hass, "http", {})


def sign(
    body: bytes,
    *,
    secret: str = SECRET,
    t: int | None = None,
    message_id: str | None = None,
    connection: str = CONN,
) -> dict[str, str]:
    """Headers of a PMS -> HA message."""
    message_id = message_id or str(uuid.uuid4())
    return {
        "X-TapeChart-Signature": signature_header(
            secret, int(time.time()) if t is None else t, message_id, "POST", PATH_WEBHOOK, body
        ),
        "X-TapeChart-Id": message_id,
        "X-TapeChart-Connection": connection,
        "Content-Type": "application/json",
    }


def events_posted(aioclient_mock: AiohttpClientMocker) -> list[dict[str, Any]]:
    """Every event POSTed to /events, in order."""
    out: list[dict[str, Any]] = []
    for method, url, data, _headers in aioclient_mock.mock_calls:
        if method.lower() == "post" and str(url) == EVENTS_URL:
            out.extend(json.loads(data)["events"])
    return out


@pytest.fixture
def setup_entry(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    rooms_answer: dict[str, Any],
    ha_urls: None,
) -> Callable[..., Any]:
    """Set up a paired entry against a mocked PMS."""

    async def _setup(
        options: dict[str, Any] | None = None,
        rooms: dict[str, Any] | None = None,
        mock_events: bool = True,
    ) -> MockConfigEntry:
        aioclient_mock.get(ROOMS_URL, json=copy.deepcopy(rooms or rooms_answer))
        if mock_events:
            aioclient_mock.post(EVENTS_URL, json={"ok": True, "accepted": 1})
        entry = MockConfigEntry(
            domain=DOMAIN,
            title="Villa Bozhe",
            unique_id=PROPERTY,
            data=dict(ENTRY_DATA),
            options=options or {},
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        return entry

    return _setup
