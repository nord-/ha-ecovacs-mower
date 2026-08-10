"""The state map and the control commands.

The module under test imports Home Assistant, which cannot be imported on Windows
(``fcntl``). The imports therefore live inside the test functions and the whole
file is marked ``requires_ha`` — otherwise collection itself crashes before any
skip marker gets a chance to apply. The source of truth is CI on ubuntu-latest.
"""

import pytest

from . import requires_ha

pytestmark = requires_ha


@pytest.mark.parametrize(
    ("state_name", "expected_name"),
    [
        ("CLEANING", "MOWING"),
        ("PAUSED", "PAUSED"),
        ("RETURNING", "RETURNING"),
        ("DOCKED", "DOCKED"),
        ("ERROR", "ERROR"),
    ],
)
def test_state_mapping(state_name: str, expected_name: str) -> None:
    from deebot_client.models import State
    from homeassistant.components.lawn_mower import LawnMowerActivity

    from custom_components.ecovacs_mower.lawn_mower import _STATE_TO_MOWER_STATE

    state = getattr(State, state_name)
    expected = getattr(LawnMowerActivity, expected_name)
    assert _STATE_TO_MOWER_STATE[state] == expected


def test_idle_maps_to_paused_not_docked() -> None:
    # A mower standing still in the middle of the lawn is paused, not docked.
    from deebot_client.models import State
    from homeassistant.components.lawn_mower import LawnMowerActivity

    from custom_components.ecovacs_mower.lawn_mower import _STATE_TO_MOWER_STATE

    assert _STATE_TO_MOWER_STATE[State.IDLE] == LawnMowerActivity.PAUSED


def test_every_state_is_mapped() -> None:
    # An unhandled state raises KeyError in the callback and silently breaks the
    # entity.
    from deebot_client.models import State

    from custom_components.ecovacs_mower.lawn_mower import _STATE_TO_MOWER_STATE

    assert set(_STATE_TO_MOWER_STATE) == set(State)


def test_supported_features() -> None:
    # LawnMowerEntity uses HA's CachedProperties metaclass for
    # "supported_features", which rewrites the class attribute
    # ``_attr_supported_features`` into a property. Read on the class (without an
    # instance) that yields the property object itself, not the flag value — which
    # is why it is read via an instance, exactly as HA does at runtime.
    # ``__new__`` bypasses ``__init__`` (which requires a real ``Device``) since
    # the descriptor does not depend on it having run.
    from homeassistant.components.lawn_mower import LawnMowerEntityFeature

    from custom_components.ecovacs_mower.lawn_mower import EcovacsMower

    instance = EcovacsMower.__new__(EcovacsMower)
    assert instance._attr_supported_features == (
        LawnMowerEntityFeature.DOCK
        | LawnMowerEntityFeature.PAUSE
        | LawnMowerEntityFeature.START_MOWING
    )
