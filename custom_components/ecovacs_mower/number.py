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
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import override

from deebot_client.capabilities import CapabilitySet
from deebot_client.events import CutDirectionEvent, VolumeEvent
from deebot_client.events.base import Event

from homeassistant.components.number import NumberEntity, NumberEntityDescription
from homeassistant.const import DEGREE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EcovacsMowerConfigEntry
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
