"""Tests for the mower-adapted commands."""

import asyncio
import logging
from unittest.mock import AsyncMock, Mock, call, patch

import pytest
from deebot_client.command import Command
from deebot_client.commands.json.clean import Clean, CleanV2, GetCleanInfo
from deebot_client.commands.json.life_span import GetLifeSpan
from deebot_client.commands.json.stats import GetStats
from deebot_client.event_bus import EventBus
from deebot_client.events import LifeSpan, LifeSpanEvent, StateEvent, StatsEvent
from deebot_client.events.base import Event
from deebot_client.message import HandlingResult, HandlingState
from deebot_client.models import CleanAction, State

from custom_components.ecovacs_mower.deebot_patch.commands import (
    CleanMower,
    GetChargeStateMower,
    GetCleanInfoMower,
    GetLifeSpanMower,
    GetProtectState,
    GetRainDelay,
    GetStatsMower,
    MowerStateRefresh,
    SetRainDelay,
    _CleanNonV2,
    _CleanV2Mower,
    _GetCleanInfoNonV2,
    _GetCleanInfoV2,
)
from custom_components.ecovacs_mower.deebot_patch.families import (
    Family,
    commit,
    selected,
)
from custom_components.ecovacs_mower.deebot_patch.messages import (
    MowerBeacon,
    MowerBeaconsEvent,
    MowerProtectStateEvent,
    MowerRainDelayEvent,
    MowerStatsEvent,
    OnProtectState,
    OnRainDelay,
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


_DEVICE_INFO = {
    "did": "test-did",
    "class": "2i0fns",
    "resource": "test-resource",
    "company": "eco-ng",
    "name": "test-name",
}


def test_publishes_to_clean_topic_not_clean_v2() -> None:
    # The core of the bug: GOAT listens on "clean", not "clean_V2".
    assert CleanMower.NAME == "clean"
    assert CleanV2.NAME == "clean_V2"


def test_is_a_clean_command() -> None:
    assert issubclass(CleanMower, Clean)


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        (CleanAction.START, {"act": "start", "content": {"type": "auto"}}),
        (CleanAction.PAUSE, {"act": "pause", "content": {"type": "auto"}}),
        (CleanAction.RESUME, {"act": "resume", "content": {"type": "auto"}}),
        (CleanAction.STOP, {"act": "stop", "content": {"type": "auto"}}),
    ],
)
def test_the_non_v2_delegate_keeps_the_mower_payload_shape(
    action: CleanAction, expected: dict[str, object]
) -> None:
    # Unlike CleanV2, which sends an empty type on pause, we echo "auto" — that
    # is what the app does against a lawn mower.
    assert _CleanNonV2(action)._args == expected


def test_the_v2_delegate_is_the_librarys_own_shape() -> None:
    # Left exactly as the library builds it: the resume payload the reporter
    # captured being acked in 526 ms on firmware 1.36.208 is
    # {"act": "resume", "content": {}}, which is CleanV2's shape, not ours.
    assert _CleanV2Mower(CleanAction.RESUME)._args == {"act": "resume", "content": {}}
    assert _CleanV2Mower(CleanAction.START)._args == {
        "act": "start",
        "content": {"type": "auto"},
    }


def test_get_protect_state_asks_on_the_command_name_not_the_message_name() -> None:
    # Issue #31. onProtectState is what the device pushes; getProtectState is
    # what asks for it. Ecovacs' own app sends the latter at startup.
    assert GetProtectState.NAME == "getProtectState"
    assert OnProtectState.NAME == "onProtectState"


def test_get_protect_state_takes_no_arguments() -> None:
    assert GetProtectState()._args == {}


def test_get_protect_state_parses_the_answer_with_the_message_handler() -> None:
    # The whole point of inheriting OnProtectState: one parser, two entry
    # points. A copy would drift the day the payload gains a flag. Compared as
    # functions — a classmethod accessed on two classes gives two distinct bound
    # objects even when it is the same code.
    assert (
        GetProtectState._handle_body_data_dict.__func__
        is OnProtectState._handle_body_data_dict.__func__
    )


def test_get_protect_state_notifies_the_protection_flags() -> None:
    # The behaviour that matters: an answer to getProtectState reaches the
    # entities the same way a push does.
    event_bus = Mock()
    GetProtectState._handle_body_data_dict(
        event_bus,
        {
            "isAnimProtect": 0,
            "isRainProtect": 1,
            "isRainDelay": 1,
            "isEStop": 0,
            "isLocked": 0,
        },
    )
    assert event_bus.notify.call_args_list == [
        call(
            MowerProtectStateEvent(
                rain_protect=True,
                rain_delay=True,
                emergency_stop=False,
                locked=False,
                animal_protect=False,
            )
        )
    ]


