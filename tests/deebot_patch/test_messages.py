"""Tests for the message handlers the library lacks."""

from __future__ import annotations

import asyncio
from dataclasses import fields
from typing import Any
from unittest.mock import AsyncMock, Mock, call

import pytest
from deebot_client.event_bus import EventBus
from deebot_client.events import Position, PositionsEvent, StateEvent, StatsEvent
from deebot_client.message import HandlingState
from deebot_client.messages.json import MESSAGES
from deebot_client.messages.json.stats import OnStats
from deebot_client.models import State
from deebot_client.rs.map import PositionType

from custom_components.ecovacs_mower.deebot_patch import apply
from custom_components.ecovacs_mower.deebot_patch.messages import (
    MowerProtectStateEvent,
    MowerStatsEvent,
    MowerTriggerEvent,
    OnChargeInfo,
    OnPos,
    OnProtectState,
    OnScheduleTaskInfo,
    OnStatsMower,
)


def _wrap(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "header": {
            "pri": 1,
            "tzm": 120,
            "ts": "1782211283816888165",
            "ver": "0.0.1",
            "fwVer": "1.9.16",
            "hwVer": "0.1.1",
        },
        "body": {"data": data},
    }


def _notified[EventT](
    message, data: dict[str, Any], event_type: type[EventT]
) -> list[EventT]:
    """Run the handler and return the events of that type it notified.

    Filtering by type is not cosmetic: ``Message.handle`` also notifies a
    ``FirmwareEvent`` parsed from the payload header, for every message.
    """
    event_bus = Mock()
    message.handle(event_bus, _wrap(data))
    return [
        call.args[0]
        for call in event_bus.notify.call_args_list
        if isinstance(call.args[0], event_type)
    ]


def _notified_states(message, data: dict[str, Any]) -> list[State]:
    """Run the handler and return the states that were notified."""
    return [event.state for event in _notified(message, data, StateEvent)]


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("goCharging", State.RETURNING),
        ("idle", State.DOCKED),
    ],
)
def test_on_charge_info(state: str, expected: State) -> None:
    # GOAT reports its return to the dock via onChargeInfo: "goCharging" on the
    # way home, "idle" once the job is done and it is standing in the dock.
    data = {"cid": "122", "trigger": "app", "state": state, "other": "0"}
    assert _notified_states(OnChargeInfo, data) == [expected]


@pytest.mark.parametrize("state", ["clean", "unknownState", ""])
def test_on_charge_info_ignores_other_states(state: str) -> None:
    data = {"cid": "122", "trigger": "app", "state": state}
    assert _notified_states(OnChargeInfo, data) == []


def test_on_charge_info_alert_overrides_docked_state() -> None:
    # "idle" normally means DOCKED, but trigger="alert" is an error state and must
    # win even if state would otherwise read as a successful docking.
    data = {"cid": "122", "trigger": "alert", "state": "idle"}
    assert _notified_states(OnChargeInfo, data) == [State.ERROR]


def test_on_charge_info_alert_overrides_ignored_state() -> None:
    # The same check must also take effect for a state that is otherwise ignored.
    data = {"cid": "122", "trigger": "alert", "state": "unknownState"}
    assert _notified_states(OnChargeInfo, data) == [State.ERROR]


@pytest.mark.parametrize(
    ("state", "clean_state", "expected"),
    [
        ("clean", {"motionState": "working"}, State.CLEANING),
        ("clean", {"motionState": "pause"}, State.PAUSED),
        ("clean", {"motionState": "goCharging"}, State.RETURNING),
        ("goCharging", None, State.RETURNING),
        ("idle", None, State.IDLE),
    ],
)
def test_on_schedule_task_info(
    state: str, clean_state: dict[str, Any] | None, expected: State
) -> None:
    # Scheduled runs are reported via onScheduleTaskInfo with the same payload as
    # onCleanInfo.
    data: dict[str, Any] = {"trigger": "continue", "other": "0", "state": state}
    if clean_state is not None:
        data["cleanState"] = clean_state
    assert _notified_states(OnScheduleTaskInfo, data) == [expected]


def test_on_schedule_task_info_alert_maps_to_error() -> None:
    data = {"trigger": "alert", "state": "clean"}
    assert _notified_states(OnScheduleTaskInfo, data) == [State.ERROR]


@pytest.mark.parametrize("state", ["unknownState", ""])
def test_on_schedule_task_info_ignores_other_states(state: str) -> None:
    data = {"trigger": "continue", "state": state}
    assert _notified_states(OnScheduleTaskInfo, data) == []


