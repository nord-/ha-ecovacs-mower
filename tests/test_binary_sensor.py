"""The binary sensors mirror the flags in one onProtectState payload.

The module under test imports Home Assistant, so the imports live inside the
test functions and the file is marked ``requires_ha`` — see test_lawn_mower.py
for why. The parsing of the payload itself is tested in
``tests/deebot_patch/test_messages.py``, which needs no HA and runs anywhere.
"""

import json
from pathlib import Path

from tests import requires_ha

pytestmark = requires_ha

ROOT = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"

EXPECTED_KEYS = {
    "rain_protect",
    "rain_delay",
    "emergency_stop",
    "locked",
    "animal_protect",
}


def test_expected_binary_sensor_keys() -> None:
    """Locks the set. If it changes, that must be a decision, not an accident."""
    from custom_components.ecovacs_mower.binary_sensor import ENTITY_DESCRIPTIONS

    assert {d.key for d in ENTITY_DESCRIPTIONS} == EXPECTED_KEYS


def test_every_flag_of_the_event_has_a_sensor() -> None:
    """The event's fields and the entities must not drift apart.

    A flag added to ``MowerProtectStateEvent`` without an entity is parsed and
    then thrown away, which is invisible at runtime.
    """
    from dataclasses import fields

    from custom_components.ecovacs_mower.binary_sensor import ENTITY_DESCRIPTIONS
    from custom_components.ecovacs_mower.deebot_patch.messages import (
        MowerProtectStateEvent,
    )

    assert {f.name for f in fields(MowerProtectStateEvent)} == {
        d.key for d in ENTITY_DESCRIPTIONS
    }


def test_value_fn_reads_the_matching_flag() -> None:
    """Each description must read its own field, not a neighbour's.

    Five near-identical lambdas are exactly where a copy-paste slip hides, and
    it would surface as "the mower reports rain when it is really locked".
    """
    from custom_components.ecovacs_mower.binary_sensor import ENTITY_DESCRIPTIONS
    from custom_components.ecovacs_mower.deebot_patch.messages import (
        MowerProtectStateEvent,
    )

    all_false = MowerProtectStateEvent(**dict.fromkeys(EXPECTED_KEYS, False))
    for description in ENTITY_DESCRIPTIONS:
        only_this_one = MowerProtectStateEvent(
            **{key: key == description.key for key in EXPECTED_KEYS}
        )
        assert description.value_fn(only_this_one) is True, description.key
        assert description.value_fn(all_false) is False, description.key


def test_rain_protect_is_the_only_moisture_flag() -> None:
    """``rain_protect`` reports wet or dry, so it says so; the rest do not.

    Two samples, with the mower's rain protection switched on in both, settle
    what one sample could not: ``isRainProtect`` was 1 two seconds before a
    rain-stopped run, and 0 on a dry day with the mower under cover (the
    ``getProtectState`` answer from firmware 1.13.10). A flag that moves while
    the setting stands still is not the setting.

    ``rain_delay`` deliberately stays classless: it is the hold period the
    mower waits out after rain, not a moisture reading. See
    ``MowerProtectStateEvent``.
    """
    from homeassistant.components.binary_sensor import BinarySensorDeviceClass

    from custom_components.ecovacs_mower.binary_sensor import ENTITY_DESCRIPTIONS

    classes = {d.key: d.device_class for d in ENTITY_DESCRIPTIONS}
    assert classes.pop("rain_protect") is BinarySensorDeviceClass.MOISTURE
    assert set(classes.values()) == {None}


def test_rain_is_the_only_non_diagnostic_pair() -> None:
    """Rain explains the mower's behaviour; the rest is diagnostics.

    Not a style choice: a diagnostic entity is hidden from the device page's
    main section, which is the wrong place for the reason a scheduled run
    stopped.
    """
    from homeassistant.const import EntityCategory

    from custom_components.ecovacs_mower.binary_sensor import ENTITY_DESCRIPTIONS

    primary = {
        d.key
        for d in ENTITY_DESCRIPTIONS
        if d.entity_category is not EntityCategory.DIAGNOSTIC
    }
    assert primary == {"rain_protect", "rain_delay"}


