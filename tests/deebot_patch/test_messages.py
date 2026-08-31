"""Tests for the message handlers the library lacks."""

from __future__ import annotations

import asyncio
from dataclasses import fields
from typing import Any
from unittest.mock import AsyncMock, Mock, call, patch

import pytest
from deebot_client.commands.json.clean import GetCleanInfo
from deebot_client.event_bus import EventBus
from deebot_client.events import Position, PositionsEvent, StateEvent, StatsEvent
from deebot_client.events.base import Event
from deebot_client.message import HandlingResult, HandlingState
from deebot_client.messages import get_message
from deebot_client.messages.json import MESSAGES
from deebot_client.messages.json.stats import OnStats
from deebot_client.models import State, StaticDeviceInfo
from deebot_client.rs.map import PositionType

from custom_components.ecovacs_mower.deebot_patch import apply
from custom_components.ecovacs_mower.deebot_patch.commands import GetChargeStateMower
from custom_components.ecovacs_mower.deebot_patch.messages import (
    MowerBeaconsEvent,
    MowerJobEdgeEvent,
    MowerProtectStateEvent,
    MowerRainDelayEvent,
    MowerStatsEvent,
    MowerTriggerEvent,
    OnChargeInfo,
    OnChargeState,
    OnCleanInfo,
    OnMowBorderStart,
    OnMowBorderStop,
    OnMowScheduleStart,
    OnMowScheduleStop,
    OnMowSpotAreaStart,
    OnMowSpotAreaStop,
    OnPos,
    OnProtectState,
    OnRainDelay,
    OnScheduleTaskInfo,
    OnStatsMower,
    OnUwb,
    handle_clean_info,
    notify_mower_beacons,
)
from custom_components.ecovacs_mower.deebot_patch.state_precedence import register


def _bus() -> EventBus:
    return EventBus(AsyncMock(), Mock(get_refresh_commands=lambda _event: []))


def _collect[EventT: Event](bus: EventBus, event_type: type[EventT]) -> list[EventT]:
    """Subscribe to *event_type* and return the growing list of what arrives."""
    received: list[EventT] = []

    async def on_event(event: EventT) -> None:
        received.append(event)

    bus.subscribe(event_type, on_event)
    return received


def _static_device_info() -> StaticDeviceInfo:
    """The minimum get_message() reads: a data type and a map capability flag."""
    from deebot_client.const import DataType

    return StaticDeviceInfo(DataType.JSON, Mock(map=None))


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


def test_on_pos_clears_docked_when_far_from_the_charger() -> None:
    # Issue #67's gate has no recovery path if the departure push itself is
    # the one firmware 1.13.10 drops. A deebotPos sample clearly away from the
    # dock-at-origin is evidence enough to stop withholding PAUSED/IDLE, even
    # with no state push having arrived at all.
    bus = Mock()
    record = register(bus)
    record.dock()
    record.suppressed = State.PAUSED

    data = {
        "deebotPos": {"x": 5000, "y": 0, "a": 0, "invalid": 0},
        "chargePos": _CHARGER_UNKNOWN,
        "mid": "0",
    }
    OnPos._handle_body_data_dict(bus, data)

    assert record.docked is False
    assert record.suppressed is None


def test_on_pos_leaves_docked_alone_for_a_sample_near_the_charger() -> None:
    bus = Mock()
    record = register(bus)
    record.dock()

    data = {
        "deebotPos": {"x": 10, "y": -10, "a": 0, "invalid": 0},
        "chargePos": _CHARGER_UNKNOWN,
        "mid": "0",
    }
    OnPos._handle_body_data_dict(bus, data)

    assert record.docked is True


def test_on_pos_measures_from_a_reported_charger_position() -> None:
    # Never observed on the verified hardware (chargePos there is always
    # flagged 1, hence the (0, 0) fallback), but a firmware that does report a
    # valid, non-origin chargePos must not have a deebotPos next to it read as
    # "clearly away" just because it is far from the unrelated origin.
    bus = Mock()
    record = register(bus)
    record.dock()

    data = {
        "deebotPos": {"x": 5010, "y": 0, "a": 0, "invalid": 0},
        "chargePos": [{"x": 5000, "y": 0, "a": 0, "t": 1, "invalid": 0}],
        "mid": "0",
    }
    OnPos._handle_body_data_dict(bus, data)

    assert record.docked is True