def _notified_triggers(message, data: dict[str, Any]) -> list[str]:
    return [event.trigger for event in _notified(message, data, MowerTriggerEvent)]


@pytest.mark.parametrize("message", [OnChargeInfo, OnScheduleTaskInfo])
@pytest.mark.parametrize("trigger", ["rain", "workComplete", "app", "continue"])
def test_trigger_is_republished_verbatim(message, trigger: str) -> None:
    # The patch layer must not interpret the value: "rain" is the only one the
    # sensor cares about today, but a future consumer may care about another.
    data = {"cid": "122", "trigger": trigger, "state": "goCharging"}
    assert _notified_triggers(message, data) == [trigger]


@pytest.mark.parametrize("message", [OnChargeInfo, OnScheduleTaskInfo])
def test_trigger_is_republished_for_unmappable_states(message) -> None:
    # Why the mower stopped is worth having even when this layer cannot tell
    # what it is doing — the two are parsed independently.
    data = {"cid": "122", "trigger": "rain", "state": "unknownState"}
    assert _notified_triggers(message, data) == ["rain"]


@pytest.mark.parametrize("message", [OnChargeInfo, OnScheduleTaskInfo])
def test_no_trigger_event_without_a_trigger(message) -> None:
    assert _notified_triggers(message, {"state": "goCharging"}) == []


def test_rain_trigger_survives_the_real_interruption_sequence() -> None:
    # The exact order from the captured log: the schedule pauses, the mower
    # heads home, and a minute later the dock reports "workComplete" — the
    # device's own summary, which says nothing about rain.
    assert _notified_triggers(
        OnScheduleTaskInfo,
        {"trigger": "rain", "state": "clean", "cleanState": {"motionState": "pause"}},
    ) == ["rain"]
    assert _notified_triggers(
        OnChargeInfo, {"cid": "122", "trigger": "rain", "state": "goCharging"}
    ) == ["rain"]
    assert _notified_triggers(
        OnChargeInfo, {"cid": "122", "trigger": "workComplete", "state": "idle"}
    ) == ["workComplete"]


async def test_repeated_rain_triggers_are_not_deduped_on_a_real_bus() -> None:
    # A resume that follows a rain stop (onCleanInfo, owned by the library)
    # publishes no trigger, so the event bus's "last_event" for
    # MowerTriggerEvent can still be "rain" when a *second*, genuine rain stop
    # happens. `Mock()` in the other tests can't catch that — only a real
    # `EventBus`, which suppresses a notification equal to the previous one of
    # the same type, exercises the bug this event's `_seq` field closes.
    bus = EventBus(AsyncMock(), Mock(get_refresh_commands=lambda _event: []))
    received: list[str] = []

    async def on_trigger(event: MowerTriggerEvent) -> None:
        received.append(event.trigger)

    bus.subscribe(MowerTriggerEvent, on_trigger)
    bus.notify(MowerTriggerEvent("rain"))
    bus.notify(MowerTriggerEvent("rain"))
    await asyncio.sleep(0)

    assert received == ["rain", "rain"]


# The payload as the device actually sends it, captured while a scheduled run
# was cut short by rain. isPinCode and isPrepareDataSuccess are part of it and
# must be ignored, not choke the parsing.
_PROTECT_PAYLOAD = {
    "isAnimProtect": 0,
    "isRainProtect": 1,
    "isRainDelay": 0,
    "isEStop": 0,
    "isLocked": 0,
    "isPinCode": 0,
    "isPrepareDataSuccess": 1,
}


# The getProtectState answer that settled what isRainProtect means: firmware
# 1.13.10, dry day, mower parked under cover, with rain protection AND animal
# protection both switched on in the app. Both flags read 0 anyway, which is
# what rules out the "this protection is enabled" reading for either of them.
# Pinned here so the evidence the moisture device class rests on lives in the
# repo, not only in a docstring.
_PROTECT_PAYLOAD_DRY = {
    "isAnimProtect": 0,
    "isRainProtect": 0,
    "isRainDelay": 0,
    "isEStop": 0,
    "isLocked": 0,
    "isPinCode": 0,
    "isPrepareDataSuccess": 1,
}


def test_on_protect_state_dry_day_sample_reads_every_flag_false() -> None:
    assert _notified(OnProtectState, _PROTECT_PAYLOAD_DRY, MowerProtectStateEvent) == [
        MowerProtectStateEvent(
            rain_protect=False,
            rain_delay=False,
            emergency_stop=False,
            locked=False,
            animal_protect=False,
        )
    ]