def test_every_description_has_a_translation() -> None:
    """A missing key yields raw strings in the UI."""
    from custom_components.ecovacs_mower.binary_sensor import ENTITY_DESCRIPTIONS

    strings = json.loads((ROOT / "strings.json").read_text(encoding="utf-8"))
    names = strings["entity"]["binary_sensor"]

    for description in ENTITY_DESCRIPTIONS:
        assert description.translation_key in names, description.key


def test_every_binary_sensor_has_an_icon() -> None:
    """Without its own icon the entity gets HA's generic one — easy to miss."""
    from custom_components.ecovacs_mower.binary_sensor import ENTITY_DESCRIPTIONS

    icons = json.loads((ROOT / "icons.json").read_text(encoding="utf-8"))
    names = icons["entity"]["binary_sensor"]

    for description in ENTITY_DESCRIPTIONS:
        assert description.translation_key in names, description.key


def test_no_stale_binary_sensor_translations_or_icons() -> None:
    """Every key in strings.json/icons.json must belong to a real entity."""
    from custom_components.ecovacs_mower.binary_sensor import (
        ENTITY_DESCRIPTIONS,
        EcovacsFaultBinarySensor,
    )

    strings = json.loads((ROOT / "strings.json").read_text(encoding="utf-8"))
    icons = json.loads((ROOT / "icons.json").read_text(encoding="utf-8"))
    keys = {d.translation_key for d in ENTITY_DESCRIPTIONS if d.translation_key} | {
        EcovacsFaultBinarySensor.entity_description.translation_key
    }

    assert set(strings["entity"]["binary_sensor"]) <= keys
    assert set(icons["entity"]["binary_sensor"]) <= keys


def test_the_fault_sensor_has_a_translation_and_an_icon() -> None:
    """Not in ENTITY_DESCRIPTIONS, so the loops above never reach it."""
    from custom_components.ecovacs_mower.binary_sensor import EcovacsFaultBinarySensor

    key = EcovacsFaultBinarySensor.entity_description.translation_key
    strings = json.loads((ROOT / "strings.json").read_text(encoding="utf-8"))
    icons = json.loads((ROOT / "icons.json").read_text(encoding="utf-8"))

    assert key in strings["entity"]["binary_sensor"]
    assert key in icons["entity"]["binary_sensor"]


def test_the_fault_sensor_is_a_problem_the_user_can_see() -> None:
    """Issue #53 asked for something to automate on.

    Enabled by default and not diagnostic, unlike ``sensor.<device>_error``:
    the fault this entity reports is the one the raw error sensor was reading
    ``0`` for while a jammed mower sat on the lawn for 38 minutes. An entity
    nobody finds warns nobody.
    """
    from homeassistant.components.binary_sensor import BinarySensorDeviceClass

    from custom_components.ecovacs_mower.binary_sensor import EcovacsFaultBinarySensor

    description = EcovacsFaultBinarySensor.entity_description
    assert description.device_class is BinarySensorDeviceClass.PROBLEM
    assert description.entity_registry_enabled_default is True
    assert description.entity_category is None


def test_the_fault_sensor_starts_off_and_not_unknown() -> None:
    """The latch publishes only on a change, so a healthy mower publishes nothing.

    Without a default the entity would sit at ``unknown`` forever on a mower
    that has never faulted, which on a problem sensor reads as a broken sensor
    rather than a working one with nothing to report.

    Read through an instance, not off the class: HA's ``CachedProperties``
    metaclass turns ``_attr_is_on`` into a property, so the class attribute is
    the property object rather than the default it carries.
    """
    from unittest.mock import Mock

    from custom_components.ecovacs_mower.binary_sensor import EcovacsFaultBinarySensor

    device = Mock()
    device.device_info = {"did": "test-did"}

    assert EcovacsFaultBinarySensor(device).is_on is False


def test_platform_is_forwarded() -> None:
    """A platform file that is not in PLATFORMS is never loaded at all."""
    from homeassistant.const import Platform

    from custom_components.ecovacs_mower import PLATFORMS

    assert Platform.BINARY_SENSOR in PLATFORMS
