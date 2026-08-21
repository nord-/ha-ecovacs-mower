"""The sensor set must mirror what a GOAT actually has."""

from tests import requires_ha

pytestmark = requires_ha


def test_no_station_sensor() -> None:
    """station_state describes a vacuum station's dust bag."""
    from custom_components.ecovacs_mower.sensor import ENTITY_DESCRIPTIONS

    assert not any(d.key == "station_state" for d in ENTITY_DESCRIPTIONS)


def test_no_legacy_classes() -> None:
    from custom_components.ecovacs_mower import sensor

    assert not hasattr(sensor, "EcovacsLegacyBatterySensor")
    assert not hasattr(sensor, "EcovacsLegacyLifespanSensor")
    assert not hasattr(sensor, "LEGACY_LIFESPAN_SENSORS")


def test_expected_sensor_keys() -> None:
    """Locks the set. If it changes, that must be a decision, not an accident.

    ``error`` does not live in ``ENTITY_DESCRIPTIONS``: just as in core,
    ``EcovacsErrorSensor`` has its ``entity_description`` as a class attribute and
    is built separately in ``async_setup_entry``, not via
    ``get_supported_entities``. Putting it in ``ENTITY_DESCRIPTIONS`` would have
    made get_supported_entities build a second, generic ``EcovacsSensor`` with the
    same unique_id (``{did}_error``) as the real ``EcovacsErrorSensor`` — a
    collision in the entity registry. The test therefore merges the two sources
    into one set.
    """
    from homeassistant.const import ATTR_BATTERY_LEVEL

    from custom_components.ecovacs_mower.sensor import (
        ENTITY_DESCRIPTIONS,
        EcovacsActivitySensor,
        EcovacsErrorSensor,
    )

    keys = {d.key for d in ENTITY_DESCRIPTIONS} | {
        EcovacsErrorSensor.entity_description.key,
        EcovacsActivitySensor.entity_description.key,
    }
    assert keys == {
        "stats_area",
        "stats_time",
        "total_stats_area",
        "total_stats_time",
        "total_stats_cleanings",
        ATTR_BATTERY_LEVEL,
        "network_ip",
        "network_rssi",
        "network_ssid",
        "error",
        "activity",
    }


def test_four_lifespan_sensors() -> None:
    from custom_components.ecovacs_mower.sensor import LIFESPAN_ENTITY_DESCRIPTIONS

    assert {d.key for d in LIFESPAN_ENTITY_DESCRIPTIONS} == {
        "lifespan_blade",
        "lifespan_lens_brush",
        "lifespan_trimmer_brush",
        "lifespan_weed_rope",
    }


def test_every_description_has_a_translation() -> None:
    """A missing key yields raw strings in the UI.

    Also includes ``EcovacsErrorSensor``, which has its own
    ``entity_description`` outside ``ENTITY_DESCRIPTIONS`` (see
    ``test_expected_sensor_keys``).
    """
    import json
    from pathlib import Path

    from custom_components.ecovacs_mower.sensor import (
        ENTITY_DESCRIPTIONS,
        LIFESPAN_ENTITY_DESCRIPTIONS,
        EcovacsActivitySensor,
        EcovacsErrorSensor,
    )

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    strings = json.loads((root / "strings.json").read_text(encoding="utf-8"))
    names = strings["entity"]["sensor"]

    descriptions = (
        *ENTITY_DESCRIPTIONS,
        *LIFESPAN_ENTITY_DESCRIPTIONS,
        EcovacsErrorSensor.entity_description,
        EcovacsActivitySensor.entity_description,
    )
    for description in descriptions:
        if description.translation_key:
            assert description.translation_key in names, description.key


def test_every_sensor_has_an_icon() -> None:
    """A sensor without its own icon gets HA's generic icon — easy to miss.

    Sensor was the first platform and created ``icons.json``; the pattern of one
    icon test per platform was invented a task later and had never been
    retrofitted here until now. Includes ``EcovacsErrorSensor`` for the same
    reason as ``test_every_description_has_a_translation``.
    """
    import json
    from pathlib import Path

    from custom_components.ecovacs_mower.sensor import (
        ENTITY_DESCRIPTIONS,
        LIFESPAN_ENTITY_DESCRIPTIONS,
        EcovacsActivitySensor,
        EcovacsErrorSensor,
    )

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    icons = json.loads((root / "icons.json").read_text(encoding="utf-8"))
    names = icons["entity"]["sensor"]

    descriptions = (
        *ENTITY_DESCRIPTIONS,
        *LIFESPAN_ENTITY_DESCRIPTIONS,
        EcovacsErrorSensor.entity_description,
        EcovacsActivitySensor.entity_description,
    )
    for description in descriptions:
        if description.translation_key:
            assert description.translation_key in names, description.key


