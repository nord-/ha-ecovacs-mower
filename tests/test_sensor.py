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
        EcovacsMowingProgressSensor,
    )

    keys = {d.key for d in ENTITY_DESCRIPTIONS} | {
        EcovacsErrorSensor.entity_description.key,
        EcovacsActivitySensor.entity_description.key,
        EcovacsMowingProgressSensor.entity_description.key,
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
        "mowing_progress",
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
        EcovacsMowingProgressSensor,
        beacon_entity_description,
    )

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    strings = json.loads((root / "strings.json").read_text(encoding="utf-8"))
    names = strings["entity"]["sensor"]

    descriptions = (
        *ENTITY_DESCRIPTIONS,
        *LIFESPAN_ENTITY_DESCRIPTIONS,
        EcovacsErrorSensor.entity_description,
        EcovacsActivitySensor.entity_description,
        EcovacsMowingProgressSensor.entity_description,
        # Built per beacon at runtime, so there is no tuple to splat. Every
        # serial yields the same translation key, which is what these three
        # tests check.
        beacon_entity_description("EXAMPLE"),
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
        EcovacsMowingProgressSensor,
        beacon_entity_description,
    )

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    icons = json.loads((root / "icons.json").read_text(encoding="utf-8"))
    names = icons["entity"]["sensor"]

    descriptions = (
        *ENTITY_DESCRIPTIONS,
        *LIFESPAN_ENTITY_DESCRIPTIONS,
        EcovacsErrorSensor.entity_description,
        EcovacsActivitySensor.entity_description,
        EcovacsMowingProgressSensor.entity_description,
        # Built per beacon at runtime, so there is no tuple to splat. Every
        # serial yields the same translation key, which is what these three
        # tests check.
        beacon_entity_description("EXAMPLE"),
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
        EcovacsMowingProgressSensor,
        beacon_entity_description,
    )

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    strings = json.loads((root / "strings.json").read_text(encoding="utf-8"))
    icons = json.loads((root / "icons.json").read_text(encoding="utf-8"))

    descriptions = (
        *ENTITY_DESCRIPTIONS,
        *LIFESPAN_ENTITY_DESCRIPTIONS,
        EcovacsErrorSensor.entity_description,
        EcovacsActivitySensor.entity_description,
        EcovacsMowingProgressSensor.entity_description,
        # Built per beacon at runtime, so there is no tuple to splat. Every
        # serial yields the same translation key, which is what these three
        # tests check.
        beacon_entity_description("EXAMPLE"),
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


async def test_progress_sensor_is_gated_on_supported_classes() -> None:
    """An unsupported mower class would sit at unknown forever without this.

    MowerStatsEvent's refresh command only exists for classes deebot_patch has
    patched (hardware.py's SUPPORTED_CLASSES); every other MOWER class has no
    command behind it at all.
    """
    from unittest.mock import MagicMock, patch

    from deebot_client.capabilities import DeviceType

    from custom_components.ecovacs_mower.deebot_patch import SUPPORTED_CLASSES
    from custom_components.ecovacs_mower.sensor import (
        EcovacsMowingProgressSensor,
        async_setup_entry,
    )

    def _mower(class_: str, did: str):
        device = MagicMock()
        device.capabilities.device_type = DeviceType.MOWER
        device.capabilities.error = None
        device.capabilities.life_span.types = ()
        device.device_info = {"did": did, "class": class_}
        return device

    supported = _mower(next(iter(SUPPORTED_CLASSES)), "did-supported")
    unsupported = _mower("not-a-real-class", "did-unsupported")

    config_entry = MagicMock()
    config_entry.runtime_data.devices = [supported, unsupported]

    added: list = []
    with patch(
        "custom_components.ecovacs_mower.sensor.get_supported_entities",
        return_value=[],
    ):
        await async_setup_entry(MagicMock(), config_entry, added.extend)

    progress_devices = {
        e._device for e in added if isinstance(e, EcovacsMowingProgressSensor)
    }
    assert progress_devices == {supported}


def _bare_progress_sensor():
    """A progress sensor without HA, for the same reason as the activity one."""
    from unittest.mock import Mock

    from custom_components.ecovacs_mower.sensor import EcovacsMowingProgressSensor

    sensor = EcovacsMowingProgressSensor.__new__(EcovacsMowingProgressSensor)
    sensor._device = Mock()
    sensor._last_state = None
    sensor.async_write_ha_state = lambda: None
    return sensor


def _job(area: int = 211275, mowed: int = 87825):
    from custom_components.ecovacs_mower.deebot_patch.messages import MowerStatsEvent

    return MowerStatsEvent(area=area, mowed_area=mowed)


def _state_event(state):
    from deebot_client.events import StateEvent

    return StateEvent(state)


def test_progress_is_the_ratio_of_the_two_fields() -> None:
    # A real getStats answer from a mowing GOAT O1200: 87825 cm2 cut of a
    # 211275 cm2 job.
    from custom_components.ecovacs_mower.sensor import _progress

    assert _progress(211275, 87825) == 42


def test_no_job_is_unknown_rather_than_zero() -> None:
    """Between jobs the device reports zeros, which is not a job at 0 %.

    Reported as 0, every idle mower would look exactly like one that has just
    started, and "notify me when it reaches 100" would have no way to tell the
    difference from the state alone.
    """
    from custom_components.ecovacs_mower.sensor import _progress

    assert _progress(0, 0) is None


def test_a_firmware_that_omits_the_field_is_unknown() -> None:
    from custom_components.ecovacs_mower.sensor import _progress

    assert _progress(211275, None) is None


def test_progress_never_exceeds_a_hundred() -> None:
    from custom_components.ecovacs_mower.sensor import _progress

    assert _progress(100, 150) == 100


async def test_a_job_starting_is_looked_at_at_once() -> None:
    """Otherwise the first reading of a run waits for the platform's next tick."""
    from deebot_client.models import State

    from custom_components.ecovacs_mower.deebot_patch.messages import MowerStatsEvent

    sensor = _bare_progress_sensor()

    await sensor._on_state(_state_event(State.CLEANING))

    sensor._device.events.request_refresh.assert_called_once_with(MowerStatsEvent)


async def test_repeated_pushes_of_the_same_state_do_not_re_trigger() -> None:
    """A push is not a transition; entering CLEANING while already CLEANING is not."""
    from deebot_client.models import State

    sensor = _bare_progress_sensor()

    await sensor._on_state(_state_event(State.CLEANING))
    await sensor._on_state(_state_event(State.CLEANING))

    sensor._device.events.request_refresh.assert_called_once()


async def test_docking_clears_the_reading_immediately() -> None:
    """Docking must not wait for a zeroed getStats answer to clear the sensor.

    A GOAT G1-800 on firmware 1.36.208 never zeros mowedArea/area between jobs
    at all (reported against issue #39) — six hours after a job ended, a
    fresh getStats answer still read the finished job's numbers. Waiting for
    a zero would leave this firmware stuck showing the last job's percentage
    forever, so the state itself — not another getStats round trip — is what
    clears it.
    """
    from deebot_client.models import State

    sensor = _bare_progress_sensor()
    sensor._attr_native_value = 42

    await sensor._on_state(_state_event(State.DOCKED))

    assert sensor._attr_native_value is None
    sensor._device.events.request_refresh.assert_not_called()


async def test_a_stats_answer_while_docked_is_not_trusted() -> None:
    """The same firmware quirk can also feed a stale answer to _on_stats directly.

    Gating on _progress()'s zero check alone is not enough — it never fires
    on this firmware — so _on_stats also has to know the mower is currently
    parked and refuse the answer regardless of what it contains.
    """
    from deebot_client.models import State

    sensor = _bare_progress_sensor()
    sensor._last_state = State.DOCKED

    await sensor._on_stats(_job(1374800, 1374800))

    assert sensor._attr_native_value is None


async def test_a_stats_answer_before_any_state_is_seen_is_not_trusted() -> None:
    """Startup ordering between the two subscriptions is not guaranteed.

    If a stats answer happens to arrive before this sensor has seen its first
    StateEvent, there is no basis yet for believing a job is running.
    """
    sensor = _bare_progress_sensor()
    assert sensor._last_state is None

    await sensor._on_stats(_job())

    assert sensor._attr_native_value is None


async def test_the_states_in_between_are_left_alone() -> None:
    """No state asks for a fresh answer except a start; the rest ride the tick.

    Clearing the value is a separate question — see the tests below for the
    states that end a job away from the dock.
    """
    from deebot_client.models import State

    for state in (State.PAUSED, State.IDLE, State.RETURNING, State.ERROR):
        sensor = _bare_progress_sensor()
        await sensor._on_state(_state_event(state))
        sensor._device.events.request_refresh.assert_not_called()


async def test_the_percentage_follows_the_answer() -> None:
    from deebot_client.models import State

    sensor = _bare_progress_sensor()
    sensor._last_state = State.CLEANING

    await sensor._on_stats(_job(211275, 208275))
    assert sensor._attr_native_value == 99

    await sensor._on_stats(_job(0, 0))
    assert sensor._attr_native_value is None


def test_error_description_prefers_the_library_and_fills_its_gaps(caplog) -> None:
    """The library's text wins; ours only covers what it has no entry for."""
    import logging

    from deebot_client.const import ERROR_CODES

    from custom_components.ecovacs_mower.sensor import (
        _MOWER_ERROR_CODES,
        _UNKNOWN_CODES_REPORTED,
        _error_description,
    )

    caplog.set_level(logging.WARNING, logger="custom_components.ecovacs_mower.sensor")

    # A code the library knows keeps the library's wording, even if this table
    # were ever to grow an entry for it.
    assert _error_description(101, ERROR_CODES[101]) == ERROR_CODES[101]

    # No overlap: an entry here that the library also has is a disagreement
    # nobody would notice, since the branch above means it is never read.
    assert not set(_MOWER_ERROR_CODES) & set(ERROR_CODES)

    assert _error_description(422, None) == _MOWER_ERROR_CODES[422]
    assert _error_description(406, None) == _MOWER_ERROR_CODES[406]

    try:
        _UNKNOWN_CODES_REPORTED.discard(9999)
        assert _error_description(9999, None) is None
        assert "9999" in caplog.text
        assert "issues/37" in caplog.text

        # Asked once, not on every push for as long as the condition lasts.
        caplog.clear()
        assert _error_description(9999, None) is None
        assert not caplog.text
    finally:
        _UNKNOWN_CODES_REPORTED.discard(9999)


async def test_a_job_ending_away_from_the_dock_clears_the_reading() -> None:
    """Docking is not the only way a job ends (issue #55).

    A mower that faults out on the lawn goes to ERROR and never reaches
    DOCKED, and one that simply stops pushes an idle. Gating only on DOCKED
    left the last percentage standing in both cases — on a firmware branch
    whose stats never zero, indefinitely.
    """
    from deebot_client.models import State

    for state in (State.ERROR, State.IDLE):
        sensor = _bare_progress_sensor()
        sensor._last_state = State.CLEANING
        sensor._attr_native_value = 93

        await sensor._on_state(_state_event(state))

        assert sensor._attr_native_value is None, state


async def test_a_stats_answer_after_a_job_ended_away_from_the_dock_is_ignored() -> None:
    # The push keeps arriving for a while after the job stops on some
    # firmware; without the gate it would refill the value just cleared.
    from deebot_client.models import State

    for state in (State.ERROR, State.IDLE):
        sensor = _bare_progress_sensor()
        sensor._last_state = state

        await sensor._on_stats(_job(1374800, 1374800))

        assert sensor._attr_native_value is None, state


async def test_the_states_a_job_runs_in_still_report() -> None:
    # The gate is inverted; make sure it did not invert too far.
    from deebot_client.models import State

    for state in (State.CLEANING, State.PAUSED, State.RETURNING):
        sensor = _bare_progress_sensor()
        sensor._last_state = state

        await sensor._on_stats(_job(211275, 87825))

        assert sensor._attr_native_value == 42, state


async def test_an_unchanged_percentage_is_not_written_again() -> None:
    """onStats arrives about twice a second; whole percents do not (issue #55).

    On the captured O800 RTK job one percent is 2089 cm2 and a push moves a few
    hundred, so most pushes round to the number already showing. HA's state
    machine already short-circuits an unchanged write with no recorder row, so
    the guard is not about rows — it saves the state-machine round trip itself,
    roughly five times a second the entity has nothing new to report.
    """
    from unittest.mock import Mock

    from deebot_client.models import State

    sensor = _bare_progress_sensor()
    sensor._last_state = State.CLEANING
    sensor.async_write_ha_state = Mock()

    # Three consecutive pushes from the issue #56 log, all 50% of 208900.
    for mowed in (104500, 104550, 105000):
        await sensor._on_stats(_job(208900, mowed))

    assert sensor._attr_native_value == 50
    assert sensor.async_write_ha_state.call_count == 1


async def test_a_changed_percentage_is_written() -> None:
    from unittest.mock import Mock

    from deebot_client.models import State

    sensor = _bare_progress_sensor()
    sensor._last_state = State.CLEANING
    sensor.async_write_ha_state = Mock()

    await sensor._on_stats(_job(208900, 104500))
    await sensor._on_stats(_job(208900, 106589))

    assert sensor._attr_native_value == 51
    assert sensor.async_write_ha_state.call_count == 2
def _beacons(*pairs: tuple[str, float]):
    from custom_components.ecovacs_mower.deebot_patch.messages import (
        MowerBeacon,
        MowerBeaconsEvent,
    )

    return MowerBeaconsEvent(
        beacons=tuple(MowerBeacon(sn=sn, percent=percent) for sn, percent in pairs)
    )


def _beacon_mower(class_: str = "77atlz", did: str = "did-beacon"):
    """A patched mower class, mocked the way the progress-sensor test mocks one."""
    from unittest.mock import MagicMock

    from deebot_client.capabilities import DeviceType

    device = MagicMock()
    device.capabilities.device_type = DeviceType.MOWER
    device.capabilities.error = None
    device.capabilities.life_span.types = ()
    device.device_info = {"did": did, "class": class_}
    return device


async def _set_up_beacons(device):
    """Run the platform for *device* and return the added list and the callback.

    The beacons are not known at setup time — the count comes out of the first
    getLifeSpan answer — so what the platform installs is a subscription, and
    the entities appear when it fires.
    """
    from unittest.mock import MagicMock, patch

    from custom_components.ecovacs_mower.deebot_patch.messages import MowerBeaconsEvent
    from custom_components.ecovacs_mower.sensor import async_setup_entry

    config_entry = MagicMock()
    config_entry.runtime_data.devices = [device]

    added: list = []
    with patch(
        "custom_components.ecovacs_mower.sensor.get_supported_entities",
        return_value=[],
    ):
        await async_setup_entry(MagicMock(), config_entry, added.extend)

    event_type, callback = device.events.subscribe.call_args[0]
    assert event_type is MowerBeaconsEvent
    return added, callback


def _beacon_sensors(added: list):
    from custom_components.ecovacs_mower.sensor import EcovacsBeaconSensor

    return [e for e in added if isinstance(e, EcovacsBeaconSensor)]


async def test_no_beacon_sensor_exists_before_the_device_has_answered() -> None:
    """The count is only known from the payload, so setup cannot enumerate them."""
    added, _ = await _set_up_beacons(_beacon_mower())

    assert _beacon_sensors(added) == []


async def test_a_beacon_sensor_appears_for_every_serial_reported() -> None:
    # The real answer in issue #40 carried four, one of them flat.
    added, on_beacons = await _set_up_beacons(_beacon_mower())

    await on_beacons(_beacons(("A1", 0.0), ("A2", 83.0), ("A3", 68.0), ("A4", 73.0)))

    assert [s.entity_description.serial for s in _beacon_sensors(added)] == [
        "A1",
        "A2",
        "A3",
        "A4",
    ]


async def test_a_serial_already_known_does_not_get_a_second_entity() -> None:
    # getLifeSpan is polled, so the same set arrives over and over.
    added, on_beacons = await _set_up_beacons(_beacon_mower())

    await on_beacons(_beacons(("A1", 83.0)))
    await on_beacons(_beacons(("A1", 82.0)))

    assert len(_beacon_sensors(added)) == 1


async def test_a_beacon_added_later_gets_its_own_entity() -> None:
    """A replacement beacon has its own serial and is a new entity."""
    added, on_beacons = await _set_up_beacons(_beacon_mower())

    await on_beacons(_beacons(("A1", 83.0)))
    await on_beacons(_beacons(("A1", 83.0), ("A2", 100.0)))

    assert [s.entity_description.serial for s in _beacon_sensors(added)] == ["A1", "A2"]


async def test_the_beacon_unique_id_carries_the_serial() -> None:
    """Four beacons on one device need four ids, and the serial is the only key.

    The payload has no index and no guaranteed order, so numbering them by
    arrival would reshuffle the entities the day an answer comes back in a
    different order.
    """
    added, on_beacons = await _set_up_beacons(_beacon_mower(did="did-x"))

    await on_beacons(_beacons(("A1", 83.0), ("A2", 68.0)))

    assert [s.unique_id for s in _beacon_sensors(added)] == [
        "did-x_beacon_A1",
        "did-x_beacon_A2",
    ]


async def test_beacon_sensors_are_gated_on_supported_classes() -> None:
    """Same gate as the progress sensor: no patch, no refresh command, no value."""
    unsupported = _beacon_mower(class_="not-a-real-class", did="did-unsupported")

    from unittest.mock import MagicMock, patch

    from custom_components.ecovacs_mower.sensor import async_setup_entry

    config_entry = MagicMock()
    config_entry.runtime_data.devices = [unsupported]

    with patch(
        "custom_components.ecovacs_mower.sensor.get_supported_entities",
        return_value=[],
    ):
        await async_setup_entry(MagicMock(), config_entry, [].extend)

    unsupported.events.subscribe.assert_not_called()


def _bare_beacon_sensor(serial: str):
    """A beacon sensor without HA, for the same reason as the progress one."""
    from unittest.mock import Mock

    from custom_components.ecovacs_mower.sensor import (
        EcovacsBeaconSensor,
        beacon_entity_description,
    )

    sensor = EcovacsBeaconSensor.__new__(EcovacsBeaconSensor)
    sensor.entity_description = beacon_entity_description(serial)
    sensor._device = Mock()
    sensor.async_write_ha_state = lambda: None
    return sensor


async def test_a_beacon_sensor_reads_only_its_own_serial() -> None:
    sensor = _bare_beacon_sensor("A2")

    await sensor._on_beacons(_beacons(("A1", 0.0), ("A2", 83.0), ("A3", 68.0)))

    assert sensor._attr_native_value == 83.0


async def test_a_beacon_missing_from_a_later_answer_goes_unknown() -> None:
    """A swapped-out beacon must not sit there showing the dead cell's charge.

    The device simply stops listing it. Keeping the last value would leave a
    ghost at 0 % that nothing can ever clear, and a low-battery automation
    firing on a beacon that is no longer on the lawn.
    """
    sensor = _bare_beacon_sensor("A2")

    await sensor._on_beacons(_beacons(("A2", 83.0)))
    await sensor._on_beacons(_beacons(("A1", 68.0)))

    assert sensor._attr_native_value is None


def test_a_beacon_sensor_is_a_battery_percentage() -> None:
    """device_class battery is what buys HA's own low-battery handling."""
    from homeassistant.components.sensor import SensorDeviceClass
    from homeassistant.const import PERCENTAGE

    from custom_components.ecovacs_mower.sensor import beacon_entity_description

    description = beacon_entity_description("A1")
    assert description.device_class is SensorDeviceClass.BATTERY
    assert description.native_unit_of_measurement == PERCENTAGE


def test_a_beacon_sensor_is_named_after_its_serial() -> None:
    """The serial is the code the app's maintenance page prints next to it."""
    from custom_components.ecovacs_mower.sensor import beacon_entity_description

    description = beacon_entity_description("A1")
    assert description.translation_key == "beacon"
    assert description.key == "beacon_A1"
