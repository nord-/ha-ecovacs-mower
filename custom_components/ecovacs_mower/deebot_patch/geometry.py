"""Pure decoding of the GOAT map wire format.

No deebot-client or Home Assistant imports — this module must stay runnable
on any Python. The format was decoded live on 2026-08-10 from a GOAT O1200
and verified against the official app; the full wire-format description is
in docs/superpowers/specs/2026-08-10-mower-map-design.md (appendix).
"""

from __future__ import annotations

import base64
import json
import logging
import lzma
import re
from collections import OrderedDict
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

STEP_MM = 50

type Point = tuple[int, int]
type Polygon = list[Point]
type Segment = tuple[Point, Point]

# Chain-code digits are the 8 king-move directions, clockwise from
# north-west. Verified by the boundary polygon closing with zero error —
# only one of the 16 possible direction mappings does.
_DIRECTIONS: dict[str, Point] = {
    "1": (-1, 1), "2": (0, 1), "3": (1, 1), "4": (1, 0),
    "5": (1, -1), "6": (0, -1), "7": (-1, -1), "8": (-1, 0),
}
_TOKEN = re.compile(r"(\d+)(?:\((\d+)\))?")


def decompress(info: str) -> bytes:
    """Decompress a map blob: base64 + LZMA_ALONE.

    The wire format omits 4 of LZMA_ALONE's 8 uncompressed-size header
    bytes; they are re-inserted as zeros at offset 8 — the same repair
    deebot-client's own ``decompress_7z_base64_data`` performs.
    """
    raw = bytearray(base64.b64decode(info))
    raw[8:8] = b"\x00\x00\x00\x00"
    return lzma.LZMADecompressor(lzma.FORMAT_ALONE).decompress(bytes(raw))


def _points(spec: str) -> Polygon:
    """Decode a plain ``x,y;x,y;…`` list into mm points.

    Parts without a comma are skipped: a leading id or kind marker, and
    the empty field a trailing ";" leaves behind.

    Fields past the second are dropped. Firmware 1.36 appends a third one
    to every boundary point of ``onMapInfo_V2`` — ``1`` on all 692 of the
    capture in issue #81 — and nothing downstream has a use for it yet.
    """
    points: Polygon = []
    for part in spec.split(";"):
        if "," in part:
            x, y = part.split(",")[:2]
            points.append((int(x), int(y)))
    return points


def chain_to_points(spec: str) -> Polygon:
    """Decode ``"x,y;<chain code>"`` into absolute mm points.

    ``(n)`` repeats the immediately preceding digit n times total. Digit
    "0" occurs as a marker in some traces and moves nothing.
    """
    start, _, code = spec.partition(";")
    x, y = (int(value) for value in start.split(","))
    points: Polygon = [(x, y)]
    for digits, count in _TOKEN.findall(code):
        expanded = digits[:-1] + digits[-1] * (int(count) if count else 1)
        for char in expanded:
            if char not in _DIRECTIONS:
                continue
            dx, dy = _DIRECTIONS[char]
            x += dx * STEP_MM
            y += dy * STEP_MM
            points.append((x, y))
    return points


class FragmentBuffer:
    """Reassemble multipart blobs.

    The wire format has no total-parts field; completion is detected by a
    decompression attempt succeeding at exactly ``info_size`` bytes.
    Bounded so a lost fragment cannot leak memory.
    """

    def __init__(self, max_batches: int = 16) -> None:
        self._batches: OrderedDict[str, dict[int, str]] = OrderedDict()
        self._max_batches = max_batches

    def add(
        self, batid: str, index: int, fragment: str, info_size: int
    ) -> bytes | None:
        """Add a fragment; return the decoded blob once complete, else None."""
        parts = self._batches.setdefault(batid, {})
        self._batches.move_to_end(batid)
        parts[index] = fragment
        while len(self._batches) > self._max_batches:
            self._batches.popitem(last=False)

        joined = "".join(parts[i] for i in sorted(parts))
        try:
            blob = decompress(joined)
        except (lzma.LZMAError, ValueError):
            # binascii.Error is a ValueError: a partial base64 string can
            # fail either at decode or at decompression. Both mean
            # "incomplete — wait for the next fragment".
            return None
        if len(blob) != info_size:
            return None
        del self._batches[batid]
        return blob


