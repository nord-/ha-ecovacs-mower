"""A trim now fires the event entities (issue #138).

Imports Home Assistant, so the file is marked ``requires_ha``; the source of
truth is CI. The edge comes from the real handler, not a hand-built event, so
this fails if the handler's job type or trigger stops reaching the entities.
"""

from unittest.mock import Mock

from tests import requires_ha

pytestmark = requires_ha

_START = {"EdgeMode": 1, "mapId": "1", "trigger": "app", "workType": 31}
_STOP = {
    "mowType": 1,
    "mowedArea": 32.82,
    "trigger": "workComplete",
    "workArea": 32.82,
    "workType": 31,
}
_PAUSE = {
    "mowType": 1,
    "mowedArea": 0.0375,
    "trigger": "app",
    "workArea": 3.81,
    "workType": 31,
}
_RESUME = {"trigger": "app", "workType": 31}


def _edge(message, body):
    from custom_components.ecovacs_mower.deebot_patch.messages import MowerJobEdgeEvent

    bus = Mock()
    message._handle_body(bus, body)
    (event,) = (
        call.args[0]
        for call in bus.notify.call_args_list
        if isinstance(call.args[0], MowerJobEdgeEvent)
    )
    return event


async def test_a_finished_trim_fires_last_mowing_job() -> None:
    from custom_components.ecovacs_mower.deebot_patch.messages import (
        OnMowBorderRotateStop,
    )
    from tests.test_event import _bare_event_entity

    entity = _bare_event_entity()

    await entity._on_job_edge(_edge(OnMowBorderRotateStop, _STOP))

    assert entity.state_attributes["event_type"] == "finished"
    entity.async_write_ha_state.assert_called_once()


async def test_a_trim_started_from_the_app_fires_job_started() -> None:
    from custom_components.ecovacs_mower.deebot_patch.messages import (
        OnMowBorderRotateStart,
    )
    from tests.test_event import _bare_job_started_entity

    entity = _bare_job_started_entity()

    await entity._on_job_edge(_edge(OnMowBorderRotateStart, _START))

    assert entity.state_attributes["event_type"] == "app"
    assert entity.state_attributes["job_type"] == "borderrotate"


async def test_a_trim_resumed_from_the_app_fires_job_started_as_a_resume() -> None:
    from custom_components.ecovacs_mower.deebot_patch.messages import (
        OnMowBorderRotateResume,
    )
    from tests.test_event import _bare_job_started_entity

    entity = _bare_job_started_entity()

    await entity._on_job_edge(_edge(OnMowBorderRotateResume, _RESUME))

    assert entity.state_attributes["event_type"] == "app"
    assert entity.state_attributes["phase"] == "resume"
    assert entity.state_attributes["job_type"] == "borderrotate"


async def test_a_paused_trim_fires_neither_event() -> None:
    """``app`` is mapped for a stop and declared for a start: the phase decides."""
    from custom_components.ecovacs_mower.deebot_patch.messages import (
        OnMowBorderRotatePause,
    )
    from tests.test_event import _bare_event_entity, _bare_job_started_entity

    edge = _edge(OnMowBorderRotatePause, _PAUSE)
    last_job = _bare_event_entity()
    job_started = _bare_job_started_entity()

    await last_job._on_job_edge(edge)
    await job_started._on_job_edge(edge)

    assert last_job.state_attributes["event_type"] is None
    assert job_started.state_attributes["event_type"] is None
