"""Message handlers deebot-client lacks for lawn mowers.

Corresponds to DeebotUniverse/client.py PR #1647. GOAT reports its state via
three unsolicited MQTT messages, but the library only handles one of them:

    onCleanInfo         manual start/pause      handled by the library
    onScheduleTaskInfo  scheduled run           falls through as unknown
    onChargeInfo        returning / finished    falls through as unknown

Without the latter two the entity never leaves "docked" during a scheduled run,
and never returns to "returning"/"docked" once the work is finished.

Two additions here are not part of that PR:

``MowerTriggerEvent`` republishes the ``trigger`` field that ``onChargeInfo``
and ``onScheduleTaskInfo`` carry. The state parsing ignores it on purpose — a
trigger says who asked, not what the mower is doing — but it is the only place
the device states *why* a run stopped: ``"rain"`` when the rain sensor cut it
short, ``"workComplete"`` when it finished, ``"app"`` when someone pressed a
button. Without it, a run cut short by rain is indistinguishable from one that
simply finished.

``onProtectState`` is a fourth unhandled message. It carries the mower's
protection flags. Whether ``isRainProtect`` means "it is raining" or only
"rain protection is switched on" is **not** established: the one captured
sample has it at 1 while the settings message has ``RainDetect: 1``, which fits
both readings. Nothing derives the mower's state from those flags for that
reason — they are exposed raw, and the state's rain handling is built on
``trigger`` instead, which needs no interpretation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from deebot_client.events import StateEvent
from deebot_client.events.base import Event
from deebot_client.message import HandlingResult, MessageBodyDataDict
from deebot_client.models import State

if TYPE_CHECKING:
    from deebot_client.event_bus import EventBus


@dataclass(frozen=True)
class MowerTriggerEvent(Event):
    """What the device says caused its current state.

    The raw string, uninterpreted: this layer decodes the wire format and
    nothing else. Observed values are ``rain``, ``workComplete``, ``app`` and
    ``continue``; ``alert`` appears in the library's own parsing.
    """

    trigger: str


def notify_trigger(event_bus: EventBus, data: dict[str, Any]) -> None:
    """Publish the payload's trigger, if it has one.

    Called for every message that carries the field, whatever its state says,
    including states this layer cannot map. Publishing the uninteresting
    triggers matters as much as publishing ``rain``: the event bus suppresses an
    event equal to the previous one, so without the ``workComplete`` in between,
    the rain of the *next* interrupted run would be swallowed as a duplicate.
    """
    if trigger := data.get("trigger"):
        event_bus.notify(MowerTriggerEvent(trigger))


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
        notify_trigger(event_bus, data)

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
        """Handle message->body->data.

        The trigger is published from here rather than from
        ``handle_clean_info``, which is kept a verbatim copy of the library's
        own parsing so it stays easy to diff against upstream.
        """
        notify_trigger(event_bus, data)
        return handle_clean_info(event_bus, data)


@dataclass(frozen=True)
class MowerProtectStateEvent(Event):
    """The mower's protection flags, from ``onProtectState``.

    The whole set arrives together and each value holds until the next message.

    ``rain_protect`` keeps the wire field's own name on purpose. The single
    captured sample has ``isRainProtect: 1`` two seconds before a rain-stopped
    run, which reads like "it is raining" — but the settings message in the same
    log has ``RainDetect: 1``, and ``isAnimProtect: 0`` likewise matches
    ``ProtectAnimal.enable: 0``, so "this protection is switched on" fits the
    data just as well. Calling the field ``raining`` would bake a guess into the
    name; a sample from a dry period would settle it.
    """

    rain_protect: bool
    rain_delay: bool
    emergency_stop: bool
    locked: bool
    animal_protect: bool


class OnProtectState(MessageBodyDataDict):
    """Rain, animal, emergency-stop and lock protection."""

    NAME = "onProtectState"

    # Wire key -> event field. The payload also carries isPinCode and
    # isPrepareDataSuccess; both describe the app's pairing flow rather than
    # anything about the mower, so they are left out on purpose.
    _FLAGS: ClassVar[dict[str, str]] = {
        "isRainProtect": "rain_protect",
        "isRainDelay": "rain_delay",
        "isEStop": "emergency_stop",
        "isLocked": "locked",
        "isAnimProtect": "animal_protect",
    }

    @classmethod
    def _handle_body_data_dict(
        cls, event_bus: EventBus, data: dict[str, Any]
    ) -> HandlingResult:
        """Handle message->body->data.

        All five flags must be present. A partial payload is dropped instead of
        defaulting the missing ones to False: claiming "no rain protection" or
        "no emergency stop" from a message that never said so is worse than
        keeping the previous value, which is what the entities do when nothing
        arrives.
        """
        if not cls._FLAGS.keys() <= data.keys():
            return HandlingResult.analyse()

        event_bus.notify(
            MowerProtectStateEvent(
                **{field: bool(data[key]) for key, field in cls._FLAGS.items()}
            )
        )
        return HandlingResult.success()
