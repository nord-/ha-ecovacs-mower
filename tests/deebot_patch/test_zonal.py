"""Tests for the GOAT zone-mowing command."""

from unittest.mock import AsyncMock, patch

import pytest
from deebot_client.command import Command
from deebot_client.commands.json.clean import Clean, CleanV2
from deebot_client.hardware import _DEVICES, get_static_device_info
from deebot_client.models import CleanMode

from custom_components.ecovacs_mower.deebot_patch.hardware import patch_device_info
from custom_components.ecovacs_mower.deebot_patch.zonal import (
    MowArea,
    _ZoneCleanNonV2,
    _ZoneCleanV2,
)
from custom_components.ecovacs_mower.deebot_patch.families import Family, selected

from .test_commands import _DEVICE_INFO, _OK, _NO_ANSWER, _transport


@pytest.fixture(autouse=True)
def _clear_cache():
    """Empty the library's cache between tests."""
    for class_ in ("e4gqia",):
        _DEVICES.pop(class_, None)
    yield
    for class_ in ("e4gqia",):
        _DEVICES.pop(class_, None)


def test_zone_commands_are_clean_commands() -> None:
    assert issubclass(_ZoneCleanNonV2, Clean)
    assert issubclass(_ZoneCleanV2, CleanV2)


def test_spot_area_payload_uses_saved_area_ids() -> None:
    command = _ZoneCleanNonV2([1, 2, 3])
    assert command.NAME == "clean"
    assert command._args == {
        "act": "start",
        "content": {"type": "spotArea", "value": "1,2,3"},
    }


def test_v2_spot_area_payload_has_the_same_nested_shape() -> None:
    command = _ZoneCleanV2([7])
    assert command.NAME == "clean_V2"
    assert command._args == {
        "act": "start",
        "content": {"type": "spotArea", "value": "7"},
    }


def test_mow_area_requires_spot_area_mode() -> None:
    with pytest.raises(ValueError, match="Unsupported mower area mode"):
        MowArea(CleanMode.AUTO, [1])


def test_mow_area_requires_an_area() -> None:
    with pytest.raises(ValueError, match="At least one area ID"):
        MowArea(CleanMode.SPOT_AREA, [])


def test_mow_area_rejects_multiple_cleanings() -> None:
    with pytest.raises(ValueError, match="exactly one cleaning pass"):
        MowArea(CleanMode.SPOT_AREA, [1], 2)


def test_mow_area_equality_includes_area_ids() -> None:
    assert MowArea(CleanMode.SPOT_AREA, [1, 3]) == MowArea(
        CleanMode.SPOT_AREA, [1, 3]
    )
    assert MowArea(CleanMode.SPOT_AREA, [1, 3]) != MowArea(
        CleanMode.SPOT_AREA, [1, 2]
    )


async def test_patch_exposes_the_area_command() -> None:
    await patch_device_info("e4gqia")
    info = await get_static_device_info("e4gqia")
    assert info.capabilities.clean.action.area is MowArea


async def test_mow_area_executes_first_on_non_v2() -> None:
    fake, sent = _transport(_OK)
    command = MowArea(CleanMode.SPOT_AREA, [1, 3])

    with patch.object(Command, "_execute", fake):
        await command._execute(AsyncMock(), _DEVICE_INFO, AsyncMock())

    assert sent == ["clean"]
    assert command._delegate(Family.NON_V2)._args["content"]["value"] == "1,3"
    assert selected(_DEVICE_INFO["did"]) is Family.NON_V2


async def test_mow_area_falls_back_to_v2_and_commits_family() -> None:
    fake, sent = _transport(_NO_ANSWER, _OK)
    command = MowArea(CleanMode.SPOT_AREA, [1, 3])

    with patch.object(Command, "_execute", fake):
        await command._execute(AsyncMock(), _DEVICE_INFO, AsyncMock())

    assert sent == ["clean", "clean_V2"]
    assert command._delegate(Family.V2)._args["content"]["value"] == "1,3"
    assert selected(_DEVICE_INFO["did"]) is Family.V2


def test_mow_area_keeps_clean_contract() -> None:
    assert issubclass(MowArea, Clean)
    assert MowArea.NAME == "clean"
