"""The event bus's refreshes, held for a burst and sent as one ``getInfo`` (issue #85).

The bus here is the library's own, with capabilities that refresh each event
with the commands a G1-800 is refreshed with, and the device's sender is a
recorder: what these tests count is what the bus would hand the device.
``GetInfo`` itself, what it sends and how it reads the answer, is
``test_info.py``.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import Mock, patch

import pytest

from deebot_client.command import Command
from deebot_client.commands.json import GetBattery, GetBorderSwitch, GetError
from deebot_client.event_bus import EventBus
from deebot_client.events import (
    AvailabilityEvent,
    BatteryEvent,
    BorderSwitchEvent,
    ErrorEvent,
    LifeSpanEvent,
    StateEvent,
    StatsEvent,
)
from deebot_client.events.base import Event

from custom_components.ecovacs_mower.deebot_patch import (
    PatchContractError,
    batch_refreshes,
)
from custom_components.ecovacs_mower.deebot_patch.commands import (
    GetBorderSwitchMower,
    GetLifeSpanMower,
    GetStatsMower,
    MowerStateRefresh,
)
from custom_components.ecovacs_mower.deebot_patch.info import (
    GetInfo,
    RefreshBatch,
    _batchable,
)
from custom_components.ecovacs_mower.deebot_patch.messages import (
    MowerBorderSwitchEvent,
    MowerStatsEvent,
)

_QUIET = 0.02
_LONGEST = 0.2

_REFRESHES: dict[type[Event], list[Command]] = {
    AvailabilityEvent: [GetBattery()],
    BatteryEvent: [GetBattery()],
    ErrorEvent: [GetError()],
    StateEvent: [MowerStateRefresh()],
    StatsEvent: [GetStatsMower()],
    MowerStatsEvent: [GetStatsMower()],
    BorderSwitchEvent: [GetBorderSwitch()],
    MowerBorderSwitchEvent: [GetBorderSwitchMower()],
    LifeSpanEvent: [GetLifeSpanMower([])],
}


@pytest.fixture(autouse=True)
def _short_bursts():
    """A burst of hundredths of a second, so that the tests do not wait."""
    with (
        patch("custom_components.ecovacs_mower.deebot_patch.info._QUIET", _QUIET),
        patch("custom_components.ecovacs_mower.deebot_patch.info._LONGEST", _LONGEST),
    ):
        yield


class _Device:
    """The device's sender: records each command it is handed, and when."""

    def __init__(self) -> None:
        self.sent: list[Command] = []
        self.times: list[float] = []

    async def __call__(self, command: Command) -> dict[str, Any]:
        self.sent.append(command)
        self.times.append(asyncio.get_running_loop().time())
        return {"ret": "ok"}


def _bus(device: _Device) -> EventBus:
    bus = EventBus(
        device,
        Mock(get_refresh_commands=lambda event: _REFRESHES.get(event, [])),
    )
    batch_refreshes(bus)
    return bus


def _subscribe(bus: EventBus, *events: type[Event]) -> None:
    async def on_event(_event: Event) -> None:
        pass

    for event in events:
        bus.subscribe(event, on_event)


async def _burst_over() -> None:
    await asyncio.sleep(_LONGEST * 3)


def _names(command: Command) -> list[str]:
    assert isinstance(command, GetInfo)
    return [type(member).__name__ for member in command._commands]


async def test_a_burst_of_refreshes_goes_out_as_one_getinfo() -> None:
    device = _Device()
    bus = _bus(device)

    _subscribe(bus, BatteryEvent, ErrorEvent, StateEvent, StatsEvent)
    await asyncio.sleep(0)
    # Held while the burst lasts: nothing has gone out yet.
    assert device.sent == []
    await _burst_over()

    assert len(device.sent) == 1
    assert _names(device.sent[0]) == [
        "GetBattery",
        "GetError",
        "MowerStateRefresh",
        "GetStatsMower",
    ]


async def test_a_refresh_that_comes_alone_is_sent_as_it_is() -> None:
    device = _Device()
    bus = _bus(device)

    _subscribe(bus, ErrorEvent)
    await _burst_over()

    assert [type(command) for command in device.sent] == [GetError]


async def test_the_state_refresh_alone_is_sent_as_it_is() -> None:
    # Its own two requests in turn, as before: a burst of one sends nothing
    # it did not send before.
    device = _Device()
    bus = _bus(device)

    _subscribe(bus, StateEvent)
    await _burst_over()

    assert [type(command) for command in device.sent] == [MowerStateRefresh]


async def test_a_command_that_cannot_be_named_in_a_batch_is_not_held() -> None:
    device = _Device()
    bus = _bus(device)

    _subscribe(bus, LifeSpanEvent, ErrorEvent, BatteryEvent)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # The beacons' getLifeSpan goes out at once, while the burst still waits.
    assert [type(command) for command in device.sent] == [GetLifeSpanMower]
    await _burst_over()
    assert _names(device.sent[1]) == ["GetError", "GetBattery"]


async def test_one_command_refreshing_two_events_is_asked_for_once() -> None:
    device = _Device()
    bus = _bus(device)

    _subscribe(bus, StatsEvent, MowerStatsEvent, AvailabilityEvent, BatteryEvent)
    await _burst_over()

    assert _names(device.sent[0]) == ["GetStatsMower", "GetBattery"]


