"""Commands adapted for GOAT lawn mowers.

The library maps every GOAT class to ``CleanV2``, which publishes on
``iot/p2p/clean_V2``. The mower's firmware listens on ``iot/p2p/clean`` and
ignores clean_V2 entirely, which yields "No response received for command
clean_V2" and makes start and pause do nothing.

``CleanMower`` inherits ``Clean`` (topic ``clean``) but sends a V2-formatted
payload, which is what Ecovacs' own app does.

Corresponds to DeebotUniverse/client.py PR #1624, without its caching of the
active clean type — that is only needed for customArea, which is out of scope.

``GetCleanInfoMower`` fixes an answer rather than a request: ``getCleanInfo`` is
sent and answered, and the answer is a constant ``idle`` whatever the mower is
doing (issue #48).

``GetProtectState`` is not a fix for a broken command but a command the library
does not have at all: the mower pushes ``onProtectState`` when a protection flag
flips, and nothing had ever asked for the current value (issue #31).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from deebot_client.commands.json.clean import Clean, GetCleanInfo
from deebot_client.commands.json.common import JsonCommandWithMessageHandling
from deebot_client.message import HandlingResult
from deebot_client.models import CleanMode

from .messages import OnProtectState

if TYPE_CHECKING:
    from deebot_client.event_bus import EventBus
    from deebot_client.models import CleanAction


class CleanMower(Clean):
    """Mow command: the ``clean`` topic with a V2 payload."""

    def _get_args(self, action: CleanAction) -> dict[str, Any]:
        return {"act": action.value, "content": {"type": CleanMode.AUTO.value}}


class GetCleanInfoMower(GetCleanInfo):
    """``getCleanInfo`` with the answer that is always the same left out.

    GOAT firmware answers this command with ``{"state": "idle"}`` whatever the
    mower is doing. Not occasionally: every one of the 74 polls during a run on
    2026-08-24 answered ``idle``, from 09:02 to 15:07, while the mower was
    cutting and its own ``mowedArea`` counter climbed from 0 to 303 m². Across 38
    hours of logging there is no answer with any other value.

    Upstream maps ``idle`` to ``State.IDLE``, so the state capability's
    five-minute refresh replaced the state that had arrived by push with a state
    the device sends unconditionally: the entities read "mowing" for a minute or
    two after a job started and "paused" for the hours of cutting that followed
    (issue #48).

    Dropping that one branch is the whole fix. The command is still worth
    sending for the states it *can* report — ``clean`` with a motion state, and
    ``goCharging`` — and removing it from the state commands altogether would
    leave the state unknown after a restart until the device happened to push.

    This deliberately does not change what an ``idle`` *push* means.
    ``onCleanInfo`` resolves to the library's own ``GetCleanInfo`` through
    ``get_legacy_message()``, which this class does not touch, and there an
    ``idle`` is a real event: the device chose to send it, at the moment
    something stopped. It is the polled answer that carries no information.
    """

    @classmethod
    def _handle_body_data_dict(
        cls, event_bus: EventBus, data: dict[str, Any]
    ) -> HandlingResult:
        """Handle message->body->data, minus the state that says nothing.

        ``success()`` rather than ``analyse()``: the payload parsed fine and
        there is simply nothing to publish. ``analyse()`` would log "Could not
        handle getCleanInfo" every five minutes for an answer this class
        understands perfectly. ``OnPos`` and ``OnMI`` make the same distinction.

        The error path is kept ahead of the drop. ``trigger: "alert"`` means the
        mower is in an error state whatever the state field says, and that is not
        something to swallow because ``idle`` came along with it.
        """
        if data.get("trigger") != "alert" and data.get("state") == "idle":
            return HandlingResult.success()
        return super()._handle_body_data_dict(event_bus, data)


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
