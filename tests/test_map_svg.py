"""Tests for the SVG renderer. Pure Python — runs on Windows."""

from __future__ import annotations

import re

import pytest

from custom_components.ecovacs_mower.map import MowerMap
from custom_components.ecovacs_mower.map_svg import render

BOUNDARY = [(0, 0), (10000, 0), (10000, 10000), (0, 10000)]


def _populated_map() -> MowerMap:
    mower_map = MowerMap()
    mower_map.update_map_info(BOUNDARY, [[(1, 1), (2, 2), (3, 1)]], [])
    mower_map.update_nogo([[(100, 100), (200, 100), (200, 200)]])
    mower_map.update_obstacles([[(300, 300), (400, 300), (400, 400)]])
    mower_map.update_coverage({("1", 5): [((500, 0), (500, 9000))]})
    mower_map.update_position(1000, 2000, 90)
    return mower_map


def test_empty_map_renders_placeholder() -> None:
    svg = render(MowerMap())
    assert svg.startswith("<svg")
    assert "No map data yet" in svg


def test_populated_map_renders_all_layers() -> None:
    svg = render(_populated_map())
    assert svg.startswith("<svg")
    assert "No map data yet" not in svg
    assert 'class="boundary"' in svg
    assert 'class="lane"' in svg
    assert 'class="nogo"' in svg
    assert 'class="obstacle"' in svg
    assert 'class="zone"' in svg
    assert 'class="track"' in svg
    assert 'class="dock"' in svg
    assert 'class="mower"' in svg


def test_marker_defaults_to_dock_without_position() -> None:
    mower_map = MowerMap()
    mower_map.update_map_info(BOUNDARY, None, None)
    svg = render(mower_map)
    # A map without position data still shows the mower — at the dock.
    assert 'class="mower"' in svg


def _mower_and_heading_endpoint(svg: str) -> tuple[float, float, float, float]:
    mower_cx, mower_cy = re.search(
        r'class="mower" cx="([\d.]+)" cy="([\d.]+)"', svg
    ).groups()
    line_x2, line_y2 = re.search(
        r'class="heading"[^>]*x2="([\d.]+)" y2="([\d.]+)"', svg
    ).groups()
    return float(mower_cx), float(mower_cy), float(line_x2), float(line_y2)


@pytest.mark.parametrize(
    ("heading", "dx", "dy"),
    [
        (0, 1, 0),  # +x: screen right
        (90, 0, -1),  # +y: screen up, the SVG y axis grows downwards
        (180, -1, 0),
        (-90, 0, 1),
    ],
)
def test_heading_arrow_points_along_the_reported_heading(
    heading: int, dx: int, dy: int
) -> None:
    # The heading is degrees counter-clockwise from the frame's +x axis.
    # Measured on a G1-800 (firmware 1.36.208): it matched the direction of
    # travel to within a few degrees (issue #41 worked it out from symptoms
    # and ended up 90 degrees off).
    mower_map = MowerMap()
    mower_map.update_map_info(BOUNDARY, None, None)
    mower_map.update_position(1000, 1000, heading)
    mower_cx, mower_cy, line_x2, line_y2 = _mower_and_heading_endpoint(
        render(mower_map)
    )
    # The arrow is 10 px long, and coordinates are written with one decimal.
    assert line_x2 - mower_cx == pytest.approx(10 * dx, abs=0.2)
    assert line_y2 - mower_cy == pytest.approx(10 * dy, abs=0.2)


def test_heading_arrow_turns_the_way_the_mower_turns() -> None:
    # An increasing heading is a counter-clockwise turn of the mower as seen
    # on the map, so the arrow must go from pointing right (0) via up (90)
    # to left (180), never the mirror image.
    def endpoint(heading: int) -> tuple[float, float]:
        mower_map = MowerMap()
        mower_map.update_map_info(BOUNDARY, None, None)
        mower_map.update_position(1000, 1000, heading)
        cx, cy, x2, y2 = _mower_and_heading_endpoint(render(mower_map))
        return x2 - cx, y2 - cy

    right, up, left = endpoint(0), endpoint(90), endpoint(180)
    assert right[0] > 0 and abs(right[1]) < 0.1
    assert up[1] < 0 and abs(up[0]) < 0.1
    assert left[0] < 0 and abs(left[1]) < 0.1


def test_svg_is_valid_xml() -> None:
    import xml.etree.ElementTree as ET

    ET.fromstring(render(_populated_map()))
    ET.fromstring(render(MowerMap()))


def test_covered_area_renders_with_holes_punched_out() -> None:
    # Firmware 1.17 sends coverage as an outline plus the unmowed islands
    # inside it. One path, even-odd fill: the holes cut the fill away.
    mower_map = MowerMap()
    mower_map.update_map_info(BOUNDARY, None, None)
    mower_map.update_covered(
        [[(0, 0), (9000, 0), (9000, 9000), (0, 9000)]],
        [[(1000, 1000), (2000, 1000), (2000, 2000)]],
    )
    svg = render(mower_map)
    covered = next(
        line for line in svg.splitlines() if 'class="covered"' in line
    )
    assert 'fill-rule="evenodd"' in covered
    # Outer ring and hole are subpaths of the same path element. Match "M"
    # only where it starts a moveto (followed by a coordinate digit/sign),
    # not wherever it happens to occur — e.g. inside a colour hex.
    assert len(re.findall(r"M[\d.-]", covered)) == 2


def test_covered_area_alone_is_not_the_placeholder() -> None:
    mower_map = MowerMap()
    mower_map.update_covered([[(0, 0), (9000, 0), (9000, 9000)]], [])
    svg = render(mower_map)
    assert "No map data yet" not in svg
    assert 'class="covered"' in svg
