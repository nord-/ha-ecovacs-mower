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


def test_translation_exists() -> None:
    import json
    from pathlib import Path

    from custom_components.ecovacs_mower.image import EcovacsMowerMap

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    strings = json.loads((root / "strings.json").read_text(encoding="utf-8"))
    key = EcovacsMowerMap.entity_description.translation_key
    assert key in strings["entity"]["image"]


def test_map_entity_has_an_icon() -> None:
    import json
    from pathlib import Path

    from custom_components.ecovacs_mower.image import EcovacsMowerMap

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    icons = json.loads((root / "icons.json").read_text(encoding="utf-8"))
    key = EcovacsMowerMap.entity_description.translation_key
    assert key in icons["entity"]["image"]


def test_no_stale_image_translations_or_icons() -> None:
    """Every key in strings.json/icons.json must belong to a real image entity.

    The converse of the tests above: they check description -> string/icon, not
    the other way around. Without this, a leftover key for a removed image entity would
    go unnoticed.
    """
    import json
    from pathlib import Path

    from custom_components.ecovacs_mower.image import EcovacsMowerMap

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    strings = json.loads((root / "strings.json").read_text(encoding="utf-8"))
    icons = json.loads((root / "icons.json").read_text(encoding="utf-8"))
    key = EcovacsMowerMap.entity_description.translation_key

    assert set(strings["entity"]["image"]) <= {key}
    assert set(icons["entity"]["image"]) <= {key}


async def test_position_bumps_are_throttled() -> None:
    from datetime import timedelta
    from unittest.mock import MagicMock, patch

    from homeassistant.util import dt as dt_util

    from custom_components.ecovacs_mower.image import EcovacsMowerMap

    instance = EcovacsMowerMap.__new__(EcovacsMowerMap)
    instance.async_write_ha_state = MagicMock()

    now = dt_util.utcnow()
    instance._attr_image_last_updated = now
    with patch(
        "custom_components.ecovacs_mower.image.async_call_later"
    ) as call_later:
        await EcovacsMowerMap._on_positions(instance, MagicMock())
    assert instance._attr_image_last_updated == now  # too soon, no bump
    instance.async_write_ha_state.assert_not_called()
    call_later.assert_called_once()  # but the last word is not lost

    instance._attr_image_last_updated = now - timedelta(seconds=3)
    await EcovacsMowerMap._on_positions(instance, MagicMock())
    assert instance._attr_image_last_updated >= now  # old enough, bumped
    instance.async_write_ha_state.assert_called_once()


async def test_a_skipped_position_is_published_when_the_interval_is_up() -> None:
    # The mower stops: no position follows the one that was skipped, so
    # without a trailing refresh the state would stay behind for good.
    from unittest.mock import MagicMock, patch

    from homeassistant.util import dt as dt_util

    from custom_components.ecovacs_mower.image import (
        POSITION_UPDATE_INTERVAL_SECONDS,
        EcovacsMowerMap,
    )

    instance = EcovacsMowerMap.__new__(EcovacsMowerMap)
    instance.hass = MagicMock()
    instance.async_write_ha_state = MagicMock()
    instance._attr_image_last_updated = dt_util.utcnow()

    with patch(
        "custom_components.ecovacs_mower.image.async_call_later"
    ) as call_later:
        await EcovacsMowerMap._on_positions(instance, MagicMock())
        await EcovacsMowerMap._on_positions(instance, MagicMock())

    # A burst of skipped positions leaves exactly one refresh behind, due
    # within the interval.
    call_later.assert_called_once()
    _hass, delay, action = call_later.call_args.args
    assert 0 < delay <= POSITION_UPDATE_INTERVAL_SECONDS
    instance.async_write_ha_state.assert_not_called()

    before = instance._attr_image_last_updated
    await action(dt_util.utcnow())
    instance.async_write_ha_state.assert_called_once()
    assert instance._attr_image_last_updated > before
    assert instance._trailing_bump is None


async def test_a_bump_that_is_due_cancels_the_pending_refresh() -> None:
    from datetime import timedelta
    from unittest.mock import MagicMock

    from homeassistant.util import dt as dt_util

    from custom_components.ecovacs_mower.image import EcovacsMowerMap

    instance = EcovacsMowerMap.__new__(EcovacsMowerMap)
    instance.async_write_ha_state = MagicMock()
    cancel = MagicMock()
    instance._trailing_bump = cancel
    instance._attr_image_last_updated = dt_util.utcnow() - timedelta(seconds=3)

    await EcovacsMowerMap._on_positions(instance, MagicMock())

    # The refresh would only repeat what this bump just wrote.
    cancel.assert_called_once()
    assert instance._trailing_bump is None
    instance.async_write_ha_state.assert_called_once()