# The payload is a real answer to getStats, captured while a GOAT O1200 was
# mowing: area is the job's target, mowedArea the part already cut.
_MOWING = {"area": 211275, "time": 704, "mowedArea": 87825}


def test_get_stats_mower_asks_the_same_command_as_the_library() -> None:
    # Issue #39. Only the parsing is replaced. If the name diverged this would
    # be a second request for the same numbers.
    assert GetStatsMower.NAME == GetStats.NAME == "getStats"


def test_get_stats_mower_keeps_the_field_the_library_drops() -> None:
    event_bus = Mock()
    GetStatsMower._handle_body_data_dict(event_bus, _MOWING)
    assert (
        call(MowerStatsEvent(area=211275, mowed_area=87825))
        in event_bus.notify.call_args_list
    )


def test_get_stats_mower_still_notifies_the_librarys_own_event() -> None:
    # The area and time sensors subscribe to StatsEvent and must not notice
    # that the command behind them was swapped.
    event_bus = Mock()
    GetStatsMower._handle_body_data_dict(event_bus, _MOWING)
    assert (
        call(StatsEvent(area=211275, time=704, type=None))
        in event_bus.notify.call_args_list
    )


def test_get_stats_mower_reports_a_missing_field_as_none() -> None:
    # A firmware that does not send mowedArea must still yield area and time.
    # The progress entity reads "unknown" rather than the whole answer failing.
    event_bus = Mock()
    GetStatsMower._handle_body_data_dict(event_bus, {"area": 0, "time": 0})
    assert (
        call(MowerStatsEvent(area=0, mowed_area=None))
        in event_bus.notify.call_args_list
    )


def test_the_mower_variant_asks_the_same_command() -> None:
    # Issue #48. Only one answer is ignored; the request is unchanged, and the
    # V2 name is still the one GOAT does not answer at all.
    assert GetCleanInfoMower.NAME == GetCleanInfo.NAME == "getCleanInfo"


def test_a_polled_idle_notifies_nothing() -> None:
    """The answer that made the poll overwrite the truth every five minutes.

    Every one of the 74 polls during the run on 2026-08-24 answered exactly
    this, while the mower was cutting.
    """
    event_bus = Mock()
    result = _GetCleanInfoNonV2._handle_body_data_dict(event_bus, {"state": "idle"})

    event_bus.notify.assert_not_called()
    # success(), not analyse(): the payload parsed, there was nothing to say.
    assert result.state is HandlingState.SUCCESS


def test_the_librarys_own_command_still_believes_an_idle() -> None:
    """Documents both the bug and the fix's blast radius.

    An idle *push* keeps its meaning: onCleanInfo resolves to the library's own
    GetCleanInfo through get_legacy_message(), which the patch does not touch.
    """
    event_bus = Mock()
    GetCleanInfo._handle_body_data_dict(event_bus, {"state": "idle"})

    assert event_bus.notify.call_args_list == [call(StateEvent(State.IDLE))]


def test_a_working_answer_is_still_believed() -> None:
    event_bus = Mock()
    _GetCleanInfoNonV2._handle_body_data_dict(
        event_bus, {"state": "clean", "cleanState": {"motionState": "working"}}
    )

    assert event_bus.notify.call_args_list == [call(StateEvent(State.CLEANING))]


def test_a_go_charging_answer_is_still_believed() -> None:
    event_bus = Mock()
    _GetCleanInfoNonV2._handle_body_data_dict(event_bus, {"state": "goCharging"})

    assert event_bus.notify.call_args_list == [call(StateEvent(State.RETURNING))]


def test_an_alert_wins_over_the_dropped_idle() -> None:
    """An error is not something to swallow because idle came along with it."""
    event_bus = Mock()
    _GetCleanInfoNonV2._handle_body_data_dict(
        event_bus, {"state": "idle", "trigger": "alert"}
    )

    assert event_bus.notify.call_args_list == [call(StateEvent(State.ERROR))]


# A real answer to getLifeSpan from a GOAT G1-800 with four UWB beacons, one of
# them flat, captured while the app was showing the low-beacon banner (issue
# #40). The serials are placeholders; the reporter redacted the real ones. The
# order is the device's own, and it is what makes the abort visible: lensBrush
# comes after the beacons.
_LIFE_SPANS_WITH_BEACONS = [
    {"type": "blade", "left": 2473, "total": 4800},
    {"type": "uwbCell", "sn": "BEACON-1", "left": 0, "total": 100},
    {"type": "uwbCell", "sn": "BEACON-2", "left": 83, "total": 100},
    {"type": "uwbCell", "sn": "BEACON-3", "left": 68, "total": 100},
    {"type": "uwbCell", "sn": "BEACON-4", "left": 73, "total": 100},
    {"type": "lensBrush", "left": 1000, "total": 1000},
]


