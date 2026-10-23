"""The Tape Chart integration: rooms of a hotel PMS in Home Assistant."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import TapeChartAuthError, TapeChartClient, TapeChartError
from .const import (
    CONF_CONNECTION_ID,
    CONF_PMS_URL,
    CONF_SECRET,
    CONF_WEBHOOK_ID,
    DOMAIN,
)
from .hub import TapeChartHub, async_delete_cloudhook
from .outbox import async_remove_outbox

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.EVENT, Platform.SENSOR]

type TapeChartConfigEntry = ConfigEntry[TapeChartHub]


def _client(hass: HomeAssistant, entry: ConfigEntry) -> TapeChartClient:
    return TapeChartClient(
        async_get_clientsession(hass),
        entry.data[CONF_PMS_URL],
        entry.data[CONF_CONNECTION_ID],
        entry.data[CONF_SECRET],
    )


async def async_setup_entry(hass: HomeAssistant, entry: TapeChartConfigEntry) -> bool:
    """Set up one connection (one property)."""
    client = _client(hass, entry)
    try:
        answer = await client.async_get_rooms()
    except TapeChartAuthError as err:
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="auth_failed"
        ) from err
    except TapeChartError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"error": err.slug},
        ) from err

    hub = TapeChartHub(hass, entry, client)
    entry.runtime_data = hub
    await hub.async_setup(answer)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_update_listener(
    hass: HomeAssistant, entry: TapeChartConfigEntry
) -> None:
    """Options changed (mapping, settings)."""
    await entry.runtime_data.async_options_updated()


async def async_unload_entry(hass: HomeAssistant, entry: TapeChartConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_unload()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """The integration was removed: tell the PMS, drop the cloudhook and outbox."""
    try:
        await _client(hass, entry).async_unpair()
    except TapeChartError as err:
        _LOGGER.info("Tape Chart: the PMS was not told about the removal (%s)", err)
    await async_delete_cloudhook(hass, entry.data[CONF_WEBHOOK_ID])
    await async_remove_outbox(hass, entry.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: TapeChartConfigEntry, device: dr.DeviceEntry
) -> bool:
    """A room device may be deleted by hand only when the PMS no longer lists it."""
    hub = entry.runtime_data
    return not any(
        ident[0] == DOMAIN and ident[1] in hub.rooms for ident in device.identifiers
    )
