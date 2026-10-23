"""Pairing, reauth and the https rule."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch
import uuid

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.tapechart.const import DOMAIN

from .conftest import CONN, ENTRY_DATA, PAIR_URL, PMS, PROPERTY, SECRET, sign

PAIR_OK = {
    "ok": True,
    "connection_id": CONN,
    "secret": SECRET,
    "webhook_url": "http://192.168.1.10:8123/api/webhook/x",
    "property": {"id": PROPERTY, "name": "Villa Bozhe", "timezone": "Europe/Sofia"},
}


@pytest.fixture(autouse=True)
def no_setup():
    """The flow is under test, not the entry setup."""
    with patch(
        "custom_components.tapechart.async_setup_entry", return_value=True
    ) as mock:
        yield mock


async def _start(hass: HomeAssistant) -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return result


async def test_pair_ok_answers_the_probe(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_client_no_auth,
    ha_urls: None,
) -> None:
    """The PMS's probe ping, signed with our probe_token, is answered during /pair."""
    client = await hass_client_no_auth()
    seen: dict[str, Any] = {}

    async def _pair(method: str, url: Any, data: bytes) -> AiohttpClientMockResponse:
        request = json.loads(data)
        seen["request"] = request
        ha = request["ha"]
        path = "/" + ha["webhook_urls"][0].split("/", 3)[3]
        body = json.dumps({"type": "ping", "nonce": "n1"}).encode()
        # Wrong key: refused.
        bad = await client.post(path, data=body, headers=sign(body, secret="0" * 64))
        seen["bad"] = bad.status
        # Not a ping, even correctly signed: refused.
        other = json.dumps({"type": "command", "command_id": "x"}).encode()
        resp = await client.post(
            path, data=other, headers=sign(other, secret=ha["probe_token"])
        )
        seen["other"] = resp.status
        good = await client.post(
            path, data=body, headers=sign(body, secret=ha["probe_token"])
        )
        seen["good"] = (good.status, await good.json())
        return AiohttpClientMockResponse("post", url, json=PAIR_OK)

    aioclient_mock.post(PAIR_URL, side_effect=_pair)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"pms_url": PMS + "/", "code": "abcd efgh"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Villa Bozhe"
    assert result["result"].unique_id == PROPERTY
    data = result["data"]
    assert data["pms_url"] == PMS
    assert data["secret"] == SECRET
    assert data["connection_id"] == CONN

    request = seen["request"]
    assert request["code"] == "ABCD-EFGH"
    assert len(request["ha"]["probe_token"]) == 64
    assert request["ha"]["webhook_urls"] == [
        f"http://192.168.1.10:8123/api/webhook/{data['webhook_id']}",
        f"https://ha.example.com/api/webhook/{data['webhook_id']}",
    ]
    assert seen["bad"] == 401
    assert seen["other"] == 401
    assert seen["good"] == (200, {"ok": True, "nonce": "n1"})

    # After the flow the pairing handler is gone (the probe token is dead).
    client2 = await hass_client_no_auth()
    body = json.dumps({"type": "ping", "nonce": "n2"}).encode()
    resp = await client2.post(
        f"/api/webhook/{data['webhook_id']}",
        data=body,
        headers=sign(body, secret=request["ha"]["probe_token"]),
    )
    assert resp.status == 200  # HA's answer for an unknown webhook…
    assert (await resp.text()) == ""  # …with no nonce echoed


