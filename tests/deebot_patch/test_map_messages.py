"""Tests for the map message handlers.

Imports deebot-client, so this file only truly runs where the library is
installed (CI; local Python 3.12 cannot install the cp314-only wheels).
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from deebot_client.event_bus import EventBus

from custom_components.ecovacs_mower.deebot_patch.hardware import (
    profile_for_class,
)
from custom_components.ecovacs_mower.deebot_patch.map_messages import (
    MowerCoverageEvent,
    MowerRemainingEvent,
    MowerCoveredAreaEvent,
    MowerMapInfoEvent,
    MowerNoGoZonesEvent,
    MowerObstaclesEvent,
    OnArI,
    OnMapInfo,
    OnMapTrace,
    OnMapTrack,
    OnMI,
    OnSpecialContour,
)
from custom_components.ecovacs_mower.deebot_patch.messages import (
    OnMowScheduleStop,
)
from custom_components.ecovacs_mower.deebot_patch.state_precedence import (
    register,
)

FIXTURES: dict[str, list[Any]] = json.loads(
    (Path(__file__).parent / "fixtures" / "map_capture.json").read_text()
)


_MAP_EVENTS = (
    MowerCoverageEvent,
    MowerRemainingEvent,
    MowerCoveredAreaEvent,
    MowerMapInfoEvent,
    MowerNoGoZonesEvent,
    MowerObstaclesEvent,
)


def _nogo_events(event_bus: Mock) -> list[Any]:
    """Every MowerNoGoZonesEvent *event_bus* has been handed, in order."""
    return [
        call.args[0]
        for call in event_bus.notify.call_args_list
        if isinstance(call.args[0], MowerNoGoZonesEvent)
    ]


def _feed(message, event_bus, key: str) -> None:
    """Hand every captured fragment of a fixture to *message*, in order.

    Deep-copied, because handling reads the payload apart — a fixture fed
    to a second bus, or twice to one, has to arrive intact.
    """
    for item in sorted(
        FIXTURES[key], key=lambda i: int(i["payload"]["body"]["data"]["index"])
    ):
        message.handle(event_bus, deepcopy(item["payload"]))


def _notified(message, key: str, class_: str | None = None) -> list[Any]:
    """Feed every captured fragment of a fixture through the handler.

    Only map events are returned: the library notifies a FirmwareEvent
    from the real headers' fwVer before body handling runs.

    *class_* registers the bus as that device class's, which is how a
    handler reads the profile flags; left out, the bus is an unregistered
    one and nothing is validated.
    """
    event_bus = Mock()
    if class_ is not None:
        register(event_bus, profile_for_class(class_))
    _feed(message, event_bus, key)
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
    assert event.boundaries[0][0] == (-31000, 1800)
    assert event.zones is None and event.flush_edges is None


def test_on_mi_notifies_every_outline_and_the_corridors() -> None:
    events = _notified(OnMI, "on_mi_multi_area")
    assert len(events) == 1
    assert [len(outline) for outline in events[0].boundaries] == [
        2420,
        1222,
        499,
    ]
    assert [len(corridor) for corridor in events[0].corridors] == [11, 79]


def test_on_mi_idle_notifies_nothing() -> None:
    assert _notified(OnMI, "on_mi_idle") == []


def test_on_ari_multipart_notifies_map_info_obstacles_and_nogo() -> None:
    events = _notified(OnArI, "on_ari_multipart", class_="9bts2s")
    assert len(events) == 3
    map_info = next(e for e in events if isinstance(e, MowerMapInfoEvent))
    obstacles = next(e for e in events if isinstance(e, MowerObstaclesEvent))
    nogo = next(e for e in events if isinstance(e, MowerNoGoZonesEvent))
    assert map_info.boundaries is not None and len(map_info.zones) == 3
    assert len(obstacles.obstacles) == 15
    assert len(nogo.zones) == 2  # section 2, no longer merged into zones


def test_on_ari_keeps_section_2_in_the_zones_without_the_flag() -> None:
    # This fixture is the O1200 LiDAR Pro's, whose section 2 nobody has
    # checked against the app: it keeps master's reading, five zones and
    # no no-go event, so onSpecialContour stays the source of that layer.
    events = _notified(OnArI, "on_ari_multipart", class_="2i0fns")
    assert [type(e).__name__ for e in events] == [
        "MowerMapInfoEvent",
        "MowerObstaclesEvent",
    ]
    assert len(events[0].zones) == 5


def test_on_ari_obstacle_update_leaves_geometry_alone() -> None:
    events = _notified(OnArI, "on_ari_obstacles_only")
    assert len(events) == 1
    assert isinstance(events[0], MowerObstaclesEvent)
    assert len(events[0].obstacles) == 14


def test_on_map_track_notifies_the_rows_still_to_mow() -> None:
    events = _notified(OnMapTrack, "on_map_track_single_lane", class_="9bts2s")
    assert len(events) == 1
    assert isinstance(events[0], MowerRemainingEvent)
    assert events[0].rows == {("3", 67): [((-26825, 2400), (-26825, 4199))]}


def test_on_map_track_notifies_coverage_without_the_flag() -> None:
    # The same bytes, read as this integration has always read them. The
    # measurement behind the other reading is one job on one mower, so
    # every class it was not taken on keeps what it had.
    events = _notified(OnMapTrack, "on_map_track_single_lane", class_="2i0fns")
    assert len(events) == 1
    assert isinstance(events[0], MowerCoverageEvent)
    assert events[0].lanes == {("3", 67): [((-26825, 2400), (-26825, 4199))]}


def test_on_map_track_on_an_unregistered_bus_notifies_coverage() -> None:
    # No record, no profile, nothing validated — the safe reading.
    events = _notified(OnMapTrack, "on_map_track_single_lane")
    assert [type(event) for event in events] == [MowerCoverageEvent]


def test_on_map_track_keeps_an_empty_full_plan_to_itself_without_the_flag() -> (
    None
):
    # Under the remaining-plan reading an empty full blob says the job is
    # finished, which is the most informative blob there is. Under the
    # coverage reading it says nothing at all, and publishing it as a
    # complete set would wipe the layer.
    event_bus = Mock()
    register(event_bus, profile_for_class("2i0fns"))
    OnMapTrack._notify(event_bus, json.dumps([["1", "1"]]).encode())

    assert event_bus.notify.call_args_list == []


async def test_an_identical_plan_after_a_stop_is_not_dropped_as_a_duplicate() -> (
    None
):
    """Stop a job before a row is mowed, start it again: the same plan.

    ``EventBus`` keeps the last event of each type and drops the next one
    equal to it. So a reset written into the map without going through
    the bus would leave the dropped plan cached, and the identical plan
    the restarted job reports would be discarded as a duplicate — an
    empty map that further polls could not refill, until a mowed row made
    the plan differ. Driven on a real bus, because a Mock one never
    dedupes and would pass either way.
    """
    bus = EventBus(AsyncMock(), Mock(get_refresh_commands=lambda _event: []))
    register(bus, profile_for_class("9bts2s"))
    received: list[MowerRemainingEvent] = []

    async def on_remaining(event: MowerRemainingEvent) -> None:
        received.append(event)

    bus.subscribe(MowerRemainingEvent, on_remaining)

    _feed(OnMapTrack, bus, "on_map_track_multipart")
    # Only the trigger is read, and every stop drops the plan.
    OnMowScheduleStop._handle_body(bus, {"trigger": "app"})
    _feed(OnMapTrack, bus, "on_map_track_multipart")
    await asyncio.sleep(0)

    # The captured plan's 126 records fall on 123 distinct (zone, row)
    # keys, and both replays of it are the same whole plan.
    assert [(len(event.rows), event.complete) for event in received] == [
        (123, True),
        (0, True),
        (123, True),
    ]


def test_on_special_contour_notifies_nogo_zones() -> None:
    events = _notified(OnSpecialContour, "on_special_contour")
    assert len(events) == 1
    assert isinstance(events[0], MowerNoGoZonesEvent)
    assert len(events[0].zones) == 2


def test_on_ari_nogo_zones_outrank_on_special_contour() -> None:
    # Both messages carry no-go zones on a 1.13 mower, and the event holds
    # the complete set, so without a rule they would overwrite each other
    # turn by turn. onArI reports per area and traced; onSpecialContour on
    # one of those mowers carries a single point (issue #41).
    event_bus = Mock()
    register(event_bus, profile_for_class("9bts2s"))
    for item in sorted(
        FIXTURES["on_ari_multipart"],
        key=lambda i: int(i["payload"]["body"]["data"]["index"]),
    ):
        OnArI.handle(event_bus, deepcopy(item["payload"]))
    from_on_ari = _nogo_events(event_bus)
    OnSpecialContour.handle(
        event_bus, deepcopy(FIXTURES["on_special_contour"][0]["payload"])
    )

    # The traced section-2 zones stand; the contour message added nothing.
    assert [len(event.zones) for event in from_on_ari] == [2]
    assert _nogo_events(event_bus) == from_on_ari


def test_on_special_contour_stays_the_source_without_the_flag() -> None:
    # The decision is the class's, not the order the messages arrive in.
    # A latch on "onArI has reported zones once" would have handed the
    # layer to section 2 on the first onArI and never handed it back —
    # on the O1200, permanently losing a zone section 2 never mentions.
    event_bus = Mock()
    register(event_bus, profile_for_class("2i0fns"))
    for item in sorted(
        FIXTURES["on_ari_multipart"],
        key=lambda i: int(i["payload"]["body"]["data"]["index"]),
    ):
        OnArI.handle(event_bus, deepcopy(item["payload"]))
    assert _nogo_events(event_bus) == []

    OnSpecialContour.handle(
        event_bus, deepcopy(FIXTURES["on_special_contour"][0]["payload"])
    )
    zones = _nogo_events(event_bus)
    assert len(zones) == 1 and len(zones[0].zones) == 2


def test_on_special_contour_is_published_on_an_unregistered_bus() -> None:
    # An ordinary Deebot vacuum's bus, and a patched mower registered
    # before its profile was known: nothing is validated, so this message
    # keeps the layer.
    event_bus = Mock()
    OnSpecialContour.handle(
        event_bus, deepcopy(FIXTURES["on_special_contour"][0]["payload"])
    )

    zones = _nogo_events(event_bus)
    assert len(zones) == 1 and len(zones[0].zones) == 2


def test_on_special_contour_without_a_polygon_publishes_nothing() -> None:
    # fw 1.13.8 pushes a record whose geometry is a single point while the
    # real no-go zone rides in onArI section 2. Published, it would have
    # replaced that zone with the one point.
    assert _notified(OnSpecialContour, "on_special_contour_no_polygon") == []


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
    assert MESSAGES["onMapTrace"] is OnMapTrace
    assert MESSAGES["onSpecialContour"] is OnSpecialContour
    # The full name, not the suffix-stripped one: get_message() matches
    # onMapInfo_V2 exactly before it strips _V2, and the library ships its
    # own class under that key. Registering "onMapInfo" would be dead.
    assert MESSAGES["onMapInfo_V2"] is OnMapInfo


def test_on_mi_v117_notifies_boundary() -> None:
    events = _notified(OnMI, "on_mi_full_v117")
    assert len(events) == 1
    assert isinstance(events[0], MowerMapInfoEvent)
    assert events[0].boundaries[0][0] == (-10800, 7900)


def test_on_mi_v117_idle_notifies_nothing() -> None:
    assert _notified(OnMI, "on_mi_idle_v117") == []


def test_on_ari_v117_notifies_zones_obstacles_and_nogo() -> None:
    events = _notified(OnArI, "on_ari_v117_nogo_zones")
    map_info = next(e for e in events if isinstance(e, MowerMapInfoEvent))
    obstacles = next(e for e in events if isinstance(e, MowerObstaclesEvent))
    nogo = next(e for e in events if isinstance(e, MowerNoGoZonesEvent))
    assert len(map_info.zones) == 4
    assert map_info.boundaries is None  # 1.17 sends it via onMI
    assert len(obstacles.obstacles) == 29
    # No onSpecialContour on 1.17: the no-go zones ride in onArI instead.
    assert len(nogo.zones) == 4


def test_on_ari_v117_bare_ids_notify_no_map_info() -> None:
    events = _notified(OnArI, "on_ari_v117_ids_only")
    assert not [e for e in events if isinstance(e, MowerMapInfoEvent)]
    assert len(next(e for e in events if isinstance(e, MowerObstaclesEvent)).obstacles) == 3


def test_on_map_trace_notifies_covered_area() -> None:
    events = _notified(OnMapTrace, "on_map_trace_v117_multipart")
    assert len(events) == 1
    assert isinstance(events[0], MowerCoveredAreaEvent)
    assert len(events[0].areas) == 1
    assert len(events[0].holes) == 6


def test_on_map_info_is_a_pure_alias_of_on_mi() -> None:
    # The rename is the only difference the handler knows about: fed the
    # same blob, the two names must publish the identical event. That is
    # the point of subclassing rather than writing a second handler, and
    # it holds whichever dialect the blob turns out to be in — the payload
    # is decided per blob in geometry.py (issue #81).
    assert _notified(OnMapInfo, "on_mi_full_v117") == _notified(
        OnMI, "on_mi_full_v117"
    )
    assert _notified(OnMapInfo, "on_map_info_v2_g1800") == _notified(
        OnMI, "on_map_info_v2_g1800"
    )


def test_on_map_info_publishes_the_captured_boundary() -> None:
    # What issue #81 is actually for, against the blob the device sent
    # rather than an onMI stand-in: seven fragments, 32 KB, one boundary
    # out the other end. Until this fixture existed the wiring was
    # unverified on its central point — and it did not in fact work, since
    # firmware 1.36 leads the record with an id instead of "s1".
    events = _notified(OnMapInfo, "on_map_info_v2_g1800")
    assert len(events) == 1
    assert isinstance(events[0], MowerMapInfoEvent)
    assert events[0].boundaries[0][0] == (-11918, 1959)
    assert len(events[0].boundaries[0]) == 692
    assert events[0].zones is None and events[0].flush_edges is None


def test_on_map_info_publishes_nothing_before_the_last_fragment() -> None:
    # Seven fragments is the most any capture has needed, and the buffer
    # is what keeps a partial 32 KB blob off the bus. Six of them must be
    # silent — a premature notify would publish a truncated lawn.
    fragments = sorted(
        FIXTURES["on_map_info_v2_g1800"],
        key=lambda item: int(item["payload"]["body"]["data"]["index"]),
    )
    event_bus = Mock()
    for fragment in fragments[:-1]:
        OnMapInfo.handle(event_bus, fragment["payload"])
    assert not [
        call.args[0]
        for call in event_bus.notify.call_args_list
        if isinstance(call.args[0], _MAP_EVENTS)
    ]
    OnMapInfo.handle(event_bus, fragments[-1]["payload"])
    assert [
        call.args[0]
        for call in event_bus.notify.call_args_list
        if isinstance(call.args[0], MowerMapInfoEvent)
    ]


def test_on_map_info_reassembles_independently_of_on_mi() -> None:
    # __init_subclass__ hands every subclass its own FragmentBuffer, so the
    # two names never share a batch. Half a blob into OnMI must leave
    # OnMapInfo with nothing, and vice versa.
    fragments = sorted(
        FIXTURES["on_mi_full_v117"],
        key=lambda item: int(item["payload"]["body"]["data"]["index"]),
    )
    assert len(fragments) > 1, "needs a multipart fixture to mean anything"

    assert OnMapInfo._buffer is not OnMI._buffer

    event_bus = Mock()
    OnMI.handle(event_bus, fragments[0]["payload"])
    for fragment in fragments[1:]:
        OnMapInfo.handle(event_bus, fragment["payload"])
    assert not [
        call.args[0]
        for call in event_bus.notify.call_args_list
        if isinstance(call.args[0], _MAP_EVENTS)
    ]


def test_on_map_info_does_not_inherit_the_outline_version_filter() -> None:
    # The library's own onMapInfo_V2 handler drops any blob whose outlineVer
    # is not "1". Firmware 1.36 sends outlineVer "0" on the map it reports as
    # in use, which is why the boundary was claimed and then discarded. Our
    # handler must publish it regardless of the field.
    fragments = sorted(
        FIXTURES["on_map_info_v2_g1800"],
        key=lambda item: int(item["payload"]["body"]["data"]["index"]),
    )
    # The captured value, not a synthetic one: the device really does send
    # outlineVer "0" on the map it flags as using: 1.
    assert fragments[0]["payload"]["body"]["data"]["outlineVer"] == "0"
    assert fragments[0]["payload"]["body"]["data"]["using"] == 1

    event_bus = Mock()
    for fragment in fragments:
        OnMapInfo.handle(event_bus, deepcopy(fragment["payload"]))

    events = [
        call.args[0]
        for call in event_bus.notify.call_args_list
        if isinstance(call.args[0], MowerMapInfoEvent)
    ]
    assert len(events) == 1
    assert events[0].boundaries[0][0] == (-11918, 1959)


def test_on_map_trace_notifies_the_live_runs_of_a_job() -> None:
    # The same decode as test_geometry, but through the handler and on the
    # firmware that reported issue #52: two runs out of section 3, and not
    # the empty polygon an id-only section 1 used to contribute.
    events = _notified(OnMapTrace, "on_map_trace_g1800_job_two_runs")
    assert len(events) == 1
    assert isinstance(events[0], MowerCoveredAreaEvent)
    assert [len(area) for area in events[0].areas] == [7, 52]
    assert events[0].holes == []


def test_on_map_trace_notifies_an_emptied_blob() -> None:
    # The filter drops empty polygons, never the event. A blob whose
    # sections hold nothing but ids is how the mower says the coverage is
    # gone — the firmware sent one a second into the border job captured on
    # issue #52 — and the map layer only clears itself if that still
    # arrives as an event.
    events = _notified(OnMapTrace, "on_map_trace_g1800_job_cleared")
    assert len(events) == 1
    assert isinstance(events[0], MowerCoveredAreaEvent)
    assert events[0].areas == []
    assert events[0].holes == []


def test_the_first_fragment_of_a_map_message_teaches_the_map_id() -> None:
    # Written by the handler, not learned from an event: the bus drops repeated
    # events before subscribers run, and the id has to be known before the
    # blob is even complete — a border job cannot wait for a full outline.
    from custom_components.ecovacs_mower.deebot_patch.state_precedence import (
        map_id_for,
        register,
    )

    event_bus = Mock()
    register(event_bus)
    fragments = sorted(
        FIXTURES["on_map_info_v2_g1800"],
        key=lambda i: int(i["payload"]["body"]["data"]["index"]),
    )
    OnMapInfo.handle(event_bus, deepcopy(fragments[0]["payload"]))

    assert map_id_for(event_bus) == "123456789"


def test_learning_the_map_id_requests_the_area_refresh_once() -> None:
    # GetAreaSet sends nothing until the id is known, so learning it is what
    # gets the area names asked for; a repeat of the same id asks again for
    # nothing.
    from custom_components.ecovacs_mower.deebot_patch.areas import MowerAreaEvent
    from custom_components.ecovacs_mower.deebot_patch.state_precedence import (
        register,
    )

    event_bus = Mock()
    register(event_bus)
    for _ in range(2):
        OnMI.handle(event_bus, deepcopy(FIXTURES["on_mi_full"][0]["payload"]))

    event_bus.request_refresh.assert_called_once_with(MowerAreaEvent)


def test_a_map_id_of_zero_teaches_nothing() -> None:
    from custom_components.ecovacs_mower.deebot_patch.state_precedence import (
        map_id_for,
        register,
    )

    event_bus = Mock()
    register(event_bus)
    for item in FIXTURES["on_map_track_multipart"]:
        OnMapTrack.handle(event_bus, deepcopy(item["payload"]))

    assert map_id_for(event_bus) is None


def test_a_fragment_for_a_map_not_in_use_teaches_nothing() -> None:
    # The envelope's own "using" flag, passed through to the record: a stored
    # but inactive map in a getMapInfo_V2 answer must not become the border
    # job's target. Constructed, not captured — no fixture has using 0.
    from custom_components.ecovacs_mower.deebot_patch.state_precedence import (
        map_id_for,
        register,
    )

    event_bus = Mock()
    register(event_bus)
    fragment = deepcopy(FIXTURES["on_map_info_v2_g1800"][0]["payload"])
    fragment["body"]["data"]["mid"] = "987654321"
    fragment["body"]["data"]["using"] = 0
    OnMapInfo.handle(event_bus, fragment)

    assert map_id_for(event_bus) is None


def test_an_undecodable_blob_still_teaches_the_map_id() -> None:
    from custom_components.ecovacs_mower.deebot_patch.state_precedence import (
        map_id_for,
        register,
    )

    event_bus = Mock()
    register(event_bus)
    payload = deepcopy(FIXTURES["on_mi_full"][0]["payload"])
    payload["body"]["data"]["info"] = "not base64 at all!!"
    OnMI.handle(event_bus, payload)  # must not raise

    assert map_id_for(event_bus) == "1"


def test_an_unregistered_bus_records_no_map_id() -> None:
    # A Deebot vacuum on the same account reaches these handlers too; it must
    # not get a record made for it as a side effect.
    from custom_components.ecovacs_mower.deebot_patch.state_precedence import (
        map_id_for,
        record_for,
    )

    event_bus = Mock()
    OnMI.handle(event_bus, deepcopy(FIXTURES["on_mi_full"][0]["payload"]))

    assert record_for(event_bus) is None
    assert map_id_for(event_bus) is None
