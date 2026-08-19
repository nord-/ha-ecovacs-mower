"""Ecovacs Mower — Home Assistant integration for GOAT lawn mowers."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import CONF_CREDENTIALS, DOMAIN
from .controller import EcovacsController, async_remove_map_store

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.EVENT,
    Platform.IMAGE,
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

    def _persist_account_credentials(account: dict[str, str]) -> None:
        """Write a replacement account pair back into the entry.

        Wired to the controller's authenticator, whose password fallback can
        mint a fresh pair when the stored one has gone stale; without this the
        replacement is only ever held in memory, and the next reload pays a
        doomed token login before falling back to the password again.
        """
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_CREDENTIALS: account}
        )

    controller = EcovacsController(
        hass, entry.data, on_account_credentials_changed=_persist_account_credentials
    )
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


async def async_remove_entry(
    hass: HomeAssistant, entry: EcovacsMowerConfigEntry
) -> None:
    """Delete the entry's persisted map stores.

    Runs after unload but before the device registry is cleared for this
    entry, so the devices (and the ``did`` in each one's identifiers) are
    still there to enumerate.
    """
    device_registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(
        device_registry, entry.entry_id
    ):
        for domain, did in device.identifiers:
            if domain == DOMAIN:
                await async_remove_map_store(hass, did)
