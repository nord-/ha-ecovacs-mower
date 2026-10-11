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

``EcovacsAnimalProtectionSwitch`` is an addition of the same kind, for the
animal-protection setting (issue #45).
"""

from dataclasses import dataclass
from datetime import time
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
from .deebot_patch.commands import SetAnimProtect, SetRainDelay
from .deebot_patch.messages import MowerAnimProtectEvent, MowerRainDelayEvent
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
    for device in controller.devices:
        if device.capabilities.device_type is DeviceType.MOWER:
            entities.append(EcovacsRainDetectionSwitch(device))
            entities.append(EcovacsAnimalProtectionSwitch(device))
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

        ``SetRainDelay``'s docstring claims the device pushes ``onRainDelay`` on
        its own answer too, but that has not been confirmed against hardware. If
        it does not, requesting a refresh is what re-reads the state instead of
        leaving the frontend's optimistic value to flip back once its timeout
        expires.
        """
        if self._delay is None:
            raise HomeAssistantError(
                "The mower has not reported its rain delay, so the rain sensor "
                "cannot be switched without overwriting it. Set the rain delay "
                "first, or wait for the mower to report one"
            )

        await self._execute_command(SetRainDelay(enable=enable, delay=self._delay))
        self._device.events.request_refresh(MowerRainDelayEvent)


class EcovacsAnimalProtectionSwitch(
    EcovacsEntity[Capabilities],
    SwitchEntity,
):
    """Whether animal protection is switched on.

    The setting, not ``binary_sensor.<device>_animal_protect``: that one is
    whether the protection is in effect right now, which it is only inside the
    nightly window this switch's attributes show. Turned on at noon, the switch
    reads on and the binary sensor stays off until the window opens.

    Only the toggle is exposed. The window is shown, not editable: it is
    resent unchanged on every toggle, as the app does, and editing it is left
    to the app.

    Disabled by default, like every other settings switch here.
    """

    entity_description = SwitchEntityDescription(
        key="animal_protection",
        translation_key="animal_protection",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.CONFIG,
    )

    def __init__(self, device: Device) -> None:
        """Initialize entity."""
        super().__init__(device, device.capabilities)
        # The window half of the same setting. Held here because
        # ``setAnimProtect`` carries all three fields — see ``_set``.
        self._start: time | None = None
        self._end: time | None = None

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """The window the protection applies in, as the app shows it."""
        return {
            "start": self._start.strftime("%H:%M") if self._start else None,
            "end": self._end.strftime("%H:%M") if self._end else None,
        }

    @override
    async def async_added_to_hass(self) -> None:
        """Set up the event listeners now that hass is ready."""
        await super().async_added_to_hass()

        async def on_event(event: MowerAnimProtectEvent) -> None:
            self._attr_is_on = event.enabled
            self._start = event.start
            self._end = event.end
            self.async_write_ha_state()

        self._subscribe(MowerAnimProtectEvent, on_event)

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the entity on."""
        await self._set(True)

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the entity off."""
        await self._set(False)

    async def _set(self, enable: bool) -> None:
        """Send the toggle, with the window the device last reported.

        Refused while the window is unknown, for the reason the rain switch
        refuses without a delay: any default is a window the owner never
        chose, written to the mower as if they had. A refresh follows the
        write for the same reason as there, too.
        """
        if self._start is None or self._end is None:
            raise HomeAssistantError(
                "The mower has not reported its animal protection window, so "
                "the setting cannot be switched without overwriting it. Wait "
                "for the mower to report it, or change it in the Ecovacs app"
            )

        await self._execute_command(
            SetAnimProtect(enable=enable, start=self._start, end=self._end)
        )
        self._device.events.request_refresh(MowerAnimProtectEvent)
