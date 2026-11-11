"""The edge-mowing setting: ``getBorderSwitch``, ``setBorderSwitch`` and the push.

The payloads are the ones reported on issue #166: a G1-800 (``77atlz``,
firmware 1.36.208) answers ``getBorderSwitch`` with both fields and pushes
``onBorderSwitch`` with both after every write, and an A1600 LiDAR Pro
(``e4gqia``) refuses a write without ``mode``.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from deebot_client.commands.json.common import ExecuteCommand
from deebot_client.event_bus import EventBus
from deebot_client.hardware import _DEVICES, get_static_device_info
from deebot_client.message import HandlingResult, HandlingState
from deebot_client.messages.json import MESSAGES

from custom_components.ecovacs_mower.deebot_patch import (
    PatchContractError,
    apply,
    verify_capabilities,
)
from custom_components.ecovacs_mower.deebot_patch.commands import (
    GetBorderSwitchMower,
    SetBorderSwitchMower,
)
from custom_components.ecovacs_mower.deebot_patch.hardware import (
    SUPPORTED_CLASSES,
    MowerProfile,
    patch_device_info,
    profile_for_class,
)
from custom_components.ecovacs_mower.deebot_patch.messages import (
    MowerBorderSwitchEvent,
    OnBorderSwitch,
    border_switch_for,
    reset_border_switch_readings,
)

G1_800 = "77atlz"
A1600_RTK = "xmp9ds"
O800_RTK_2 = "2px96q"
A1600_LIDAR_PRO = "e4gqia"


@pytest.fixture(autouse=True)
def _clean_state():
    """Empty the readings and the library's device cache around every test."""
    reset_border_switch_readings()
    for class_ in SUPPORTED_CLASSES:
        _DEVICES.pop(class_, None)
    yield
    reset_border_switch_readings()
    for class_ in SUPPORTED_CLASSES:
        _DEVICES.pop(class_, None)


def _push(bus: Any, data: Any) -> HandlingResult:
    return OnBorderSwitch.handle(
        bus, {"header": {"pri": 1, "ver": "0.0.1"}, "body": {"data": data}}
    )


def _events(bus: Mock) -> list[MowerBorderSwitchEvent]:
    return [
        call.args[0]
        for call in bus.notify.call_args_list
        if isinstance(call.args[0], MowerBorderSwitchEvent)
    ]


# --- the push and the answer -------------------------------------------------


def test_the_push_is_registered() -> None:
    apply()

    assert MESSAGES["onBorderSwitch"] is OnBorderSwitch


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"enable": 1, "mode": 0}, MowerBorderSwitchEvent(True, 0)),
        ({"enable": 0, "mode": 0}, MowerBorderSwitchEvent(False, 0)),
        ({"enable": 1, "mode": 2}, MowerBorderSwitchEvent(True, 2)),
        ({"enable": True, "mode": 1}, MowerBorderSwitchEvent(True, 1)),
    ],
)
def test_a_push_publishes_both_fields_and_records_them(
    data: dict, expected: MowerBorderSwitchEvent
) -> None:
    bus = Mock()

    result = _push(bus, data)

    assert result.state is HandlingState.SUCCESS
    assert _events(bus) == [expected]
    assert border_switch_for(bus) == (expected.enabled, expected.mode)


def test_nothing_is_known_before_the_first_report() -> None:
    assert border_switch_for(Mock()) == (None, None)


def test_a_mode_nobody_has_seen_is_kept_as_reported() -> None:
    # A write that changes only enable has to send the mode back unchanged,
    # whatever it is.
    bus = Mock()

    _push(bus, {"enable": 1, "mode": 7})

    assert border_switch_for(bus) == (True, 7)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        # The library's shape: no mode at all.
        ({"enable": 0}, (False, 1)),
        ({"mode": 2}, (True, 2)),
        # A field of the wrong type is a missing field.
        ({"enable": "0", "mode": 2}, (True, 2)),
        ({"enable": 0, "mode": True}, (False, 1)),
        ({"enable": 0, "mode": -1}, (False, 1)),
        ({"enable": 0, "mode": "2"}, (False, 1)),
    ],
)
def test_a_short_payload_keeps_the_other_field(data: dict, expected: tuple) -> None:
    bus = Mock()
    _push(bus, {"enable": 1, "mode": 1})

    _push(bus, data)

    assert border_switch_for(bus) == expected


def test_a_payload_without_mode_leaves_it_unknown() -> None:
    bus = Mock()

    _push(bus, {"enable": 1})

    assert border_switch_for(bus) == (True, None)
    assert _events(bus) == [MowerBorderSwitchEvent(True, None)]


