"""Per-device facts the handlers learn from the stream.

The mower reports charging and a paused plan as two orthogonal facts, and
``capabilities.state`` collapses them into a single ``StateEvent``; this
module holds the little state needed to prefer the first over the second
(issue #67). It also remembers the id of the map the mower is using, read from
the envelope of every map message, because a border job has to name it
(issue #12), the type of the running job, because pause, resume and stop have
to name that (issue #94), and the two halves of a departure from the dock,
because a scheduled run on some firmware leaves without a state message
(issue #120). Plus the registry that says which event buses belong to a
patched mower at all.

Keyed by ``EventBus`` rather than by ``did`` because message handlers are
classmethods that receive nothing else. An ``EventBus`` is per device, which
makes it both the natural key and self-cleaning: a ``WeakKeyDictionary`` entry
dies with its device, so nothing has to unregister on unload.

``MowerStateRecord`` must never hold a reference back to its bus. That would
make the value keep the key alive and the entry would never be collected.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary

if TYPE_CHECKING:
    from deebot_client.event_bus import EventBus
    from deebot_client.models import State

# How far apart, in monotonic seconds, a start or resume bury point and the
# mower's own "stopped charging" push may arrive and still be one departure
# from the dock (issue #120). The observed gaps are 3 to 8 s, in both orders;
# a minute leaves room for a slower departure (a longer boot, a wait for a
# positioning fix) while keeping a false pair, an unplug on a full battery
# near an announcement that never left, to one minute around it.
DEPARTURE_WINDOW = 60.0


def _within_window(stamp: float | None, now: float) -> bool:
    """Whether the other half, stamped at *stamp*, is close enough to pair."""
    return stamp is not None and now - stamp <= DEPARTURE_WINDOW


@dataclass
class MowerStateRecord:
    """What this device is doing, as far as the handlers need to remember.

    ``departure_edge_at`` and ``charge_left_at`` are the two halves of a
    departure from the dock (issue #120), in monotonic seconds: a start or
    resume bury point, and the mower's own ``onChargeState`` push saying it
    stopped charging. Neither alone proves the mower left — an announcement
    can stay on the charger in a rain delay, and an unplug can be a full
    battery — so CLEANING is written only when both arrive within
    ``DEPARTURE_WINDOW``, in either order. The handlers that report a state
    cancel both, since a reported state is better evidence than either half,
    and so does a pause or stop bury point: the job it announced paused or
    ended before the mower left. ``dock()`` clears only the unplug, and
    ``move()`` neither.
    """

    docked: bool = False
    suppressed: State | None = None
    map_id: str | None = None
    job_type: str | None = None
    departure_edge_at: float | None = None
    charge_left_at: float | None = None

    def note_job(self, content: object) -> None:
        """Remember the type of the running job, from ``cleanState.content``.

        The wire string as it is (``"auto"``, ``"spotArea"``, ``"border"``), not
        an enum: the mow command echoes it back and never interprets it, so a
        type this integration has never heard of costs nothing to carry.

        Issue #94. The mower wants the running job's type on pause, resume and
        stop. On an O1200 a resume with ``type: auto`` against a paused
        ``spotArea`` job was acknowledged with ``code 0`` and ignored; the
        app's resume, carrying ``spotArea``, moved the mower two seconds later.
        The ``onCleanInfo`` a job produces is the only place the type is
        reported, so it is kept here for the command to read. Neither
        ``move()`` nor ``dock()`` clears it: a plan paused on the charger is
        the same plan, and resuming it needs the type it was started with.

        A job that ends by finishing rather than by a stop is cleared by the
        ``mow-*-stop`` bury point with ``trigger: workComplete``, not by any
        state message: the captured natural-completion sequence
        (``onChargeInfo goCharging`` → ``onChargeState isCharging=1`` →
        ``onChargeInfo state=idle trigger=workComplete``) never touches
        ``cleanState``, so ``_OnMowJobEdge`` calls ``end_job()`` instead
        (issue #104). That narrows the gap without closing it: the mower has
        been seen to finish a run without sending its bury points at all
        (see ``const.py``), and then the type survives, so the next start
        from the dock goes out as a resume. Whether that self-corrects on
        the next job's first push depends on whether an ordinary automatic
        job's ``onCleanInfo`` carries ``content: {"type": "auto"}`` —
        unconfirmed, no device log of one exists yet. ``CleanMower``,
        ``MowArea`` and ``MowBorder`` write their own type here the moment
        they issue a start, which closes the window between pressing start
        and the first push for a job this integration begins; it does not
        help a job the mower starts on its own schedule.
        """
        if not isinstance(content, dict):
            return
        job_type = content.get("type")
        if isinstance(job_type, str) and job_type:
            self.job_type = job_type

    def end_job(self) -> None:
        """The mower reported that the job is over.

        Two messages say so: a pushed ``idle`` with no ``cleanState``, which
        is what the app's *End* produces, and a ``mow-*-stop`` bury point with
        ``trigger: workComplete``, which is how a job that finished on its
        own ends (issue #104). A job that is over has no type to echo. A
        later start begins a new ``auto`` job.
        """
        self.job_type = None

    def note_departure_edge(self, now: float) -> bool:
        """A start or resume bury point arrived at *now*; True if that completes a departure.

        Completing one clears both halves. Otherwise this becomes the waiting
        half, and an unplug older than the window is forgotten.
        """
        paired = _within_window(self.charge_left_at, now)
        self.departure_edge_at = None if paired else now
        self.charge_left_at = None
        return paired

    def note_charge_left(self, now: float) -> bool:
        """The mower pushed that it stopped charging at *now*; True if that completes a departure.

        The mirror of ``note_departure_edge()``.
        """
        paired = _within_window(self.departure_edge_at, now)
        self.charge_left_at = None if paired else now
        self.departure_edge_at = None
        return paired

    def cancel_departure(self) -> None:
        """Forget both halves: the mower reported a state, or its job paused or ended."""
        self.departure_edge_at = None
        self.charge_left_at = None

    def dock(self) -> None:
        """The mower is on its charger.

        Charging again refutes an unplug, so that half goes. The
        announcement half stays: this also runs on every repeat confirmation
        of a dock already known (a ``getChargeState`` answer, a refresh, a
        repeated push), and the window already bounds it (issue #120).
        """
        self.docked = True
        self.charge_left_at = None

    def move(self) -> None:
        """The mower reported working or returning.

        The suppressed plan state goes with it: from here the bus's own last
        ``StateEvent`` is the honest answer, and keeping a stale ``PAUSED``
        would let the mow command resume a plan that has since ended.

        Neither half of a departure is touched: ``OnPos`` calls this for a
        position sample away from the dock and publishes no state, and a
        sample between the two halves must not break the pair (issue #120).
        """
        self.docked = False
        self.suppressed = None

    def note_map(self, mid: object, using: object = None) -> None:
        """Remember the map the mower reports, from any map message's envelope.

        ``"0"`` is not a map: it is what the library's own ``OnCachedMapInfo``
        skips as "no map", and what an idle mower's ``onMapTrack`` envelopes
        carry. Anything that is not a non-empty string is ignored too, so a
        malformed envelope cannot erase an id a good one taught.

        ``using`` is the envelope's own word on whether this is the active
        map. ``onMI``, ``onArI``, ``onSpecialContour`` and ``onMapInfo_V2``
        carry it; ``onMapTrack`` and ``onMapTrace`` do not. An explicit ``0``
        (or ``"0"``) is a stored map the mower is not on, and a border job
        must not name it — the answer to ``getMapInfo_V2`` may carry such a
        map alongside the active one. An absent field says nothing and the
        id is taken as is.
        """
        if not isinstance(mid, str) or mid in ("", "0"):
            return
        if using in (0, "0"):
            return
        self.map_id = mid


_RECORDS: WeakKeyDictionary[EventBus, MowerStateRecord] = WeakKeyDictionary()


def register(event_bus: EventBus) -> MowerStateRecord:
    """Mark *event_bus* as a patched mower's, and return its record.

    Registration is also the marker that separates a mower from an ordinary
    Deebot vacuum on the same account: ``MESSAGES`` is global, so the message
    handlers this integration registers are reached for every JSON device, and
    they use this to tell whose payload they are looking at.
    """
    return _RECORDS.setdefault(event_bus, MowerStateRecord())


def record_for(event_bus: EventBus) -> MowerStateRecord | None:
    """The record for *event_bus*, or ``None`` if it is not a patched mower's."""
    return _RECORDS.get(event_bus)


def map_id_for(event_bus: EventBus) -> str | None:
    """The id of the map *event_bus*'s mower reports, or ``None`` if unknown.

    ``None`` both for an unregistered bus and for a registered one whose mower
    has not sent a map message yet; the caller cannot tell them apart and does
    not need to — neither can run a border job.
    """
    record = record_for(event_bus)
    return None if record is None else record.map_id


def reset() -> None:
    """Forget every record. Tests only."""
    _RECORDS.clear()
