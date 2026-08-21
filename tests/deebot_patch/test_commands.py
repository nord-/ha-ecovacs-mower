"""Tests for the mower-adapted commands."""

from unittest.mock import Mock, call

from deebot_client.commands.json.clean import Clean, CleanV2
from deebot_client.models import CleanAction

from custom_components.ecovacs_mower.deebot_patch.commands import (
    CleanMower,
    GetProtectState,
)
from custom_components.ecovacs_mower.deebot_patch.messages import (
    MowerProtectStateEvent,
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
