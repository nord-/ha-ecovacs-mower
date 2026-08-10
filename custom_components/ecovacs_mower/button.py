"""Ecovacs button module.

Forked from Home Assistant core (``homeassistant/components/ecovacs/button.py``).
``relocate`` has been removed: it is gated on ``caps.map``, which the GOAT mower
(2i0fns) does not have. ``STATION_ENTITY_DESCRIPTIONS`` and
``EcovacsStationActionButtonEntity`` describe a vacuum station's emptying and mop
drying — no buttons a mower has — and have been removed along with the import of
``SUPPORTED_STATION_ACTIONS``.

Added beyond core: ``play_sound``. The capability exists on ``2i0fns`` but is not
exposed by the core integration. It reuses core's existing
``EcovacsButtonEntity``/``EcovacsButtonEntityDescription`` — no new entity class
is needed, see ``play_sound: CapabilityExecute[[]]`` in
``deebot_client/capabilities.py``.

The annotation on ``EcovacsButtonEntity.entity_description`` is corrected
relative to core, which states ``EcovacsLifespanButtonEntityDescription`` —
likely a copy-paste slip, since the class never uses the lifespan description.
Harmless at runtime, but an incorrect type in a fork is harder to spot than in
upstream.
"""

from dataclasses import dataclass
from typing import override

from deebot_client.capabilities import CapabilityExecute, CapabilityLifeSpan
from deebot_client.events import LifeSpan

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EcovacsMowerConfigEntry
from .const import SUPPORTED_LIFESPANS
from .entity import (
    EcovacsCapabilityEntityDescription,
    EcovacsDescriptionEntity,
    EcovacsEntity,
)
from .util import get_supported_entities


@dataclass(kw_only=True, frozen=True)
class EcovacsButtonEntityDescription(
    ButtonEntityDescription,
    EcovacsCapabilityEntityDescription,
):
    """Ecovacs button entity description."""


@dataclass(kw_only=True, frozen=True)
class EcovacsLifespanButtonEntityDescription(ButtonEntityDescription):
    """Ecovacs lifespan button entity description."""

    component: LifeSpan


ENTITY_DESCRIPTIONS: tuple[EcovacsButtonEntityDescription, ...] = (
    EcovacsButtonEntityDescription(
        capability_fn=lambda caps: caps.play_sound,
        key="play_sound",
        translation_key="play_sound",
        # Deliberately without entity_category. Locating the mower is an action
        # you reach for when it is stuck, not diagnostic data, so it belongs
        # among the controls. HA also does not expose diagnostic and
        # configuration entities to voice assistants by default — and asking the
        # mower to make a sound is exactly what you want to be able to do
        # without first hunting it down in the UI.
    ),
)


LIFESPAN_ENTITY_DESCRIPTIONS = tuple(
    EcovacsLifespanButtonEntityDescription(
        component=component,
        key=f"reset_lifespan_{component.name.lower()}",
        translation_key=f"reset_lifespan_{component.name.lower()}",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    )
    for component in SUPPORTED_LIFESPANS
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: EcovacsMowerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add entities for passed config_entry in HA."""
    controller = config_entry.runtime_data
    entities: list[EcovacsEntity] = get_supported_entities(
        controller, EcovacsButtonEntity, ENTITY_DESCRIPTIONS
    )
    entities.extend(
        EcovacsResetLifespanButtonEntity(
            device, device.capabilities.life_span, description
        )
        for device in controller.devices
        for description in LIFESPAN_ENTITY_DESCRIPTIONS
        if description.component in device.capabilities.life_span.types
    )
    async_add_entities(entities)


class EcovacsButtonEntity(
    EcovacsDescriptionEntity[CapabilityExecute],
    ButtonEntity,
):
    """Ecovacs button entity."""

    entity_description: EcovacsButtonEntityDescription

    @override
    async def async_press(self) -> None:
        """Press the button."""
        await self._device.execute_command(self._capability.execute())


class EcovacsResetLifespanButtonEntity(
    EcovacsDescriptionEntity[CapabilityLifeSpan],
    ButtonEntity,
):
    """Ecovacs reset lifespan button entity."""

    entity_description: EcovacsLifespanButtonEntityDescription

    @override
    async def async_press(self) -> None:
        """Press the button."""
        await self._device.execute_command(
            self._capability.reset(self.entity_description.component)
        )
