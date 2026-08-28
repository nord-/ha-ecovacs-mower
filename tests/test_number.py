"""Number entities: volume, cut direction and the rain delay."""

import pytest

from tests import requires_ha

pytestmark = requires_ha


def _all_translation_keys() -> set[str]:
    """Every number's translation key, including the standalone rain delay.

    The rain delay is not in ENTITY_DESCRIPTIONS and cannot be: the setting is
    not a deebot-client capability, so there is no field for capability_fn to
    read (issue #54). Reading its key off the class instead of a hardcoded
    literal means a typo there fails these tests instead of merely looking
    like a permitted extra key.
    """
    from custom_components.ecovacs_mower.number import (
        ENTITY_DESCRIPTIONS,
        EcovacsRainDelayNumber,
    )

    return {d.translation_key for d in ENTITY_DESCRIPTIONS} | {
        EcovacsRainDelayNumber.entity_description.translation_key
    }


def test_expected_number_keys() -> None:
    from custom_components.ecovacs_mower.number import ENTITY_DESCRIPTIONS

    assert {d.key for d in ENTITY_DESCRIPTIONS} == {"volume", "cut_direction"}


def test_cut_direction_is_a_line_orientation() -> None:
    """0-180 degrees, not 0-359.

    The cut direction is a line orientation, not a compass bearing: 180 degrees
    covers every possible stripe pattern, since 190 and 10 give the same result.
    Verified against HA 2026.7.4.
    """
    from custom_components.ecovacs_mower.number import ENTITY_DESCRIPTIONS

    cut_direction = next(d for d in ENTITY_DESCRIPTIONS if d.key == "cut_direction")
    assert cut_direction.native_min_value == 0
    assert cut_direction.native_max_value == 180


def test_every_description_has_a_translation() -> None:
    import json
    from pathlib import Path

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    strings = json.loads((root / "strings.json").read_text(encoding="utf-8"))
    names = strings["entity"]["number"]

    for key in _all_translation_keys():
        assert key in names, key


def test_every_number_has_an_icon() -> None:
    """A number without its own icon gets HA's generic slider — easy to miss."""
    import json
    from pathlib import Path

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    icons = json.loads((root / "icons.json").read_text(encoding="utf-8"))
    names = icons["entity"]["number"]

    for key in _all_translation_keys():
        assert key in names, key


def test_no_stale_number_translations_or_icons() -> None:
    """Every key in strings.json/icons.json must belong to a real number.

    The converse of the tests above: they check description -> string/icon, not
    the other way around. Without this, a leftover key for a removed number would
    go unnoticed.
    """
    import json
    from pathlib import Path

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    strings = json.loads((root / "strings.json").read_text(encoding="utf-8"))
    icons = json.loads((root / "icons.json").read_text(encoding="utf-8"))

    keys = _all_translation_keys()
    assert set(strings["entity"]["number"]) <= keys
    assert set(icons["entity"]["number"]) <= keys


def _device():
    from unittest.mock import AsyncMock, Mock

    device = Mock()
    device.device_info = {"did": "test-did"}
    device.execute_command = AsyncMock(return_value={"ret": "ok"})
    return device


def _rain_delay(device):
    from custom_components.ecovacs_mower.number import EcovacsRainDelayNumber

    entity = EcovacsRainDelayNumber(device)
    entity.async_write_ha_state = lambda: None
    return entity


async def _rain_delay_callback(device):
    """Run async_added_to_hass and return the MowerRainDelayEvent callback."""
    from custom_components.ecovacs_mower.deebot_patch.messages import (
        MowerRainDelayEvent,
    )

    entity = _rain_delay(device)
    await entity.async_added_to_hass()

    for call in device.events.subscribe.call_args_list:
        event_type, callback = call.args
        if event_type is MowerRainDelayEvent:
            return entity, callback
    raise AssertionError("The rain delay never subscribed to MowerRainDelayEvent")


