"""Tests for the writable per-area mowing parameter number entities."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from tests import requires_ha

pytestmark = requires_ha


def _device() -> MagicMock:
    device = MagicMock()
    device.device_info = {"did": "did", "name": "name", "class": "e4gqia"}
    device.capabilities = MagicMock()
    device.events = MagicMock()
    return device


def _mapping():
    from custom_components.ecovacs_mower.area_numbers import AREA_PARAMETER_MAPPINGS

    return AREA_PARAMETER_MAPPINGS["e4gqia"]


def _full_area(area_id: str = "12"):
    from custom_components.ecovacs_mower.deebot_patch.areas import MowerArea

    return MowerArea(
        area_id, mow_height_level=7, cut_mode=4, obstacle_height=15, angle=90
    )


def _seed_area(device: MagicMock, area) -> None:
    """Seed the authoritative area snapshot ``area_for`` reads.

    ``area_for``/``_areas_for`` are ``deebot_patch.areas`` internals; tests
    reach into them directly because nothing public populates this state
    outside of a real ``getAreaParameter``/``getAreaSet`` exchange.
    """
    from custom_components.ecovacs_mower.deebot_patch import areas

    areas._areas_for(device.events)[area.area_id] = area


def _cutting_height_description():
    from custom_components.ecovacs_mower.area_numbers import area_number_descriptions

    return area_number_descriptions("12", area_mapping=_mapping())[0]


def _mowing_speed_description():
    from custom_components.ecovacs_mower.area_numbers import area_number_descriptions

    return area_number_descriptions("12", area_mapping=_mapping())[1]


def test_build_set_area_parameter_refuses_an_incomplete_snapshot() -> None:
    from custom_components.ecovacs_mower.area_numbers import build_set_area_parameter

    incomplete = {
        "mow_height_level": 7,
        "cut_mode": None,
        "obstacle_height": 15,
        "angle": 90,
    }
    assert build_set_area_parameter("12", incomplete, "mow_height_level", 6) is None


def test_build_set_area_parameter_merges_the_other_three_values() -> None:
    from custom_components.ecovacs_mower.area_numbers import build_set_area_parameter

    current = {
        "mow_height_level": 7,
        "cut_mode": 4,
        "obstacle_height": 15,
        "angle": 90,
    }
    command = build_set_area_parameter("12", current, "cut_mode", 2)
    assert command._args["mowHeightLevel"] == 7
    assert command._args["cutMode"] == 2
    assert command._args["obstacleHeight"] == 15
    assert command._args["angle"] == 90


async def test_write_raises_when_the_area_has_not_reported_yet() -> None:
    from custom_components.ecovacs_mower.area_numbers import (
        EcovacsAreaNumber,
        _PendingAreaWrite,
    )
    from homeassistant.exceptions import HomeAssistantError

    device = _device()
    entity = EcovacsAreaNumber(
        device, "12", _cutting_height_description(), "North lawn", _PendingAreaWrite()
    )
    entity._execute_command = AsyncMock()

    try:
        await entity.async_set_native_value(7)
        raise AssertionError("expected HomeAssistantError")
    except HomeAssistantError:
        pass
    entity._execute_command.assert_not_called()


def test_every_slider_position_maps_to_a_raw_speed() -> None:
    # The frontend sends min + step * n, which is not always the float the
    # table spells: 0.40 + 0.05 * 4 is 0.6000000000000001.
    speed = _mapping().cut_speed
    positions = [
        speed.native_min_value + speed.native_step * n
        for n in range(len(speed.values))
    ]

    assert [speed.to_raw(value) for value in positions] == [7, 6, 5, 4, 3, 2, 1]


async def test_write_raises_for_an_unrepresentable_value() -> None:
    from custom_components.ecovacs_mower.area_numbers import (
        EcovacsAreaNumber,
        _PendingAreaWrite,
    )
    from homeassistant.exceptions import HomeAssistantError

    device = _device()
    _seed_area(device, _full_area())
    entity = EcovacsAreaNumber(
        device, "12", _cutting_height_description(), "North lawn", _PendingAreaWrite()
    )
    entity._execute_command = AsyncMock()

    try:
        # 12cm is not one of the seven validated A1600 heights.
        await entity.async_set_native_value(12)
        raise AssertionError("expected HomeAssistantError")
    except HomeAssistantError:
        pass
    entity._execute_command.assert_not_called()


async def test_a_successful_write_sends_one_complete_command_and_refreshes() -> None:
    from custom_components.ecovacs_mower.area_numbers import (
        EcovacsAreaNumber,
        _PendingAreaWrite,
    )

    device = _device()
    _seed_area(device, _full_area())
    entity = EcovacsAreaNumber(
        device, "12", _cutting_height_description(), "North lawn", _PendingAreaWrite()
    )
    entity._execute_command = AsyncMock()

    await entity.async_set_native_value(6)

    entity._execute_command.assert_called_once()
    command = entity._execute_command.call_args[0][0]
    assert command._args["mowHeightLevel"] == 4  # raw value for 6cm
    assert command._args["cutMode"] == 4
    device.events.request_refresh.assert_called_once()


async def test_back_to_back_writes_do_not_revert_each_other() -> None:
    """Regression test for the stale-snapshot race the Copilot review found.

    Two fast writes to different parameters of the same area must not use
    the same unconfirmed authoritative snapshot for both: the second write
    has to build on the first write's *sent* values, not the pre-first-write
    state, or it silently reverts the first change.
    """
    from custom_components.ecovacs_mower.area_numbers import (
        EcovacsAreaNumber,
        _PendingAreaWrite,
    )

    device = _device()
    _seed_area(device, _full_area())
    pending = _PendingAreaWrite()
    height_entity = EcovacsAreaNumber(
        device, "12", _cutting_height_description(), "North lawn", pending
    )
    speed_entity = EcovacsAreaNumber(
        device, "12", _mowing_speed_description(), "North lawn", pending
    )
    height_entity._execute_command = AsyncMock()
    speed_entity._execute_command = AsyncMock()

    # The authoritative snapshot is deliberately left unchanged here to
    # simulate the mower's confirmation not having arrived yet.
    await height_entity.async_set_native_value(6)
    await speed_entity.async_set_native_value(0.55)

    second_command = speed_entity._execute_command.call_args[0][0]
    assert second_command._args["mowHeightLevel"] == 4  # 6cm from the first write
    assert second_command._args["cutMode"] == 4  # 0.55 m/s


def _area_pair(device: MagicMock):
    """The height and speed entities of area 12, sharing one pending write."""
    from custom_components.ecovacs_mower.area_numbers import (
        EcovacsAreaNumber,
        _PendingAreaWrite,
    )

    pending = _PendingAreaWrite()
    height = EcovacsAreaNumber(
        device, "12", _cutting_height_description(), "North lawn", pending
    )
    speed = EcovacsAreaNumber(
        device, "12", _mowing_speed_description(), "North lawn", pending
    )
    return height, speed


async def test_overlapping_writes_do_not_revert_each_other() -> None:
    """Two writes in flight at once: the second must wait for the first."""
    import asyncio

    device = _device()
    _seed_area(device, _full_area())
    height, speed = _area_pair(device)
    answered = asyncio.Event()
    sent = []

    async def slow_mower(command) -> bool:
        sent.append(command)
        await answered.wait()
        return True

    height._execute_command = slow_mower
    speed._execute_command = slow_mower

    writes = asyncio.gather(
        height.async_set_native_value(6), speed.async_set_native_value(0.55)
    )
    await asyncio.sleep(0)
    answered.set()
    await writes

    assert [command._args["mowHeightLevel"] for command in sent] == [4, 4]
    assert sent[1]._args["cutMode"] == 4  # 0.55 m/s


async def test_a_write_the_mower_did_not_confirm_is_not_merged_into_the_next() -> None:
    device = _device()
    _seed_area(device, _full_area())
    height, speed = _area_pair(device)
    height._execute_command = AsyncMock(return_value=False)
    speed._execute_command = AsyncMock(return_value=True)

    await height.async_set_native_value(6)
    await speed.async_set_native_value(0.55)

    assert speed._execute_command.call_args[0][0]._args["mowHeightLevel"] == 7


async def test_a_repeated_report_still_retires_the_pending_write() -> None:
    """The mower ignored the write and reported the old values unchanged.

    The bus drops that repeat before any subscriber sees it, so the pending
    write has to be retired by the handler's count, not by an event.
    """
    from custom_components.ecovacs_mower.deebot_patch.areas import (
        apply_area_parameters,
    )

    device = _device()
    _seed_area(device, _full_area())
    height, speed = _area_pair(device)
    height._execute_command = AsyncMock(return_value=True)
    speed._execute_command = AsyncMock(return_value=True)

    await height.async_set_native_value(6)
    apply_area_parameters(
        device.events,
        [
            {
                "areaID": "12",
                "mowHeightLevel": 7,
                "cutMode": 4,
                "obstacleHeight": 15,
                "angle": 90,
            }
        ],
    )
    await speed.async_set_native_value(0.55)

    assert speed._execute_command.call_args[0][0]._args["mowHeightLevel"] == 7


async def test_a_report_for_another_area_keeps_the_pending_write() -> None:
    """A push carrying only area 13 says nothing about area 12's write."""
    from custom_components.ecovacs_mower.deebot_patch.areas import (
        apply_area_parameters,
    )

    device = _device()
    _seed_area(device, _full_area())
    height, speed = _area_pair(device)
    height._execute_command = AsyncMock(return_value=True)
    speed._execute_command = AsyncMock(return_value=True)

    await height.async_set_native_value(6)
    apply_area_parameters(
        device.events,
        [
            {
                "areaID": "13",
                "mowHeightLevel": 2,
                "cutMode": 3,
                "obstacleHeight": 1,
                "angle": 0,
            }
        ],
    )
    await speed.async_set_native_value(0.55)

    assert speed._execute_command.call_args[0][0]._args["mowHeightLevel"] == 4


