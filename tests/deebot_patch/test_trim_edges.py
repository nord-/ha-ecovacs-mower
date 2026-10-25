"""The trimming job's own bury points (issue #138).

Captured from an app-started trim on a GOAT A3000 LiDAR Pro (``51rcxt``,
firmware 1.13.10), identifiers replaced. The O1200 LiDAR Pro sends the same two
names for its edge cut (issue #127), which is why they are registered for every
patched mower and not for a class.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock

from deebot_client.messages.json import MESSAGES

from custom_components.ecovacs_mower.deebot_patch import apply
from custom_components.ecovacs_mower.deebot_patch.messages import (
    MowerJobEdgeEvent,
    OnMowBorderRotateStart,
    OnMowBorderRotateStop,
)

_START = {
    "EdgeMode": 1,
    "jobId": "0000000000000001",
    "mapId": "1",
    "trigger": "app",
    "workType": 31,
}

# The end of the same trim: it ran to completion, so the trigger is
# ``workComplete`` and both areas are the one number.
_STOP_COMPLETE = {
    "evaluationTime": 1094,
    "jobId": "0000000000000001",
    "mowType": 1,
    "mowedArea": 32.82,
    "time": 1400.45,
    "trigger": "workComplete",
    "workArea": 32.82,
    "workType": 31,
}


def _edges(message: Any, body: dict[str, Any]) -> list[MowerJobEdgeEvent]:
    bus = Mock()
    message._handle_body(bus, body)
    return [
        call.args[0]
        for call in bus.notify.call_args_list
        if isinstance(call.args[0], MowerJobEdgeEvent)
    ]


def test_a_trim_start_publishes_a_start_edge_with_its_own_job_type() -> None:
    (event,) = _edges(OnMowBorderRotateStart, _START)

    assert event.phase == "start"
    assert event.trigger == "app"
    assert event.job_type == "borderrotate"


def test_a_completed_trim_publishes_both_areas() -> None:
    (event,) = _edges(OnMowBorderRotateStop, _STOP_COMPLETE)

    assert event.phase == "stop"
    assert event.trigger == "workComplete"
    assert event.job_type == "borderrotate"
    assert event.mowed_area == 32.82
    assert event.work_area == 32.82


def test_the_trim_edge_names_are_registered() -> None:
    apply()

    assert (
        MESSAGES["onFwBuryPoint-bd_task-mow-border-rotate-start"]
        is OnMowBorderRotateStart
    )
    assert (
        MESSAGES["onFwBuryPoint-bd_task-mow-border-rotate-stop"]
        is OnMowBorderRotateStop
    )
