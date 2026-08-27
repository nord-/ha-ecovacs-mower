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

``MowerStatsEvent`` carries ``mowedArea``, a field the library's ``StatsEvent``
drops — the only number that moves while a job runs (issue #39). Two handlers
produce it from the same three numbers: ``GetStatsMower`` in ``commands.py``
parses the answer to ``getStats``, and ``OnStatsMower`` below parses the
``onStats`` push that some classes send and others never do (issue #55).

``OnPos`` is different in kind from the rest of this module: ``onPos`` is not
unhandled, it is handled wrongly. See the class for what and why.

``MowerBeaconsEvent`` carries the UWB beacons a beacon-guided GOAT reports
inside its ``getLifeSpan`` answer, which the library drops on the floor and
takes the rest of the answer with it (issue #40). Its parser is called from
``GetLifeSpanMower`` in ``commands.py``.

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
from deebot_client.messages.json.stats import OnStats
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


@dataclass(frozen=True)
class MowerStatsEvent(Event):
    """The running job's target area and how much of it is already cut.

    Both numbers come from one ``getStats`` answer, and the pair is what makes
    a percentage possible. ``area`` is not what its name suggests: on GOAT it is
    the *target* area of the current job — what the app shows as the mowing zone
    — and it stands still for the whole run while ``mowedArea`` climbs towards
    it. Calibrated against the owner's lawn: the ``bd_task-mow-*`` telemetry
    reported ``workArea`` 320.5675 m² for a full mow, and the zone polygons the
    map decoder had already parsed sum to 319.30 m².

    An edge-cutting job (``workType`` 33 rather than 18) reports the strip it
    sweeps, 21.13 m² on the same lawn, not the lawn. The consumer does not care:
    a ratio of the two fields is the fraction of *this* job that is done, and
    the unit cancels.

    Both are cm², and on the firmware this was built against both read 0
    between jobs — which is why a zero ``area`` means "no job" rather than zero
    percent. That convention is not universal: a G1-800 on 1.36.208 still
    reported the finished job's numbers six hours later (issue #55), so the
    entity gates on the mower's state as well and does not rely on this alone.
    """

    area: int | None
    mowed_area: int | None


def notify_mower_stats(event_bus: EventBus, data: dict[str, Any]) -> None:
    """Publish the pair a stats payload carries, from wherever it arrived.

    Both entry points parse the same three numbers: ``getStats`` answers with
    them and ``onStats`` pushes them. A copy in each would drift the day the
    payload gains a field, and the two cannot be one class — the message
    registry and the command topic are keyed on ``NAME``, and these two names
    differ.

    Missing keys become ``None`` rather than 0: a firmware that does not report
    ``mowedArea`` should leave the progress entity unknown, not claim the job
    has not started.
    """
    event_bus.notify(
        MowerStatsEvent(area=data.get("area"), mowed_area=data.get("mowedArea"))
    )


@dataclass(frozen=True)
class MowerBeacon:
    """One UWB beacon's serial and how much of its dry cell is left.

    ``sn`` is the code the app's maintenance page prints next to each beacon,
    which is the only thing that tells four otherwise identical entries apart —
    the payload has no index and no guaranteed order.
    """

    sn: str
    percent: float


@dataclass(frozen=True)
class MowerBeaconsEvent(Event):
    """Every beacon the mower reported, in the order the device listed them.

    One event for the whole set rather than one per beacon: the event bus keeps
    the last event of each type and hands it to whoever subscribes later, so
    four separate notifications of the same type would leave a late subscriber
    holding only the fourth.
    """

    beacons: tuple[MowerBeacon, ...]


# The wire type of a beacon's dry cell. Not a LifeSpan member, and cannot become
# one: the enum has members already, and Python enums are closed once they do.
BEACON_COMPONENT = "uwbCell"


def notify_mower_beacons(event_bus: EventBus, data: list[dict[str, Any]]) -> None:
    """Publish the beacon entries a life-span answer carries, if it has any.

    Nothing is published when there are none. A mower that navigates without
    beacons — the O1200 is one — would otherwise report an empty set, which
    reads as "no charge left" rather than "no beacons".

    An entry that cannot be turned into a reading is dropped on its own instead
    of taking the others with it. That is the entire failure this class exists
    to undo, and repeating it one level down would be absurd.
    """
    beacons: list[MowerBeacon] = []

    for component in data:
        if component.get("type") != BEACON_COMPONENT:
            continue

        sn = component.get("sn")
        if not isinstance(sn, str) or not sn:
            # Without a serial the reading cannot be attributed to a beacon,
            # and attributing it to the wrong one is worse than losing it.
            _LOGGER.warning("Beacon entry without a serial, dropped: %s", component)
            continue

        try:
            left = int(component["left"])
            total = int(component["total"])
        except (KeyError, TypeError, ValueError):
            _LOGGER.warning(
                "Beacon entry %s is not a pair of numbers: %s", sn, component
            )
            continue

        if total <= 0:
            _LOGGER.warning("Beacon entry %s reports a total of %s, dropped", sn, total)
            continue

        # Rounded the way the library rounds its own life spans, so a beacon and
        # a blade cannot disagree about what 51.52 means.
        beacons.append(MowerBeacon(sn=sn, percent=round((left / total) * 100, 2)))

    if beacons:
        event_bus.notify(MowerBeaconsEvent(beacons=tuple(beacons)))


class OnStatsMower(OnStats):
    """``onStats``, keeping the field the library throws away.

    The exact counterpart of ``GetStatsMower`` in ``commands.py``: same three
    numbers, same dropped one, different entry point. Upstream's ``OnStats``
    notifies ``StatsEvent`` from ``area`` and ``time`` and never looks at
    ``mowedArea``, so before this the push was parsed and the only number that
    moves during a job was discarded.

    #39 built the entity on a poll alone, on the finding that ``onStats`` did
    not arrive once in 38 hours of logging. It does arrive: a GOAT O800 RTK
    (``2px96q``, firmware 1.17.11) pushed it about twice a second through an
    eleven-minute job, carrying the whole curve from ``mowedArea`` 0 to 208900
    against an ``area`` of 208900, and a GOAT G1-800 (``77atlz``, 1.36.208)
    sent 441 of them in one job (issue #55). Whether the O1200 the 38-hour
    window covers is genuinely silent is unsettled — the comment above
    ``POLL_INTERVAL`` in ``const.py`` records ``onStats`` still arriving there
    on 2026-08-21, from the same hardware.

    So the poll stays, on the narrower justification that it is what fills the
    entity in after a restart and the floor under a push that may not come.
    Where the push does arrive this makes the reading follow the mower rather
    than the five-minute tick, and covers the same firmware's intermittently
    unanswered ``getStats`` for free.
    """

    @classmethod
    def _handle_body_data_dict(
        cls, event_bus: EventBus, data: dict[str, Any]
    ) -> HandlingResult:
        """Publish the mower's own pair, then defer to upstream's parsing.

        In that order and not the reverse: upstream indexes ``data["area"]``
        and ``data["time"]`` directly and raises ``KeyError`` when a push omits
        either, which would otherwise take ``notify_mower_stats`` down with it.
        No observed firmware sends such a push, but there is no reason to make
        this handler less robust than its own fallback.
        """
        notify_mower_stats(event_bus, data)
        return super()._handle_body_data_dict(event_bus, data)


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
    payload calls the flag, and the field name should not editorialise. The
    rain-protection *setting* reading is ruled out, though; what survives is
    the sensor's own reading:

        sample                                 setting                   isRainProtect
        two seconds before a rain-stopped run  RainDetect: 1 (settings)  1
        dry day, mower parked under cover      on in the app             0

    The second sample is a ``getProtectState`` answer from firmware 1.13.10;
    the first row's ``RainDetect: 1`` is read off the settings message in the
    same log, not off a live field. A flag that moves while the setting stands
    still is not the setting. That same answer had ``isAnimProtect: 0`` with
    animal protection switched on, which rules the setting reading out a
    second time — on a sibling flag rather than this one, so it leans on the
    five flags being the same kind of thing — and it was the match between
    ``isAnimProtect: 0`` and ``ProtectAnimal.enable: 0`` in the first sample
    that had made the two readings look equally good. ``binary_sensor.py``
    gives ``rain_protect`` the ``moisture`` device class on the strength of
    this.

    What the samples do *not* separate is "the rain sensor is wet" from "the
    mower is currently held for rain": both are 1 two seconds before a
    rain-stopped run and 0 on a dry day under cover. ``moisture`` is the right
    class either way — in practice the two coincide, and no class fits the
    second reading better — but telling them apart needs the same rain event
    ``rain_delay`` needs, below.

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