async def test_a_removed_area_becomes_unavailable_and_recovers() -> None:
    from custom_components.ecovacs_mower.deebot_patch.areas import MowerAreaEvent

    device = _device()
    height, _ = _area_pair(device)
    height.async_write_ha_state = MagicMock()
    area = _full_area()

    await height._on_area_state(MowerAreaEvent((area,)))
    assert height.available
    assert height.native_value == 3  # raw 7

    await height._on_area_state(MowerAreaEvent(()))
    assert not height.available
    assert height.native_value is None

    await height._on_area_state(MowerAreaEvent((area,)))
    assert height.available
    assert height.native_value == 3


async def test_an_area_report_writes_the_state_once_and_renames_only_on_change() -> None:
    from dataclasses import replace
    from unittest.mock import patch

    from custom_components.ecovacs_mower.deebot_patch.areas import MowerAreaEvent

    device = _device()
    height, _ = _area_pair(device)
    height.hass = MagicMock()
    height.entity_id = "number.north_lawn_cutting_height"
    height.async_write_ha_state = MagicMock()
    area = replace(_full_area(), name="North lawn")

    with patch("custom_components.ecovacs_mower.area_numbers.er") as er:
        await height._on_area_state(MowerAreaEvent((area,)))
        assert height.async_write_ha_state.call_count == 1
        er.async_get.assert_not_called()

        await height._on_area_state(
            MowerAreaEvent((replace(area, name="South lawn"),))
        )
        assert height.async_write_ha_state.call_count == 2
        er.async_get.return_value.async_update_entity.assert_called_once_with(
            "number.north_lawn_cutting_height",
            original_name="South lawn - Cutting height",
        )


