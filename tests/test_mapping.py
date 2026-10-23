"""Normalization (contract §1) and fill-by-pattern."""

from __future__ import annotations

import pytest

from custom_components.tapechart.mapping import (
    EntityInfo,
    Target,
    climate_plan,
    decimal_string,
    device_prefix,
    fill_by_pattern,
    normalize_state,
    number_in_name,
    online_from_states,
    substitute_number,
)

NO_CLIMATE = {
    "current_temperature": None,
    "temperature": None,
    "min_temp": None,
    "max_temp": None,
    "target_temp_step": None,
    "hvac_modes": [],
    "hvac_action": None,
}


@pytest.mark.parametrize(
    ("role", "state", "attrs", "expected"),
    [
        ("presence", "on", {}, ("on", {})),
        ("presence", "off", {}, ("off", {})),
        ("presence", "unknown", {}, ("unavailable", {})),
        ("presence", "unavailable", {}, ("unavailable", {})),
        ("presence", None, None, ("unavailable", {})),
        ("keycard", "on", {}, ("on", {})),
        ("dnd", "off", {"friendly_name": "x"}, ("off", {})),
        ("door", "weird", {}, ("unavailable", {})),
        (
            "alarm",
            "on",
            {"device_class": "moisture"},
            ("on", {"device_class": "moisture"}),
        ),
        ("alarm", "unknown", {}, ("unavailable", {"device_class": None})),
        (
            "climate",
            "heat",
            {"current_temperature": 21.5, "temperature": "22", "hvac_modes": ["heat"]},
            (
                "heat",
                {
                    **NO_CLIMATE,
                    "current_temperature": 21.5,
                    "temperature": 22.0,
                    "hvac_modes": ["heat"],
                },
            ),
        ),
        (
            "climate",
            "unavailable",
            {},
            ("unavailable", NO_CLIMATE),
        ),
        (
            "climate",
            "off",
            {"current_temperature": "nan"},
            ("off", NO_CLIMATE),
        ),
        ("temperature", "25.50", {}, ("25.5", {})),
        ("temperature", "21", {}, ("21", {})),
        ("temperature", "-0.0", {}, ("0", {})),
        ("temperature", "abc", {}, ("unavailable", {})),
        ("temperature", "unknown", {}, ("unavailable", {})),
        ("online", "on", {}, ("on", {})),
    ],
)
def test_normalize(role, state, attrs, expected) -> None:
    """Every role normalizes to the wire vocabulary only."""
    assert normalize_state(role, state, attrs) == expected


def test_normalize_scenario_is_not_a_signal() -> None:
    """Scenario roles are command targets, never signals."""
    with pytest.raises(ValueError):
        normalize_state("scenario_welcome", "on", {})


def test_decimal_string_bounds() -> None:
    """Long or non-finite numbers are not sent."""
    assert decimal_string("1e2") == "100"
    assert decimal_string("inf") is None
    assert decimal_string("12345678901234567.5") is None


@pytest.mark.parametrize(
    ("entity_id", "src", "dst", "expected"),
    [
        (
            "binary_sensor.room_205_bathroom_pir",
            "205",
            "206",
            "binary_sensor.room_206_bathroom_pir",
        ),
        (
            "climate.room_205_thermostat_climate_controller",
            "205",
            "101",
            "climate.room_101_thermostat_climate_controller",
        ),
        ("sensor.room205temp", "205", "206", "sensor.room206temp"),
        ("sensor.room_2050_temp", "205", "206", None),
        ("sensor.room_1205_temp", "205", "206", None),
        ("sensor.lobby_temp", "205", "206", None),
        ("switch.room_a_1_light", "A-1", "B-2", "switch.room_b_2_light"),
        ("switch.room_ab_1_light", "B-1", "C-1", None),
        ("sensor.room_205_temp", "205", "205", None),
    ],
)
def test_substitute_number(entity_id, src, dst, expected) -> None:
    """The room number is replaced only as a whole token."""
    assert substitute_number(entity_id, src, dst) == expected


def test_fill_by_number() -> None:
    """Existing entities are proposed; missing ones are reported; mapped roles kept."""
    existing = {
        "binary_sensor.room_206_bathroom_pir",
        "climate.room_206_thermostat_climate_controller",
        "sensor.room_207_room_temp",
        "binary_sensor.room_207_bathroom_pir",
    }
    source = {
        "presence": "binary_sensor.room_205_bathroom_pir",
        "climate": "climate.room_205_thermostat_climate_controller",
        "temperature": "sensor.room_205_room_temp",
    }
    result = fill_by_pattern(
        "205",
        source,
        None,
        {
            "u206": Target("206", {}),
            "u207": Target("207", {"presence": "binary_sensor.custom"}),
            "u300": Target("300", {}),
        },
        existing.__contains__,
    )
    assert result["u206"].roles == {
        "presence": "binary_sensor.room_206_bathroom_pir",
        "climate": "climate.room_206_thermostat_climate_controller",
    }
    assert result["u206"].missing == ["temperature"]
    assert result["u207"].roles == {"temperature": "sensor.room_207_room_temp"}
    assert "u300" not in result

    overwritten = fill_by_pattern(
        "205",
        source,
        None,
        {"u207": Target("207", {"presence": "binary_sensor.custom"})},
        existing.__contains__,
        overwrite=True,
    )
    assert (
        overwritten["u207"].roles["presence"] == "binary_sensor.room_207_bathroom_pir"
    )


