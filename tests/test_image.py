"""The map image entity.

The module under test imports Home Assistant, which cannot be imported on
Windows (fcntl). Imports live inside the tests and the file is marked
requires_ha. The source of truth is CI on ubuntu-latest.
"""

import pytest

from . import requires_ha

pytestmark = requires_ha


def test_image_platform_is_registered() -> None:
    from homeassistant.const import Platform

    from custom_components.ecovacs_mower import PLATFORMS

    assert Platform.IMAGE in PLATFORMS


def test_content_type_is_svg() -> None:
    from custom_components.ecovacs_mower.image import EcovacsMowerMap

    instance = EcovacsMowerMap.__new__(EcovacsMowerMap)
    assert instance._attr_content_type == "image/svg+xml"


def test_entity_description_key_is_map() -> None:
    from custom_components.ecovacs_mower.image import EcovacsMowerMap

    assert EcovacsMowerMap.entity_description.key == "map"
    assert EcovacsMowerMap.entity_description.translation_key == "map"


def test_constructor_reaches_image_entity_init() -> None:
    # Regression guard for the MRO chain: EcovacsEntity.__init__ forwards
    # **kwargs to super(), which in this MRO is ImageEntity.__init__ —
    # requiring hass. Every other test constructs via __new__ and would
    # never catch a broken constructor.
    from unittest.mock import MagicMock

    from custom_components.ecovacs_mower.image import EcovacsMowerMap
    from custom_components.ecovacs_mower.map import MowerMap

    device = MagicMock()
    device.device_info = {"did": "did-1"}
    entity = EcovacsMowerMap(device, MowerMap(), MagicMock())
    assert entity._attr_unique_id == "did-1_map"


async def test_async_image_renders_the_map() -> None:
    from custom_components.ecovacs_mower.image import EcovacsMowerMap
    from custom_components.ecovacs_mower.map import MowerMap

    instance = EcovacsMowerMap.__new__(EcovacsMowerMap)
    mower_map = MowerMap()
    instance._map = mower_map

    image = await EcovacsMowerMap.async_image(instance)
    assert image is not None
    assert image.startswith(b"<svg")
    assert b"No map data yet" in image

    mower_map.update_map_info([(0, 0), (100, 0), (100, 100)], None, None)
    image = await EcovacsMowerMap.async_image(instance)
    assert b"No map data yet" not in image
    assert b'class="boundary"' in image


async def test_position_bumps_are_throttled() -> None:
    from datetime import timedelta
    from unittest.mock import MagicMock

    from homeassistant.util import dt as dt_util

    from custom_components.ecovacs_mower.image import EcovacsMowerMap

    instance = EcovacsMowerMap.__new__(EcovacsMowerMap)
    instance.async_write_ha_state = MagicMock()

    now = dt_util.utcnow()
    instance._attr_image_last_updated = now
    await EcovacsMowerMap._on_positions(instance, MagicMock())
    assert instance._attr_image_last_updated == now  # too soon, no bump

    instance._attr_image_last_updated = now - timedelta(seconds=3)
    await EcovacsMowerMap._on_positions(instance, MagicMock())
    assert instance._attr_image_last_updated >= now  # old enough, bumped
    instance.async_write_ha_state.assert_called_once()
