"""Tests for the pure map-format decoding.

Fixtures are real payloads captured 2026-08-10 from a GOAT O1200 (2i0fns,
fw 1.11.31) and verified against the official app's map; the ``_v117`` ones
were captured 2026-08-26 from two GOAT O800 RTK (2px96q) on firmware 1.17.8
and 1.17.11 (issue #41). No deebot-client or Home Assistant needed — this
file runs on Windows.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from custom_components.ecovacs_mower.deebot_patch.geometry import (
    STEP_MM,
    FragmentBuffer,
    chain_to_points,
    decompress,
    parse_area_info,
    parse_map_info,
    parse_map_trace,
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


def _point_set(record: str) -> set[tuple[int, int]]:
    """The distinct x,y of a record, ignoring any field past the second."""
    return {
        tuple(int(value) for value in part.split(",")[:2])
        for part in record.split(";")
        if "," in part
    }


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


def test_parse_area_info_logs_when_zone_sections_split(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Every captured onArI moves sections 1/2 together (both "1" or both
    # "0"). If firmware ever splits them, the merge below would silently
    # drop the other section's zones — this DEBUG line is the only trace.
    blob = json.dumps([["m", "1", "1", "10;0,0;2"]]).encode()
    with caplog.at_level("DEBUG"):
        area = parse_area_info(blob)
    assert area.map_info.zones is not None
    assert "without its pair" in caplog.text


def test_parse_area_info_does_not_log_when_sections_match(
    caplog: pytest.LogCaptureFixture,
) -> None:
    blob = json.dumps(
        [["m", "1", "1", "10;0,0;2"], ["m", "2", "1", "11;0,0;2"]]
    ).encode()
    with caplog.at_level("DEBUG"):
        parse_area_info(blob)
    assert "without its pair" not in caplog.text


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


# Firmware 1.17 (issue #41) sends the same blobs in a different dialect:
# explicit point lists instead of chain codes, no per-section update flag,
# and onMapTrace in place of onMapTrack. Fixtures suffixed _v117 are real
# payloads captured 2026-08-26 from two GOAT O800 RTK (2px96q), one on
# 1.17.8 and one on 1.17.11.


def test_parse_map_info_v117_reads_the_point_list_boundary() -> None:
    info = parse_map_info(_blob("on_mi_full_v117"))
    assert info.boundary is not None
    assert info.boundary[0] == (-10800, 7900)
    assert len(info.boundary) == 1117
    assert info.zones is None and info.corridors is None


def test_parse_map_info_v117_idle_carries_nothing() -> None:
    # The idle snapshot is a bare ["1","1"]. The 1.13 parser indexed past
    # the end of it, so the blob was dropped as undecodable (#41).
    info = parse_map_info(_blob("on_mi_idle_v117"))
    assert info.boundary is None
    assert info.zones is None and info.corridors is None


# ``on_map_info_v2_g1800`` is the seven-fragment ``onMapInfo_V2`` answer a
# GOAT G1-800 (77atlz, fw 1.36.208) gave on 2026-08-30, byte for byte, with
# only ``mid`` and the header timestamps replaced. The device sent the same
# 32137-byte blob three times that day and all three reassemble identically
# (issue #81).
def test_map_info_v2_reassembles_to_the_size_the_device_declared() -> None:
    # Seven fragments, and the blob that decides issue #81. Pinned before
    # anything is read out of it: a short reassembly would fail the tests
    # below in a way that looks like a parser bug.
    fragments = _fragments("on_map_info_v2_g1800")
    assert len(fragments) == 7
    blob = _blob("on_map_info_v2_g1800")
    assert len(blob) == fragments[0]["infoSize"] == 32137


def test_parse_map_info_reads_the_firmware_136_boundary() -> None:
    # The record leads with an id, not the "s1" marker of every earlier
    # dialect. Keyed on "s1" alone, the parser walked past this blob and
    # returned no boundary — 32 KB of lawn claimed and dropped (#81).
    info = parse_map_info(_blob("on_map_info_v2_g1800"))
    assert info.boundary is not None
    assert info.boundary[0] == (-11918, 1959)
    assert len(info.boundary) == 692
    assert info.zones is None and info.corridors is None
    # Same closure rule as every other dialect: within one grid step.
    end_x, end_y = info.boundary[-1]
    assert abs(end_x - info.boundary[0][0]) <= STEP_MM
    assert abs(end_y - info.boundary[0][1]) <= STEP_MM


def test_parse_map_info_drops_the_firmware_136_point_flag() -> None:
    # Every point is "<x>,<y>,1" here. Unpacking two values out of three
    # raises, and the raise is swallowed as an undecodable blob, so the
    # third field has to be dropped rather than merely ignored downstream.
    blob = _blob("on_map_info_v2_g1800")
    assert b";-11918,1959,1;" in blob
    boundary = parse_map_info(blob).boundary
    assert all(len(point) == 2 for point in boundary)


def test_map_info_v2_section_2_retraces_section_1() -> None:
    # Not read, and recorded so the fixture says why. Section 2 is the same
    # outline at the 50 mm step where section 1 collapses straight runs:
    # 1924 points against 692, and it contains every one of them. Section 1
    # is the boundary in every dialect, so that is what is published.
    sections = json.loads(_blob("on_map_info_v2_g1800"))
    coarse = _point_set(sections[0][1])
    fine = _point_set(sections[1][1])
    assert len(coarse) == 672 and len(fine) == 1909
    assert coarse <= fine


def test_map_info_v2_section_3_holds_the_islands() -> None:
    # Thirteen closed shapes inside the lawn, ids 100-116 — the same
    # numbering onArI's obstacle section uses. MapInfo has nowhere to put
    # them yet; the fixture keeps them for whoever wires them up.
    sections = json.loads(_blob("on_map_info_v2_g1800"))
    islands = sections[2][1:]
    assert len(islands) == 13
    assert [record.split(";")[0] for record in islands][:3] == [
        "100",
        "101",
        "102",
    ]
    assert sections[3] == ["4"] and sections[4] == ["5"]


def test_parse_area_info_v117_zones_obstacles_and_empty_nogo() -> None:
    area = parse_area_info(_blob("on_ari_v117_multipart"))
    assert [len(zone) for zone in area.map_info.zones] == [326, 462, 479]
    assert area.map_info.zones[0][0] == (-10800, 7900)
    assert len(area.obstacles) == 3
    # Section 2 with no items at all: this lawn has no no-go zones.
    assert area.nogo == []
    # 1.17 has no boundary or corridor section; onMI carries the outline.
    assert area.map_info.boundary is None
    assert area.map_info.corridors is None


def test_parse_area_info_v117_reads_nogo_zones() -> None:
    # Confirmed by the reporter: four no-go zones in the app, four rings
    # in section 2, each inside one of the four section 1 mowing zones.
    area = parse_area_info(_blob("on_ari_v117_nogo_zones"))
    assert len(area.map_info.zones) == 4
    assert [len(zone) for zone in area.nogo] == [166, 87, 91, 127]
    assert len(area.obstacles) == 29


def test_parse_area_info_v117_bare_ids_mean_no_update() -> None:
    # ["1","1","1","2","3"]: the three zone ids exist, none of them sent
    # geometry. Reading that as "no zones" would wipe the stored lawn.
    area = parse_area_info(_blob("on_ari_v117_ids_only"))
    assert area.map_info.zones is None
    assert len(area.obstacles) == 3


def test_parse_area_info_v117_all_ids_only_is_still_detected() -> None:
    # Same shape as on_ari_v117_ids_only, but section 3 is also ids-only —
    # nothing in the blob has a ";" for the coordinate scan to key off, so
    # detection must fall back to the 2-element ["mid","section"] entries.
    # Misdetecting this as 1.13 routes it through the chain parser, which
    # reads zone id "1" as a stray flag "1" and reports zones=[] instead of
    # None — wiping the stored lawn on what is really a no-op heartbeat.
    blob = json.dumps(
        [
            ["1", "1", "1", "2", "3"],
            ["1", "2"],
            ["1", "3", "100", "101", "102"],
            ["1", "4"],
        ]
    ).encode()
    area = parse_area_info(blob)
    assert area.map_info.zones is None
    assert area.obstacles is None
    assert area.nogo == []  # section 2 present, no items: no no-go zones


def test_parse_area_info_v113_obstacles_only_still_chain_parsed() -> None:
    # A 1.13 blob never has a 2-element entry (the flag at index 2 is
    # mandatory), so it must not trip the new v117 length-2 signal.
    area = parse_area_info(_blob("on_ari_obstacles_only"))
    assert area.map_info.zones is None
    assert len(area.obstacles) == 14


def test_parse_area_info_v117_trailing_separator_is_not_geometry() -> None:
    # "1;" is an id with a trailing separator and no points — same as a
    # bare id, not geometry. Counting it as a point would mark the section
    # updated (with empty polygons) instead of unchanged.
    blob = json.dumps([["m", "1", "1;", "2;"]]).encode()
    area = parse_area_info(blob)
    assert area.map_info.zones is None


def test_parse_area_info_v117_empty_section_1_is_no_update(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # No capture has an empty section 1 — every lawn has at least one zone
    # — so an empty section 1 is treated as "no update", not "zero zones",
    # unlike sections 2/3 where "no items" is a confirmed empty set.
    blob = json.dumps([["m", "1"]]).encode()
    with caplog.at_level("DEBUG"):
        area = parse_area_info(blob)
    assert area.map_info.zones is None
    assert area.nogo is None  # section 2 not sent at all: no update either
    assert "no items" in caplog.text


def test_parse_map_trace_single_ring() -> None:
    covered = parse_map_trace(_blob("on_map_trace_v117_single"))
    assert len(covered.areas) == 1
    assert covered.areas[0][0] == (-4800, -3900)
    assert len(covered.areas[0]) == 14
    assert covered.holes == []


def test_parse_map_trace_multipart_carries_holes() -> None:
    covered = parse_map_trace(_blob("on_map_trace_v117_multipart"))
    assert len(covered.areas) == 1
    assert len(covered.areas[0]) == 257
    assert [len(hole) for hole in covered.holes] == [8, 14, 24, 8, 8, 8]
