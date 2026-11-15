"""Border mowing (issue #12) and trimming (issue #138) for GOAT mowers.

Border mowing is a task type of its own, not the ``Edge`` setting the
``border_switch`` entity toggles: that setting decides whether an ordinary job
also trims the perimeter, while this command starts a job that does nothing
else. The request shape was captured from the Ecovacs app on a GOAT G1-800
(``77atlz``, firmware 1.36.208)::

    clean_V2  {"act": "start", "content": {"type": "border", "value": "mid:<mid>"}}

``mid`` is the id of the map in use, which every map message names in its
envelope — see ``state_precedence.note_map``. The mower stores everything
else about the job, so the command carries nothing but that id.

The non-V2 delegate sends the same nested payload on ``clean``, which is what
the confirmed ``spotArea`` command does on that family. That shape was never
captured from the app, but a border job started this way from Home Assistant
ran to completion on an A3000 LiDAR Pro (``51rcxt``, firmware 1.13.10, issue
#135). The ``border_mowing`` flag on ``hardware.MowerProfile`` keeps the
button off every class where neither shape has been confirmed.
"""

from __future__ import annotations

from typing import Any

from deebot_client.command import Command
from deebot_client.commands.json.clean import Clean, CleanV2
from deebot_client.models import CleanAction

from .commands import _TaskClean, _TaskStart
from .families import Family


_TYPE_BORDER = "border"


class _BorderClean(_TaskClean):
    """The border payload, before a topic is chosen."""

    def __init__(self, map_id: str) -> None:
        super().__init__(_TYPE_BORDER, f"mid:{map_id}")


class _BorderCleanNonV2(_BorderClean, Clean):
    """Send the border payload on the ``clean`` topic."""


class _BorderCleanV2(_BorderClean, CleanV2):
    """Send the border payload on the ``clean_V2`` topic."""


class MowBorder(_TaskStart):
    """Start a border job, on whichever clean command the mower answers."""

    _JOB_TYPE = _TYPE_BORDER

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

    def _make_delegates(self) -> dict[Family, Command]:
        return {
            Family.NON_V2: _BorderCleanNonV2(self._map_id),
            Family.V2: _BorderCleanV2(self._map_id),
        }


# The trimming job (issue #138): the edge cut with the string trimmer, which
# the app calls Trimming. Captured from the app on an A3000 LiDAR Pro
# (``51rcxt``) and an O1200 LiDAR Pro (``2i0fns``), always on ``clean``::
#
#     clean  {"act": "start", "content": {"type": "borderrotate", "value": "reid:1"}}
#
# ``reid`` read ``reid:1`` on every capture. It is not the map id — the
# O1200's map has a different nine-digit one — and the app offers no choice of
# area for a trim, so it is sent as a constant. Stop, pause and resume reuse
# the job type the mower last reported and need nothing here.
_TYPE_TRIM = "borderrotate"
_TRIM_VALUE = "reid:1"


class _TrimClean(_TaskClean):
    """The trimming payload, before a topic is chosen."""

    def __init__(self) -> None:
        super().__init__(_TYPE_TRIM, _TRIM_VALUE)


class _TrimCleanNonV2(_TrimClean, Clean):
    """Send the trimming payload on the ``clean`` topic."""


class _TrimCleanV2(_TrimClean, CleanV2):
    """Send the trimming payload on the ``clean_V2`` topic."""


class MowTrim(_TaskStart):
    """Start a trimming job, on whichever clean command the mower answers."""

    _JOB_TYPE = _TYPE_TRIM

    def __init__(self) -> None:
        """Build a trimming start; it takes no argument."""
        self._delegates: dict[Family, Command] = {}
        super().__init__(CleanAction.START)

    def _get_args(self, action: CleanAction) -> dict[str, Any]:
        # Inert as a wire payload, like MowBorder's: the delegates build their
        # own. The job type keeps repr() apart from the other task starts.
        return {"act": action.value, "type": _TYPE_TRIM}

    def _make_delegates(self) -> dict[Family, Command]:
        return {
            Family.NON_V2: _TrimCleanNonV2(),
            Family.V2: _TrimCleanV2(),
        }
