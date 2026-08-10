"""The GPS position tracker.

The module under test imports Home Assistant, which cannot be imported on Windows
(``fcntl``). The imports therefore live inside the test functions and the whole
file is marked ``requires_ha`` — otherwise collection itself crashes before any
skip marker gets a chance to apply. The source of truth is CI on ubuntu-latest.
"""

import json
from pathlib import Path

from tests import requires_ha

pytestmark = requires_ha

ROOT = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"


def test_platform_is_registered() -> None:
    """A platform module nothing forwards to is never set up."""
    from homeassistant.const import Platform

    from custom_components.ecovacs_mower import PLATFORMS

    assert Platform.DEVICE_TRACKER in PLATFORMS


def test_source_type_is_gps() -> None:
    """Anything but GPS makes HA ignore the latitude/longitude attributes."""
    from homeassistant.components.device_tracker import SourceType

    from custom_components.ecovacs_mower.device_tracker import EcovacsMowerTracker

    assert EcovacsMowerTracker._attr_source_type == SourceType.GPS


def test_subscribes_to_gps_position_event() -> None:
    """The event carries WGS84 degrees; PositionsEvent (map-relative mm) does not.

    ``GpsPositionEvent`` has no entry in ``Capabilities``, so nothing else in this
    integration would catch a swap to the wrong event type: the entity would just
    stay silent.
    """
    import inspect

    from custom_components.ecovacs_mower import device_tracker

    source = inspect.getsource(device_tracker.EcovacsMowerTracker)
    assert "GpsPositionEvent" in source
    assert "PositionsEvent" not in source


def test_position_updates_write_state() -> None:
    """Feed the subscribed callback an event and check both coordinates land."""
    import asyncio
    from unittest.mock import MagicMock

    from deebot_client.events import GpsPositionEvent

    from custom_components.ecovacs_mower.device_tracker import EcovacsMowerTracker

    # __new__ bypasses __init__, which requires a real Device. Only the callback
    # registered by async_added_to_hass is under test here.
    tracker = EcovacsMowerTracker.__new__(EcovacsMowerTracker)
    tracker._subscribed_events = set()
    tracker._always_available = True
    tracker._device = MagicMock()
    tracker.async_on_remove = MagicMock()
    tracker.async_write_ha_state = MagicMock()

    asyncio.run(tracker.async_added_to_hass())

    subscribe = tracker._device.events.subscribe
    event_type, callback = subscribe.call_args.args
    assert event_type is GpsPositionEvent

    asyncio.run(callback(GpsPositionEvent(longitude=13.19, latitude=55.71)))

    assert tracker.latitude == 55.71
    assert tracker.longitude == 13.19
    tracker.async_write_ha_state.assert_called_once()


def test_no_position_means_no_state() -> None:
    """Until the first onGpsPos the entity must read "unknown", not "away".

    ``TrackerEntity.state`` falls back to STATE_NOT_HOME as soon as a coordinate
    pair exists, so an accidental 0/0 default would place the mower in the Gulf of
    Guinea and report it as away from home.
    """
    from custom_components.ecovacs_mower.device_tracker import EcovacsMowerTracker

    tracker = EcovacsMowerTracker.__new__(EcovacsMowerTracker)

    assert tracker.latitude is None
    assert tracker.longitude is None


def test_translation_and_icon_exist() -> None:
    from custom_components.ecovacs_mower.device_tracker import EcovacsMowerTracker

    key = EcovacsMowerTracker.entity_description.translation_key
    strings = json.loads((ROOT / "strings.json").read_text(encoding="utf-8"))
    icons = json.loads((ROOT / "icons.json").read_text(encoding="utf-8"))

    assert key in strings["entity"]["device_tracker"]
    assert key in icons["entity"]["device_tracker"]


def test_no_stale_device_tracker_translations_or_icons() -> None:
    """The converse: every key must belong to a real entity."""
    from custom_components.ecovacs_mower.device_tracker import EcovacsMowerTracker

    keys = {EcovacsMowerTracker.entity_description.translation_key}
    strings = json.loads((ROOT / "strings.json").read_text(encoding="utf-8"))
    icons = json.loads((ROOT / "icons.json").read_text(encoding="utf-8"))

    assert set(strings["entity"]["device_tracker"]) <= keys
    assert set(icons["entity"]["device_tracker"]) <= keys
