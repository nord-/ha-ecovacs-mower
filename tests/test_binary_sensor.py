"""The rain flag: the one lasting trace of a rained-off run.

The module under test imports Home Assistant, which cannot be imported on
Windows (fcntl). Imports live inside the tests and the file is marked
requires_ha. The source of truth is CI on ubuntu-latest.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from . import requires_ha

pytestmark = requires_ha


def _rain_sensor():
    """A rain sensor wired to a mock device, without hass."""
    from custom_components.ecovacs_mower.binary_sensor import EcovacsRainSensor

    device = MagicMock()
    device.device_info = {"did": "did-1"}
    device.execute_command = AsyncMock()
    return EcovacsRainSensor(device), device


def test_key_and_device_class() -> None:
    """MOISTURE is what makes the state read "Wet"/"Dry" rather than "On"/"Off"."""
    from homeassistant.components.binary_sensor import BinarySensorDeviceClass

    from custom_components.ecovacs_mower.binary_sensor import EcovacsRainSensor

    description = EcovacsRainSensor.entity_description
    assert description.key == "rain"
    assert description.translation_key == "rain"
    assert description.device_class is BinarySensorDeviceClass.MOISTURE


def test_unknown_until_the_device_reports() -> None:
    # Defaulting to "dry" would claim it is not raining on the strength of no
    # information at all.
    sensor, _ = _rain_sensor()
    assert sensor.is_on is None


async def test_rain_event_sets_the_flag_and_the_delay_attribute() -> None:
    from custom_components.ecovacs_mower.binary_sensor import ATTR_RAIN_DELAY
    from custom_components.ecovacs_mower.deebot_patch.messages import (
        MowerProtectStateEvent,
    )

    sensor, _ = _rain_sensor()
    with patch.object(sensor, "async_write_ha_state"):
        await sensor._on_protect_state(
            MowerProtectStateEvent(raining=True, rain_delay=False)
        )

    assert sensor.is_on is True
    assert sensor.extra_state_attributes == {ATTR_RAIN_DELAY: False}


async def test_dry_event_clears_the_flag() -> None:
    from custom_components.ecovacs_mower.deebot_patch.messages import (
        MowerProtectStateEvent,
    )

    sensor, _ = _rain_sensor()
    with patch.object(sensor, "async_write_ha_state"):
        await sensor._on_protect_state(
            MowerProtectStateEvent(raining=True, rain_delay=False)
        )
        await sensor._on_protect_state(
            MowerProtectStateEvent(raining=False, rain_delay=True)
        )

    assert sensor.is_on is False


async def test_asks_the_device_for_the_current_flags() -> None:
    # onProtectState only arrives on a change, so without this the flag would be
    # unknown from a restart until the next change of weather.
    from custom_components.ecovacs_mower.deebot_patch.commands import GetProtectState

    sensor, device = _rain_sensor()
    await sensor._async_request_protect_state()

    device.execute_command.assert_awaited_once()
    assert isinstance(device.execute_command.await_args.args[0], GetProtectState)


async def test_a_failing_request_does_not_propagate() -> None:
    # getProtectState is unconfirmed against hardware. A firmware that rejects it
    # must cost nothing more than an unknown flag.
    sensor, device = _rain_sensor()
    device.execute_command.side_effect = RuntimeError("no response")

    await sensor._async_request_protect_state()

    assert sensor.is_on is None


def test_every_description_has_a_translation() -> None:
    """A missing key yields raw strings in the UI."""
    import json
    from pathlib import Path

    from custom_components.ecovacs_mower.binary_sensor import EcovacsRainSensor

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    strings = json.loads((root / "strings.json").read_text(encoding="utf-8"))

    assert (
        EcovacsRainSensor.entity_description.translation_key
        in strings["entity"]["binary_sensor"]
    )


def test_every_binary_sensor_has_an_icon() -> None:
    import json
    from pathlib import Path

    from custom_components.ecovacs_mower.binary_sensor import EcovacsRainSensor

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    icons = json.loads((root / "icons.json").read_text(encoding="utf-8"))

    assert (
        EcovacsRainSensor.entity_description.translation_key
        in icons["entity"]["binary_sensor"]
    )


def test_no_stale_binary_sensor_translations_or_icons() -> None:
    """Every key in strings.json/icons.json must belong to a real entity."""
    import json
    from pathlib import Path

    from custom_components.ecovacs_mower.binary_sensor import EcovacsRainSensor

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    strings = json.loads((root / "strings.json").read_text(encoding="utf-8"))
    icons = json.loads((root / "icons.json").read_text(encoding="utf-8"))

    keys = {EcovacsRainSensor.entity_description.translation_key}
    assert set(strings["entity"]["binary_sensor"]) <= keys
    assert set(icons["entity"]["binary_sensor"]) <= keys


def test_platform_is_registered() -> None:
    """A platform missing from PLATFORMS is never set up."""
    from homeassistant.const import Platform

    from custom_components.ecovacs_mower import PLATFORMS

    assert Platform.BINARY_SENSOR in PLATFORMS


async def test_only_mowers_get_a_rain_flag() -> None:
    """A vacuum has no rain sensor to report from."""
    from deebot_client.capabilities import DeviceType

    from custom_components.ecovacs_mower.binary_sensor import (
        EcovacsRainSensor,
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

    assert all(isinstance(entity, EcovacsRainSensor) for entity in added)
    assert [entity._attr_unique_id for entity in added] == ["mower-1_rain"]
