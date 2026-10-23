"""Constants for the Tape Chart integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "tapechart"
MANUFACTURER: Final = "Tape Chart"

# Config entry data keys.
CONF_PMS_URL: Final = "pms_url"
CONF_CODE: Final = "code"
CONF_CONNECTION_ID: Final = "connection_id"
CONF_SECRET: Final = "secret"
CONF_WEBHOOK_ID: Final = "webhook_id"
CONF_WEBHOOK_URL: Final = "webhook_url"
CONF_PROPERTY_ID: Final = "property_id"
CONF_PROPERTY_NAME: Final = "property_name"
CONF_PROPERTY_TIMEZONE: Final = "property_timezone"

# Options keys.
OPT_MAPPING: Final = "mapping"
OPT_DEVICES: Final = "devices"
OPT_PRESENCE_HOLD: Final = "presence_hold_minutes"
OPT_TEMP_DELTA: Final = "temperature_min_delta"
OPT_TEMP_INTERVAL: Final = "temperature_min_interval"

DEFAULT_PRESENCE_HOLD: Final = 5
DEFAULT_TEMP_DELTA: Final = 0.5
DEFAULT_TEMP_INTERVAL: Final = 60

# Wire (contract §3 – §5).
HEADER_SIGNATURE: Final = "X-TapeChart-Signature"
HEADER_ID: Final = "X-TapeChart-Id"
HEADER_CONNECTION: Final = "X-TapeChart-Connection"
SIGNATURE_TOLERANCE: Final = 300
DEDUPE_SECONDS: Final = 15 * 60
MAX_WEBHOOK_BODY: Final = 256 * 1024
MAX_BATCH: Final = 100
MAX_OUTBOX: Final = 5000

ROOM_STATUSES: Final = [
    "vacant",
    "arriving",
    "occupied",
    "departing",
    "departed",
    "out_of_service",
]

STAY_EVENTS: Final = [
    "checked_in",
    "checked_out",
    "moved_in",
    "moved_out",
    "stay_changed",
    "cancelled",
]

SCENARIOS: Final = ("welcome", "eco", "off")

# Role -> HA domains allowed (contract §1). Order is the order of the mapping form.
ROLE_DOMAINS: Final[dict[str, tuple[str, ...]]] = {
    "presence": ("binary_sensor", "input_boolean"),
    "keycard": ("binary_sensor", "input_boolean", "switch"),
    "door": ("binary_sensor",),
    "window": ("binary_sensor",),
    "dnd": ("binary_sensor", "input_boolean", "switch"),
    "mur": ("binary_sensor", "input_boolean", "switch"),
    "climate": ("climate",),
    "temperature": ("sensor",),
    "online": ("binary_sensor",),
    "alarm": ("binary_sensor",),
    "scenario_welcome": ("script", "scene"),
    "scenario_eco": ("script", "scene"),
    "scenario_off": ("script", "scene"),
}

ROLES: Final = tuple(ROLE_DOMAINS)
SIGNAL_ROLES: Final = tuple(r for r in ROLES if not r.startswith("scenario_"))
BINARY_ROLES: Final = frozenset(
    {"presence", "keycard", "door", "window", "dnd", "mur", "online", "alarm"}
)

STORAGE_VERSION: Final = 1

# `online` may be derived from a device's availability instead of an entity:
# the mapping then holds "device:<device_id>" for the role.
DEVICE_PREFIX: Final = "device:"
CONF_CLOUDHOOK_URL: Final = "cloudhook_url"
