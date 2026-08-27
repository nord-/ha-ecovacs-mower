"""Render a MowerMap as an SVG image.

Pure Python, stdlib only. The output is served by the image entity as
image/svg+xml; layer classes exist for the tests, styling is inline. The
coordinate system is the mower's own map frame in mm (dock at origin);
the y axis is flipped because SVG grows downwards.
"""

from __future__ import annotations

import math

from .deebot_patch.geometry import Point, Polygon
from .map import MowerMap

_SCALE = 0.02  # px per mm — a 30 m lawn becomes ~600 px
_MARGIN_MM = 500

_BACKGROUND = "#1c2620"
_LAWN_FILL = "#2e5d34"
_LAWN_EDGE = "#7fd68a"
_LANE = "#4a8f52"
_ZONE = "#c9a94e"
_CORRIDOR = "#5aa0c8"
_NOGO_FILL = "#8f3a3a"
_NOGO_EDGE = "#e07a7a"
_OBSTACLE = "#c86a2e"
_TRACK = "#e8e16a"
_MOWER = "#ffe93d"
_DOCK = "#5aa0c8"

_PLACEHOLDER = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="200">'
    f'<rect width="400" height="200" fill="{_BACKGROUND}"/>'
    f'<text x="200" y="105" fill="{_LAWN_EDGE}" font-family="sans-serif" '
    'font-size="16" text-anchor="middle">No map data yet</text></svg>'
)


def render(mower_map: MowerMap) -> str:
    """Render the map, or a placeholder when there is nothing to draw."""
    if mower_map.is_empty:
        return _PLACEHOLDER

    points: list[Point] = list(mower_map.track)
    points.append(mower_map.dock)
    for polygon in (
        [mower_map.boundary] if mower_map.boundary else []
    ) + mower_map.zones + mower_map.corridors + mower_map.obstacles + (
        mower_map.nogo_zones
    ) + mower_map.covered:
        points.extend(polygon)
    for segments in mower_map.lanes.values():
        for start, end in segments:
            points.extend((start, end))
    if mower_map.position:
        points.append(mower_map.position)

    min_x = min(p[0] for p in points) - _MARGIN_MM
    max_x = max(p[0] for p in points) + _MARGIN_MM
    min_y = min(p[1] for p in points) - _MARGIN_MM
    max_y = max(p[1] for p in points) + _MARGIN_MM
    width = (max_x - min_x) * _SCALE
    height = (max_y - min_y) * _SCALE

    def xy(point: Point) -> str:
        return (
            f"{(point[0] - min_x) * _SCALE:.1f},"
            f"{(max_y - point[1]) * _SCALE:.1f}"
        )

    def path(polygon: Polygon, close: bool = True) -> str:
        return "M" + " L".join(xy(p) for p in polygon) + ("Z" if close else "")

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" '
        f'height="{height:.0f}" viewBox="0 0 {width:.0f} {height:.0f}">',
        f'<rect width="{width:.0f}" height="{height:.0f}" '
        f'fill="{_BACKGROUND}"/>',
    ]
    if mower_map.boundary:
        parts.append(
            f'<path class="boundary" d="{path(mower_map.boundary)}" '
            f'fill="{_LAWN_FILL}" stroke="{_LAWN_EDGE}" stroke-width="1.5"/>'
        )
    if mower_map.covered:
        # One path, even-odd fill: the holes are subpaths that cut the
        # mowed area away wherever they overlap it. Every hole a 1.17
        # mower has sent sits inside one of the outlines.
        rings = " ".join(
            path(polygon)
            for polygon in mower_map.covered + mower_map.covered_holes
        )
        parts.append(
            f'<path class="covered" d="{rings}" fill="{_LANE}" '
            'fill-rule="evenodd" opacity="0.9"/>'
        )
    for segments in mower_map.lanes.values():
        for start, end in segments:
            parts.append(
                f'<path class="lane" d="{path([start, end], close=False)}" '
                f'stroke="{_LANE}" stroke-width="2" fill="none" '
                'opacity="0.9"/>'
            )
    for polygon in mower_map.zones:
        parts.append(
            f'<path class="zone" d="{path(polygon)}" fill="none" '
            f'stroke="{_ZONE}" stroke-width="1" stroke-dasharray="4 3"/>'
        )
    for polygon in mower_map.corridors:
        parts.append(
            f'<path class="corridor" d="{path(polygon)}" fill="none" '
            f'stroke="{_CORRIDOR}" stroke-width="1"/>'
        )
    for polygon in mower_map.nogo_zones:
        parts.append(
            f'<path class="nogo" d="{path(polygon)}" fill="{_NOGO_FILL}" '
            f'opacity="0.55" stroke="{_NOGO_EDGE}" stroke-width="1"/>'
        )
    for polygon in mower_map.obstacles:
        parts.append(
            f'<path class="obstacle" d="{path(polygon)}" '
            f'fill="{_OBSTACLE}" opacity="0.8"/>'
        )
    if len(mower_map.track) >= 1:
        parts.append(
            f'<path class="track" '
            f'd="{path(mower_map.track, close=False)}" fill="none" '
            f'stroke="{_TRACK}" stroke-width="1.2" opacity="0.9"/>'
        )

    dock_x, dock_y = (v for v in xy(mower_map.dock).split(","))
    parts.append(
        f'<rect class="dock" x="{float(dock_x) - 4:.1f}" '
        f'y="{float(dock_y) - 4:.1f}" width="8" height="8" '
        f'fill="{_DOCK}" stroke="#000" stroke-width="1"/>'
    )

    # The marker defaults to the dock: after a restart the map always
    # shows something, and the first onPos corrects it within seconds.
    marker = mower_map.position or mower_map.dock
    marker_x, marker_y = (float(v) for v in xy(marker).split(","))
    # Heading convention ASSUMED: 0 degrees = north (up), increasing
    # clockwise. The capture only proves a=0 at the dock; verify the
    # arrow against the real mower in Task 9 and flip sin/cos if wrong.
    angle = math.radians(mower_map.heading)
    tip_x = marker_x + 10 * math.sin(angle)
    tip_y = marker_y - 10 * math.cos(angle)
    parts.append(
        f'<line class="heading" x1="{marker_x:.1f}" y1="{marker_y:.1f}" '
        f'x2="{tip_x:.1f}" y2="{tip_y:.1f}" stroke="{_MOWER}" '
        'stroke-width="2"/>'
    )
    parts.append(
        f'<circle class="mower" cx="{marker_x:.1f}" cy="{marker_y:.1f}" '
        f'r="5" fill="{_MOWER}" stroke="#000" stroke-width="1"/>'
    )
    parts.append("</svg>")
    return "\n".join(parts)
