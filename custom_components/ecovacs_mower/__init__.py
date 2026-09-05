"""Ecovacs Mower — Home Assistant integration for GOAT lawn mowers."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, device_registry as dr, service
from homeassistant.helpers.typing import ConfigType

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


def _valid_area_id(value: object) -> int:
    """Validate an area ID, accepting the string form produced by HA's text selector."""
    if isinstance(value, bool):
        raise vol.Invalid("area ID must be an integer between 0 and 999")
    if isinstance(value, str):
        if not value.isdecimal():
            raise vol.Invalid("area ID must be an integer between 0 and 999")
        value = int(value)
    if not isinstance(value, int) or not 0 <= value <= 999:
        raise vol.Invalid("area ID must be an integer between 0 and 999")
    return value


AREA_IDS_SCHEMA = vol.All(
    cv.ensure_list,
    vol.Length(min=1),
    [_valid_area_id],
)


CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the integration domain."""
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        "mow_area",
        entity_domain="lawn_mower",
        schema={vol.Required("area_ids"): AREA_IDS_SCHEMA},
        func="async_mow_area",
    )
    return True


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