@dataclass(frozen=True)
class MapInfo:
    """Static map geometry. None means "no update", never "empty".

    ``corridors`` are the passages joining two mowed areas, which arrive
    on onMI rather than onArI; ``flush_edges`` are arcs of one area's own
    outline. The two were one field until the passages turned up.

    ``boundaries`` holds one lawn outline per mowed area. Every dialect
    reports exactly one, except a chain-dialect ``onArI`` from a mower
    whose map has more than one area.
    """

    boundaries: list[Polygon] | None = None
    zones: list[Polygon] | None = None
    flush_edges: list[Polygon] | None = None
    corridors: list[Polygon] | None = None


@dataclass(frozen=True)
class AreaInfo:
    """Decoded onArI payload."""

    map_info: MapInfo
    obstacles: list[Polygon] | None
    # Section 2: always in the 1.17 dialect, and in the chain dialect on a
    # class whose profile claims it. onSpecialContour carries no-go zones
    # too, up to firmware 1.13 — see parse_special_contour.
    nogo: list[Polygon] | None = None


@dataclass(frozen=True)
class MapTrack:
    """Decoded onMapTrack: lane stretches per 100 mm row.

    Deliberately neutral about what they are. On a class whose profile
    sets ``remaining_plan`` they are the stretches of the current job the
    mower has not reached yet — measured on an O800 RTK (``9bts2s``, fw
    1.13.8) while a job ran: one row's length fell 10.25 m -> 0.00 m over
    70 seconds at 0.33 m/s, mowing speed, and the row then vanished from
    the next snapshot. Across the same capture 1 row finished and was
    dropped, 1 shrank and none appeared, and the whole-map total only
    ever fell, 809.6 m -> 742.1 m. Coverage can only grow, so there this
    is its complement.

    Everywhere else they are read as coverage, which is what this
    integration has always done and what nobody has checked. ``OnMapTrack``
    is where the two readings part; the bytes are the same either way.

    An empty segment list is a row with nothing in it. ``full`` says the
    blob is the whole set rather than the single row the mower is working
    on, which only matters under the remaining-plan reading.
    """

    rows: dict[tuple[str, int], list[Segment]]
    full: bool


@dataclass(frozen=True)
class CoveredArea:
    """Decoded onMapTrace: the mowed area, and the holes left in it."""

    areas: list[Polygon]
    holes: list[Polygon]


