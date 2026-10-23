"""Room binary sensor: a guest is checked in."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import TapeChartConfigEntry
from .entity import TapeChartRoomEntity, setup_room_platform

PARALLEL_UPDATES = 0

# No device class: this is the booking fact, not physical occupancy.
GUEST_CHECKED_IN = BinarySensorEntityDescription(
    key="guest_checked_in", translation_key="guest_checked_in"
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TapeChartConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the room binary sensors."""
    hub = entry.runtime_data
    setup_room_platform(
        hass,
        hub,
        async_add_entities,
        lambda unit_id: [TapeChartCheckedIn(hub, unit_id, GUEST_CHECKED_IN)],
    )


class TapeChartCheckedIn(TapeChartRoomEntity, BinarySensorEntity):
    """On while the room's stay is checked in."""

    @property
    def is_on(self) -> bool:
        """Checked in."""
        return bool(self.stay.get("checked_in"))
