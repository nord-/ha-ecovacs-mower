"""Commands adapted for GOAT lawn mowers.

The library maps every GOAT class to ``CleanV2``, which publishes on
``iot/p2p/clean_V2``. The mower's firmware listens on ``iot/p2p/clean`` and
ignores clean_V2 entirely, which yields "No response received for command
clean_V2" and makes start and pause do nothing.

``CleanMower`` inherits ``Clean`` (topic ``clean``) but sends a V2-formatted
payload, which is what Ecovacs' own app does.

Corresponds to DeebotUniverse/client.py PR #1624, without its caching of the
active clean type — that is only needed for customArea, which is out of scope.

``GetProtectState`` is not a fix for anything upstream got wrong; the library
simply has no command for the mower's protection flags. It exists so the rain
flag has a value before the device next changes it — see below.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from deebot_client.commands.json.clean import Clean
from deebot_client.commands.json.common import JsonCommandWithMessageHandling
from deebot_client.message import MessageBodyDataDict
from deebot_client.models import CleanMode

from .messages import handle_protect_state

if TYPE_CHECKING:
    from deebot_client.event_bus import EventBus
    from deebot_client.message import HandlingResult
    from deebot_client.models import CleanAction


class CleanMower(Clean):
    """Mow command: the ``clean`` topic with a V2 payload."""

    def _get_args(self, action: CleanAction) -> dict[str, Any]:
        return {"act": action.value, "content": {"type": CleanMode.AUTO.value}}


class GetProtectState(JsonCommandWithMessageHandling, MessageBodyDataDict):
    """Ask for the protection flags, rain included.

    onProtectState only arrives when a flag flips, so after a Home Assistant
    restart the rain flag would otherwise stay unknown until the next change of
    weather — which is precisely when it is being looked at. The command name
    follows Ecovacs' on<X>/get<X> convention and the observed onProtectState body
    carries the ``code``/``msg`` envelope of a command reply, so it is very
    likely the same handler on the device. It has not been confirmed against
    hardware: if the firmware does not know the command, the library logs that it
    got no response and the flag stays unknown until the mower reports one.
    """

    NAME = "getProtectState"

    @classmethod
    def _handle_body_data_dict(
        cls, event_bus: EventBus, data: dict[str, Any]
    ) -> HandlingResult:
        """Handle message->body->data."""
        return handle_protect_state(event_bus, data)
