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

    Neither ``error`` nor ``activity`` lives in ``ENTITY_DESCRIPTIONS``: just as in core,
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


def test_every_state_maps_to_an_activity() -> None:
    """An unmapped state leaves the sensor blank while the mower is doing something."""
    from deebot_client.models import State

    from custom_components.ecovacs_mower.sensor import _STATE_TO_ACTIVITY

    assert set(_STATE_TO_ACTIVITY) == set(State)


def test_activity_matches_the_lawn_mower_reading_of_the_state() -> None:
    """The two maps are separate on purpose, but must not disagree.

    ``sensor.activity`` is the lawn_mower activity plus rain. If the two ever
    read a state differently, one of the entities is lying.
    """
    from homeassistant.components.lawn_mower import LawnMowerActivity

    from custom_components.ecovacs_mower.lawn_mower import _STATE_TO_MOWER_STATE
    from custom_components.ecovacs_mower.sensor import _STATE_TO_ACTIVITY

    for state, activity in _STATE_TO_ACTIVITY.items():
        assert activity == LawnMowerActivity(_STATE_TO_MOWER_STATE[state]).value


def test_rain_renames_the_three_activities_a_rained_off_run_passes_through() -> None:
    from deebot_client.models import State

    from custom_components.ecovacs_mower.sensor import activity_key

    assert activity_key(State.PAUSED, raining=True) == "paused_rain"
    assert activity_key(State.RETURNING, raining=True) == "returning_rain"
    assert activity_key(State.DOCKED, raining=True) == "docked_rain_delay"


def test_rain_does_not_rename_mowing_or_error() -> None:
    # A mower that is mowing is not rained off, and a fault outranks the weather.
    from deebot_client.models import State

    from custom_components.ecovacs_mower.sensor import activity_key

    assert activity_key(State.CLEANING, raining=True) == "mowing"
    assert activity_key(State.ERROR, raining=True) == "error"


def test_activity_without_rain_is_the_plain_activity() -> None:
    from deebot_client.models import State

    from custom_components.ecovacs_mower.sensor import activity_key

    assert activity_key(State.DOCKED, raining=False) == "docked"
    assert activity_key(State.RETURNING, raining=False) == "returning"


def test_activity_is_none_before_the_first_state_event() -> None:
    # The rain flag can arrive first; "raining" alone says nothing about what the
    # mower is doing, and an invented activity would be worse than no state.
    from custom_components.ecovacs_mower.sensor import activity_key

    assert activity_key(None, raining=True) is None


def test_activity_options_cover_every_value_the_sensor_can_report() -> None:
    """SensorDeviceClass.ENUM rejects a value that is not in options."""
    from deebot_client.models import State

    from custom_components.ecovacs_mower.sensor import ACTIVITY_OPTIONS, activity_key

    reported = {
        activity_key(state, raining=raining)
        for state in State
        for raining in (True, False)
    }
    assert reported == set(ACTIVITY_OPTIONS)


def test_activity_options_have_no_duplicates() -> None:
    # Two states map to "paused"; a duplicated option makes HA raise.
    from custom_components.ecovacs_mower.sensor import ACTIVITY_OPTIONS

    assert len(ACTIVITY_OPTIONS) == len(set(ACTIVITY_OPTIONS))


def test_every_activity_option_has_a_state_translation() -> None:
    """An enum sensor shows the raw key for a state it has no translation for."""
    import json
    from pathlib import Path

    from custom_components.ecovacs_mower.sensor import ACTIVITY_OPTIONS

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    strings = json.loads((root / "strings.json").read_text(encoding="utf-8"))
    states = strings["entity"]["sensor"]["activity"]["state"]

    assert set(states) == set(ACTIVITY_OPTIONS)


def test_no_stale_activity_state_icons() -> None:
    import json
    from pathlib import Path

    from custom_components.ecovacs_mower.sensor import ACTIVITY_OPTIONS

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    icons = json.loads((root / "icons.json").read_text(encoding="utf-8"))

    assert set(icons["entity"]["sensor"]["activity"]["state"]) <= set(ACTIVITY_OPTIONS)


async def test_activity_sensor_is_only_created_for_mowers() -> None:
    """The sensor reads a mower's activity; a vacuum's states are not these."""
    from unittest.mock import MagicMock

    from deebot_client.capabilities import DeviceType

    from custom_components.ecovacs_mower.sensor import (
        EcovacsActivitySensor,
        async_setup_entry,
    )

    def _device(did: str, device_type: DeviceType) -> MagicMock:
        device = MagicMock()
        device.device_info = {"did": did}
        device.capabilities.device_type = device_type
        return device

    added: list[object] = []
    config_entry = MagicMock()
    config_entry.runtime_data.devices = [
        _device("mower-1", DeviceType.MOWER),
        _device("vacuum-1", DeviceType.VACUUM),
    ]

    await async_setup_entry(MagicMock(), config_entry, added.extend)

    activity = [e for e in added if isinstance(e, EcovacsActivitySensor)]
    assert [e._attr_unique_id for e in activity] == ["mower-1_activity"]