def test_on_protect_state() -> None:
    assert _notified(OnProtectState, _PROTECT_PAYLOAD, MowerProtectStateEvent) == [
        MowerProtectStateEvent(
            rain_protect=True,
            rain_delay=False,
            emergency_stop=False,
            locked=False,
            animal_protect=False,
        )
    ]


def test_on_protect_state_flags_are_booleans() -> None:
    # The wire format is 0/1. An int would compare unequal to the previous
    # event's bool on the event bus and notify subscribers for nothing.
    (event,) = _notified(OnProtectState, _PROTECT_PAYLOAD, MowerProtectStateEvent)
    assert all(
        isinstance(value, bool)
        for value in (
            event.rain_protect,
            event.rain_delay,
            event.emergency_stop,
            event.locked,
            event.animal_protect,
        )
    )


def test_on_protect_state_maps_every_flag() -> None:
    payload = dict.fromkeys(_PROTECT_PAYLOAD, 1)
    assert _notified(OnProtectState, payload, MowerProtectStateEvent) == [
        MowerProtectStateEvent(
            rain_protect=True,
            rain_delay=True,
            emergency_stop=True,
            locked=True,
            animal_protect=True,
        )
    ]


@pytest.mark.parametrize(("key", "field_name"), OnProtectState._FLAGS.items())
def test_on_protect_state_maps_each_wire_key_to_its_own_field(
    key: str, field_name: str
) -> None:
    # test_on_protect_state_maps_every_flag sets all five flags at once, which
    # can't catch two of them swapped (e.g. isEStop and isLocked mixed up) —
    # only a one-hot payload per flag can.
    payload = {**dict.fromkeys(_PROTECT_PAYLOAD, 0), key: 1}
    (event,) = _notified(OnProtectState, payload, MowerProtectStateEvent)
    assert {f.name for f in fields(event) if getattr(event, f.name)} == {field_name}


@pytest.mark.parametrize("missing", list(OnProtectState._FLAGS))
def test_on_protect_state_drops_partial_payloads(missing: str) -> None:
    # Defaulting a missing flag to False would report "not raining" or "no
    # emergency stop" from a message that never said so. Keeping the previous
    # value is the safer failure.
    payload = {k: v for k, v in _PROTECT_PAYLOAD.items() if k != missing}
    assert _notified(OnProtectState, payload, MowerProtectStateEvent) == []


_CHARGER_UNKNOWN = [{"x": 0, "y": 0, "a": 0, "t": 1, "invalid": 1}]


def _positions(data: dict[str, Any]) -> list[Position]:
    """Run OnPos and return every position it published."""
    return [
        position
        for event in _notified(OnPos, data, PositionsEvent)
        for position in event.positions
    ]


@pytest.mark.parametrize("invalid", [0, 2])
def test_on_pos_keeps_localized_and_dead_reckoned_samples(invalid: int) -> None:
    # The reason this handler exists. Firmware 1.13.10 flags roughly nine of
    # ten samples "invalid": 2 during a run (102 of 115 in a six-minute
    # capture), and they interpolate the same smooth 2 Hz path as the
    # "invalid": 0 ones, 5-15 cm apart at 0.16 m/s. Upstream's GetPos keeps
    # only 0, which decimates the track to 11% of its samples.
    data = {
        "deebotPos": {"x": -31025, "y": 3525, "a": -88, "invalid": invalid},
        "chargePos": _CHARGER_UNKNOWN,
        "mid": "0",
    }
    assert _positions(data) == [
        Position(type=PositionType.DEEBOT, x=-31025, y=3525, a=-88)
    ]


def test_on_pos_drops_a_sample_the_device_calls_invalid() -> None:
    # Bit 0 is the invalid flag: 1 means the device has no position to report,
    # which is what chargePos carries on every sample from this hardware.
    data = {
        "deebotPos": {"x": 0, "y": 0, "a": 0, "invalid": 1},
        "chargePos": _CHARGER_UNKNOWN,
        "mid": "0",
    }
    assert _notified(OnPos, data, PositionsEvent) == []


def test_on_pos_handles_a_payload_with_nothing_to_publish() -> None:
    # A docked mower sends exactly this. Upstream answers analyse(), which
    # logs "Could not handle onPos" for a message we parsed fine; the layer
    # keeps analyse() for payloads that would not parse (see OnMI).
    data = {
        "deebotPos": {"x": 0, "y": 0, "a": 0, "invalid": 1},
        "chargePos": _CHARGER_UNKNOWN,
        "mid": "0",
    }
    result = OnPos._handle_body_data_dict(Mock(), data)
    assert result.state is HandlingState.SUCCESS