def parse_map_info(blob: bytes) -> MapInfo:
    """Parse onMI, in any of three dialects:

    ``[["1","s1;1;<x,y>;<chain code>"],["2","1"]]`` up to firmware 1.13,
    ``[["1","s1;<x,y>;<x,y>;…"],["2"]]`` from 1.17 on, and
    ``[["1","<id>;<x,y,flag>;…"],["2",…],["3",…],["4"],["5"]]`` from 1.36,
    where the message is named ``onMapInfo_V2``.

    The first two have an idle variant carrying no geometry — ``s1;0;``
    and a bare ``1`` respectively — which yields all-None.

    Record 1 holds **one element per mowed area**, not one outline: the
    first is marked ``s1`` and the rest lead with the area's own id,
    otherwise identically shaped. A three-lawn GOAT O800 RTK (``9bts2s``,
    fw 1.13.8) sends ``s1;1;…``, ``2;1;…`` and ``3;1;…`` in it — 2420,
    1222 and 499 points (issue #41). Reading only the ``s1`` element
    dropped the other lawns here, and since the event replaces the stored
    set, an onMI arriving after an onArI took the map back down to one.

    1.36 drops the ``s1`` marker for a record id, appends a per-point
    flag, and adds sections. Only section 1 is read here. Section 2 is
    the same outline retraced at the 50 mm grid step — 1924 points to
    section 1's 692, every one of which it also contains — and section 3
    holds the islands inside the lawn, thirteen of them in the capture
    behind issue #81. Neither has a home in ``MapInfo`` yet, and section 1
    is the boundary in every dialect, so publishing more than it would be
    a new claim rather than a decode. The fixture carries all five.
    """
    boundaries: list[Polygon] = []
    corridors: list[Polygon] = []
    for entry in json.loads(blob):
        if len(entry) < 2:
            continue
        if entry[0] == "2":
            corridors.extend(_corridors(entry[1:]))
            continue
        if entry[0] != "1":
            continue
        for element in entry[1:]:
            fields = element.split(";")
            if len(fields) < 2:
                # 1.17's idle marker: the record is the bare id.
                continue
            if fields[1] == "1":
                # 1.13: "<id>;1;<x,y>;<chain code>", where the first
                # area's id is the marker "s1".
                outline = chain_to_points(";".join(fields[2:]))
            elif "," in fields[1]:
                # 1.17 and 1.36: points from the second field on, under
                # "s1" and under an id respectively.
                outline = _points(element)
            else:
                continue
            if outline:
                boundaries.append(outline)
    # Record 1 and record 2 arrive in the same reassembled blob, so a
    # blob that carries outlines is a full snapshot of both: an empty
    # record 2 there says the passages are gone, not that none were
    # reported. Only the idle variant, which carries no outline either,
    # is "no update" — otherwise a passage would outlive the lawn it
    # joins, in the map and in the store, with nothing able to clear it.
    return MapInfo(
        boundaries=boundaries or None,
        corridors=corridors if boundaries else (corridors or None),
    )


def _corridors(elements: list[str]) -> list[Polygon]:
    """Decode onMI record 2: ``<id>,<from>,<to>;<x,y>;<chain code>`` each.

    A three-lawn GOAT O800 RTK (``9bts2s``, fw 1.13.8) sends ``1,1,2``
    and ``2,2,3`` there, 11 and 79 points long, and the two ids in each
    header are exactly the areas whose outlines the ends touch — 0 mm
    from both (issue #41). Record 2 was documented as a bare marker,
    which is all a single-area map puts in it.

    The header is also what tells a corridor from everything else a
    record 2 has held: it carries commas, where every other dialect leads
    with a plain id — firmware 1.36's retrace of the outline (``129``) and
    its islands (``100``…) included, which must not be read as passages.
    """
    corridors: list[Polygon] = []
    for element in elements:
        fields = element.split(";")
        if len(fields) < 3 or "," not in fields[0]:
            continue
        if points := chain_to_points(";".join(fields[1:])):
            corridors.append(points)
    return corridors


def _polygons(items: list[str]) -> list[Polygon]:
    """Decode ``<id>;<x,y>;<chain>`` items, skipping id-only entries."""
    result: list[Polygon] = []
    for item in items:
        _, _, spec = item.partition(";")
        if spec:
            result.append(chain_to_points(spec))
    return result


def _is_point_list_dialect(entries: list[list[str]]) -> bool:
    """True for the 1.17 dialect, which sends point lists, not chain codes.

    A 2-element entry — ``[<mid>, <section>]``, no items — is conclusive on
    its own: 1.13 entries always carry a flag at index 2, so they are never
    shorter than 3. This catches a 1.17 blob where every section is either
    empty or ids-only, which otherwise carries no ";" anywhere for the
    coordinate scan below to find.

    Past that, an item is ``<id>;<x,y>;<chain code>`` up to firmware 1.13
    and ``<id>;<x,y>;<x,y>;…`` from 1.17 on, so the third field decides.
    Scanning from index 2 covers both layouts: 1.13's section flag sits
    there but holds no ";", so it never matches.

    Section 4 is left out of that scan. It is empty in every capture the
    rest of this parser was written from, but an O800 RTK on firmware
    1.13.8 fills it with point lists — ``<id>;<x,y>;<x,y>;…`` — while
    every other section of the same blob is chain-coded. Scanning it
    reports the 1.17 dialect for a 1.13 blob, and only on the heartbeats
    where section 4 carries an update, so one mower flips dialect from
    blob to blob. It can discriminate nothing anyway: no 1.17 capture has
    content there either, and sections 1-3 carry the point lists that
    identify that dialect.
    """
    if any(len(entry) == 2 for entry in entries):
        return True
    for entry in entries:
        if len(entry) > 1 and entry[1] == "4":
            continue
        for item in entry[2:]:
            fields = item.split(";")
            if len(fields) > 2 and "," in fields[2]:
                return True
    return False


