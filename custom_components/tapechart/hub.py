"""The runtime of one Tape Chart connection (one config entry = one property)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any
import uuid

from aiohttp import web
from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_CORE_CONFIG_UPDATE, __version__ as HA_VERSION
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.util import dt as dt_util

from .api import TapeChartAuthError, TapeChartClient, TapeChartError
from .const import (
    CONF_CLOUDHOOK_URL,
    CONF_CONNECTION_ID,
    CONF_PROPERTY_ID,
    CONF_PROPERTY_NAME,
    CONF_SECRET,
    CONF_WEBHOOK_ID,
    DEFAULT_PRESENCE_HOLD,
    DEFAULT_TEMP_DELTA,
    DEFAULT_TEMP_INTERVAL,
    DEVICE_PREFIX,
    DOMAIN,
    HEADER_CONNECTION,
    HEADER_ID,
    HEADER_SIGNATURE,
    MAX_WEBHOOK_BODY,
    OPT_MAPPING,
    OPT_PRESENCE_HOLD,
    OPT_TEMP_DELTA,
    OPT_TEMP_INTERVAL,
    ROLE_DOMAINS,
    SCENARIOS,
    SIGNAL_ROLES,
    SIGNATURE_TOLERANCE,
    STAY_EVENTS,
)
from .forwarder import Forwarder
from .outbox import Outbox
from .seen import SeenStore
from .signing import PATH_WEBHOOK, verify_signature

_LOGGER = logging.getLogger(__name__)

_ENTITY_ID = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
UNAUTHORIZED_BODY = '{"ok":false,"error":"unauthorized"}'
ISSUE_CONNECTION = "connection_lost"
COMMAND_TIMEOUT = 8


def signal_room(entry_id: str, unit_id: str) -> str:
    """Dispatcher signal: one room's state changed."""
    return f"{DOMAIN}_{entry_id}_room_{unit_id}"


def signal_stay_event(entry_id: str, unit_id: str) -> str:
    """Dispatcher signal: fire the room's stay event entity."""
    return f"{DOMAIN}_{entry_id}_event_{unit_id}"


def signal_new_rooms(entry_id: str) -> str:
    """Dispatcher signal: new rooms appeared (list of unit ids)."""
    return f"{DOMAIN}_{entry_id}_new_rooms"


def local_webhook_urls(hass: HomeAssistant, webhook_id: str) -> list[str]:
    """Internal (LAN) then external URL of the webhook, when configured."""
    path = webhook.async_generate_path(webhook_id)
    urls: list[str] = []
    for kwargs in (
        {"allow_external": False, "allow_cloud": False, "allow_ip": True},
        {"allow_internal": False, "allow_cloud": False},
    ):
        try:
            urls.append(get_url(hass, **kwargs) + path)
        except NoURLAvailableError:
            continue
    return list(dict.fromkeys(urls))


def cloud_subscribed(hass: HomeAssistant) -> bool:
    """Home Assistant Cloud is logged in with a live subscription."""
    if "cloud" not in hass.config.components:
        return False
    from homeassistant.components import cloud

    return cloud.async_active_subscription(hass)


async def async_cloudhook(hass: HomeAssistant, webhook_id: str) -> str | None:
    """The Nabu Casa cloudhook URL of the webhook, created when needed."""
    if not cloud_subscribed(hass):
        return None
    from homeassistant.components import cloud

    try:
        return await cloud.async_get_or_create_cloudhook(hass, webhook_id)
    except cloud.CloudNotAvailable:
        _LOGGER.debug("Home Assistant Cloud is not connected; no cloudhook yet")
        return None


async def async_webhook_urls(
    hass: HomeAssistant, webhook_id: str, cloudhook_url: str | None = None
) -> tuple[list[str], str | None]:
    """Every URL the PMS may use to reach the webhook, best first, and the cloudhook."""
    cloudhook_url = cloudhook_url or await async_cloudhook(hass, webhook_id)
    urls = local_webhook_urls(hass, webhook_id)
    if cloudhook_url:
        urls.append(cloudhook_url)
    return list(dict.fromkeys(urls)), cloudhook_url


