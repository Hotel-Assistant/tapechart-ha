"""Persistent, ordered outbox of events for POST /events (contract §4.3)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store

from .api import TapeChartAuthError, TapeChartError, TapeChartRejectedError
from .const import DOMAIN, MAX_BATCH, MAX_OUTBOX, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)

BACKOFF = (5, 15, 60, 300, 900)
SAVE_DELAY = 1.0
# The PMS accepts 600 events per connection per minute (§9 M5); stay under it.
RATE_EVENTS = 500
RATE_WINDOW = 60.0


def _coalesce_key(event: dict[str, Any]) -> tuple[str, ...] | None:
    """Events a newer one may replace while both are still unsent."""
    if event.get("type") == "mapping":
        return ("mapping", str(event.get("unit_id")))
    if event.get("type") == "health":
        return ("health",)
    return None


def _store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.outbox.{entry_id}")


async def async_remove_outbox(hass: HomeAssistant, entry_id: str) -> None:
    """Delete the stored outbox of a removed config entry."""
    await _store(hass, entry_id).async_remove()


class Outbox:
    """Events are appended, persisted, then posted in order in batches of ≤ 100."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        send: Callable[[list[dict[str, Any]]], Awaitable[Any]],
        on_auth_error: Callable[[], None],
    ) -> None:
        """Initialize."""
        self.hass = hass
        self._entry = entry
        self._store = _store(hass, entry.entry_id)
        self._window_start = 0.0
        self._window_count = 0
        self._send = send
        self._on_auth_error = on_auth_error
        self.events: list[dict[str, Any]] = []
        self._inflight = 0
        self._isolate = 0  # after a rejected batch: send one by one this many
        self._draining = False
        self._attempt = 0
        self._retry_unsub: CALLBACK_TYPE | None = None
        self._stopped = False
        self.paused = False
        self.last_error: str | None = None

    async def async_load(self) -> None:
        """Load events left from before a restart."""
        data = await self._store.async_load()
        if data and isinstance(data.get("events"), list):
            self.events = [e for e in data["events"] if isinstance(e, dict)]

    def _data(self) -> dict[str, Any]:
        return {"events": self.events}

    @callback
    def _save(self) -> None:
        if self._stopped:
            return  # a reloaded entry owns the file now
        self._store.async_delay_save(self._data, SAVE_DELAY)

    @callback
    def async_add(self, events: list[dict[str, Any]]) -> None:
        """Queue events and kick the drain."""
        if not events:
            return
        for event in events:
            key = _coalesce_key(event)
            if key is not None:
                # Replace-all semantics: an unsent older copy is obsolete.
                self.events[self._inflight :] = [
                    e for e in self.events[self._inflight :] if _coalesce_key(e) != key
                ]
            self.events.append(event)
        overflow = len(self.events) - MAX_OUTBOX
        if overflow > 0:
            drop = min(overflow, len(self.events) - self._inflight)
            del self.events[self._inflight : self._inflight + drop]
            _LOGGER.warning("Tape Chart outbox full, dropped %s oldest events", drop)
        self._save()
        self.async_kick()

    @callback
    def async_kick(self) -> None:
        """Start draining unless already draining, waiting to retry, or paused."""
        if (
            self._stopped
            or self.paused
            or self._draining
            or self._retry_unsub is not None
            or not self.events
        ):
            return
        self._draining = True
        self._entry.async_create_task(
            self.hass, self._async_drain(), "tapechart_outbox", eager_start=True
        )

    @callback
    def _retry(self, _now: Any) -> None:
        self._retry_unsub = None
        self.async_kick()

    async def _async_drain(self) -> None:
        try:
            while self.events and not self._stopped and not self.paused:
                batch = self.events[: 1 if self._isolate else MAX_BATCH]
                if (wait := self._rate_wait(len(batch))) > 0:
                    self._retry_unsub = async_call_later(self.hass, wait, self._retry)
                    return
                self._inflight = len(batch)
                try:
                    await self._send(batch)
                except TapeChartRejectedError as err:
                    # The PMS refuses the WHOLE batch for one bad event (§9 H1):
                    # find it by sending one at a time, drop only that one.
                    self._inflight = 0
                    if len(batch) > 1:
                        self._isolate = len(batch)
                        continue
                    _LOGGER.warning(
                        "Tape Chart refused a %s event (%s); it is dropped",
                        batch[0].get("type"),
                        err.slug,
                    )
                    del self.events[:1]
                    self._isolate = max(0, self._isolate - 1)
                    self._save()
                    continue
                except TapeChartAuthError as err:
                    self.last_error = err.slug
                    self.paused = True
                    self._on_auth_error()
                    return
                except TapeChartError as err:
                    self.last_error = err.slug
                    delay = BACKOFF[min(self._attempt, len(BACKOFF) - 1)]
                    self._attempt += 1
                    _LOGGER.debug(
                        "Tape Chart send failed (%s), retry in %ss", err, delay
                    )
                    self._retry_unsub = async_call_later(self.hass, delay, self._retry)
                    return
                finally:
                    self._inflight = 0
                del self.events[: len(batch)]
                if self._isolate:
                    self._isolate = max(0, self._isolate - len(batch))
                self._attempt = 0
                self.last_error = None
                self._save()
        finally:
            self._draining = False

    def _rate_wait(self, count: int) -> float:
        """Seconds to wait before sending `count` more events (0 = send now)."""
        now = self.hass.loop.time()
        if now - self._window_start >= RATE_WINDOW:
            self._window_start, self._window_count = now, 0
        if self._window_count and self._window_count + count > RATE_EVENTS:
            return self._window_start + RATE_WINDOW - now
        self._window_count += count
        return 0.0

    @callback
    def async_stop(self) -> None:
        """Stop draining; unsent events stay persisted."""
        self._stopped = True
        if self._retry_unsub is not None:
            self._retry_unsub()
            self._retry_unsub = None

    async def async_flush_to_disk(self) -> None:
        """Write the outbox now (unload)."""
        await self._store.async_save(self._data())
