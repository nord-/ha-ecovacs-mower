"""``OnError`` republishes the whole code list (issue #133).

The library's ``getError`` handler forwards only ``codes[-1]``; the list is what
the owner needs to report. The bus is a real ``EventBus`` for the reason
``tests/test_fault.py`` gives: whether a repeat is delivered is part of the
contract, and a ``Mock`` would not drop what the real bus drops.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

from deebot_client.event_bus import EventBus
from deebot_client.events import ErrorEvent

from custom_components.ecovacs_mower.deebot_patch.messages import (
    MowerErrorCodesEvent,
    OnError,
)
from custom_components.ecovacs_mower.deebot_patch.state_precedence import register


def _bus(*, mower: bool = True) -> EventBus:
    bus = EventBus(AsyncMock(), Mock(get_refresh_commands=lambda _event: []))
    if mower:
        register(bus)
    return bus


def _push(bus: EventBus, codes: object) -> None:
    OnError.handle(bus, {"body": {"data": {"code": codes}}})


async def _heard(bus: EventBus) -> list[tuple[int, ...]]:
    """Subscribe, push nothing yet; the caller reads the list after pushing."""
    heard: list[tuple[int, ...]] = []

    async def on_codes(event: MowerErrorCodesEvent) -> None:
        heard.append(event.codes)

    bus.subscribe(MowerErrorCodesEvent, on_codes)
    return heard


async def _settle() -> None:
    for _ in range(4):
        await asyncio.sleep(0)


async def test_the_whole_list_is_published_in_the_order_sent() -> None:
    bus = _bus()
    heard = await _heard(bus)

    _push(bus, [622, 627, 621])
    await _settle()

    assert heard == [(622, 627, 621)]
    # The library's reading is untouched: only the last code, as ever.
    assert bus.get_last_event(ErrorEvent).code == 621


async def test_the_heartbeat_publishes_nothing() -> None:
    bus = _bus()
    heard = await _heard(bus)

    _push(bus, [0])
    _push(bus, [])
    _push(bus, [0, 0])
    await _settle()

    assert heard == []


async def test_a_zero_beside_a_fault_is_not_listed() -> None:
    bus = _bus()
    heard = await _heard(bus)

    _push(bus, [0, 622])
    await _settle()

    assert heard == [(622,)]


async def test_the_same_list_twice_is_two_reports() -> None:
    # Without the sequence number the bus would drop the second as a repeat.
    bus = _bus()
    heard = await _heard(bus)

    _push(bus, [622, 627])
    await _settle()
    _push(bus, [622, 627])
    await _settle()

    assert heard == [(622, 627), (622, 627)]


async def test_a_bus_that_is_not_a_patched_mowers_gets_the_librarys_behaviour() -> None:
    # A vacuum on the same account reaches the global handler too.
    bus = _bus(mower=False)
    heard = await _heard(bus)

    _push(bus, [622, 627])
    await _settle()

    assert heard == []
    assert bus.get_last_event(ErrorEvent).code == 627


async def test_a_body_without_a_list_publishes_nothing() -> None:
    bus = _bus()
    heard = await _heard(bus)

    _push(bus, "622")
    OnError.handle(bus, {"body": {"data": {}}})
    await _settle()

    assert heard == []
