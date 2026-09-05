"""The state map and the control commands.

The module under test imports Home Assistant, which cannot be imported on Windows
(``fcntl``). The imports therefore live inside the test functions and the whole
file is marked ``requires_ha`` — otherwise collection itself crashes before any
skip marker gets a chance to apply. The source of truth is CI on ubuntu-latest.
"""

import pytest

from . import requires_ha

pytestmark = requires_ha


@pytest.mark.parametrize(
    ("state_name", "expected_name"),
    [
        ("CLEANING", "MOWING"),
        ("PAUSED", "PAUSED"),
        ("RETURNING", "RETURNING"),
        ("DOCKED", "DOCKED"),
        ("ERROR", "ERROR"),
    ],
)
def test_state_mapping(state_name: str, expected_name: str) -> None:
    from deebot_client.models import State
    from homeassistant.components.lawn_mower import LawnMowerActivity

    from custom_components.ecovacs_mower.lawn_mower import _STATE_TO_MOWER_STATE

    state = getattr(State, state_name)
    expected = getattr(LawnMowerActivity, expected_name)
    assert _STATE_TO_MOWER_STATE[state] == expected


def test_idle_maps_to_paused_not_docked() -> None:
    # A mower standing still in the middle of the lawn is paused, not docked.
    from deebot_client.models import State
    from homeassistant.components.lawn_mower import LawnMowerActivity

    from custom_components.ecovacs_mower.lawn_mower import _STATE_TO_MOWER_STATE

    assert _STATE_TO_MOWER_STATE[State.IDLE] == LawnMowerActivity.PAUSED


def test_every_state_is_mapped() -> None:
    # An unhandled state raises KeyError in the callback and silently breaks the
    # entity.
    from deebot_client.models import State

    from custom_components.ecovacs_mower.lawn_mower import _STATE_TO_MOWER_STATE

    assert set(_STATE_TO_MOWER_STATE) == set(State)


def test_supported_features() -> None:
    # LawnMowerEntity uses HA's CachedProperties metaclass for
    # "supported_features", which rewrites the class attribute
    # ``_attr_supported_features`` into a property. Read on the class (without an
    # instance) that yields the property object itself, not the flag value — which
    # is why it is read via an instance, exactly as HA does at runtime.
    # ``__new__`` bypasses ``__init__`` (which requires a real ``Device``) since
    # the descriptor does not depend on it having run.
    from homeassistant.components.lawn_mower import LawnMowerEntityFeature

    from custom_components.ecovacs_mower.lawn_mower import EcovacsMower

    instance = EcovacsMower.__new__(EcovacsMower)
    assert instance._attr_supported_features == (
        LawnMowerEntityFeature.DOCK
        | LawnMowerEntityFeature.PAUSE
        | LawnMowerEntityFeature.START_MOWING
    )


async def test_one_tick_asks_for_the_state_and_the_stats() -> None:
    """The mowing progress rides this tick instead of running one of its own.

    It wants the same answer on the same trigger at the same interval, and one
    getStats notifies both StatsEvent and MowerStatsEvent, so a second timer
    would buy nothing but drift. StatsEvent, not MowerStatsEvent, is what gets
    asked for directly: Device.__init__ always subscribes to it, so the stats
    round trip does not depend on the progress sensor being enabled.
    """
    from unittest.mock import MagicMock

    from deebot_client.events import StateEvent, StatsEvent

    from custom_components.ecovacs_mower.lawn_mower import EcovacsMower

    device = MagicMock()
    device.device_info = {"did": "did"}
    mower = EcovacsMower(device, MagicMock())
    mower._subscribed_events = {StateEvent}

    await mower.async_update()

    asked = {call.args[0] for call in device.events.request_refresh.call_args_list}
    assert asked == {StateEvent, StatsEvent}


async def test_a_dropped_leaving_push_is_still_bounded_by_the_poll() -> None:
    """Starting from HA restarts the controller's tick, not just a pushed StateEvent.

    Without this, a start command whose onCleanInfo/onChargeInfo push never
    arrives (firmware 1.13.x's documented failure mode) leaves the controller's
    poll stopped and nothing polls until the next availability flap. The tick
    itself lives in EcovacsController, not here — see test_controller.py — so
    this only checks that the entity asks the controller to (re)start it.
    """
    from unittest.mock import AsyncMock, MagicMock

    from deebot_client.models import CleanAction

    from custom_components.ecovacs_mower.lawn_mower import EcovacsMower

    device = MagicMock()
    device.device_info = {"did": "did"}
    controller = MagicMock()
    mower = EcovacsMower(device, controller)
    mower._execute_command = AsyncMock()

    await mower._clean_command(CleanAction.START)
    controller.start_polling.assert_called_once_with(device)

    # Pausing is not a leaving-the-dock command; it must not poke the
    # controller's poll at all.
    controller.start_polling.reset_mock()
    await mower._clean_command(CleanAction.PAUSE)
    controller.start_polling.assert_not_called()


async def test_mow_area_dispatches_through_entity_command() -> None:
    """A supported mower builds MowArea and uses the entity command wrapper."""
    from unittest.mock import AsyncMock, MagicMock

    from deebot_client.models import CleanMode

    from custom_components.ecovacs_mower.deebot_patch.zonal import MowArea
    from custom_components.ecovacs_mower.lawn_mower import EcovacsMower

    execute = AsyncMock()
    controller = MagicMock()
    device = MagicMock()
    device.capabilities.clean.action.area = MowArea
    mower = EcovacsMower(device, controller)
    mower._execute_command = execute

    await mower.async_mow_area([1, 3])

    controller.start_polling.assert_called_once_with(device)
    execute.assert_awaited_once()
    command = execute.await_args.args[0]
    assert command == MowArea(CleanMode.SPOT_AREA, [1, 3])


async def test_mow_area_rejects_mowers_without_spot_area() -> None:
    """A mower with another area command is not advertised as zone capable."""
    from unittest.mock import AsyncMock, MagicMock

    from homeassistant.exceptions import HomeAssistantError

    from custom_components.ecovacs_mower.lawn_mower import EcovacsMower

    device = MagicMock()
    mower = EcovacsMower(device, MagicMock())
    mower._execute_command = AsyncMock()

    with pytest.raises(HomeAssistantError, match="does not support zone mowing"):
        await mower.async_mow_area([1])

    device.capabilities.clean.action.area = None
    with pytest.raises(HomeAssistantError, match="does not support zone mowing"):
        await mower.async_mow_area([1])