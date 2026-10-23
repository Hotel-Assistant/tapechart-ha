"""Pure mapping helpers: state normalization (contract §1) and fill-by-pattern."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import math
import os
import re
from typing import Any

from .const import BINARY_ROLES, ROLE_DOMAINS

UNAVAILABLE = "unavailable"
_MISSING_STATES = frozenset({"unavailable", "unknown", "none", ""})


def _number(value: Any) -> float | None:
    """Return a finite float or None."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def decimal_string(value: Any) -> str | None:
    """Render a number as a plain decimal string ("25.5", "21", "-3.25")."""
    if _number(value) is None:
        return None
    try:
        dec = Decimal(str(value).strip()).normalize()
    except InvalidOperation:
        return None
    text = format(dec, "f")
    text = "0" if text in ("-0", "") else text
    return text if len(text) <= 16 else None


def normalize_state(
    role: str, state: str | None, attributes: Mapping[str, Any] | None
) -> tuple[str, dict[str, Any]]:
    """Normalize an HA state to the wire `state` + `attributes` for a role."""
    attributes = attributes or {}
    raw = (state or "").strip()
    missing = raw.lower() in _MISSING_STATES

    if role in BINARY_ROLES:
        attrs: dict[str, Any] = {}
        if role == "alarm":
            attrs["device_class"] = attributes.get("device_class")
        if missing or raw not in ("on", "off"):
            return UNAVAILABLE, attrs
        return raw, attrs

    if role == "climate":
        attrs = {
            "current_temperature": _number(attributes.get("current_temperature")),
            "temperature": _number(attributes.get("temperature")),
        }
        if missing:
            return UNAVAILABLE, attrs
        return raw, attrs

    if role == "temperature":
        if missing:
            return UNAVAILABLE, {}
        text = decimal_string(raw)
        return (text if text is not None else UNAVAILABLE), {}

    raise ValueError(f"role {role} is not a signal")


def _number_pattern(number: str) -> re.Pattern[str]:
    """Match a room number as a whole token inside an entity id."""
    token = re.escape(number)
    if number.isdigit():
        # room_205_pir, room205pir: bounded by non-digits only.
        return re.compile(rf"(?<![0-9]){token}(?![0-9])")
    return re.compile(rf"(?<![0-9a-z]){token}(?![0-9a-z])")


def number_slug(number: str) -> str:
    """Room number as it appears inside entity ids ("A-1" -> "a_1")."""
    slug = re.sub(r"[^0-9a-z]+", "_", number.strip().lower()).strip("_")
    return slug


def substitute_number(entity_id: str, source: str, target: str) -> str | None:
    """Swap the source room number for the target's; None if it is not there."""
    src, dst = number_slug(source), number_slug(target)
    if not src or not dst or src == dst:
        return None
    domain, sep, object_id = entity_id.partition(".")
    if not sep:
        return None
    new_object_id, count = _number_pattern(src).subn(dst, object_id)
    if count == 0:
        return None
    return f"{domain}.{new_object_id}"


@dataclass(frozen=True)
class EntityInfo:
    """What the matcher needs to know about one entity of a device."""

    entity_id: str
    unique_id: str | None = None
    original_name: str | None = None
    translation_key: str | None = None

    @property
    def domain(self) -> str:
        """Entity domain."""
        return self.entity_id.split(".", 1)[0]


@dataclass
class Target:
    """A room to fill: its number, current roles and (optional) device entities."""

    number: str
    roles: Mapping[str, str]
    device_entities: list[EntityInfo] | None = None


@dataclass
class Proposal:
    """What fill-by-pattern found for one room."""

    roles: dict[str, str] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)


_SEPARATORS = "-_:."


