"""Persistent memory of the PMS messages and commands already handled (§9 H5).

Message ids are kept 15 minutes (the PMS retries a message under the same id with a
fresh `t`), command ids 24 hours (a command runs at most once), across restarts.
"""

from __future__ import annotations

import time
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .const import DEDUPE_SECONDS, DOMAIN, STORAGE_VERSION

COMMAND_SECONDS = 24 * 3600
MAX_ENTRIES = 5000
SAVE_DELAY = 2.0


class SeenStore:
    """id -> (expires at, the answer given)."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialize."""
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.seen.{entry_id}"
        )
        self._data: dict[str, dict[str, list[Any]]] = {"messages": {}, "commands": {}}

    async def async_load(self) -> None:
        """Load and prune."""
        data = await self._store.async_load() or {}
        for kind in ("messages", "commands"):
            if isinstance(data.get(kind), dict):
                self._data[kind] = data[kind]
        self._prune()

    async def async_remove(self) -> None:
        """Delete the file."""
        await self._store.async_remove()

    async def async_flush(self) -> None:
        """Write now."""
        await self._store.async_save(self._data)

    def _prune(self) -> None:
        now = time.time()
        for kind, items in self._data.items():
            live = {
                k: v for k, v in items.items() if isinstance(v, list) and v[0] > now
            }
            if len(live) > MAX_ENTRIES:
                live = dict(
                    sorted(live.items(), key=lambda kv: kv[1][0])[-MAX_ENTRIES:]
                )
            self._data[kind] = live

    def _get(self, kind: str, key: str) -> dict[str, Any] | None:
        item = self._data[kind].get(key)
        if item is None or item[0] <= time.time():
            return None
        return item[1]

    @callback
    def _put(self, kind: str, key: str, answer: dict[str, Any], seconds: int) -> None:
        self._data[kind][key] = [time.time() + seconds, answer]
        if len(self._data[kind]) > MAX_ENTRIES:
            self._prune()
        self._store.async_delay_save(lambda: self._data, SAVE_DELAY)

    def message(self, message_id: str) -> dict[str, Any] | None:
        """The answer to an already-handled message id."""
        return self._get("messages", message_id)

    @callback
    def remember_message(self, message_id: str, answer: dict[str, Any]) -> None:
        """Remember a handled message id."""
        self._put("messages", message_id, answer, DEDUPE_SECONDS)

    def command(self, command_id: str) -> dict[str, Any] | None:
        """The answer of a command that already ran."""
        return self._get("commands", command_id)

    async def async_remember_command(
        self, command_id: str, answer: dict[str, Any]
    ) -> None:
        """Remember a command that ran, on disk at once (no replay after a crash)."""
        self._put("commands", command_id, answer, COMMAND_SECONDS)
        await self._store.async_save(self._data)