async def async_delete_cloudhook(hass: HomeAssistant, webhook_id: str) -> None:
    """Best-effort removal of a cloudhook created for a webhook id."""
    if not cloud_subscribed(hass):
        return
    from homeassistant.components import cloud

    try:
        await cloud.async_delete_cloudhook(hass, webhook_id)
    except (cloud.CloudNotAvailable, ValueError):
        _LOGGER.debug("Cloudhook for the Tape Chart webhook not deleted")


def _json_response(payload: dict[str, Any], status: int = 200) -> web.Response:
    return web.json_response(payload, status=status)


def _unauthorized() -> web.Response:
    return web.Response(
        text=UNAUTHORIZED_BODY, status=401, content_type="application/json"
    )


class TapeChartHub:
    """Rooms, webhook, forwarding and the outbox of one connection."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: TapeChartClient
    ) -> None:
        """Initialize."""
        self.hass = hass
        self.entry = entry
        self.client = client
        self.rooms: dict[str, dict[str, Any]] = {}
        self.property_name: str = entry.data.get(CONF_PROPERTY_NAME) or entry.title
        self.property_id: str = entry.data[CONF_PROPERTY_ID]
        self.connection_id: str = entry.data[CONF_CONNECTION_ID]
        self.webhook_id: str = entry.data[CONF_WEBHOOK_ID]
        self.webhook_urls: list[str] = []
        self.mapping: dict[str, dict[str, str]] = {}
        self.disconnected = False
        self.last_webhook_at: str | None = None
        self.seen = SeenStore(hass, entry.entry_id)
        self._commands: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._refetch_lock = asyncio.Lock()
        self.outbox = Outbox(
            hass, entry.entry_id, client.async_post_events, self._async_auth_failed
        )
        self.forwarder = Forwarder(hass, self.outbox.async_add)
        self._apply_settings(entry.options)

    # -- life cycle --------------------------------------------------------

    async def async_setup(self, rooms_answer: dict[str, Any]) -> None:
        """Start: rooms are known; register the webhook; send health + mapping."""
        self._apply_rooms(rooms_answer, from_push=False)
        await self.outbox.async_load()
        await self.seen.async_load()
        webhook.async_register(
            self.hass,
            DOMAIN,
            f"Tape Chart · {self.property_name}",
            self.webhook_id,
            self.async_handle_webhook,
            local_only=False,
            allowed_methods=["POST"],
        )
        self.webhook_urls, cloudhook = await async_webhook_urls(
            self.hass, self.webhook_id, self.entry.data.get(CONF_CLOUDHOOK_URL)
        )
        self._store_cloudhook(cloudhook)
        if cloudhook is None and cloud_subscribed(self.hass):
            from homeassistant.components import cloud

            self.entry.async_on_unload(
                cloud.async_listen_connection_change(
                    self.hass, self._async_cloud_connection_changed
                )
            )
        self.outbox.async_add([self._health_event()])
        self.mapping = _clean_mapping(self.entry.options.get(OPT_MAPPING, {}))
        self.outbox.async_add(
            [self._mapping_event(unit) for unit in self.mapping if unit in self.rooms]
        )
        self.forwarder.async_set_mapping(
            self._live_mapping(), send_current=self._signal_keys(self._live_mapping())
        )
        self.entry.async_on_unload(
            self.hass.bus.async_listen(
                EVENT_CORE_CONFIG_UPDATE, self._async_core_config_updated
            )
        )
        ir.async_delete_issue(
            self.hass, DOMAIN, f"{ISSUE_CONNECTION}_{self.entry.entry_id}"
        )

    async def async_unload(self) -> None:
        """Stop everything; keep unsent events on disk."""
        webhook.async_unregister(self.hass, self.webhook_id)
        self.forwarder.async_shutdown()
        self.outbox.async_stop()
        await self.outbox.async_flush_to_disk()
        await self.seen.async_flush()

    @callback
    def _apply_settings(self, options: Any) -> None:
        self.forwarder.async_configure(
            options.get(OPT_PRESENCE_HOLD, DEFAULT_PRESENCE_HOLD),
            options.get(OPT_TEMP_DELTA, DEFAULT_TEMP_DELTA),
            options.get(OPT_TEMP_INTERVAL, DEFAULT_TEMP_INTERVAL),
        )

    async def async_options_updated(self) -> None:
        """Options changed: settings, and the mapping of the rooms that were touched."""
        self._apply_settings(self.entry.options)
        new = _clean_mapping(self.entry.options.get(OPT_MAPPING, {}))
        old = self.mapping
        if new == old:
            return
        touched = {u for u in set(old) | set(new) if old.get(u, {}) != new.get(u, {})}
        self.mapping = new
        self.outbox.async_add(
            [
                self._mapping_event(unit)
                for unit in sorted(touched)
                if unit in self.rooms
            ]
        )
        changed: set[tuple[str, str]] = set()
        for unit in touched:
            for role, entity_id in new.get(unit, {}).items():
                if role in SIGNAL_ROLES and old.get(unit, {}).get(role) != entity_id:
                    changed.add((unit, role))
        self.forwarder.async_set_mapping(self._live_mapping(), send_current=changed)

    def _live_mapping(self) -> dict[str, dict[str, str]]:
        return {u: r for u, r in self.mapping.items() if u in self.rooms}

    @staticmethod
    def _signal_keys(mapping: dict[str, dict[str, str]]) -> set[tuple[str, str]]:
        return {
            (u, r) for u, roles in mapping.items() for r in roles if r in SIGNAL_ROLES
        }

    @callback
    def _async_core_config_updated(self, _event: Event) -> None:
        self.entry.async_create_background_task(
            self.hass, self._async_refresh_urls(), "tapechart_refresh_urls"
        )

    async def _async_cloud_connection_changed(self, state: Any) -> None:
        """Cloud connected after startup: make the cloudhook, tell the PMS."""
        if getattr(state, "value", state) == "cloud_connected":
            await self._async_refresh_urls()

    @callback
    def _store_cloudhook(self, cloudhook: str | None) -> None:
        if cloudhook and cloudhook != self.entry.data.get(CONF_CLOUDHOOK_URL):
            self.hass.config_entries.async_update_entry(
                self.entry, data={**self.entry.data, CONF_CLOUDHOOK_URL: cloudhook}
            )

    async def _async_refresh_urls(self) -> None:
        urls, cloudhook = await async_webhook_urls(
            self.hass, self.webhook_id, self.entry.data.get(CONF_CLOUDHOOK_URL)
        )
        self._store_cloudhook(cloudhook)
        if urls != self.webhook_urls:
            self.webhook_urls = urls
            self.outbox.async_add([self._health_event()])

    # -- outgoing events ---------------------------------------------------

    def _health_event(self) -> dict[str, Any]:
        return {
            "id": str(uuid.uuid4()),
            "type": "health",
            "ha_version": HA_VERSION,
            "webhook_urls": list(self.webhook_urls),
        }

    def _entity_name(self, entity_id: str) -> str:
        if entity_id.startswith(DEVICE_PREFIX):
            device = dr.async_get(self.hass).async_get(entity_id[len(DEVICE_PREFIX) :])
            if device is not None:
                return device.name_by_user or device.name or entity_id
            return entity_id
        if (state := self.hass.states.get(entity_id)) is not None and state.name:
            return state.name
        if (reg := er.async_get(self.hass).async_get(entity_id)) is not None:
            return reg.name or reg.original_name or entity_id
        return entity_id

    def _wire_entity(self, role: str, entity_id: str) -> dict[str, str] | None:
        """The mapping entry the PMS accepts (§9 H1), or None to leave it out."""
        name = self._entity_name(entity_id)
        if entity_id.startswith(DEVICE_PREFIX):
            # `online` from device availability has no entity of its own; name a
            # binary_sensor of that device so the PMS can show where it comes from.
            members = sorted(
                e.entity_id
                for e in er.async_entries_for_device(
                    er.async_get(self.hass), entity_id[len(DEVICE_PREFIX) :]
                )
                if e.domain in ROLE_DOMAINS[role]
            )
            if not members:
                return None
            entity_id, name = members[0], f"{name} (availability)"
        if not _ENTITY_ID.match(entity_id) or entity_id.split(".", 1)[
            0
        ] not in ROLE_DOMAINS.get(role, ()):
            return None
        return {"entity_id": entity_id, "name": name[:120]}

    def _mapping_event(self, unit_id: str) -> dict[str, Any]:
        roles: dict[str, dict[str, str]] = {}
        for role, entity_id in self.mapping.get(unit_id, {}).items():
            if role in ROLE_DOMAINS and (wire := self._wire_entity(role, entity_id)):
                roles[role] = wire
        return {
            "id": str(uuid.uuid4()),
            "type": "mapping",
            "unit_id": unit_id,
            "roles": roles,
        }

    # -- connection loss ---------------------------------------------------

    @callback
    def _async_auth_failed(self) -> None:
        """401 or `disconnected`: stop sending and ask for a new pairing code."""
        if self.disconnected:
            return
        self.disconnected = True
        self.outbox.paused = True
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{ISSUE_CONNECTION}_{self.entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_CONNECTION,
            translation_placeholders={"property": self.property_name},
        )
        self.entry.async_start_reauth(self.hass)

    # -- rooms -------------------------------------------------------------

    async def async_refetch_rooms(self) -> None:
        """GET /rooms again (on `rooms_changed`)."""
        async with self._refetch_lock:
            try:
                answer = await self.client.async_get_rooms()
            except TapeChartAuthError:
                self._async_auth_failed()
                return
            except TapeChartError as err:
                _LOGGER.warning("Tape Chart: could not refresh the rooms: %s", err)
                return
            self._apply_rooms(answer, from_push=False)

    @callback
    def _apply_rooms(self, answer: dict[str, Any], *, from_push: bool) -> None:
        prop = answer.get("property")
        if (
            isinstance(prop, dict)
            and prop.get("name")
            and prop["name"] != self.property_name
        ):
            # Tolerant reader: /rooms may carry the property (renames).
            self.property_name = str(prop["name"])
            self.hass.config_entries.async_update_entry(
                self.entry,
                title=self.property_name,
                data={**self.entry.data, CONF_PROPERTY_NAME: self.property_name},
            )
            for unit_id in self.rooms:
                self._update_device(unit_id)
        seen: set[str] = set()
        added: list[str] = []
        for room in answer.get("rooms") or []:
            if not isinstance(room, dict) or not room.get("unit_id"):
                continue
            unit_id = str(room["unit_id"])
            seen.add(unit_id)
            if unit_id not in self.rooms:
                added.append(unit_id)
            self.async_update_room(room, from_push=from_push)
        removed = [u for u in self.rooms if u not in seen]
        for unit_id in removed:
            self.rooms.pop(unit_id)
            self._remove_device(unit_id)
        if added:
            async_dispatcher_send(
                self.hass, signal_new_rooms(self.entry.entry_id), added
            )
            if self.mapping and any(u in self.mapping for u in added):
                self.forwarder.async_set_mapping(
                    self._live_mapping(),
                    send_current=self._signal_keys(
                        {u: self.mapping[u] for u in added if u in self.mapping}
                    ),
                )
                self.outbox.async_add(
                    [self._mapping_event(u) for u in added if u in self.mapping]
                )
        elif removed and self.mapping:
            self.forwarder.async_set_mapping(self._live_mapping())

    @callback
    def async_update_room(self, room: dict[str, Any], *, from_push: bool) -> bool:
        """Apply one room_state; False when it is not newer than what we hold."""
        unit_id = str(room.get("unit_id"))
        if room.get("property_id") and str(room["property_id"]) != self.property_id:
            return False
        held = self.rooms.get(unit_id)
        try:
            version = int(room.get("version", 0))
        except (TypeError, ValueError):
            return False
        if held is not None and version <= int(held.get("version", 0)):
            return False
        is_new = held is None
        self.rooms[unit_id] = room
        if is_new and from_push:
            # A room we did not know: let the platforms add it.
            async_dispatcher_send(
                self.hass, signal_new_rooms(self.entry.entry_id), [unit_id]
            )
            return True
        if held is not None and (
            held.get("number") != room.get("number")
            or held.get("room_type") != room.get("room_type")
        ):
            self._update_device(unit_id)
        async_dispatcher_send(self.hass, signal_room(self.entry.entry_id, unit_id))
        if from_push and held is not None:
            events = [e for e in room.get("events") or [] if e in STAY_EVENTS]
            if not events:
                events = _transition_events(held.get("status"), room.get("status"))
            if events:
                async_dispatcher_send(
                    self.hass, signal_stay_event(self.entry.entry_id, unit_id), events
                )
        return True

    def device_name(self, unit_id: str) -> str:
        """`<property> · <number>`."""
        number = (self.rooms.get(unit_id) or {}).get("number") or unit_id
        return f"{self.property_name} · {number}"

    def _room_device(
        self, registry: dr.DeviceRegistry, unit_id: str
    ) -> dr.DeviceEntry | None:
        """This entry's device for a room (no identifier lookup across entries)."""
        for device in dr.async_entries_for_config_entry(registry, self.entry.entry_id):
            if (DOMAIN, unit_id) in device.identifiers:
                return device
        return None

    @callback
    def _update_device(self, unit_id: str) -> None:
        registry = dr.async_get(self.hass)
        device = self._room_device(registry, unit_id)
        if device is None:
            return
        registry.async_update_device(
            device.id,
            name=self.device_name(unit_id),
            model=(self.rooms.get(unit_id) or {}).get("room_type"),
        )

    @callback
    def _remove_device(self, unit_id: str) -> None:
        registry = dr.async_get(self.hass)
        device = self._room_device(registry, unit_id)
        if device is not None:
            registry.async_update_device(
                device.id, remove_config_entry_id=self.entry.entry_id
            )

    # -- webhook (contract §5) ---------------------------------------------

    async def async_handle_webhook(
        self, hass: HomeAssistant, webhook_id: str, request: web.Request
    ) -> web.Response:
        """Verify, de-duplicate and dispatch one PMS message."""
        if isinstance(request, web.Request):
            body = await request.read()
        else:  # MockRequest from a Nabu Casa cloudhook
            body = await request.content.read()
        if len(body) > MAX_WEBHOOK_BODY:
            return _json_response({"ok": False, "error": "bad_request"}, 413)
        headers = request.headers
        if headers.get(HEADER_CONNECTION) != self.connection_id:
            return _unauthorized()
        message_id = headers.get(HEADER_ID) or ""
        if not verify_signature(
            self.entry.data[CONF_SECRET],
            headers.get(HEADER_SIGNATURE),
            message_id,
            request.method,
            PATH_WEBHOOK,
            body,
            dt_util.utcnow().timestamp(),
            SIGNATURE_TOLERANCE,
        ):
            return _unauthorized()
        if (cached := self.seen.message(message_id)) is not None:
            return _json_response(cached)
        try:
            message = json.loads(body)
        except ValueError:
            return _json_response({"ok": False, "error": "bad_request"}, 400)
        if not isinstance(message, dict):
            return _json_response({"ok": False, "error": "bad_request"}, 400)

        self.last_webhook_at = dt_util.utcnow().isoformat()
        try:
            answer = await self._async_dispatch(message)
        except Exception:
            # HA would answer 200 for a crashed handler; the PMS must retry.
            _LOGGER.exception(
                "Tape Chart: error handling a %s message", message.get("type")
            )
            return _json_response({"ok": False, "error": "internal_error"}, 500)
        if answer is None:
            return _json_response({"ok": False, "error": "bad_request"}, 400)
        self.seen.remember_message(message_id, answer)
        return _json_response(answer)

    async def _async_dispatch(self, message: dict[str, Any]) -> dict[str, Any] | None:
        kind = message.get("type")
        if kind == "ping":
            return {"ok": True, "nonce": message.get("nonce")}
        if kind == "room_state":
            room = message.get("room")
            if not isinstance(room, dict) or not room.get("unit_id"):
                return None
            self.async_update_room(room, from_push=True)
            return {"ok": True}
        if kind == "command":
            return await self._async_command(message)
        if kind == "rooms_changed":
            self.entry.async_create_background_task(
                self.hass, self.async_refetch_rooms(), "tapechart_refetch_rooms"
            )
            return {"ok": True}
        if kind == "disconnected":
            self._async_auth_failed()
            return {"ok": True}
        return None

    async def _async_command(self, message: dict[str, Any]) -> dict[str, Any]:
        """Run a command at most once per command_id (also across restarts)."""
        command_id = str(message.get("command_id") or "")
        if not command_id:
            return {"ok": False, "error": "failed"}
        if (done := self.seen.command(command_id)) is not None:
            return done
        if (running := self._commands.get(command_id)) is not None:
            return await asyncio.shield(running)
        future: asyncio.Future[dict[str, Any]] = self.hass.loop.create_future()
        self._commands[command_id] = future
        try:
            answer = await self._async_run_command(message)
            self.seen.remember_command(command_id, answer)
            future.set_result(answer)
            return answer
        except BaseException as err:
            future.set_exception(err)
            future.exception()  # retrieved: no "never retrieved" warning
            raise
        finally:
            self._commands.pop(command_id, None)

    async def _async_run_command(self, message: dict[str, Any]) -> dict[str, Any]:
        unit_id = str(message.get("unit_id") or "")
        scenario = message.get("scenario")
        if unit_id not in self.rooms:
            return {"ok": False, "error": "unknown_room"}
        if scenario not in SCENARIOS:
            return {"ok": False, "error": "not_mapped"}
        entity_id = self.mapping.get(unit_id, {}).get(f"scenario_{scenario}")
        if not entity_id:
            return {"ok": False, "error": "not_mapped"}
        domain = entity_id.split(".", 1)[0]
        if domain not in ("script", "scene") or self.hass.states.get(entity_id) is None:
            return {"ok": False, "error": "failed"}
        try:
            async with asyncio.timeout(COMMAND_TIMEOUT):
                await self.hass.services.async_call(
                    domain, "turn_on", {"entity_id": entity_id}, blocking=True
                )
        except (HomeAssistantError, TimeoutError, ValueError) as err:
            _LOGGER.warning("Tape Chart %s for %s failed: %s", scenario, entity_id, err)
            return {"ok": False, "error": "failed"}
        _LOGGER.info(
            "Tape Chart ran %s (%s) for %s",
            scenario,
            entity_id,
            self.device_name(unit_id),
        )
        return {"ok": True}


def _transition_events(old: Any, new: Any) -> list[str]:
    """Stay events implied by a status change when the PMS named none."""
    if old == new:
        return []
    if new == "occupied" and old not in ("occupied", "departing"):
        return ["checked_in"]
    if new == "departed" and old in ("occupied", "departing"):
        return ["checked_out"]
    return []


def _clean_mapping(raw: Any) -> dict[str, dict[str, str]]:
    """Mapping from options, without empty values."""
    result: dict[str, dict[str, str]] = {}
    if not isinstance(raw, dict):
        return result
    for unit_id, roles in raw.items():
        if not isinstance(roles, dict):
            continue
        clean = {str(r): str(e) for r, e in roles.items() if e}
        if clean:
            result[str(unit_id)] = clean
    return result
