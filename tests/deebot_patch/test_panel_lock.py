"""The panel lock is held in the record the handlers write (issue #99).

A locked mower discards every command without an error, so the guard in
``entity.py`` has to read the lock at the moment a command goes out. The bus is
a real ``EventBus`` for the reason ``tests/test_fault.py`` gives.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

from deebot_client.event_bus import EventBus

from custom_components.ecovacs_mower.deebot_patch.messages import (
    MowerProtectStateEvent,
    OnProtectState,
)
from custom_components.ecovacs_mower.deebot_patch.state_precedence import (
    is_locked,
    register,
)


def _bus(*, mower: bool = True) -> EventBus:
    bus = EventBus(AsyncMock(), Mock(get_refresh_commands=lambda _event: []))
    if mower:
        register(bus)
    return bus


def _protect(*, locked: int, **overrides: int) -> dict:
    """An ``onProtectState`` push, in the shape an O1200 on 1.13.10 sent."""
    data = {
        "isAnimProtect": 0,
        "isRainProtect": 0,
        "isRainDelay": 0,
        "isEStop": 0,
        "isLocked": locked,
        "isPinCode": 0,
        "isPrepareDataSuccess": 1,
    }
    data.update(overrides)
    return {"body": {"data": data, "code": 0, "msg": "ok"}}


def test_a_mower_is_not_locked_until_it_says_so() -> None:
    assert is_locked(_bus()) is False


def test_the_lock_is_recorded_before_the_event_goes_out() -> None:
    bus = _bus()

    OnProtectState.handle(bus, _protect(locked=1))

    assert is_locked(bus) is True
    assert bus.get_last_event(MowerProtectStateEvent).locked is True


def test_unlocking_by_hand_releases_it() -> None:
    # The sequence from the issue: locked at 15:36:38, released at 15:37:38.
    bus = _bus()
    OnProtectState.handle(bus, _protect(locked=1))

    OnProtectState.handle(bus, _protect(locked=0, isPinCode=1))

    assert is_locked(bus) is False


def test_a_partial_push_leaves_the_lock_as_it_was() -> None:
    # An incomplete payload is dropped by the handler; it must not unlock.
    bus = _bus()
    OnProtectState.handle(bus, _protect(locked=1))

    OnProtectState.handle(bus, {"body": {"data": {"isLocked": 0}}})

    assert is_locked(bus) is True


def test_a_bus_that_is_not_a_patched_mowers_is_never_locked() -> None:
    # A vacuum on the same account reaches the global handler too.
    bus = _bus(mower=False)

    OnProtectState.handle(bus, _protect(locked=1))

    assert is_locked(bus) is False
