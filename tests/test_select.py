"""The edge mode select (issue #166)."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from tests import requires_ha

pytestmark = requires_ha

ROOT = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"


def _device():
    device = Mock()
    device.device_info = {"did": "test-did"}
    device.execute_command = AsyncMock(return_value={"ret": "ok"})
    return device


def _select(device):
    from custom_components.ecovacs_mower.select import EcovacsEdgeModeSelect

    entity = EcovacsEdgeModeSelect(device)
    entity.async_write_ha_state = lambda: None
    return entity


def _report(device, data: dict) -> None:
    """Let the mower report its edge setting, as a push or an answer does."""
    from custom_components.ecovacs_mower.deebot_patch.messages import OnBorderSwitch

    OnBorderSwitch.handle(device.events, {"header": {}, "body": {"data": data}})


async def _select_callback(device):
    """Run async_added_to_hass and return the MowerBorderSwitchEvent callback."""
    from custom_components.ecovacs_mower.deebot_patch.messages import (
        MowerBorderSwitchEvent,
    )

    entity = _select(device)
    await entity.async_added_to_hass()

    for call in device.events.subscribe.call_args_list:
        event_type, callback = call.args
        if event_type is MowerBorderSwitchEvent:
            return entity, callback
    raise AssertionError("The select never subscribed to MowerBorderSwitchEvent")


def _mower(class_: str):
    from deebot_client.capabilities import DeviceType

    device = MagicMock()
    device.capabilities.device_type = DeviceType.MOWER
    device.device_info = {"did": f"did-{class_}", "class": class_}
    return device


def test_platform_is_forwarded() -> None:
    """A platform file that is not in PLATFORMS is never loaded at all."""
    from homeassistant.const import Platform

    from custom_components.ecovacs_mower import PLATFORMS

    assert Platform.SELECT in PLATFORMS


def test_the_modes_are_the_three_the_apps_write() -> None:
    """Raw value to option, as captured from the two RTK apps."""
    from custom_components.ecovacs_mower.select import EDGE_MODE_OPTIONS

    assert EDGE_MODE_OPTIONS == {
        0: "along_boundary",
        1: "edge_mowing",
        2: "cross_boundary",
    }


def test_the_select_is_a_config_entity_disabled_by_default() -> None:
    from homeassistant.const import EntityCategory

    from custom_components.ecovacs_mower.select import (
        EDGE_MODE_OPTIONS,
        EcovacsEdgeModeSelect,
    )

    description = EcovacsEdgeModeSelect.entity_description
    assert description.key == "edge_mode"
    assert description.entity_category is EntityCategory.CONFIG
    assert description.entity_registry_enabled_default is False
    assert _select(_device()).options == list(EDGE_MODE_OPTIONS.values())


def test_translations_and_icons_match_the_select_in_both_directions() -> None:
    from custom_components.ecovacs_mower.select import (
        EDGE_MODE_OPTIONS,
        EcovacsEdgeModeSelect,
    )

    key = EcovacsEdgeModeSelect.entity_description.translation_key
    strings = json.loads((ROOT / "strings.json").read_text(encoding="utf-8"))
    icons = json.loads((ROOT / "icons.json").read_text(encoding="utf-8"))

    assert set(strings["entity"]["select"]) == {key}
    assert set(icons["entity"]["select"]) == {key}
    assert set(strings["entity"]["select"][key]["state"]) == set(
        EDGE_MODE_OPTIONS.values()
    )


async def test_the_select_exists_only_for_a_class_with_the_flag() -> None:
    """The two RTK classes whose apps have the modes, and nobody else.

    Not the G1-800, which has the on/off switch, and not the A1600 LiDAR Pro,
    whose app shows no edge setting.
    """
    from custom_components.ecovacs_mower.select import async_setup_entry

    config_entry = MagicMock()
    config_entry.runtime_data.devices = [
        _mower(class_)
        for class_ in ("77atlz", "xmp9ds", "2px96q", "e4gqia", "2i0fns", "nope")
    ]
    added: list = []
    await async_setup_entry(MagicMock(), config_entry, added.extend)

    assert [e.unique_id for e in added] == [
        "did-xmp9ds_edge_mode",
        "did-2px96q_edge_mode",
    ]


async def test_a_vacuum_on_the_account_gets_no_select() -> None:
    from deebot_client.capabilities import DeviceType

    from custom_components.ecovacs_mower.select import async_setup_entry

    vacuum = _mower("xmp9ds")
    vacuum.capabilities.device_type = DeviceType.VACUUM
    config_entry = MagicMock()
    config_entry.runtime_data.devices = [vacuum]
    added: list = []
    await async_setup_entry(MagicMock(), config_entry, added.extend)

    assert added == []


def test_the_select_is_unknown_before_the_first_event() -> None:
    assert _select(_device()).current_option is None


@pytest.mark.parametrize(
    ("mode", "option"),
    [
        (0, "along_boundary"),
        (1, "edge_mowing"),
        (2, "cross_boundary"),
        # A mode nobody has named, and one the mower has not reported.
        (3, None),
        (None, None),
    ],
)
async def test_the_select_shows_the_reported_mode(mode, option) -> None:
    from custom_components.ecovacs_mower.deebot_patch.messages import (
        MowerBorderSwitchEvent,
    )

    entity, callback = await _select_callback(_device())
    await callback(MowerBorderSwitchEvent(enabled=True, mode=mode))

    assert entity.current_option == option


@pytest.mark.parametrize("enabled", [0, 1])
@pytest.mark.parametrize(
    ("option", "mode"),
    [("along_boundary", 0), ("edge_mowing", 1), ("cross_boundary", 2)],
)
async def test_choosing_a_mode_resends_the_reported_enable(
    option: str, mode: int, enabled: int
) -> None:
    """One write, both fields: enable is the mower's, never a fixed 1."""
    from custom_components.ecovacs_mower.deebot_patch.commands import (
        SetBorderSwitchMower,
    )

    device = _device()
    entity = _select(device)
    _report(device, {"enable": enabled, "mode": 1})

    await entity.async_select_option(option)

    (command,) = device.execute_command.call_args.args
    assert isinstance(command, SetBorderSwitchMower)
    assert command._args == {"enable": enabled, "mode": mode}


