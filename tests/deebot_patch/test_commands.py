"""Tests for the mower-adapted commands."""

from unittest.mock import Mock, call

from deebot_client.commands.json.clean import Clean, CleanV2, GetCleanInfo
from deebot_client.commands.json.stats import GetStats
from deebot_client.events import StateEvent, StatsEvent
from deebot_client.message import HandlingState
from deebot_client.models import CleanAction, State

from custom_components.ecovacs_mower.deebot_patch.commands import (
    CleanMower,
    GetCleanInfoMower,
    GetProtectState,
    GetStatsMower,
)
from custom_components.ecovacs_mower.deebot_patch.messages import (
    MowerProtectStateEvent,
    MowerStatsEvent,
    OnProtectState,
)


def test_publishes_to_clean_topic_not_clean_v2() -> None:
    # The core of the bug: GOAT listens on "clean", not "clean_V2".
    assert CleanMower.NAME == "clean"
    assert CleanV2.NAME == "clean_V2"


def test_is_a_clean_command() -> None:
    assert issubclass(CleanMower, Clean)


def test_start_uses_v2_content_shape() -> None:
    command = CleanMower(CleanAction.START)
    assert command._args == {"act": "start", "content": {"type": "auto"}}


def test_pause_uses_v2_content_shape() -> None:
    # Unlike CleanV2, which sends an empty type on pause, we echo "auto" — that
    # is what the app does against a lawn mower.
    command = CleanMower(CleanAction.PAUSE)
    assert command._args == {"act": "pause", "content": {"type": "auto"}}


def test_resume_uses_v2_content_shape() -> None:
    command = CleanMower(CleanAction.RESUME)
    assert command._args == {"act": "resume", "content": {"type": "auto"}}


def test_stop_uses_v2_content_shape() -> None:
    command = CleanMower(CleanAction.STOP)
    assert command._args == {"act": "stop", "content": {"type": "auto"}}


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
    result = GetCleanInfoMower._handle_body_data_dict(event_bus, {"state": "idle"})

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
    GetCleanInfoMower._handle_body_data_dict(
        event_bus, {"state": "clean", "cleanState": {"motionState": "working"}}
    )

    assert event_bus.notify.call_args_list == [call(StateEvent(State.CLEANING))]


def test_a_go_charging_answer_is_still_believed() -> None:
    event_bus = Mock()
    GetCleanInfoMower._handle_body_data_dict(event_bus, {"state": "goCharging"})

    assert event_bus.notify.call_args_list == [call(StateEvent(State.RETURNING))]


def test_an_alert_wins_over_the_dropped_idle() -> None:
    """An error is not something to swallow because idle came along with it."""
    event_bus = Mock()
    GetCleanInfoMower._handle_body_data_dict(
        event_bus, {"state": "idle", "trigger": "alert"}
    )

    assert event_bus.notify.call_args_list == [call(StateEvent(State.ERROR))]
