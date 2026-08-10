"""Message handlers deebot-client lacks for lawn mowers.

Corresponds to DeebotUniverse/client.py PR #1647. GOAT reports its state via
three unsolicited MQTT messages, but the library only handles one of them:

    onCleanInfo         manual start/pause      handled by the library
    onScheduleTaskInfo  scheduled run           falls through as unknown
    onChargeInfo        returning / finished    falls through as unknown

Without the latter two the entity never leaves "docked" during a scheduled run,
and never returns to "returning"/"docked" once the work is finished.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from deebot_client.events import StateEvent
from deebot_client.message import HandlingResult, MessageBodyDataDict
from deebot_client.models import State

if TYPE_CHECKING:
    from deebot_client.event_bus import EventBus


def handle_clean_info(event_bus: EventBus, data: dict[str, Any]) -> HandlingResult:
    """Parse a clean-info payload and notify the corresponding state.

    Shared by ``onScheduleTaskInfo`` and ``onCleanInfo``, which have identical
    payloads: ``{"state": "clean", "cleanState": {"motionState": "working"}}``.

    The state is derived from ``state`` — what the device is doing — not from
    ``trigger``, which only says who requested the action.
    """
    status: State | None = None
    state = data.get("state")
    if data.get("trigger") == "alert":
        status = State.ERROR
    # "washing" is mop washing and can never occur on a lawn mower. It is kept
    # anyway: the branch is copied verbatim from the library's own clean-info
    # parsing, and an identical copy is easier to diff against upstream the day
    # PR #1647 is merged and this can be deleted.
    elif state in ("clean", "washing"):
        clean_state = data.get("cleanState", {})
        motion_state = clean_state.get("motionState")
        if motion_state == "working":
            status = State.CLEANING
        elif motion_state == "pause":
            status = State.PAUSED
        elif motion_state == "goCharging":
            status = State.RETURNING
    elif state == "goCharging":
        status = State.RETURNING
    elif state == "idle":
        status = State.IDLE

    if status is not None:
        event_bus.notify(StateEvent(status))
        return HandlingResult.success()

    return HandlingResult.analyse()


class OnChargeInfo(MessageBodyDataDict):
    """Returning home and docking."""

    NAME = "onChargeInfo"

    @classmethod
    def _handle_body_data_dict(
        cls, event_bus: EventBus, data: dict[str, Any]
    ) -> HandlingResult:
        """Handle message->body->data.

        Unlike clean-info, the state here sits at the top level: "goCharging"
        on the way home, "idle" once the work is finished.
        """
        # The check comes before the state matching so it wins regardless of
        # which state happens to come along. Not observed in the device logs
        # (only "app" and "workComplete" have been seen for onChargeInfo), but
        # "trigger": "alert" unambiguously means an error state and must not
        # fall through silently just because the combination is unusual.
        if data.get("trigger") == "alert":
            event_bus.notify(StateEvent(State.ERROR))
            return HandlingResult.success()

        match data.get("state"):
            case "goCharging":
                status = State.RETURNING
            case "idle":
                status = State.DOCKED
            case _:
                return HandlingResult.analyse()

        event_bus.notify(StateEvent(status))
        return HandlingResult.success()


class OnScheduleTaskInfo(MessageBodyDataDict):
    """Scheduled mowing run."""

    NAME = "onScheduleTaskInfo"

    @classmethod
    def _handle_body_data_dict(
        cls, event_bus: EventBus, data: dict[str, Any]
    ) -> HandlingResult:
        """Handle message->body->data."""
        return handle_clean_info(event_bus, data)
