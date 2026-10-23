"""Forward mapped entities' states to the PMS (contract §7 "Forwarding").

Only entities mapped to a signal role are tracked; nothing else leaves Home
Assistant. Presence `on` goes out at once and `off` only after the hold; numeric
temperatures (and a thermostat's current temperature) are debounced by a minimum
delta and a minimum interval.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
import uuid

from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.util import dt as dt_util

from .const import DEVICE_PREFIX, SIGNAL_ROLES
from .mapping import UNAVAILABLE, normalize_state, online_from_states

Key = tuple[str, str]  # (unit_id, role)


@dataclass
class _Track:
    """What was last sent for one (unit, role), and what is waiting."""

    sent: tuple[str, dict[str, Any]] | None = None
    sent_at: datetime | None = None
    pending: dict[str, Any] | None = None
    timer: CALLBACK_TYPE | None = None

    def cancel(self) -> None:
        if self.timer is not None:
            self.timer()
            self.timer = None
        self.pending = None


class Forwarder:
    """Tracks mapped entities and feeds normalized signals into the outbox."""

    def __init__(
        self,
        hass: HomeAssistant,
        emit: Callable[[list[dict[str, Any]]], None],
    ) -> None:
        """Initialize."""
        self.hass = hass
        self._emit = emit
        self._by_entity: dict[str, list[Key]] = {}
        self._device_keys: dict[Key, list[str]] = {}
        self._tracks: dict[Key, _Track] = {}
        self._unsub: CALLBACK_TYPE | None = None
        self.presence_hold = 300.0
        self.temp_delta = 0.5
        self.temp_interval = 60.0

    @callback
    def async_configure(
        self, presence_hold_minutes: float, temp_delta: float, temp_interval: float
    ) -> None:
        """Apply the settings step's values."""
        self.presence_hold = max(0.0, float(presence_hold_minutes)) * 60
        self.temp_delta = max(0.0, float(temp_delta))
        self.temp_interval = max(0.0, float(temp_interval))

    @callback
    def async_set_mapping(
        self, mapping: dict[str, dict[str, str]], send_current: set[Key] | None = None
    ) -> None:
        """(Re)subscribe to the mapped signal entities.

        `send_current` names the (unit, role) pairs whose entity's current state is
        sent right away (startup: all of them; after a mapping change: the new ones).
        """
        self.async_stop()
        by_entity: dict[str, list[Key]] = {}
        device_keys: dict[Key, list[str]] = {}
        registry = er.async_get(self.hass)
        for unit_id, roles in mapping.items():
            for role, entity_id in roles.items():
                if role not in SIGNAL_ROLES or not entity_id:
                    continue
                if entity_id.startswith(DEVICE_PREFIX):
                    # Availability of a whole device: watch all its entities.
                    members = [
                        e.entity_id
                        for e in er.async_entries_for_device(
                            registry, entity_id[len(DEVICE_PREFIX) :]
                        )
                    ]
                    device_keys[(unit_id, role)] = members
                    for member in members:
                        by_entity.setdefault(member, []).append((unit_id, role))
                    continue
                by_entity.setdefault(entity_id, []).append((unit_id, role))
        self._by_entity = by_entity
        self._device_keys = device_keys
        live = {key for keys in by_entity.values() for key in keys} | set(device_keys)
        for key in list(self._tracks):
            if key not in live:
                self._tracks.pop(key).cancel()
        if by_entity:
            self._unsub = async_track_state_change_event(
                self.hass, list(by_entity), self._async_state_changed
            )
        if send_current:
            for entity_id, keys in by_entity.items():
                state = self.hass.states.get(entity_id)
                for key in keys:
                    if key in send_current and key not in device_keys:
                        self._tracks.pop(key, _Track()).cancel()
                        self._handle(key, state, force=True)
            for key in device_keys:
                if key in send_current:
                    self._tracks.pop(key, _Track()).cancel()
                    self._handle_device(key, force=True)

    @callback
    def async_stop(self) -> None:
        """Stop listening (pending holds are cancelled with the subscription)."""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    @callback
    def async_shutdown(self) -> None:
        """Stop listening and cancel every timer."""
        self.async_stop()
        for track in self._tracks.values():
            track.cancel()
        self._tracks.clear()

    @callback
    def _async_state_changed(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        for key in self._by_entity.get(entity_id, ()):
            if key in self._device_keys:
                self._handle_device(key)
            else:
                self._handle(key, event.data["new_state"])

    @callback
    def _handle_device(self, key: Key, force: bool = False) -> None:
        """`online` from device availability: off when every entity is down."""
        states = [self.hass.states.get(e) for e in self._device_keys.get(key, [])]
        value: tuple[str, dict[str, Any]] = (
            online_from_states([s.state if s else None for s in states]),
            {},
        )
        track = self._tracks.setdefault(key, _Track())
        if force or value != track.sent:
            changed = max(
                (s.last_updated for s in states if s is not None),
                default=dt_util.utcnow(),
            )
            self._send(key, value, changed)

    # -- the rules ---------------------------------------------------------

    def _signal(
        self, key: Key, value: tuple[str, dict[str, Any]], changed_at: datetime
    ) -> dict[str, Any]:
        unit_id, role = key
        return {
            "id": str(uuid.uuid4()),
            "type": "signal",
            "unit_id": unit_id,
            "role": role,
            "state": value[0],
            "attributes": value[1],
            "changed_at": dt_util.as_utc(changed_at).isoformat(),
        }

    @callback
    def _send(
        self, key: Key, value: tuple[str, dict[str, Any]], changed_at: datetime
    ) -> None:
        track = self._tracks.setdefault(key, _Track())
        track.sent = value
        track.sent_at = dt_util.utcnow()
        self._emit([self._signal(key, value, changed_at)])

    @callback
    def _schedule(
        self,
        key: Key,
        delay: float,
        value: tuple[str, dict[str, Any]],
        changed_at: datetime,
    ) -> None:
        track = self._tracks.setdefault(key, _Track())
        track.cancel()
        track.pending = {"value": value, "changed_at": changed_at}

        @callback
        def _fire(_now: Any) -> None:
            track.timer = None
            pending, track.pending = track.pending, None
            if pending is not None:
                self._send(key, pending["value"], pending["changed_at"])

        track.timer = async_call_later(self.hass, delay, _fire)

    @callback
    def _handle(self, key: Key, state: State | None, force: bool = False) -> None:
        _unit_id, role = key
        if state is None:
            value: tuple[str, dict[str, Any]] = normalize_state(role, None, None)
            changed_at = dt_util.utcnow()
        else:
            value = normalize_state(role, state.state, state.attributes)
            changed_at = state.last_updated
        track = self._tracks.setdefault(key, _Track())

        if force:
            self._send(key, value, changed_at)
            return

        if role == "presence":
            self._presence(key, track, value, changed_at)
        elif role == "temperature":
            self._numeric(key, track, value, changed_at, _temp_of(value))
        elif role == "climate":
            self._climate(key, track, value, changed_at)
        else:
            track.cancel()
            if value != track.sent:
                self._send(key, value, changed_at)

    @callback
    def _presence(
        self,
        key: Key,
        track: _Track,
        value: tuple[str, dict[str, Any]],
        changed_at: datetime,
    ) -> None:
        if value[0] == "off":
            if track.sent is not None and track.sent[0] == "off":
                track.cancel()
                return
            if track.pending is not None:
                return  # an off is already held; keep its original time
            if self.presence_hold <= 0:
                self._send(key, value, changed_at)
            else:
                self._schedule(key, self.presence_hold, value, changed_at)
            return
        # on / unavailable: at once, and a held off is void.
        track.cancel()
        if value != track.sent:
            self._send(key, value, changed_at)

    @callback
    def _numeric(
        self,
        key: Key,
        track: _Track,
        value: tuple[str, dict[str, Any]],
        changed_at: datetime,
        number: float | None,
    ) -> None:
        if value == track.sent:
            track.cancel()
            return
        last = (
            _temp_of(track.sent)
            if key[1] == "temperature"
            else _climate_temp(track.sent)
        )
        if number is None or last is None:
            # Availability flipped, or nothing sent yet: no debounce.
            track.cancel()
            self._send(key, value, changed_at)
            return
        if abs(number - last) < self.temp_delta:
            track.cancel()
            return
        elapsed = (
            (dt_util.utcnow() - track.sent_at).total_seconds()
            if track.sent_at is not None
            else self.temp_interval
        )
        if elapsed >= self.temp_interval:
            track.cancel()
            self._send(key, value, changed_at)
        else:
            # Trailing send with the newest value once the interval has passed.
            self._schedule(key, self.temp_interval - elapsed, value, changed_at)

    @callback
    def _climate(
        self,
        key: Key,
        track: _Track,
        value: tuple[str, dict[str, Any]],
        changed_at: datetime,
    ) -> None:
        sent = track.sent
        if (
            sent is None
            or value[0] != sent[0]
            or value[1].get("temperature") != sent[1].get("temperature")
            or value[0] == UNAVAILABLE
        ):
            # Mode or setpoint: a deliberate change, sent at once.
            track.cancel()
            if value != sent:
                self._send(key, value, changed_at)
            return
        self._numeric(key, track, value, changed_at, _climate_temp(value))


def _temp_of(value: tuple[str, dict[str, Any]] | None) -> float | None:
    if value is None or value[0] == UNAVAILABLE:
        return None
    try:
        return float(value[0])
    except ValueError:
        return None


def _climate_temp(value: tuple[str, dict[str, Any]] | None) -> float | None:
    if value is None:
        return None
    current = value[1].get("current_temperature")
    return float(current) if isinstance(current, (int, float)) else None