def test_on_pos_still_clears_docked_far_from_a_valid_charger_position() -> None:
    bus = Mock()
    record = register(bus)
    record.dock()
    record.suppressed = State.PAUSED

    data = {
        "deebotPos": {"x": 10000, "y": 0, "a": 0, "invalid": 0},
        "chargePos": [{"x": 5000, "y": 0, "a": 0, "t": 1, "invalid": 0}],
        "mid": "0",
    }
    OnPos._handle_body_data_dict(bus, data)

    assert record.docked is False
    assert record.suppressed is None


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


# Verbatim from the log attached to issue #54, a GOAT G1-800 (77atlz) on
# firmware 1.36.208. The push arrived the moment the rain sensor was switched
# back on in the app, and the app's own setting read three hours — which is
# what pins the unit of ``delay`` to minutes.
_RAIN_DELAY = {"enable": 1, "delay": 180}


def test_on_rain_delay_notifies_the_setting_and_its_delay() -> None:
    assert _notified(OnRainDelay, _RAIN_DELAY, MowerRainDelayEvent) == [
        MowerRainDelayEvent(enabled=True, delay=180)
    ]


def test_on_rain_delay_reports_the_sensor_switched_off() -> None:
    # The delay survives the sensor being switched off: the device keeps the
    # configured hold, and the number entity must keep showing it.
    assert _notified(OnRainDelay, {"enable": 0, "delay": 180}, MowerRainDelayEvent) == [
        MowerRainDelayEvent(enabled=False, delay=180)
    ]


def test_on_rain_delay_without_a_delay_leaves_it_unknown() -> None:
    # None rather than 0, for the same reason notify_mower_stats does it: a
    # firmware that does not report the field should leave the entity unknown,
    # not claim the mower resumes the instant the rain stops.
    assert _notified(OnRainDelay, {"enable": 1}, MowerRainDelayEvent) == [
        MowerRainDelayEvent(enabled=True, delay=None)
    ]


def test_on_rain_delay_ignores_a_delay_that_is_not_a_number() -> None:
    # The enable half is still worth publishing; only the delay is lost.
    assert _notified(
        OnRainDelay, {"enable": 1, "delay": "180"}, MowerRainDelayEvent
    ) == [MowerRainDelayEvent(enabled=True, delay=None)]


def test_on_rain_delay_rejects_a_non_0_1_enable() -> None:
    # A bare bool(enable) would read "0" as truthy, and that reading is what
    # the next setRainDelay writes back — dropping the whole payload is the
    # same refusal a missing enable field gets.
    assert _notified(
        OnRainDelay, {"enable": "0", "delay": 180}, MowerRainDelayEvent
    ) == []


def test_on_rain_delay_without_enable_is_not_handled() -> None:
    # Same rule as onProtectState's missing flags: defaulting to False would
    # claim the rain sensor is switched off on the strength of a payload that
    # never said so, and the switch would send that back on the next
    # setRainDelay.
    assert _notified(OnRainDelay, {"delay": 180}, MowerRainDelayEvent) == []


def test_on_rain_delay_without_enable_asks_for_analysis() -> None:
    # The handler directly, not through Message.handle: that wrapper turns an
    # ANALYSE into ANALYSE_LOGGED once it has logged the payload, so going
    # through it would assert on the library's bookkeeping rather than on what
    # this handler decided.
    result = OnRainDelay._handle_body_data_dict(Mock(), {"delay": 180})
    assert result.state is HandlingState.ANALYSE


def test_on_rain_delay_message_name() -> None:
    assert OnRainDelay.NAME == "onRainDelay"


def test_on_rain_delay_is_registered_by_apply() -> None:
    # The library has no handler at all, so without this the push is logged as
    # 'Unknown message "onRainDelay"' and both entities stay unknown until
    # something else asks (issue #54).
    apply()
    assert MESSAGES["onRainDelay"] is OnRainDelay
async def test_a_paused_plan_is_suppressed_while_docked() -> None:
    # Issue #67: charging and plan-paused are both true, and the entity must
    # read docked. 133.37 of 137.51 m2 done, plan paused, mower on the charger.
    bus = _bus()
    record = register(bus)
    record.dock()
    published = _collect(bus, StateEvent)

    handle_clean_info(
        bus,
        {"trigger": "none", "state": "clean", "cleanState": {"motionState": "pause"}},
    )
    await asyncio.sleep(0)

    assert published == []
    # Kept, so the mow command can still tell resume from start.
    assert record.suppressed is State.PAUSED