def device_prefix(entities: list[EntityInfo]) -> str:
    """The part every unique_id of one device starts with (ESPHome: the MAC + '-')."""
    ids = [e.unique_id for e in entities if e.unique_id]
    if len(ids) < 2:
        return ""
    prefix = os.path.commonprefix(ids)  # noqa: RUF071 - character prefix wanted
    # Cut back to a separator so a shared word start is not eaten.
    while prefix and prefix[-1] not in _SEPARATORS:
        prefix = prefix[:-1]
    return prefix


def match_in_device(
    source: EntityInfo,
    source_prefix: str,
    targets: list[EntityInfo],
    target_prefix: str,
) -> str | None:
    """The target device's counterpart of a source entity, or None.

    In order: the unique_id with the device's own prefix removed, the original
    name, the translation key, each only among entities of the same domain and
    only when exactly one entity matches.
    """
    same_domain = [t for t in targets if t.domain == source.domain]

    def _one(pred: Callable[[EntityInfo], bool]) -> str | None:
        found = [t.entity_id for t in same_domain if pred(t)]
        return found[0] if len(found) == 1 else None

    if source.unique_id and source_prefix and target_prefix:
        suffix = source.unique_id[len(source_prefix) :]
        if (
            suffix
            and (
                hit := _one(
                    lambda t: (
                        bool(t.unique_id)
                        and t.unique_id.startswith(target_prefix)  # type: ignore[union-attr]
                        and t.unique_id[len(target_prefix) :] == suffix
                    )  # type: ignore[index]
                )
            )
        ):
            return hit
    if source.original_name and (
        hit := _one(
            lambda t: (t.original_name or "").lower() == source.original_name.lower()
        )  # type: ignore[union-attr]
    ):
        return hit
    if source.translation_key and (
        hit := _one(lambda t: t.translation_key == source.translation_key)
    ):
        return hit
    return None


def fill_by_pattern(
    source_number: str,
    source_roles: Mapping[str, str],
    source_device_entities: list[EntityInfo] | None,
    targets: Mapping[str, Target],
    exists: Callable[[str], bool],
    overwrite: bool = False,
) -> dict[str, Proposal]:
    """Propose mappings for other rooms from one mapped room.

    Per role: (a) when both rooms have a device, the counterpart inside the
    target device (`match_in_device`); (b) else the room number swapped inside
    the entity id; (c) else the role is reported missing. A role already mapped
    is kept unless `overwrite`. Only rooms with something found are returned.
    """
    by_id = {e.entity_id: e for e in source_device_entities or []}
    src_prefix = device_prefix(source_device_entities or [])
    result: dict[str, Proposal] = {}
    for unit_id, target in targets.items():
        proposal = Proposal()
        tgt_prefix = device_prefix(target.device_entities or [])
        for role, entity_id in source_roles.items():
            if role not in ROLE_DOMAINS:
                continue
            if target.roles.get(role) and not overwrite:
                continue
            candidate: str | None = None
            if target.device_entities and entity_id in by_id:
                candidate = match_in_device(
                    by_id[entity_id], src_prefix, target.device_entities, tgt_prefix
                )
            if candidate is None:
                swapped = substitute_number(entity_id, source_number, target.number)
                if swapped is not None and exists(swapped):
                    candidate = swapped
            if (
                candidate is None
                or candidate.split(".", 1)[0] not in ROLE_DOMAINS[role]
            ):
                proposal.missing.append(role)
                continue
            if target.roles.get(role) != candidate:
                proposal.roles[role] = candidate
        if proposal.roles:
            result[unit_id] = proposal
    return result


def number_in_name(name: str | None, number: str) -> bool:
    """Does a device name carry the room number as a token ("Room 205")?"""
    if not name:
        return False
    slug = number_slug(number)
    return bool(slug) and bool(_number_pattern(slug).search(number_slug(name)))


def online_from_states(states: list[str | None]) -> str:
    """Device availability as the `online` role: off when every entity is down."""
    if not states:
        return UNAVAILABLE
    down = all((s or "").lower() in ("unavailable", "unknown", "") for s in states)
    return "off" if down else "on"