@pytest.mark.parametrize("data", [{}, {"enable": 2}, {"enable": None, "mode": None}])
def test_a_payload_with_nothing_usable_publishes_nothing(data: dict) -> None:
    bus = Mock()

    result = _push(bus, data)

    assert result.state is not HandlingState.SUCCESS
    assert _events(bus) == []
    assert border_switch_for(bus) == (None, None)


def test_readings_are_kept_per_mower() -> None:
    first, second = Mock(), Mock()

    _push(first, {"enable": 1, "mode": 2})
    _push(second, {"enable": 0, "mode": 0})

    assert border_switch_for(first) == (True, 2)
    assert border_switch_for(second) == (False, 0)


async def test_a_push_the_bus_drops_still_sets_the_next_write() -> None:
    # The bus drops an event equal to the last one it published before any
    # subscriber runs, so of these four pushes a subscriber sees two. The
    # write is built from what the handler recorded, which does not depend on
    # any of them having been delivered.
    bus = EventBus(AsyncMock(), Mock(get_refresh_commands=lambda _event: []))
    received: list[MowerBorderSwitchEvent] = []

    async def on_event(event: MowerBorderSwitchEvent) -> None:
        received.append(event)

    bus.subscribe(MowerBorderSwitchEvent, on_event)

    for data in (
        {"enable": 1, "mode": 1},
        {"enable": 1, "mode": 1},
        {"enable": 1, "mode": 2},
        {"enable": 1, "mode": 2},
    ):
        _push(bus, data)
    await asyncio.sleep(0)

    assert received == [
        MowerBorderSwitchEvent(True, 1),
        MowerBorderSwitchEvent(True, 2),
    ]
    assert SetBorderSwitchMower.from_reported(bus, enable=False)._args == {
        "enable": 0,
        "mode": 2,
    }


# --- the read ----------------------------------------------------------------


def test_the_read_names_the_wire_command_and_takes_no_arguments() -> None:
    assert GetBorderSwitchMower.NAME == "getBorderSwitch"
    assert GetBorderSwitchMower()._args == {}


def test_the_read_parses_its_answer_with_the_push_handler() -> None:
    assert (
        GetBorderSwitchMower._handle_body_data_dict.__func__
        is OnBorderSwitch._handle_body_data_dict.__func__
    )


def test_the_read_records_mode_from_the_answer() -> None:
    # What a G1-800 answers through devmanager.do.
    bus = Mock()

    result = GetBorderSwitchMower()._handle_response(
        bus,
        {
            "ret": "ok",
            "resp": {
                "header": {"pri": 1, "ver": "0.0.1", "fwVer": "1.36.208"},
                "body": {"code": 0, "msg": "ok", "data": {"enable": 1, "mode": 0}},
            },
        },
    )

    assert result.state is HandlingState.SUCCESS
    assert border_switch_for(bus) == (True, 0)


def test_a_failed_read_records_nothing() -> None:
    bus = Mock()

    result = GetBorderSwitchMower()._handle_response(
        bus, {"ret": "fail", "errno": 500}
    )

    assert result.state is not HandlingState.SUCCESS
    assert border_switch_for(bus) == (None, None)


# --- the write ---------------------------------------------------------------


def test_the_write_carries_both_fields_as_integers() -> None:
    assert SetBorderSwitchMower.NAME == "setBorderSwitch"
    assert SetBorderSwitchMower(enable=True, mode=2)._args == {"enable": 1, "mode": 2}
    assert SetBorderSwitchMower(enable=False, mode=0)._args == {
        "enable": 0,
        "mode": 0,
    }


def test_the_write_reports_a_refusal_as_a_failure() -> None:
    # An A1600 LiDAR Pro's answer to the library's enable-only write. With
    # CustomCommand it would pass for success.
    assert issubclass(SetBorderSwitchMower, ExecuteCommand)

    result = SetBorderSwitchMower._handle_body(
        Mock(), {"code": 20011, "msg": "mode is null"}
    )

    assert result.state is HandlingState.FAILED


@pytest.mark.parametrize(
    ("reported", "enable", "expected"),
    [
        ({"enable": 1, "mode": 0}, False, {"enable": 0, "mode": 0}),
        ({"enable": 0, "mode": 0}, True, {"enable": 1, "mode": 0}),
        # The mode an A1600 LiDAR Pro reads: a fixed 0 would overwrite it.
        ({"enable": 1, "mode": 1}, False, {"enable": 0, "mode": 1}),
    ],
)
def test_changing_enable_resends_the_reported_mode(
    reported: dict, enable: bool, expected: dict
) -> None:
    bus = Mock()
    _push(bus, reported)

    assert SetBorderSwitchMower.from_reported(bus, enable=enable)._args == expected


