"""Tests for the SVG renderer. Pure Python — runs on Windows."""

from __future__ import annotations

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


def test_svg_is_valid_xml() -> None:
    import xml.etree.ElementTree as ET

    ET.fromstring(render(_populated_map()))
    ET.fromstring(render(MowerMap()))
