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

Added beyond core as well: the mower-command buttons, ``mow_border`` (issue
#12) and ``end_task`` (issue #51). Neither is a library capability — one needs
the device's recorded map id, the other sends a ``CleanAction`` HA's
``lawn_mower`` platform has no feature for — so they are described by
``EcovacsMowerCommandButtonEntityDescription``, whose ``command_fn`` builds
the command from the device at press time. A third such button is one more
entry in ``MOWER_COMMAND_DESCRIPTIONS``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import override

from deebot_client.capabilities import (
    Capabilities,
    CapabilityExecute,
    CapabilityLifeSpan,
    DeviceType,
)
from deebot_client.command import Command
from deebot_client.device import Device
from deebot_client.events import LifeSpan
from deebot_client.models import CleanAction

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EcovacsMowerConfigEntry
from .const import SUPPORTED_LIFESPANS
from .controller import EcovacsController
from .deebot_patch.border import MowBorder
from .deebot_patch.commands import CleanMower
from .deebot_patch.hardware import MowerProfile, profile_for_class
from .deebot_patch.map_messages import MowerMapInfoEvent
from .deebot_patch.state_precedence import map_id_for
from .entity import (
    EcovacsCapabilityEntityDescription,
    EcovacsDescriptionEntity,
    EcovacsEntity,
)
from .fault import FaultLatch
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


def _border_command(device: Device) -> Command:
    """The border start for *device*'s current map, or a clear refusal.

    The id is learned from the map messages' envelopes (state_precedence), and
    on the one class that has this button the mower answers getMapInfo_V2
    within seconds of setup, so an unknown id is the rare case. Asking for
    that refresh here makes the failure heal itself: the next press works —
    unless map setup itself failed (see controller._setup_map), in which case
    there is no MowerMapInfoEvent subscriber, the refresh is a no-op, and
    every press keeps answering the same way; the controller's warning log
    is the only sign of that. request_refresh() is untested here because the
    tests mock device.events rather than a live EventBus.
    """
    map_id = map_id_for(device.events)
    if map_id is None:
        device.events.request_refresh(MowerMapInfoEvent)
        raise HomeAssistantError(
            "The mower has not reported its map yet; try again in a moment. "
            "If this persists, check the log for a map setup failure."
        )
    return MowBorder(map_id)


@dataclass(kw_only=True, frozen=True)
class EcovacsMowerCommandButtonEntityDescription(ButtonEntityDescription):
    """A button that sends one command built from the device at press time."""

    command_fn: Callable[[Device], Command]
    # None means every mower, the unsupported classes included. A function
    # limits the button to supported classes whose MowerProfile says the
    # command's request shape is confirmed there; an unsupported class has no
    # profile and so never passes it.
    profile_fn: Callable[[MowerProfile], bool] | None = None
    # A command that takes the mower off its dock never produces a StateEvent
    # on its own, so the controller's poll has to be nudged — the same nudge
    # lawn_mower.py gives start_mowing and mow_area.
    starts_job: bool = False


MOWER_COMMAND_DESCRIPTIONS: tuple[EcovacsMowerCommandButtonEntityDescription, ...] = (
    EcovacsMowerCommandButtonEntityDescription(
        key="mow_border",
        translation_key="mow_border",
        command_fn=_border_command,
        profile_fn=lambda profile: profile.border_mowing,
        starts_job=True,
        # No entity_category, for the reason play_sound gives above: a
        # control, not diagnostics or configuration.
    ),
    EcovacsMowerCommandButtonEntityDescription(
        key="end_task",
        translation_key="end_task",
        command_fn=lambda device: CleanMower(CleanAction.STOP),
        # Every supported mower. The V2 payload is the app's own, captured on
        # issue #51; the non-V2 payload is the shape pause already uses on
        # that hardware, and the reporter there owns the mower that has to
        # confirm it.
        profile_fn=None,
        starts_job=False,
    ),
)


def _mower_command_entities(
    controller: EcovacsController,
) -> list[EcovacsMowerCommandButtonEntity]:
    """One command button per mower per description whose profile gate passes."""
    return [
        EcovacsMowerCommandButtonEntity(device, controller, description)
        for device in controller.devices
        if device.capabilities.device_type is DeviceType.MOWER
        for description in MOWER_COMMAND_DESCRIPTIONS
        if description.profile_fn is None
        or (
            (profile := profile_for_class(device.device_info["class"])) is not None
            and description.profile_fn(profile)
        )
    ]


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
    entities.extend(
        EcovacsClearFaultButtonEntity(
            device, controller.fault_latches[device.device_info["did"]]
        )
        for device in controller.devices
        if device.capabilities.device_type is DeviceType.MOWER
    )
    entities.extend(_mower_command_entities(controller))
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
        await self._execute_command(self._capability.execute())


class EcovacsResetLifespanButtonEntity(
    EcovacsDescriptionEntity[CapabilityLifeSpan],
    ButtonEntity,
):
    """Ecovacs reset lifespan button entity."""

    entity_description: EcovacsLifespanButtonEntityDescription

    @override
    async def async_press(self) -> None:
        """Press the button."""
        await self._execute_command(
            self._capability.reset(self.entity_description.component)
        )


class EcovacsClearFaultButtonEntity(
    EcovacsEntity[Capabilities],
    ButtonEntity,
):
    """Release the latched fault on ``binary_sensor.<device>_fault``.

    Issue #53. The only button here that sends nothing to the mower: the latch
    is ours, and the device has no notion of an acknowledged fault. That is
    also why it exists at all — docking and starting a job are the device's
    signals, and if some firmware never reports either, this is what keeps the
    latch from being permanently stuck. It needs no cooperation from the mower.

    ``_always_available`` for the same reason: the latch is local state, so
    clearing it must work while the mower is unreachable — which is exactly
    when a stale fault is most likely to be the thing bothering someone.
    """

    _always_available = True
    entity_description = ButtonEntityDescription(
        key="clear_fault",
        translation_key="clear_fault",
        # Not diagnostic and not config: it is an action taken in response to
        # the fault entity, and belongs next to the controls, for the same
        # reason play_sound does above.
    )

    def __init__(self, device: Device, latch: FaultLatch) -> None:
        """Initialize entity."""
        super().__init__(device, device.capabilities)
        self._latch = latch

    @override
    async def async_press(self) -> None:
        """Press the button."""
        self._latch.clear_by_request()


class EcovacsMowerCommandButtonEntity(
    EcovacsEntity[Capabilities],
    ButtonEntity,
):
    """A button whose command is built from the device when pressed.

    Issues #12 and #51. ``entity_description`` is assigned before the base
    ``__init__`` runs because ``EcovacsEntity.__init__`` reads its ``key`` for
    the unique id — the same order ``EcovacsDescriptionEntity`` uses.
    """

    entity_description: EcovacsMowerCommandButtonEntityDescription

    def __init__(
        self,
        device: Device,
        controller: EcovacsController,
        description: EcovacsMowerCommandButtonEntityDescription,
    ) -> None:
        """Initialize entity."""
        self.entity_description = description
        super().__init__(device, device.capabilities)
        self._controller = controller

    @override
    async def async_press(self) -> None:
        """Press the button."""
        # Built first: a refusal (no map id yet) must not restart the poll
        # for a job that is not going to start.
        command = self.entity_description.command_fn(self._device)
        if self.entity_description.starts_job:
            self._controller.start_polling(self._device)
        await self._execute_command(command)