def test_get_life_span_mower_asks_the_same_command_as_the_library() -> None:
    # Issue #40. The device answers with every component it has whatever the
    # request lists, so only the parsing is replaced.
    assert GetLifeSpanMower.NAME == GetLifeSpan.NAME == "getLifeSpan"


def test_the_librarys_own_command_aborts_on_the_first_beacon() -> None:
    """Documents the bug and its blast radius.

    LifeSpan is a StrEnum without a _missing_ hook, so LifeSpan("uwbCell")
    raises. Message.handle catches it and logs "Could not parse getLifeSpan",
    but the components before the raise have already been notified and the ones
    after it never are: blade gets through, lensBrush does not. That is why the
    lens brush entity on a beacon-guided mower reads a value that never moves.
    """
    event_bus = Mock()
    with pytest.raises(ValueError, match="uwbCell"):
        GetLifeSpan._handle_body_data_list(event_bus, _LIFE_SPANS_WITH_BEACONS)

    assert event_bus.notify.call_args_list == [
        call(LifeSpanEvent(LifeSpan.BLADE, 51.52, 2473))
    ]


def test_get_life_span_mower_publishes_the_beacons_the_enum_has_no_member_for() -> None:
    event_bus = Mock()
    GetLifeSpanMower._handle_body_data_list(event_bus, _LIFE_SPANS_WITH_BEACONS)

    assert (
        call(
            MowerBeaconsEvent(
                beacons=(
                    MowerBeacon(sn="BEACON-1", percent=0.0),
                    MowerBeacon(sn="BEACON-2", percent=83.0),
                    MowerBeacon(sn="BEACON-3", percent=68.0),
                    MowerBeacon(sn="BEACON-4", percent=73.0),
                )
            )
        )
        in event_bus.notify.call_args_list
    )


def test_get_life_span_mower_still_publishes_the_component_after_the_beacons() -> None:
    # The half of the fix that is not about beacons at all: with the abort gone,
    # lensBrush is reported for the first time on this hardware.
    event_bus = Mock()
    GetLifeSpanMower._handle_body_data_list(event_bus, _LIFE_SPANS_WITH_BEACONS)

    assert (
        call(LifeSpanEvent(LifeSpan.LENS_BRUSH, 100.0, 1000))
        in event_bus.notify.call_args_list
    )


def test_get_life_span_mower_still_publishes_the_component_before_them() -> None:
    event_bus = Mock()
    GetLifeSpanMower._handle_body_data_list(event_bus, _LIFE_SPANS_WITH_BEACONS)

    assert (
        call(LifeSpanEvent(LifeSpan.BLADE, 51.52, 2473))
        in event_bus.notify.call_args_list
    )


def test_get_life_span_mower_says_nothing_about_beacons_when_there_are_none() -> None:
    # The O1200 navigates without beacons. An empty set would be a claim that it
    # has none left, not that it has none.
    event_bus = Mock()
    GetLifeSpanMower._handle_body_data_list(
        event_bus, [{"type": "blade", "left": 2473, "total": 4800}]
    )

    assert not any(
        isinstance(notified.args[0], MowerBeaconsEvent)
        for notified in event_bus.notify.call_args_list
    )


def test_get_life_span_mower_drops_a_beacon_it_cannot_divide() -> None:
    # A zero total is a ZeroDivisionError. Losing the one entry is the point of
    # this class: the rest of the answer still arrives.
    event_bus = Mock()
    GetLifeSpanMower._handle_body_data_list(
        event_bus,
        [
            {"type": "uwbCell", "sn": "BEACON-1", "left": 0, "total": 0},
            {"type": "uwbCell", "sn": "BEACON-2", "left": 50, "total": 100},
        ],
    )

    assert (
        call(MowerBeaconsEvent(beacons=(MowerBeacon(sn="BEACON-2", percent=50.0),)))
        in event_bus.notify.call_args_list
    )