def _esphome(
    mac: str, prefix: str, extra: dict[str, str] | None = None
) -> list[EntityInfo]:
    """An ESPHome room controller like the hotel's (unique_id = MAC-domain-key)."""
    items = {
        "binary_sensor": ("bathroom_pir", "Bathroom PIR"),
        "climate": ("thermostat_climate_controller", "Thermostat Climate Controller"),
        "sensor": ("room_temp", "Room Temp"),
    }
    out = [
        EntityInfo(
            f"{domain}.{prefix}_{key}",
            unique_id=f"{mac}-{domain}-{key}",
            original_name=name,
        )
        for domain, (key, name) in items.items()
    ]
    out.append(
        EntityInfo(
            f"binary_sensor.{prefix}_thermostat_control_input",
            unique_id=f"{mac}-binary_sensor-thermostat_control_input",
            original_name="Thermostat Control Input",
        )
    )
    return out


def test_fill_by_device_when_ids_differ() -> None:
    """Room 108's entities are named engo_test_*: the device match still finds them."""
    src = _esphome("14:33:5C:22:AC:A8", "room_205")
    dst = _esphome("A0:B7:65:01:02:03", "engo_test")
    result = fill_by_pattern(
        "205",
        {
            "presence": "binary_sensor.room_205_bathroom_pir",
            "climate": "climate.room_205_thermostat_climate_controller",
            "temperature": "sensor.room_205_room_temp",
        },
        src,
        {"u108": Target("108", {}, dst)},
        lambda _e: False,
    )
    assert result["u108"].roles == {
        "presence": "binary_sensor.engo_test_bathroom_pir",
        "climate": "climate.engo_test_thermostat_climate_controller",
        "temperature": "sensor.engo_test_room_temp",
    }
    assert result["u108"].missing == []


def test_fill_by_device_falls_back_to_original_name() -> None:
    """Without comparable unique_ids, the original name in the device decides."""
    src = [EntityInfo("binary_sensor.room_205_pir", original_name="Bathroom PIR")]
    dst = [
        EntityInfo("binary_sensor.x_motion", original_name="bathroom pir"),
        EntityInfo("binary_sensor.x_door", original_name="Door"),
    ]
    result = fill_by_pattern(
        "205",
        {"presence": "binary_sensor.room_205_pir"},
        src,
        {"u1": Target("101", {}, dst)},
        lambda _e: False,
    )
    assert result["u1"].roles == {"presence": "binary_sensor.x_motion"}


def test_device_prefix() -> None:
    """The MAC part common to all unique_ids, cut at a separator."""
    assert device_prefix(_esphome("14:33:5C:22:AC:A8", "r")) == "14:33:5C:22:AC:A8-"
    assert device_prefix([EntityInfo("a.b", unique_id="x")]) == ""


def test_number_in_name() -> None:
    """A device name carries the room number as a token."""
    assert number_in_name("Room 205", "205")
    assert number_in_name("room_205_controller", "205")
    assert not number_in_name("Room 2051", "205")
    assert not number_in_name(None, "205")


def test_online_from_states() -> None:
    """Off when every entity is down, on otherwise, unavailable with none."""
    assert online_from_states(["unavailable", "unknown"]) == "off"
    assert online_from_states(["unavailable", "on"]) == "on"
    assert online_from_states([None]) == "off"
    assert online_from_states([]) == "unavailable"


def test_climate_attributes_full() -> None:
    """All §10 attributes; bad modes and long actions are dropped; ≤ 1 KB."""
    attrs = {
        "current_temperature": 21.4,
        "temperature": 22,
        "min_temp": 7,
        "max_temp": 35,
        "target_temp_step": 0.5,
        "hvac_modes": [
            "off",
            "heat",
            "Heat",
            "fan-only",
            "x" * 33,
            *[f"mode_{chr(97 + i)}" for i in range(20)],
        ],
        "hvac_action": "heating",
        "preset_modes": ["eco"],
        "friendly_name": "Thermostat",
    }
    state, out = normalize_state("climate", "heat", attrs)
    assert state == "heat"
    assert out["min_temp"] == 7.0 and out["max_temp"] == 35.0
    assert out["target_temp_step"] == 0.5
    assert out["hvac_action"] == "heating"
    assert out["hvac_modes"][:2] == ["off", "heat"]
    assert len(out["hvac_modes"]) == 10
    assert all(m.isascii() and m.replace("_", "").isalnum() for m in out["hvac_modes"])
    assert set(out) == set(NO_CLIMATE)
    import json

    assert len(json.dumps(out)) <= 1024
    _s, long_action = normalize_state("climate", "heat", {"hvac_action": "y" * 40})
    assert long_action["hvac_action"] is None


THERMO = {
    "min_temp": 7,
    "max_temp": 30,
    "target_temp_step": 0.5,
    "hvac_modes": ["off", "heat"],
}


@pytest.mark.parametrize(
    ("temperature", "mode", "expected"),
    [
        (21.5, None, (None, 21.5)),
        (21.3, "heat", ("heat", 21.5)),
        ("22", None, (None, 22.0)),
        (None, "off", ("off", None)),
        (7, None, (None, 7.0)),
        (30, None, (None, 30.0)),
        (30.1, None, None),
        (6.9, None, None),
        ("abc", None, None),
        (True, None, None),
        (None, "cool", None),
        (None, 5, None),
        (None, None, None),
    ],
)
def test_climate_plan(temperature, mode, expected) -> None:
    """Range, step rounding and mode checks against the entity."""
    assert climate_plan(THERMO, temperature, mode) == expected


def test_climate_plan_without_limits_or_step() -> None:
    """No min/max known: a temperature is refused; no step: tenths."""
    assert climate_plan({"hvac_modes": ["heat"]}, 21, None) is None
    assert climate_plan({"min_temp": 5, "max_temp": 30}, 21.26, None) == (None, 21.3)