async def test_the_same_payload_publishes_when_not_docked() -> None:
    # A pause halfway across a lawn is a real paused: manual, or rain.
    bus = _bus()
    register(bus)
    published = _collect(bus, StateEvent)

    handle_clean_info(
        bus,
        {"trigger": "none", "state": "clean", "cleanState": {"motionState": "pause"}},
    )
    await asyncio.sleep(0)

    assert [event.state for event in published] == [State.PAUSED]


async def test_working_passes_and_clears_the_dock() -> None:
    bus = _bus()
    record = register(bus)
    record.dock()
    record.suppressed = State.PAUSED
    published = _collect(bus, StateEvent)

    handle_clean_info(
        bus,
        {"trigger": "none", "state": "clean", "cleanState": {"motionState": "working"}},
    )
    await asyncio.sleep(0)

    assert [event.state for event in published] == [State.CLEANING]
    assert record.docked is False
    assert record.suppressed is None


async def test_a_repeated_working_push_clears_the_dock_both_times() -> None:
    # The captured telemetry has two CLEANING pushes sixteen seconds apart. The
    # bus drops the second StateEvent as equal to the first, which is why the
    # record is written by the handler and not by a subscription to the bus.
    bus = _bus()
    record = register(bus)
    payload = {
        "trigger": "none",
        "state": "clean",
        "cleanState": {"motionState": "working"},
    }

    handle_clean_info(bus, payload)
    record.dock()
    handle_clean_info(bus, payload)

    assert record.docked is False


async def test_go_charging_passes_and_clears_the_dock() -> None:
    bus = _bus()
    record = register(bus)
    record.dock()
    published = _collect(bus, StateEvent)

    handle_clean_info(bus, {"trigger": "none", "state": "goCharging"})
    await asyncio.sleep(0)

    assert [event.state for event in published] == [State.RETURNING]
    assert record.docked is False


async def test_an_alert_is_never_suppressed() -> None:
    # An error on the dock is real, and the fault latch (#53) must see it.
    bus = _bus()
    register(bus).dock()
    published = _collect(bus, StateEvent)

    handle_clean_info(
        bus,
        {"trigger": "alert", "state": "clean", "cleanState": {"motionState": "pause"}},
    )
    await asyncio.sleep(0)

    assert [event.state for event in published] == [State.ERROR]


async def test_an_unregistered_bus_is_never_gated() -> None:
    # A Deebot vacuum on the same account: no record, so nothing is withheld.
    bus = _bus()
    published = _collect(bus, StateEvent)

    handle_clean_info(
        bus,
        {"trigger": "none", "state": "clean", "cleanState": {"motionState": "pause"}},
    )
    await asyncio.sleep(0)

    assert [event.state for event in published] == [State.PAUSED]


async def test_a_suppressed_idle_replaces_an_earlier_suppressed_pause() -> None:
    # Last suppressed value wins: a plan that finished on the dock must not
    # leave a PAUSED behind for the mow command to resume against.
    bus = _bus()
    record = register(bus)
    record.dock()
    record.suppressed = State.PAUSED

    handle_clean_info(bus, {"trigger": "none", "state": "idle"})

    assert record.suppressed is State.IDLE


async def test_on_clean_info_is_reached_for_both_names() -> None:
    # get_message applies removesuffix("_V2") before the legacy fallback, so
    # one registration catches onCleanInfo and onCleanInfo_V2 both. The 1.36
    # firmware pushes the second spelling.
    apply()
    static = _static_device_info()

    assert get_message("onCleanInfo", static) is OnCleanInfo
    assert get_message("onCleanInfo_V2", static) is OnCleanInfo


async def test_a_vacuums_clean_info_goes_to_the_library_handler() -> None:
    # MESSAGES is global, so this handler is reached for every JSON device.
    # An unregistered bus must come out exactly where it would have without us:
    # handle_clean_info does not carry the library's customArea branch, so
    # "not a superset" is a real difference, not a formality.
    bus = _bus()
    published = _collect(bus, StateEvent)
    payload = {
        "state": "clean",
        "cleanState": {
            "motionState": "working",
            "content": {"type": "customArea", "value": "1,2,3,4"},
        },
    }

    with patch.object(
        GetCleanInfo, "_handle_body_data_dict", return_value=HandlingResult.success()
    ) as library_handler:
        OnCleanInfo._handle_body_data_dict(bus, payload)

    library_handler.assert_called_once_with(bus, payload)
    assert published == []


async def test_a_registered_mowers_clean_info_is_gated() -> None:
    bus = _bus()
    register(bus).dock()
    published = _collect(bus, StateEvent)

    OnCleanInfo._handle_body_data_dict(
        bus,
        {"trigger": "none", "state": "clean", "cleanState": {"motionState": "pause"}},
    )
    await asyncio.sleep(0)

    assert published == []


