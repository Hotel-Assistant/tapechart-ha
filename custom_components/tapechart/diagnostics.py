"""Diagnostics: connection, mapping and rooms, with secrets and guest names removed."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import TapeChartConfigEntry
from .const import (
    CONF_CLOUDHOOK_URL,
    CONF_CONNECTION_ID,
    CONF_SECRET,
    CONF_WEBHOOK_ID,
    CONF_WEBHOOK_URL,
)

TO_REDACT_ENTRY = {
    CONF_SECRET,
    CONF_WEBHOOK_ID,
    CONF_WEBHOOK_URL,
    CONF_CLOUDHOOK_URL,
    CONF_CONNECTION_ID,
}
TO_REDACT_ROOM = {"guest", "companions", "name", "first_name", "last_name"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: TapeChartConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    hub = entry.runtime_data
    return {
        "entry": {
            "title": entry.title,
            "data": async_redact_data(dict(entry.data), TO_REDACT_ENTRY),
            "options": dict(entry.options),
        },
        "connection": {
            "disconnected": hub.disconnected,
            "webhook_urls": len(hub.webhook_urls),
            "last_webhook_at": hub.last_webhook_at,
            "outbox_size": len(hub.outbox.events),
            "outbox_paused": hub.outbox.paused,
            "outbox_last_error": hub.outbox.last_error,
        },
        "rooms": [
            async_redact_data(room, TO_REDACT_ROOM) for room in hub.rooms.values()
        ],
    }
