"""Render a MowerMap as an SVG image.

Pure Python, stdlib only. The output is served by the image entity as
image/svg+xml; layer classes exist for the tests, styling is inline. The
coordinate system is the mower's own map frame in mm (dock at origin);
the y axis is flipped because SVG grows downwards.

Two layers follow the device's reading of ``onMapTrack``
(``MowerMap.remaining_plan``): the remaining lanes of the current job,
with no driven path, or the coverage lanes and the path, which is what
every class without that reading keeps. ``MowerMap`` keeps the path
either way and the image entity always publishes it as an attribute.
"""

from __future__ import annotations

import math
from xml.sax.saxutils import escape

from .deebot_patch.geometry import Point, Polygon
from .map import MowerMap

_SCALE = 0.02  # px per mm — a 30 m lawn becomes ~600 px
_MARGIN_MM = 500

_BACKGROUND = "#1c2620"
# The lawn is one green, because on the class that has been watched the
# mower never says a lawn is mowed — it says what is left of the job it
# is on. That is _UNCUT, drawn over the lawn in the lighter, yellower
# green of grass still standing; where it is absent the map is making no
# claim. Light over dark rather than the other way round because the
# remaining lanes are thin and must carry at a dashboard's size, and a
# lawn outside the current job gets no lanes at all and must not read as
# finished.
_LAWN_FILL = "#2c4a33"
_LAWN_EDGE = "#cdf0d4"
_UNCUT = "#b7e26a"
# The two layers that claim the opposite — ground already covered — share
# the darker green coverage has always been drawn in here, read as cut
# rather than as standing: _LANE is onMapTrack under the coverage
# reading, which is every class the remaining-plan reading has not been
# confirmed on, and the 1.17 onMapTrace area, whose own docstring reads
# section 1 as the outline of what HAS been mowed. Neither may borrow
# _UNCUT's light green, however well it would carry: #7ecb87 against
# #b7e26a is 1.31:1, two light greens a glance cannot separate, and a
# 1.17 mower draws coverage while never drawing a remaining lane, so on
# its map that brightness would say the opposite of what it says above.
# Against the darker lawn fill _LANE reads better than it used to anyway
# — 2.50:1, where it was 1.96:1 on the old fill. Neither layer has been
# checked against hardware the way onMapTrack now has on one class.
_LANE = "#4a8f52"
_TRACK = "#e8e16a"
_ZONE = "#c9a94e"
_FLUSH_EDGE = "#5aa0c8"
_CORRIDOR = "#8f7fd6"
_LABEL = "#e6f0e8"
_NOGO_FILL = "#8f3a3a"
_NOGO_EDGE = "#e07a7a"
_OBSTACLE = "#c86a2e"
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

    points: list[Point] = [mower_map.dock]
    if not mower_map.remaining_plan:
        points.extend(mower_map.track)
    for polygon in (
        mower_map.boundaries
        + mower_map.zones
        + mower_map.flush_edges
        + mower_map.corridors
        + mower_map.obstacles
        + mower_map.nogo_zones
        + mower_map.covered
    ):
        points.extend(polygon)
    for rows in (mower_map.remaining, mower_map.lanes):
        for segments in rows.values():
            for start, end in segments:
                points.extend((start, end))
    if mower_map.position:
        points.append(mower_map.position)
    points.extend(point for point, _ in mower_map.labels)

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
    for polygon in mower_map.boundaries:
        parts.append(
            f'<path class="boundary" d="{path(polygon)}" '
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
    for segments in mower_map.remaining.values():
        for start, end in segments:
            # One row's width, so neighbouring rows meet and a block of
            # them reads as an area rather than as hatching.
            parts.append(
                f'<path class="uncut" d="{path([start, end], close=False)}" '
                f'stroke="{_UNCUT}" stroke-width="2" fill="none" '
                'opacity="0.95"/>'
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
        # Open too: a corridor is the passage from one area to the next.
        parts.append(
            f'<path class="corridor" d="{path(polygon, close=False)}" '
            f'fill="none" stroke="{_CORRIDOR}" stroke-width="2"/>'
        )
    for polygon in mower_map.flush_edges:
        # Open: these are arcs of the lawn outline, marking where the
        # mower is told to mow flush against the edge — not rings.
        parts.append(
            f'<path class="flush-edge" d="{path(polygon, close=False)}" '
            f'fill="none" stroke="{_FLUSH_EDGE}" stroke-width="1"/>'
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
    for point, name in mower_map.labels:
        label_x, label_y = xy(point).split(",")
        parts.append(
            f'<text class="label" x="{label_x}" y="{label_y}" '
            f'fill="{_LABEL}" font-family="sans-serif" font-size="11" '
            f'text-anchor="middle">{escape(name)}</text>'
        )

    if mower_map.track and not mower_map.remaining_plan:
        # Only under the coverage reading. Where onMapTrack is the
        # remaining plan this line is a handful of samples minutes apart,
        # and any path through them crosses lawns the mower never drove
        # over; where it is not, the path is the only thing that says
        # where the mower has been between two pushes.
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
    # Heading convention: the reported heading is the direction the mower
    # faces, in degrees counter-clockwise from the frame's +x axis, so
    # front(a) = (cos a, sin a) in the map frame. Measured on a G1-800
    # (firmware 1.36.208) over a zone run: against the direction of travel
    # between consecutive positions the heading differed by 0.5 degrees at
    # the median (-6 to +13 over 18 samples). The previous (sin a, -cos a)
    # was worked out from the symptoms in issue #41, not measured, and is
    # 90 degrees off: the turn sense was right, the zero was not. Only that
    # class and firmware have been measured. The SVG y axis grows downwards,
    # hence the minus on the sine.
    angle = math.radians(mower_map.heading)
    tip_x = marker_x + 10 * math.cos(angle)
    tip_y = marker_y - 10 * math.sin(angle)
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
