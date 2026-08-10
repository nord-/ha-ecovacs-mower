"""The device_tracker entity for Ecovacs GOAT.

Not forked from Home Assistant core — core's ``ecovacs`` integration has no
device_tracker platform.

Unlike every other platform here this one is not built from
``ENTITY_DESCRIPTIONS``: ``GpsPositionEvent`` has no entry in ``Capabilities``,
so there is nothing for ``capability_fn`` to look up. ``deebot-client``
registers ``OnGpsPos`` in its global ``MESSAGES`` dict and ``get_message()``
resolves an exact name match before it consults the device's capabilities, so
subscribing to the event is all that is required — the patch layer stays out of
this entirely.

The event is push-only. There is no ``getGpsPos`` command to request it with,
which means the entity has no position until the mower sends its first message,
and ``async_update`` is a no-op for it.
"""

from __future__ import annotations

import logging
from typing import override

from deebot_client.capabilities import Capabilities, DeviceType
from deebot_client.device import Device
from deebot_client.events import GpsPositionEvent
from homeassistant.components.device_tracker import (
    SourceType,
    TrackerEntity,
    TrackerEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EcovacsMowerConfigEntry
from .entity import EcovacsEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: EcovacsMowerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a position tracker for every mower."""
    controller = config_entry.runtime_data
    # ponytail: added unconditionally for every mower. Whether a given model has
    # a GNSS receiver at all is not exposed anywhere we can read at setup time —
    # only the RTK models are known to send onGpsPos. A mower that never sends it
    # leaves the entity in "unknown", which is the honest state. Gate this on the
    # device class the day a capture proves which classes do and don't.
    trackers = [
        EcovacsMowerTracker(device)
        for device in controller.devices
        if device.capabilities.device_type is DeviceType.MOWER
    ]
    _LOGGER.debug("Adding mower position trackers: %s", trackers)
    async_add_entities(trackers)


class EcovacsMowerTracker(EcovacsEntity[Capabilities], TrackerEntity):
    """The mower's last reported GPS position."""

    _attr_source_type = SourceType.GPS

    entity_description = TrackerEntityDescription(
        key="position", translation_key="position"
    )

    def __init__(self, device: Device) -> None:
        """Initialize the tracker."""
        super().__init__(device, device.capabilities)

    @override
    async def async_added_to_hass(self) -> None:
        """Subscribe to GPS position events."""
        await super().async_added_to_hass()

        async def on_gps_position(event: GpsPositionEvent) -> None:
            self._attr_latitude = event.latitude
            self._attr_longitude = event.longitude
            self.async_write_ha_state()

        self._subscribe(GpsPositionEvent, on_gps_position)
