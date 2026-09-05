"""async_remove_entry must clean up per-device map stores.

The module under test imports Home Assistant, which cannot be imported on
Windows (fcntl). Imports live inside the tests and the file is marked
requires_ha. The source of truth is CI on ubuntu-latest.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol

from . import requires_ha

pytestmark = requires_ha


async def test_async_setup_registers_mow_area_entity_service() -> None:
    """Register the action before any config entry is loaded."""
    from custom_components.ecovacs_mower import async_setup

    hass = MagicMock()
    with patch(
        "custom_components.ecovacs_mower.service.async_register_platform_entity_service"
    ) as register:
        assert await async_setup(hass, {})

    register.assert_called_once()
    kwargs = register.call_args.kwargs
    assert kwargs["entity_domain"] == "lawn_mower"
    assert kwargs["func"] == "async_mow_area"

    from custom_components.ecovacs_mower.lawn_mower import EcovacsMower

    assert hasattr(EcovacsMower, kwargs["func"])


async def test_mow_area_service_reaches_registered_entity(hass) -> None:
    """A valid service call reaches the registered lawn-mower entity method."""
    from homeassistant.helpers.entity import Entity
    from homeassistant.helpers.entity_platform import DATA_DOMAIN_PLATFORM_ENTITIES

    from custom_components.ecovacs_mower import async_setup

    class TestMowerEntity(Entity):
        """Minimal real entity for exercising HA's entity-service dispatch."""

        _attr_should_poll = False

        def __init__(self) -> None:
            self.called_area_ids: list[int] | None = None

        async def async_mow_area(self, area_ids: list[int]) -> None:
            self.called_area_ids = area_ids

    entity = TestMowerEntity()
    entity.entity_id = "lawn_mower.goat"
    hass.data.setdefault(DATA_DOMAIN_PLATFORM_ENTITIES, {})[
        ("lawn_mower", "ecovacs_mower")
    ] = {entity.entity_id: entity}

    await async_setup(hass, {})
    await hass.services.async_call(
        "ecovacs_mower",
        "mow_area",
        {"entity_id": entity.entity_id, "area_ids": [1, 3]},
        blocking=True,
    )

    assert entity.called_area_ids == [1, 3]


async def test_mow_area_service_validates_area_ids(hass) -> None:
    """Invalid area IDs fail at the service boundary."""
    from custom_components.ecovacs_mower import async_setup

    await async_setup(hass, {})

    with pytest.raises(vol.MultipleInvalid):
        await hass.services.async_call(
            "ecovacs_mower",
            "mow_area",
            {"entity_id": "lawn_mower.goat"},
            blocking=True,
        )

    with pytest.raises(vol.MultipleInvalid):
        await hass.services.async_call(
            "ecovacs_mower",
            "mow_area",
            {"entity_id": "lawn_mower.goat", "area_ids": [-1]},
            blocking=True,
        )


@pytest.mark.parametrize("value", [0, 1, 999, "0", "1", [0, 1, 999], ["0", "1", "999"]])
def test_area_ids_accept_valid_values(value) -> None:
    """The service schema accepts numeric IDs and text-selector strings."""
    from custom_components.ecovacs_mower import AREA_IDS_SCHEMA

    expected = value if isinstance(value, list) else [value]
    expected = [int(item) for item in expected]
    assert AREA_IDS_SCHEMA(value) == expected


@pytest.mark.parametrize("value", [-1, 1000, 1.5, "1.5", "abc", "", None, True])
def test_area_ids_reject_invalid_values(value) -> None:
    """The service schema rejects malformed or out-of-range IDs."""
    from custom_components.ecovacs_mower import AREA_IDS_SCHEMA

    with pytest.raises(vol.Invalid):
        AREA_IDS_SCHEMA(value)


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


async def test_account_credentials_change_persists_to_the_entry() -> None:
    # The other half of the fallback fix in deebot_patch/authentication.py: a
    # replacement pair minted there only reaches the entry through the
    # callback wired up here. Without this wiring the replacement lives only
    # in memory and the next reload pays a doomed token login first.
    from custom_components.ecovacs_mower import async_setup_entry
    from custom_components.ecovacs_mower.const import CONF_CREDENTIALS

    hass = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    entry = MagicMock()
    entry.data = {"existing": "value"}

    with patch(
        "custom_components.ecovacs_mower.EcovacsController"
    ) as controller_cls:
        controller_cls.return_value.initialize = AsyncMock()
        await async_setup_entry(hass, entry)

    _, kwargs = controller_cls.call_args
    on_changed = kwargs["on_account_credentials_changed"]
    account = {"access_token": "tok", "user_id": "uid"}
    on_changed(account)

    hass.config_entries.async_update_entry.assert_called_once_with(
        entry, data={"existing": "value", CONF_CREDENTIALS: account}
    )