def parse_area_info(blob: bytes, area_nogo: bool = False) -> AreaInfo:
    """Parse onArI, in whichever dialect the firmware speaks.

    *area_nogo* says whether this device's section 2 has been confirmed to
    hold its no-go zones; see ``MowerProfile.area_nogo``. It reaches only
    the chain dialect, where the alternative is the merge into the mowing
    zones that every class had before: the 1.17 dialect has read section 2
    as the no-go set since before there was a flag, and nothing here
    changes that.
    """
    entries = json.loads(blob)
    if _is_point_list_dialect(entries):
        return _parse_area_info_v117(entries)
    return _parse_area_info_chain(entries, area_nogo)


# Section 4 is unused in every capture this parser was written from; the
# DEBUG line below asks about it once per process instead of once per blob
# (it repeats every heartbeat once a firmware does populate it).
_section_4_reported = False


def _parse_area_info_v117(entries: list[list[str]]) -> AreaInfo:
    """Parse 1.17 onArI: ``["<mid>","<section>","<id>;<x,y>;<x,y>;…", …]``.

    Sections are 1 mowing zones, 2 no-go zones, 3 obstacles, 4 unused.
    The per-section update flag is gone; an item is either ``<id>`` alone
    — the id exists, its geometry did not change — or an id followed by
    its points. A section whose items all lack geometry is therefore "no
    update", while a section with no items at all is "there are none" —
    except section 1: no capture has an empty one, since a mapped lawn
    never legitimately has zero zones, so an empty section 1 is treated
    as "no update" too rather than risk wiping the lawn on an unconfirmed
    shape.

    Neither the lawn outline nor the flush-mow edges have a section
    here: onMI still carries the outline, and no 1.17 capture has shown
    anything in section 6.
    """
    sections: dict[str, list[Polygon]] = {}
    for entry in entries:
        items = entry[2:]
        polygons = [p for p in (_points(item) for item in items) if p]
        if not items and entry[1] == "1":
            _LOGGER.debug(
                "onArI section 1 arrived with no items; treating it as "
                "no-update rather than zero zones, since no capture has "
                "confirmed that shape. Please report if this is wrong."
            )
            continue
        if items and not polygons:
            continue  # ids only: this section did not change
        sections[entry[1]] = polygons
    global _section_4_reported
    if sections.get("4") and not _section_4_reported:
        _section_4_reported = True
        _LOGGER.debug(
            "onArI section 4 carried %d polygons; it was empty in every "
            "capture this parser was written from",
            len(sections["4"]),
        )
    return AreaInfo(
        map_info=MapInfo(zones=sections.get("1")),
        obstacles=sections.get("3"),
        nogo=sections.get("2"),
    )


