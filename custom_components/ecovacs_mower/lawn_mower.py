"""The lawn_mower entity for Ecovacs GOAT."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import override

from deebot_client.capabilities import Capabilities, DeviceType
from deebot_client.device import Device
from deebot_client.events import StateEvent
from deebot_client.models import CleanAction, State
from homeassistant.components.lawn_mower import (
    LawnMowerActivity,
    LawnMowerEntity,
    LawnMowerEntityEntityDescription,
    LawnMowerEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EcovacsMowerConfigEntry
from .entity import EcovacsEntity

_LOGGER = logging.getLogger(__name__)

# The mower is not a reliable narrator of its own state. On 2026-08-21 it
# finished a run, drove home and started charging without sending
# onChargeInfo, onChargeState, or even the bury-point task events it logs for
# itself — while onStats, onBattery, onPos and onMapTrack all kept arriving.
# The entity stayed "mowing" for two hours; one homeassistant.update_entity
# corrected it in 200 ms, over REST, so the answer was there the whole time
# and nobody had asked for it.
#
# Five minutes: worst case the state is that stale, against two commands per
# interval on Ecovacs' cloud API. Push still does the fast path — this only
# catches what push drops.
SCAN_INTERVAL = timedelta(minutes=5)

# IDLE means "standing still", not "standing in the dock" — hence PAUSED.
# Docking is reported separately via onChargeInfo with state "idle", which
# deebot_patch.messages translates to State.DOCKED.
_STATE_TO_MOWER_STATE = {
    State.IDLE: LawnMowerActivity.PAUSED,
    State.CLEANING: LawnMowerActivity.MOWING,
    State.RETURNING: LawnMowerActivity.RETURNING,
    State.DOCKED: LawnMowerActivity.DOCKED,
    State.ERROR: LawnMowerActivity.ERROR,
    State.PAUSED: LawnMowerActivity.PAUSED,
}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: EcovacsMowerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the lawn mowers."""
    controller = config_entry.runtime_data
    mowers = [
        EcovacsMower(device)
        for device in controller.devices
        if device.capabilities.device_type is DeviceType.MOWER
    ]
    _LOGGER.debug("Adding mowers: %s", mowers)
    async_add_entities(mowers)


class EcovacsMower(EcovacsEntity[Capabilities], LawnMowerEntity):
    """An Ecovacs GOAT lawn mower."""

    # The base class does not poll, and every other platform here keeps it that
    # way: their events either arrive or are genuinely unknown. This one is the
    # exception because a wrong mower state drives automations. Its refresh
    # publishes StateEvent on the bus, so the activity sensor is corrected by
    # the same round trip rather than needing a poll of its own.
    _attr_should_poll = True

    _attr_supported_features = (
        LawnMowerEntityFeature.DOCK
        | LawnMowerEntityFeature.PAUSE
        | LawnMowerEntityFeature.START_MOWING
    )

    entity_description = LawnMowerEntityEntityDescription(key="mower", name=None)

    def __init__(self, device: Device) -> None:
        """Initialize the lawn mower."""
        super().__init__(device, device.capabilities)

    @override
    async def async_added_to_hass(self) -> None:
        """Subscribe to state events."""
        await super().async_added_to_hass()

        async def on_status(event: StateEvent) -> None:
            activity = _STATE_TO_MOWER_STATE.get(event.state)
            if activity is None:
                _LOGGER.warning("Unhandled state from device: %s", event.state)
                return
            self._attr_activity = activity
            self.async_write_ha_state()

        self._subscribe(self._capability.state.event, on_status)

    async def _clean_command(self, action: CleanAction) -> None:
        await self._execute_command(
            self._capability.clean.action.command(action)
        )

    @override
    async def async_start_mowing(self) -> None:
        """Start or resume mowing."""
        await self._clean_command(CleanAction.START)

    @override
    async def async_pause(self) -> None:
        """Pause mowing."""
        await self._clean_command(CleanAction.PAUSE)

    @override
    async def async_dock(self) -> None:
        """Send the mower back to the dock."""
        await self._execute_command(self._capability.charge.execute())
