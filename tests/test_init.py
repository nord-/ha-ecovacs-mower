"""async_remove_entry must clean up per-device map stores.

The module under test imports Home Assistant, which cannot be imported on
Windows (fcntl). Imports live inside the tests and the file is marked
requires_ha. The source of truth is CI on ubuntu-latest.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from . import requires_ha

pytestmark = requires_ha


async def test_async_remove_entry_removes_stores_for_every_mower_device() -> None:
    from custom_components.ecovacs_mower import async_remove_entry
    from custom_components.ecovacs_mower.const import DOMAIN

    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "entry-1"

    mower_device = MagicMock()
    mower_device.identifiers = {(DOMAIN, "did-1")}
    # A device from another integration should never be touched, even if it
    # somehow showed up for this entry.
    other_domain_device = MagicMock()
    other_domain_device.identifiers = {("other_domain", "not-a-mower")}

    with (
        patch("custom_components.ecovacs_mower.dr.async_get"),
        patch(
            "custom_components.ecovacs_mower.dr.async_entries_for_config_entry",
            return_value=[mower_device, other_domain_device],
        ),
        patch(
            "custom_components.ecovacs_mower.async_remove_map_store",
            new_callable=AsyncMock,
        ) as remove_store,
    ):
        await async_remove_entry(hass, entry)

    remove_store.assert_awaited_once_with(hass, "did-1")