def _parse_area_info_chain(
    entries: list[list[str]], area_nogo: bool
) -> AreaInfo:
    """Parse onArI up to 1.13: 1/2 zones, 3 obstacles, 5 outlines, 6 edges.

    Entry format: ``["<area>", "<section>", "<flag>", *items]`` where flag
    "0" means "no update" for that section — represented as None so stored
    geometry is never wiped by a heartbeat.

    **The leading field is the area id, not the map id.** The map is named
    by the message envelope, and a mower whose map holds three lawns sends
    one entry per (area, section) pair, so the same section number arrives
    three times. Every capture this parser was written from had a single
    area, where the two readings are indistinguishable; a GOAT O800 RTK
    (``9bts2s``, fw 1.13.8) with three lawns is what told them apart
    (issue #41). Keyed by section alone, each area's entry replaced the one
    before it and only the last area survived — for the outline that meant
    two thirds of the map silently disappeared.

    Section 6 holds the stretches of edge the mower is told to mow flush
    against. On that mower all 230, 106 and 186 points of its three
    records lie exactly on the outline of area 1 — every one — and its
    owner drew those stretches in the app for that purpose. They are arcs
    of the boundary, which is why the renderer leaves them open. It was
    called "corridors" here until then, from a guess made when no capture
    had anything in the section; the passages between lawns are a
    different thing and arrive on onMI.

    An area's items are therefore accumulated into their section instead of
    replacing it. One consequence is worth naming: a section is published
    as soon as *any* area updates it, and what is published then holds only
    the areas that sent geometry in that blob, because the events carry
    complete sets. Both blobs captured from that mower move a section's
    flag together across all three areas — one full snapshot, one
    obstacles-only heartbeat — so an area updating a section on its own is
    not a shape anything has shown. Keeping per-area state all the way into
    ``MowerMap`` is what would make it impossible, and that is a much
    larger change than this one.
    """
    sections: dict[str, list[str]] = {}
    for entry in entries:
        if len(entry) > 2 and entry[2] == "1":
            sections.setdefault(entry[1], []).extend(entry[3:])

    # Section 2 holds the no-go zones on a class that says so, as it
    # already does in the 1.17 dialect. The GOAT O800 RTK of issue #41 is
    # the one such class: of its three areas, the one carrying a
    # section-2 ring is the one its owner has a no-go zone in, and the
    # other two carry nothing. That is about position, not size — a small
    # ring passes just as well for a small mowing zone, which is why the
    # O1200's section-2 point counts prove nothing either way and are not
    # part of the argument.
    #
    # Everywhere else the section joins section 1's mowing zones, which is
    # what every class did before one of them was checked. The O1200
    # LiDAR Pro is why: its section 2 holds two rings inside one of the
    # two rectangles its own onSpecialContour calls a no-go zone, and
    # says nothing at all about the other, so reading it as the no-go set
    # there would drop a real zone (PR #136 review). Keeping the merge
    # leaves that mower exactly as it was.
    zones = _polygons(sections["1"]) if "1" in sections else None
    nogo = _polygons(sections["2"]) if "2" in sections else None
    if not area_nogo:
        if zones is None:
            zones = nogo
        elif nogo is not None:
            zones = zones + nogo
        nogo = None

    boundaries: list[Polygon] | None = None
    if "5" in sections:
        # An empty section 5 means "no update", not "no lawn": a heartbeat
        # that carried no geometry must never wipe a stored outline.
        boundaries = _polygons(sections["5"]) or None

    flush_edges = _polygons(sections["6"]) if "6" in sections else None
    obstacles = _polygons(sections["3"]) if "3" in sections else None

    return AreaInfo(
        map_info=MapInfo(
            boundaries=boundaries, zones=zones, flush_edges=flush_edges
        ),
        obstacles=obstacles,
        nogo=nogo,
    )


def parse_map_track(blob: bytes) -> MapTrack:
    """Parse onMapTrack lane records: ``<zone>;1;<row>[;x,y;x,y…]``.

    The coordinates come in pairs, each the two ends of a stretch of that
    row still to mow — see ``MapTrack`` for why "still to mow" rather
    than "mowed". Point counts are always even: over one 247-record
    snapshot, 172 records carried 2 points, 52 carried 4, 9 carried 6 and
    5 carried 8, which is a row crossed by an obstacle reported as two or
    more stretches. Rows are 100 mm apart, and a row's two ends need not
    share a y — the mower lays its lanes at the configured cutting
    direction, so a span can run at any angle.

    A record without coordinates is a row with nothing left in it, and a
    row the mower has finished is simply absent from the next snapshot —
    which is why ``full`` matters. ``entry[1]`` says which kind of blob
    this is: ``"1"`` is the whole remaining plan, as sent in answer to
    ``getMapTrack``, and ``"2"`` is the single row the mower is working
    on, pushed as it goes. A full blob replaces the stored set; an
    incremental one merges into it. Merging a full one instead leaves
    every finished row in place for ever — on a real map that grew the
    stored plan to 266 stretches against the mower's own 114.

    Perimeter-trace records (kind 2 in ``fields[1]``, not to be confused
    with the blob kind above) are ignored — one point each, and not part
    of the plan.
    """
    rows: dict[tuple[str, int], list[Segment]] = {}
    full = False
    for entry in json.loads(blob):
        full = full or (len(entry) > 1 and entry[1] == "1")
        for record in entry[2:]:
            fields = record.split(";")
            if len(fields) < 3 or fields[1] != "1":
                continue
            points = [
                tuple(int(value) for value in part.split(","))
                for part in fields[3:]
                if "," in part
            ]
            rows[(fields[0], int(fields[2]))] = list(
                zip(points[::2], points[1::2])
            )
    return MapTrack(rows=rows, full=full)