async def test_discovery_never_writes_an_entity_state() -> None:
    """A disabled entity was never added, and writing its state raises.

    HA aborts adding a disabled entity without raising, so the platform's own
    handler must leave the entities it created alone; each one projects its
    area from its own subscription, which a disabled entity never makes.
    """
    from custom_components.ecovacs_mower.area_numbers import (
        _setup_device_area_numbers,
    )
    from custom_components.ecovacs_mower.deebot_patch.areas import MowerAreaEvent

    device = _device()
    config_entry = MagicMock()
    add_entities = MagicMock()

    _setup_device_area_numbers(device, config_entry, add_entities, _mapping())
    on_area_state = device.events.subscribe.call_args[0][1]
    area = _full_area()

    await on_area_state(MowerAreaEvent((area,)))
    entities = add_entities.call_args[0][0]
    for entity in entities:
        entity.async_write_ha_state = MagicMock(
            side_effect=RuntimeError("Attribute hass is None")
        )

    await on_area_state(MowerAreaEvent((area, _full_area("13"))))
    await on_area_state(MowerAreaEvent((_full_area("13"),)))

    assert add_entities.call_count == 2  # area 13 was still discovered
    for entity in entities:
        entity.async_write_ha_state.assert_not_called()


def _entry_with(device: MagicMock) -> MagicMock:
    config_entry = MagicMock()
    config_entry.runtime_data.devices = [device]
    return config_entry


@pytest.mark.parametrize(
    "class_",
    [
        # Patched, but its area parameters were never validated.
        "9bts2s",
        # Not a patched class at all.
        "npwtuz",
    ],
)
async def test_no_area_entities_without_a_validated_mapping(class_) -> None:
    from deebot_client.capabilities import DeviceType

    from custom_components.ecovacs_mower.area_numbers import async_setup_area_numbers

    device = _device()
    device.device_info["class"] = class_
    device.capabilities.device_type = DeviceType.MOWER
    add_entities = MagicMock()

    await async_setup_area_numbers(_entry_with(device), add_entities)

    device.events.subscribe.assert_not_called()
    add_entities.assert_not_called()


async def test_no_area_entities_for_a_vacuum() -> None:
    from deebot_client.capabilities import DeviceType

    from custom_components.ecovacs_mower.area_numbers import async_setup_area_numbers

    device = _device()
    device.capabilities.device_type = DeviceType.VACUUM

    await async_setup_area_numbers(_entry_with(device), MagicMock())

    device.events.subscribe.assert_not_called()


async def test_the_validated_mower_gets_area_discovery() -> None:
    from deebot_client.capabilities import DeviceType

    from custom_components.ecovacs_mower.area_numbers import async_setup_area_numbers
    from custom_components.ecovacs_mower.deebot_patch.areas import MowerAreaEvent

    device = _device()
    device.capabilities.device_type = DeviceType.MOWER

    await async_setup_area_numbers(_entry_with(device), MagicMock())

    assert device.events.subscribe.call_args[0][0] is MowerAreaEvent


@pytest.mark.parametrize(
    ("wire", "app"),
    [(0, 270), (270, 0), (271, 359), (90, 180), (359, 271)],
)
def test_the_a1600_cut_angle_round_trips(wire, app) -> None:
    angle = _mapping().cut_angle

    assert angle.to_native(wire) == app
    assert angle.to_raw(app) == wire


def test_the_a1600_cut_angle_rejects_values_outside_a_circle() -> None:
    angle = _mapping().cut_angle

    assert angle.to_native(360) is None
    assert angle.to_raw(-1) is None