@pytest.mark.parametrize("trigger", ["none", "rain"])
async def test_on_clean_info_does_not_publish_a_trigger(trigger: str) -> None:
    # Unlike OnScheduleTaskInfo. notify_trigger publishes any non-empty string,
    # and onCleanInfo carries "trigger": "none" in every captured sample — and
    # MowerTriggerEvent carries a _seq, so none of them would be deduped. That
    # is one event, one task and one debug line per push, for a consumer that
    # only reacts to "rain" (sensor.py). Taking the trigger from here would be a
    # behaviour change this fix has no reason to make.
    bus = _bus()
    register(bus)
    triggers = _collect(bus, MowerTriggerEvent)

    OnCleanInfo._handle_body_data_dict(
        bus,
        {"trigger": trigger, "state": "clean", "cleanState": {"motionState": "pause"}},
    )
    await asyncio.sleep(0)

    # "none" is what every captured sample carries, and is the case that would
    # have produced the noise. "rain" is the one value a consumer acts on, so it
    # is the case where leaking a trigger from here would change behaviour.
    assert triggers == []


async def test_the_pushed_charge_state_sets_the_dock_too() -> None:
    # Observed on 1.36.208 in issue #67: iot/atr/onChargeState carrying
    # {"isCharging": 1, "mode": "slot"}, four seconds before a paused plan.
    bus = _bus()
    record = register(bus)
    published = _collect(bus, StateEvent)

    OnChargeState._handle_body_data_dict(bus, {"isCharging": 1, "mode": "slot"})
    await asyncio.sleep(0)

    assert record.docked is True
    assert [event.state for event in published] == [State.DOCKED]


async def test_on_charge_state_is_registered_and_reachable() -> None:
    apply()
    assert get_message("onChargeState", _static_device_info()) is OnChargeState


async def test_docking_sets_the_dock_and_go_charging_clears_it() -> None:
    # onChargeInfo is how a mid-session docking is announced. The poll loop
    # stops on DOCKED, so nothing asks getChargeState again — without this the
    # record would stay unset until something restarted polling, which is one
    # flap per docking.
    bus = _bus()
    record = register(bus)
    record.dock()

    OnChargeInfo._handle_body_data_dict(bus, {"trigger": "app", "state": "goCharging"})
    assert record.docked is False

    OnChargeInfo._handle_body_data_dict(bus, {"trigger": "workComplete", "state": "idle"})
    assert record.docked is True


async def test_an_unregistered_bus_survives_the_charge_handlers() -> None:
    bus = _bus()
    published = _collect(bus, StateEvent)

    GetChargeStateMower._handle_body_data_dict(bus, {"isCharging": 1})
    OnChargeState._handle_body_data_dict(bus, {"isCharging": 1})
    await asyncio.sleep(0)

    assert [event.state for event in published] == [State.DOCKED]


async def test_a_fail_coded_push_matches_the_librarys_own_handler() -> None:
    # {"msg": "fail", "code": "30007"} — "already charging" answered as a
    # failure — never reaches _handle_body_data_dict at all:
    # GetChargeState._handle_body branches on the code before descending into
    # body->data. Driving _handle_body_data_dict directly, like the sibling
    # test above, cannot catch a regression in that branch — only going
    # through handle() does, which is what this test does on both sides.
    #
    # apply() registers OnChargeState for onChargeState on every JSON device
    # on the account (see the class docstring), so an ordinary Deebot vacuum's
    # push has to come out exactly where it would have without us: compared
    # here against the library's own GetChargeState.handle() on the same body.
    from deebot_client.commands.json.charge_state import GetChargeState

    message = {"body": {"msg": "fail", "code": "30007"}}

    library_bus = Mock()
    library_result = GetChargeState.handle(library_bus, message)

    bus = _bus()
    record = register(bus)
    published = _collect(bus, StateEvent)

    result = OnChargeState.handle(bus, message)
    await asyncio.sleep(0)

    assert result.state == library_result.state
    assert [event.state for event in published] == [State.DOCKED]
    # The fail-code dock recording (issue #67, GetChargeStateMower._handle_body
    # in commands.py) is reachable from this push path too, not only from a
    # getChargeState answer — the bug this test guards against left it
    # unreachable here.
    assert record.docked is True