def test_get_life_span_mower_drops_a_beacon_with_no_serial() -> None:
    # The serial is what tells four beacons apart. An entry without one cannot
    # be attributed to a beacon and must not be attributed to the wrong one.
    event_bus = Mock()
    GetLifeSpanMower._handle_body_data_list(
        event_bus,
        [
            {"type": "uwbCell", "left": 20, "total": 100},
            {"type": "uwbCell", "sn": "BEACON-2", "left": 50, "total": 100},
        ],
    )

    assert (
        call(MowerBeaconsEvent(beacons=(MowerBeacon(sn="BEACON-2", percent=50.0),)))
        in event_bus.notify.call_args_list
    )


def test_get_life_span_mower_drops_a_repeated_serial() -> None:
    # A repeated sn would otherwise reach the sensor platform as two entities
    # sharing one unique_id. Keep the first reading, drop the rest.
    event_bus = Mock()
    GetLifeSpanMower._handle_body_data_list(
        event_bus,
        [
            {"type": "uwbCell", "sn": "BEACON-1", "left": 20, "total": 100},
            {"type": "uwbCell", "sn": "BEACON-1", "left": 80, "total": 100},
            {"type": "uwbCell", "sn": "BEACON-2", "left": 50, "total": 100},
        ],
    )

    assert (
        call(
            MowerBeaconsEvent(
                beacons=(
                    MowerBeacon(sn="BEACON-1", percent=20.0),
                    MowerBeacon(sn="BEACON-2", percent=50.0),
                )
            )
        )
        in event_bus.notify.call_args_list
    )


def test_get_life_span_mower_survives_a_component_it_has_never_heard_of() -> None:
    # uwbCell is the one unknown component this firmware sends. A later one must
    # not take the whole answer down the way uwbCell does today.
    event_bus = Mock()
    GetLifeSpanMower._handle_body_data_list(
        event_bus,
        [
            {"type": "somethingNew", "left": 1, "total": 2},
            {"type": "blade", "left": 2473, "total": 4800},
        ],
    )

    assert (
        call(LifeSpanEvent(LifeSpan.BLADE, 51.52, 2473))
        in event_bus.notify.call_args_list
    )


def test_get_rain_delay_asks_on_the_command_name_not_the_message_name() -> None:
    # Issue #54. Same shape as getProtectState: the library has neither, and
    # the answer carries the same payload as the push. Ecovacs' own app sends
    # it, and Janverhu/ecovacs-goat-g1 requests it at startup against a GOAT G1
    # and parses the answer as an onRainDelay payload.
    assert GetRainDelay.NAME == "getRainDelay"
    assert OnRainDelay.NAME == "onRainDelay"


def test_get_rain_delay_takes_no_arguments() -> None:
    assert GetRainDelay()._args == {}


def test_get_rain_delay_parses_the_answer_with_the_message_handler() -> None:
    # One parser, two entry points — the reason OnRainDelay is inherited rather
    # than copied. Compared as functions: a classmethod accessed on two classes
    # gives two distinct bound objects even when it is the same code.
    assert (
        GetRainDelay._handle_body_data_dict.__func__
        is OnRainDelay._handle_body_data_dict.__func__
    )


def test_get_rain_delay_notifies_the_setting_and_its_delay() -> None:
    event_bus = Mock()
    GetRainDelay._handle_body_data_dict(event_bus, {"enable": 1, "delay": 180})
    assert event_bus.notify.call_args_list == [
        call(MowerRainDelayEvent(enabled=True, delay=180))
    ]


def test_set_rain_delay_is_the_write_side_of_the_same_setting() -> None:
    assert SetRainDelay.NAME == "setRainDelay"


def test_set_rain_delay_sends_both_fields() -> None:
    # The whole reason the two entities have to know each other's value: the
    # device wants the pair, so switching the sensor on has to carry the delay
    # along and setting the delay has to carry the sensor's state.
    assert SetRainDelay(enable=True, delay=180)._args == {"enable": 1, "delay": 180}


def test_set_rain_delay_sends_zero_for_the_sensor_switched_off() -> None:
    # The wire wants 0/1, not JSON booleans.
    assert SetRainDelay(enable=False, delay=180)._args == {"enable": 0, "delay": 180}


def test_set_rain_delay_reports_a_refusal_instead_of_claiming_success() -> None:
    # ExecuteCommand's contract: a non-zero code is a failure. Without it a
    # rejected setting would look like it took, and the entity would only flip
    # back when the next push disagreed.
    assert (
        SetRainDelay._handle_body(Mock(), {"code": 500, "msg": "fail"}).state
        is HandlingState.FAILED
    )
    assert (
        SetRainDelay._handle_body(Mock(), {"code": 0, "msg": "ok"}).state
        is HandlingState.SUCCESS
    )
