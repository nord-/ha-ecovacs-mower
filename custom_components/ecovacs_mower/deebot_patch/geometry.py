"""Pure decoding of the GOAT map wire format.

No deebot-client or Home Assistant imports — this module must stay runnable
on any Python. The format was decoded live on 2026-08-10 from a GOAT O1200
and verified against the official app; the full wire-format description is
in docs/superpowers/specs/2026-08-10-mower-map-design.md (appendix).
"""

from __future__ import annotations

import base64
import lzma
import re
from collections import OrderedDict

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
