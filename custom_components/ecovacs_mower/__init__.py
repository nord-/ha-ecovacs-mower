"""Ecovacs Mower — Home Assistant integration for GOAT lawn mowers."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .controller import EcovacsController

PLATFORMS = [
    Platform.BUTTON,
    Platform.EVENT,
    Platform.LAWN_MOWER,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
]

type EcovacsMowerConfigEntry = ConfigEntry[EcovacsController]


async def async_setup_entry(
    hass: HomeAssistant, entry: EcovacsMowerConfigEntry
) -> bool:
    """Set up the integration from a config entry."""
    controller = EcovacsController(hass, entry.data)
    entry.async_on_unload(controller.teardown)
    await controller.initialize()
    entry.runtime_data = controller
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: EcovacsMowerConfigEntry
) -> bool:
    """Unload the config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