# The four task bury points that mark a job's boundaries, captured verbatim.
# schedule-* from a GOAT O1200 (2i0fns, fw 1.13.10) on 2026-08-28; spotarea-*
# from a GOAT O800 RTK (2px96q, fw 1.17.8). Issue #73.
_SCHEDULE_STOP_COMPLETE = {
    "bid": "4411787917350066",
    "gid": "G1787394423395",
    "index": "0000078947",
    "jobId": "4641787900401334",
    "mapId": "511702305",
    "mowType": 1,
    "mowedArea": 320.567505,
    "sid": "8651787917350067",
    "time": 12328.418945,
    "trigger": "workComplete",
    "ts": "1787917350067",
    "workArea": 320.567505,
    "workType": 18,
}
_SPOTAREA_STOP_COMPLETE = {
    "bid": "8621787752144121",
    "gid": "G1695142647602",
    "index": "0000006171",
    "jobId": "4921787750720504",
    "mapId": "1782436840",
    "mowType": 0,
    "mowedArea": 24.287498,
    "sid": "1231787752144121",
    "time": 1351.234253,
    "trigger": "workComplete",
    "ts": "1787752144122",
    "workArea": 32.162498,
    "workType": 2,
}
_SCHEDULE_START = {
    "bid": "4671787900414387",
    "gid": "G1787394423395",
    "index": "0000065374",
    "jobId": "4641787900401334",
    "mapId": "511702305",
    "sid": "6011787900414387",
    "trigger": "schedule",
    "ts": "1787900414388",
    "workType": 18,
}
_SPOTAREA_START_APP = {
    "areaNum": 2,
    "bid": "2111787728912399",
    "gid": "G1695142651057",
    "index": "0000003454",
    "jobId": "3351787728907186",
    "mapId": "628175011",
    "sid": "3681787728912399",
    "trigger": "app",
    "ts": "1787728912400",
}


def _notified_edges(message, data: dict[str, Any]) -> list[MowerJobEdgeEvent]:
    bus = Mock()
    message._handle_body(bus, data)
    return [
        call_.args[0]
        for call_ in bus.notify.call_args_list
        if isinstance(call_.args[0], MowerJobEdgeEvent)
    ]


def test_a_completed_schedule_job_publishes_both_areas() -> None:
    """The completion carries the numbers the progress sensor needs (issue #73)."""
    (event,) = _notified_edges(OnMowScheduleStop, _SCHEDULE_STOP_COMPLETE)

    assert event.phase == "stop"
    assert event.trigger == "workComplete"
    assert event.mowed_area == 320.567505
    assert event.work_area == 320.567505


def test_a_completed_zone_job_publishes_its_own_areas() -> None:
    """A zone completion is not 100 %: workArea is the polygon's estimate."""
    (event,) = _notified_edges(OnMowSpotAreaStop, _SPOTAREA_STOP_COMPLETE)

    assert event.phase == "stop"
    assert event.trigger == "workComplete"
    assert event.mowed_area == 24.287498
    assert event.work_area == 32.162498


def test_a_start_publishes_no_areas() -> None:
    """A start says a job began, not how far along it is — the fields are absent."""
    (event,) = _notified_edges(OnMowScheduleStart, _SCHEDULE_START)

    assert event.phase == "start"
    assert event.trigger == "schedule"
    assert event.mowed_area is None
    assert event.work_area is None


def test_an_app_started_zone_job_is_a_start_too() -> None:
    """The middle topic segment is the job type, not the trigger (issue #73)."""
    (event,) = _notified_edges(OnMowSpotAreaStart, _SPOTAREA_START_APP)

    assert event.phase == "start"
    assert event.trigger == "app"


def test_the_raw_trigger_is_published_uninterpreted() -> None:
    """This layer decodes the wire format and nothing else.

    ``reborn`` is a start trigger seen seven times in twelve minutes on a
    2px96q, each with a fresh jobId. Deciding that it does not begin a new job
    is the sensor's business, not this handler's — the handler must not filter
    it out.
    """
    (event,) = _notified_edges(
        OnMowSpotAreaStart, {**_SPOTAREA_START_APP, "trigger": "reborn"}
    )

    assert event.trigger == "reborn"


def test_a_payload_without_a_trigger_is_analysed() -> None:
    result = OnMowScheduleStop._handle_body(Mock(), {"mowedArea": 1.0})

    assert result.state == HandlingState.ANALYSE


