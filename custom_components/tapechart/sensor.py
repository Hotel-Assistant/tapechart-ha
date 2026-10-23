"""Room sensors: status, guest, party, stay dates."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import TapeChartConfigEntry
from .const import ROOM_STATUSES
from .entity import TapeChartRoomEntity, setup_room_platform

PARALLEL_UPDATES = 0

Room = dict[str, Any]


def _stay(room: Room) -> Room:
    return room.get("stay") or {}


def _guest(room: Room) -> Room:
    return _stay(room).get("guest") or {}


def _ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = dt_util.parse_datetime(value)
    # A timestamp sensor needs a time zone; a naive value would fail the write.
    return parsed if parsed is not None and parsed.tzinfo is not None else None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _count(room: Room, key: str) -> int | None:
    """A party / nights count: 0 when the room has no stay (never "unknown")."""
    stay = room.get("stay")
    if not stay:
        return 0
    return _int(stay.get(key))


GUEST_LANGUAGES = ["en", "bg", "none"]


def _language(room: Room) -> str | None:
    """The guest's language; `none` when nobody is staying or arriving.

    With guest names switched off the PMS sends no guest, so the language is not known.
    """
    if not room.get("stay"):
        return "none"
    locale = _guest(room).get("locale")
    return locale if locale in ("en", "bg") else None


@dataclass(frozen=True, kw_only=True)
class TapeChartSensorDescription(SensorEntityDescription):
    """A room sensor: how to read its value and attributes off room_state."""

    value_fn: Callable[[Room], Any]
    attrs_fn: Callable[[Room], dict[str, Any]] | None = None
    status: bool = False


def _status(room: Room) -> str | None:
    status = room.get("status")
    return status if status in ROOM_STATUSES else None


def _next_arrival(room: Room) -> Room:
    return room.get("next_arrival") or {}


SENSORS: tuple[TapeChartSensorDescription, ...] = (
    TapeChartSensorDescription(
        key="status",
        translation_key="status",
        device_class=SensorDeviceClass.ENUM,
        options=list(ROOM_STATUSES),
        value_fn=_status,
        status=True,
    ),
    TapeChartSensorDescription(
        key="guest_language",
        translation_key="guest_language",
        device_class=SensorDeviceClass.ENUM,
        options=GUEST_LANGUAGES,
        value_fn=_language,
    ),
    TapeChartSensorDescription(
        key="adults",
        translation_key="adults",
        value_fn=lambda room: _count(room, "adults"),
    ),
    TapeChartSensorDescription(
        key="children",
        translation_key="children",
        value_fn=lambda room: _count(room, "children"),
        attrs_fn=lambda room: {"ages": list(_stay(room).get("child_ages") or [])},
    ),
    TapeChartSensorDescription(
        key="check_in",
        translation_key="check_in",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda room: _ts(_stay(room).get("check_in_at")),
    ),
    TapeChartSensorDescription(
        key="check_out",
        translation_key="check_out",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda room: _ts(_stay(room).get("check_out_at")),
    ),
    TapeChartSensorDescription(
        key="nights",
        translation_key="nights",
        value_fn=lambda room: _count(room, "nights"),
    ),
    TapeChartSensorDescription(
        key="nights_left",
        translation_key="nights_left",
        value_fn=lambda room: _count(room, "nights_left"),
    ),
    TapeChartSensorDescription(
        key="next_arrival",
        translation_key="next_arrival",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda room: _ts(_next_arrival(room).get("check_in_at")),
        attrs_fn=lambda room: {
            "adults": _next_arrival(room).get("adults"),
            "children": _next_arrival(room).get("children"),
        },
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TapeChartConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the room sensors."""
    hub = entry.runtime_data
    setup_room_platform(
        hass,
        hub,
        async_add_entities,
        lambda unit_id: [
            (TapeChartStatusSensor if d.status else TapeChartSensor)(hub, unit_id, d)
            for d in SENSORS
        ],
    )


class TapeChartSensor(TapeChartRoomEntity, SensorEntity):
    """A room sensor."""

    entity_description: TapeChartSensorDescription

    @property
    def native_value(self) -> Any:
        """The value off the latest room_state."""
        return self.entity_description.value_fn(self.room)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Extra attributes."""
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.room)


class TapeChartStatusSensor(TapeChartSensor):
    """The room status; the guest's names ride along as unrecorded attributes.

    Names are never an entity STATE (§9 M2): HA keeps states in history and the
    logbook, while `_unrecorded_attributes` keeps these out of the recorder.
    """

    _unrecorded_attributes = frozenset({"guest", "companions"})

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """business_date and, when sent, the names."""
        room = self.room
        attrs: dict[str, Any] = {"business_date": room.get("business_date")}
        if name := _guest(room).get("name"):
            attrs["guest"] = name
        if companions := _stay(room).get("companions"):
            attrs["companions"] = list(companions)
        return attrs
