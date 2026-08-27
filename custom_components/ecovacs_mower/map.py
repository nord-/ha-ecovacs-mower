"""In-memory map state for one mower.

Pure Python: no Home Assistant, no deebot-client. Fed by the controller
from the map events; consumed by map_svg.py. Position track and marker are
deliberately volatile — after a restart the marker defaults to the dock
and the first onPos corrects it within seconds (KISS decision, 2026-08-10).
"""

from __future__ import annotations

from typing import Any, Self

from .deebot_patch.geometry import Point, Polygon, Segment

TRACK_MAX_POINTS = 2000


class MowerMap:
    """Latest known map geometry and position for one device."""

    def __init__(self) -> None:
        self.boundary: Polygon | None = None
        self.zones: list[Polygon] = []
        self.corridors: list[Polygon] = []
        self.obstacles: list[Polygon] = []
        self.nogo_zones: list[Polygon] = []
        self.lanes: dict[tuple[str, int], list[Segment]] = {}
        # Firmware 1.17 reports coverage as outlines with holes instead of
        # lanes. A device speaks one dialect, so only one of the two is
        # ever populated.
        self.covered: list[Polygon] = []
        self.covered_holes: list[Polygon] = []
        self.track: list[Point] = []
        self.position: Point | None = None
        self.heading: int = 0
        # The map frame's origin is the dock: the first valid deebotPos of
        # a session is exactly (0, 0) and chargePos is never valid on the
        # verified hardware. Overwritten if a device ever reports a valid
        # charger position.
        self.dock: Point = (0, 0)

    @property
    def is_empty(self) -> bool:
        """True when there is no geometry worth rendering."""
        return (
            self.boundary is None
            and not self.zones
            and not self.lanes
            and not self.covered
            and not self.nogo_zones
            and not self.obstacles
            and not self.corridors
        )

    def update_map_info(
        self,
        boundary: Polygon | None,
        zones: list[Polygon] | None,
        corridors: list[Polygon] | None,
    ) -> None:
        """Merge static geometry; None means "unchanged"."""
        if boundary is not None:
            self.boundary = boundary
        if zones is not None:
            self.zones = zones
        if corridors is not None:
            self.corridors = corridors

    def update_obstacles(self, obstacles: list[Polygon]) -> None:
        """Replace the obstacle set (events carry the complete set)."""
        self.obstacles = obstacles

    def update_nogo(self, zones: list[Polygon]) -> None:
        """Replace the no-go zones (events carry the complete set)."""
        self.nogo_zones = zones

    def update_coverage(
        self, lanes: dict[tuple[str, int], list[Segment]]
    ) -> None:
        """Merge lane rows; a resent row replaces its previous extent."""
        self.lanes.update(lanes)

    def update_covered(
        self, areas: list[Polygon], holes: list[Polygon]
    ) -> None:
        """Replace the mowed area (every onMapTrace blob is a snapshot)."""
        self.covered = areas
        self.covered_holes = holes

    def update_position(self, x: int, y: int, a: int) -> None:
        """Move the marker and extend the track."""
        self.position = (x, y)
        self.heading = a
        self.track.append((x, y))
        if len(self.track) > TRACK_MAX_POINTS:
            # Keep the recent half dense, thin the older half. Repeated
            # compaction gives logarithmically thinning history.
            dense = TRACK_MAX_POINTS // 2
            self.track = self.track[:-dense:2] + self.track[-dense:]

    def as_dict(self) -> dict[str, Any]:
        """Serializable snapshot for Store. Track/position stay volatile."""
        return {
            "boundary": self.boundary,
            "zones": self.zones,
            "corridors": self.corridors,
            "obstacles": self.obstacles,
            "nogo_zones": self.nogo_zones,
            "lanes": [
                [zone, row, segments]
                for (zone, row), segments in self.lanes.items()
            ],
            "covered": self.covered,
            "covered_holes": self.covered_holes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Rebuild from a Store snapshot (JSON turned tuples into lists)."""

        def polygon(points: list[Any]) -> Polygon:
            return [(int(p[0]), int(p[1])) for p in points]

        mower_map = cls()
        if data.get("boundary"):
            mower_map.boundary = polygon(data["boundary"])
        mower_map.zones = [polygon(p) for p in data.get("zones", [])]
        mower_map.corridors = [polygon(p) for p in data.get("corridors", [])]
        mower_map.obstacles = [polygon(p) for p in data.get("obstacles", [])]
        mower_map.nogo_zones = [
            polygon(p) for p in data.get("nogo_zones", [])
        ]
        mower_map.covered = [polygon(p) for p in data.get("covered", [])]
        mower_map.covered_holes = [
            polygon(p) for p in data.get("covered_holes", [])
        ]
        mower_map.lanes = {
            (str(zone), int(row)): [
                ((int(a[0]), int(a[1])), (int(b[0]), int(b[1])))
                for a, b in segments
            ]
            for zone, row, segments in data.get("lanes", [])
        }
        return mower_map