def test_no_stale_sensor_translations_or_icons() -> None:
    """Every key in strings.json/icons.json must belong to a real sensor.

    The converse of the tests above: they check description → string/icon, not the
    other way around. Without this, a leftover key for a removed sensor would go
    unnoticed.
    """
    import json
    from pathlib import Path

    from custom_components.ecovacs_mower.sensor import (
        ENTITY_DESCRIPTIONS,
        LIFESPAN_ENTITY_DESCRIPTIONS,
        EcovacsActivitySensor,
        EcovacsErrorSensor,
    )

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    strings = json.loads((root / "strings.json").read_text(encoding="utf-8"))
    icons = json.loads((root / "icons.json").read_text(encoding="utf-8"))

    descriptions = (
        *ENTITY_DESCRIPTIONS,
        *LIFESPAN_ENTITY_DESCRIPTIONS,
        EcovacsErrorSensor.entity_description,
        EcovacsActivitySensor.entity_description,
    )
    keys = {d.translation_key for d in descriptions if d.translation_key}

    assert set(strings["entity"]["sensor"]) <= keys
    assert set(icons["entity"]["sensor"]) <= keys


def test_every_state_is_an_activity() -> None:
    """An unmapped state logs a warning and freezes the sensor on its old value.

    The same guarantee ``test_every_state_is_mapped`` gives for the lawn_mower
    entity: if deebot-client gains a State, this must be a decision.
    """
    from deebot_client.models import State

    from custom_components.ecovacs_mower.sensor import _STATE_TO_ACTIVITY

    assert set(_STATE_TO_ACTIVITY) == set(State)


def test_activity_agrees_with_the_lawn_mower_entity_for_non_rain_states() -> None:
    """Two tables encode the same state judgements; nothing else keeps them in sync."""
    from custom_components.ecovacs_mower.lawn_mower import _STATE_TO_MOWER_STATE
    from custom_components.ecovacs_mower.sensor import _STATE_TO_ACTIVITY

    assert _STATE_TO_ACTIVITY == {
        state: mower_state.value
        for state, mower_state in _STATE_TO_MOWER_STATE.items()
    }


def test_activity_options_are_exactly_the_reachable_states() -> None:
    """HA rejects a value an enum sensor did not declare in ``options``."""
    from custom_components.ecovacs_mower.sensor import (
        ACTIVITY_OPTIONS,
        _RAIN_ACTIVITY,
        _STATE_TO_ACTIVITY,
    )

    assert set(ACTIVITY_OPTIONS) == {
        *_STATE_TO_ACTIVITY.values(),
        *_RAIN_ACTIVITY.values(),
    }
    assert set(ACTIVITY_OPTIONS) == {
        "mowing",
        "paused",
        "paused_rain",
        "returning",
        "returning_rain",
        "docked",
        "docked_rain_delay",
        "error",
    }


def test_every_activity_option_is_translated() -> None:
    """An untranslated option shows the raw key in the UI, both ways round."""
    import json
    from pathlib import Path

    from custom_components.ecovacs_mower.sensor import ACTIVITY_OPTIONS

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    strings = json.loads((root / "strings.json").read_text(encoding="utf-8"))
    states = strings["entity"]["sensor"]["activity"]["state"]

    assert set(states) == set(ACTIVITY_OPTIONS)


def test_activity_icon_states_are_real_options() -> None:
    """An icon for a state that cannot happen is dead weight."""
    import json
    from pathlib import Path

    from custom_components.ecovacs_mower.sensor import ACTIVITY_OPTIONS

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    icons = json.loads((root / "icons.json").read_text(encoding="utf-8"))

    assert set(icons["entity"]["sensor"]["activity"]["state"]) <= set(ACTIVITY_OPTIONS)


def test_activity_is_an_enum_sensor() -> None:
    """Without the ENUM device class HA treats the value as a plain string.

    The options would then not be validated and the translated state names in
    strings.json would never be used.
    """
    from homeassistant.components.sensor import SensorDeviceClass

    from custom_components.ecovacs_mower.sensor import (
        ACTIVITY_OPTIONS,
        EcovacsActivitySensor,
    )

    description = EcovacsActivitySensor.entity_description
    assert description.device_class is SensorDeviceClass.ENUM
    assert description.options == ACTIVITY_OPTIONS


def test_rain_turns_the_state_into_its_rain_variant() -> None:
    """Rain is only folded in where it changes the meaning."""
    from deebot_client.models import State

    from custom_components.ecovacs_mower.sensor import ACTIVITY_OPTIONS, _activity

    assert _activity(State.DOCKED, interrupted_by_rain=False) == "docked"
    assert _activity(State.DOCKED, interrupted_by_rain=True) == "docked_rain_delay"
    assert _activity(State.RETURNING, interrupted_by_rain=True) == "returning_rain"
    assert _activity(State.PAUSED, interrupted_by_rain=True) == "paused_rain"
    assert _activity(State.IDLE, interrupted_by_rain=True) == "paused_rain"

    # Mowing and error keep their state: see the comment on _RAIN_ACTIVITY.
    assert _activity(State.CLEANING, interrupted_by_rain=True) == "mowing"
    assert _activity(State.ERROR, interrupted_by_rain=True) == "error"

    # Every combination must land inside the declared options.
    for state in State:
        for rain in (False, True):
            assert _activity(state, interrupted_by_rain=rain) in ACTIVITY_OPTIONS


