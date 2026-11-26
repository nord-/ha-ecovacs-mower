"""``getInfo``: several getters in one request (issue #85).

The envelope is the one a G1-800 (``77atlz``, firmware 1.36.208) answered
with on the issue: each getter's own reply under its name, an unknown name
left out, and an answer that would be too large coming back with ``data``
empty. The transport is faked one level below ``Command._execute``, at the
HTTP call, so that the requests counted here are the requests that would go
out.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from deebot_client.command import Command
from deebot_client.commands.json.common import JsonCommandWithMessageHandling
from deebot_client.event_bus import EventBus
from deebot_client.events import AvailabilityEvent, StateEvent, StatsEvent
from deebot_client.events.base import Event
from deebot_client.exceptions import ApiTimeoutError
from deebot_client.message import HandlingResult, HandlingState, MessageBodyDataDict
from deebot_client.models import State

from custom_components.ecovacs_mower.deebot_patch.commands import (
    GetChargeStateMower,
    GetCleanInfoMower,
    GetStatsMower,
    MowerStateRefresh,
)
from custom_components.ecovacs_mower.deebot_patch.families import (
    Family,
    commit,
    selected,
)
from custom_components.ecovacs_mower.deebot_patch.info import GetInfo, state_and_stats
from custom_components.ecovacs_mower.deebot_patch.messages import MowerStatsEvent
from custom_components.ecovacs_mower.deebot_patch.state_precedence import register

_DEVICE_INFO = {"did": "did-1", "class": "77atlz", "resource": "res"}

_HEADER = {"pri": 1, "tzm": 60, "ts": "1", "ver": "0.0.1", "fwVer": "1.36.208"}
_CHARGING = {"code": 0, "msg": "ok", "data": {"isCharging": 1, "mode": "slot"}}
_PAUSED_PLAN = {
    "code": 0,
    "msg": "ok",
    "data": {
        "trigger": "none",
        "state": "clean",
        "cleanState": {"motionState": "pause"},
    },
}
_STATS = {
    "code": 0,
    "msg": "ok",
    "data": {"area": 211275, "time": 704, "mowedArea": 87825},
}
_REPLIES = {
    "getChargeState": _CHARGING,
    "getCleanInfo": _PAUSED_PLAN,
    "getCleanInfo_V2": _PAUSED_PLAN,
    "getStats": _STATS,
}
# What a getter made up by a test is answered with.
_OK = {"code": 0, "msg": "ok", "data": {}}

_NO_ANSWER = {"ret": "fail", "errno": 500, "debug": "wait for response timed out"}
_OFFLINE = {"ret": "fail", "errno": 4200}


def _bus() -> EventBus:
    return EventBus(AsyncMock(), Mock(get_refresh_commands=lambda _event: []))


def _collect[EventT: Event](bus: EventBus, event_type: type[EventT]) -> list[EventT]:
    received: list[EventT] = []

    async def on_event(event: EventT) -> None:
        received.append(event)

    bus.subscribe(event_type, on_event)
    return received


def _batch(**replies: dict[str, Any]) -> dict[str, Any]:
    """A ``getInfo`` answer carrying *replies*, as ``devmanager.do`` returns it."""
    return {
        "ret": "ok",
        "resp": {
            "header": _HEADER,
            "body": {"code": 0, "msg": "ok", "data": dict(replies)},
        },
    }


def _plain(reply: dict[str, Any]) -> dict[str, Any]:
    """The answer to a getter sent as the plain command it is."""
    return {"ret": "ok", "resp": {"header": _HEADER, "body": reply}}


class _Mower:
    """A fake ``devmanager.do``: answers by rule, and records what was asked.

    *limit* is the number of getters one ``getInfo`` answers; a batch above
    it comes back with ``data`` empty, as an answer over the size cap does.
    *unknown* names are left out of a batch, and *failing* ones carry a
    non-zero code there. Sent as plain commands, all of them answer.
    """

    def __init__(
        self,
        *,
        limit: int = 99,
        unknown: frozenset[str] = frozenset(),
        failing: frozenset[str] = frozenset(),
        plain: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.limit = limit
        self.unknown = unknown
        self.failing = failing
        self.plain = plain or {}
        self.sent: list[tuple[str, ...]] = []

    async def __call__(
        self, command: Command, authenticator: Any, device_info: Any
    ) -> dict[str, Any]:
        if command.NAME != "getInfo":
            self.sent.append((command.NAME,))
            return self.plain.get(
                command.NAME, _plain(_REPLIES.get(command.NAME, _OK))
            )

        names = tuple(command._args)
        self.sent.append(("getInfo", *names))
        if len(names) > self.limit:
            return _batch()
        return _batch(
            **{
                name: (
                    {"code": 1124520, "msg": "fail"}
                    if name in self.failing
                    else _REPLIES.get(name, _OK)
                )
                for name in names
                if name not in self.unknown
            }
        )


def _transport(mower: _Mower):
    """*mower* as the method it replaces: a function, so that it is bound."""

    async def execute_api_request(self, authenticator, device_info):
        return await mower(self, authenticator, device_info)

    return execute_api_request


async def _run(mower: _Mower, command: GetInfo, bus: EventBus):
    with patch.object(Command, "_execute_api_request", _transport(mower)):
        result = await command._execute(AsyncMock(), _DEVICE_INFO, bus)
    await asyncio.sleep(0)
    return result


def _getter(name: str) -> JsonCommandWithMessageHandling:
    """A getter called *name*, whose reply nothing reads."""

    class Getter(JsonCommandWithMessageHandling, MessageBodyDataDict):
        NAME = name

        @classmethod
        def _handle_body_data_dict(cls, event_bus, data):
            return HandlingResult.success()

    return Getter()


async def test_three_getters_are_one_request() -> None:
    mower = _Mower()

    result, _ = await _run(mower, state_and_stats(), _bus())

    assert mower.sent == [("getInfo", "getChargeState", "getCleanInfo", "getStats")]
    assert result.state is HandlingState.SUCCESS


async def test_the_request_is_a_list_of_names_under_body_data() -> None:
    from custom_components.ecovacs_mower.deebot_patch.info import _InfoRequest

    payload = _InfoRequest(["getChargeState", "getStats"])._get_payload()

    assert payload["body"] == {"data": ["getChargeState", "getStats"]}


async def test_each_reply_reaches_its_own_handler() -> None:
    bus = _bus()
    stats = _collect(bus, StatsEvent)
    mower_stats = _collect(bus, MowerStatsEvent)

    await _run(_Mower(), state_and_stats(), bus)

    assert [(event.area, event.time) for event in stats] == [(211275, 704)]
    assert len(mower_stats) == 1


async def test_the_charge_state_is_read_before_the_clean_info() -> None:
    # Issue #67 through one answer: a paused plan on a mower that is on its
    # charger is docked, and only because the charge reply was handled first.
    bus = _bus()
    register(bus)
    published = _collect(bus, StateEvent)

    await _run(_Mower(), state_and_stats(), bus)

    assert [event.state for event in published] == [State.DOCKED]


async def test_the_clean_info_getter_is_named_for_the_family_in_use() -> None:
    commit(_DEVICE_INFO["did"], Family.V2)
    mower = _Mower()

    await _run(mower, state_and_stats(), _bus())

    assert mower.sent == [("getInfo", "getChargeState", "getCleanInfo_V2", "getStats")]


async def test_an_empty_answer_is_split_in_half_and_asked_again() -> None:
    # Too large for one answer: data comes back empty, with nothing naming
    # what was left out.
    mower = _Mower(limit=2)
    bus = _bus()
    stats = _collect(bus, StatsEvent)

    result, _ = await _run(mower, state_and_stats(), bus)

    assert mower.sent == [
        ("getInfo", "getChargeState", "getCleanInfo", "getStats"),
        ("getInfo", "getChargeState"),
        ("getInfo", "getCleanInfo", "getStats"),
    ]
    assert result.state is HandlingState.SUCCESS
    assert len(stats) == 1


async def test_the_halves_are_split_again_while_they_come_back_empty() -> None:
    mower = _Mower(limit=1)

    await _run(mower, state_and_stats(), _bus())

    assert mower.sent == [
        ("getInfo", "getChargeState", "getCleanInfo", "getStats"),
        ("getInfo", "getChargeState"),
        ("getInfo", "getCleanInfo", "getStats"),
        ("getInfo", "getCleanInfo"),
        ("getInfo", "getStats"),
    ]


_SIZES = "custom_components.ecovacs_mower.deebot_patch.info._REPLY_SIZES"
_UNMEASURED = "custom_components.ecovacs_mower.deebot_patch.info._UNMEASURED_REPLY"


async def test_a_batch_is_cut_before_its_answer_would_pass_the_cap() -> None:
    # 196 for the empty answer plus 308 for each of these: two fit under
    # 1000, three would not, so the third goes in a batch of its own
    # without the three being asked together first.
    mower = _Mower()
    getters = [_getter(name) for name in ("getAaaa", "getBbbb", "getCccc")]

    with patch(_SIZES, {"getAaaa": 297, "getBbbb": 297, "getCccc": 297}):
        await _run(mower, GetInfo(getters), _bus())

    assert mower.sent == [
        ("getInfo", "getAaaa", "getBbbb"),
        ("getInfo", "getCccc"),
    ]


async def test_a_getter_nobody_measured_counts_at_the_default_size() -> None:
    mower = _Mower()
    getters = [_getter(name) for name in ("getAaaa", "getBbbb", "getCccc")]

    with patch(_SIZES, {}), patch(_UNMEASURED, 297):
        await _run(mower, GetInfo(getters), _bus())

    assert [len(request) - 1 for request in mower.sent] == [2, 1]


def test_the_measured_sizes_put_the_tick_in_one_batch() -> None:
    from custom_components.ecovacs_mower.deebot_patch.info import (
        _ANSWER_BUDGET,
        _EMPTY_ANSWER,
        _reply_size,
    )

    names = ("getChargeState", "getCleanInfo_V2", "getStats")
    assert _EMPTY_ANSWER + sum(map(_reply_size, names)) <= _ANSWER_BUDGET


async def test_an_answer_larger_than_its_estimate_is_still_split() -> None:
    # The estimate only decides where the first requests are cut; the
    # mower's own refusal still splits a batch it got wrong.
    mower = _Mower(limit=1)
    getters = [_getter(name) for name in ("getAaaa", "getBbbb")]

    with patch(_SIZES, {"getAaaa": 1, "getBbbb": 1}):
        await _run(mower, GetInfo(getters), _bus())

    assert mower.sent == [
        ("getInfo", "getAaaa", "getBbbb"),
        ("getInfo", "getAaaa"),
        ("getInfo", "getBbbb"),
    ]


def _refusing(body: dict[str, Any], sent: list[tuple[str, ...]]):
    """A transport whose every ``getInfo`` is answered with *body*."""

    async def execute_api_request(self, authenticator, device_info):
        if self.NAME != "getInfo":
            sent.append((self.NAME,))
            return _plain(_REPLIES[self.NAME])
        sent.append(("getInfo", *self._args))
        return {"ret": "ok", "resp": {"header": _HEADER, "body": body}}

    return execute_api_request


async def test_an_answer_refused_another_way_is_not_split() -> None:
    # Only code 0 with an empty data object is the too-large refusal. Any
    # other empty answer would be refused again for each half, so the three
    # getters go out alone: four requests where splitting would send eight.
    sent: list[tuple[str, ...]] = []
    bus = _bus()
    stats = _collect(bus, StatsEvent)

    with patch.object(
        Command, "_execute_api_request", _refusing({"code": 1, "msg": "fail"}, sent)
    ):
        result, _ = await state_and_stats()._execute(AsyncMock(), _DEVICE_INFO, bus)
    await asyncio.sleep(0)

    assert sent == [
        ("getInfo", "getChargeState", "getCleanInfo", "getStats"),
        ("getChargeState",),
        ("getCleanInfo",),
        ("getStats",),
    ]
    assert result.state is HandlingState.SUCCESS
    assert len(stats) == 1


async def test_an_answer_without_data_is_not_split() -> None:
    sent: list[tuple[str, ...]] = []

    with patch.object(
        Command, "_execute_api_request", _refusing({"code": 0, "msg": "ok"}, sent)
    ):
        await state_and_stats()._execute(AsyncMock(), _DEVICE_INFO, _bus())

    assert [request[0] for request in sent] == [
        "getInfo",
        "getChargeState",
        "getCleanInfo",
        "getStats",
    ]


async def test_a_getter_unanswered_alone_is_sent_as_the_plain_command() -> None:
    mower = _Mower(limit=0)
    bus = _bus()
    stats = _collect(bus, StatsEvent)

    result, _ = await _run(mower, GetInfo([GetStatsMower()]), bus)

    assert mower.sent == [("getInfo", "getStats"), ("getStats",)]
    assert result.state is HandlingState.SUCCESS
    assert len(stats) == 1


async def test_a_name_left_out_of_an_answer_is_asked_for_alone() -> None:
    # An unknown name is dropped without a key while the rest is answered,
    # so the batch is not split: only the missing getter goes out again.
    mower = _Mower(unknown=frozenset({"getStats"}))
    bus = _bus()
    stats = _collect(bus, StatsEvent)

    await _run(mower, state_and_stats(), bus)

    assert mower.sent == [
        ("getInfo", "getChargeState", "getCleanInfo", "getStats"),
        ("getStats",),
    ]
    assert len(stats) == 1


async def test_a_reply_with_a_failure_code_is_asked_for_alone() -> None:
    mower = _Mower(failing=frozenset({"getStats"}))
    bus = _bus()
    stats = _collect(bus, StatsEvent)

    await _run(mower, state_and_stats(), bus)

    assert mower.sent[-1] == ("getStats",)
    assert len(stats) == 1


async def test_an_unanswered_getter_keeps_its_place_in_the_order() -> None:
    # The charge state left out of the batch is asked alone before the clean
    # info reply, which arrived first, is interpreted.
    mower = _Mower(unknown=frozenset({"getChargeState"}))
    bus = _bus()
    register(bus)
    published = _collect(bus, StateEvent)

    await _run(mower, state_and_stats(), bus)

    assert [event.state for event in published] == [State.DOCKED]


async def test_the_other_family_is_learned_through_the_plain_command() -> None:
    # A mower on the V2 dialect whose family is not known yet: the batch
    # names getCleanInfo, which is left out, and the wrapper finds V2.
    mower = _Mower(
        unknown=frozenset({"getCleanInfo"}), plain={"getCleanInfo": _NO_ANSWER}
    )

    await _run(mower, state_and_stats(), _bus())

    assert mower.sent == [
        ("getInfo", "getChargeState", "getCleanInfo", "getStats"),
        ("getCleanInfo",),
        ("getCleanInfo_V2",),
    ]
    assert selected(_DEVICE_INFO["did"]) is Family.V2


async def test_an_offline_mower_is_asked_once() -> None:
    sent: list[str] = []

    async def offline(command, authenticator, device_info):
        sent.append(command.NAME)
        return _OFFLINE

    bus = _bus()
    availability = _collect(bus, AvailabilityEvent)
    with patch.object(Command, "_execute_api_request", offline):
        result, _ = await state_and_stats()._execute(AsyncMock(), _DEVICE_INFO, bus)
    await asyncio.sleep(0)

    assert sent == ["getInfo"]
    assert result.state is HandlingState.FAILED
    assert [event.available for event in availability] == [False]


async def test_no_answer_to_the_request_is_not_asked_again() -> None:
    sent: list[str] = []

    async def silent(command, authenticator, device_info):
        sent.append(command.NAME)
        return _NO_ANSWER

    with patch.object(Command, "_execute_api_request", silent):
        result, _ = await state_and_stats()._execute(AsyncMock(), _DEVICE_INFO, _bus())

    assert sent == ["getInfo"]
    assert result.state is HandlingState.FAILED


async def test_a_timeout_is_not_asked_again() -> None:
    sent: list[str] = []

    async def timeout(command, authenticator, device_info):
        sent.append(command.NAME)
        raise ApiTimeoutError("devmanager.do", 60)

    with patch.object(Command, "_execute_api_request", timeout):
        result, response = await state_and_stats()._execute(
            AsyncMock(), _DEVICE_INFO, _bus()
        )

    assert sent == ["getInfo"]
    assert result.state is HandlingState.FAILED
    assert response == {}


async def test_replies_that_arrived_before_a_half_went_unanswered_are_kept() -> None:
    answers = [_batch(), _batch(getChargeState=_CHARGING), _NO_ANSWER]
    sent: list[tuple[str, ...]] = []

    async def scripted(command, authenticator, device_info):
        sent.append((command.NAME, *command._args))
        return answers.pop(0)

    bus = _bus()
    published = _collect(bus, StateEvent)
    with patch.object(Command, "_execute_api_request", scripted):
        result, _ = await state_and_stats()._execute(AsyncMock(), _DEVICE_INFO, bus)
    await asyncio.sleep(0)

    # The second half got no answer, so neither it nor its getters are asked
    # again, and the charge state that did arrive is still published.
    assert len(sent) == 3
    assert [event.state for event in published] == [State.DOCKED]
    assert result.state is HandlingState.SUCCESS


async def test_the_device_counts_as_reached_when_a_getter_answered() -> None:
    with patch.object(Command, "_execute_api_request", _transport(_Mower())):
        result = await state_and_stats().execute(AsyncMock(), _DEVICE_INFO, _bus())

    assert result.device_reached is True
    assert result.raw_response["ret"] == "ok"


async def test_the_device_does_not_count_as_reached_when_none_did() -> None:
    async def silent(command, authenticator, device_info):
        return _NO_ANSWER

    with patch.object(Command, "_execute_api_request", silent):
        result = await state_and_stats().execute(AsyncMock(), _DEVICE_INFO, _bus())

    assert result.device_reached is False


def test_the_poll_asks_for_the_state_refresh_and_the_stats() -> None:
    # The state is the refresh every patched class has, whose halves are
    # the charge state and the clean info, in that order (issue #67).
    batch = state_and_stats()

    assert [type(command) for command in batch._commands] == [
        MowerStateRefresh,
        GetStatsMower,
    ]
    assert [type(half) for half in MowerStateRefresh.halves()] == [
        GetChargeStateMower,
        GetCleanInfoMower,
    ]


async def test_a_getter_asked_alone_holds_back_only_its_own_command() -> None:
    # The clean info of a dialect not learned yet is left out of the batch
    # and waits out an errno 500 alone. The stats arrived in the same answer
    # and are handled before that, not after.
    bus = _bus()
    handled_before: list[bool] = []
    mower = _Mower(unknown=frozenset({"getCleanInfo"}))

    async def transport(command, authenticator, device_info):
        if command.NAME == "getCleanInfo":
            handled_before.append(bus.get_last_event(StatsEvent) is not None)
        return await mower(command, authenticator, device_info)

    with patch.object(Command, "_execute_api_request", _transport(transport)):
        await state_and_stats()._execute(AsyncMock(), _DEVICE_INFO, bus)

    assert handled_before == [True]


async def test_two_commands_that_read_one_getter_name_it_once() -> None:
    # The library's GetBorderSwitch and the patch layer's GetBorderSwitchMower
    # both read getBorderSwitch and publish different events: one name in the
    # request, and the reply goes to both.
    from deebot_client.commands.json import GetBorderSwitch
    from deebot_client.events import BorderSwitchEvent

    from custom_components.ecovacs_mower.deebot_patch.commands import (
        GetBorderSwitchMower,
    )
    from custom_components.ecovacs_mower.deebot_patch.messages import (
        MowerBorderSwitchEvent,
    )

    reply = {"code": 0, "msg": "ok", "data": {"enable": 1, "mode": 0}}
    mower = _Mower()
    bus = _bus()
    library = _collect(bus, BorderSwitchEvent)
    patched = _collect(bus, MowerBorderSwitchEvent)

    with patch.dict(_REPLIES, {"getBorderSwitch": reply}):
        await _run(mower, GetInfo([GetBorderSwitch(), GetBorderSwitchMower()]), bus)

    assert mower.sent == [("getInfo", "getBorderSwitch")]
    assert len(library) == 1
    assert len(patched) == 1


def test_two_batches_of_the_same_getters_are_equal() -> None:
    assert state_and_stats() == state_and_stats()
    assert state_and_stats() != GetInfo([GetStatsMower()])
    assert hash(state_and_stats()) == hash(state_and_stats())
