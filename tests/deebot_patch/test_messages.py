"""Tests for the message handlers the library lacks."""

from __future__ import annotations

import asyncio
from dataclasses import fields
from datetime import time
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
from custom_components.ecovacs_mower.deebot_patch import messages as messages_module
from custom_components.ecovacs_mower.deebot_patch.commands import GetChargeStateMower
from custom_components.ecovacs_mower.deebot_patch.messages import (
    MowerAnimProtectEvent,
    MowerBeaconsEvent,
    MowerCoreTemperatureEvent,
    MowerJobEdgeEvent,
    MowerProtectStateEvent,
    MowerRainDelayEvent,
    MowerStatsEvent,
    MowerTriggerEvent,
    OnAnimProtect,
    OnChargeInfo,
    OnChargeState,
    OnCleanInfo,
    OnCoreInfo,
    OnMowAutoStart,
    OnMowAutoStop,
    OnMowBorderStart,
    OnMowBorderStop,
    OnMowSchedulePause,
    OnMowScheduleResume,
    OnMowScheduleStart,
    OnMowScheduleStop,
    OnMowSpotAreaPause,
    OnMowSpotAreaResume,
    OnMowSpotAreaStart,
    OnMowSpotAreaStop,
    OnPos,
    OnProtectState,
    OnRainDelay,
    OnReturnNormalStart,
    OnScheduleTaskInfo,
    OnStatsMower,
    OnUwb,
    handle_clean_info,
    notify_mower_beacons,
)
from custom_components.ecovacs_mower.deebot_patch.state_precedence import (
    DEPARTURE_WINDOW,
    MowerStateRecord,
    register,
)


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


# The push that followed a toggle in the app, captured on a GOAT G1-800
# (77atlz) on firmware 1.36.208 with deebot_client at debug (issue #45). The
# clock arrives padded here and unpadded in the getAnimProtect answer.
_ANIM_PROTECT = {"enable": 1, "start": "19:00", "end": "07:00"}


def test_on_anim_protect_notifies_the_setting_and_its_window() -> None:
    assert _notified(OnAnimProtect, _ANIM_PROTECT, MowerAnimProtectEvent) == [
        MowerAnimProtectEvent(enabled=True, start=time(19, 0), end=time(7, 0))
    ]


def test_on_anim_protect_reads_the_unpadded_clock_of_the_get_answer() -> None:
    # "19:0" is how getAnimProtect answers the same window. Both forms have to
    # land on the same event, or the switch would flap between a known and an
    # unknown window depending on which of the two arrived last.
    data = {"enable": 0, "start": "19:0", "end": "7:0"}
    assert _notified(OnAnimProtect, data, MowerAnimProtectEvent) == [
        MowerAnimProtectEvent(enabled=False, start=time(19, 0), end=time(7, 0))
    ]


@pytest.mark.parametrize(
    "clock", [None, "", "19", "19:", ":00", "24:00", "19:60", "7:5a", 1900, "-1:00"]
)
def test_on_anim_protect_leaves_an_unreadable_clock_unknown(clock: object) -> None:
    # The setting is still worth publishing; only the unreadable end is lost,
    # and None is what makes the switch refuse to write it back.
    data = {"enable": 1, "start": clock, "end": "07:00"}
    assert _notified(OnAnimProtect, data, MowerAnimProtectEvent) == [
        MowerAnimProtectEvent(enabled=True, start=None, end=time(7, 0))
    ]


def test_on_anim_protect_without_a_window_leaves_it_unknown() -> None:
    assert _notified(OnAnimProtect, {"enable": 1}, MowerAnimProtectEvent) == [
        MowerAnimProtectEvent(enabled=True, start=None, end=None)
    ]


@pytest.mark.parametrize("enable", [None, "0", "1", 2, -1])
def test_on_anim_protect_drops_a_payload_without_a_usable_enable(
    enable: object,
) -> None:
    # Same rule as onRainDelay: setAnimProtect writes the setting back, so a
    # guessed enable would be sent to the mower on the next toggle.
    data: dict[str, Any] = {"start": "19:00", "end": "07:00"}
    if enable is not None:
        data["enable"] = enable
    assert _notified(OnAnimProtect, data, MowerAnimProtectEvent) == []


def test_on_anim_protect_without_enable_asks_for_analysis() -> None:
    result = OnAnimProtect._handle_body_data_dict(
        Mock(), {"start": "19:00", "end": "07:00"}
    )
    assert result.state is HandlingState.ANALYSE


def test_on_anim_protect_message_name() -> None:
    assert OnAnimProtect.NAME == "onAnimProtect"


def test_on_anim_protect_is_registered_by_apply() -> None:
    # Logged as 'Unknown message "onAnimProtect"' until now (issue #45).
    apply()
    assert MESSAGES["onAnimProtect"] is OnAnimProtect


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


# auto-* from a GOAT G1-800 (77atlz, fw 1.36.208) on 2026-09-04, issue #74: a
# job started from Home Assistant. The library's ``Clean`` sends
# ``type: "auto"`` on a start, so this is the one job type the integration
# itself starts — and it was the one without a handler. The stop speaks the
# border dialect (``triggerType``, ``cuttedArea``) and adds ``pauseId``/
# ``resumeId`` for the pause and resume the job went through. The report
# listed ``triggerType``, ``cuttedArea``, ``workArea`` and the id fields
# (``mowId``/``pauseId``/``resumeId``/``bid``/``sid``) and quoted the areas;
# the ids are placeholders of the observed shape. ``index`` and ``ts`` were
# not in the report — they are carried over from ``_BORDER_STOP``.
_AUTO_STOP = {
    "bid": "1011788091708000",
    "index": "0000001204",
    "mowId": "1788089834000000",
    "pauseId": "1788091081000000",
    "resumeId": "1788091088000000",
    "sid": "2021788091708000",
    "triggerType": "app",
    "ts": "1788091708000",
    "cuttedArea": 48.865,
    "workArea": 137.675,
}
# The start was not quoted in the report. Modelled on the border start from
# the same firmware, which is the other job type it announces in this dialect.
_AUTO_START = {
    "index": "0000001187",
    "mapid": "2049987783",
    "mowId": "1788089834000000",
    "triggerType": "app",
    "ts": "1788089834000",
}


def test_an_auto_stop_from_home_assistant_publishes_both_areas() -> None:
    """A job the integration started ends on ``mow-auto-stop`` (issue #74)."""
    (event,) = _notified_edges(OnMowAutoStop, _AUTO_STOP)

    assert event.phase == "stop"
    assert event.trigger == "app"
    assert event.mowed_area == 48.865
    assert event.work_area == 137.675