def test_the_rain_delay_is_not_capability_driven() -> None:
    from custom_components.ecovacs_mower.number import (
        ENTITY_DESCRIPTIONS,
        EcovacsRainDelayNumber,
    )

    assert EcovacsRainDelayNumber.entity_description.key not in {
        d.key for d in ENTITY_DESCRIPTIONS
    }


def test_the_rain_delay_is_a_span_of_minutes() -> None:
    """Minutes, and a box rather than a slider.

    The unit is fixed by the reporter's app reading three hours against a
    ``delay`` of 180. The range mirrors Janverhu/ecovacs-goat-g1, which drives
    the same command against a GOAT G1; a full day is a generous ceiling and
    the true one is unknown. A 0-1440 slider would be unusable, hence the box.
    """
    from homeassistant.components.number import NumberMode
    from homeassistant.const import EntityCategory, UnitOfTime

    from custom_components.ecovacs_mower.number import EcovacsRainDelayNumber

    description = EcovacsRainDelayNumber.entity_description
    assert description.key == "rain_delay"
    assert description.native_unit_of_measurement == UnitOfTime.MINUTES
    assert description.native_min_value == 0
    assert description.native_max_value == 1440
    assert description.native_step == 1
    assert description.mode is NumberMode.BOX
    assert description.entity_category is EntityCategory.CONFIG
    assert description.entity_registry_enabled_default is False


def test_the_rain_delay_is_unknown_before_the_first_event() -> None:
    assert _rain_delay(_device()).native_value is None


async def test_the_rain_delay_follows_the_setting() -> None:
    from custom_components.ecovacs_mower.deebot_patch.messages import (
        MowerRainDelayEvent,
    )

    entity, callback = await _rain_delay_callback(_device())
    await callback(MowerRainDelayEvent(enabled=True, delay=180))

    assert entity.native_value == 180


async def test_a_firmware_that_omits_the_delay_leaves_the_entity_blank() -> None:
    """Not 0, which would read as "resumes the moment the rain stops"."""
    from custom_components.ecovacs_mower.deebot_patch.messages import (
        MowerRainDelayEvent,
    )

    entity, callback = await _rain_delay_callback(_device())
    await callback(MowerRainDelayEvent(enabled=True, delay=None))

    assert entity.native_value is None


@pytest.mark.parametrize("enabled", [True, False])
async def test_setting_the_delay_carries_the_sensor_state_along(enabled: bool) -> None:
    """The mirror image of the switch: setRainDelay wants the pair.

    Sending the duration alone is the one way this entity could silently switch
    the owner's rain sensor off.
    """
    from custom_components.ecovacs_mower.deebot_patch.commands import SetRainDelay
    from custom_components.ecovacs_mower.deebot_patch.messages import (
        MowerRainDelayEvent,
    )

    device = _device()
    entity, callback = await _rain_delay_callback(device)
    await callback(MowerRainDelayEvent(enabled=enabled, delay=180))

    await entity.async_set_native_value(90.0)

    (command,) = device.execute_command.call_args.args
    assert isinstance(command, SetRainDelay)
    assert command._args == {"enable": int(enabled), "delay": 90}


async def test_the_rain_delay_requests_a_refresh_after_writing() -> None:
    """Mirror of the switch's post-write refresh — see its test for why."""
    from custom_components.ecovacs_mower.deebot_patch.messages import (
        MowerRainDelayEvent,
    )

    device = _device()
    entity, callback = await _rain_delay_callback(device)
    await callback(MowerRainDelayEvent(enabled=True, delay=180))

    await entity.async_set_native_value(90.0)

    device.events.request_refresh.assert_called_once_with(MowerRainDelayEvent)


async def test_the_rain_delay_refuses_to_write_before_it_knows_the_sensor() -> None:
    """Nothing has arrived yet, so ``enable`` would have to be invented."""
    from homeassistant.exceptions import HomeAssistantError

    device = _device()
    entity = _rain_delay(device)

    with pytest.raises(HomeAssistantError):
        await entity.async_set_native_value(90.0)
    device.execute_command.assert_not_called()
