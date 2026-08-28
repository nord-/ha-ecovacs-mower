"""Ecovacs number module.

Forked from Home Assistant core (``homeassistant/components/ecovacs/number.py``).
The descriptions for ``clean_count`` and ``water_amount`` have been removed: they
require capabilities (``clean.count`` and ``water.amount`` respectively) that the
GOAT mower (2i0fns) does not declare, so ``get_supported_entities`` would have
filtered them out anyway — but dead descriptions do not belong in a
mower-specific fork. ``volume`` and ``cut_direction`` are kept: both are the
mower's own settings (notification sound volume and the line orientation of the
mowing pattern, respectively).

Core's ``EcovacsNumberEntity.__init__`` reads min/max off the capability if it is
a ``CapabilityNumber`` (e.g. ``water_amount``, ``mop_auto_wash_frequency`` — mop
features a lawn mower does not have). ``volume`` and ``cut_direction`` are both
plain ``CapabilitySet``, not ``CapabilityNumber``, so that branch never triggers
here and has been removed along with the import.

``EcovacsRainDelayNumber`` is an addition rather than a fork: it is the second
half of the rain sensor's setting, the minutes the mower waits before resuming
after rain (issue #54). Like the switch that carries the first half, it sits
outside ``ENTITY_DESCRIPTIONS`` because the setting is not a deebot-client
capability.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import override

from deebot_client.capabilities import Capabilities, CapabilitySet, DeviceType
from deebot_client.device import Device
from deebot_client.events import CutDirectionEvent, VolumeEvent
from deebot_client.events.base import Event

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import DEGREE, EntityCategory, UnitOfTime
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
class EcovacsNumberEntityDescription[EventT: Event](
    NumberEntityDescription,
    EcovacsCapabilityEntityDescription,
):
    """Ecovacs number entity description."""

    native_max_value_fn: Callable[[EventT], float | int | None] = lambda _: None
    value_fn: Callable[[EventT], float | None]


ENTITY_DESCRIPTIONS: tuple[EcovacsNumberEntityDescription, ...] = (
    EcovacsNumberEntityDescription[VolumeEvent](
        capability_fn=lambda caps: caps.settings.volume,
        value_fn=lambda e: e.volume,
        native_max_value_fn=lambda e: e.maximum,
        key="volume",
        translation_key="volume",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.CONFIG,
        native_min_value=0,
        native_max_value=10,
        native_step=1.0,
    ),
    EcovacsNumberEntityDescription[CutDirectionEvent](
        capability_fn=lambda caps: caps.settings.cut_direction,
        value_fn=lambda e: e.angle,
        key="cut_direction",
        translation_key="cut_direction",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.CONFIG,
        native_min_value=0,
        native_max_value=180,
        native_step=1.0,
        native_unit_of_measurement=DEGREE,
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
        controller, EcovacsNumberEntity, ENTITY_DESCRIPTIONS
    )
    entities.extend(
        EcovacsRainDelayNumber(device)
        for device in controller.devices
        if device.capabilities.device_type is DeviceType.MOWER
    )
    if entities:
        async_add_entities(entities)


class EcovacsNumberEntity[EventT: Event](
    EcovacsDescriptionEntity[CapabilitySet[EventT, [int]]],
    NumberEntity,
):
    """Ecovacs number entity."""

    entity_description: EcovacsNumberEntityDescription

    @override
    async def async_added_to_hass(self) -> None:
        """Set up the event listeners now that hass is ready."""
        await super().async_added_to_hass()

        async def on_event(event: EventT) -> None:
            self._attr_native_value = self.entity_description.value_fn(event)
            if maximum := self.entity_description.native_max_value_fn(event):
                self._attr_native_max_value = maximum
            self.async_write_ha_state()

        self._subscribe(self._capability.event, on_event)

    @override
    async def async_set_native_value(self, value: float) -> None:
        """Set new value."""
        await self._execute_command(self._capability.set(int(value)))


class EcovacsRainDelayNumber(
    EcovacsEntity[Capabilities],
    NumberEntity,
):
    """How long the mower waits before resuming after rain.

    The other half of the rain sensor's setting; the switch that carries the
    first half is in ``switch.py``. Not the same thing as
    ``binary_sensor.<device>_rain_delay``, which is the device's own flag for
    whether it is holding right now — this is the configured length of that
    hold.

    The unit is minutes: the reporter's app read three hours against a payload
    of ``"delay": 180``. The range mirrors Janverhu/ecovacs-goat-g1, which
    drives the same command against a GOAT G1 — a day is a generous ceiling and
    the firmware's real one is not known. A box rather than a slider follows
    from the range: a 0-1440 slider cannot be aimed at a particular hour.
    """

    entity_description = NumberEntityDescription(
        key="rain_delay",
        translation_key="rain_delay",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.CONFIG,
        native_min_value=0,
        native_max_value=1440,
        native_step=1,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        mode=NumberMode.BOX,
    )

    def __init__(self, device: Device) -> None:
        """Initialize entity."""
        super().__init__(device, device.capabilities)
        # The toggle half of the same setting, held for the same reason the
        # switch holds the delay — see ``async_set_native_value``.
        self._enabled: bool | None = None

    @override
    async def async_added_to_hass(self) -> None:
        """Set up the event listeners now that hass is ready."""
        await super().async_added_to_hass()

        async def on_event(event: MowerRainDelayEvent) -> None:
            self._attr_native_value = event.delay
            self._enabled = event.enabled
            self.async_write_ha_state()

        self._subscribe(MowerRainDelayEvent, on_event)

    @override
    async def async_set_native_value(self, value: float) -> None:
        """Send the new delay, with the sensor state the device last reported.

        The mirror image of the switch: ``setRainDelay`` carries both fields, so
        writing the duration alone would switch the rain sensor to whatever this
        entity assumed. Refusing until the device has said is the only answer
        that cannot silently disable it.
        """
        if self._enabled is None:
            raise HomeAssistantError(
                "The mower has not reported whether its rain sensor is on, so "
                "the delay cannot be set without risking switching the sensor "
                "off. Wait for the mower to report its setting"
            )

        await self._execute_command(
            SetRainDelay(enable=self._enabled, delay=int(value))
        )