def test_on_pos_reports_a_valid_charger_position() -> None:
    # Never observed on the verified hardware — chargePos is always flagged 1
    # there, which is why map.py assumes the dock sits at the origin. Handled
    # anyway, and in upstream's order: the mower first, the dock second.
    data = {
        "deebotPos": {"x": -808, "y": -62, "a": -4, "invalid": 2},
        "chargePos": [{"x": 0, "y": 0, "a": 0, "t": 1, "invalid": 0}],
        "mid": "0",
    }
    assert _positions(data) == [
        Position(type=PositionType.DEEBOT, x=-808, y=-62, a=-4),
        Position(type=PositionType.CHARGER, x=0, y=0, a=0),
    ]


def test_on_pos_accepts_a_single_charger_position_as_a_dict() -> None:
    # chargePos arrives as a list on the verified hardware, but upstream reads
    # either shape and a handler that replaces it must not be stricter.
    data = {
        "deebotPos": {"x": -808, "y": -62, "a": -4, "invalid": 0},
        "chargePos": {"x": 10, "y": 20, "a": 0, "invalid": 0},
        "mid": "0",
    }
    assert Position(type=PositionType.CHARGER, x=10, y=20, a=0) in _positions(data)


def test_message_names() -> None:
    # The names are the keys in the library's registry and must match exactly.
    assert OnChargeInfo.NAME == "onChargeInfo"
    assert OnPos.NAME == "onPos"
    assert OnProtectState.NAME == "onProtectState"
    assert OnScheduleTaskInfo.NAME == "onScheduleTaskInfo"


# Verbatim from the log attached to issue #56, a GOAT O800 RTK (2px96q) on
# firmware 1.17.11 mid-job. The push carries the same three numbers getStats
# answers with, and no mowid — that field is a 77atlz/1.36.208 extra and cannot
# be relied on as a job identity.
_PUSHED = {"time": 977, "area": 208900, "mowedArea": 105925}


def test_on_stats_mower_listens_on_the_librarys_own_message_name() -> None:
    # Replacing the parsing of an existing push, not registering a second one.
    assert OnStatsMower.NAME == OnStats.NAME == "onStats"


def test_on_stats_mower_publishes_the_mowed_area_the_library_drops() -> None:
    # Issue #55. onStats arrives at about 2 Hz on the classes that send it at
    # all, carrying the one number that moves during a job — while upstream's
    # OnStats notifies StatsEvent only, so the progress entity never saw it.
    event_bus = Mock()
    OnStatsMower._handle_body_data_dict(event_bus, _PUSHED)
    assert (
        call(MowerStatsEvent(area=208900, mowed_area=105925))
        in event_bus.notify.call_args_list
    )


def test_on_stats_mower_still_notifies_the_librarys_own_event() -> None:
    # The area and time sensors subscribe to StatsEvent and must not notice
    # that the handler behind the push was swapped.
    event_bus = Mock()
    OnStatsMower._handle_body_data_dict(event_bus, _PUSHED)
    assert (
        call(StatsEvent(area=208900, time=977, type=None))
        in event_bus.notify.call_args_list
    )


def test_on_stats_mower_reports_a_missing_field_as_none() -> None:
    # Same contract as GetStatsMower: absent mowedArea leaves the progress
    # entity unknown rather than claiming the job has not started.
    event_bus = Mock()
    OnStatsMower._handle_body_data_dict(event_bus, {"area": 208900, "time": 977})
    assert (
        call(MowerStatsEvent(area=208900, mowed_area=None))
        in event_bus.notify.call_args_list
    )


def test_on_stats_mower_survives_upstream_raising_on_a_missing_field() -> None:
    # Upstream's OnStats indexes data["area"]/data["time"] directly and raises
    # KeyError when a push omits either. The mower event must not be lost with
    # it, so notify_mower_stats has to run before super() gets the chance.
    event_bus = Mock()
    with pytest.raises(KeyError):
        OnStatsMower._handle_body_data_dict(event_bus, {"time": 977, "mowedArea": 5})
    assert (
        call(MowerStatsEvent(area=None, mowed_area=5))
        in event_bus.notify.call_args_list
    )


def test_on_stats_mower_is_registered_by_apply() -> None:
    # Without this the push resolves to the library's OnStats and the mowed
    # area is dropped on the floor, which is the whole of issue #55.
    apply()
    assert MESSAGES["onStats"] is OnStatsMower
