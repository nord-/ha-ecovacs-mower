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
    """
    points: Polygon = []
    for part in spec.split(";"):
        if "," in part:
            x, y = part.split(",")
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
    """Static map geometry. None means "no update", never "empty"."""

    boundary: Polygon | None = None
    zones: list[Polygon] | None = None
    corridors: list[Polygon] | None = None


@dataclass(frozen=True)
class AreaInfo:
    """Decoded onArI payload."""

    map_info: MapInfo
    obstacles: list[Polygon] | None
    # No-go zones ride in onArI from firmware 1.17 on; up to 1.13 they
    # arrive in onSpecialContour and this stays None.
    nogo: list[Polygon] | None = None


@dataclass(frozen=True)
class MapTrack:
    """Decoded onMapTrack coverage. An empty segment list clears the row."""

    lanes: dict[tuple[str, int], list[Segment]]


@dataclass(frozen=True)
class CoveredArea:
    """Decoded onMapTrace: the mowed area, and the holes left in it."""

    areas: list[Polygon]
    holes: list[Polygon]


def parse_map_info(blob: bytes) -> MapInfo:
    """Parse onMI, in either dialect:

    ``[["1","s1;1;<x,y>;<chain code>"],["2","1"]]`` up to firmware 1.13,
    ``[["1","s1;<x,y>;<x,y>;…"],["2"]]`` from 1.17 on.

    Both have an idle variant carrying no geometry — ``s1;0;`` and a bare
    ``1`` respectively — which yields all-None.
    """
    boundary: Polygon | None = None
    for entry in json.loads(blob):
        if entry[0] != "1" or len(entry) < 2:
            continue
        fields = entry[1].split(";")
        if fields[0] != "s1" or len(fields) < 2:
            continue
        if "," in fields[1]:
            boundary = _points(entry[1])
        elif fields[1] == "1":
            boundary = chain_to_points(";".join(fields[2:]))
    return MapInfo(boundary=boundary)


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
    """
    if any(len(entry) == 2 for entry in entries):
        return True
    for entry in entries:
        for item in entry[2:]:
            fields = item.split(";")
            if len(fields) > 2 and "," in fields[2]:
                return True
    return False


def parse_area_info(blob: bytes) -> AreaInfo:
    """Parse onArI, in whichever dialect the firmware speaks."""
    entries = json.loads(blob)
    if _is_point_list_dialect(entries):
        return _parse_area_info_v117(entries)
    return _parse_area_info_chain(entries)


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

    Neither the lawn outline nor corridors have a section here: onMI
    still carries the outline, and no capture has shown corridors.
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


def _parse_area_info_chain(entries: list[list[str]]) -> AreaInfo:
    """Parse onArI up to 1.13: 1/2 zones, 3 obstacles, 5 boundary, 6 corridors.

    Entry format: ``["<mid>", "<section>", "<flag>", *items]`` where flag
    "0" means "no update" for that section — represented as None so stored
    geometry is never wiped by a heartbeat.
    """
    sections: dict[str, list[str]] = {}
    for entry in entries:
        if len(entry) > 2 and entry[2] == "1":
            sections[entry[1]] = entry[3:]

    # ponytail: sections 1 and 2 are merged into one zones list — every
    # captured onArI updates them together (both flag "1" or both "0").
    # If a capture ever shows one without the other, one section's zones
    # would wipe the other's: split them into separate None-able fields
    # like the rest at that point.
    zones: list[Polygon] | None = None
    if "1" in sections or "2" in sections:
        if ("1" in sections) != ("2" in sections):
            _LOGGER.debug(
                "onArI updated zone section %s without its pair; the "
                "sections-always-move-together assumption may be wrong",
                "1" if "1" in sections else "2",
            )
        zones = _polygons(sections.get("1", []) + sections.get("2", []))

    boundary: Polygon | None = None
    if "5" in sections:
        boundary_polygons = _polygons(sections["5"])
        boundary = boundary_polygons[0] if boundary_polygons else None

    corridors = _polygons(sections["6"]) if "6" in sections else None
    obstacles = _polygons(sections["3"]) if "3" in sections else None

    return AreaInfo(
        map_info=MapInfo(boundary=boundary, zones=zones, corridors=corridors),
        obstacles=obstacles,
    )


def parse_map_track(blob: bytes) -> MapTrack:
    """Parse onMapTrack lane records: ``<zone>;1;<row>[;x,y;x,y…]``.

    Coordinate pairs are mowed spans on a 100 mm row; a record without
    coordinates clears the row. Perimeter-trace records (kind 2) are
    ignored — the live position track covers that visual, and rendering
    them is not in scope (see spec, "layers").
    """
    lanes: dict[tuple[str, int], list[Segment]] = {}
    for entry in json.loads(blob):
        for record in entry[2:]:
            fields = record.split(";")
            if len(fields) < 3 or fields[1] != "1":
                continue
            points = [
                tuple(int(value) for value in part.split(","))
                for part in fields[3:]
                if "," in part
            ]
            lanes[(fields[0], int(fields[2]))] = list(
                zip(points[::2], points[1::2])
            )
    return MapTrack(lanes=lanes)


def parse_map_trace(blob: bytes) -> CoveredArea:
    """Parse onMapTrace: ``[["1","0;<x,y>;…"],["2","0;<x,y>;…", …],["3"]]``.

    Firmware 1.17's replacement for onMapTrack, and a different shape:
    section 1 is the outline of what has been mowed and section 2 the
    unmowed islands inside it, where onMapTrack sent spans per row.
    Section 3 was empty in all 3660 blobs of the issue-41 captures. Every
    blob is a complete snapshot, never an increment.
    """
    sections: dict[str, list[Polygon]] = {"1": [], "2": []}
    for entry in json.loads(blob):
        if (section := sections.get(entry[0])) is not None:
            section.extend(_points(record) for record in entry[1:])
    return CoveredArea(areas=sections["1"], holes=sections["2"])


def parse_special_contour(blob: bytes) -> list[Polygon]:
    """Parse onSpecialContour no-go zones: plain ``x,y;x,y;…;`` polygons."""
    return [_points(entry[4]) for entry in json.loads(blob)]
