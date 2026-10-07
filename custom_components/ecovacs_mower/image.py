"""The map image entity for Ecovacs GOAT.

Not in HA core's ecovacs (whose vacuum map rendering was cut from this
fork); this is a mower-specific replacement built on the decoded GOAT map
messages. The SVG is rendered lazily when the frontend fetches the image;
events only decide when the image counts as new.

The mower's position and the dock also go out as attributes, in the map
frame's own units (mm), so a card can draw the mower on something other
than the SVG — an aerial photo, for instance.
"""

from __future__ import annotations

import logging
from typing import override

from deebot_client.capabilities import Capabilities, DeviceType
from deebot_client.device import Device
from deebot_client.events.map import PositionsEvent

from homeassistant.components.image import ImageEntity, ImageEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import EcovacsMowerConfigEntry
from .deebot_patch.map_messages import (
    MowerCoverageEvent,
    MowerCoveredAreaEvent,
    MowerMapInfoEvent,
    MowerNoGoZonesEvent,
    MowerObstaclesEvent,
)
from .entity import EcovacsEntity
from .map import MowerMap
from .map_svg import render

_LOGGER = logging.getLogger(__name__)

# Position events arrive at ~2 Hz; bumping image_last_updated for each one
# would make the frontend re-fetch twice a second.
POSITION_UPDATE_INTERVAL_SECONDS = 2

ATTR_POSITION_X = "position_x"
ATTR_POSITION_Y = "position_y"
ATTR_HEADING = "heading"
ATTR_DOCK_X = "dock_x"
ATTR_DOCK_Y = "dock_y"


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: EcovacsMowerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the map image for each mower."""
    controller = config_entry.runtime_data
    entities = [
        EcovacsMowerMap(device, controller.maps[device.device_info["did"]], hass)
        for device in controller.devices
        if device.capabilities.device_type is DeviceType.MOWER
        and device.device_info["did"] in controller.maps
    ]
    async_add_entities(entities)


class EcovacsMowerMap(EcovacsEntity[Capabilities], ImageEntity):
    """The mower's map, rendered as SVG."""

    _attr_content_type = "image/svg+xml"
    # The position moves every couple of seconds while mowing; the recorder
    # has no use for a row per move.
    _unrecorded_attributes = frozenset(
        {ATTR_POSITION_X, ATTR_POSITION_Y, ATTR_HEADING}
    )
    entity_description = ImageEntityDescription(key="map", translation_key="map")

    def __init__(
        self, device: Device, mower_map: MowerMap, hass: HomeAssistant
    ) -> None:
        """Initialize the map image."""
        # hass rides through EcovacsEntity's **kwargs: its
        # super().__init__(**kwargs) resolves to ImageEntity.__init__ in
        # this MRO, which requires hass. An explicit second call to
        # ImageEntity.__init__ would never be reached — the chained call
        # raises TypeError first.
        super().__init__(device, device.capabilities, hass=hass)
        self._map = mower_map
        self._attr_image_last_updated = dt_util.utcnow()

    @override
    async def async_added_to_hass(self) -> None:
        """Subscribe to the events that make the image stale."""
        await super().async_added_to_hass()

        self._subscribe(MowerMapInfoEvent, self._on_geometry)
        self._subscribe(MowerObstaclesEvent, self._on_geometry)
        self._subscribe(MowerCoverageEvent, self._on_geometry)
        self._subscribe(MowerCoveredAreaEvent, self._on_geometry)
        self._subscribe(MowerNoGoZonesEvent, self._on_geometry)
        self._subscribe(PositionsEvent, self._on_positions)

    @property
    @override
    def extra_state_attributes(self) -> dict[str, int | None]:
        """Where the mower and the dock are, in mm in the map frame.

        Read from the map when the state is written rather than copied by
        a handler, because the controller owns the map and updates it
        independently of this entity. The position stays ``None`` until the
        first fix of a session: unlike the SVG marker, which falls back to
        the dock so there is always something to draw, a number that is
        not known should not pass for one that is.
        """
        position = self._map.position
        return {
            ATTR_POSITION_X: position[0] if position else None,
            ATTR_POSITION_Y: position[1] if position else None,
            ATTR_HEADING: self._map.heading if position else None,
            ATTR_DOCK_X: self._map.dock[0],
            ATTR_DOCK_Y: self._map.dock[1],
        }

    def _bump(self) -> None:
        self._attr_image_last_updated = dt_util.utcnow()
        self.async_write_ha_state()

    async def _on_geometry(
        self,
        event: MowerMapInfoEvent
        | MowerObstaclesEvent
        | MowerCoverageEvent
        | MowerCoveredAreaEvent
        | MowerNoGoZonesEvent,
    ) -> None:
        self._bump()

    async def _on_positions(self, event: PositionsEvent) -> None:
        # Throttled: a skipped bump is corrected by the next position half
        # a second later, or by the next geometry event.
        elapsed = dt_util.utcnow() - self._attr_image_last_updated
        if elapsed.total_seconds() >= POSITION_UPDATE_INTERVAL_SECONDS:
            self._bump()

    @override
    async def async_image(self) -> bytes | None:
        """Render the current map."""
        return render(self._map).encode()
