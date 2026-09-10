"""Border mowing for GOAT mowers (issue #12).

Border mowing is a task type of its own, not the ``Edge`` setting the
``border_switch`` entity toggles: that setting decides whether an ordinary job
also trims the perimeter, while this command starts a job that does nothing
else. The request shape was captured from the Ecovacs app on a GOAT G1-800
(``77atlz``, firmware 1.36.208)::

    clean_V2  {"act": "start", "content": {"type": "border", "value": "mid:<mid>"}}

``mid`` is the id of the map in use, which every map message names in its
envelope — see ``state_precedence.note_map``. The mower stores everything
else about the job, so the command carries nothing but that id.

Only the ``clean_V2`` shape is confirmed. The non-V2 delegate sends the same
nested payload on ``clean``, which is what the confirmed ``spotArea`` command
does on that family; ``hardware.BORDER_CLASSES`` keeps the button off the
non-V2 hardware until someone confirms it there.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from deebot_client.command import Command
from deebot_client.commands.json.clean import Clean, CleanV2
from deebot_client.message import HandlingResult
from deebot_client.models import CleanAction

from .commands import _AdaptiveFamily, _TaskClean
from .families import Family
from .state_precedence import record_for

if TYPE_CHECKING:
    from deebot_client.authentication import Authenticator
    from deebot_client.event_bus import EventBus
    from deebot_client.models import ApiDeviceInfo


_TYPE_BORDER = "border"


class _BorderClean(_TaskClean):
    """The border payload, before a topic is chosen."""

    def __init__(self, map_id: str) -> None:
        super().__init__(_TYPE_BORDER, f"mid:{map_id}")


class _BorderCleanNonV2(_BorderClean, Clean):
    """Send the border payload on the ``clean`` topic."""


class _BorderCleanV2(_BorderClean, CleanV2):
    """Send the border payload on the ``clean_V2`` topic."""


class MowBorder(_AdaptiveFamily, Clean):
    """Start a border job, on whichever clean command the mower answers.

    Still a ``Clean`` subclass for the reasons ``CleanMower`` gives:
    ``ExecuteCommand`` supplies the concrete ``_handle_body``, and the patch
    layer promises that its mow commands are ``Clean`` commands.
    """

    def __init__(self, map_id: str) -> None:
        """Build a border start for the map *map_id*."""
        if not map_id or map_id == "0":
            # The entity never lets this through; this keeps the patch layer
            # itself from putting "mid:0" on the wire.
            raise ValueError("A border job needs the id of the map in use")
        self._map_id = map_id
        self._delegates: dict[Family, Command] = {}
        super().__init__(CleanAction.START)

    def _get_args(self, action: CleanAction) -> dict[str, Any]:
        # Inert as a wire payload: the delegates build their own, and _execute
        # is fully overridden. Keyed on the map id so Command.__eq__ and repr()
        # tell two instances apart — see CleanMower._get_args for the pattern.
        return {"act": action.value, "mid": self._map_id}

    def _delegate(self, family: Family) -> Command:
        """Return the command for the selected wire family."""
        return self._delegates[family]

    async def _execute(
        self,
        authenticator: Authenticator,
        device_info: ApiDeviceInfo,
        event_bus: EventBus,
    ) -> tuple[HandlingResult, dict[str, Any]]:
        """Build the two wire variants and let the adaptive family choose."""
        # Written here, not just sent: closes the window between issuing a
        # start and the first onCleanInfo push, where a quick pause would
        # otherwise echo whatever the previous job left behind (issue #94).
        if (record := record_for(event_bus)) is not None:
            record.job_type = _TYPE_BORDER
        self._delegates = {
            Family.NON_V2: _BorderCleanNonV2(self._map_id),
            Family.V2: _BorderCleanV2(self._map_id),
        }
        return await super()._execute(authenticator, device_info, event_bus)
