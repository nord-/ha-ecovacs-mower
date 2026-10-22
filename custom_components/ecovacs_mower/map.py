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
    """Latest known map geometry and position for one device.

    ``remaining_plan`` is the device's reading of ``onMapTrack``, from
    ``MowerProfile.remaining_plan``. It decides which of ``remaining`` and
    ``lanes`` this map keeps, which store key the rows go under, and
    whether ``map_svg`` draws the driven path. The map has to carry it
    because the renderer and the store see nothing else of the device.
    """

    def __init__(self, remaining_plan: bool = False) -> None:
        self.remaining_plan = remaining_plan
        # One outline per mowed area: a multi-area map has one per
        # lawn, every other mower exactly one.
        self.boundaries: list[Polygon] = []
        self.zones: list[Polygon] = []
        self.flush_edges: list[Polygon] = []
        # The passages joining two areas, from onMI. Open paths, not
        # rings, like the flush edges above.
        self.corridors: list[Polygon] = []
        # Each area's name at the point the mower itself puts it, from
        # getAreaSet. Ordered by area id so the rendering is stable.
        self.labels: list[tuple[Point, str]] = []
        self.obstacles: list[Polygon] = []
        self.nogo_zones: list[Polygon] = []
        # The two readings of onMapTrack, keyed (zone, row): remaining is
        # the lanes of the current job the mower has not reached yet, on a
        # class where that was measured; lanes is the coverage reading
        # every other class keeps. remaining_plan picks one, so only one of
        # them is ever populated. See MapTrack in deebot_patch/geometry.py.
        self.remaining: dict[tuple[str, int], list[Segment]] = {}
        self.lanes: dict[tuple[str, int], list[Segment]] = {}
        # Firmware 1.17 reports coverage as outlines with holes instead of
        # lanes. A device speaks one dialect, so only one of the two is
        # ever populated.
        self.covered: list[Polygon] = []
        self.covered_holes: list[Polygon] = []
        # Kept and published as an entity attribute, but not drawn: see
        # the note in map_svg.py.
        self.track: list[Point] = []
        self.position: Point | None = None
        self.heading: int = 0
        # The map frame's origin is the dock: the first valid deebotPos of
        # a session is exactly (0, 0), and on firmware 1.11.31 and 1.13.10
        # — the captures this map was designed against — chargePos is
        # flagged invalid on every sample, leaving nothing else to use.
        # Firmware 1.36.208 does report it, and reports (0, 0), so the
        # assumption is confirmed rather than undermined (issue #52).
        # Overwritten whenever a device reports a valid charger position.
        self.dock: Point = (0, 0)

    @property
    def is_empty(self) -> bool:
        """True when there is no geometry worth rendering."""
        return (
            not self.boundaries
            and not self.zones
            and not self.remaining
            and not self.lanes
            and not self.covered
            and not self.nogo_zones
            and not self.obstacles
            and not self.flush_edges
            and not self.corridors
        )

    def update_map_info(
        self,
        boundaries: list[Polygon] | None,
        zones: list[Polygon] | None,
        flush_edges: list[Polygon] | None,
        corridors: list[Polygon] | None = None,
    ) -> None:
        """Merge static geometry; None means "unchanged"."""
        if boundaries is not None:
            self.boundaries = boundaries
        if zones is not None:
            self.zones = zones
        if flush_edges is not None:
            self.flush_edges = flush_edges
        if corridors is not None:
            self.corridors = corridors

    def update_labels(self, labels: list[tuple[Point, str]]) -> None:
        """Replace the area labels (the inventory is a complete set)."""
        self.labels = labels

    def update_obstacles(self, obstacles: list[Polygon]) -> None:
        """Replace the obstacle set (events carry the complete set)."""
        self.obstacles = obstacles

    def update_nogo(self, zones: list[Polygon]) -> None:
        """Replace the no-go zones (events carry the complete set)."""
        self.nogo_zones = zones

    def update_coverage(
        self, lanes: dict[tuple[str, int], list[Segment]]
    ) -> None:
        """Merge lane rows; a resent row replaces its previous extent.

        The coverage reading, for a class where nothing says the rows are
        the remainder of a job. Merge-forever, because under that reading
        a row the mower stops reporting has still been mowed.
        """
        self.lanes.update(lanes)

    def update_remaining(
        self,
        rows: dict[tuple[str, int], list[Segment]],
        complete: bool = False,
    ) -> None:
        """Take a full plan whole, or merge a single reported row.

        A row shrinks as the mower works along it, and a finished one
        is dropped from the next full plan rather than reported empty —
        so merging a full plan would keep every finished row for ever.
        """
        if complete:
            self.remaining = dict(rows)
        else:
            self.remaining.update(rows)

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

    def sampled_track(self, limit: int) -> list[Point]:
        """The track thinned to roughly ``limit`` points, last point included.

        For consumers that cannot take the full track — a state attribute is
        sent to every client on each change. Evenly spaced, so the shape is
        kept; the newest point is always there so the path ends where the
        mower is.
        """
        if len(self.track) <= limit:
            return list(self.track)
        step = -(-len(self.track) // limit)
        points = self.track[::step]
        if points[-1] != self.track[-1]:
            points.append(self.track[-1])
        return points

    def as_dict(self) -> dict[str, Any]:
        """Serializable snapshot for Store. Track/position stay volatile."""
        return {
            "boundaries": self.boundaries,
            "zones": self.zones,
            "flush_edges": self.flush_edges,
            "corridors": self.corridors,
            "labels": [[list(point), name] for point, name in self.labels],
            "obstacles": self.obstacles,
            "nogo_zones": self.nogo_zones,
            # One key per reading, and only the device's own is written.
            # A map under the remaining-plan reading must not read a
            # stored "lanes": that key holds every onMapTrack row ever
            # reported, merged forever, so taken for the plan it would
            # draw the whole season's mowing as still to cut and keep
            # doing so until a full plan arrived — which needs the mower
            # out and a poll tick. An empty layer until the next job is
            # the honest version of not knowing. A map under the coverage
            # reading keeps reading "lanes", where nothing has changed.
            # The store version stays at 1 either way:
            # Store.async_load raises NotImplementedError for a version
            # with no migration hook, and neither key needs one.
            **(
                {
                    "remaining": [
                        [zone, row, segments]
                        for (zone, row), segments in self.remaining.items()
                    ]
                }
                if self.remaining_plan
                else {
                    "lanes": [
                        [zone, row, segments]
                        for (zone, row), segments in self.lanes.items()
                    ]
                }
            ),
            "covered": self.covered,
            "covered_holes": self.covered_holes,
        }

    @classmethod
    def from_dict(
        cls, data: dict[str, Any], remaining_plan: bool = False
    ) -> Self:
        """Rebuild from a Store snapshot (JSON turned tuples into lists)."""

        def polygon(points: list[Any]) -> Polygon:
            return [(int(p[0]), int(p[1])) for p in points]

        mower_map = cls(remaining_plan=remaining_plan)
        if data.get("boundaries"):
            mower_map.boundaries = [
                polygon(points) for points in data["boundaries"]
            ]
        elif data.get("boundary"):
            # A snapshot written before a map could hold more than one
            # outline. The store version is deliberately not bumped for
            # this: Store.async_load raises NotImplementedError for a
            # version it has no migration hook for, and one outline needs
            # nothing but wrapping.
            mower_map.boundaries = [polygon(data["boundary"])]
        mower_map.zones = [polygon(p) for p in data.get("zones", [])]
        # "corridors" is what section 6 was called before it was named
        # for the flush-mow edges it holds, and is what older snapshots
        # store it under. In one of those the key is the flush edges and
        # there are no passages; in a current one the two are separate.
        legacy = "flush_edges" not in data
        mower_map.flush_edges = [
            polygon(p)
            for p in (data.get("corridors") if legacy else data["flush_edges"])
            or []
        ]
        if not legacy:
            mower_map.corridors = [
                polygon(p) for p in data.get("corridors", [])
            ]
        mower_map.obstacles = [polygon(p) for p in data.get("obstacles", [])]
        mower_map.nogo_zones = [
            polygon(p) for p in data.get("nogo_zones", [])
        ]
        mower_map.covered = [polygon(p) for p in data.get("covered", [])]
        mower_map.covered_holes = [
            polygon(p) for p in data.get("covered_holes", [])
        ]
        mower_map.labels = [
            ((int(point[0]), int(point[1])), str(name))
            for point, name in data.get("labels", [])
        ]
        # Only the device's own key; see as_dict.
        rows = {
            (str(zone), int(row)): [
                ((int(a[0]), int(a[1])), (int(b[0]), int(b[1])))
                for a, b in segments
            ]
            for zone, row, segments in data.get(
                "remaining" if remaining_plan else "lanes", []
            )
        }
        if remaining_plan:
            mower_map.remaining = rows
        else:
            mower_map.lanes = rows
        return mower_map