# border-* from a GOAT G1-800 (77atlz, fw 1.36.208) on 2026-08-30, issue #74.
# A third payload dialect: ``triggerType`` instead of ``trigger``, and the
# stop carries ``cuttedArea`` instead of ``mowedArea``.
_BORDER_START = {
    "index": "0000000842",
    "mapid": "2049987783",
    "mowId": "1788074264229565",
    "triggerType": "app",
    "ts": "1788074264230",
}
_BORDER_STOP = {
    "index": "0000000851",
    "mapid": "2049987783",
    "mowId": "1788074264229565",
    "triggerType": "app",
    "ts": "1788074293897",
    "cuttedArea": 1.63,
    "workArea": 19.24,
    "time": 0.083333,
}


def test_a_border_start_reads_the_trigger_from_triggerType() -> None:
    """The border dialect names its trigger ``triggerType`` (issue #74)."""
    (event,) = _notified_edges(OnMowBorderStart, _BORDER_START)

    assert event.phase == "start"
    assert event.trigger == "app"
    assert event.mowed_area is None
    assert event.work_area is None


def test_a_border_stop_reads_the_mowed_area_from_cuttedArea() -> None:
    """The border stop calls its mowed square metres ``cuttedArea`` (issue #74)."""
    (event,) = _notified_edges(OnMowBorderStop, _BORDER_STOP)

    assert event.phase == "stop"
    assert event.trigger == "app"
    assert event.mowed_area == 1.63
    assert event.work_area == 19.24


def test_the_original_keys_win_over_the_border_dialect() -> None:
    """A payload carrying both spellings is read the way it always was.

    The fallbacks must not change what the schedule and spot-area paths see —
    ``trigger`` before ``triggerType``, ``mowedArea`` before ``cuttedArea``.
    """
    (event,) = _notified_edges(
        OnMowScheduleStop,
        {**_SCHEDULE_STOP_COMPLETE, "triggerType": "app", "cuttedArea": 1.0},
    )

    assert event.trigger == "workComplete"
    assert event.mowed_area == 320.567505


def test_the_border_job_edge_names_are_registered() -> None:
    apply()

    assert MESSAGES["onFwBuryPoint-bd_task-mow-border-start"] is OnMowBorderStart
    assert MESSAGES["onFwBuryPoint-bd_task-mow-border-stop"] is OnMowBorderStop


async def test_two_identical_starts_both_reach_the_subscriber() -> None:
    """_seq, not defensiveness: two reborn starts are byte-identical payloads.

    EventBus.notify drops an event equal to the previous one of the same type
    before any subscriber runs, so without _seq the second start would be
    swallowed and a new job would keep the previous job's percentage.
    """
    bus = _bus()
    published = _collect(bus, MowerJobEdgeEvent)

    OnMowScheduleStart._handle_body(bus, _SCHEDULE_START)
    OnMowScheduleStart._handle_body(bus, _SCHEDULE_START)
    await asyncio.sleep(0)

    assert len(published) == 2


def test_the_schedule_and_spotarea_job_edge_names_are_registered() -> None:
    apply()

    assert MESSAGES["onFwBuryPoint-bd_task-mow-schedule-start"] is OnMowScheduleStart
    assert MESSAGES["onFwBuryPoint-bd_task-mow-schedule-stop"] is OnMowScheduleStop
    assert MESSAGES["onFwBuryPoint-bd_task-mow-spotarea-start"] is OnMowSpotAreaStart
    assert MESSAGES["onFwBuryPoint-bd_task-mow-spotarea-stop"] is OnMowSpotAreaStop


def test_the_return_trip_is_not_registered() -> None:
    """onFwBuryPoint-bd_task-return-normal-stop also carries trigger workComplete.

    There it means the drive home finished, not the job. Registering it would
    make the sensor write a final value on a docking (issue #73).
    """
    apply()

    assert "onFwBuryPoint-bd_task-return-normal-stop" not in MESSAGES


def test_a_job_edge_survives_the_whole_message_path() -> None:
    """Every other test here calls _handle_body directly, which skips the path
    the wire actually takes.

    handle() -> MessageDictOrJson._handle -> MessageBody._handle_dict -> ours,
    against the raw bytes as logged, so the header is parsed and the body is
    forwarded without a ``data`` wrapper. Also the resolution the mqtt client
    performs: get_message() on the topic's third segment.
    """
    apply()

    raw = (
        b'{"header":{"tzm":120,"ts":"1787917350068470636","fwVer":"1.13.10"},'
        b'"body":{"bid":"4411787917350066","jobId":"4641787900401334",'
        b'"mowType":1,"mowedArea":320.567505,"time":12328.418945,'
        b'"trigger":"workComplete","workArea":320.567505,"workType":18}}'
    )
    static = _static_device_info()
    bus = Mock()

    assert get_message("onFwBuryPoint-bd_task-mow-schedule-stop", static) is (
        OnMowScheduleStop
    )
    assert get_message("onFwBuryPoint-bd_task-return-normal-stop", static) is None

    result = OnMowScheduleStop.handle(bus, raw)

    assert result.state == HandlingState.SUCCESS
    (event,) = [
        call_.args[0]
        for call_ in bus.notify.call_args_list
        if isinstance(call_.args[0], MowerJobEdgeEvent)
    ]
    assert event.phase == "stop"
    assert event.trigger == "workComplete"
    assert event.work_area == 320.567505


