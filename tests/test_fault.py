"""Tests for the fault latch (issue #53).

No Home Assistant import anywhere in this file, and none in ``fault.py``
either, so these run locally on Windows alongside ``tests/deebot_patch/``:

    python -m pytest tests/test_fault.py -p no:homeassistant -v

The bus is a real ``EventBus`` rather than a ``Mock`` in every test that cares
about how often something is published. ``EventBus.notify`` suppresses an event
equal to the previous one of the same type, and that suppression is part of the
latch's contract — a ``Mock`` would happily record duplicates the real bus
swallows and prove nothing.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

from deebot_client.event_bus import EventBus
from deebot_client.events import ErrorEvent, StateEvent
from deebot_client.models import State
import pytest

from custom_components.ecovacs_mower.deebot_patch.messages import handle_clean_info
from custom_components.ecovacs_mower.deebot_patch.state_precedence import register
from custom_components.ecovacs_mower.fault import FaultLatch, MowerFaultEvent

# The fault from the issue: pushed exactly once, then overwritten 89 ms later.
_BLOCKED = 406
_BLOCKED_TEXT = "Blade-disc blocked! Blade-disc cannot rotate."

_WEAK_SIGNAL = 422


def _latch() -> tuple[FaultLatch, list[MowerFaultEvent]]:
    """A subscribed latch on a real bus, and the events it publishes."""
    bus = EventBus(AsyncMock(), Mock(get_refresh_commands=lambda _event: []))
    published: list[MowerFaultEvent] = []

    async def on_fault(event: MowerFaultEvent) -> None:
        published.append(event)

    bus.subscribe(MowerFaultEvent, on_fault)
    latch = FaultLatch(Mock(events=bus, device_info={"did": "test-did"}))
    latch.subscribe()
    return latch, published


async def _settle() -> None:
    """Let the bus's tasks run to the end of the chain.

    ``EventBus.notify`` dispatches to a subscriber in a task, and the latch's
    own handler notifies again, so a published fault is two task hops away from
    the caller — one ``sleep(0)`` only gets as far as the latch.
    """
    for _ in range(4):
        await asyncio.sleep(0)


async def _push_error(bus: EventBus, code: int) -> None:
    """Push what ``GetError`` notifies for one ``onError`` payload."""
    bus.notify(ErrorEvent(code, None))
    await _settle()


async def _push_state(bus: EventBus, state: State) -> None:
    bus.notify(StateEvent(state))
    await _settle()


async def test_a_fault_latches_and_publishes_the_code() -> None:
    latch, published = _latch()

    await _push_error(latch._device.events, _BLOCKED)

    assert latch.code == _BLOCKED
    assert published == [MowerFaultEvent(code=_BLOCKED, description=_BLOCKED_TEXT)]


async def test_a_zero_does_not_clear_the_latch() -> None:
    """The bug. code:[0] arrives 89 ms later and then 3076 more times."""
    latch, published = _latch()
    bus = latch._device.events

    await _push_error(bus, _BLOCKED)
    await _push_error(bus, 0)

    assert latch.code == _BLOCKED
    assert len(published) == 1


async def test_a_flapping_fault_publishes_one_transition() -> None:
    """406 -> 0 -> 406 -> 0 is four distinct ErrorEvents and one fault."""
    latch, published = _latch()
    bus = latch._device.events

    for code in (_BLOCKED, 0, _BLOCKED, 0):
        await _push_error(bus, code)

    assert latch.code == _BLOCKED
    assert len(published) == 1


@pytest.mark.parametrize("state", [State.DOCKED, State.CLEANING])
async def test_recovery_clears_the_latch(state: State) -> None:
    latch, published = _latch()
    bus = latch._device.events

    await _push_error(bus, _BLOCKED)
    await _push_state(bus, state)

    assert latch.code is None
    assert published[-1] == MowerFaultEvent(code=None, description=None)


@pytest.mark.parametrize(
    "state", [State.PAUSED, State.IDLE, State.RETURNING, State.ERROR]
)
async def test_a_state_that_is_not_recovery_does_not_clear(state: State) -> None:
    """Paused off the dock is exactly the situation a fault survives in."""
    latch, published = _latch()
    bus = latch._device.events

    await _push_error(bus, _BLOCKED)
    await _push_state(bus, state)

    assert latch.code == _BLOCKED
    assert len(published) == 1


async def test_a_clear_request_clears_the_latch() -> None:
    """The button. The one clear that needs no firmware cooperation at all."""
    latch, published = _latch()

    await _push_error(latch._device.events, _BLOCKED)
    latch.clear_by_request()
    await asyncio.sleep(0)

    assert latch.code is None
    assert published[-1] == MowerFaultEvent(code=None, description=None)


async def test_a_clear_with_nothing_latched_publishes_nothing() -> None:
    latch, published = _latch()

    latch.clear_by_request()
    await _push_state(latch._device.events, State.DOCKED)

    assert latch.code is None
    assert published == []


async def test_a_new_code_replaces_the_latched_one() -> None:
    """The latest non-zero code is the current diagnosis."""
    latch, published = _latch()
    bus = latch._device.events

    await _push_error(bus, _BLOCKED)
    await _push_error(bus, _WEAK_SIGNAL)

    assert latch.code == _WEAK_SIGNAL
    assert published[-1].code == _WEAK_SIGNAL
    assert len(published) == 2


async def test_an_unknown_code_latches_without_a_description() -> None:
    """A code in neither table still has to raise the fault."""
    latch, published = _latch()

    await _push_error(latch._device.events, 999)

    assert latch.code == 999
    assert published == [MowerFaultEvent(code=999, description=None)]


async def test_a_late_subscriber_gets_the_latched_fault() -> None:
    """A binary_sensor enabled after the fault must not come up as OK.

    ``EventBus`` records ``last_event`` before it checks for subscribers, so a
    fault published while nothing listened is replayed on subscribe. The latch
    depends on that instead of re-publishing on its own.
    """
    bus = EventBus(AsyncMock(), Mock(get_refresh_commands=lambda _event: []))
    latch = FaultLatch(Mock(events=bus, device_info={"did": "test-did"}))
    latch.subscribe()

    await _push_error(bus, _BLOCKED)

    replayed: list[MowerFaultEvent] = []

    async def on_fault(event: MowerFaultEvent) -> None:
        replayed.append(event)

    bus.subscribe(MowerFaultEvent, on_fault)
    await asyncio.sleep(0)

    assert replayed == [MowerFaultEvent(code=_BLOCKED, description=_BLOCKED_TEXT)]


async def test_a_latched_fault_survives_a_docked_paused_flap() -> None:
    """Issue #67 defeats issue #53, and it takes four steps to reproduce —
    PAUSED alone proves nothing, because PAUSED is not in _RECOVERY_STATES
    and the latch would survive it with or without the gate.

    The real break needs the second DOCKED:
        DOCKED -> fault latched -> PAUSED -> DOCKED  (cleared, wrongly)
    The bus drops an event equal to the previous one, so while the mower sits
    on its charger a repeated DOCKED is swallowed and a fault latched there
    survives. The flapping PAUSED breaks that chain: the next DOCKED is a new
    event, reaches the latch, and clears a fault nobody resolved.

    With the gate the PAUSED is never published, last_event stays DOCKED, and
    the second DOCKED is deduped. The dedup is part of the fix here, not just
    the hazard it is elsewhere in this change.
    """
    latch, published = _latch()
    bus = latch._device.events
    record = register(bus)

    await _push_state(bus, State.DOCKED)
    # handle_clean_info's gate reads record.docked; OnChargeInfo/
    # GetChargeStateMower are what set it in production. Setting it directly
    # keeps this test about the gate, not about those handlers, which are
    # already covered in tests/deebot_patch/test_messages.py.
    record.dock()

    await _push_error(bus, _BLOCKED)
    assert latch.code == _BLOCKED

    handle_clean_info(
        bus,
        {"trigger": "none", "state": "clean", "cleanState": {"motionState": "pause"}},
    )
    await _push_state(bus, State.DOCKED)

    assert latch.code == _BLOCKED
