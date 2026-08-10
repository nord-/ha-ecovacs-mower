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
    FragmentBuffer,
    chain_to_points,
    decompress,
    parse_area_info,
    parse_map_info,
    parse_map_track,
    parse_special_contour,
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


def _fragments(key: str) -> list[dict[str, Any]]:
    """All fragments for a multipart fixture, sorted by index."""
    return sorted(
        (item["payload"]["body"]["data"] for item in FIXTURES[key]),
        key=lambda d: int(d["index"]),
    )


def test_fragment_buffer_reassembles_multipart_blob() -> None:
    first, second = _fragments("on_ari_multipart")
    buffer = FragmentBuffer()
    assert (
        buffer.add(first["batid"], 0, first["info"], first["infoSize"]) is None
    )
    blob = buffer.add(second["batid"], 1, second["info"], second["infoSize"])
    assert blob is not None
    assert len(blob) == first["infoSize"] == 4609


def test_fragment_buffer_handles_out_of_order_arrival() -> None:
    first, second = _fragments("on_ari_multipart")
    buffer = FragmentBuffer()
    assert (
        buffer.add(second["batid"], 1, second["info"], second["infoSize"])
        is None
    )
    blob = buffer.add(first["batid"], 0, first["info"], first["infoSize"])
    assert blob is not None
    assert len(blob) == 4609


def test_fragment_buffer_single_part_completes_immediately() -> None:
    data = fixture_data("on_mi_full")
    buffer = FragmentBuffer()
    blob = buffer.add(data["batid"], 0, data["info"], data["infoSize"])
    assert blob is not None and len(blob) == 1350


def test_fragment_buffer_completed_batch_is_dropped() -> None:
    data = fixture_data("on_mi_full")
    buffer = FragmentBuffer()
    buffer.add(data["batid"], 0, data["info"], data["infoSize"])
    # A resend of the same batid starts a fresh batch, not a stale merge.
    blob = buffer.add(data["batid"], 0, data["info"], data["infoSize"])
    assert blob is not None and len(blob) == 1350


def test_fragment_buffer_evicts_oldest_when_full() -> None:
    first, second = _fragments("on_ari_multipart")
    buffer = FragmentBuffer(max_batches=2)
    buffer.add("stale", 0, first["info"], first["infoSize"])
    buffer.add("newer", 0, first["info"], first["infoSize"])
    buffer.add("newest", 0, first["info"], first["infoSize"])  # evicts "stale"
    # Completing "stale" now cannot succeed: its first fragment is gone.
    assert (
        buffer.add("stale", 1, second["info"], second["infoSize"]) is None
    )


def _blob(key: str) -> bytes:
    """Decode a fixture, joining fragments when multipart."""
    fragments = _fragments(key)
    return decompress("".join(f["info"] for f in fragments))


def test_parse_map_info_extracts_boundary() -> None:
    info = parse_map_info(_blob("on_mi_full"))
    assert info.boundary is not None
    assert info.boundary[0] == (-31000, 1800)
    assert len(info.boundary) == 2415
    assert info.zones is None and info.corridors is None


def test_parse_map_info_idle_carries_nothing() -> None:
    info = parse_map_info(_blob("on_mi_idle"))
    assert info.boundary is None
    assert info.zones is None and info.corridors is None


def test_parse_area_info_full_snapshot() -> None:
    area = parse_area_info(_blob("on_ari_multipart"))
    assert area.map_info.boundary is not None
    assert area.map_info.boundary[0] == (-31000, 1800)
    assert len(area.map_info.zones) == 5  # sections 1 + 2
    assert len(area.map_info.corridors) == 3  # section 6
    assert len(area.obstacles) == 15  # section 3, ids 100-114


def test_parse_area_info_zero_sections_mean_no_update() -> None:
    area = parse_area_info(_blob("on_ari_obstacles_only"))
    assert area.map_info.boundary is None
    assert area.map_info.zones is None
    assert area.map_info.corridors is None
    assert len(area.obstacles) == 14


def test_parse_map_track_single_lane() -> None:
    track = parse_map_track(_blob("on_map_track_single_lane"))
    assert track.lanes == {("3", 67): [((-26825, 2400), (-26825, 4199))]}


def test_parse_map_track_bare_record_clears_the_row() -> None:
    track = parse_map_track(_blob("on_map_track_multipart"))
    assert track.lanes[("1", 36)] == []  # record "1;1;36" — no coordinates
    assert track.lanes[("1", 41)] == [
        ((-30850, 4025), (-28000, 4025))
    ]


def test_parse_special_contour() -> None:
    polygons = parse_special_contour(_blob("on_special_contour"))
    assert len(polygons) == 2
    assert polygons[0] == [
        (-29233, 1843), (-28568, 2576), (-27815, 1891), (-28481, 1158)
    ]
