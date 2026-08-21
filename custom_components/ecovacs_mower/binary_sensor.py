"""Binary sensors for the protection flags GOAT reports.

Not in HA core's ecovacs, which has no binary_sensor platform at all: the flags
come from ``onProtectState``, one of the unsolicited messages the library has no
handler for either (see ``deebot_patch.messages``).

These are the device's raw flags and nothing derives the mower's state from
them: whether ``isRainProtect`` reports rain or only that rain protection is
enabled is unsettled, and ``deebot_patch.messages`` documents why. The
rain-aware states on ``sensor.<device>_activity`` come from the ``trigger``
field instead.

These entities are refreshed by ``GetProtectState``, which
``deebot_patch.hardware`` wires to ``MowerProtectStateEvent`` because the
library's ``Capabilities`` has no field for the flags. The event bus asks for it
when the first of these entities subscribes, so the flags have a value from
startup instead of waiting for the device to push a change — which it only does
when one flips, and rain protection that is simply left switched on never flips
(issue #31).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import override

from deebot_client.capabilities import Capabilities, DeviceType

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EcovacsMowerConfigEntry
from .deebot_patch.messages import MowerProtectStateEvent
from .entity import EcovacsDescriptionEntity


@dataclass(kw_only=True, frozen=True)
class EcovacsProtectStateBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describes a binary sensor backed by one onProtectState flag.

    There is no ``capability_fn`` here, unlike the other declarative platforms:
    the flags are not a deebot-client capability, so these entities cannot be
    built by ``util.get_supported_entities``.
    """

    value_fn: Callable[[MowerProtectStateEvent], bool]


ENTITY_DESCRIPTIONS: tuple[EcovacsProtectStateBinarySensorEntityDescription, ...] = (
    # No device class on purpose. BinarySensorDeviceClass.MOISTURE would make
    # this read "Wet"/"Dry", which claims the flag means "it is raining right
    # now" — and that is exactly what the single captured sample cannot show
    # (see MowerProtectStateEvent). "On"/"Off" for a flag named "Rain
    # protection" over-promises nothing.
    EcovacsProtectStateBinarySensorEntityDescription(
        key="rain_protect",
        translation_key="rain_protect",
        value_fn=lambda e: e.rain_protect,
    ),
    EcovacsProtectStateBinarySensorEntityDescription(
        key="rain_delay",
        translation_key="rain_delay",
        value_fn=lambda e: e.rain_delay,
    ),
    EcovacsProtectStateBinarySensorEntityDescription(
        key="emergency_stop",
        translation_key="emergency_stop",
        value_fn=lambda e: e.emergency_stop,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # Not the same thing as the child_lock switch: that one is a setting, this
    # is whether the mower is currently locked and refusing to move.
    EcovacsProtectStateBinarySensorEntityDescription(
        key="locked",
        translation_key="locked",
        value_fn=lambda e: e.locked,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EcovacsProtectStateBinarySensorEntityDescription(
        key="animal_protect",
        translation_key="animal_protect",
        value_fn=lambda e: e.animal_protect,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: EcovacsMowerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add entities for passed config_entry in HA."""
    controller = config_entry.runtime_data
    async_add_entities(
        EcovacsProtectStateBinarySensor(device, device.capabilities, description)
        for device in controller.devices
        if device.capabilities.device_type is DeviceType.MOWER
        for description in ENTITY_DESCRIPTIONS
    )


class EcovacsProtectStateBinarySensor(
    EcovacsDescriptionEntity[Capabilities],
    BinarySensorEntity,
):
    """One flag out of the mower's protection state."""

    entity_description: EcovacsProtectStateBinarySensorEntityDescription

    @override
    async def async_added_to_hass(self) -> None:
        """Set up the event listeners now that hass is ready."""
        await super().async_added_to_hass()

        async def on_event(event: MowerProtectStateEvent) -> None:
            self._attr_is_on = self.entity_description.value_fn(event)
            self.async_write_ha_state()

        self._subscribe(MowerProtectStateEvent, on_event)