def test_an_auto_start_is_a_start() -> None:
    (event,) = _notified_edges(OnMowAutoStart, _AUTO_START)

    assert event.phase == "start"
    assert event.trigger == "app"
    assert event.mowed_area is None
    assert event.work_area is None


def test_the_auto_job_edge_names_are_registered() -> None:
    apply()

    assert MESSAGES["onFwBuryPoint-bd_task-mow-auto-start"] is OnMowAutoStart
    assert MESSAGES["onFwBuryPoint-bd_task-mow-auto-stop"] is OnMowAutoStop


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


# Issue #120. The G1-800 (77atlz, fw 1.36.208) bodies are verbatim from the
# reporter's log, ids anonymised by the reporter; the O1200 (2i0fns, fw
# 1.13.10) bodies are from the maintainer's own mower on 2026-10-12. See the
# spec's Fixtures section for the full sequences.
_G1800_RESUME_1300 = {
    "bid": "7101791802821723",
    "gid": "G1791648417330",
    "index": "0000000435",
    "mapid": "0809159361",
    "pauseId": "9211791733414102",
    "resumeId": "2261791802821723",
    "scheduleId": "1791725416643502",
    "sid": "6211791802821723",
    "triggerType": "schedule",
    "ts": "1791802821723",
}
_G1800_PAUSE_1330 = {
    "bid": "9831791804621723",
    "cuttedArea": 199.292500,
    "gid": "G1791648417330",
    "index": "0000000454",
    "mapid": "0809159361",
    "pauseId": "4491791804621723",
    "resumeId": "2261791802821723",
    "scheduleId": "1791725416643502",
    "sid": "4301791804621723",
    "time": 0,
    "triggerType": "schedule",
    "ts": "1791804621723",
    "workArea": 280.177500,
}
_G1800_RESUME_1400 = {
    "bid": "7931791806421723",
    "gid": "G1791648417330",
    "index": "0000000491",
    "mapid": "0809159361",
    "pauseId": "4491791804621723",
    "resumeId": "1241791806421724",
    "scheduleId": "1791725416643502",
    "sid": "0781791806421724",
    "triggerType": "schedule",
    "ts": "1791806421724",
}
_G1800_STOP_1501 = {
    "bid": "0341791810117742",
    "cuttedArea": 280.202525,
    "gid": "G1791648417330",
    "index": "0000000521",
    "mapid": "0809159361",
    "pauseId": "4491791804621723",
    "resumeId": "1241791806421724",
    "scheduleId": "1791725416643502",
    "sid": "4061791810117742",
    "time": 194.183334,
    # Lower-case c, unlike every other firmware's workComplete. A separate
    # bug in the stop handling, out of scope here (see the spec's Non-goals).
    "triggerType": "workcomplete",
    "ts": "1791810117742",
    "workArea": 280.202525,
}
_O1200_RESUME_1147 = {
    "bid": "7081791798470484",
    "gid": "G1790503263492",
    "index": "0000133051",
    "jobId": "5251791788453307",
    "mapId": "511702305",
    "sid": "4161791798470484",
    "trigger": "continue",
    "ts": "1791798470485",
    "workType": 18,
}
_O1200_START_0901 = {
    "bid": "7301791788466359",
    "gid": "G1790503263492",
    "index": "0000126501",
    "jobId": "5251791788453307",
    "mapId": "511702305",
    "sid": "8781791788466360",
    "trigger": "schedule",
    "ts": "1791788466360",
    "workType": 18,
}


@pytest.mark.parametrize(
    ("message", "phase", "job_type"),
    [
        (OnMowAutoStart, "start", "auto"),
        (OnMowAutoStop, "stop", "auto"),
        (OnMowBorderStart, "start", "border"),
        (OnMowBorderStop, "stop", "border"),
        (OnMowSchedulePause, "pause", "schedule"),
        (OnMowScheduleResume, "resume", "schedule"),
        (OnMowScheduleStart, "start", "schedule"),
        (OnMowScheduleStop, "stop", "schedule"),
        (OnMowSpotAreaPause, "pause", "spotarea"),
        (OnMowSpotAreaResume, "resume", "spotarea"),
        (OnMowSpotAreaStart, "start", "spotarea"),
        (OnMowSpotAreaStop, "stop", "spotarea"),
    ],
)
def test_every_edge_carries_its_phase_and_job_type(
    message, phase: str, job_type: str
) -> None:
    """The middle topic segment is the job type, and it travels with the edge.

    It is the one signal that says "the schedule's job" the same way on every
    firmware: the O1200's trigger: continue and the G1-800's triggerType:
    schedule both arrive on mow-schedule-resume (issue #120).
    """
    (event,) = _notified_edges(message, {"trigger": "app"})

    assert event.phase == phase
    assert event.job_type == job_type


def test_a_g1800_scheduled_resume_is_a_resume_of_the_schedules_job() -> None:
    """The window opening at 13:00 in issue #120, in the triggerType dialect."""
    (event,) = _notified_edges(OnMowScheduleResume, _G1800_RESUME_1300)

    assert event.phase == "resume"
    assert event.trigger == "schedule"
    assert event.job_type == "schedule"
    assert event.mowed_area is None


def test_a_g1800_scheduled_pause_carries_its_areas() -> None:
    """The window closing at 13:30 in issue #120: cuttedArea, not mowedArea."""
    (event,) = _notified_edges(OnMowSchedulePause, _G1800_PAUSE_1330)

    assert event.phase == "pause"
    assert event.trigger == "schedule"
    assert event.mowed_area == 199.2925
    assert event.work_area == 280.1775


def test_an_o1200_charge_break_resume_says_continue() -> None:
    """The O1200's resume after a charge break, on the same topic (2026-10-12)."""
    (event,) = _notified_edges(OnMowScheduleResume, _O1200_RESUME_1147)

    assert event.phase == "resume"
    assert event.trigger == "continue"
    assert event.job_type == "schedule"


def test_the_pause_and_resume_job_edge_names_are_registered() -> None:
    """Logged as 'Unknown message' until issue #120."""
    apply()

    assert MESSAGES["onFwBuryPoint-bd_task-mow-schedule-pause"] is OnMowSchedulePause
    assert MESSAGES["onFwBuryPoint-bd_task-mow-schedule-resume"] is OnMowScheduleResume
    assert MESSAGES["onFwBuryPoint-bd_task-mow-spotarea-pause"] is OnMowSpotAreaPause
    assert MESSAGES["onFwBuryPoint-bd_task-mow-spotarea-resume"] is OnMowSpotAreaResume


