"""The shared command wrapper on the entity base class.

Imports Home Assistant, so the imports live inside the tests and the file is
marked ``requires_ha``. The source of truth is CI on ubuntu-latest.
"""

import logging

import pytest

from tests import requires_ha

pytestmark = requires_ha


def _entity(response: dict) -> object:
    """An entity whose device answers *response* to any command."""
    from unittest.mock import AsyncMock, MagicMock

    from homeassistant.helpers.entity import EntityDescription

    from custom_components.ecovacs_mower.entity import EcovacsEntity

    class _Entity(EcovacsEntity[object]):
        entity_description = EntityDescription(key="test")

    device = MagicMock()
    device.device_info = {"did": "did"}
    device.execute_command = AsyncMock(return_value=response)
    return _Entity(device, object())


async def test_unconfirmed_command_is_logged_under_this_integration(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The point of the wrapper: a line the integration page's log filter finds.

    That filter is the literal domain string, so what matters is not only that
    something is logged but that the logger name carries ``ecovacs_mower``.
    """
    from deebot_client.commands.json.charge_state import GetChargeState

    with caplog.at_level(logging.WARNING):
        await _entity({})._execute_command(GetChargeState())

    # Filtered so an unrelated WARNING+ from hass or deebot_client during the
    # call can't fail this for a reason that has nothing to do with the wrapper.
    records = [r for r in caplog.records if r.name.startswith("custom_components.ecovacs_mower")]
    assert len(records) == 1
    record = records[0]
    assert "ecovacs_mower" in record.name
    assert GetChargeState.NAME in record.getMessage()


async def test_confirmed_command_logs_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from deebot_client.commands.json.charge_state import GetChargeState

    with caplog.at_level(logging.WARNING):
        await _entity({"ret": "ok"})._execute_command(GetChargeState())

    assert not caplog.records


async def test_an_unconfirmed_command_names_the_family(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A user asked to send logs must not be shown the wrong command name: the
    wrapper's NAME stays "clean" even when clean_V2 is what went out.

    Uses ``CleanMower`` rather than ``GetChargeState``: since entity.py only
    names the family for a command the family switch actually applies to (see
    ``test_the_family_is_not_named_for_a_command_without_one`` below),
    exercising the "family gets named" path needs one of the two commands
    that have one.
    """
    from deebot_client.models import CleanAction

    from custom_components.ecovacs_mower.deebot_patch.commands import CleanMower
    from custom_components.ecovacs_mower.deebot_patch.families import (
        Family,
        commit,
        reset,
    )

    commit("did", Family.V2)
    try:
        with caplog.at_level(logging.WARNING):
            await _entity({})._execute_command(CleanMower(CleanAction.START))
    finally:
        reset()

    # `commit()` above logs its own INFO record, ahead of the `at_level`
    # block that only takes effect once entered — so without the level check
    # here, that unrelated record (captured because pytest_homeassistant sets
    # the root logger to DEBUG under `-v`, as CI runs it) would double this
    # list and fail the test for a reason that has nothing to do with the
    # wrapper.
    records = [
        r
        for r in caplog.records
        if r.name.startswith("custom_components.ecovacs_mower")
        and r.levelno >= logging.WARNING
    ]
    assert len(records) == 1
    message = records[0].getMessage()
    assert CleanMower.NAME in message
    assert str(Family.V2) in message


async def test_an_unconfirmed_command_names_both_families_on_a_double_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A double failure sends both delegates but commits neither (issue #42):
    the warning must not understate that by naming only the family that was
    current before the attempt.
    """
    from deebot_client.models import CleanAction

    from custom_components.ecovacs_mower.deebot_patch.commands import CleanMower
    from custom_components.ecovacs_mower.deebot_patch.families import (
        Family,
        note_attempt,
        reset,
    )

    note_attempt("did", Family.NON_V2, Family.V2)
    try:
        with caplog.at_level(logging.WARNING):
            await _entity({})._execute_command(CleanMower(CleanAction.START))
    finally:
        reset()

    records = [
        r for r in caplog.records if r.name.startswith("custom_components.ecovacs_mower")
    ]
    assert len(records) == 1
    assert "non-V2 and V2" in records[0].getMessage()


async def test_the_family_is_not_named_for_a_command_without_one(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Charge has no V2/non-V2 dialect; naming a family for it would claim one.

    Uses ``GetChargeState`` as the stand-in for "a command without a family":
    it is a plain library command, not one of the two ``_AdaptiveFamily``
    wrappers.
    """
    from deebot_client.commands.json.charge_state import GetChargeState

    with caplog.at_level(logging.WARNING):
        await _entity({})._execute_command(GetChargeState())

    records = [r for r in caplog.records if r.name.startswith("custom_components.ecovacs_mower")]
    assert len(records) == 1
    assert "family" not in records[0].getMessage()
