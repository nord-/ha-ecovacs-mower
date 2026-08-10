"""Tests for the map message handlers.

Imports deebot-client, so this file only truly runs where the library is
installed (CI; local Python 3.12 cannot install the cp314-only wheels).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from custom_components.ecovacs_mower.deebot_patch.map_messages import (
    MowerCoverageEvent,
    MowerMapInfoEvent,
    MowerNoGoZonesEvent,
    MowerObstaclesEvent,
    OnArI,
    OnMapTrack,
    OnMI,
    OnSpecialContour,
)

FIXTURES: dict[str, list[Any]] = json.loads(
    (Path(__file__).parent / "fixtures" / "map_capture.json").read_text()
)


_MAP_EVENTS = (
    MowerCoverageEvent,
    MowerMapInfoEvent,
    MowerNoGoZonesEvent,
    MowerObstaclesEvent,
)


def _notified(message, key: str) -> list[Any]:
    """Feed every captured fragment of a fixture through the handler.

    Only map events are returned: the library notifies a FirmwareEvent
    from the real headers' fwVer before body handling runs.
    """
    event_bus = Mock()
    for item in sorted(
        FIXTURES[key], key=lambda i: int(i["payload"]["body"]["data"]["index"])
    ):
        message.handle(event_bus, item["payload"])
    return [
        call.args[0]
        for call in event_bus.notify.call_args_list
        if isinstance(call.args[0], _MAP_EVENTS)
    ]


def test_on_mi_notifies_boundary() -> None:
    events = _notified(OnMI, "on_mi_full")
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, MowerMapInfoEvent)
    assert event.boundary[0] == (-31000, 1800)
    assert event.zones is None and event.corridors is None


def test_on_mi_idle_notifies_nothing() -> None:
    assert _notified(OnMI, "on_mi_idle") == []


def test_on_ari_multipart_notifies_map_info_and_obstacles() -> None:
    events = _notified(OnArI, "on_ari_multipart")
    assert len(events) == 2
    map_info = next(e for e in events if isinstance(e, MowerMapInfoEvent))
    obstacles = next(e for e in events if isinstance(e, MowerObstaclesEvent))
    assert map_info.boundary is not None and len(map_info.zones) == 5
    assert len(obstacles.obstacles) == 15


def test_on_ari_obstacle_update_leaves_geometry_alone() -> None:
    events = _notified(OnArI, "on_ari_obstacles_only")
    assert len(events) == 1
    assert isinstance(events[0], MowerObstaclesEvent)
    assert len(events[0].obstacles) == 14


def test_on_map_track_notifies_coverage() -> None:
    events = _notified(OnMapTrack, "on_map_track_single_lane")
    assert len(events) == 1
    assert isinstance(events[0], MowerCoverageEvent)
    assert events[0].lanes == {("3", 67): [((-26825, 2400), (-26825, 4199))]}


def test_on_special_contour_notifies_nogo_zones() -> None:
    events = _notified(OnSpecialContour, "on_special_contour")
    assert len(events) == 1
    assert isinstance(events[0], MowerNoGoZonesEvent)
    assert len(events[0].zones) == 2


def test_corrupt_info_is_swallowed() -> None:
    # Map data is best effort: garbage must not raise out of the handler.
    event_bus = Mock()
    payload = {
        "header": {"ts": "0", "tzm": 120, "fwVer": "1.11.31"},
        "body": {
            "data": {
                "mid": "1",
                "batid": "broken",
                "index": "0",
                "infoSize": 10,
                # Truncated base64 of a real payload: decompression never
                # succeeds, so the buffer waits forever (until evicted).
                "info": FIXTURES["on_mi_full"][0]["payload"]["body"]["data"][
                    "info"
                ][:40],
            }
        },
    }
    OnMI.handle(event_bus, payload)  # must not raise
    assert not [
        call
        for call in event_bus.notify.call_args_list
        if isinstance(call.args[0], _MAP_EVENTS)
    ]


def test_scalar_blob_type_error_is_swallowed() -> None:
    # A blob that decodes to a JSON scalar (not a list) raises TypeError
    # while iterating in the parsers — must be swallowed like any other
    # malformed payload, never escape to deebot-client's dispatch.
    event_bus = Mock()
    payload = {
        "header": {"ts": "0", "tzm": 120, "fwVer": "1.11.31"},
        "body": {
            "data": {
                "mid": "1",
                "batid": "scalar",
                "index": "0",
                "infoSize": 2,
                "info": "ignored",
            }
        },
    }
    with patch.object(OnMI._buffer, "add", return_value=b"42"):
        OnMI.handle(event_bus, payload)  # must not raise
    assert not [
        call
        for call in event_bus.notify.call_args_list
        if isinstance(call.args[0], _MAP_EVENTS)
    ]


def test_apply_registers_the_map_messages() -> None:
    from deebot_client.messages.json import MESSAGES

    from custom_components.ecovacs_mower.deebot_patch import apply

    apply()
    assert MESSAGES["onMI"] is OnMI
    assert MESSAGES["onArI"] is OnArI
    assert MESSAGES["onMapTrack"] is OnMapTrack
    assert MESSAGES["onSpecialContour"] is OnSpecialContour