# The onUWB push verbatim from the log attached to issue #40, a GOAT G1-800
# (77atlz) on firmware 1.36.208 with four beacons, captured during a border job.
# Serials are placeholders; the reporter redacted the real ones. x/y are zeroed
# in the push — unlike the getPos reply, where uwbPos carries map-frame
# coordinates — and state/otaResult read 0 on all four including the flat one,
# which is what rules state out as a health flag.
_UWB_PUSH = {
    "mid": 2049987783,
    "uwbPos": [
        {"x": 0, "y": 0, "sn": "BEACON-1", "state": 0, "battery": 100, "otaResult": 0},
        {"x": 0, "y": 0, "sn": "BEACON-2", "state": 0, "battery": 68, "otaResult": 0},
        {"x": 0, "y": 0, "sn": "BEACON-3", "state": 0, "battery": 0, "otaResult": 0},
        {"x": 0, "y": 0, "sn": "BEACON-4", "state": 0, "battery": 73, "otaResult": 0},
    ],
}

# The same four beacons as the life-span source reports them, from the same
# capture. BEACON-1 is the one the two sources disagree about: a round 100 in
# the push above, 83 here, in all seven samples across two days and before and
# after another beacon's cells were replaced. The other three agree exactly.
_LIFE_SPANS_SAME_BEACONS = [
    {"type": "uwbCell", "sn": "BEACON-1", "left": 83, "total": 100},
    {"type": "uwbCell", "sn": "BEACON-2", "left": 68, "total": 100},
    {"type": "uwbCell", "sn": "BEACON-3", "left": 0, "total": 100},
    {"type": "uwbCell", "sn": "BEACON-4", "left": 73, "total": 100},
]


def _pushed_beacons(bus, data: dict[str, Any]) -> list[tuple[str, float]]:
    """Run OnUwb against *bus* and return the readings it published.

    The bus is passed in rather than made here: every precedence test needs the
    life-span source to have spoken on the *same* bus first, and the readings
    are keyed by it.
    """
    OnUwb._handle_body_data_dict(bus, data)
    return [
        (beacon.sn, beacon.percent)
        for call_ in bus.notify.call_args_list
        if isinstance(call_.args[0], MowerBeaconsEvent)
        for beacon in call_.args[0].beacons
    ]


def test_on_uwb_publishes_the_batteries_the_push_carries() -> None:
    # Nothing has polled yet, which is the case this exists for: the poll is
    # what fails with errno 500 on this firmware (issue #42).
    assert _pushed_beacons(Mock(), _UWB_PUSH) == [
        ("BEACON-1", 100.0),
        ("BEACON-2", 68.0),
        ("BEACON-3", 0.0),
        ("BEACON-4", 73.0),
    ]


def test_on_uwb_keeps_the_life_span_reading_where_the_two_disagree() -> None:
    # The whole reason this handler cannot simply republish what it is handed.
    # BEACON-1 reads 100 in the push and 83 in the life-span answer, every
    # sample; feeding both into one event would flap the sensor between them.
    # Which number is *right* is unsettled, so the one that is never optimistic
    # wins and the other is only ever a floor.
    # Only the disputed beacon has been polled, so the push has something to
    # contribute for the other three and does publish — which is the only case
    # where the two numbers could ever meet in one event.
    bus = Mock()
    notify_mower_beacons(bus, _LIFE_SPANS_SAME_BEACONS[:1])
    # Without this the life-span event itself would be counted as the push's
    # output and the assertion would hold whatever OnUwb did.
    bus.reset_mock()

    assert ("BEACON-1", 83.0) in _pushed_beacons(bus, _UWB_PUSH)