def test_the_end_of_the_return_trip_is_not_registered() -> None:
    """onFwBuryPoint-bd_task-return-normal-stop also carries trigger workComplete.

    There it means the drive home finished, not the job. Registering it would
    make the sensor write a final value on a docking (issue #73). Its sibling
    return-normal-start is registered (issue #120); the docking this one
    announces already arrives as onChargeState isCharging 1.
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


def test_on_uwb_unions_in_a_polled_beacon_the_push_omits() -> None:
    # Whole relative to the poll, not just to this push: BEACON-4 has been
    # polled but this push does not mention it at all, and a fifth, never
    # polled serial is what makes the push contribute and therefore publish.
    # Omission is not the same as the life-span source withdrawing its claim —
    # only notify_mower_beacons gets to do that — so BEACON-4 must still
    # appear, sourced from the poll.
    bus = Mock()
    notify_mower_beacons(bus, _LIFE_SPANS_SAME_BEACONS)
    bus.reset_mock()

    published = _pushed_beacons(
        bus,
        {
            "uwbPos": [
                {"sn": "BEACON-1", "battery": 100},
                {"sn": "BEACON-2", "battery": 68},
                {"sn": "BEACON-3", "battery": 0},
                {"sn": "BEACON-5", "battery": 42},
            ]
        },
    )

    assert set(published) == {
        ("BEACON-1", 83.0),
        ("BEACON-2", 68.0),
        ("BEACON-3", 0.0),
        ("BEACON-4", 73.0),
        ("BEACON-5", 42.0),
    }


def test_life_span_readings_are_kept_per_bus() -> None:
    # _LIFE_SPAN_READINGS is keyed by EventBus for the same reason
    # state_precedence's records are: a message handler is a classmethod that
    # receives nothing else to key on, and every device has its own bus. A
    # life-span answer on one device must not decide precedence for another.
    bus_a, bus_b = Mock(), Mock()
    notify_mower_beacons(bus_a, _LIFE_SPANS_SAME_BEACONS)
    bus_a.reset_mock()
    bus_b.reset_mock()

    assert _pushed_beacons(bus_b, _UWB_PUSH) == [
        ("BEACON-1", 100.0),
        ("BEACON-2", 68.0),
        ("BEACON-3", 0.0),
        ("BEACON-4", 73.0),
    ]


def test_a_life_span_answer_with_no_beacons_at_all_stops_speaking_for_any() -> None:
    # The replace has to happen even when the new set is empty: a mower that
    # stops reporting beacons entirely (unpaired, or a firmware that drops
    # the component) is exactly the case where a stale reading would
    # otherwise linger forever, since there is never a later non-empty
    # answer to replace it.
    bus = Mock()
    notify_mower_beacons(bus, _LIFE_SPANS_SAME_BEACONS)
    notify_mower_beacons(bus, [])
    bus.reset_mock()

    assert _pushed_beacons(bus, _UWB_PUSH) == [
        ("BEACON-1", 100.0),
        ("BEACON-2", 68.0),
        ("BEACON-3", 0.0),
        ("BEACON-4", 73.0),
    ]


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


def test_on_uwb_drops_a_non_dict_entry() -> None:
    # A bare string in uwbPos has no .get, so the isinstance guard is what
    # keeps it from raising instead of just being dropped like any other
    # entry without a usable serial.
    assert _pushed_beacons(
        Mock(),
        {"uwbPos": ["not-a-dict", {"sn": "BEACON-2", "battery": 50}]},
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


# Issue #94: the handler remembers the type of the running job.


async def test_clean_info_records_the_job_type() -> None:
    bus = _bus()
    record = register(bus)

    handle_clean_info(
        bus,
        {
            "trigger": "app",
            "state": "clean",
            "cleanState": {
                "motionState": "working",
                "cid": "122",
                "content": {"type": "spotArea", "value": "2"},
            },
        },
    )

    assert record.job_type == "spotArea"


async def test_a_paused_clean_info_records_the_job_type_too() -> None:
    # The forced poll while the O1200 stood paused on the lawn answered this.
    bus = _bus()
    record = register(bus)

    handle_clean_info(
        bus,
        {
            "trigger": "app",
            "state": "clean",
            "cleanState": {"motionState": "pause", "content": {"type": "spotArea"}},
        },
    )

    assert record.job_type == "spotArea"


async def test_a_paused_clean_info_records_the_job_type_even_while_docked() -> None:
    # The #67 gate withholds the StateEvent; it must not withhold the type.
    bus = _bus()
    record = register(bus)
    record.dock()

    handle_clean_info(
        bus,
        {
            "state": "clean",
            "cleanState": {"motionState": "pause", "content": {"type": "spotArea"}},
        },
    )

    assert record.job_type == "spotArea"


async def test_an_idle_push_without_clean_state_forgets_the_job_type() -> None:
    # What the app's End produced on 2026-09-10: state idle, no cleanState.
    bus = _bus()
    record = register(bus)
    record.note_job({"type": "spotArea"})

    handle_clean_info(bus, {"trigger": "app", "other": "", "state": "idle"})

    assert record.job_type is None


async def test_a_clean_info_without_content_keeps_the_job_type() -> None:
    # onScheduleTaskInfo shares this handler and carries no content.
    bus = _bus()
    record = register(bus)
    record.note_job({"type": "spotArea"})

    handle_clean_info(bus, {"state": "clean", "cleanState": {"motionState": "working"}})

    assert record.job_type == "spotArea"


async def test_a_null_clean_state_does_not_raise() -> None:
    # A present-but-null cleanState is not covered by note_job's own dict
    # guard, since data.get("cleanState", {}) only falls back on an absent
    # key. Not observed on the wire; held to the same standard OnStatsMower
    # holds itself to for a push no firmware is known to send.
    bus = _bus()
    record = register(bus)
    record.note_job({"type": "spotArea"})

    result = handle_clean_info(
        bus, {"trigger": "alert", "state": "idle", "cleanState": None}
    )

    assert result.state is HandlingState.SUCCESS
    assert record.job_type == "spotArea"


async def test_a_vacuum_without_a_record_still_parses() -> None:
    bus = _bus()
    published = _collect(bus, StateEvent)

    handle_clean_info(
        bus,
        {
            "state": "clean",
            "cleanState": {"motionState": "working", "content": {"type": "auto"}},
        },
    )
    await asyncio.sleep(0)

    assert [event.state for event in published] == [State.CLEANING]


# The charge pushes and the O1200's own state pushes around a departure, from
# the same two logs as the job edges above.
_UNPLUGGED = {"data": {"isCharging": 0, "mode": "slot"}}
_PLUGGED_IN = {"data": {"isCharging": 1, "mode": "slot"}}
_O1200_PUSH_0900 = {
    "data": {
        "trigger": "schedule",
        "other": "loading",
        "rotationPause": 0,
        "ssid": "0",
        "state": "clean",
        "cleanState": {
            "motionState": "working",
            "content": {"type": "auto", "subContent": {"type": "idle"}},
        },
    }
}
_O1200_PUSH_1147 = {
    "data": {
        "trigger": "continue",
        "other": "",
        "rotationPause": 0,
        "ssid": "1",
        "state": "clean",
        "cleanState": {
            "motionState": "working",
            "content": {"type": "auto", "subContent": {"type": "back"}},
        },
    }
}
_POS_AWAY_FROM_THE_DOCK = {
    "deebotPos": {"x": 5000, "y": 0, "a": 0, "invalid": 0},
    "chargePos": _CHARGER_UNKNOWN,
    "mid": "0",
}


class _Clock:
    """A monotonic clock the test steps by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    """Replace the clock the departure pairing reads (issue #120)."""
    stepped = _Clock()
    monkeypatch.setattr(messages_module, "monotonic", stepped)
    return stepped


