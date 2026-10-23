"""Base entity: one room of the PMS."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity, EntityDescription
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN, MANUFACTURER
from .hub import TapeChartHub, signal_new_rooms, signal_room


class TapeChartRoomEntity(Entity):
    """An entity that belongs to one room device."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self, hub: TapeChartHub, unit_id: str, description: EntityDescription
    ) -> None:
        """Initialize."""
        self.hub = hub
        self.unit_id = unit_id
        self.entity_description = description
        self._attr_unique_id = f"{unit_id}_{description.key}"
        room = hub.rooms.get(unit_id) or {}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, unit_id)},
            name=hub.device_name(unit_id),
            manufacturer=MANUFACTURER,
            model=room.get("room_type"),
            configuration_url=f"{hub.client.pms_url}/inventory/rooms/{unit_id}",
        )

    @property
    def room(self) -> dict[str, Any]:
        """The latest room_state."""
        return self.hub.rooms.get(self.unit_id) or {}

    @property
    def stay(self) -> dict[str, Any]:
        """The room's stay, or {}."""
        return self.room.get("stay") or {}

    @property
    def available(self) -> bool:
        """A room the PMS no longer lists is unavailable."""
        return self.unit_id in self.hub.rooms

    async def async_added_to_hass(self) -> None:
        """Follow the room's updates."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_room(self.hub.entry.entry_id, self.unit_id),
                self._async_room_updated,
            )
        )

    @callback
    def _async_room_updated(self) -> None:
        self.async_write_ha_state()


def setup_room_platform(
    hass: HomeAssistant,
    hub: TapeChartHub,
    async_add_entities: AddConfigEntryEntitiesCallback,
    build: Callable[[str], list[Entity]],
) -> None:
    """Add entities for every room now and for rooms that appear later."""
    entities: list[Entity] = []
    for unit_id in hub.rooms:
        entities.extend(build(unit_id))
    async_add_entities(entities)

    @callback
    def _new_rooms(unit_ids: list[str]) -> None:
        added: list[Entity] = []
        for unit_id in unit_ids:
            added.extend(build(unit_id))
        async_add_entities(added)

    hub.entry.async_on_unload(
        async_dispatcher_connect(hass, signal_new_rooms(hub.entry.entry_id), _new_rooms)
    )
