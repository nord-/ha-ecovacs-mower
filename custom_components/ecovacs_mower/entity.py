"""Ecovacs mqtt entity module.

Forked from Home Assistant core (``homeassistant/components/ecovacs/entity.py``).
The base class for XMPP-connected devices and its dependency on the legacy
client library have been removed: this integration only supports MQTT.
"""

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
import logging
from typing import Any, override

from deebot_client.capabilities import Capabilities
from deebot_client.command import Command
from deebot_client.device import Device
from deebot_client.events import AvailabilityEvent
from deebot_client.events.base import Event

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity, EntityDescription

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class EcovacsEntity[CapabilityEntityT](Entity):
    """Ecovacs entity."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _always_available: bool = False

    def __init__(
        self,
        device: Device,
        capability: CapabilityEntityT,
        **kwargs: Any,
    ) -> None:
        """Initialize entity."""
        super().__init__(**kwargs)
        self._attr_unique_id = (
            f"{device.device_info['did']}_{self.entity_description.key}"
        )

        self._device = device
        self._capability = capability
        self._subscribed_events: set[type[Event]] = set()

    @property
    @override
    def device_info(self) -> DeviceInfo | None:
        """Return device specific attributes."""
        device_info = self._device.device_info
        info = DeviceInfo(
            identifiers={(DOMAIN, device_info["did"])},
            manufacturer="Ecovacs",
            sw_version=self._device.fw_version,
            serial_number=device_info["name"],
            model_id=device_info["class"],
        )

        if nick := device_info.get("nick"):
            info["name"] = nick

        if model := device_info.get("deviceName"):
            info["model"] = model

        if mac := self._device.mac:
            info["connections"] = {(dr.CONNECTION_NETWORK_MAC, mac)}

        return info

    @override
    async def async_added_to_hass(self) -> None:
        """Set up the event listeners now that hass is ready."""
        await super().async_added_to_hass()

        if not self._always_available:

            async def on_available(event: AvailabilityEvent) -> None:
                self._attr_available = event.available
                self.async_write_ha_state()

            self._subscribe(AvailabilityEvent, on_available)

    async def _execute_command(self, command: Command) -> None:
        """Send *command* to the device, and log it here when it is not confirmed.

        ``Device.execute_command`` returns the raw response, which is an empty
        dict on every failure path: no answer from the mower (``errno`` 500),
        device offline (4200), a REST timeout, an unhandled response. The library
        logs the reason under ``deebot_client``, and those lines are invisible in
        the integration page's "Show logs" — that filter is the literal string
        ``ecovacs_mower`` (issue #26). Logging the fact here puts at least one
        line under a logger name that carries the domain, pointing at the log
        that has the detail.

        Not confirmed is not the same as not executed: the library documents
        ``device_reached`` as "responded in time", not "did what it was told", so
        this warns and never raises — a mower that starts mowing but answers too
        late must not surface as a failed service call.
        """
        if not await self._device.execute_command(command):
            _LOGGER.warning(
                "The mower did not confirm command %s. The reason is logged by "
                "deebot_client; enable debug logging for this integration to see "
                "the exchange",
                command.NAME,
            )

    def _subscribe[EventT: Event](
        self,
        event_type: type[EventT],
        callback: Callable[[EventT], Coroutine[Any, Any, None]],
    ) -> None:
        """Subscribe to events."""
        self._subscribed_events.add(event_type)
        self.async_on_remove(self._device.events.subscribe(event_type, callback))

    async def async_update(self) -> None:
        """Update the entity.

        Only used by the generic entity update service.
        """
        for event_type in self._subscribed_events:
            self._device.events.request_refresh(event_type)


class EcovacsDescriptionEntity[CapabilityEntityT](EcovacsEntity[CapabilityEntityT]):
    """Ecovacs entity."""

    def __init__(
        self,
        device: Device,
        capability: CapabilityEntityT,
        entity_description: EntityDescription,
        **kwargs: Any,
    ) -> None:
        """Initialize entity."""
        self.entity_description = entity_description
        super().__init__(device, capability, **kwargs)


@dataclass(kw_only=True, frozen=True)
class EcovacsCapabilityEntityDescription[CapabilityEntityT](
    EntityDescription,
):
    """Ecovacs entity description."""

    capability_fn: Callable[[Capabilities], CapabilityEntityT | None]