def _push(bus: EventBus, message, body: dict[str, Any]) -> HandlingResult:
    """Deliver *body* the way the wire does, through ``handle()``."""
    return message.handle(bus, {"body": body})


def _last_state(bus: EventBus) -> State | None:
    event = bus.get_last_event(StateEvent)
    return None if event is None else event.state


def _docked_mower() -> tuple[EventBus, MowerStateRecord, list[StateEvent]]:
    """A registered mower on its charger, as the #120 run starts.

    Subscribed before the dock push, so the list holds every state notified
    from here on, the opening DOCKED included.
    """
    bus = _bus()
    record = register(bus)
    published = _collect(bus, StateEvent)
    _push(bus, OnChargeState, _PLUGGED_IN)
    return bus, record, published


def _states(published: list[StateEvent]) -> list[State]:
    return [event.state for event in published]


async def test_a_resume_from_the_dock_waits_for_the_charger(clock: _Clock) -> None:
    """Issue #120. An announcement alone is not proof that the mower left.

    A resume announced while the mower stays on its charger (a rain delay, a
    fault) would otherwise read mowing, and nothing would correct it: the
    poll publishes DOCKED only on isCharging 1, and the polled idle is
    discarded (#48).
    """
    bus, record, _ = _docked_mower()

    _push(bus, OnMowScheduleResume, _G1800_RESUME_1300)

    assert _last_state(bus) is State.DOCKED
    assert record.departure_edge_at == clock.now


async def test_the_unplug_after_a_resume_edge_writes_cleaning(clock: _Clock) -> None:
    """The #120 order: resume edge at 13:00:23.762, isCharging 0 at 13:00:26.501."""
    bus, record, published = _docked_mower()

    _push(bus, OnMowScheduleResume, _G1800_RESUME_1300)
    clock.now += 2.739
    _push(bus, OnChargeState, _UNPLUGGED)
    await asyncio.sleep(0)

    assert _states(published) == [State.DOCKED, State.CLEANING]
    assert record.docked is False
    assert record.departure_edge_at is None
    assert record.charge_left_at is None


async def test_an_unplug_before_the_resume_edge_pairs_too(clock: _Clock) -> None:
    """The O1200's charge-break order, without its own push: isCharging 0 at
    11:47:42.141, the resume edge at 11:47:50.636 (2026-10-12)."""
    bus, record, published = _docked_mower()

    _push(bus, OnChargeState, _UNPLUGGED)
    assert _last_state(bus) is State.DOCKED
    clock.now += 8.495
    _push(bus, OnMowScheduleResume, _O1200_RESUME_1147)
    await asyncio.sleep(0)

    assert _states(published) == [State.DOCKED, State.CLEANING]
    assert record.docked is False


@pytest.mark.parametrize("edge_first", [True, False])
async def test_neither_order_pairs_across_the_window(
    clock: _Clock, edge_first: bool
) -> None:
    bus, _, published = _docked_mower()
    steps = [
        (OnMowScheduleResume, _G1800_RESUME_1300),
        (OnChargeState, _UNPLUGGED),
    ]
    if not edge_first:
        steps.reverse()

    _push(bus, *steps[0])
    clock.now += DEPARTURE_WINDOW + 0.001
    _push(bus, *steps[1])
    await asyncio.sleep(0)

    assert _states(published) == [State.DOCKED]


async def test_charging_again_cancels_an_unplug(clock: _Clock) -> None:
    """dock() refutes the unplug: the mower is back on its charger."""
    bus, record, published = _docked_mower()

    _push(bus, OnChargeState, _UNPLUGGED)
    clock.now += 1
    _push(bus, OnChargeState, _PLUGGED_IN)
    clock.now += 1
    _push(bus, OnMowScheduleResume, _G1800_RESUME_1300)
    await asyncio.sleep(0)

    assert _states(published) == [State.DOCKED]
    assert record.charge_left_at is None


def _report_paused(bus: EventBus) -> None:
    handle_clean_info(
        bus,
        {"trigger": "none", "state": "clean", "cleanState": {"motionState": "pause"}},
    )


def _report_go_charging(bus: EventBus) -> None:
    OnChargeInfo._handle_body_data_dict(bus, {"trigger": "app", "state": "goCharging"})


def _report_alert(bus: EventBus) -> None:
    OnChargeInfo._handle_body_data_dict(bus, {"trigger": "alert", "state": "idle"})


@pytest.mark.parametrize(
    ("report", "reported"),
    [
        (_report_paused, State.PAUSED),
        (_report_go_charging, State.RETURNING),
        (_report_alert, State.ERROR),
    ],
    ids=["clean-info-paused", "charge-info-go-charging", "charge-info-alert"],
)
@pytest.mark.parametrize("edge_first", [True, False])
async def test_a_reported_state_between_the_two_halves_cancels_both(
    clock: _Clock, report, reported: State, edge_first: bool
) -> None:
    """A reported state is better evidence than either half, and a stale half
    must not re-assert CLEANING over a PAUSED pushed in between."""
    bus, record, published = _docked_mower()
    steps = [
        (OnMowScheduleResume, _G1800_RESUME_1300),
        (OnChargeState, _UNPLUGGED),
    ]
    if not edge_first:
        steps.reverse()

    _push(bus, *steps[0])
    # Off the charger as far as the record knows, as OnPos leaves it, so the
    # PAUSED is published rather than withheld (#67).
    record.move()
    report(bus)

    assert record.departure_edge_at is None
    assert record.charge_left_at is None

    clock.now += 3
    _push(bus, *steps[1])
    await asyncio.sleep(0)

    assert _states(published) == [State.DOCKED, reported]