@pytest.mark.parametrize(
    ("status", "answer", "error"),
    [
        (400, {"ok": False, "error": "invalid_code"}, "invalid_code"),
        (400, {"ok": False, "error": "code_expired"}, "code_expired"),
        (502, {"ok": False, "error": "unreachable"}, "unreachable"),
        (429, {"ok": False, "error": "rate_limited"}, "rate_limited"),
        (500, None, "cannot_connect"),
    ],
)
async def test_pair_errors(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    ha_urls: None,
    status: int,
    answer: dict | None,
    error: str,
) -> None:
    """PMS refusals map to form errors; the form can be retried."""
    if answer is None:
        aioclient_mock.post(PAIR_URL, status=status, text="<html>oops</html>")
    else:
        aioclient_mock.post(PAIR_URL, status=status, json=answer)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"pms_url": PMS, "code": "ABCD-EFGH"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}

    aioclient_mock.clear_requests()
    aioclient_mock.post(PAIR_URL, json=PAIR_OK)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"pms_url": PMS, "code": "ABCD-EFGH"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_pair_network_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, ha_urls: None
) -> None:
    """A PMS that cannot be reached."""
    aioclient_mock.post(PAIR_URL, exc=TimeoutError())
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"pms_url": PMS, "code": "ABCD-EFGH"}
    )
    assert result["errors"] == {"base": "cannot_connect"}


@pytest.mark.parametrize(
    ("url", "error"),
    [
        ("ftp://pms.example.com", "invalid_url"),
        ("pms.example.com", "invalid_url"),
        ("http://pms.example.com", "insecure_url"),
        ("http://8.8.8.8:3000", "insecure_url"),
    ],
)
async def test_bad_urls(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    ha_urls: None,
    url: str,
    error: str,
) -> None:
    """Non-http(s) is invalid; plain http only for a LAN address."""
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"pms_url": url, "code": "ABCD-EFGH"}
    )
    assert result["errors"] == {"pms_url": error}
    assert not aioclient_mock.mock_calls


@pytest.mark.parametrize(
    "url", ["http://192.168.1.20:3000", "http://pms.local", "http://pms"]
)
async def test_lan_http_allowed(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, ha_urls: None, url: str
) -> None:
    """A self-hosted PMS in the hotel LAN may use plain http."""
    aioclient_mock.post(url + "/api/home-assistant/pair", json=PAIR_OK)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"pms_url": url, "code": "ABCD-EFGH"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_no_ha_url(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Without any HA URL the PMS could never reach us."""
    with patch(
        "custom_components.tapechart.config_flow.async_webhook_urls",
        return_value=([], None),
    ):
        result = await _start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"pms_url": PMS, "code": "ABCD-EFGH"}
        )
    assert result["errors"] == {"base": "no_ha_url"}


async def test_second_pairing_updates_the_entry(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, ha_urls: None
) -> None:
    """The same property paired again updates the existing entry (new secret)."""
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=PROPERTY, title="Villa Bozhe", data=dict(ENTRY_DATA)
    )
    entry.add_to_hass(hass)
    new_conn = str(uuid.uuid4())
    aioclient_mock.post(
        PAIR_URL,
        json={**PAIR_OK, "connection_id": new_conn, "secret": "ab" * 32},
    )
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"pms_url": PMS, "code": "ABCD-EFGH"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data["connection_id"] == new_conn
    assert entry.data["secret"] == "ab" * 32
    assert entry.data["webhook_id"] != ENTRY_DATA["webhook_id"]
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_reauth(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, ha_urls: None
) -> None:
    """Reauth asks for a new code, keeps the options, refuses another property."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=PROPERTY,
        title="Villa Bozhe",
        data=dict(ENTRY_DATA),
        options={"mapping": {"u": {"presence": "binary_sensor.x"}}},
    )
    entry.add_to_hass(hass)

    aioclient_mock.post(
        PAIR_URL, json={**PAIR_OK, "property": {"id": "other", "name": "Other"}}
    )
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"pms_url": PMS, "code": "ABCD-EFGH"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_property"

    aioclient_mock.clear_requests()
    aioclient_mock.post(PAIR_URL, json={**PAIR_OK, "secret": "cd" * 32})
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"pms_url": PMS, "code": "ABCD-EFGH"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["secret"] == "cd" * 32
    assert entry.options["mapping"] == {"u": {"presence": "binary_sensor.x"}}
