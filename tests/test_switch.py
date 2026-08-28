"""The switches must correspond to the settings a GOAT has."""

import pytest

from tests import requires_ha

pytestmark = requires_ha


def _all_translation_keys() -> set[str]:
    """Every switch's translation key, including the standalone rain switch.

    The rain sensor's switch is not in ENTITY_DESCRIPTIONS and cannot be: the
    setting is not a deebot-client capability, so there is no field for
    capability_fn to read (issue #54). Reading its key off the class instead of
    a hardcoded literal means a typo there fails these tests instead of merely
    looking like a permitted extra key.
    """
    from custom_components.ecovacs_mower.switch import (
        ENTITY_DESCRIPTIONS,
        EcovacsRainDetectionSwitch,
    )

    return {d.translation_key for d in ENTITY_DESCRIPTIONS} | {
        EcovacsRainDetectionSwitch.entity_description.translation_key
    }


def test_expected_switch_keys() -> None:
    """Locks the set. If it changes, that must be a decision, not an accident."""
    from custom_components.ecovacs_mower.switch import ENTITY_DESCRIPTIONS

    assert {d.key for d in ENTITY_DESCRIPTIONS} == {
        "advanced_mode",
        "true_detect",
        "border_switch",
        "child_lock",
        "move_up_warning",
        "cross_map_border_warning",
        "safe_protect",
    }


def test_no_vacuum_only_switches() -> None:
    """The capabilities do not exist on 2i0fns, so the entities would be empty anyway."""
    from custom_components.ecovacs_mower.switch import ENTITY_DESCRIPTIONS

    keys = {d.key for d in ENTITY_DESCRIPTIONS}
    assert keys.isdisjoint(
        {"continuous_cleaning", "carpet_auto_fan_boost", "clean_preference", "border_spin"}
    )


def test_every_description_has_a_translation() -> None:
    """A missing key yields raw strings in the UI."""
    import json
    from pathlib import Path

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    strings = json.loads((root / "strings.json").read_text(encoding="utf-8"))
    names = strings["entity"]["switch"]

    for key in _all_translation_keys():
        assert key in names, key


def test_every_switch_has_an_icon() -> None:
    """A switch without its own icon gets HA's generic toggle — easy to miss."""
    import json
    from pathlib import Path

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    icons = json.loads((root / "icons.json").read_text(encoding="utf-8"))
    names = icons["entity"]["switch"]

    for key in _all_translation_keys():
        assert key in names, key


def test_no_stale_switch_translations_or_icons() -> None:
    """Every key in strings.json/icons.json must belong to a real switch.

    The converse of the tests above: they check description -> string/icon, not
    the other way around. Without this, a leftover key for a removed switch would
    go unnoticed.
    """
    import json
    from pathlib import Path

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    strings = json.loads((root / "strings.json").read_text(encoding="utf-8"))
    icons = json.loads((root / "icons.json").read_text(encoding="utf-8"))

    keys = _all_translation_keys()
    assert set(strings["entity"]["switch"]) <= keys
    assert set(icons["entity"]["switch"]) <= keys


def _rain_switch(device):
    from custom_components.ecovacs_mower.switch import EcovacsRainDetectionSwitch

    entity = EcovacsRainDetectionSwitch(device)
    entity.async_write_ha_state = lambda: None
    return entity


def _device():
    from unittest.mock import AsyncMock, Mock

    device = Mock()
    device.device_info = {"did": "test-did"}
    device.execute_command = AsyncMock(return_value={"ret": "ok"})
    return device


async def _rain_switch_callback(device):
    """Run async_added_to_hass and return the MowerRainDelayEvent callback."""
    from custom_components.ecovacs_mower.deebot_patch.messages import (
        MowerRainDelayEvent,
    )

    entity = _rain_switch(device)
    await entity.async_added_to_hass()

    for call in device.events.subscribe.call_args_list:
        event_type, callback = call.args
        if event_type is MowerRainDelayEvent:
            return entity, callback
    raise AssertionError("The rain switch never subscribed to MowerRainDelayEvent")