@pytest.mark.parametrize("edge_first", [True, False])
async def test_an_error_on_the_charger_between_the_two_halves_stands(
    clock: _Clock, edge_first: bool
) -> None:
    """A fault before the mower leaves: onError, then the owner lifts it off.

    The library publishes that ERROR through ``GetError``, which knows nothing
    of the pair, and the record still says docked. CLEANING over it would
    release the fault latch (#53) for a mower that never left.

    The error cancels the first half as it arrives, so the second one is left
    waiting for a partner rather than completing a pair.
    """
    apply()
    bus, record, published = _docked_mower()
    steps = [
        (OnMowScheduleResume, _G1800_RESUME_1300),
        (OnChargeState, _UNPLUGGED),
    ]
    if not edge_first:
        steps.reverse()

    _push(bus, *steps[0])
    get_message("onError", _static_device_info()).handle(
        bus, {"body": {"data": {"code": [406]}}}
    )
    assert record.departure_edge_at is None
    assert record.charge_left_at is None
    clock.now += 3
    _push(bus, *steps[1])
    await asyncio.sleep(0)

    assert _states(published) == [State.DOCKED, State.ERROR]
    assert record.docked is True


async def test_a_polled_error_between_the_two_halves_stands(clock: _Clock) -> None:
    """A ``getError`` answer goes straight to the library's ``GetError``.

    It cancels nothing, so the pair still completes, and the ERROR gate in
    ``_confirm_departure`` is what keeps CLEANING off it.
    """
    from deebot_client.commands.json.error import GetError

    bus, record, published = _docked_mower()

    _push(bus, OnMowScheduleResume, _G1800_RESUME_1300)
    GetError._handle_body_data_dict(bus, {"code": [406]})
    assert record.departure_edge_at is not None
    clock.now += 3
    _push(bus, OnChargeState, _UNPLUGGED)
    await asyncio.sleep(0)

    assert _states(published) == [State.DOCKED, State.ERROR]
    assert record.departure_edge_at is None
    assert record.charge_left_at is None


async def test_an_error_cancels_the_announcement_before_the_dock_hides_it(
    clock: _Clock,
) -> None:
    """Resume, onError, isCharging 1, isCharging 0, all inside the window.

    The charging confirmation replaces the ERROR with DOCKED and keeps the
    announcement (``dock()`` only refutes an unplug), so by the time the unplug
    arrives nothing in the last state says a fault came in between. The error
    has to settle the pair when it arrives.
    """
    apply()
    bus, record, published = _docked_mower()

    _push(bus, OnMowScheduleResume, _G1800_RESUME_1300)
    get_message("onError", _static_device_info()).handle(
        bus, {"body": {"data": {"code": [406]}}}
    )
    assert record.departure_edge_at is None
    clock.now += 1
    _push(bus, OnChargeState, _PLUGGED_IN)
    clock.now += 1
    _push(bus, OnChargeState, _UNPLUGGED)
    await asyncio.sleep(0)

    assert State.CLEANING not in _states(published)


async def test_an_error_free_heartbeat_keeps_the_announcement(clock: _Clock) -> None:
    """``code: [0]`` arrives constantly on 1.36.208 and reports no fault."""
    apply()
    bus, record, published = _docked_mower()

    _push(bus, OnMowScheduleResume, _G1800_RESUME_1300)
    get_message("onError", _static_device_info()).handle(
        bus, {"body": {"data": {"code": [0]}}}
    )
    clock.now += 2.739
    _push(bus, OnChargeState, _UNPLUGGED)
    await asyncio.sleep(0)

    assert _states(published) == [State.DOCKED, State.CLEANING]


async def test_the_apps_end_on_the_dock_cancels_a_waiting_departure(
    clock: _Clock,
) -> None:
    """A pushed idle without cleanState is the app's End: the job is over.

    On the dock that idle is withheld (#67), but the job it ends must not
    leave its start edge waiting for an unplug to pair with.
    """
    bus, record, published = _docked_mower()

    _push(
        bus, OnMowScheduleStart, {"mapid": "0809159361", "triggerType": "schedule"}
    )
    handle_clean_info(bus, {"trigger": "app", "state": "idle"})
    assert record.departure_edge_at is None
    clock.now += 2
    _push(bus, OnChargeState, _UNPLUGGED)
    await asyncio.sleep(0)

    assert _states(published) == [State.DOCKED]


async def test_a_withheld_paused_plan_does_not_cancel_the_departure(
    clock: _Clock,
) -> None:
    """A paused-plan push on the dock (#67) says the mower is still docked,
    which is what the pair is waiting to see resolved, not evidence against it."""
    bus, record, published = _docked_mower()

    _push(bus, OnMowScheduleResume, _G1800_RESUME_1300)
    _report_paused(bus)
    assert record.suppressed is State.PAUSED
    clock.now += 2.739
    _push(bus, OnChargeState, _UNPLUGGED)
    await asyncio.sleep(0)

    assert _states(published) == [State.DOCKED, State.CLEANING]
    # move() forgets the withheld plan: the mower is out, and a stale PAUSED
    # would let the mow command resume a plan that has moved on.
    assert record.suppressed is None


@pytest.mark.parametrize(
    ("message", "body"),
    [
        (OnMowSchedulePause, _G1800_PAUSE_1330),
        (OnMowScheduleStop, _G1800_STOP_1501),
        (OnMowSchedulePause, {"mapid": "0809159361"}),
    ],
    ids=["pause", "stop", "pause-without-a-trigger"],
)
async def test_a_pause_or_stop_edge_cancels_a_pending_departure(
    clock: _Clock, message, body: dict[str, Any]
) -> None:
    """The job paused or ended before the mower left, so a later unplug is not
    that job leaving. The cancel runs before the trigger parsing, so an edge
    the parsing gives up on still cancels."""
    bus, _, published = _docked_mower()

    _push(bus, OnMowScheduleResume, _G1800_RESUME_1300)
    _push(bus, message, body)
    clock.now += 2.739
    _push(bus, OnChargeState, _UNPLUGGED)
    await asyncio.sleep(0)

    assert _states(published) == [State.DOCKED]


def _answer_charging(bus: EventBus) -> None:
    GetChargeStateMower._handle_body(bus, {"code": 0, "data": {"isCharging": 1}})


