"""Tests for get_client_device_id — the load-bearing property of the 1013 fix.

Ecovacs requires email verification of the client's device ID (error code 1013)
unless the same ID is reused on every login. If a new ID were generated every
time, the user would end up in an endless verification loop without understanding
why. These tests prove directly that an already known ID in the configuration
always wins over generating a new one, and that a new ID is only generated when
none exists from before.

``util.py`` only imports ``homeassistant.core``/``const``/``util``, not
``homeassistant.runner`` (which does the unguarded ``import fcntl`` that otherwise
crashes collection on Windows). The module can therefore be imported directly
here without the pytest-homeassistant-custom-component plugin, and the tests need
no ``hass`` fixture — they can actually run locally, not just be collected as
skipped.
"""

import string

from homeassistant.const import CONF_DEVICE_ID

from custom_components.ecovacs_mower.util import get_client_device_id

_DEVICE_ID_ALPHABET = set(string.ascii_uppercase + string.digits)


def test_existing_device_id_is_reused() -> None:
    """The core of the 1013 fix: a known ID is reused, never regenerated."""
    config = {CONF_DEVICE_ID: "ALREADY-VERIFIED-ID"}

    assert get_client_device_id(None, False, config) == "ALREADY-VERIFIED-ID"
    # Holds regardless of installation mode — a reauth against a self-hosted entry
    # must not trigger a new verification round either.
    assert get_client_device_id(None, True, config) == "ALREADY-VERIFIED-ID"


def test_missing_device_id_is_generated() -> None:
    """Without a previous ID, a new random ID is generated."""
    device_id = get_client_device_id(None, False, {})

    assert device_id
    assert len(device_id) == 8
    assert set(device_id) <= _DEVICE_ID_ALPHABET


def test_supported_lifespans_are_the_four_a_mower_has() -> None:
    """Only the components 2i0fns actually declares.

    Core exposes 12 of the ``LifeSpan`` enum's 26 members, all vacuum-oriented.
    BLADE and LENS_BRUSH are on that list; TRIMMER_BRUSH and WEED_ROPE are not
    there at all — they are mower-specific components core never exposes.
    """
    from deebot_client.events import LifeSpan

    from custom_components.ecovacs_mower.const import SUPPORTED_LIFESPANS

    assert set(SUPPORTED_LIFESPANS) == {
        LifeSpan.BLADE,
        LifeSpan.LENS_BRUSH,
        LifeSpan.TRIMMER_BRUSH,
        LifeSpan.WEED_ROPE,
    }


def test_supported_lifespans_match_the_target_device() -> None:
    """Our list must not contain anything the device does not have."""
    import asyncio

    from deebot_client.hardware import _DEVICES, get_static_device_info

    from custom_components.ecovacs_mower.const import SUPPORTED_LIFESPANS

    # get_static_device_info seeds the global cache. The repo convention is to
    # leave it as we found it — see tests/deebot_patch/test_hardware.py.
    try:
        info = asyncio.run(get_static_device_info("2i0fns"))
        assert info is not None
        assert set(SUPPORTED_LIFESPANS) <= set(info.capabilities.life_span.types)
    finally:
        _DEVICES.pop("2i0fns", None)
