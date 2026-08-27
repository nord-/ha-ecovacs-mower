"""Binary sensors for the protection flags GOAT reports.

Not in HA core's ecovacs, which has no binary_sensor platform at all: the flags
come from ``onProtectState``, one of the unsolicited messages the library has no
handler for either (see ``deebot_patch.messages``).

These are the device's raw flags and nothing derives the mower's state from
them; the rain-aware states on ``sensor.<device>_activity`` come from the
``trigger`` field instead. ``isRainProtect`` is the rain sensor's own reading —
wet or dry, and not whether rain protection is switched on — which is why
``rain_protect`` is the one flag here that carries a device class. The same
evidence shows ``animal_protect`` is not its setting either, but nothing
positive is established there, so it keeps the wire field's name (issue #45).
``deebot_patch.messages`` has the evidence.

These entities are refreshed by ``GetProtectState``, which
``deebot_patch.hardware`` wires to ``MowerProtectStateEvent`` because the
library's ``Capabilities`` has no field for the flags. The event bus asks for it
when the first of these entities subscribes, so the flags have a value from
startup instead of waiting for the device to push a change — which it only does
when a flag flips, so a dry spell with nothing else happening produces no push
at all (issue #31).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import override

from deebot_client.capabilities import Capabilities, DeviceType
from deebot_client.device import Device

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import ATTR_CODE, CONF_DESCRIPTION, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EcovacsMowerConfigEntry
from .deebot_patch.messages import MowerProtectStateEvent
from .entity import EcovacsDescriptionEntity, EcovacsEntity
from .fault import MowerFaultEvent


@dataclass(kw_only=True, frozen=True)
class EcovacsProtectStateBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describes a binary sensor backed by one onProtectState flag.

    There is no ``capability_fn`` here, unlike the other declarative platforms:
    the flags are not a deebot-client capability, so these entities cannot be
    built by ``util.get_supported_entities``.
    """

    value_fn: Callable[[MowerProtectStateEvent], bool]


ENTITY_DESCRIPTIONS: tuple[EcovacsProtectStateBinarySensorEntityDescription, ...] = (
    # MOISTURE reads "Wet"/"Dry", which is what this flag reports: it is the
    # rain sensor, not the rain-protection setting. Two samples with the
    # setting switched on in both rule the setting out — 1 two seconds before a
    # rain-stopped run, 0 on a dry day with the mower parked under cover. They
    # do not separate a wet sensor from a mower currently held for rain, which
    # moisture fits either way. See MowerProtectStateEvent for the full
    # evidence.
    #
    # The key stays "rain_protect" although the display name no longer says
    # that: unique_id is built from it (entity.py), so renaming the key would
    # orphan every existing entity and its history for a cosmetic gain.
    EcovacsProtectStateBinarySensorEntityDescription(
        key="rain_protect",
        translation_key="rain_protect",
        device_class=BinarySensorDeviceClass.MOISTURE,
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
    # The name is provisional. The dry-day sample that settled rain_protect had
    # isAnimProtect: 0 with animal protection switched on in the app, so this
    # flag is not the setting either. Unlike rain_protect there is no positive
    # reading to rename it to — "an animal is detected" and "the mower is
    # holding for an animal" both fit the one sample we have — so it keeps the
    # wire field's name and no device class until a sighting says which
    # (issue #45).
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
    entities: list[EcovacsEntity] = [
        EcovacsProtectStateBinarySensor(device, device.capabilities, description)
        for device in controller.devices
        if device.capabilities.device_type is DeviceType.MOWER
        for description in ENTITY_DESCRIPTIONS
    ]
    entities.extend(
        EcovacsFaultBinarySensor(device)
        for device in controller.devices
        if device.capabilities.device_type is DeviceType.MOWER
    )
    async_add_entities(entities)


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


class EcovacsFaultBinarySensor(
    EcovacsEntity[Capabilities],
    BinarySensorEntity,
):
    """Whether the mower has a fault nobody has dealt with yet.

    Issue #53. The distinction from ``sensor.<device>_error`` is the whole
    point: that sensor answers "what did the device last say", which on
    firmware 1.36.208 was ``0`` for the 38 minutes a jammed mower sat on the
    lawn. This one answers "is there an unresolved fault", and only something
    that cannot coexist with one clears it. The code and its text ride along as
    attributes so a notification can name the fault without reading the other
    entity — and so an automation can trigger on ``attribute: code`` when a
    second, different fault arrives while this is already on.

    Enabled by default, and not diagnostic: this is the entity the issue asked
    for, and one nobody finds is one nobody is warned by. The state machine is
    in ``fault.py`` and lives on the controller, so disabling this entity stops
    the reporting, never the latching.
    """

    # Off, not unknown, before the first event. The latch publishes only when
    # it changes, so a mower that has never faulted publishes nothing at all —
    # and "unknown" on a problem sensor reads as "something is wrong with the
    # sensor" when the truth is that nothing is wrong with the mower.
    _attr_is_on = False

    # The description is static text derived from the code, so recording it
    # every time the fault changes buys nothing. Same reasoning as on the error
    # sensor. The code itself is recorded — that is the history worth keeping.
    _unrecorded_attributes = frozenset({CONF_DESCRIPTION})
    entity_description = BinarySensorEntityDescription(
        key="fault",
        translation_key="fault",
        device_class=BinarySensorDeviceClass.PROBLEM,
    )

    def __init__(self, device: Device) -> None:
        """Initialize entity."""
        super().__init__(device, device.capabilities)

    @override
    async def async_added_to_hass(self) -> None:
        """Set up the event listeners now that hass is ready."""
        await super().async_added_to_hass()

        async def on_event(event: MowerFaultEvent) -> None:
            self._attr_is_on = event.code is not None
            self._attr_extra_state_attributes = {
                ATTR_CODE: event.code,
                CONF_DESCRIPTION: event.description,
            }
            self.async_write_ha_state()

        self._subscribe(MowerFaultEvent, on_event)