def _bare_activity_sensor():
    """An EcovacsActivitySensor with no device and no hass behind it.

    ``__new__`` skips ``__init__`` (which needs a real Device), and
    ``async_write_ha_state`` is stubbed because there is no hass to write to —
    the same trick test_lawn_mower.py uses for supported_features. What is under
    test is the bookkeeping in the two callbacks, which touches neither.
    """
    from custom_components.ecovacs_mower.sensor import EcovacsActivitySensor

    sensor = EcovacsActivitySensor.__new__(EcovacsActivitySensor)
    sensor._state = None
    sensor._interrupted_by_rain = False
    sensor.async_write_ha_state = lambda: None
    return sensor


async def test_rain_reason_survives_the_real_interruption_sequence() -> None:
    """The captured sequence, event by event, must end on docked_rain_delay.

    This is the whole feature: the device reports "workComplete" when it reaches
    the dock even though rain is what sent it there, so a naive "last trigger
    wins" would show a plain "docked" — indistinguishable from a finished run,
    which is the problem this sensor exists to solve.
    """
    from deebot_client.events import StateEvent
    from deebot_client.models import State

    from custom_components.ecovacs_mower.deebot_patch.messages import MowerTriggerEvent

    sensor = _bare_activity_sensor()

    await sensor._on_state(StateEvent(State.CLEANING))
    assert sensor._attr_native_value == "mowing"

    await sensor._on_trigger(MowerTriggerEvent("rain"))
    await sensor._on_state(StateEvent(State.PAUSED))
    assert sensor._attr_native_value == "paused_rain"

    await sensor._on_state(StateEvent(State.RETURNING))
    assert sensor._attr_native_value == "returning_rain"

    await sensor._on_state(StateEvent(State.DOCKED))
    assert sensor._attr_native_value == "docked_rain_delay"

    # 56 seconds later, from the log. The reason must not be thrown away.
    await sensor._on_trigger(MowerTriggerEvent("workComplete"))
    assert sensor._attr_native_value == "docked_rain_delay"


async def test_mowing_again_clears_the_rain_reason() -> None:
    """Cutting grass is the only signal that the rain stop is over."""
    from deebot_client.events import StateEvent
    from deebot_client.models import State

    from custom_components.ecovacs_mower.deebot_patch.messages import MowerTriggerEvent

    sensor = _bare_activity_sensor()
    await sensor._on_trigger(MowerTriggerEvent("rain"))
    await sensor._on_state(StateEvent(State.DOCKED))
    assert sensor._attr_native_value == "docked_rain_delay"

    await sensor._on_state(StateEvent(State.CLEANING))
    assert sensor._attr_native_value == "mowing"

    # A hand-pressed pause after the rain is over is just "paused".
    await sensor._on_state(StateEvent(State.PAUSED))
    assert sensor._attr_native_value == "paused"


async def test_a_trigger_alone_does_not_produce_a_state() -> None:
    """Without a StateEvent there is nothing to qualify, so no value yet.

    The reason is still remembered: a trigger can arrive before the state it
    belongs to, and must survive until then.
    """
    from deebot_client.events import StateEvent
    from deebot_client.models import State

    from custom_components.ecovacs_mower.deebot_patch.messages import MowerTriggerEvent

    sensor = _bare_activity_sensor()
    await sensor._on_trigger(MowerTriggerEvent("rain"))
    assert sensor._attr_native_value is None

    await sensor._on_state(StateEvent(State.RETURNING))
    assert sensor._attr_native_value == "returning_rain"


async def test_unmappable_state_keeps_the_previous_value() -> None:
    """A state outside the options would be rejected by HA outright."""
    from unittest.mock import Mock

    from deebot_client.events import StateEvent
    from deebot_client.models import State

    sensor = _bare_activity_sensor()
    await sensor._on_state(StateEvent(State.CLEANING))

    unknown = Mock()
    unknown.state = "somethingNew"
    await sensor._on_state(unknown)
    assert sensor._attr_native_value == "mowing"


def test_duration_sensors_are_displayed_in_a_unit_the_frontend_expands() -> None:
    # The frontend only formats a duration sensor as a duration when its
    # displayed unit is one of min/h/d (DURATION_UNITS in
    # common/datetime/format_duration.ts). Suggest seconds and the state
    # renders as a bare "14490 s" instead.
    from homeassistant.components.sensor import SensorDeviceClass
    from homeassistant.const import UnitOfTime

    from custom_components.ecovacs_mower.sensor import ENTITY_DESCRIPTIONS

    durations = [
        d
        for d in ENTITY_DESCRIPTIONS
        if d.device_class is SensorDeviceClass.DURATION
    ]
    assert durations
    for description in durations:
        assert description.suggested_unit_of_measurement in (
            UnitOfTime.MINUTES,
            UnitOfTime.HOURS,
            UnitOfTime.DAYS,
        ), description.key
