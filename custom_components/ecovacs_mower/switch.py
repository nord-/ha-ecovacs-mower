"""Ecovacs switch module.

Forked from Home Assistant core (``homeassistant/components/ecovacs/switch.py``).
The descriptions for ``continuous_cleaning``, ``carpet_auto_fan_boost`` and
``clean_preference`` have been removed: they require capabilities the GOAT mower
(2i0fns) does not declare, so ``get_supported_entities`` would have filtered them
out anyway — but dead descriptions do not belong in a mower-specific fork.
``border_spin`` has also been removed: that is edge brushing on a vacuum, not
edge mowing. ``border_switch`` is the mower's edge-mowing setting and is kept.

``EcovacsRainDetectionSwitch`` is an addition rather than a fork: the rain
sensor is the one row of the app's Configuration page with no entity behind it
(issue #54). It sits outside ``ENTITY_DESCRIPTIONS`` because the setting is not
a deebot-client capability, the same position the protection-flag binary
sensors are in.
"""

from dataclasses import dataclass
from typing import Any, override

from deebot_client.capabilities import Capabilities, CapabilitySetEnable, DeviceType
from deebot_client.device import Device
from deebot_client.events import EnableEvent

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EcovacsMowerConfigEntry
from .deebot_patch.commands import SetRainDelay
from .deebot_patch.messages import MowerRainDelayEvent
from .entity import (
    EcovacsCapabilityEntityDescription,
    EcovacsDescriptionEntity,
    EcovacsEntity,
)
from .util import get_supported_entities


@dataclass(kw_only=True, frozen=True)
class EcovacsSwitchEntityDescription(
    SwitchEntityDescription,
    EcovacsCapabilityEntityDescription[CapabilitySetEnable],
):
    """Ecovacs switch entity description."""


ENTITY_DESCRIPTIONS: tuple[EcovacsSwitchEntityDescription, ...] = (
    EcovacsSwitchEntityDescription(
        capability_fn=lambda c: c.settings.advanced_mode,
        key="advanced_mode",
        translation_key="advanced_mode",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.CONFIG,
    ),
    EcovacsSwitchEntityDescription(
        capability_fn=lambda c: c.settings.true_detect,
        key="true_detect",
        translation_key="true_detect",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.CONFIG,
    ),
    EcovacsSwitchEntityDescription(
        capability_fn=lambda c: c.settings.border_switch,
        key="border_switch",
        translation_key="border_switch",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.CONFIG,
    ),
    EcovacsSwitchEntityDescription(
        capability_fn=lambda c: c.settings.child_lock,
        key="child_lock",
        translation_key="child_lock",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.CONFIG,
    ),
    EcovacsSwitchEntityDescription(
        capability_fn=lambda c: c.settings.moveup_warning,
        key="move_up_warning",
        translation_key="move_up_warning",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.CONFIG,
    ),
    EcovacsSwitchEntityDescription(
        capability_fn=lambda c: c.settings.cross_map_border_warning,
        key="cross_map_border_warning",
        translation_key="cross_map_border_warning",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.CONFIG,
    ),
    EcovacsSwitchEntityDescription(
        capability_fn=lambda c: c.settings.safe_protect,
        key="safe_protect",
        translation_key="safe_protect",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.CONFIG,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: EcovacsMowerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add entities for passed config_entry in HA."""
    controller = config_entry.runtime_data
    entities: list[EcovacsEntity] = get_supported_entities(
        controller, EcovacsSwitchEntity, ENTITY_DESCRIPTIONS
    )
    entities.extend(
        EcovacsRainDetectionSwitch(device)
        for device in controller.devices
        if device.capabilities.device_type is DeviceType.MOWER
    )
    if entities:
        async_add_entities(entities)


class EcovacsSwitchEntity(
    EcovacsDescriptionEntity[CapabilitySetEnable],
    SwitchEntity,
):
    """Ecovacs switch entity."""

    entity_description: EcovacsSwitchEntityDescription

    _attr_is_on = False

    @override
    async def async_added_to_hass(self) -> None:
        """Set up the event listeners now that hass is ready."""
        await super().async_added_to_hass()

        async def on_event(event: EnableEvent) -> None:
            self._attr_is_on = event.enabled
            self.async_write_ha_state()

        self._subscribe(self._capability.event, on_event)

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the entity on."""
        await self._execute_command(self._capability.set(True))

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the entity off."""
        await self._execute_command(self._capability.set(False))


class EcovacsRainDetectionSwitch(
    EcovacsEntity[Capabilities],
    SwitchEntity,
):
    """Whether the mower's rain sensor is allowed to stop a run.

    The setting the app's Configuration page calls the rain sensor, and the
    settings message calls ``RainDetect``. Not to be confused with
    ``binary_sensor.<device>_rain_sensor``, which is that sensor's live
    reading — this switch is what decides whether the mower listens to it at
    all.

    Not built from ``ENTITY_DESCRIPTIONS``: ``get_supported_entities`` needs a
    ``capability_fn``, and ``Capabilities`` has no field for this setting. Same
    reason the protection-flag binary sensors have their own platform setup.

    Disabled by default, like the seven capability-backed settings switches
    above.
    """

    entity_description = SwitchEntityDescription(
        key="rain_detection",
        translation_key="rain_detection",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.CONFIG,
    )

    def __init__(self, device: Device) -> None:
        """Initialize entity."""
        super().__init__(device, device.capabilities)
        # The duration half of the same setting. Held here because
        # ``setRainDelay`` carries both fields, so the toggle cannot be sent
        # without it — see ``_set``.
        self._delay: int | None = None

    @override
    async def async_added_to_hass(self) -> None:
        """Set up the event listeners now that hass is ready."""
        await super().async_added_to_hass()

        async def on_event(event: MowerRainDelayEvent) -> None:
            self._attr_is_on = event.enabled
            self._delay = event.delay
            self.async_write_ha_state()

        self._subscribe(MowerRainDelayEvent, on_event)

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the entity on."""
        await self._set(True)

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the entity off."""
        await self._set(False)

    async def _set(self, enable: bool) -> None:
        """Send the toggle, with the delay the device last reported.

        The device wants the pair, so this entity has to resend a value it does
        not own. Refusing when it is unknown is deliberate: any default here is
        a hold the owner never chose, written to the mower as if they had. The
        delay is normally known — ``GetRainDelay`` fetches both fields when the
        first of the two entities subscribes — so this is the narrow case of a
        firmware that answers without one.
        """
        if self._delay is None:
            raise HomeAssistantError(
                "The mower has not reported its rain delay, so the rain sensor "
                "cannot be switched without overwriting it. Set the rain delay "
                "first, or wait for the mower to report one"
            )

        await self._execute_command(SetRainDelay(enable=enable, delay=self._delay))
