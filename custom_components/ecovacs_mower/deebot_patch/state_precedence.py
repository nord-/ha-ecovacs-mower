"""Per-device facts the handlers learn from the stream.

Two of them. The mower reports charging and a paused plan as two orthogonal
facts, and ``capabilities.state`` collapses them into a single ``StateEvent``;
this module holds the little state needed to prefer the first over the second
(issue #67). It also remembers the id of the map the mower is using, read from
the envelope of every map message, because a border job has to name it
(issue #12), and the type of the running job, because pause, resume and stop
have to name that (issue #94). Plus the registry that says which event buses
belong to a patched mower at all.

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


@dataclass
class MowerStateRecord:
    """What this device is doing, as far as the precedence rule is concerned."""

    docked: bool = False
    suppressed: State | None = None
    map_id: str | None = None
    job_type: str | None = None

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
        """
        if not isinstance(content, dict):
            return
        job_type = content.get("type")
        if isinstance(job_type, str) and job_type:
            self.job_type = job_type

    def end_job(self) -> None:
        """The mower reported ``idle`` with no ``cleanState``: the job is over.

        That is what the app's *End* produces, and a job that is over has no
        type to echo. A later start begins a new ``auto`` job.
        """
        self.job_type = None

    def dock(self) -> None:
        """The mower is on its charger."""
        self.docked = True

    def move(self) -> None:
        """The mower reported working or returning.

        The suppressed plan state goes with it: from here the bus's own last
        ``StateEvent`` is the honest answer, and keeping a stale ``PAUSED``
        would let the mow command resume a plan that has since ended.
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
