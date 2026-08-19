"""The rain flag for Ecovacs GOAT.

Not in Home Assistant core's ``ecovacs`` — there is no binary_sensor platform
there at all. It exists here because rain is the one reason a GOAT interrupts a
scheduled run that leaves no trace in the mower's state: the device pauses,
returns and docks exactly as it would after finishing, and the only lasting
evidence is ``isRainProtect`` in onProtectState.
"""

from __future__ import annotations

import logging
from typing import override

from deebot_client.capabilities import Capabilities, DeviceType
from deebot_client.device import Device

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EcovacsMowerConfigEntry
from .deebot_patch.commands import GetProtectState
from .deebot_patch.messages import MowerProtectStateEvent
from .entity import EcovacsEntity

_LOGGER = logging.getLogger(__name__)

ATTR_RAIN_DELAY = "rain_delay"


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: EcovacsMowerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the rain flag for each mower."""
    controller = config_entry.runtime_data
    async_add_entities(
        EcovacsRainSensor(device)
        for device in controller.devices
        if device.capabilities.device_type is DeviceType.MOWER
    )


class EcovacsRainSensor(EcovacsEntity[Capabilities], BinarySensorEntity):
    """Whether the mower currently considers mowing rained off."""

    entity_description = BinarySensorEntityDescription(
        key="rain",
        translation_key="rain",
        device_class=BinarySensorDeviceClass.MOISTURE,
    )

    def __init__(self, device: Device) -> None:
        """Initialize the rain flag."""
        super().__init__(device, device.capabilities)

    @override
    async def async_added_to_hass(self) -> None:
        """Subscribe to the protect-state events and ask for the current value."""
        await super().async_added_to_hass()

        self._subscribe(MowerProtectStateEvent, self._on_protect_state)

        # The device sends onProtectState only when a flag flips, and the event
        # bus has no refresh command for an event type the library does not know
        # about (get_refresh_commands returns [] for it). Without this the flag
        # would read "unknown" from a restart until the next change of weather.
        self.hass.async_create_task(self._async_request_protect_state())

    @override
    async def async_update(self) -> None:
        """Re-read the flag, for homeassistant.update_entity."""
        await super().async_update()
        await self._async_request_protect_state()

    async def _async_request_protect_state(self) -> None:
        try:
            await self._device.execute_command(GetProtectState())
        except Exception:
            # getProtectState is unconfirmed against hardware (see the command's
            # docstring). A firmware that does not know it must cost nothing more
            # than an unknown flag, so nothing here may propagate. The library
            # does the same in its own availability worker.
            _LOGGER.debug("Could not read the protection state", exc_info=True)

    async def _on_protect_state(self, event: MowerProtectStateEvent) -> None:
        self._attr_is_on = event.raining
        self._attr_extra_state_attributes = {ATTR_RAIN_DELAY: event.rain_delay}
        self.async_write_ha_state()