def _push_charging(bus: EventBus) -> None:
    _push(bus, OnChargeState, _PLUGGED_IN)


def _report_docked(bus: EventBus) -> None:
    OnChargeInfo._handle_body_data_dict(bus, {"trigger": "workComplete", "state": "idle"})


@pytest.mark.parametrize(
    "report",
    [_answer_charging, _push_charging, _report_docked],
    ids=["get-charge-state-answer", "charge-state-push", "charge-info-idle"],
)
async def test_a_repeat_dock_report_does_not_stop_the_pair(
    clock: _Clock, report
) -> None:
    """Edge first, as at 13:00:23 -> 13:00:26. A dock already known is
    confirmed again by update_entity, by the refreshes and by repeated
    pushes; being on the charger one second after an announcement does not
    refute a departure three seconds away."""
    bus, _, published = _docked_mower()

    _push(bus, OnMowScheduleResume, _G1800_RESUME_1300)
    clock.now += 1
    report(bus)
    clock.now += 1.739
    _push(bus, OnChargeState, _UNPLUGGED)
    await asyncio.sleep(0)

    assert _states(published) == [State.DOCKED, State.CLEANING]


async def test_a_position_sample_between_the_two_halves_does_not_stop_the_pair(
    clock: _Clock,
) -> None:
    """OnPos calls move() for a sample away from the dock and publishes no
    state; a getPos from the map image's update_entity (#115) can land
    between the two halves."""
    bus, record, published = _docked_mower()

    _push(bus, OnMowScheduleResume, _G1800_RESUME_1300)
    OnPos._handle_body_data_dict(bus, _POS_AWAY_FROM_THE_DOCK)
    assert record.docked is False
    clock.now += 2.739
    _push(bus, OnChargeState, _UNPLUGGED)
    await asyncio.sleep(0)

    assert _states(published) == [State.DOCKED, State.CLEANING]


async def test_a_get_charge_state_answer_never_completes_a_pair(clock: _Clock) -> None:
    """Only the push counts. An answer is a snapshot, asked while the mower is
    docked, and pairing it would write CLEANING for a mower that never left."""
    bus, record, published = _docked_mower()

    _push(bus, OnMowScheduleResume, _G1800_RESUME_1300)
    clock.now += 2.739
    GetChargeStateMower._handle_body(bus, {"code": 0, "data": {"isCharging": 0}})
    await asyncio.sleep(0)

    assert _states(published) == [State.DOCKED]
    assert record.charge_left_at is None


async def test_an_edge_arms_after_a_restart_with_no_state_yet(clock: _Clock) -> None:
    """After a restart the record starts undocked, and a mower that has said
    nothing since must still be able to leave."""
    bus = _bus()
    register(bus)
    published = _collect(bus, StateEvent)

    _push(bus, OnMowScheduleResume, _G1800_RESUME_1300)
    clock.now += 2.739
    _push(bus, OnChargeState, _UNPLUGGED)
    await asyncio.sleep(0)

    assert _states(published) == [State.CLEANING]


async def test_an_edge_arms_when_only_the_record_has_left_the_dock(
    clock: _Clock,
) -> None:
    """OnPos can clear record.docked without publishing, leaving DOCKED as the
    bus's last state."""
    bus, record, published = _docked_mower()
    record.move()

    _push(bus, OnMowScheduleResume, _G1800_RESUME_1300)
    clock.now += 2.739
    _push(bus, OnChargeState, _UNPLUGGED)
    await asyncio.sleep(0)

    assert _states(published) == [State.DOCKED, State.CLEANING]


@pytest.mark.parametrize("pushed", [State.CLEANING, State.PAUSED, State.RETURNING])
async def test_an_edge_after_a_pushed_state_away_from_the_dock_does_not_arm(
    clock: _Clock, pushed: State
) -> None:
    """The O1200's start edge lands 13 s after its own CLEANING push, and a
    pause or a rain return in those 13 s must not be overwritten. The same
    rule keeps a repeated reborn start out on the lawn from leaving a half
    armed for a later unplug."""
    bus = _bus()
    record = register(bus)
    bus.notify(StateEvent(pushed))

    _push(bus, OnMowScheduleResume, _G1800_RESUME_1300)
    assert record.departure_edge_at is None
    clock.now += 2.739
    _push(bus, OnChargeState, _UNPLUGGED)

    assert _last_state(bus) is pushed


@pytest.mark.parametrize(
    "body",
    [{"msg": "fail", "code": "30007"}, _PLUGGED_IN],
    ids=["fail-shaped", "charging"],
)
async def test_a_charge_push_other_than_an_unplug_stamps_nothing(
    clock: _Clock, body: dict[str, Any]
) -> None:
    """Checked through the pair, not the stamp: both bodies reach dock(),
    which clears charge_left_at anyway, so only a missing CLEANING proves
    nothing was stamped."""
    bus, record, published = _docked_mower()

    _push(bus, OnMowScheduleResume, _G1800_RESUME_1300)
    clock.now += 1
    _push(bus, OnChargeState, body)
    await asyncio.sleep(0)

    assert _states(published) == [State.DOCKED]
    assert record.departure_edge_at is not None


async def test_a_start_edge_without_a_trigger_still_counts(clock: _Clock) -> None:
    """The departure is settled before the trigger parsing gives up."""
    bus, _, published = _docked_mower()

    result = _push(bus, OnMowScheduleStart, {"mapId": "511702305"})
    clock.now += 7
    _push(bus, OnChargeState, _UNPLUGGED)
    await asyncio.sleep(0)

    # Through handle(), the library's _handle_error_or_analyse wrapper turns
    # the handler's ANALYSE into ANALYSE_LOGGED on the way out.
    assert result.state is HandlingState.ANALYSE_LOGGED
    assert _states(published) == [State.DOCKED, State.CLEANING]


async def test_an_unregistered_bus_gets_no_state_from_a_departure(
    clock: _Clock,
) -> None:
    """A Deebot vacuum on the same account. MESSAGES is global, so these
    handlers see its pushes too, and must publish nothing of their own."""
    bus = _bus()
    published = _collect(bus, StateEvent)

    _push(bus, OnMowScheduleResume, _G1800_RESUME_1300)
    clock.now += 2.739
    result = _push(bus, OnChargeState, _UNPLUGGED)
    await asyncio.sleep(0)

    assert published == []
    assert result.state is HandlingState.SUCCESS


