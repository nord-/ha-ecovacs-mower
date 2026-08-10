"""Map message handlers deebot-client lacks for lawn mowers.

GOAT reports its map via four unsolicited MQTT messages — onMI, onArI,
onMapTrack, onSpecialContour — none of which exist in the library (the
vacuum world uses getMapTrace/getMajorMap instead). The wire format is
documented in docs/superpowers/specs/2026-08-10-mower-map-design.md.

Map data is best effort: a broken payload logs at DEBUG and is dropped,
it never raises and never touches the control path.
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any, ClassVar

from deebot_client.events.base import Event
from deebot_client.message import HandlingResult, MessageBodyDataDict

from .geometry import (
    FragmentBuffer,
    Polygon,
    Segment,
    parse_area_info,
    parse_map_info,
    parse_map_track,
    parse_special_contour,
)

if TYPE_CHECKING:
    from deebot_client.event_bus import EventBus

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class MowerMapInfoEvent(Event):
    """Static map geometry. None fields mean "unchanged", never "empty"."""

    boundary: Polygon | None
    zones: list[Polygon] | None
    corridors: list[Polygon] | None


@dataclass(frozen=True)
class MowerObstaclesEvent(Event):
    """Detected obstacles; the list is the complete current set."""

    obstacles: list[Polygon]


@dataclass(frozen=True)
class MowerCoverageEvent(Event):
    """Mowed lane spans keyed (zone, row); an empty list clears the row."""

    lanes: dict[tuple[str, int], list[Segment]]


@dataclass(frozen=True)
class MowerNoGoZonesEvent(Event):
    """User-defined no-go zones; the list is the complete current set."""

    zones: list[Polygon]


class _MapMessage(MessageBodyDataDict, ABC):
    """Shared multipart buffering and best-effort decoding.

    ABC must be a direct base: the library's __init_subclass__ NAME check
    exempts only classes with ABC in __bases__.
    """

    _buffer: ClassVar[FragmentBuffer]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # One buffer per message type. Shared across devices: batids are
        # random enough that a collision between two mowers on the same
        # account would at worst drop one blob.
        cls._buffer = FragmentBuffer()

    @classmethod
    def _handle_body_data_dict(
        cls, event_bus: EventBus, data: dict[str, Any]
    ) -> HandlingResult:
        info = data.get("info")
        if not info:
            return HandlingResult.analyse()
        blob = cls._buffer.add(
            str(data.get("batid", "")),
            int(data.get("index", 0)),
            info,
            int(data.get("infoSize", -1)),
        )
        if blob is None:
            # Waiting for more fragments (or the blob is corrupt, in which
            # case the capped buffer eventually evicts it).
            return HandlingResult.success()
        try:
            return cls._notify(event_bus, blob)
        except (TypeError, ValueError, KeyError, IndexError):
            # json.JSONDecodeError is a ValueError. TypeError covers a blob
            # that decodes to something non-iterable (e.g. a bare int).
            # Best effort: log and drop, never disturb the control path.
            _LOGGER.debug(
                "Undecodable %s blob (batid %s)", cls.NAME, data.get("batid")
            )
            return HandlingResult.analyse()

    @classmethod
    def _notify(cls, event_bus: EventBus, blob: bytes) -> HandlingResult:
        raise NotImplementedError


class OnMI(_MapMessage):
    """Map info: the lawn boundary outline."""

    NAME = "onMI"

    @classmethod
    def _notify(cls, event_bus: EventBus, blob: bytes) -> HandlingResult:
        map_info = parse_map_info(blob)
        if map_info.boundary is None:
            return HandlingResult.success()  # idle snapshot, nothing new
        event_bus.notify(
            MowerMapInfoEvent(
                boundary=map_info.boundary, zones=None, corridors=None
            )
        )
        return HandlingResult.success()


class OnArI(_MapMessage):
    """Area info: zones, obstacles, boundary, corridors."""

    NAME = "onArI"

    @classmethod
    def _notify(cls, event_bus: EventBus, blob: bytes) -> HandlingResult:
        area = parse_area_info(blob)
        map_info = area.map_info
        if (
            map_info.boundary is not None
            or map_info.zones is not None
            or map_info.corridors is not None
        ):
            event_bus.notify(
                MowerMapInfoEvent(
                    boundary=map_info.boundary,
                    zones=map_info.zones,
                    corridors=map_info.corridors,
                )
            )
        if area.obstacles is not None:
            event_bus.notify(MowerObstaclesEvent(obstacles=area.obstacles))
        return HandlingResult.success()


class OnMapTrack(_MapMessage):
    """Coverage: mowed lane spans per 100 mm row."""

    NAME = "onMapTrack"

    @classmethod
    def _notify(cls, event_bus: EventBus, blob: bytes) -> HandlingResult:
        track = parse_map_track(blob)
        if track.lanes:
            event_bus.notify(MowerCoverageEvent(lanes=track.lanes))
        return HandlingResult.success()


class OnSpecialContour(_MapMessage):
    """User-defined no-go zones."""

    NAME = "onSpecialContour"

    @classmethod
    def _notify(cls, event_bus: EventBus, blob: bytes) -> HandlingResult:
        event_bus.notify(
            MowerNoGoZonesEvent(zones=parse_special_contour(blob))
        )
        return HandlingResult.success()