async def test_charging_sets_the_dock_and_still_publishes_docked() -> None:
    bus = _bus()
    record = register(bus)
    published = _collect(bus, StateEvent)

    GetChargeStateMower._handle_body_data_dict(bus, {"isCharging": 1, "mode": "slot"})
    await asyncio.sleep(0)

    assert record.docked is True
    # Upstream's own behaviour is kept: nothing that works today changes.
    assert [event.state for event in published] == [State.DOCKED]


async def test_not_charging_does_not_clear_the_dock() -> None:
    # A mower that finished charging while parked reports isCharging 0 without
    # having moved. Clearing here would let the plan state win again, which is
    # issue #67 returning through a different door. Only motion clears.
    bus = _bus()
    record = register(bus)
    record.dock()

    GetChargeStateMower._handle_body_data_dict(bus, {"isCharging": 0, "mode": "slot"})

    assert record.docked is True


async def test_a_fail_coded_answer_docks_the_record_and_still_publishes_docked() -> None:
    # {"msg": "fail", "code": "30007"} is "already charging" answered as a
    # failure. It never reaches _handle_body_data_dict above:
    # GetChargeState._handle_body branches on the code before descending into
    # body->data at all, so this is the path that must be driven directly.
    bus = _bus()
    record = register(bus)
    published = _collect(bus, StateEvent)

    GetChargeStateMower._handle_body(bus, {"msg": "fail", "code": "30007"})
    await asyncio.sleep(0)

    assert record.docked is True
    assert [event.state for event in published] == [State.DOCKED]


def test_an_unrecognised_fail_code_does_not_dock_the_record() -> None:
    # Upstream answers analyse() for a code it does not recognise and never
    # publishes DOCKED. Pins the condition to the published outcome, not to
    # "code is non-zero" — a body this shape must not dock on a hypothesis of
    # its own.
    bus = _bus()
    record = register(bus)

    result = GetChargeStateMower._handle_body(bus, {"msg": "fail", "code": "999"})

    assert record.docked is False
    assert result.state is HandlingState.ANALYSE


async def test_the_charge_half_is_awaited_before_the_clean_half() -> None:
    # The whole point, and it has to fail for a TaskGroup design, not just for
    # a design that happens to skip the second call (that is what the offline
    # test below already proves). A fake with no internal await point cannot
    # tell the two designs apart: both create the charge task before the clean
    # one, and with nothing to suspend on, each task runs to completion in
    # creation order regardless of whether that order came from a TaskGroup or
    # a plain sequential await.
    #
    # So the charge half yields to the event loop once before recording
    # itself, and the clean half records immediately, with no yield at all.
    # Under the sequential design there is nothing else running while the
    # charge half is suspended, so it still finishes — and is recorded —
    # first, however many times it yields. Under a TaskGroup, both tasks are
    # already scheduled before either result is known: the event loop runs
    # exactly the two tasks that existed when the TaskGroup's `async with`
    # block suspended, in creation order, before either gets to yield again.
    # By the time that happens the clean task — never suspending — has
    # already recorded and finished, so a single yield from the charge task is
    # already enough to let it go second; a second yield would only delay the
    # charge task further without changing the recorded order. `asyncio.sleep(0)`
    # yields a turn without adding wall-clock time, so the test stays
    # deterministic.
    order: list[str] = []

    async def fake_execute(self, authenticator, device_info, event_bus):
        if self.NAME == "getChargeState":
            await asyncio.sleep(0)
        order.append(self.NAME)
        return HandlingResult.success(), {"ret": "ok"}

    with patch.object(Command, "_execute", fake_execute):
        await MowerStateRefresh()._execute(AsyncMock(), _DEVICE_INFO, _bus())

    assert order == ["getChargeState", "getCleanInfo"]


async def test_an_offline_charge_answer_stops_the_refresh() -> None:
    # errno 4200 means the mower is offline and the library's own handler has
    # already published AvailabilityEvent(False). Asking for clean-info next
    # buys a second timeout and nothing else.
    sent: list[str] = []

    async def fake_execute(self, authenticator, device_info, event_bus):
        sent.append(self.NAME)
        return HandlingResult(HandlingState.FAILED), {"ret": "fail", "errno": 4200}

    with patch.object(Command, "_execute", fake_execute):
        await MowerStateRefresh()._execute(AsyncMock(), _DEVICE_INFO, _bus())

    assert sent == ["getChargeState"]


