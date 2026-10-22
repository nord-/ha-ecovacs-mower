"""Tests for the in-memory map model. Pure Python — runs on Windows."""

from __future__ import annotations

from custom_components.ecovacs_mower.map import TRACK_MAX_POINTS, MowerMap

BOUNDARY = [(0, 0), (1000, 0), (1000, 1000), (0, 1000)]


def test_none_fields_leave_previous_values_untouched() -> None:
    mower_map = MowerMap()
    mower_map.update_map_info([BOUNDARY], [[(1, 1)]], None)
    # An idle heartbeat: boundary only, zones/flush_edges None.
    mower_map.update_map_info(None, None, None)
    assert mower_map.boundaries == [BOUNDARY]
    assert mower_map.zones == [[(1, 1)]]


def test_coverage_merges_per_row() -> None:
    mower_map = MowerMap()
    mower_map.update_remaining({("1", 5): [((0, 0), (0, 100))]})
    mower_map.update_remaining({("1", 6): [((100, 0), (100, 100))]})
    # Resent row replaces its previous extent.
    mower_map.update_remaining({("1", 5): [((0, 0), (0, 500))]})
    assert mower_map.remaining == {
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
    mower_map = MowerMap(remaining_plan=True)
    mower_map.update_map_info(
        [BOUNDARY], [[(1, 2), (3, 4)]], [], [[(9, 9), (9, 19)]]
    )
    mower_map.update_obstacles([[(5, 6)]])
    mower_map.update_nogo([[(7, 8)]])
    mower_map.update_remaining({("1", 5): [((0, 0), (0, 100))]})
    mower_map.update_labels([((11, 12), "Hinten")])
    mower_map.update_position(1, 2, 3)

    # Simulate the JSON round trip Store performs (tuples become lists).
    import json

    data = json.loads(json.dumps(mower_map.as_dict()))
    restored = MowerMap.from_dict(data, remaining_plan=True)

    assert restored.boundaries == [BOUNDARY]
    assert restored.zones == [[(1, 2), (3, 4)]]
    assert restored.corridors == [[(9, 9), (9, 19)]]
    assert restored.obstacles == [[(5, 6)]]
    assert restored.nogo_zones == [[(7, 8)]]
    assert restored.remaining == {("1", 5): [((0, 0), (0, 100))]}
    assert restored.labels == [((11, 12), "Hinten")]
    # Track and position are deliberately not persisted.
    assert restored.track == [] and restored.position is None


def test_a_legacy_lanes_snapshot_is_ignored_under_the_plan_reading() -> None:
    """The old key held every row ever reported, merged forever.

    Read back as the remaining plan it would claim the whole season's
    mowing is still to cut, and keep claiming it until a full plan
    arrives — which needs the mower out and a poll tick. An empty layer
    is the honest version of not knowing.
    """
    restored = MowerMap.from_dict(
        {
            "boundaries": [[[0, 0], [10, 0], [10, 10]]],
            "lanes": [["1", 5, [[[0, 0], [0, 100]]]]],
        },
        remaining_plan=True,
    )

    assert restored.boundaries == [[(0, 0), (10, 0), (10, 10)]]
    assert restored.remaining == {}


def test_a_lanes_snapshot_still_loads_under_the_coverage_reading() -> None:
    # The counter-proof: on a class that never got the new reading the
    # key means what it always meant, and dropping it would throw away a
    # working layer in the name of fixing another class's bug.
    restored = MowerMap.from_dict(
        {"lanes": [["1", 5, [[[0, 0], [0, 100]]]]]}
    )

    assert restored.lanes == {("1", 5): [((0, 0), (0, 100))]}
    assert restored.remaining == {}


def test_each_reading_writes_only_its_own_key() -> None:
    # Writing both would leave the old key growing in a plan-reading
    # snapshot, and a downgrade would read it back as the wrong thing.
    plan = MowerMap(remaining_plan=True)
    plan.update_remaining({("1", 5): [((0, 0), (0, 100))]})
    coverage = MowerMap()
    coverage.update_coverage({("1", 5): [((0, 0), (0, 100))]})

    assert "lanes" not in plan.as_dict()
    assert plan.as_dict()["remaining"] == [["1", 5, [((0, 0), (0, 100))]]]
    assert "remaining" not in coverage.as_dict()
    assert coverage.as_dict()["lanes"] == [["1", 5, [((0, 0), (0, 100))]]]


def test_a_legacy_snapshot_s_one_outline_is_wrapped() -> None:
    # Snapshots written before a map could hold more than one outline
    # store a single polygon under "boundary". The store version is
    # unchanged, so those are the snapshots on disk after an upgrade.
    restored = MowerMap.from_dict({"boundary": [[0, 0], [10, 0], [10, 10]]})
    assert restored.boundaries == [[(0, 0), (10, 0), (10, 10)]]


def test_a_legacy_snapshot_s_corridors_are_the_flush_edges() -> None:
    # Section 6 was stored under "corridors" before it was named for the
    # flush-mow edges it holds, and that is what is on disk after an
    # upgrade.
    restored = MowerMap.from_dict({"corridors": [[[0, 0], [10, 0]]]})
    assert restored.flush_edges == [[(0, 0), (10, 0)]]
    # And nothing lands in the passages, which that snapshot never held.
    assert restored.corridors == []


def test_is_empty() -> None:
    mower_map = MowerMap()
    assert mower_map.is_empty
    mower_map.update_position(0, 0, 0)  # position alone is not a map
    assert mower_map.is_empty
    mower_map.update_map_info([BOUNDARY], None, None)
    assert not mower_map.is_empty


def test_is_empty_false_for_nogo_only() -> None:
    # A store restored from a session where only onSpecialContour arrived —
    # no boundary/zones/lanes yet — must not render as the empty placeholder.
    mower_map = MowerMap()
    mower_map.update_nogo([[(0, 0)]])
    assert not mower_map.is_empty


def test_is_empty_false_for_obstacles_only() -> None:
    mower_map = MowerMap()
    mower_map.update_obstacles([[(0, 0)]])
    assert not mower_map.is_empty


def test_is_empty_false_for_flush_edges_only() -> None:
    # Section 6 alone is still a map worth drawing.
    mower_map = MowerMap()
    mower_map.update_map_info(None, None, [[(0, 0)]])
    assert not mower_map.is_empty


def test_covered_area_replaces_the_previous_snapshot() -> None:
    # Every onMapTrace blob is a complete snapshot of the mowed area, not
    # an increment: the ring is re-simplified and can lose points.
    mower_map = MowerMap()
    mower_map.update_covered([[(0, 0), (100, 0), (100, 100)]], [[(10, 10)]])
    mower_map.update_covered([[(0, 0), (200, 0)]], [])
    assert mower_map.covered == [[(0, 0), (200, 0)]]
    assert mower_map.covered_holes == []


def test_is_empty_false_for_covered_area_only() -> None:
    mower_map = MowerMap()
    mower_map.update_covered([[(0, 0), (100, 0), (100, 100)]], [])
    assert not mower_map.is_empty


def test_round_trip_keeps_the_covered_area() -> None:
    import json

    mower_map = MowerMap()
    mower_map.update_covered([[(0, 0), (100, 0)]], [[(10, 10), (20, 20)]])
    restored = MowerMap.from_dict(json.loads(json.dumps(mower_map.as_dict())))
    assert restored.covered == [[(0, 0), (100, 0)]]
    assert restored.covered_holes == [[(10, 10), (20, 20)]]


def test_a_full_plan_replaces_the_stored_one() -> None:
    # A row the mower has finished is absent from the next full plan, not
    # reported empty. Merging one keeps every finished row for ever.
    mower_map = MowerMap()
    mower_map.update_remaining(
        {("1", 1): [((0, 0), (900, 0))], ("1", 2): [((0, 100), (900, 100))]},
        complete=True,
    )
    mower_map.update_remaining({("1", 2): [((0, 100), (400, 100))]}, complete=True)

    assert mower_map.remaining == {("1", 2): [((0, 100), (400, 100))]}


def test_an_empty_full_plan_means_the_job_is_done() -> None:
    mower_map = MowerMap()
    mower_map.update_remaining({("1", 1): [((0, 0), (900, 0))]}, complete=True)
    mower_map.update_remaining({}, complete=True)

    assert mower_map.remaining == {}


def test_a_single_reported_row_leaves_the_others_alone() -> None:
    # The mower pushes the row it is working on while it works; the rest of
    # the plan is not in that blob and must not be dropped.
    mower_map = MowerMap()
    mower_map.update_remaining(
        {("1", 1): [((0, 0), (900, 0))], ("1", 2): [((0, 100), (900, 100))]},
        complete=True,
    )
    mower_map.update_remaining({("1", 2): [((0, 100), (400, 100))]})

    assert mower_map.remaining == {
        ("1", 1): [((0, 0), (900, 0))],
        ("1", 2): [((0, 100), (400, 100))],
    }


def test_sampled_track_returns_a_short_track_as_it_is() -> None:
    mower_map = MowerMap()
    assert mower_map.sampled_track(10) == []
    mower_map.update_position(1, 2, 0)
    mower_map.update_position(3, 4, 0)

    sampled = mower_map.sampled_track(10)

    assert sampled == [(1, 2), (3, 4)]
    # A copy: the caller cannot reach into the map's own list.
    assert sampled is not mower_map.track


def test_sampled_track_thins_evenly_and_keeps_the_ends() -> None:
    mower_map = MowerMap()
    for i in range(1000):
        mower_map.update_position(i, 0, 0)

    sampled = mower_map.sampled_track(100)

    assert len(sampled) <= 101
    assert sampled[0] == (0, 0)
    assert sampled[-1] == (999, 0)
    xs = [x for x, _ in sampled]
    assert xs == sorted(xs)
