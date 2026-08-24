"""Commands adapted for GOAT lawn mowers.

The library maps every GOAT class to ``CleanV2``, which publishes on
``iot/p2p/clean_V2``. The mower's firmware listens on ``iot/p2p/clean`` and
ignores clean_V2 entirely, which yields "No response received for command
clean_V2" and makes start and pause do nothing.

``CleanMower`` inherits ``Clean`` (topic ``clean``) but sends a V2-formatted
payload, which is what Ecovacs' own app does.

Corresponds to DeebotUniverse/client.py PR #1624, without its caching of the
active clean type — that is only needed for customArea, which is out of scope.

``GetProtectState`` is not a fix for a broken command but a command the library
does not have at all: the mower pushes ``onProtectState`` when a protection flag
flips, and nothing had ever asked for the current value (issue #31).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from deebot_client.commands.json.clean import Clean
from deebot_client.commands.json.common import JsonCommandWithMessageHandling
from deebot_client.models import CleanMode

from .messages import OnProtectState

if TYPE_CHECKING:
    from deebot_client.models import CleanAction


class CleanMower(Clean):
    """Mow command: the ``clean`` topic with a V2 payload."""

    def _get_args(self, action: CleanAction) -> dict[str, Any]:
        return {"act": action.value, "content": {"type": CleanMode.AUTO.value}}


class GetProtectState(JsonCommandWithMessageHandling, OnProtectState):
    """Ask for the protection flags instead of waiting for a push.

    The device sends ``onProtectState`` when a flag flips and never otherwise,
    so before this the five ``binary_sensor`` entities read "unknown" from
    startup until the weather or the mower next changed something — which
    through a dry, uneventful spell is a very long time (issue #31).

    ``OnProtectState`` is inherited for its handler: the answer to
    ``getProtectState`` carries the same payload as the push, so both entry
    points must parse it the same way, and a copy would drift the day the
    payload gains a flag. Only ``NAME`` differs, which is also why the pair
    cannot be one class — the message registry and the command topic are keyed
    on that one string.

    Evidence that the command exists on the wire, since the library has no
    definition to copy: Ecovacs' own app sends it, and
    ``Janverhu/ecovacs-goat-g1`` requests it at startup against a GOAT G1 and
    parses the answer as an ``onProtectState`` payload. It takes no arguments.
    """

    NAME = "getProtectState"