@pytest.mark.parametrize("reported", [None, {"mode": 1}])
async def test_the_select_refuses_to_write_without_a_reported_enable(
    reported,
) -> None:
    from homeassistant.exceptions import HomeAssistantError

    device = _device()
    entity = _select(device)
    if reported is not None:
        _report(device, reported)

    with pytest.raises(HomeAssistantError, match="edge setting"):
        await entity.async_select_option("edge_mowing")
    device.execute_command.assert_not_called()


async def test_the_select_reads_back_after_a_confirmed_write() -> None:
    """The read is what delivers the new mode where the mower pushes nothing."""
    from custom_components.ecovacs_mower.deebot_patch.messages import (
        MowerBorderSwitchEvent,
    )

    device = _device()
    entity = _select(device)
    _report(device, {"enable": 1, "mode": 0})

    await entity.async_select_option("cross_boundary")

    device.events.request_refresh.assert_called_once_with(MowerBorderSwitchEvent)


async def test_an_unconfirmed_mode_write_changes_nothing() -> None:
    from custom_components.ecovacs_mower.deebot_patch.messages import (
        MowerBorderSwitchEvent,
        border_switch_for,
    )

    device = _device()
    device.execute_command = AsyncMock(return_value={})
    entity, callback = await _select_callback(device)
    _report(device, {"enable": 1, "mode": 0})
    await callback(MowerBorderSwitchEvent(enabled=True, mode=0))

    await entity.async_select_option("cross_boundary")

    assert entity.current_option == "along_boundary"
    assert border_switch_for(device.events) == (True, 0)
    device.events.request_refresh.assert_not_called()
