"""Config flow (pairing with a code), reauth (a new code) and the room mapping."""

from __future__ import annotations

from collections.abc import Mapping
import ipaddress
import json
import logging
import re
import secrets
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web
from homeassistant.components import webhook
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    instance_id,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    DeviceSelector,
    DeviceSelectorConfig,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .api import (
    TapeChartClient,
    TapeChartConnectionError,
    TapeChartError,
    async_pair,
    normalize_code,
    normalize_url,
)
from .const import (
    CONF_CLOUDHOOK_URL,
    CONF_CODE,
    CONF_CONNECTION_ID,
    CONF_PMS_URL,
    CONF_PROPERTY_ID,
    CONF_PROPERTY_NAME,
    CONF_PROPERTY_TIMEZONE,
    CONF_SECRET,
    CONF_WEBHOOK_ID,
    CONF_WEBHOOK_URL,
    DEFAULT_PRESENCE_HOLD,
    DEFAULT_TEMP_DELTA,
    DEFAULT_TEMP_INTERVAL,
    DEVICE_PREFIX,
    DOMAIN,
    HEADER_ID,
    HEADER_SIGNATURE,
    MAX_WEBHOOK_BODY,
    OPT_DEVICES,
    OPT_MAPPING,
    OPT_PRESENCE_HOLD,
    OPT_TEMP_DELTA,
    OPT_TEMP_INTERVAL,
    ROLE_DOMAINS,
    ROLES,
    SIGNATURE_TOLERANCE,
)
from .hub import async_delete_cloudhook, async_webhook_urls
from .mapping import EntityInfo, Proposal, Target, fill_by_pattern, number_in_name
from .signing import PATH_WEBHOOK, verify_signature

_LOGGER = logging.getLogger(__name__)

ONLINE_FROM_DEVICE = "online_from_device"
ONLY_DEVICE = "only_device"

PAIR_ERRORS = {
    "invalid_code": "invalid_code",
    "code_expired": "code_expired",
    "unreachable": "unreachable",
    "rate_limited": "rate_limited",
    "bad_request": "invalid_code",
    "not_found": "invalid_code",
}


def _pairing_handler(probe_token: str) -> Any:
    """The webhook handler while pairing (§9 H5).

    The PMS probes the webhook URLs with a `ping` signed with OUR probe_token
    before it answers /pair. Only a correctly signed ping is answered; anything
    else, and anything after the flow step, is refused.
    """

    async def _handle(
        hass: HomeAssistant, webhook_id: str, request: web.Request
    ) -> web.Response:
        if isinstance(request, web.Request):
            body = await request.read()
        else:  # MockRequest from a Nabu Casa cloudhook
            body = await request.content.read()
        message_id = request.headers.get(HEADER_ID) or ""
        message: Any = None
        if len(body) <= MAX_WEBHOOK_BODY and verify_signature(
            probe_token,
            request.headers.get(HEADER_SIGNATURE),
            message_id,
            request.method,
            PATH_WEBHOOK,
            body,
            dt_util.utcnow().timestamp(),
            SIGNATURE_TOLERANCE,
        ):
            try:
                message = json.loads(body)
            except ValueError:
                message = None
        if isinstance(message, dict) and message.get("type") == "ping":
            return web.json_response({"ok": True, "nonce": message.get("nonce")})
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

    return _handle


def _lan_host(host: str | None) -> bool:
    """A private-range address or a LAN-only name (http allowed there only)."""
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        name = host.lower().rstrip(".")
        return "." not in name or name.endswith((".local", ".lan", ".home.arpa"))
    return ip.is_private or ip.is_loopback or ip.is_link_local


def _all_devices(registry: dr.DeviceRegistry) -> list[dr.DeviceEntry]:
    """Every device; iterating the registry yields entries from HA 2026.x, ids before."""
    items = list(registry.devices)
    if items and isinstance(items[0], str):
        return [registry.devices[i] for i in items]
    return items


def _natural(number: str) -> tuple[Any, ...]:
    return tuple(int(p) if p.isdigit() else p for p in re.split(r"(\d+)", number))


