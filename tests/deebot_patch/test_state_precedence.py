"""Tests for the per-device state record (issue #67)."""

from __future__ import annotations

import gc
from unittest.mock import AsyncMock, Mock

from deebot_client.event_bus import EventBus
from deebot_client.models import State

from custom_components.ecovacs_mower.deebot_patch.state_precedence import (
    MowerStateRecord,
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
