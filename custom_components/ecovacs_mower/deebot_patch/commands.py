"""Commands adapted for GOAT lawn mowers.

The library maps every GOAT class to ``CleanV2``, which publishes on
``iot/p2p/clean_V2``. The mower's firmware listens on ``iot/p2p/clean`` and
ignores clean_V2 entirely, which yields "No response received for command
clean_V2" and makes start and pause do nothing.

``CleanMower`` inherits ``Clean`` (topic ``clean``) but sends a V2-formatted
payload, which is what Ecovacs' own app does.

Corresponds to DeebotUniverse/client.py PR #1624, without its caching of the
active clean type — that is only needed for customArea, which is out of scope.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from deebot_client.commands.json.clean import Clean
from deebot_client.models import CleanMode

if TYPE_CHECKING:
    from deebot_client.models import CleanAction


class CleanMower(Clean):
    """Mow command: the ``clean`` topic with a V2 payload."""

    def _get_args(self, action: CleanAction) -> dict[str, Any]:
        return {"act": action.value, "content": {"type": CleanMode.AUTO.value}}