def test_on_uwb_says_nothing_when_the_poll_has_reported_every_beacon() -> None:
    # Once every serial is polled the push contributes nothing, so it publishes
    # nothing rather than a set the bus would have to dedupe.
    bus = Mock()
    notify_mower_beacons(bus, _LIFE_SPANS_SAME_BEACONS)
    bus.reset_mock()

    assert _pushed_beacons(bus, _UWB_PUSH) == []


def test_on_uwb_fills_in_the_beacon_the_poll_never_reported() -> None:
    # The set is published whole, not just the serial the push contributes:
    # EcovacsBeaconSensor reads a beacon missing from an event as unknown, so a
    # partial set would blank the three the poll does know.
    bus = Mock()
    notify_mower_beacons(bus, _LIFE_SPANS_SAME_BEACONS[:3])
    bus.reset_mock()

    assert _pushed_beacons(bus, _UWB_PUSH) == [
        ("BEACON-1", 83.0),
        ("BEACON-2", 68.0),
        ("BEACON-3", 0.0),
        ("BEACON-4", 73.0),
    ]


def test_a_life_span_answer_that_drops_a_beacon_stops_speaking_for_it() -> None:
    # The readings are replaced, not merged: an answer that no longer lists
    # BEACON-1 is the life-span source withdrawing its claim about it, and the
    # push is then the only thing that knows anything.
    bus = Mock()
    notify_mower_beacons(bus, _LIFE_SPANS_SAME_BEACONS)
    notify_mower_beacons(bus, _LIFE_SPANS_SAME_BEACONS[1:])
    bus.reset_mock()

    assert ("BEACON-1", 100.0) in _pushed_beacons(bus, _UWB_PUSH)


def test_on_uwb_ignores_the_zeroed_coordinates() -> None:
    # x/y are 0 on every sample of this push, so they are not positions. OnPos
    # is where a real one comes from.
    bus = Mock()
    OnUwb._handle_body_data_dict(bus, _UWB_PUSH)

    assert not any(
        isinstance(call_.args[0], PositionsEvent)
        for call_ in bus.notify.call_args_list
    )


def test_on_uwb_drops_a_beacon_with_no_serial() -> None:
    # Same rule as the life-span parser: without a serial the reading cannot be
    # attributed to a beacon, and the wrong beacon is worse than none.
    assert _pushed_beacons(
        Mock(),
        {
            "uwbPos": [
                {"battery": 20},
                {"sn": "BEACON-2", "battery": 50},
            ]
        },
    ) == [("BEACON-2", 50.0)]


def test_on_uwb_drops_a_repeated_serial() -> None:
    # A repeated sn would reach the sensor platform as two entities sharing one
    # unique_id. Keep the first reading, drop the rest.
    assert _pushed_beacons(
        Mock(),
        {
            "uwbPos": [
                {"sn": "BEACON-1", "battery": 20},
                {"sn": "BEACON-1", "battery": 80},
            ]
        },
    ) == [("BEACON-1", 20.0)]


@pytest.mark.parametrize("battery", ["68", None, True, -1, 101])
def test_on_uwb_drops_a_battery_it_cannot_read_as_a_percentage(battery: Any) -> None:
    # A bare float() would turn "68" into a reading and True into 1 %. Out of
    # range is dropped too: this field feeds a battery device class, and HA's
    # own low-battery handling is built on it meaning what it says.
    assert _pushed_beacons(
        Mock(),
        {
            "uwbPos": [
                {"sn": "BEACON-1", "battery": battery},
                {"sn": "BEACON-2", "battery": 50},
            ]
        },
    ) == [("BEACON-2", 50.0)]


def test_on_uwb_without_a_position_list_asks_for_analysis() -> None:
    # The handler directly rather than through Message.handle, for the reason
    # test_on_rain_delay_without_enable_asks_for_analysis documents.
    result = OnUwb._handle_body_data_dict(Mock(), {"mid": 1})
    assert result.state is HandlingState.ANALYSE


def test_on_uwb_handles_a_push_with_no_beacons() -> None:
    # A mower that navigates without beacons has an empty list to report, not a
    # payload this handler failed to understand.
    result = OnUwb._handle_body_data_dict(Mock(), {"mid": 1, "uwbPos": []})
    assert result.state is HandlingState.SUCCESS


def test_on_uwb_message_name() -> None:
    assert OnUwb.NAME == "onUWB"


def test_on_uwb_is_registered_by_apply() -> None:
    # The library has no handler at all, so without this the push is logged as
    # 'Unknown message "onUWB"' and the beacon sensors move only on the poll
    # (issue #40).
    apply()
    assert MESSAGES["onUWB"] is OnUwb
