"""Tests for the per-device state record (issues #67 and #12)."""

from __future__ import annotations

import gc
from unittest.mock import AsyncMock, Mock

from deebot_client.event_bus import EventBus
from deebot_client.models import State

from custom_components.ecovacs_mower.deebot_patch.state_precedence import (
    MowerStateRecord,
    map_id_for,
    record_for,
    register,
)


def _bus() -> EventBus:
    return EventBus(AsyncMock(), Mock(get_refresh_commands=lambda _event: []))


def test_an_unregistered_bus_has_no_record() -> None:
    # This is what makes an ordinary Deebot vacuum on the same account take the
    # library's own path: no record, no gate.
    assert record_for(_bus()) is None


def test_registering_returns_a_record_that_starts_undocked() -> None:
    record = register(_bus())
    assert record.docked is False
    assert record.suppressed is None


def test_the_same_bus_gets_the_same_record() -> None:
    bus = _bus()
    assert register(bus) is record_for(bus)


def test_two_buses_are_independent() -> None:
    first, second = _bus(), _bus()
    register(first).dock()
    register(second)
    assert record_for(first).docked is True
    assert record_for(second).docked is False


def test_dock_sets_and_move_clears_including_the_suppressed_state() -> None:
    record = MowerStateRecord()
    record.dock()
    record.suppressed = State.PAUSED
    assert record.docked is True

    record.move()
    assert record.docked is False
    # Forgotten, not kept: once the mower is moving the last StateEvent is the
    # honest answer again, and a stale PAUSED would outlive the plan it named.
    assert record.suppressed is None


def test_the_entry_is_dropped_when_its_bus_is_collected() -> None:
    bus = _bus()
    register(bus)
    del bus
    gc.collect()
    # WeakKeyDictionary, so nothing has to unregister on config-entry unload.
    assert not _registry_size()


def _registry_size() -> dict[object, object]:
    from custom_components.ecovacs_mower.deebot_patch import state_precedence

    return dict(state_precedence._RECORDS)


def test_a_new_record_knows_no_map() -> None:
    assert MowerStateRecord().map_id is None


def test_note_map_keeps_a_real_map_id_and_follows_a_change() -> None:
    record = MowerStateRecord()
    record.note_map("2049987783")
    assert record.map_id == "2049987783"
    # Last writer wins: a map switched in the app is the map the next border
    # job should run on.
    record.note_map("1")
    assert record.map_id == "1"


def test_note_map_ignores_everything_that_is_not_a_map() -> None:
    # "0" is the library's own "no map" marker (OnCachedMapInfo) and what the
    # onMapTrack envelopes of an idle mower carry; an empty string and a
    # missing field say nothing either.
    record = MowerStateRecord()
    record.note_map("7")
    for junk in (None, "", "0", 0, 12, ["1"]):
        record.note_map(junk)
    assert record.map_id == "7"


def test_note_map_skips_a_map_the_mower_is_not_using() -> None:
    # A getMapInfo_V2 answer can carry fragments for a stored map that is not
    # the active one; last-writer-wins would then name the wrong map. The
    # envelope says which is which with "using", so an explicit 0 is skipped.
    record = MowerStateRecord()
    record.note_map("7", using=1)
    record.note_map("8", using=0)
    assert record.map_id == "7"
    record.note_map("9", using="0")
    assert record.map_id == "7"


def test_note_map_accepts_an_envelope_without_using() -> None:
    # onMapTrack and onMapTrace never carry the field; absence is not "not
    # using", it is "the message does not say", and the id is still good.
    record = MowerStateRecord()
    record.note_map("7", using=None)
    assert record.map_id == "7"
    record.note_map("8")
    assert record.map_id == "8"


def test_moving_does_not_forget_the_map() -> None:
    # Leaving the dock does not change the map, unlike the suppressed state.
    record = MowerStateRecord()
    record.note_map("7")
    record.dock()
    record.move()
    assert record.map_id == "7"


def test_map_id_for_reads_the_record_and_is_none_for_strangers() -> None:
    bus = _bus()
    assert map_id_for(bus) is None  # unregistered: an ordinary vacuum
    register(bus)
    assert map_id_for(bus) is None  # registered, nothing reported yet
    record_for(bus).note_map("7")
    assert map_id_for(bus) == "7"


# Issue #94: the type of the running job, echoed on pause, resume and stop.


def test_a_new_record_knows_no_job_type() -> None:
    assert MowerStateRecord().job_type is None


def test_note_job_keeps_the_type_the_mower_reports() -> None:
    # The O1200's own onCleanInfo during an area job, 2026-09-10.
    record = MowerStateRecord()
    record.note_job({"type": "spotArea", "value": "2", "subContent": {"type": "spotArea"}})
    assert record.job_type == "spotArea"

    record.note_job({"type": "auto"})
    assert record.job_type == "auto"


def test_note_job_ignores_content_without_a_type() -> None:
    record = MowerStateRecord()
    record.note_job({"type": "spotArea"})

    for content in ({}, {"type": ""}, {"type": 3}, {"value": "2"}, None, "spotArea"):
        record.note_job(content)
        assert record.job_type == "spotArea"


def test_end_job_forgets_the_type() -> None:
    record = MowerStateRecord()
    record.note_job({"type": "spotArea"})
    record.end_job()
    assert record.job_type is None


def test_moving_and_docking_keep_the_job_type() -> None:
    # A plan paused on the charger is still the same plan; resuming it needs
    # the type it was started with.
    record = MowerStateRecord()
    record.note_job({"type": "spotArea"})
    record.move()
    assert record.job_type == "spotArea"
    record.dock()
    assert record.job_type == "spotArea"
