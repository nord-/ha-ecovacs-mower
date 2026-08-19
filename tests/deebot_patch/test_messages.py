"""Tests for the message handlers the library lacks."""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock

import pytest
from deebot_client.events import StateEvent
from deebot_client.models import State

from custom_components.ecovacs_mower.deebot_patch.messages import (
    MowerProtectStateEvent,
    OnChargeInfo,
    OnProtectState,
    OnScheduleTaskInfo,
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


def _notified_states(message, data: dict[str, Any]) -> list[State]:
    """Run the handler and return the states that were notified."""
    event_bus = Mock()
    message.handle(event_bus, _wrap(data))
    return [
        call.args[0].state
        for call in event_bus.notify.call_args_list
        if isinstance(call.args[0], StateEvent)
    ]


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


def _notified_protect_states(message, data: dict[str, Any]) -> list[MowerProtectStateEvent]:
    """Run the handler and return the protect-state events that were notified."""
    event_bus = Mock()
    message.handle(event_bus, _wrap(data))
    return [
        call.args[0]
        for call in event_bus.notify.call_args_list
        if isinstance(call.args[0], MowerProtectStateEvent)
    ]


def _protect_state(**overrides: int) -> dict[str, Any]:
    """A protect-state payload as captured from a 2i0fns (firmware 1.11.31)."""
    return {
        "isAnimProtect": 0,
        "isRainProtect": 0,
        "isRainDelay": 0,
        "isEStop": 0,
        "isLocked": 0,
        "isPinCode": 0,
        "isPrepareDataSuccess": 1,
    } | overrides


def test_on_protect_state_reports_rain() -> None:
    # The payload the mower actually sent three seconds before it paused a
    # scheduled run for rain: isRainProtect flips to 1, isRainDelay stays 0.
    events = _notified_protect_states(OnProtectState, _protect_state(isRainProtect=1))
    assert events == [MowerProtectStateEvent(raining=True, rain_delay=False)]


def test_on_protect_state_reports_dry() -> None:
    events = _notified_protect_states(OnProtectState, _protect_state())
    assert events == [MowerProtectStateEvent(raining=False, rain_delay=False)]


def test_on_protect_state_reports_the_rain_delay_separately() -> None:
    # The post-rain wait is its own flag: the firmware can be counting one down
    # without the rain sensor still being wet.
    events = _notified_protect_states(OnProtectState, _protect_state(isRainDelay=1))
    assert events == [MowerProtectStateEvent(raining=False, rain_delay=True)]


def test_on_protect_state_tolerates_a_missing_rain_delay() -> None:
    # Only isRainProtect is required; a firmware that omits the delay flag must
    # still yield a usable rain flag.
    data = _protect_state(isRainProtect=1)
    del data["isRainDelay"]
    events = _notified_protect_states(OnProtectState, data)
    assert events == [MowerProtectStateEvent(raining=True, rain_delay=False)]


def test_on_protect_state_ignores_a_payload_without_the_rain_flag() -> None:
    # Reporting "not raining" for a payload that never mentioned rain would be a
    # lie the entity cannot distinguish from a real dry reading.
    assert _notified_protect_states(OnProtectState, {"isEStop": 1}) == []


def test_message_names() -> None:
    # The names are the keys in the library's registry and must match exactly.
    assert OnChargeInfo.NAME == "onChargeInfo"
    assert OnScheduleTaskInfo.NAME == "onScheduleTaskInfo"
    assert OnProtectState.NAME == "onProtectState"