async def test_two_commands_with_one_name_are_both_kept() -> None:
    # Command.__eq__ compares the name and the arguments only, and these two
    # publish different events.
    device = _Device()
    bus = _bus(device)

    _subscribe(bus, BorderSwitchEvent, MowerBorderSwitchEvent)
    await _burst_over()

    assert _names(device.sent[0]) == ["GetBorderSwitch", "GetBorderSwitchMower"]


async def test_a_burst_that_keeps_growing_is_sent_after_the_longest_wait() -> None:
    device = _Device()
    bus = _bus(device)
    loop = asyncio.get_running_loop()

    # A refresh every half of the quiet time, for three times the longest
    # wait: the burst never goes quiet.
    events = [type(f"Trickle{index}", (Event,), {}) for index in range(60)]
    for index, event in enumerate(events):
        _REFRESHES[event] = [GetError()] if index % 2 else [GetBattery()]
    try:
        for event in events:
            _subscribe(bus, event)
            await asyncio.sleep(_QUIET / 2)
        last_arrived = loop.time()
        await _burst_over()
    finally:
        for event in events:
            _REFRESHES.pop(event)

    # Sent while refreshes were still arriving, not after the last of them.
    assert device.times[0] < last_arrived
    assert len(device.sent) > 1


async def test_a_burst_still_waiting_at_teardown_is_dropped() -> None:
    device = _Device()
    bus = _bus(device)

    _subscribe(bus, BatteryEvent, ErrorEvent)
    await asyncio.sleep(0)
    await bus.teardown()
    await _burst_over()

    assert device.sent == []
    # Nothing is left held for the next burst to inherit.
    assert bus._execute_command._held == []


async def test_the_next_refresh_after_a_burst_starts_a_new_one() -> None:
    device = _Device()
    bus = _bus(device)

    _subscribe(bus, BatteryEvent, ErrorEvent)
    await _burst_over()
    bus.request_refresh(ErrorEvent)
    await _burst_over()

    assert [type(command) for command in device.sent] == [GetInfo, GetError]


def test_installing_the_batch_twice_wraps_the_sender_once() -> None:
    device = _Device()
    bus = _bus(device)

    batch_refreshes(bus)

    assert isinstance(bus._execute_command, RefreshBatch)
    assert bus._execute_command._execute is device


def test_a_bus_without_the_sender_fails_fast() -> None:
    with pytest.raises(PatchContractError):
        batch_refreshes(object())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (GetBattery(), True),
        (GetStatsMower(), True),
        (GetBorderSwitchMower(), True),
        (MowerStateRefresh(), True),
        # Sends an explicit empty list, which a batch has no place for.
        (GetLifeSpanMower([]), False),
    ],
)
def test_only_a_getter_that_sends_its_bare_name_is_batched(
    command: Command, expected: bool
) -> None:
    assert _batchable(command) is expected


def test_a_getter_with_arguments_or_an_action_is_not_batched() -> None:
    from deebot_client.commands.json import Charge, GetCutDirection, SetVolume

    from custom_components.ecovacs_mower.deebot_patch.commands import (
        GetCleanInfoMower,
        GetMapInfoMower,
    )

    assert _batchable(GetCleanInfoMower()) is True
    assert _batchable(GetCutDirection()) is True
    assert _batchable(GetMapInfoMower()) is False
    assert _batchable(SetVolume(5)) is False
    assert _batchable(Charge()) is False


async def test_what_a_g1_is_refreshed_with_is_batched_but_two() -> None:
    # The burst on a G1-800: every refresh command it has goes into a batch
    # except the beacons' getLifeSpan and the map's getMI. A new refresh
    # that cannot be batched should be a decision, not a surprise.
    from deebot_client.hardware import _DEVICES, get_static_device_info

    from custom_components.ecovacs_mower.deebot_patch import apply
    from custom_components.ecovacs_mower.deebot_patch.commands import (
        GetMapInfoMower,
    )
    from custom_components.ecovacs_mower.deebot_patch.hardware import (
        patch_device_info,
    )

    apply()
    _DEVICES.pop("77atlz", None)
    try:
        await patch_device_info("77atlz")
        capabilities = (await get_static_device_info("77atlz")).capabilities
    finally:
        _DEVICES.pop("77atlz", None)

    commands = [
        command for commands in capabilities._events.values() for command in commands
    ]
    assert {type(command) for command in commands if not _batchable(command)} == {
        GetLifeSpanMower,
        GetMapInfoMower,
    }


async def test_every_getter_a_g1_batches_has_a_measured_size() -> None:
    # The estimate cuts the burst where it is measured, so a getter added to
    # a G1-800's refreshes without a size would be counted at the default.
    # The non-V2 clean info is the one exception: this firmware leaves that
    # name out of an answer, so it adds nothing to one.
    from deebot_client.hardware import _DEVICES, get_static_device_info

    from custom_components.ecovacs_mower.deebot_patch import apply
    from custom_components.ecovacs_mower.deebot_patch.hardware import (
        patch_device_info,
    )
    from custom_components.ecovacs_mower.deebot_patch.info import (
        _REPLY_SIZES,
        _parts,
    )

    apply()
    _DEVICES.pop("77atlz", None)
    try:
        await patch_device_info("77atlz")
        capabilities = (await get_static_device_info("77atlz")).capabilities
    finally:
        _DEVICES.pop("77atlz", None)

    names = {
        part.NAME
        for commands in capabilities._events.values()
        for command in commands
        if _batchable(command)
        for part in _parts(command)
    }
    assert names - {"getCleanInfo"} <= set(_REPLY_SIZES)
