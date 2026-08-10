"""Test package."""

import sys

import pytest

requires_ha = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Home Assistant cannot be imported on Windows (fcntl). Runs in CI.",
)