class TapeChartConfigFlow(ConfigFlow, domain=DOMAIN):
    """Pair Home Assistant with one property of a Tape Chart PMS."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        """Initialize."""
        self._webhook_id: str | None = None
        self._paired = False

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """The room mapping and settings."""
        return TapeChartOptionsFlow()

    @callback
    def async_remove(self) -> None:
        """Flow ended without pairing: drop the cloudhook made for it."""
        if self._webhook_id and not self._paired:
            self.hass.async_create_task(
                async_delete_cloudhook(self.hass, self._webhook_id)
            )

    def _schema(self, defaults: Mapping[str, Any]) -> vol.Schema:
        return self.add_suggested_values_to_schema(
            vol.Schema(
                {
                    vol.Required(CONF_PMS_URL): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.URL)
                    ),
                    vol.Required(CONF_CODE): TextSelector(),
                }
            ),
            defaults,
        )

    async def _async_pair(
        self, user_input: dict[str, Any], errors: dict[str, str]
    ) -> dict[str, Any] | None:
        """Run /pair; fill `errors` and return None on failure."""
        try:
            pms_url = normalize_url(user_input[CONF_PMS_URL])
        except ValueError:
            errors[CONF_PMS_URL] = "invalid_url"
            return None
        parts = urlsplit(pms_url)
        if parts.scheme != "https" and not _lan_host(parts.hostname):
            errors[CONF_PMS_URL] = "insecure_url"
            return None
        if self._webhook_id is None:
            self._webhook_id = webhook.async_generate_id()
        urls, cloudhook = await async_webhook_urls(self.hass, self._webhook_id)
        if not urls:
            errors["base"] = "no_ha_url"
            return None
        probe_token = secrets.token_hex(32)
        webhook.async_register(
            self.hass,
            DOMAIN,
            "Tape Chart pairing",
            self._webhook_id,
            _pairing_handler(probe_token),
            local_only=False,
            allowed_methods=["POST"],
        )
        try:
            answer = await async_pair(
                async_get_clientsession(self.hass),
                pms_url,
                normalize_code(user_input[CONF_CODE]),
                {
                    "instance_id": await instance_id.async_get(self.hass),
                    "version": HA_VERSION,
                    "location_name": self.hass.config.location_name,
                    "webhook_urls": urls,
                    "probe_token": probe_token,
                },
            )
        except TapeChartConnectionError:
            errors["base"] = "cannot_connect"
            return None
        except TapeChartError as err:
            errors["base"] = PAIR_ERRORS.get(err.slug, "unknown")
            return None
        except Exception:
            _LOGGER.exception("Unexpected error while pairing with Tape Chart")
            errors["base"] = "unknown"
            return None
        finally:
            webhook.async_unregister(self.hass, self._webhook_id)

        prop = answer.get("property") or {}
        if not prop.get("id"):
            errors["base"] = "unknown"
            return None
        self._paired = True
        return {
            CONF_PMS_URL: pms_url,
            CONF_CONNECTION_ID: str(answer["connection_id"]),
            CONF_SECRET: str(answer["secret"]),
            CONF_WEBHOOK_ID: self._webhook_id,
            CONF_WEBHOOK_URL: answer.get("webhook_url"),
            CONF_CLOUDHOOK_URL: cloudhook,
            CONF_PROPERTY_ID: str(prop["id"]),
            CONF_PROPERTY_NAME: str(prop.get("name") or prop["id"]),
            CONF_PROPERTY_TIMEZONE: prop.get("timezone"),
        }

    async def _async_retire_webhook(
        self, entry: ConfigEntry, data: dict[str, Any]
    ) -> None:
        """The entry gets a new webhook id: drop the old cloudhook."""
        old = entry.data.get(CONF_WEBHOOK_ID)
        if old and old != data[CONF_WEBHOOK_ID]:
            await async_delete_cloudhook(self.hass, old)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """PMS address + the pairing code from Settings → Integrations → Home Assistant."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data = await self._async_pair(user_input, errors)
            if data is not None:
                await self.async_set_unique_id(data[CONF_PROPERTY_ID])
                existing = self.hass.config_entries.async_entry_for_domain_unique_id(
                    DOMAIN, data[CONF_PROPERTY_ID]
                )
                if existing is not None:
                    # The PMS replaced its connection; follow it.
                    await self._async_retire_webhook(existing, data)
                # The PMS replaced its connection: the entry follows it.
                self._abort_if_unique_id_configured(updates=data, error="repaired")
                return self.async_create_entry(
                    title=data[CONF_PROPERTY_NAME], data=data
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self._schema(user_input or {}),
            errors=errors,
        )

    async def _async_wrong_property(self, data: dict[str, Any]) -> ConfigFlowResult:
        """A reauth answered with a code for ANOTHER property.

        The PMS has already replaced that property's connection with this one, so
        it must not be dropped: the entry of that property takes it over. Without
        such an entry the new connection is given back (unpaired).
        """
        other = self.hass.config_entries.async_entry_for_domain_unique_id(
            DOMAIN, data[CONF_PROPERTY_ID]
        )
        if other is not None:
            await self._async_retire_webhook(other, data)
            self.hass.config_entries.async_update_entry(
                other, title=data[CONF_PROPERTY_NAME], data={**other.data, **data}
            )
            self.hass.config_entries.async_schedule_reload(other.entry_id)
            return self.async_abort(
                reason="other_property_updated",
                description_placeholders={"property": data[CONF_PROPERTY_NAME]},
            )
        try:
            await TapeChartClient(
                async_get_clientsession(self.hass),
                data[CONF_PMS_URL],
                data[CONF_CONNECTION_ID],
                data[CONF_SECRET],
            ).async_unpair()
        except TapeChartError:
            _LOGGER.warning("Tape Chart: the unused connection was not unpaired")
        await async_delete_cloudhook(self.hass, data[CONF_WEBHOOK_ID])
        return self.async_abort(reason="wrong_property")

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """The PMS refused us or disconnected: pair again with a new code."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new pairing code."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = await self._async_pair(user_input, errors)
            if data is not None:
                if data[CONF_PROPERTY_ID] != entry.unique_id:
                    return await self._async_wrong_property(data)
                await self._async_retire_webhook(entry, data)
                return self.async_update_reload_and_abort(
                    entry, title=data[CONF_PROPERTY_NAME], data_updates=data
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self._schema(
                user_input or {CONF_PMS_URL: entry.data.get(CONF_PMS_URL)}
            ),
            errors=errors,
            description_placeholders={"property": entry.title},
        )


class TapeChartOptionsFlow(OptionsFlow):
    """Map HA entities to rooms and roles; settings for forwarding."""

    def __init__(self) -> None:
        """Initialize."""
        self._unit_id: str | None = None
        self._device_id: str | None = None
        self._only_device = True
        self._proposal: dict[str, Proposal] = {}
        self._proposal_devices: dict[str, str] = {}

    # -- helpers -----------------------------------------------------------

    @property
    def _rooms(self) -> dict[str, dict[str, Any]]:
        hub = getattr(self.config_entry, "runtime_data", None)
        return dict(hub.rooms) if hub is not None else {}

    @property
    def _mapping(self) -> dict[str, dict[str, str]]:
        return {
            unit: dict(roles)
            for unit, roles in (
                self.config_entry.options.get(OPT_MAPPING) or {}
            ).items()
        }

    @property
    def _devices(self) -> dict[str, str]:
        return dict(self.config_entry.options.get(OPT_DEVICES) or {})

    def _number(self, unit_id: str) -> str:
        return str(self._rooms.get(unit_id, {}).get("number") or unit_id)

    def _save(self, **changes: Any) -> ConfigFlowResult:
        options = {
            OPT_PRESENCE_HOLD: DEFAULT_PRESENCE_HOLD,
            OPT_TEMP_DELTA: DEFAULT_TEMP_DELTA,
            OPT_TEMP_INTERVAL: DEFAULT_TEMP_INTERVAL,
            OPT_MAPPING: {},
            OPT_DEVICES: {},
            **self.config_entry.options,
            **changes,
        }
        return self.async_create_entry(data=options)

    def _room_options(self, unit_ids: list[str]) -> list[SelectOptionDict]:
        mapping = self._mapping
        rooms = self._rooms
        result = []
        for unit_id in sorted(unit_ids, key=lambda u: _natural(self._number(u))):
            room = rooms.get(unit_id, {})
            label = self._number(unit_id)
            if room.get("room_type"):
                label += f" · {room['room_type']}"
            if mapping.get(unit_id):
                label += f" · ✓ {len(mapping[unit_id])}"
            result.append(SelectOptionDict(value=unit_id, label=label))
        return result

    def _device_entities(self, device_id: str | None) -> list[EntityInfo]:
        if not device_id:
            return []
        return [
            EntityInfo(
                entity_id=e.entity_id,
                unique_id=e.unique_id,
                original_name=e.original_name,
                translation_key=e.translation_key,
            )
            for e in er.async_entries_for_device(er.async_get(self.hass), device_id)
            if e.disabled_by is None
        ]

    def _guess_device(self, number: str, like: str | None = None) -> str | None:
        """The one device whose name carries the room number ("Room 205").

        With `like` (the source room's device), only devices of the same
        integration are considered.
        """
        registry = dr.async_get(self.hass)
        entries: set[str] | None = None
        if like and (source := registry.async_get(like)) is not None:
            entries = set(source.config_entries)
        found = [
            device.id
            for device in _all_devices(registry)
            if device.disabled_by is None
            and self.config_entry.entry_id not in device.config_entries
            and (entries is None or entries & set(device.config_entries))
            and number_in_name(device.name_by_user or device.name, number)
        ]
        return found[0] if len(found) == 1 else None

    # -- steps -------------------------------------------------------------

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Menu."""
        if not self._rooms:
            return self.async_abort(reason="not_loaded")
        return self.async_show_menu(
            step_id="init", menu_options=["map_room", "fill_pattern", "settings"]
        )

    async def async_step_map_room(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a room."""
        if user_input is not None:
            self._unit_id = user_input["room"]
            return await self.async_step_map_device()
        schema = vol.Schema(
            {
                vol.Required("room"): SelectSelector(
                    SelectSelectorConfig(
                        options=self._room_options(list(self._rooms)),
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="map_room", data_schema=schema)

    async def async_step_map_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Optionally the device that serves the room (narrows the next lists)."""
        assert self._unit_id is not None
        if user_input is not None:
            self._device_id = user_input.get("device") or None
            self._only_device = bool(user_input.get(ONLY_DEVICE, True))
            return await self.async_step_map_entities()
        number = self._number(self._unit_id)
        suggested = self._devices.get(self._unit_id) or self._guess_device(number)
        schema = vol.Schema(
            {
                vol.Optional(
                    "device", description={"suggested_value": suggested}
                ): DeviceSelector(DeviceSelectorConfig()),
                vol.Optional(ONLY_DEVICE, default=True): BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="map_device",
            data_schema=schema,
            description_placeholders={"room": number},
        )

    async def async_step_map_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """One entity per role, each filtered by the domains the role allows."""
        assert self._unit_id is not None
        current = self._mapping.get(self._unit_id, {})
        if user_input is not None:
            roles = {role: user_input[role] for role in ROLES if user_input.get(role)}
            if self._device_id and user_input.get(ONLINE_FROM_DEVICE):
                roles["online"] = DEVICE_PREFIX + self._device_id
            mapping = self._mapping
            devices = self._devices
            if roles:
                mapping[self._unit_id] = roles
            else:
                mapping.pop(self._unit_id, None)
            if self._device_id:
                devices[self._unit_id] = self._device_id
            else:
                devices.pop(self._unit_id, None)
            return self._save(**{OPT_MAPPING: mapping, OPT_DEVICES: devices})

        device_entities = (
            [e.entity_id for e in self._device_entities(self._device_id)]
            if self._only_device
            else []
        )
        online_from_device = str(current.get("online", "")).startswith(DEVICE_PREFIX)
        fields: dict[Any, Any] = {}
        for role, domains in ROLE_DOMAINS.items():
            config = EntitySelectorConfig(domain=list(domains))
            narrowed = [e for e in device_entities if e.split(".", 1)[0] in domains]
            value = current.get(role)
            if value and value.startswith(DEVICE_PREFIX):
                value = None
            if narrowed:
                if value and value not in narrowed:
                    narrowed.append(value)
                config["include_entities"] = narrowed
            fields[vol.Optional(role, description={"suggested_value": value})] = (
                EntitySelector(config)
            )
            if role == "online" and self._device_id:
                fields[vol.Optional(ONLINE_FROM_DEVICE, default=online_from_device)] = (
                    BooleanSelector()
                )
        return self.async_show_form(
            step_id="map_entities",
            data_schema=vol.Schema(fields),
            description_placeholders={"room": self._number(self._unit_id)},
        )

    async def async_step_fill_pattern(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Copy one room's mapping to the others."""
        mapping = self._mapping
        rooms = self._rooms
        sources = [u for u in mapping if u in rooms]
        if not sources:
            return self.async_abort(reason="no_mapped_rooms")
        errors: dict[str, str] = {}
        if user_input is not None:
            self._build_proposal(
                user_input["source"], overwrite=bool(user_input.get("overwrite"))
            )
            if self._proposal:
                return await self.async_step_fill_confirm()
            errors["base"] = "nothing_found"
        schema = vol.Schema(
            {
                vol.Required("source"): SelectSelector(
                    SelectSelectorConfig(
                        options=self._room_options(sources),
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional("overwrite", default=False): BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="fill_pattern",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or {}),
            errors=errors,
        )

    def _build_proposal(self, source: str, *, overwrite: bool) -> None:
        mapping = self._mapping
        devices = self._devices
        source_roles = dict(mapping[source])
        source_device = devices.get(source)
        online_from_device = str(source_roles.get("online", "")).startswith(
            DEVICE_PREFIX
        )
        if online_from_device:
            source_roles.pop("online")
        targets: dict[str, Target] = {}
        target_devices: dict[str, str] = {}
        for unit in self._rooms:
            if unit == source:
                continue
            number = self._number(unit)
            device = devices.get(unit)
            if device is None and source_device:
                device = self._guess_device(number, like=source_device)
            if device:
                target_devices[unit] = device
            targets[unit] = Target(
                number=number,
                roles=mapping.get(unit, {}),
                device_entities=self._device_entities(device) or None,
            )
        self._proposal = fill_by_pattern(
            self._number(source),
            source_roles,
            self._device_entities(source_device) or None,
            targets,
            lambda entity_id: self.hass.states.get(entity_id) is not None,
            overwrite=overwrite,
        )
        if online_from_device:
            # The derived `online` follows each target's own device.
            for unit, device in target_devices.items():
                if overwrite or not targets[unit].roles.get("online"):
                    self._proposal.setdefault(unit, Proposal()).roles["online"] = (
                        DEVICE_PREFIX + device
                    )
        self._proposal_devices = {
            unit: device
            for unit, device in target_devices.items()
            if unit in self._proposal and unit not in devices
        }

    async def async_step_fill_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show what was found and what was not; save on confirm."""
        if user_input is not None:
            mapping = self._mapping
            for unit_id, proposal in self._proposal.items():
                mapping[unit_id] = {**mapping.get(unit_id, {}), **proposal.roles}
            devices = {**self._devices, **self._proposal_devices}
            return self._save(**{OPT_MAPPING: mapping, OPT_DEVICES: devices})
        lines = []
        for unit_id in sorted(self._proposal, key=lambda u: _natural(self._number(u))):
            proposal = self._proposal[unit_id]
            lines.append(f"**{self._number(unit_id)}**")
            lines.extend(
                f"- {role}: `{entity_id}`" for role, entity_id in proposal.roles.items()
            )
            if proposal.missing:
                lines.append(f"- ✗ {', '.join(proposal.missing)}")
        return self.async_show_form(
            step_id="fill_confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "rooms": str(len(self._proposal)),
                "result": "\n".join(lines),
            },
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Presence hold and temperature debounce."""
        if user_input is not None:
            return self._save(**user_input)
        options = self.config_entry.options

        def number(key: str, default: float, top: float, step: float, unit: str) -> Any:
            return {
                vol.Required(key, default=options.get(key, default)): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        max=top,
                        step=step,
                        unit_of_measurement=unit,
                        mode=NumberSelectorMode.BOX,
                    )
                )
            }

        schema = vol.Schema(
            {
                **number(OPT_PRESENCE_HOLD, DEFAULT_PRESENCE_HOLD, 120, 1, "min"),
                **number(OPT_TEMP_DELTA, DEFAULT_TEMP_DELTA, 5, 0.1, "°"),
                **number(OPT_TEMP_INTERVAL, DEFAULT_TEMP_INTERVAL, 3600, 1, "s"),
            }
        )
        return self.async_show_form(step_id="settings", data_schema=schema)
