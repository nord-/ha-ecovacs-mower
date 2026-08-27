"""Error code to text, for the codes deebot-client has no wording for.

Moved out of ``sensor.py`` unchanged when the fault latch (issue #53) became a
second reader: the error sensor reports the code the device last sent, the latch
reports the code that is still unresolved, and both want the same text. One
table, two callers.

No Home Assistant import here, which is what lets ``fault.py`` stay HA-free and
have its tests run outside CI.
"""

from __future__ import annotations

import logging

_LOGGER = logging.getLogger(__name__)

# Error codes the mower reports that deebot-client has no text for. Its
# ERROR_CODES table (const.py) predates the mower line entirely, so a GOAT code
# it does not know falls through as a bare number — issue #37.
#
# This table only fills that gap, it never overrides the library: a code that
# means one thing on a vacuum and another on a mower would be settled upstream,
# not silently here. One entry per code, added only from an observed pairing of
# the code with the Ecovacs app's own wording for it.
MOWER_ERROR_CODES = {
    422: "Weak signal. Return to the station.",
    406: "Blade-disc blocked! Blade-disc cannot rotate.",
}

REPORT_URL = "https://github.com/nord-/ha-ecovacs-mower/issues/37"

# The mower re-sends onError for as long as the condition lasts, so warning on
# every event would fill the log with the same line. The codes already asked
# about are remembered for the lifetime of the process; a restart asks again,
# which is what makes the warning reappear for someone who never saw it.
_UNKNOWN_CODES_REPORTED: set[int] = set()


def error_description(code: int, description: str | None = None) -> str | None:
    """The library's text for the code, ours where it has none.

    Returns ``None`` for a code neither table knows, and asks — once per code —
    for the pairing that would let it be added. That request is the whole
    cataloging mechanism: the same convention this integration already uses for
    an unsupported device class.
    """
    if description is not None:
        return description

    if (text := MOWER_ERROR_CODES.get(code)) is not None:
        return text

    if code not in _UNKNOWN_CODES_REPORTED:
        _UNKNOWN_CODES_REPORTED.add(code)
        _LOGGER.warning(
            "No description for error code %s. Please report the code "
            "together with what the Ecovacs app shows for it at %s",
            code,
            REPORT_URL,
        )
    return None