async def test_pause_and_stop_edges_publish_no_state() -> None:
    """What follows them says what the mower does: return-normal-start, then
    isCharging 1. A PAUSED of their own would flash for 0.35 s."""
    bus = _bus()
    register(bus)
    bus.notify(StateEvent(State.CLEANING))

    _push(bus, OnMowSchedulePause, _G1800_PAUSE_1330)
    _push(bus, OnMowScheduleStop, _G1800_STOP_1501)

    assert _last_state(bus) is State.CLEANING


@pytest.mark.parametrize(
    ("push", "steps"),
    [
        (
            _O1200_PUSH_0900,
            [(OnMowScheduleStart, _O1200_START_0901), (OnChargeState, _UNPLUGGED)],
        ),
        (
            _O1200_PUSH_1147,
            [(OnChargeState, _UNPLUGGED), (OnMowScheduleResume, _O1200_RESUME_1147)],
        ),
    ],
    ids=["scheduled-start", "charge-break-resume"],
)
async def test_an_o1200_departure_notifies_cleaning_once(
    clock: _Clock, push: dict[str, Any], steps: list
) -> None:
    """The O1200 pushes its own CLEANING before both halves, so the pair
    writes nothing there (2026-10-12: 09:00:53, 11:47:35)."""
    bus, record, published = _docked_mower()

    _push(bus, OnScheduleTaskInfo, push)
    for message, body in steps:
        clock.now += 7
        _push(bus, message, body)
    await asyncio.sleep(0)

    assert _states(published) == [State.DOCKED, State.CLEANING]
    # The bus drops an equal CLEANING, so the list above would also pass if the
    # pair re-asserted it. The guard is the push keeping the edge from arming:
    # the unplug is left waiting for a partner instead of completing a pair.
    assert record.departure_edge_at is None
    assert record.charge_left_at is not None


async def test_a_repeated_unplug_after_a_completed_pair_changes_nothing(
    clock: _Clock,
) -> None:
    """An MQTT redelivery of the unplug must not notify a second CLEANING."""
    bus, record, published = _docked_mower()

    _push(bus, OnMowScheduleResume, _G1800_RESUME_1300)
    clock.now += 2.739
    _push(bus, OnChargeState, _UNPLUGGED)
    clock.now += 0.1
    _push(bus, OnChargeState, _UNPLUGGED)
    await asyncio.sleep(0)

    assert _states(published) == [State.DOCKED, State.CLEANING]
    assert record.docked is False


_G1800_RETURN_1330 = {
    "bid": "0031791804622040",
    "gid": "G1791648417330",
    "index": "0000000457",
    "mapid": "0809159361",
    "returnId": "1791804622039907",
    "sid": "2531791804622040",
    "triggerType": "schedule",
    "ts": "1791804622040",
    "workstate": 1,
}
_G1800_RETURN_1501 = {
    "bid": "4931791810117951",
    "gid": "G1791648417330",
    "index": "0000000526",
    "mapid": "0809159361",
    "mowId": "",
    "returnId": "1791810117949218",
    "sid": "9401791810117951",
    "triggerType": "schedule",
    "ts": "1791810117951",
    "workstate": 1,
}


def test_the_trip_home_is_registered() -> None:
    """Logged as 'Unknown message' until issue #120."""
    apply()

    assert MESSAGES["onFwBuryPoint-bd_task-return-normal-start"] is OnReturnNormalStart


async def test_the_trip_home_writes_returning() -> None:
    """On the G1-800 the bury point is the only word that it is driving home."""
    bus = _bus()
    record = register(bus)
    bus.notify(StateEvent(State.CLEANING))

    result = _push(bus, OnReturnNormalStart, _G1800_RETURN_1330)

    assert result.state is HandlingState.SUCCESS
    assert _last_state(bus) is State.RETURNING
    assert record.docked is False


async def test_a_late_trip_home_after_docking_writes_nothing() -> None:
    """It must not flip DOCKED back to RETURNING, or clear the paused plan
    #67 and the mow command rely on."""
    bus, record, published = _docked_mower()
    record.suppressed = State.PAUSED

    result = _push(bus, OnReturnNormalStart, _G1800_RETURN_1330)
    await asyncio.sleep(0)

    assert result.state is HandlingState.SUCCESS
    assert _states(published) == [State.DOCKED]
    assert record.docked is True
    assert record.suppressed is State.PAUSED


async def test_a_trip_home_after_a_pushed_error_keeps_the_error() -> None:
    """A mower stuck on its way home: goCharging, then an error, then the late
    bury point. The error stands until the mower reports something else."""
    bus = _bus()
    register(bus)
    _report_go_charging(bus)
    OnChargeInfo._handle_body_data_dict(bus, {"trigger": "alert", "state": "goCharging"})

    _push(bus, OnReturnNormalStart, _G1800_RETURN_1330)

    assert _last_state(bus) is State.ERROR


async def test_an_unregistered_bus_gets_no_state_from_the_trip_home() -> None:
    bus = _bus()
    published = _collect(bus, StateEvent)

    result = _push(bus, OnReturnNormalStart, _G1800_RETURN_1330)
    await asyncio.sleep(0)

    assert result.state is HandlingState.SUCCESS
    assert published == []


async def test_the_trip_home_is_not_a_job_edge() -> None:
    """A return is not a job boundary, and the progress sensor must not see one."""
    bus = _bus()
    register(bus)
    bus.notify(StateEvent(State.CLEANING))
    edges = _collect(bus, MowerJobEdgeEvent)

    _push(bus, OnReturnNormalStart, _G1800_RETURN_1330)
    await asyncio.sleep(0)

    assert edges == []


async def test_the_trip_home_cancels_a_pending_departure(clock: _Clock) -> None:
    bus, record, published = _docked_mower()

    _push(bus, OnMowScheduleResume, _G1800_RESUME_1300)
    record.move()  # as OnPos leaves it, so the trip home is published
    _push(bus, OnReturnNormalStart, _G1800_RETURN_1330)

    assert record.departure_edge_at is None
    assert record.charge_left_at is None

    clock.now += 3
    _push(bus, OnChargeState, _UNPLUGGED)
    await asyncio.sleep(0)

    assert _states(published) == [State.DOCKED, State.RETURNING]


