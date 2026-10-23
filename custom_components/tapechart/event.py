"""Room event entity: what happened to the stay (checked in, moved out, …)."""

from __future__ import annotations

from homeassistant.components.event import EventEntity, EventEntityDescription
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import TapeChartConfigEntry
from .const import STAY_EVENTS
from .entity import TapeChartRoomEntity, setup_room_platform
from .hub import signal_stay_event

PARALLEL_UPDATES = 0

STAY = EventEntityDescription(
    key="stay", translation_key="stay", event_types=list(STAY_EVENTS)
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TapeChartConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the room stay events."""
    hub = entry.runtime_data
    setup_room_platform(
        hass,
        hub,
        async_add_entities,
        lambda unit_id: [TapeChartStayEvent(hub, unit_id, STAY)],
    )


class TapeChartStayEvent(TapeChartRoomEntity, EventEntity):
    """Fires `checked_in`, `checked_out`, `moved_in`, … for the room."""

    async def async_added_to_hass(self) -> None:
        """Also listen for the room's stay events."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_stay_event(self.hub.entry.entry_id, self.unit_id),
                self._async_stay_events,
            )
        )

    @callback
    def _async_room_updated(self) -> None:
        """A room update alone fires nothing (the event state is the last event)."""

    @callback
    def _async_stay_events(self, events: list[str]) -> None:
        for event_type in events:
            if event_type in STAY_EVENTS:
                self._trigger_event(event_type)
        self.async_write_ha_state()
