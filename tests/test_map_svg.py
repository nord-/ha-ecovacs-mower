"""Tests for the SVG renderer. Pure Python — runs on Windows."""

from __future__ import annotations

import re

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


def test_heading_arrow_points_forward_not_backward() -> None:
    # Regression for issue #41: at heading 0 the arrow pointed to the
    # mower's back (screen "up" instead of "down") because the assumed
    # heading convention was never verified against real hardware.
    mower_map = MowerMap()
    mower_map.update_map_info(BOUNDARY, None, None)
    mower_map.update_position(1000, 1000, 0)
    mower_cx, mower_cy, line_x2, line_y2 = _mower_and_heading_endpoint(
        render(mower_map)
    )
    assert line_x2 == mower_cx
    assert line_y2 > mower_cy


def test_heading_arrow_rotates_with_real_turn_direction() -> None:
    # Regression for issue #41: a turn that increases the reported
    # heading rendered as a mirror-image turn on screen.
    mower_map = MowerMap()
    mower_map.update_map_info(BOUNDARY, None, None)
    mower_map.update_position(1000, 1000, 90)
    mower_cx, mower_cy, line_x2, line_y2 = _mower_and_heading_endpoint(
        render(mower_map)
    )
    assert line_x2 > mower_cx
    assert line_y2 == mower_cy


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
