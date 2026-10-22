"""Map message handlers deebot-client lacks for lawn mowers.

GOAT reports its map via unsolicited MQTT messages — onMI, onArI,
onMapTrack, onSpecialContour and, from firmware 1.17 on, onMapTrace —
none of which exist in the library (the vacuum world uses
getMapTrace/getMajorMap instead). The wire format is documented in
docs/superpowers/specs/2026-08-10-mower-map-design.md.

Firmware 1.17 kept the transport and changed the payloads: point lists
instead of chain codes, no-go zones inside onArI instead of
onSpecialContour, and onMapTrace instead of onMapTrack (issue #41). The
dialect is decided per blob in geometry.py, so one build serves both.

Firmware 1.36 renamed onMI to onMapInfo_V2 and, unlike the rest of these,
sends it only to a client that asked with getMapInfo_V2 — see OnMapInfo
below (issue #81). A non-V2 mower answers getMI with onMI the same way, as
long as an appping came first, and otherwise pushes it only now and then.
GetMapInfoMower in commands.py asks under whichever name the mower answers.

Map data is best effort: a broken payload logs at DEBUG and is dropped,
it never raises and never touches the control path.
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any, ClassVar

from deebot_client.events.base import Event
from deebot_client.message import HandlingResult, MessageBodyDataDict

from .areas import MowerAreaEvent
from .geometry import (
    FragmentBuffer,
    Polygon,
    Segment,
    parse_area_info,
    parse_map_info,
    parse_map_trace,
    parse_map_track,
    parse_special_contour,
)
from .state_precedence import profile_for_bus, record_for

if TYPE_CHECKING:
    from deebot_client.event_bus import EventBus

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class MowerMapInfoEvent(Event):
    """Static map geometry. None fields mean "unchanged", never "empty".

    ``boundaries`` is one lawn outline per mowed area; all but a
    multi-area chain-dialect ``onArI`` report exactly one.
    """

    boundaries: list[Polygon] | None
    zones: list[Polygon] | None
    flush_edges: list[Polygon] | None
    corridors: list[Polygon] | None = None


@dataclass(frozen=True)
class MowerObstaclesEvent(Event):
    """Detected obstacles; the list is the complete current set."""

    obstacles: list[Polygon]


@dataclass(frozen=True)
class MowerCoverageEvent(Event):
    """Mowed lane spans keyed (zone, row); an empty list clears the row.

    The reading onMapTrack gets on a class whose ``remaining_plan`` flag
    is off: unvalidated, and what this integration has always published.
    Merged forever, because under this reading nothing a row says can
    take back what an earlier one said.
    """

    lanes: dict[tuple[str, int], list[Segment]]


@dataclass(frozen=True)
class MowerRemainingEvent(Event):
    """The lanes still to mow, keyed (zone, row).

    Not coverage: see ``MapTrack``. An empty list is a row with nothing
    left in it. Published in place of ``MowerCoverageEvent`` for a class
    whose profile sets ``remaining_plan``.

    ``complete`` is the whole remaining plan, in answer to
    ``GetMapTrack``: it is the entire truth and anything not in it is
    finished. Otherwise this is the single row the mower is working on,
    pushed as it goes, and only that row changes.
    """

    rows: dict[tuple[str, int], list[Segment]]
    complete: bool = False


@dataclass(frozen=True)
class MowerCoveredAreaEvent(Event):
    """Mowed area as outlines and the unmowed islands inside them.

    Firmware 1.17's coverage shape; both lists are the complete current
    set, never an increment.
    """

    areas: list[Polygon]
    holes: list[Polygon]


@dataclass(frozen=True)
class MowerNoGoZonesEvent(Event):
    """User-defined no-go zones; the list is the complete current set."""

    zones: list[Polygon]


class _MapMessage(MessageBodyDataDict, ABC):
    """Shared multipart buffering and best-effort decoding.

    ABC must be a direct base: the library's __init_subclass__ NAME check
    exempts only classes with ABC in __bases__.
    """

    _buffer: ClassVar[FragmentBuffer]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # One buffer per message type. Shared across devices: batids are
        # random enough that a collision between two mowers on the same
        # account would at worst drop one blob.
        cls._buffer = FragmentBuffer()

    @classmethod
    def _handle_body_data_dict(
        cls, event_bus: EventBus, data: dict[str, Any]
    ) -> HandlingResult:
        # Every map message names the map in its envelope, and a border job
        # has to name it back (issue #12). Recorded here, ahead of the fragment
        # buffering, so the first fragment teaches it and a blob that never
        # completes or never decodes still does. "using" goes along so a
        # stored-but-inactive map is not mistaken for the current one. Only
        # for a registered bus: an ordinary vacuum on the same account reaches
        # this handler too.
        if (record := record_for(event_bus)) is not None:
            known = record.map_id
            record.note_map(data.get("mid"), data.get("using"))
            if record.map_id != known:
                # The area inventory belongs to a map, and GetAreaSet sends
                # nothing until it knows which one. A no-op on a mower nothing
                # subscribes MowerAreaEvent for.
                event_bus.request_refresh(MowerAreaEvent)
        info = data.get("info")
        if not info:
            return HandlingResult.analyse()
        blob = cls._buffer.add(
            str(data.get("batid", "")),
            int(data.get("index", 0)),
            info,
            int(data.get("infoSize", -1)),
        )
        if blob is None:
            # Waiting for more fragments (or the blob is corrupt, in which
            # case the capped buffer eventually evicts it).
            return HandlingResult.success()
        try:
            return cls._notify(event_bus, blob)
        except (TypeError, ValueError, KeyError, IndexError):
            # json.JSONDecodeError is a ValueError. TypeError covers a blob
            # that decodes to something non-iterable (e.g. a bare int).
            # Best effort: log and drop, never disturb the control path.
            _LOGGER.debug(
                "Undecodable %s blob (batid %s)", cls.NAME, data.get("batid")
            )
            return HandlingResult.analyse()

    @classmethod
    def _notify(cls, event_bus: EventBus, blob: bytes) -> HandlingResult:
        raise NotImplementedError


class OnMI(_MapMessage):
    """Map info: the lawn boundary outline."""

    NAME = "onMI"

    @classmethod
    def _notify(cls, event_bus: EventBus, blob: bytes) -> HandlingResult:
        map_info = parse_map_info(blob)
        if map_info.boundaries is None and map_info.corridors is None:
            return HandlingResult.success()  # idle snapshot, nothing new
        event_bus.notify(
            MowerMapInfoEvent(
                boundaries=map_info.boundaries,
                zones=None,
                flush_edges=None,
                corridors=map_info.corridors,
            )
        )
        return HandlingResult.success()


class OnMapInfo(OnMI):
    """``onMapInfo_V2``: onMI's payload under the name firmware 1.36 uses.

    A GOAT G1-800 (``77atlz``, fw 1.36.208) answers ``getMapInfo_V2`` with
    32 KB of lawn outline across seven ``onMapInfo_V2`` fragments — the
    same multipart envelope and the same 7z-base64 JSON container ``onMI``
    uses, but not the same record layout: the boundary record leads with
    an id where ``onMI`` writes ``s1``, each of its 692 points carries a
    third field, and three sections follow it. ``parse_map_info`` reads
    that dialect too, from the captured blob in the fixtures rather than
    from an assumption. ``OnMI`` keeps ``NAME = "onMI"``;
    hardware sending the short name is still supported, and
    ``_MapMessage.__init_subclass__`` gives this subclass its own
    ``FragmentBuffer``, so the two names reassemble independently.

    **The name is the full ``onMapInfo_V2``, not the suffix-stripped
    ``onMapInfo``.** ``get_message()`` tries an exact match before it
    strips ``_V2``, and the library registers its own ``OnMapInfoV2``
    under the full name — which is why the message is never logged as
    ``Unknown message`` and why registering the stripped name would
    silently never be reached. ``apply()`` overwrites that entry, the same
    way ``OnStatsMower`` overwrites ``onStats``, and asserts afterwards
    that the lookup returns this class.

    What the library's handler does with the payload is the bug: it drops
    every blob whose ``outlineVer`` is not ``"1"``, and this firmware
    sends ``outlineVer "0"`` on the map it reports as ``using: 1``. So the
    boundary arrived, was claimed, and was discarded — no
    ``MowerMapInfoEvent``, no obstacles, no no-go zones.

    Nothing here parses the blob differently from ``onMI`` — the dialect
    is decided per blob in ``geometry.py``, as it already is for firmware
    1.17 — and the fallback is unchanged: a shape ``parse_map_info`` does
    not recognise yields no boundary and ``OnMI._notify`` publishes
    nothing, the same best-effort drop every other map message makes. See
    issue #81.
    """

    NAME = "onMapInfo_V2"


class OnArI(_MapMessage):
    """Area info: zones, obstacles, outlines, flush-mow edges — plus
    no-go zones from firmware 1.17 on, and on a class whose profile says
    its chain-dialect section 2 holds them.
    """

    NAME = "onArI"

    @classmethod
    def _notify(cls, event_bus: EventBus, blob: bytes) -> HandlingResult:
        profile = profile_for_bus(event_bus)
        area = parse_area_info(
            blob, area_nogo=profile is not None and profile.area_nogo
        )
        map_info = area.map_info
        if (
            map_info.boundaries is not None
            or map_info.zones is not None
            or map_info.flush_edges is not None
        ):
            event_bus.notify(
                MowerMapInfoEvent(
                    boundaries=map_info.boundaries,
                    zones=map_info.zones,
                    flush_edges=map_info.flush_edges,
                )
            )
        if area.obstacles is not None:
            event_bus.notify(MowerObstaclesEvent(obstacles=area.obstacles))
        if area.nogo is not None:
            event_bus.notify(MowerNoGoZonesEvent(zones=area.nogo))
        return HandlingResult.success()


class OnMapTrack(_MapMessage):
    """Lane stretches per 100 mm row — what is still to mow, or coverage.

    Which of the two is the class's business, not the blob's: the bytes
    are identical and only one mower has been watched long enough to say
    what they mean. A class whose profile sets ``remaining_plan`` gets the
    measured reading; every other one keeps the coverage reading this
    integration has always published. See ``MapTrack`` and
    ``MowerProfile.remaining_plan``.
    """

    NAME = "onMapTrack"

    @classmethod
    def _notify(cls, event_bus: EventBus, blob: bytes) -> HandlingResult:
        track = parse_map_track(blob)
        profile = profile_for_bus(event_bus)
        if profile is None or not profile.remaining_plan:
            # Coverage grows and never shrinks under this reading, so an
            # empty blob has nothing to say.
            if track.rows:
                event_bus.notify(MowerCoverageEvent(lanes=track.rows))
            return HandlingResult.success()
        # An empty full snapshot is the most informative blob there is:
        # it says the job is finished. Only an empty incremental one has
        # nothing in it.
        if track.rows or track.full:
            event_bus.notify(
                MowerRemainingEvent(rows=track.rows, complete=track.full)
            )
        return HandlingResult.success()


class OnMapTrace(_MapMessage):
    """Coverage on firmware 1.17: the mowed area as filled outlines."""

    NAME = "onMapTrace"

    @classmethod
    def _notify(cls, event_bus: EventBus, blob: bytes) -> HandlingResult:
        covered = parse_map_trace(blob)
        event_bus.notify(
            MowerCoveredAreaEvent(areas=covered.areas, holes=covered.holes)
        )
        return HandlingResult.success()


class OnSpecialContour(_MapMessage):
    """User-defined no-go zones."""

    NAME = "onSpecialContour"

    @classmethod
    def _notify(cls, event_bus: EventBus, blob: bytes) -> HandlingResult:
        # Two sources for one complete-set event, which would otherwise
        # take turns overwriting each other. onArI wins where its section
        # 2 is validated: it reports per area and traced, while this
        # message has been seen carrying a single point for the very zone
        # the same mower names in full on onArI (issue #41). Where it is
        # not validated this is the source, and the only one.
        profile = profile_for_bus(event_bus)
        if profile is not None and profile.area_nogo:
            return HandlingResult.success()
        zones = parse_special_contour(blob)
        if zones is None:
            return HandlingResult.analyse()  # nothing readable in it
        event_bus.notify(MowerNoGoZonesEvent(zones=zones))
        return HandlingResult.success()


def reset_remaining_plan(event_bus: EventBus) -> None:
    """Publish the empty plan that says a finished job's remainder is gone.

    Called by the job-edge handler in ``messages.py`` on a stop edge, the
    only announcement there is that a job is over. Nothing else would
    say so: the mower pushes rows while it works and sends the whole plan
    only when asked, and the poll tick that would ask stops the moment it
    docks. So whatever was left at the last tick stayed drawn as uncut,
    and was persisted that way too — after an End, a rain stop or a
    return to charge, most of a lawn the mower never reached. An empty
    full plan is exactly how the mower itself reports a job it finished.

    Published rather than written into the map directly, even though the
    controller holds the map and could clear it from the same edge. The
    bus keeps the last event of each type and drops the next one equal to
    it, so a reset that never travelled the bus would leave the dropped
    plan cached: stop a job before the mower has mowed a row, start the
    same job again, and the identical plan is discarded as a duplicate
    and never reaches the map, which stays empty until a row is mowed and
    makes the next plan differ. Going through the bus also puts the reset
    in order with the rows that arrive after it, and leaves the map with
    one writer for the plan, ``on_remaining``.

    Only where ``onMapTrack`` is read as that plan; coverage survives a
    job by definition. The flag is read here because this module owns
    what ``onMapTrack`` means — see ``OnMapTrack``.
    """
    profile = profile_for_bus(event_bus)
    if profile is None or not profile.remaining_plan:
        return
    event_bus.notify(MowerRemainingEvent(rows={}, complete=True))
