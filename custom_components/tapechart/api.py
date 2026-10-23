"""Client for the Tape Chart PMS endpoints (contract §4).

Kept free of Home Assistant imports (it takes an aiohttp session) so it can move to
a PyPI library later.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from urllib.parse import urlsplit
import uuid

import aiohttp

from .signing import signature_header

PATH_PAIR = "/api/home-assistant/pair"
PATH_ROOMS = "/api/home-assistant/rooms"
PATH_EVENTS = "/api/home-assistant/events"
PATH_UNPAIR = "/api/home-assistant/unpair"

PAIR_TIMEOUT = 45  # the PMS probes up to three webhook URLs, 5 s each
REQUEST_TIMEOUT = 20

# Slugs that mean "this connection is no longer valid": re-pair.
AUTH_SLUGS = frozenset({"unauthorized", "not_connected"})


class TapeChartError(Exception):
    """Base error."""

    def __init__(self, slug: str = "unknown", message: str | None = None) -> None:
        """Keep the PMS error slug."""
        super().__init__(message or slug)
        self.slug = slug


class TapeChartConnectionError(TapeChartError):
    """The PMS could not be reached, timed out or answered garbage."""


class TapeChartRejectedError(TapeChartError):
    """The PMS refused the request as malformed (`bad_request`): do not retry it."""


class TapeChartAuthError(TapeChartError):
    """The PMS refused our signature or the connection is gone (401)."""


def encode_body(payload: dict[str, Any]) -> bytes:
    """Serialize a request body once; the signature covers these exact bytes."""
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def normalize_url(url: str) -> str:
    """Return the PMS base URL without a trailing slash; ValueError if invalid."""
    url = url.strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(url)
    if parts.query or parts.fragment:
        raise ValueError(url)
    return url


def normalize_code(code: str) -> str:
    """Upper-case the pairing code and restore the XXXX-XXXX dash."""
    raw = "".join(ch for ch in code.upper() if ch.isalnum())
    if len(raw) == 8:
        return f"{raw[:4]}-{raw[4:]}"
    return code.strip().upper()


async def _read_json(resp: aiohttp.ClientResponse) -> dict[str, Any]:
    try:
        data = await resp.json(content_type=None)
    except (aiohttp.ContentTypeError, json.JSONDecodeError, ValueError) as err:
        raise TapeChartConnectionError("bad_response") from err
    if not isinstance(data, dict):
        raise TapeChartConnectionError("bad_response")
    return data


def _raise_for_answer(status: int, data: dict[str, Any] | None) -> None:
    slug = str((data or {}).get("error") or "")
    if status == 401 or slug in AUTH_SLUGS:
        raise TapeChartAuthError(slug or "unauthorized")
    if status in (403, 404):
        # The endpoint or the connection is gone: retrying cannot help, re-pair.
        raise TapeChartAuthError(
            slug or ("forbidden" if status == 403 else "not_found")
        )
    if slug == "bad_request" or (status in (400, 413, 422) and slug != "rate_limited"):
        raise TapeChartRejectedError(slug or "bad_request")
    if data is not None and data.get("ok") is False:
        if status >= 500 or slug in ("timeout", "ha_error"):
            raise TapeChartConnectionError(slug or "server_error")
        raise TapeChartError(slug or "unknown")
    if status >= 500:
        raise TapeChartConnectionError("server_error")
    if status >= 400:
        raise TapeChartError(slug or "bad_request")


async def async_pair(
    session: aiohttp.ClientSession, pms_url: str, code: str, ha: dict[str, Any]
) -> dict[str, Any]:
    """POST /pair (unsigned; the code is the credential)."""
    body = encode_body({"code": code, "ha": ha})
    try:
        async with asyncio.timeout(PAIR_TIMEOUT):
            async with session.post(
                pms_url + PATH_PAIR,
                data=body,
                headers={"Content-Type": "application/json"},
            ) as resp:
                status = resp.status
                try:
                    data = await _read_json(resp)
                except TapeChartConnectionError:
                    if status >= 400:
                        raise TapeChartConnectionError("server_error") from None
                    raise
    except (aiohttp.ClientError, TimeoutError) as err:
        raise TapeChartConnectionError("cannot_connect") from err
    if data.get("ok") is not True:
        slug = str(data.get("error") or "unknown")
        if status >= 500 and slug not in ("unreachable",):
            raise TapeChartConnectionError(slug)
        raise TapeChartError(slug)
    for key in ("connection_id", "secret", "property"):
        if key not in data:
            raise TapeChartConnectionError("bad_response")
    return data


class TapeChartClient:
    """Signed client for one connection."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        pms_url: str,
        connection_id: str,
        secret: str,
    ) -> None:
        """Initialize."""
        self._session = session
        self.pms_url = pms_url.rstrip("/")
        self.connection_id = connection_id
        self._secret = secret

    def signed_headers(self, method: str, path: str, body: bytes) -> dict[str, str]:
        """Return the three wire headers; PATH is the logical one (§9 H6)."""
        # A fresh id per attempt, also on a retry (§9 H5 allows reusing it): the
        # events inside carry their own ids and the PMS ingests them idempotently,
        # while a batch re-sent under a seen request id would be answered
        # `accepted: 0` even if events were appended to it since.
        message_id = str(uuid.uuid4())
        headers = {
            "X-TapeChart-Signature": signature_header(
                self._secret, int(time.time()), message_id, method, path, body
            ),
            "X-TapeChart-Id": message_id,
            "X-TapeChart-Connection": self.connection_id,
        }
        if method != "GET":
            headers["Content-Type"] = "application/json"
        return headers

    async def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        body = b"" if payload is None else encode_body(payload)
        headers = self.signed_headers(method, path, body)
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                async with self._session.request(
                    method,
                    self.pms_url + path,
                    data=body if method != "GET" else None,
                    headers=headers,
                ) as resp:
                    status = resp.status
                    try:
                        data: dict[str, Any] | None = await _read_json(resp)
                    except TapeChartConnectionError:
                        data = None
        except (aiohttp.ClientError, TimeoutError) as err:
            raise TapeChartConnectionError("cannot_connect") from err
        _raise_for_answer(status, data)
        if data is None:
            raise TapeChartConnectionError("bad_response")
        return data

    async def async_get_rooms(self) -> dict[str, Any]:
        """GET /rooms. Returns the whole answer (`rooms`, and `property` if sent)."""
        data = await self._request("GET", PATH_ROOMS)
        if not isinstance(data.get("rooms"), list):
            raise TapeChartConnectionError("bad_response")
        return data

    async def async_post_events(self, events: list[dict[str, Any]]) -> int:
        """POST /events (1-100 events). Returns `accepted`."""
        data = await self._request("POST", PATH_EVENTS, {"events": events})
        return int(data.get("accepted", len(events)))

    async def async_unpair(self) -> None:
        """POST /unpair."""
        await self._request("POST", PATH_UNPAIR, {})
