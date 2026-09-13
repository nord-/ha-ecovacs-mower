"""Mower area state and protocol support missing from deebot-client.

The patch owns the mower-reported area snapshot because the upstream client does
not model these GOAT commands yet. Home Assistant consumes the resulting state
and does not parse the Ecovacs wire format.

The area values remain raw protocol values here. Model- and firmware-specific
conversion to human-sensible Home Assistant values belongs exclusively to the
HA layer and must not be generalized here without validation.

The area name is a separate response from ``getAreaSet``. Its ``ar`` response
contains chunked Base64/LZMA data whose decoded rows start with map ID, area ID
and the user-editable name. The decompressor is supplied by deebot-client; the
patch owns the missing command parsing in ``commands.py`` and the resulting
state here.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary

from deebot_client.events.base import Event
from deebot_client.rs.util import decompress_base64_data

if TYPE_CHECKING:
    from deebot_client.event_bus import EventBus

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class MowerArea:
    """The raw per-area parameters plus the optional app-defined name."""

    area_id: str
    name: str | None = None
    mow_height_level: int | None = None
    cut_mode: int | None = None
    obstacle_height: int | None = None
    angle: int | None = None


@dataclass(frozen=True)
class MowerAreaEvent(Event):
    """The latest area inventory and parameter snapshot known for the mower."""

    areas: tuple[MowerArea, ...]


_AREA_STATE: WeakKeyDictionary[EventBus, dict[str, MowerArea]] = WeakKeyDictionary()


def _areas_for(event_bus: EventBus) -> dict[str, MowerArea]:
    """Return the authoritative area state for one mower event bus."""
    return _AREA_STATE.setdefault(event_bus, {})


def area_for(event_bus: EventBus, area_id: str) -> MowerArea | None:
    """Return the authoritative raw state for one known area."""
    return _areas_for(event_bus).get(area_id)


def _notify(event_bus: EventBus) -> None:
    """Publish the state after the owning handler has updated it."""
    event_bus.notify(MowerAreaEvent(tuple(_areas_for(event_bus).values())))


def _as_int(value: Any) -> int | None:
    """Return an integer payload value, or None when it is absent/invalid."""
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def apply_area_parameters(event_bus: EventBus, parameters: Any) -> bool:
    """Merge a getAreaParameter/onAreaParameter payload into area state.

    Both ``GetAreaParameter``'s answer and the mower's unsolicited
    ``onAreaParameter`` push carry an identical ``areaParameters`` list and
    must parse it identically — a copy would drift the day the payload gains
    a field. Returns whether ``parameters`` was even a usable list; a bad row
    inside a good list is skipped, not rejected outright.
    """
    if not isinstance(parameters, list):
        return False
    areas = _areas_for(event_bus)
    for parameter in parameters:
        if not isinstance(parameter, dict) or parameter.get("areaID") is None:
            continue
        area_id = str(parameter["areaID"])
        current = areas.get(area_id, MowerArea(area_id))
        areas[area_id] = MowerArea(
            area_id=current.area_id,
            name=current.name,
            mow_height_level=_as_int(parameter.get("mowHeightLevel")),
            cut_mode=_as_int(parameter.get("cutMode")),
            obstacle_height=_as_int(parameter.get("obstacleHeight")),
            angle=_as_int(parameter.get("angle")),
        )
    _notify(event_bus)
    return True


class _AreaSetFragmentBuffer:
    """Reassemble multipart area-set data using deebot-client's decoder."""

    def __init__(self, max_batches: int = 16) -> None:
        """Create a bounded fragment buffer."""
        self._batches: OrderedDict[str, dict[int, str]] = OrderedDict()
        self._max_batches = max_batches

    def add(
        self, batid: str, index: int, fragment: str, info_size: int
    ) -> bytes | None:
        """Add a fragment and return decoded data when the stream is complete.

        ``infoSize`` in the mower ``ar`` response is not the size of the
        decompressed JSON returned by ``decompress_base64_data``. The A1600,
        for example, reports ``infoSize=498`` while its decoded JSON is 196
        bytes. Completion is therefore determined by successful decompression
        rather than by comparing the decompressed length with ``infoSize``.
        """
        del info_size
        parts = self._batches.setdefault(batid, {})
        self._batches.move_to_end(batid)
        parts[index] = fragment
        while len(self._batches) > self._max_batches:
            self._batches.popitem(last=False)

        joined = "".join(parts[i] for i in sorted(parts))
        try:
            blob = decompress_base64_data(joined)
        except (ValueError, RuntimeError):
            return None
        del self._batches[batid]
        return blob


def reset() -> None:
    """Forget all per-device area state. Tests only."""
    _AREA_STATE.clear()


__all__ = [
    "MowerArea",
    "MowerAreaEvent",
    "apply_area_parameters",
    "area_for",
    "reset",
]