async def test_a_paused_plan_while_charging_yields_one_docked() -> None:
    # End to end through the composite, with the answers as issue #67 captured
    # them 21 ms apart.
    bus = _bus()
    register(bus)
    published = _collect(bus, StateEvent)

    async def fake_execute(self, authenticator, device_info, event_bus):
        if self.NAME == "getChargeState":
            self.handle(event_bus, {"body": {"data": {"isCharging": 1, "mode": "slot"}}})
        else:
            self.handle(
                event_bus,
                {
                    "body": {
                        "data": {
                            "trigger": "none",
                            "state": "clean",
                            "cleanState": {"motionState": "pause"},
                        }
                    }
                },
            )
        return HandlingResult.success(), {"ret": "ok"}

    with patch.object(Command, "_execute", fake_execute):
        await MowerStateRefresh()._execute(AsyncMock(), _DEVICE_INFO, bus)
    await asyncio.sleep(0)

    assert [event.state for event in published] == [State.DOCKED]


_OK = {"ret": "ok", "resp": {"body": {"data": {}, "code": 0, "msg": "ok"}}}
_NO_ANSWER = {"ret": "fail", "errno": 500, "debug": "wait for response timed out"}
_OFFLINE = {"ret": "fail", "errno": 4200}


def _transport(*responses: dict[str, object]) -> tuple[object, list[str]]:
    """A fake Command._execute answering with *responses* in order."""
    sent: list[str] = []
    queue = list(responses)

    async def fake_execute(self, authenticator, device_info, event_bus):
        sent.append(self.NAME)
        response = queue.pop(0)
        state = (
            HandlingState.SUCCESS if response.get("ret") == "ok"
            else HandlingState.FAILED
        )
        return HandlingResult(state), response

    return fake_execute, sent


async def test_an_answered_poll_sends_one_request_and_does_not_switch() -> None:
    fake, sent = _transport(_OK)
    with patch.object(Command, "_execute", fake):
        await GetCleanInfoMower()._execute(AsyncMock(), _DEVICE_INFO, _bus())

    assert sent == ["getCleanInfo"]
    assert selected(_DEVICE_INFO["did"]) is Family.NON_V2


async def test_no_answer_tries_the_other_family_and_keeps_it() -> None:
    # Issue #42's core observation: 900+ consecutive errno 500 on getCleanInfo
    # against a firmware that answers getCleanInfo_V2 first try.
    fake, sent = _transport(_NO_ANSWER, _OK)
    with patch.object(Command, "_execute", fake):
        await GetCleanInfoMower()._execute(AsyncMock(), _DEVICE_INFO, _bus())

    assert sent == ["getCleanInfo", "getCleanInfo_V2"]
    assert selected(_DEVICE_INFO["did"]) is Family.V2


