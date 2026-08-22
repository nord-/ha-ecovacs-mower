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

``OnPos`` is different in kind from the rest of this module: ``onPos`` is not
unhandled, it is handled wrongly. See the class for what and why.

``onProtectState`` is a fourth unhandled message. It carries the mower's
protection flags. ``isRainProtect`` is the rain sensor's reading, not the
rain-protection setting — see ``MowerProtectStateEvent`` for the two samples
that establish it. Nothing derives the mower's *state* from those flags even
so: they are exposed raw, and the state's rain handling is built on ``trigger``
instead, which says why a run stopped and needs no interpretation.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from deebot_client.events import Position, PositionsEvent, StateEvent
from deebot_client.events.base import Event
from deebot_client.message import HandlingResult, MessageBodyDataDict
from deebot_client.models import State
from deebot_client.rs.map import PositionType

if TYPE_CHECKING:
    from deebot_client.event_bus import EventBus

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class MowerTriggerEvent(Event):
    """What the device says caused its current state.

    The raw string, uninterpreted: this layer decodes the wire format and
    nothing else. Observed values are ``rain``, ``workComplete``, ``app`` and
    ``continue``; ``alert`` appears in the library's own parsing.

    ``_seq`` makes every instance compare unequal to the last. The event bus
    drops a notification equal to the previous one of the same type, and a
    resume that follows a rain stop (``onCleanInfo``, owned by the library)
    never republishes a trigger — so without this, two rain stops in a row
    with no other trigger in between would have the second one silently
    dropped, which is exactly the ambiguity this event exists to remove.
    """

    trigger: str
    _seq: int = field(default_factory=itertools.count().__next__, repr=False)


def notify_trigger(event_bus: EventBus, data: dict[str, Any]) -> None:
    """Publish the payload's trigger, if it has one.

    Called for every message that carries the field, whatever its state says,
    including states this layer cannot map — a future consumer may care about
    a trigger this layer does not.
    """
    if isinstance(trigger := data.get("trigger"), str) and trigger:
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

    ``rain_protect`` keeps the wire field's own name on purpose: it is what the
    payload calls the flag, and the field name should not editorialise. What
    the flag *means* is settled, though. It is the rain sensor's reading, not
    the rain-protection setting:

        sample                                 setting        isRainProtect
        two seconds before a rain-stopped run  RainDetect: 1  1
        dry day, mower parked under cover      on in the app  0

    The second sample is a ``getProtectState`` answer from firmware 1.13.10. A
    flag that moves while the setting stands still is not the setting. That
    same answer had ``isAnimProtect: 0`` with animal protection switched on,
    which rules the setting reading out a second time and independently — it
    was the match between ``isAnimProtect: 0`` and ``ProtectAnimal.enable: 0``
    in the first sample that made the two readings look equally good.
    ``binary_sensor.py`` gives ``rain_protect`` the ``moisture`` device class on
    the strength of this.

    ``rain_delay`` stays uninterpreted and carries no device class. The working
    theory — **unconfirmed** — is that it covers the configured post-rain hold,
    three hours on the verified hardware. It would explain why the device
    reports the two separately at all: ``isRainProtect`` drops back to 0 as
    soon as the sensor dries off in the dock, while the mower is still waiting
    out its delay. Confirming it takes a rain event, where the flag should go
    to 1 when the run breaks, outlast ``rain_protect``, and clear after the
    configured delay rather than when the grass dries.
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
        defaulting the missing ones to False: claiming "the rain sensor is dry"
        or "no emergency stop" from a message that never said so is worse than
        keeping the previous value, which is what the entities do when nothing
        arrives.
        """
        if not cls._FLAGS.keys() <= data.keys():
            _LOGGER.warning(
                "onProtectState missing expected flags: %s",
                cls._FLAGS.keys() - data.keys(),
            )
            return HandlingResult.analyse()

        event_bus.notify(
            MowerProtectStateEvent(
                **{field: bool(data[key]) for key, field in cls._FLAGS.items()}
            )
        )
        return HandlingResult.success()


class OnPos(MessageBodyDataDict):
    """The mower's position, and the dock's on hardware that reports one.

    This one overrides the library instead of filling a gap. ``onPos`` has no
    entry in ``MESSAGES``, so it falls back to ``GetPos``, whose handler reads
    ``invalid`` as a boolean and keeps a sample only when it is exactly 0.

    Firmware 1.13.10 flags roughly nine of ten samples ``invalid: 2`` during a
    run — 102 of 115 in a six-minute capture — and those are not junk: they
    interleave with the ``invalid: 0`` ones along the same smooth 2 Hz path,
    5-15 cm apart at 0.16 m/s. Dropped, what is left is a tenth of the track
    with minute-wide gaps, which renders as a mower standing still, and after a
    restart as one parked in its dock. The capture the map was designed against
    is firmware 1.11.31, where every sample was ``invalid: 0`` — which is why
    the filter never showed until now.

    Only bit 0 is read as "no position". ``invalid: 1`` is what ``chargePos``
    carries on every sample from the verified hardware, and map.py's
    dock-at-the-origin assumption rests on exactly that. What bit 1 means is
    not established — dead reckoning between fixes is the obvious guess — but
    whatever it is, those coordinates track the mower, which is the only
    question this handler has to answer.

    A payload with nothing but flagged-out samples is handled, not unparsed:
    ``chargePos`` carries ``invalid: 1`` on every sample from this hardware, so
    a docked mower sends exactly that, and upstream's ``analyse()`` would log
    "Could not handle onPos" for a message this handler understood perfectly.
    ``OnMI`` makes the same distinction: success when there is nothing new to
    publish, ``analyse()`` reserved for a payload that would not parse.

    The body is otherwise upstream's, in upstream's order, so it stays easy to
    diff against ``commands/json/pos.py`` the day the filter is fixed there and
    this can be deleted.
    """

    NAME = "onPos"

    @classmethod
    def _handle_body_data_dict(
        cls, event_bus: EventBus, data: dict[str, Any]
    ) -> HandlingResult:
        """Handle message->body->data."""
        positions: list[Position] = []

        for type_str in ("deebotPos", "chargePos"):
            entries = data.get(type_str, [])
            if isinstance(entries, dict):
                entries = [entries]

            positions.extend(
                Position(
                    type=PositionType.from_str(type_str),
                    x=entry["x"],
                    y=entry["y"],
                    a=entry.get("a", 0),
                )
                for entry in entries
                if not entry.get("invalid", 0) & 1
            )

        if not positions:
            # Upstream returns analyse() here. Nothing consumes the state but
            # requested_commands, and this handler requests none, so the only
            # difference is a misleading debug line on every docked sample.
            return HandlingResult.success()

        event_bus.notify(PositionsEvent(positions=positions))
        return HandlingResult.success()
