"""Tests for the GOAT border-mowing command (issue #12)."""

from unittest.mock import AsyncMock, patch

import pytest
from deebot_client.command import Command
from deebot_client.commands.json.clean import Clean, CleanV2

from custom_components.ecovacs_mower.deebot_patch.border import (
    MowBorder,
    _BorderCleanNonV2,
    _BorderCleanV2,
)
from custom_components.ecovacs_mower.deebot_patch.families import Family, selected
from custom_components.ecovacs_mower.deebot_patch.state_precedence import register

from .test_commands import _DEVICE_INFO, _NO_ANSWER, _OK, _bus, _transport

# The request the Ecovacs app sent on a GOAT G1-800 (77atlz, fw 1.36.208),
# captured on issue #12. The map id is the one from that capture.
_CAPTURED_MAP_ID = "2049987783"
_CAPTURED_ARGS = {
    "act": "start",
    "content": {"type": "border", "value": "mid:2049987783"},
}


def test_border_delegates_are_clean_commands() -> None:
    assert issubclass(_BorderCleanNonV2, Clean)
    assert issubclass(_BorderCleanV2, CleanV2)


def test_v2_border_payload_is_the_captured_request() -> None:
    command = _BorderCleanV2(_CAPTURED_MAP_ID)
    assert command.NAME == "clean_V2"
    assert command._args == _CAPTURED_ARGS


def test_non_v2_border_payload_has_the_same_nested_shape() -> None:
    # Unconfirmed on the wire: no non-V2 mower has been captured sending a
    # border job. This is the shape spotArea uses on the non-V2 family, and
    # the class gate keeps it off that hardware until someone confirms it.
    command = _BorderCleanNonV2(_CAPTURED_MAP_ID)
    assert command.NAME == "clean"
    assert command._args == _CAPTURED_ARGS


def test_border_delegates_bypass_the_action_rewrite() -> None:
    # Clean._execute would turn this start into a resume while the mower reads
    # paused; the bypass only holds with _NoActionRewrite ahead of Clean.
    from custom_components.ecovacs_mower.deebot_patch.commands import _NoActionRewrite

    for delegate, topic_base in ((_BorderCleanNonV2, Clean), (_BorderCleanV2, CleanV2)):
        mro = delegate.__mro__
        assert mro.index(_NoActionRewrite) < mro.index(topic_base)


@pytest.mark.parametrize("map_id", ["", "0"])
def test_mow_border_refuses_a_map_id_that_is_not_a_map(map_id: str) -> None:
    with pytest.raises(ValueError, match="map"):
        MowBorder(map_id)


def test_mow_border_equality_includes_the_map_id() -> None:
    assert MowBorder("1") == MowBorder("1")
    assert MowBorder("1") != MowBorder("2")


def test_mow_border_keeps_clean_contract() -> None:
    assert issubclass(MowBorder, Clean)
    assert MowBorder.NAME == "clean"


async def test_mow_border_executes_first_on_non_v2() -> None:
    fake, sent = _transport(_OK)
    command = MowBorder(_CAPTURED_MAP_ID)

    with patch.object(Command, "_execute", fake):
        await command._execute(AsyncMock(), _DEVICE_INFO, AsyncMock())

    assert sent == ["clean"]
    assert command._delegate(Family.NON_V2)._args == _CAPTURED_ARGS
    assert selected(_DEVICE_INFO["did"]) is Family.NON_V2


async def test_mow_border_writes_border_into_the_record_before_the_first_push() -> None:
    # Closes the window between pressing start and onCleanInfo arriving: a
    # quick pause in between must not echo a stale type from a prior job.
    bus = _bus()
    record = register(bus)
    record.note_job({"type": "auto"})
    fake, _sent = _transport(_OK)
    command = MowBorder(_CAPTURED_MAP_ID)

    with patch.object(Command, "_execute", fake):
        await command._execute(AsyncMock(), _DEVICE_INFO, bus)

    assert record.job_type == "border"


async def test_mow_border_falls_back_to_v2_and_commits_family() -> None:
    fake, sent = _transport(_NO_ANSWER, _OK)
    command = MowBorder(_CAPTURED_MAP_ID)

    with patch.object(Command, "_execute", fake):
        await command._execute(AsyncMock(), _DEVICE_INFO, AsyncMock())

    assert sent == ["clean", "clean_V2"]
    assert command._delegate(Family.V2)._args == _CAPTURED_ARGS
    assert selected(_DEVICE_INFO["did"]) is Family.V2