async def test_neither_family_answering_is_the_network_not_the_dialect(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # errno 500 is "network issues or does not support the command" by the
    # library's own account, and getStats has been seen failing intermittently
    # with it. Committing on the first failure would mean every blip in a far
    # corner of the garden logged a false dialect change and cost two extra
    # requests on the poll after it.
    fake, sent = _transport(_NO_ANSWER, _NO_ANSWER)
    with caplog.at_level(logging.INFO), patch.object(Command, "_execute", fake):
        await GetCleanInfoMower()._execute(AsyncMock(), _DEVICE_INFO, _bus())

    assert sent == ["getCleanInfo", "getCleanInfo_V2"]
    assert selected(_DEVICE_INFO["did"]) is Family.NON_V2
    assert not [r for r in caplog.records if r.levelno == logging.INFO]


async def test_an_offline_mower_does_not_change_family() -> None:
    # errno 4200 and errno 500 both come back as HandlingState.FAILED, which is
    # why the response's errno is read directly instead.
    fake, sent = _transport(_OFFLINE)
    with patch.object(Command, "_execute", fake):
        await GetCleanInfoMower()._execute(AsyncMock(), _DEVICE_INFO, _bus())

    assert sent == ["getCleanInfo"]
    assert selected(_DEVICE_INFO["did"]) is Family.NON_V2


async def test_a_rest_timeout_does_not_change_family() -> None:
    # ApiTimeoutError leaves _execute returning an empty response dict.
    fake, sent = _transport({})
    with patch.object(Command, "_execute", fake):
        await GetCleanInfoMower()._execute(AsyncMock(), _DEVICE_INFO, _bus())

    assert sent == ["getCleanInfo"]
    assert selected(_DEVICE_INFO["did"]) is Family.NON_V2


async def test_a_second_attempt_going_offline_does_not_change_family(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # errno 500 on the first attempt is what triggers trying the other family
    # at all — that check is unchanged and still correct. But errno 4200 on
    # the second attempt means the mower went offline mid-attempt, not that it
    # answered on the other dialect; the old `!= 500` condition committed here
    # anyway, on an answer that never arrived.
    fake, sent = _transport(_NO_ANSWER, _OFFLINE)
    with caplog.at_level(logging.INFO), patch.object(Command, "_execute", fake):
        await GetCleanInfoMower()._execute(AsyncMock(), _DEVICE_INFO, _bus())

    assert sent == ["getCleanInfo", "getCleanInfo_V2"]
    assert selected(_DEVICE_INFO["did"]) is Family.NON_V2
    assert not [r for r in caplog.records if r.levelno == logging.INFO]


async def test_a_second_attempt_timing_out_does_not_change_family(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # An ApiTimeoutError on the second attempt leaves the response empty, the
    # same as a REST timeout on the first (test_a_rest_timeout_does_not_change_
    # family above) — no answer arrived either way, and the old `!= 500`
    # condition could not tell that apart from a real one.
    fake, sent = _transport(_NO_ANSWER, {})
    with caplog.at_level(logging.INFO), patch.object(Command, "_execute", fake):
        await GetCleanInfoMower()._execute(AsyncMock(), _DEVICE_INFO, _bus())

    assert sent == ["getCleanInfo", "getCleanInfo_V2"]
    assert selected(_DEVICE_INFO["did"]) is Family.NON_V2
    assert not [r for r in caplog.records if r.levelno == logging.INFO]


async def test_a_committed_family_is_used_directly_next_time() -> None:
    commit(_DEVICE_INFO["did"], Family.V2)
    fake, sent = _transport(_OK)
    with patch.object(Command, "_execute", fake):
        await GetCleanInfoMower()._execute(AsyncMock(), _DEVICE_INFO, _bus())

    assert sent == ["getCleanInfo_V2"]


async def test_the_committed_family_going_quiet_switches_back() -> None:
    # An over-the-air firmware update in either direction, corrected within one
    # poll interval and without a restart.
    commit(_DEVICE_INFO["did"], Family.V2)
    fake, sent = _transport(_NO_ANSWER, _OK)
    with patch.object(Command, "_execute", fake):
        await GetCleanInfoMower()._execute(AsyncMock(), _DEVICE_INFO, _bus())

    assert sent == ["getCleanInfo_V2", "getCleanInfo"]
    assert selected(_DEVICE_INFO["did"]) is Family.NON_V2


async def test_two_mowers_of_one_class_switch_independently() -> None:
    # The test that proves the side table. get_refresh_commands hands out the
    # stored instance, so both devices share one command object.
    other = {**_DEVICE_INFO, "did": "other-did"}
    command = GetCleanInfoMower()

    fake, _ = _transport(_NO_ANSWER, _OK, _OK)
    with patch.object(Command, "_execute", fake):
        await command._execute(AsyncMock(), _DEVICE_INFO, _bus())
        await command._execute(AsyncMock(), other, _bus())

    assert selected(_DEVICE_INFO["did"]) is Family.V2
    assert selected("other-did") is Family.NON_V2


def test_the_wrapper_can_be_instantiated() -> None:
    # Not a formality. MessageBody._handle_body is abstract and
    # JsonCommandWithMessageHandling mixes it in, so a wrapper derived straight
    # from that base raises TypeError: Can't instantiate abstract class — and
    # the first place that would surface is patch_device_info() at startup,
    # with the integration refusing to load.
    assert isinstance(GetCleanInfoMower(), GetCleanInfo)


@pytest.mark.parametrize(
    "delegate", [_GetCleanInfoNonV2, _GetCleanInfoV2], ids=["non-V2", "V2"]
)
async def test_both_delegates_carry_the_gate_and_the_idle_drop(delegate) -> None:
    # The hole this design nearly shipped with: delegating the V2 half to the
    # library's own GetCleanInfoV2 would leave that path with neither the idle
    # drop (#48) nor the charge gate (#67) — and #67 was observed on
    # getCleanInfo_V2. Asserting it on the non-V2 class alone passes while the
    # V2 path is unprotected, so both are parametrised here. This is the poll
    # path; test_messages.py::test_a_registered_mowers_clean_info_is_gated is
    # the same setup reached through the push handler instead. If this passes
    # while that one fails, the gate is in the wrong place.
    bus = _bus()
    register(bus).dock()
    published = _collect(bus, StateEvent)

    delegate._handle_body_data_dict(
        bus,
        {"trigger": "none", "state": "clean", "cleanState": {"motionState": "pause"}},
    )
    delegate._handle_body_data_dict(bus, {"trigger": "none", "state": "idle"})
    await asyncio.sleep(0)

    assert published == []


async def test_the_mow_command_switches_family_on_no_answer() -> None:
    fake, sent = _transport(_NO_ANSWER, _OK)
    with patch.object(Command, "_execute", fake):
        await CleanMower(CleanAction.START)._execute(
            AsyncMock(), _DEVICE_INFO, _bus()
        )

    assert sent == ["clean", "clean_V2"]
    assert selected(_DEVICE_INFO["did"]) is Family.V2


async def test_a_switch_discovered_by_the_poll_reaches_the_mow_command() -> None:
    # Why one choice per device rather than one per command: the state poll runs
    # every five minutes on its own, so discovery happens before the user
    # touches a control. Kept separate, the first press of Start on 1.36 would
    # always fail and only the second would work.
    fake, sent = _transport(_NO_ANSWER, _OK, _OK)
    with patch.object(Command, "_execute", fake):
        await GetCleanInfoMower()._execute(AsyncMock(), _DEVICE_INFO, _bus())
        await CleanMower(CleanAction.START)._execute(
            AsyncMock(), _DEVICE_INFO, _bus()
        )

    assert sent == ["getCleanInfo", "getCleanInfo_V2", "clean_V2"]


async def test_start_becomes_resume_for_a_plan_suppressed_on_the_dock() -> None:
    # The regression the precedence gate would otherwise cause. Clean._execute
    # rewrites start into resume from the last StateEvent, and the gate hides
    # the PAUSED that rewrite depends on. The reporter's plan was at 97% —
    # 133.37 of 137.51 m2 — so sending start would begin a new run over the
    # whole lawn instead of finishing the last 3%.
    bus = _bus()
    record = register(bus)
    record.dock()
    record.suppressed = State.PAUSED

    fake, sent = _transport(_OK)
    with patch.object(Command, "_execute", fake):
        command = CleanMower(CleanAction.START)
        await command._execute(AsyncMock(), _DEVICE_INFO, bus)

    assert command._delegate(Family.NON_V2)._args["act"] == "resume"


async def test_start_stays_start_when_nothing_was_suppressed() -> None:
    bus = _bus()
    register(bus)

    fake, _ = _transport(_OK)
    with patch.object(Command, "_execute", fake):
        command = CleanMower(CleanAction.START)
        await command._execute(AsyncMock(), _DEVICE_INFO, bus)

    assert command._delegate(Family.NON_V2)._args["act"] == "start"


async def test_the_delegate_does_not_second_guess_the_action_it_was_given() -> None:
    # Tests _NoActionRewrite in isolation, on the delegate, because that is the
    # only place it can be isolated: going through the wrapper would exercise
    # _effective_action, which legitimately turns RESUME into START when
    # nothing is paused, and the assertion would be about the wrong thing.
    #
    # Clean._execute rewrites in both directions — RESUME with a non-PAUSED
    # last state becomes START. Here the last state is DOCKED, which is exactly
    # what would trigger that rewrite, and the delegate must still send resume:
    # the wrapper has already decided, from a plan state Clean cannot see.
    bus = _bus()
    bus.notify(StateEvent(State.DOCKED))
    await asyncio.sleep(0)

    fake, sent = _transport(_OK)
    delegate = _CleanNonV2(CleanAction.RESUME)
    with patch.object(Command, "_execute", fake):
        await delegate._execute(AsyncMock(), _DEVICE_INFO, bus)

    assert sent == ["clean"]
    assert delegate._args["act"] == "resume"


def test_two_actions_are_not_equal_on_the_wrapper() -> None:
    # Command.__eq__ compares NAME and _args. An empty _args on every
    # CleanMower would make instances for different actions compare equal, so
    # mock.assert_called_with(CleanMower(CleanAction.PAUSE)) would pass even
    # for a call actually made with CleanAction.START.
    assert CleanMower(CleanAction.START) != CleanMower(CleanAction.PAUSE)


async def test_the_wrapper_still_turns_resume_into_start_when_nothing_is_paused() -> None:
    # The other half: dropping Clean's rewrite must not lose the behaviour, only
    # move it. The wrapper is where it lives now.
    bus = _bus()
    register(bus)
    bus.notify(StateEvent(State.DOCKED))
    await asyncio.sleep(0)

    fake, _ = _transport(_OK)
    with patch.object(Command, "_execute", fake):
        command = CleanMower(CleanAction.RESUME)
        await command._execute(AsyncMock(), _DEVICE_INFO, bus)

    assert command._delegate(Family.NON_V2)._args["act"] == "start"
