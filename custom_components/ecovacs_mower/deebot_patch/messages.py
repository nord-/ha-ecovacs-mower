"""Message handlers deebot-client lacks for lawn mowers.

Corresponds to DeebotUniverse/client.py PR #1647. GOAT reports its state via
four unsolicited MQTT messages, but the library only handles one of them:

    onCleanInfo         manual start/pause      handled by the library
    onScheduleTaskInfo  scheduled run           falls through as unknown
    onChargeInfo        returning / finished    falls through as unknown
    onProtectState      rain protection         falls through as unknown

Without onScheduleTaskInfo and onChargeInfo the entity never leaves "docked"
during a scheduled run, and never returns to "returning"/"docked" once the work
is finished.

onProtectState is not a state message at all — it is why the mower did what it
did. When rain interrupts a scheduled run the state messages only say
paused -> returning -> docked, exactly like a run that finished normally; the
rain shows up as ``trigger: "rain"`` (which says who asked, not what the device
is doing) and as ``isRainProtect: 1`` here. This is the only flag that persists
while the mower waits in the dock, so it is the one worth an entity.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from deebot_client.events import StateEvent
from deebot_client.events.base import Event
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


@dataclass(frozen=True)
class MowerProtectStateEvent(Event):
    """Rain protection, as reported by onProtectState.

    ``raining`` is the mower's own rain sensor: it stays true for as long as the
    device considers mowing rained off, which is what makes it usable as a
    state. ``rain_delay`` is the firmware's post-rain wait; observed as 0 while
    it was actually raining, so it is carried separately rather than folded into
    ``raining``.

    The payload also has isAnimProtect, isEStop, isLocked, isPinCode and
    isPrepareDataSuccess. They are deliberately not decoded: no entity would
    consume them yet, and a field nobody reads is a field nobody notices is
    wrong.
    """

    raining: bool
    rain_delay: bool


def handle_protect_state(event_bus: EventBus, data: dict[str, Any]) -> HandlingResult:
    """Parse a protect-state payload and notify the rain flags.

    Shared with ``GetProtectState``, which answers with the same body.
    """
    if "isRainProtect" not in data:
        # Not the payload this handler is for. Better analysed (and logged) than
        # silently reported as "not raining".
        return HandlingResult.analyse()

    event_bus.notify(
        MowerProtectStateEvent(
            raining=bool(data["isRainProtect"]),
            rain_delay=bool(data.get("isRainDelay", 0)),
        )
    )
    return HandlingResult.success()


class OnProtectState(MessageBodyDataDict):
    """Rain protection state."""

    NAME = "onProtectState"

    @classmethod
    def _handle_body_data_dict(
        cls, event_bus: EventBus, data: dict[str, Any]
    ) -> HandlingResult:
        """Handle message->body->data."""
        return handle_protect_state(event_bus, data)
