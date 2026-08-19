"""Tests for the mower-adapted commands."""

from typing import Any
from unittest.mock import Mock

from deebot_client.commands.json.clean import Clean, CleanV2
from deebot_client.models import CleanAction

from custom_components.ecovacs_mower.deebot_patch.commands import (
    CleanMower,
    GetProtectState,
)
from custom_components.ecovacs_mower.deebot_patch.messages import (
    MowerProtectStateEvent,
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


def test_get_protect_state_name() -> None:
    # Ecovacs' on<X>/get<X> convention: the reply comes back as onProtectState.
    assert GetProtectState.NAME == "getProtectState"


def test_get_protect_state_sends_no_arguments() -> None:
    # The device answers with every protection flag; asking for a subset is not
    # a shape this payload has.
    assert GetProtectState()._args == {}


def test_get_protect_state_notifies_the_rain_flag() -> None:
    # The command shares its handler with OnProtectState, so a reply must reach
    # the same event the message does.
    event_bus = Mock()
    body: dict[str, Any] = {
        "code": 0,
        "msg": "ok",
        "data": {"isRainProtect": 1, "isRainDelay": 0},
    }
    GetProtectState.handle(event_bus, {"body": body})

    notified = [call.args[0] for call in event_bus.notify.call_args_list]
    assert MowerProtectStateEvent(raining=True, rain_delay=False) in notified
