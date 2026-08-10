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


@dataclass(frozen=True)
class MapTrack:
    """Decoded onMapTrack coverage. An empty segment list clears the row."""

    lanes: dict[tuple[str, int], list[Segment]]


def parse_map_info(blob: bytes) -> MapInfo:
    """Parse onMI: ``[["1","s1;1;<x,y>;<chain>"],["2","1"]]``.

    The idle variant ``s1;0;`` carries no geometry and yields all-None.
    """
    boundary: Polygon | None = None
    for entry in json.loads(blob):
        fields = entry[1].split(";")
        if entry[0] == "1" and fields[0] == "s1" and fields[1] == "1":
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


def parse_area_info(blob: bytes) -> AreaInfo:
    """Parse onArI sections: 1/2 zones, 3 obstacles, 5 boundary, 6 corridors.

    Entry format: ``["<mid>", "<section>", "<flag>", *items]`` where flag
    "0" means "no update" for that section — represented as None so stored
    geometry is never wiped by a heartbeat.
    """
    sections: dict[str, list[str]] = {}
    for entry in json.loads(blob):
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


def parse_special_contour(blob: bytes) -> list[Polygon]:
    """Parse onSpecialContour no-go zones: plain ``x,y;x,y;…;`` polygons."""
    result: list[Polygon] = []
    for entry in json.loads(blob):
        result.append(
            [
                tuple(int(value) for value in part.split(","))
                for part in entry[4].rstrip(";").split(";")
                if part
            ]
        )
    return result