def parse_map_trace(blob: bytes) -> CoveredArea:
    """Parse onMapTrace: ``[["1","0;<x,y>;…"],["2","0;<x,y>;…", …],["3", …]]``.

    Firmware 1.17's replacement for onMapTrack, and a different shape:
    section 1 is the outline of what has been mowed and section 2 the
    unmowed islands inside it, where onMapTrack sent spans per row. Every
    blob is a complete snapshot, never an increment.

    **A border job moves the live outline into section 3** and leaves
    section 1 holding the batch id alone — one record per run, newest
    first, renumbered as runs are added. An auto mow on the same device
    and firmware keeps the classic section-1 form throughout, so this is
    a per-task shape rather than a firmware dialect, and coverage of a
    border job had never worked (GOAT G1-800, fw 1.36.208; the captures
    and the auto-versus-border comparison are on issue #52). Sections 1
    and 3 share a coordinate frame — an idle snapshot and a mid-job
    record of the same capture carry a verbatim six-point run — so both
    feed ``areas``. Section 3 was empty in all 3660 blobs of the issue-41
    captures because those were taken on an idle mower.

    Records carry a leading id that ``_points`` drops, so an id-only
    record decodes to an empty polygon. Those are discarded from both
    lists rather than kept: an id-only section 1 is what a border job
    sends, and an ``areas=[[]]`` that never changes makes the event bus
    dedup every later blob against the first, freezing the coverage layer
    for the length of the job. The filter is the fix, not tidiness. No
    captured blob holds an id-only hole record, so section 2 is covered
    for symmetry rather than against a known shape.
    """
    sections: dict[str, list[Polygon]] = {"1": [], "2": [], "3": []}
    for entry in json.loads(blob):
        if (section := sections.get(entry[0])) is not None:
            section.extend(_points(record) for record in entry[1:])
    return CoveredArea(
        areas=[points for points in sections["1"] + sections["3"] if points],
        holes=[points for points in sections["2"] if points],
    )


def parse_special_contour(blob: bytes) -> list[Polygon] | None:
    """Parse onSpecialContour no-go zones: plain ``x,y;x,y;…;`` polygons.

    A record carrying fewer than three points is not a polygon and is
    dropped. A GOAT O800 RTK (``9bts2s``, fw 1.13.8) keeps its no-go zone
    in onArI section 2 and pushes this on onSpecialContour regardless:

        [["1","1","2","-1828859640,-1828859302","-22813,13082;"]]

    Field 4 is one point, and field 3 is not coordinates at all.

    ``None`` means "no update", the same as everywhere else here, and is
    what a blob of records that all fail that test decodes to: the event
    carries the complete no-go set, so publishing the empty remainder
    would wipe whatever the mower had reported before it — which is how
    that single point became an entire no-go layer (issue #41). An empty
    list is still "there are none", and only a blob that really holds no
    records decodes to one.
    """
    records = json.loads(blob)
    contours = (_points(entry[4]) for entry in records)
    polygons = [points for points in contours if len(points) >= 3]
    if records and not polygons:
        return None
    return polygons