@pytest.mark.parametrize("mode", [0, 1, 2])
@pytest.mark.parametrize("enabled", [0, 1])
def test_changing_mode_resends_the_reported_enable(mode: int, enabled: int) -> None:
    bus = Mock()
    _push(bus, {"enable": enabled, "mode": 1})

    assert SetBorderSwitchMower.from_reported(bus, mode=mode)._args == {
        "enable": enabled,
        "mode": mode,
    }


def test_no_write_is_built_before_the_mower_has_reported() -> None:
    bus = Mock()

    assert SetBorderSwitchMower.from_reported(bus, enable=False) is None
    assert SetBorderSwitchMower.from_reported(bus, mode=1) is None


def test_no_write_is_built_while_the_field_to_resend_is_unknown() -> None:
    enable_only, mode_only = Mock(), Mock()
    _push(enable_only, {"enable": 1})
    _push(mode_only, {"mode": 1})

    # Nothing to resend as the mode, so the toggle cannot be written...
    assert SetBorderSwitchMower.from_reported(enable_only, enable=False) is None
    # ...and nothing to resend as the toggle, so the mode cannot either.
    assert SetBorderSwitchMower.from_reported(mode_only, mode=2) is None


def test_building_a_write_does_not_change_what_was_reported() -> None:
    # The base of the next write is the mower's report, never a write of ours
    # that it may not have taken.
    bus = Mock()
    _push(bus, {"enable": 1, "mode": 0})

    SetBorderSwitchMower.from_reported(bus, enable=False)
    SetBorderSwitchMower.from_reported(bus, mode=2)

    assert border_switch_for(bus) == (True, 0)


# --- the profile flags and the wiring ----------------------------------------


def test_the_flags_default_to_off_and_are_independent() -> None:
    assert MowerProfile("x").edge_cutting is False
    assert MowerProfile("x").edge_mode is False
    assert MowerProfile("x", edge_cutting=True).edge_mode is False
    assert MowerProfile("x", edge_mode=True).edge_cutting is False


def test_which_classes_have_which_edge_entity() -> None:
    # If a flag moves, this is the line that says so.
    assert [c for c in SUPPORTED_CLASSES if profile_for_class(c).edge_cutting] == [
        G1_800
    ]
    assert sorted(c for c in SUPPORTED_CLASSES if profile_for_class(c).edge_mode) == [
        O800_RTK_2,
        A1600_RTK,
    ]


def test_the_a1600_lidar_pro_gets_neither() -> None:
    # It reports the setting, but its app does not show one.
    profile = profile_for_class(A1600_LIDAR_PRO)

    assert not profile.edge_cutting
    assert not profile.edge_mode


@pytest.mark.parametrize("class_", sorted(SUPPORTED_CLASSES))
async def test_the_read_is_wired_for_flagged_classes_only(class_: str) -> None:
    await patch_device_info(class_)
    info = await get_static_device_info(class_)
    commands = info.capabilities.get_refresh_commands(MowerBorderSwitchEvent)

    profile = profile_for_class(class_)
    if profile.edge_cutting or profile.edge_mode:
        assert [type(c) for c in commands] == [GetBorderSwitchMower]
    else:
        assert commands == []


@pytest.mark.parametrize("class_", [G1_800, A1600_RTK, O800_RTK_2])
async def test_verify_capabilities_accepts_the_wired_read(class_: str) -> None:
    await patch_device_info(class_)
    info = await get_static_device_info(class_)

    verify_capabilities(info.capabilities, class_)


@pytest.mark.parametrize("flag", ["edge_cutting", "edge_mode"])
async def test_verify_capabilities_fails_when_the_read_is_missing(
    monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    # Patched as a class without either flag, then checked as one with it:
    # the device carries no read for the event, which is what "the patch
    # stopped reaching the device" looks like.
    from dataclasses import replace

    await patch_device_info(A1600_LIDAR_PRO)
    info = await get_static_device_info(A1600_LIDAR_PRO)
    monkeypatch.setitem(
        SUPPORTED_CLASSES,
        A1600_LIDAR_PRO,
        replace(SUPPORTED_CLASSES[A1600_LIDAR_PRO], **{flag: True}),
    )

    with pytest.raises(
        PatchContractError, match=r"instead of \[GetBorderSwitchMower\]"
    ):
        verify_capabilities(info.capabilities, A1600_LIDAR_PRO)