async def test_a_geometry_event_cancels_the_pending_refresh() -> None:
    from unittest.mock import MagicMock

    from custom_components.ecovacs_mower.image import EcovacsMowerMap

    instance = EcovacsMowerMap.__new__(EcovacsMowerMap)
    instance.async_write_ha_state = MagicMock()
    cancel = MagicMock()
    instance._trailing_bump = cancel

    await EcovacsMowerMap._on_geometry(instance, MagicMock())

    # The geometry write already carries the latest position.
    cancel.assert_called_once()
    assert instance._trailing_bump is None
    instance.async_write_ha_state.assert_called_once()


async def test_removal_drops_the_pending_refresh() -> None:
    from unittest.mock import MagicMock, patch

    from custom_components.ecovacs_mower.image import EcovacsMowerMap

    instance = EcovacsMowerMap.__new__(EcovacsMowerMap)
    cancel = MagicMock()
    instance._trailing_bump = cancel

    with patch(
        "custom_components.ecovacs_mower.entity.EcovacsEntity"
        ".async_will_remove_from_hass"
    ):
        await EcovacsMowerMap.async_will_remove_from_hass(instance)

    cancel.assert_called_once()
    assert instance._trailing_bump is None


def test_attributes_before_the_first_position_fix() -> None:
    from custom_components.ecovacs_mower.image import EcovacsMowerMap
    from custom_components.ecovacs_mower.map import MowerMap

    instance = EcovacsMowerMap.__new__(EcovacsMowerMap)
    instance._map = MowerMap()

    # The SVG marker falls back to the dock, but a number nobody measured
    # must not look like one: position and heading stay None, the dock does
    # not.
    assert instance.extra_state_attributes == {
        "position_x": None,
        "position_y": None,
        "heading": None,
        "dock_x": 0,
        "dock_y": 0,
        "track": [],
    }


def test_attributes_follow_the_map() -> None:
    from custom_components.ecovacs_mower.image import EcovacsMowerMap
    from custom_components.ecovacs_mower.map import MowerMap

    instance = EcovacsMowerMap.__new__(EcovacsMowerMap)
    mower_map = MowerMap()
    instance._map = mower_map

    mower_map.update_position(1200, -3400, 90)
    assert instance.extra_state_attributes == {
        "position_x": 1200,
        "position_y": -3400,
        "heading": 90,
        "dock_x": 0,
        "dock_y": 0,
        "track": [[1200, -3400]],
    }

    # The controller moves the map under the entity's feet; the attributes
    # must reflect that without the entity being told.
    mower_map.update_position(1500, -3000, 180)
    mower_map.dock = (10, 20)
    attributes = instance.extra_state_attributes
    assert attributes["position_x"] == 1500
    assert attributes["position_y"] == -3000
    assert attributes["heading"] == 180
    assert (attributes["dock_x"], attributes["dock_y"]) == (10, 20)


def test_moving_attributes_are_not_recorded() -> None:
    from custom_components.ecovacs_mower.image import EcovacsMowerMap

    # The dock is worth keeping in history, the position is not.
    assert EcovacsMowerMap._unrecorded_attributes == {
        "position_x",
        "position_y",
        "heading",
        "track",
    }


def test_track_attribute_is_thinned_and_ends_at_the_mower() -> None:
    # The attribute goes to every client on each change; the map keeps up to
    # 2000 points, which is far more than a card needs.
    from custom_components.ecovacs_mower.image import (
        TRACK_ATTRIBUTE_POINTS,
        EcovacsMowerMap,
    )
    from custom_components.ecovacs_mower.map import MowerMap

    instance = EcovacsMowerMap.__new__(EcovacsMowerMap)
    mower_map = MowerMap()
    instance._map = mower_map
    for i in range(1500):
        mower_map.update_position(i * 10, -i, 0)

    track = instance.extra_state_attributes["track"]

    assert len(track) <= TRACK_ATTRIBUTE_POINTS + 1
    assert track[0] == [0, 0]
    assert track[-1] == [14990, -1499]  # where the mower is now
    # JSON-friendly: lists, not tuples.
    assert all(isinstance(point, list) for point in track)
