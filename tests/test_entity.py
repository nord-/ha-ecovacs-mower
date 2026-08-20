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

    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert "ecovacs_mower" in record.name
    assert GetChargeState.NAME in record.getMessage()


async def test_confirmed_command_logs_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from deebot_client.commands.json.charge_state import GetChargeState

    with caplog.at_level(logging.WARNING):
        await _entity({"ret": "ok"})._execute_command(GetChargeState())

    assert not caplog.records