def test_the_rain_switch_is_not_capability_driven() -> None:
    """It cannot be, so it must not quietly reappear in the declarative table.

    ``get_supported_entities`` builds an entity only when ``capability_fn``
    returns something, and ``Capabilities`` has no field for this setting.
    """
    from custom_components.ecovacs_mower.switch import (
        ENTITY_DESCRIPTIONS,
        EcovacsRainDetectionSwitch,
    )

    assert EcovacsRainDetectionSwitch.entity_description.key not in {
        d.key for d in ENTITY_DESCRIPTIONS
    }


def test_the_rain_switch_is_a_config_entity_disabled_by_default() -> None:
    """Same treatment as the seven capability-backed settings switches."""
    from homeassistant.const import EntityCategory

    from custom_components.ecovacs_mower.switch import EcovacsRainDetectionSwitch

    description = EcovacsRainDetectionSwitch.entity_description
    assert description.key == "rain_detection"
    assert description.entity_category is EntityCategory.CONFIG
    assert description.entity_registry_enabled_default is False


def test_the_rain_switch_is_unknown_before_the_first_event() -> None:
    """Not False.

    The capability switches default to off because the library refreshes them
    on subscribe; this one is honest about knowing nothing yet, and reporting
    "the rain sensor is off" from a mower nobody has asked is worse than a
    blank.
    """
    assert _rain_switch(_device()).is_on is None


@pytest.mark.parametrize("enabled", [True, False])
async def test_the_rain_switch_follows_the_setting(enabled: bool) -> None:
    from custom_components.ecovacs_mower.deebot_patch.messages import (
        MowerRainDelayEvent,
    )

    entity, callback = await _rain_switch_callback(_device())
    await callback(MowerRainDelayEvent(enabled=enabled, delay=180))

    assert entity.is_on is enabled


@pytest.mark.parametrize(
    ("call", "expected"), [("async_turn_on", 1), ("async_turn_off", 0)]
)
async def test_the_rain_switch_carries_the_delay_along(
    call: str, expected: int
) -> None:
    """setRainDelay wants the pair, so the toggle has to resend the duration.

    Sending the toggle alone is the one way this entity could silently reset
    the owner's three-hour hold, which is why the value is read back off the
    last event rather than defaulted.
    """
    from custom_components.ecovacs_mower.deebot_patch.commands import SetRainDelay
    from custom_components.ecovacs_mower.deebot_patch.messages import (
        MowerRainDelayEvent,
    )

    device = _device()
    entity, callback = await _rain_switch_callback(device)
    await callback(MowerRainDelayEvent(enabled=not expected, delay=180))

    await getattr(entity, call)()

    (command,) = device.execute_command.call_args.args
    assert isinstance(command, SetRainDelay)
    assert command._args == {"enable": expected, "delay": 180}


async def test_the_rain_switch_requests_a_refresh_after_writing() -> None:
    """The device pushing ``onRainDelay`` on its own answer is unconfirmed.

    Asking for a refresh is what re-reads the state if that push never comes,
    instead of leaving the frontend's optimistic value to flip back once its
    timeout expires (issue #42).
    """
    from custom_components.ecovacs_mower.deebot_patch.messages import (
        MowerRainDelayEvent,
    )

    device = _device()
    entity, callback = await _rain_switch_callback(device)
    await callback(MowerRainDelayEvent(enabled=False, delay=180))

    await entity.async_turn_on()

    device.events.request_refresh.assert_called_once_with(MowerRainDelayEvent)


async def test_the_rain_switch_refuses_to_write_before_it_knows_the_delay() -> None:
    """A payload can carry ``enable`` without ``delay``; guessing one is not on.

    Any default here is a number the owner did not choose, written to the
    device as if they had.
    """
    from homeassistant.exceptions import HomeAssistantError

    from custom_components.ecovacs_mower.deebot_patch.messages import (
        MowerRainDelayEvent,
    )

    device = _device()
    entity, callback = await _rain_switch_callback(device)
    await callback(MowerRainDelayEvent(enabled=True, delay=None))

    with pytest.raises(HomeAssistantError):
        await entity.async_turn_off()
    device.execute_command.assert_not_called()
