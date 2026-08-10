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
        EcovacsErrorSensor,
    )

    keys = {d.key for d in ENTITY_DESCRIPTIONS} | {
        EcovacsErrorSensor.entity_description.key
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
        EcovacsErrorSensor,
    )

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    strings = json.loads((root / "strings.json").read_text(encoding="utf-8"))
    names = strings["entity"]["sensor"]

    descriptions = (
        *ENTITY_DESCRIPTIONS,
        *LIFESPAN_ENTITY_DESCRIPTIONS,
        EcovacsErrorSensor.entity_description,
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
        EcovacsErrorSensor,
    )

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    icons = json.loads((root / "icons.json").read_text(encoding="utf-8"))
    names = icons["entity"]["sensor"]

    descriptions = (
        *ENTITY_DESCRIPTIONS,
        *LIFESPAN_ENTITY_DESCRIPTIONS,
        EcovacsErrorSensor.entity_description,
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
        EcovacsErrorSensor,
    )

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    strings = json.loads((root / "strings.json").read_text(encoding="utf-8"))
    icons = json.loads((root / "icons.json").read_text(encoding="utf-8"))

    descriptions = (
        *ENTITY_DESCRIPTIONS,
        *LIFESPAN_ENTITY_DESCRIPTIONS,
        EcovacsErrorSensor.entity_description,
    )
    keys = {d.translation_key for d in descriptions if d.translation_key}

    assert set(strings["entity"]["sensor"]) <= keys
    assert set(icons["entity"]["sensor"]) <= keys
