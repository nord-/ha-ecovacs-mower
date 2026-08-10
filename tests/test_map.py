"""Tests for the in-memory map model. Pure Python — runs on Windows."""

from __future__ import annotations

from custom_components.ecovacs_mower.map import TRACK_MAX_POINTS, MowerMap

BOUNDARY = [(0, 0), (1000, 0), (1000, 1000), (0, 1000)]


def test_none_fields_leave_previous_values_untouched() -> None:
    mower_map = MowerMap()
    mower_map.update_map_info(BOUNDARY, [[(1, 1)]], None)
    # An idle heartbeat: boundary only, zones/corridors None.
    mower_map.update_map_info(None, None, None)
    assert mower_map.boundary == BOUNDARY
    assert mower_map.zones == [[(1, 1)]]


def test_coverage_merges_per_row() -> None:
    mower_map = MowerMap()
    mower_map.update_coverage({("1", 5): [((0, 0), (0, 100))]})
    mower_map.update_coverage({("1", 6): [((100, 0), (100, 100))]})
    # Resent row replaces its previous extent.
    mower_map.update_coverage({("1", 5): [((0, 0), (0, 500))]})
    assert mower_map.lanes == {
        ("1", 5): [((0, 0), (0, 500))],
        ("1", 6): [((100, 0), (100, 100))],
    }


def test_position_updates_marker_heading_and_track() -> None:
    mower_map = MowerMap()
    mower_map.update_position(-6895, -410, 159)
    assert mower_map.position == (-6895, -410)
    assert mower_map.heading == 159
    assert mower_map.track == [(-6895, -410)]


def test_track_thins_the_old_half_when_full() -> None:
    mower_map = MowerMap()
    for i in range(TRACK_MAX_POINTS + 1):
        mower_map.update_position(i, 0, 0)
    assert len(mower_map.track) < TRACK_MAX_POINTS
    # The most recent half stays dense.
    dense = mower_map.track[-(TRACK_MAX_POINTS // 2):]
    assert [x for x, _ in dense[-3:]] == [
        TRACK_MAX_POINTS - 2, TRACK_MAX_POINTS - 1, TRACK_MAX_POINTS,
    ]


def test_round_trip_through_dict() -> None:
    mower_map = MowerMap()
    mower_map.update_map_info(BOUNDARY, [[(1, 2), (3, 4)]], [])
    mower_map.update_obstacles([[(5, 6)]])
    mower_map.update_nogo([[(7, 8)]])
    mower_map.update_coverage({("1", 5): [((0, 0), (0, 100))]})
    mower_map.update_position(1, 2, 3)

    # Simulate the JSON round trip Store performs (tuples become lists).
    import json

    data = json.loads(json.dumps(mower_map.as_dict()))
    restored = MowerMap.from_dict(data)

    assert restored.boundary == BOUNDARY
    assert restored.zones == [[(1, 2), (3, 4)]]
    assert restored.obstacles == [[(5, 6)]]
    assert restored.nogo_zones == [[(7, 8)]]
    assert restored.lanes == {("1", 5): [((0, 0), (0, 100))]}
    # Track and position are deliberately not persisted.
    assert restored.track == [] and restored.position is None


def test_is_empty() -> None:
    mower_map = MowerMap()
    assert mower_map.is_empty
    mower_map.update_position(0, 0, 0)  # position alone is not a map
    assert mower_map.is_empty
    mower_map.update_map_info(BOUNDARY, None, None)
    assert not mower_map.is_empty