async def test_the_issue_120_scheduled_run_replays_to_mowing_returning_and_docked(
    clock: _Clock,
) -> None:
    """The reporter's two schedule windows (13:00-13:30, 14:00-15:01) through
    the real handlers, at the logged intervals. Before #120 the state read
    docked throughout."""
    bus, _, published = _docked_mower()

    for step, (seconds, message, body, expected) in enumerate(
        [
            (0.0, OnMowScheduleResume, _G1800_RESUME_1300, State.DOCKED),
            (2.739, OnChargeState, _UNPLUGGED, State.CLEANING),
            (1796.951, OnMowSchedulePause, _G1800_PAUSE_1330, State.CLEANING),
            (0.349, OnReturnNormalStart, _G1800_RETURN_1330, State.RETURNING),
            (72.049, OnChargeState, _PLUGGED_IN, State.DOCKED),
            (1727.974, OnMowScheduleResume, _G1800_RESUME_1400, State.DOCKED),
            (3.198, OnChargeState, _UNPLUGGED, State.CLEANING),
            (3692.673, OnMowScheduleStop, _G1800_STOP_1501, State.CLEANING),
            (0.107, OnReturnNormalStart, _G1800_RETURN_1501, State.RETURNING),
        ]
    ):
        clock.now += seconds
        _push(bus, message, body)
        assert _last_state(bus) is expected, (step, message.NAME)
    await asyncio.sleep(0)

    assert _states(published) == [
        State.DOCKED,
        State.CLEANING,
        State.RETURNING,
        State.DOCKED,
        State.CLEANING,
        State.RETURNING,
    ]


# The bury point as issue #76 captured it on a GOAT G1-800 (77atlz): the
# fields sit directly under body, there is no ``data`` wrapper, and the header
# has no ``wkVer``. The ids are placeholders.
_CORE_INFO = {"coreTemp": 36, "gid": "G0000000000000", "index": "0000000001", "ts": "0"}


def _core_header() -> dict[str, Any]:
    return {
        "pri": 1,
        "tzm": 60,
        "ts": "0",
        "ver": "0.0.1",
        "fwVer": "1.36.208",
        "hwVer": "0.1.1",
    }


def _core_temperatures(body: Any) -> list[MowerCoreTemperatureEvent]:
    """Deliver *body* through ``handle()`` and return the readings notified."""
    bus = Mock()
    OnCoreInfo.handle(bus, {"header": _core_header(), "body": body})
    return [
        call_.args[0]
        for call_ in bus.notify.call_args_list
        if isinstance(call_.args[0], MowerCoreTemperatureEvent)
    ]


def test_on_core_info_publishes_an_integer_reading() -> None:
    assert _core_temperatures(_CORE_INFO) == [MowerCoreTemperatureEvent(36)]


def test_on_core_info_publishes_a_float_reading() -> None:
    assert _core_temperatures({**_CORE_INFO, "coreTemp": 36.5}) == [
        MowerCoreTemperatureEvent(36.5)
    ]


def test_on_core_info_accepts_zero_and_negative_readings() -> None:
    # Falsy and below zero are still readings; a truthiness check would drop 0.
    assert _core_temperatures({"coreTemp": 0}) == [MowerCoreTemperatureEvent(0)]
    assert _core_temperatures({"coreTemp": -3}) == [MowerCoreTemperatureEvent(-3)]


def test_on_core_info_reports_success() -> None:
    assert OnCoreInfo._handle_body(Mock(), _CORE_INFO).state is HandlingState.SUCCESS


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"gid": "G0000000000000", "index": "0000000001", "ts": "0"},
        {"coreTemp": None},
    ],
    ids=["empty", "no-core-temp", "null"],
)
def test_on_core_info_without_a_reading_publishes_nothing(body) -> None:
    assert _core_temperatures(body) == []
    assert OnCoreInfo._handle_body(Mock(), body).state is HandlingState.ANALYSE


@pytest.mark.parametrize(
    "value",
    [True, False, "36", "", [36], {"v": 36}],
    ids=["true", "false", "str", "empty-str", "list", "dict"],
)
def test_on_core_info_rejects_a_value_that_is_not_a_real_number(value) -> None:
    # bool is an int in Python, so True would otherwise read as one degree.
    assert _core_temperatures({"coreTemp": value}) == []


def test_on_core_info_does_not_raise_on_an_int_too_large_for_a_float() -> None:
    # math.isfinite() raises OverflowError on such an int; the handler must not
    # apply it to ints.
    assert OnCoreInfo._handle_body(Mock(), {"coreTemp": 10**400}).state in (
        HandlingState.SUCCESS,
        HandlingState.ANALYSE,
    )


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf")],
    ids=["nan", "inf", "-inf"],
)
def test_on_core_info_rejects_a_non_finite_float(value) -> None:
    assert _core_temperatures({"coreTemp": value}) == []


@pytest.mark.parametrize(
    "body",
    [
        # The bd_sysinfo shape on a G1-800: a bare list of samples.
        [{"isvalid": 1, "mapid": "0", "uptime": " 18:45:55 up 4 days"}],
        [],
        "coreTemp",
        None,
        36,
    ],
    ids=["sysinfo-list", "empty-list", "str", "none", "int"],
)
def test_on_core_info_ignores_a_body_that_is_not_an_object(body) -> None:
    bus = Mock()
    result = OnCoreInfo.handle(bus, {"header": _core_header(), "body": body})

    assert result.state is not HandlingState.ERROR
    assert _core_temperatures(body) == []


def test_on_core_info_survives_a_header_without_wk_ver() -> None:
    # Bury-point headers carry no wkVer; the handler must not need one.
    bus = Mock()
    OnCoreInfo.handle(bus, {"header": {"ts": "0"}, "body": _CORE_INFO})
    OnCoreInfo.handle(bus, {"body": _CORE_INFO})

    readings = [
        call_.args[0]
        for call_ in bus.notify.call_args_list
        if isinstance(call_.args[0], MowerCoreTemperatureEvent)
    ]
    assert readings == [MowerCoreTemperatureEvent(36)] * 2


async def test_on_core_info_repeats_are_harmless_and_the_bus_drops_them() -> None:
    # The handler notifies every reading it understands; it is the bus that
    # drops an event equal to the last one. A subscriber therefore sees each
    # change once, which is right for a value that is held.
    bus = _bus()
    received = _collect(bus, MowerCoreTemperatureEvent)

    for temperature in (36, 36, 37, 36):
        result = OnCoreInfo.handle(bus, {"body": {"coreTemp": temperature}})
        assert result.state is HandlingState.SUCCESS
        await asyncio.sleep(0)

    assert [event.celsius for event in received] == [36, 37, 36]
    assert bus.get_last_event(MowerCoreTemperatureEvent).celsius == 36


def test_on_core_info_message_name() -> None:
    assert OnCoreInfo.NAME == "onFwBuryPoint-bd_coreinfo"


def test_on_core_info_is_registered_by_apply() -> None:
    # Without it the bury point is logged as unknown and the sensor never
    # leaves unknown (issue #76).
    apply()
    assert MESSAGES["onFwBuryPoint-bd_coreinfo"] is OnCoreInfo
