"""Tests for the pure map-format decoding.

Fixtures are real payloads captured 2026-08-10 from a GOAT O1200 (2i0fns,
fw 1.11.31) and verified against the official app's map. No deebot-client
or Home Assistant needed — this file runs on Windows.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from custom_components.ecovacs_mower.deebot_patch.geometry import (
    STEP_MM,
    chain_to_points,
    decompress,
)

FIXTURES: dict[str, list[Any]] = json.loads(
    (Path(__file__).parent / "fixtures" / "map_capture.json").read_text()
)


def fixture_data(key: str, index: int = 0) -> dict[str, Any]:
    """Return message->body->data for a captured payload."""
    return FIXTURES[key][index]["payload"]["body"]["data"]


def test_decompress_real_payload_matches_info_size() -> None:
    data = fixture_data("on_mi_full")
    blob = decompress(data["info"])
    assert len(blob) == data["infoSize"] == 1350
    assert blob.startswith(b'[["1","s1;1;-31000,1800;')


def test_chain_code_decodes_the_real_boundary() -> None:
    blob = json.loads(decompress(fixture_data("on_mi_full")["info"]))
    # entry format: ["1", "s1;1;<x,y>;<chain code>"]
    spec = blob[0][1].split(";", 2)[2]
    points = chain_to_points(spec)

    assert points[0] == (-31000, 1800)
    assert len(points) == 2415
    # Consecutive snapshots differ by a step or two; the loop closes to
    # within one grid step, never exactly.
    end_x, end_y = points[-1]
    assert abs(end_x - points[0][0]) <= STEP_MM
    assert abs(end_y - points[0][1]) <= STEP_MM
    # Every move is a king move on the 50 mm grid.
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        assert abs(x2 - x1) <= STEP_MM and abs(y2 - y1) <= STEP_MM


def test_chain_code_repeat_applies_to_last_digit_only() -> None:
    # "56(3)" is one SE step followed by three S steps — NOT (5,6) x3.
    points = chain_to_points("0,0;56(3)")
    assert points == [(0, 0), (50, -50), (50, -100), (50, -150), (50, -200)]


def test_chain_code_skips_the_zero_marker() -> None:
    # Digit 0 occurs as a marker in some traces and moves nothing.
    assert chain_to_points("100,200;2") == [(100, 200), (100, 250)]
    assert chain_to_points("100,200;02") == [(100, 200), (100, 250)]
