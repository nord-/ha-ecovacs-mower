"""Shared test setup.

Home Assistant cannot be imported on Windows: ``homeassistant/runner.py`` does an
unguarded ``import fcntl``, which is POSIX-only. The source of truth for test
results is therefore CI on ubuntu-latest, where the whole suite runs.

The guard below is what makes the protocol layer's tests — ``tests/deebot_patch/``,
which only touch ``deebot_client`` — runnable locally on Windows anyway. Without
it, collection of *all* tests crashes on the plugin import.
"""

import sys

import pytest

from custom_components.ecovacs_mower.deebot_patch import (
    families,
    messages,
    state_precedence,
)

# The guard only prevents the explicit load. The plugin also registers itself as a
# pytest11 entry point and is auto-loaded by pytest regardless of this file.
# Locally on Windows the flag is therefore required as well:
#
#     python -m pytest tests/deebot_patch/ -p no:homeassistant -v
#
# The flag does not belong in pytest.ini — CI needs the plugin loaded.
_HA_AVAILABLE = sys.platform != "win32"


@pytest.fixture(autouse=True)
def _reset_deebot_patch_state() -> None:
    """Clear the patch layer's per-device stores before every test.

    They have different key types and lifetimes (``did`` vs. ``EventBus``), so a
    test that reset only one used to leak state into whatever ran next. One
    fixture for the whole suite removes the need for every test module that
    touches any of them to remember to reset it.
    """
    families.reset()
    state_precedence.reset()
    messages.reset_beacon_readings()


if _HA_AVAILABLE:
    pytest_plugins = "pytest_homeassistant_custom_component"

    @pytest.fixture(autouse=True)
    def auto_enable_custom_integrations(enable_custom_integrations):
        """Let HA load custom_components under test."""
        return
