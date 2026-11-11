"""The trimming job's own bury points (issue #138).

Captured from an app-started trim on a GOAT A3000 LiDAR Pro (``51rcxt``,
firmware 1.13.10), identifiers replaced. The O1200 LiDAR Pro sends the same two
names for its edge cut (issue #127), which is why they are registered for every
patched mower and not for a class.

The pause and resume are from an O1200 LiDAR Pro (``2i0fns``, firmware
1.15.16), a trim paused and resumed from the app.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock

from deebot_client.messages.json import MESSAGES

from custom_components.ecovacs_mower.deebot_patch import apply
from custom_components.ecovacs_mower.deebot_patch.messages import (
    MowerJobEdgeEvent,
    OnMowBorderRotatePause,
    OnMowBorderRotateResume,
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

# A trim paused from the app, 46 seconds after its start edge: the areas are
# there, as on a stop.
_PAUSE_FROM_APP = {
    "jobId": "0000000000000002",
    "mowType": 1,
    "mowedArea": 0.0375,
    "time": 28.262344,
    "trigger": "app",
    "workArea": 3.81,
    "workType": 31,
}

# The resume of the same trim, 17 seconds later.
_RESUME_FROM_APP = {
    "jobId": "0000000000000002",
    "trigger": "app",
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


def test_a_trim_paused_from_the_app_publishes_a_pause_edge_with_its_areas() -> None:
    (event,) = _edges(OnMowBorderRotatePause, _PAUSE_FROM_APP)

    assert event.phase == "pause"
    assert event.trigger == "app"
    assert event.job_type == "borderrotate"
    assert event.mowed_area == 0.0375
    assert event.work_area == 3.81


def test_a_trim_resumed_from_the_app_publishes_a_resume_edge() -> None:
    (event,) = _edges(OnMowBorderRotateResume, _RESUME_FROM_APP)

    assert event.phase == "resume"
    assert event.trigger == "app"
    assert event.job_type == "borderrotate"
    assert event.mowed_area is None
    assert event.work_area is None


def test_the_trim_edge_names_are_registered() -> None:
    apply()

    assert (
        MESSAGES["onFwBuryPoint-bd_task-mow-border-rotate-pause"]
        is OnMowBorderRotatePause
    )
    assert (
        MESSAGES["onFwBuryPoint-bd_task-mow-border-rotate-resume"]
        is OnMowBorderRotateResume
    )

    assert (
        MESSAGES["onFwBuryPoint-bd_task-mow-border-rotate-start"]
        is OnMowBorderRotateStart
    )
    assert (
        MESSAGES["onFwBuryPoint-bd_task-mow-border-rotate-stop"]
        is OnMowBorderRotateStop
    )
