"""``getInfo``: several getters in one request (issue #85).

The Ecovacs app never asks a mower for its charge state or its stats one at a
time. It sends ``getInfo`` with a list of getter names and gets one answer
back, carrying each getter's own reply under its name. ``devmanager.do``, the
HTTP path every command of the library goes out on, accepts the same request,
so a refresh that needs three answers can be one round trip instead of three.

What was measured on a G1-800 (``77atlz``, firmware 1.36.208), and what
``GetInfo`` is built around:

* **The list is the caller's.** Getters the app never batches are answered
  too, in any mix.
* **A name the mower does not know is left out of the answer.** No key and no
  error, while the rest of the batch is answered normally.
* **A getter that fails carries its own non-zero ``code``**, and the rest of
  the batch is unaffected.
* **An answer that would be too large comes back empty.** The cap is on the
  size of the whole reply, somewhere between 1069 and 1081 bytes, not on the
  number of getters: fourteen small ones are answered in one request and five
  are refused when four of them are large. The refusal says nothing: ``ret``
  is ``ok``, the body's ``code`` is 0, and ``data`` is an empty object.

So a missing key means "not answered", whatever the reason, and a reply with
every key missing is read as "too large": the batch is split in half and each
half is asked again. That needs no sizes in advance, which matters because
they are not constant: the beacon list alone grows with every beacon. A
getter still unanswered when it is alone in its batch, or left out of an
answer that carries others, is sent as the plain command it is.

No other class has been checked, so a class gets this only through the
``get_info`` flag on its ``MowerProfile``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from deebot_client.command import CommandWithMessageHandling
from deebot_client.commands.json.common import (
    JsonCommand,
    JsonCommandWithMessageHandling,
)
from deebot_client.exceptions import ApiTimeoutError
from deebot_client.message import HandlingResult, HandlingState

from .commands import (
    GetChargeStateMower,
    GetCleanInfoMower,
    GetStatsMower,
    _AdaptiveFamily,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from deebot_client.authentication import Authenticator
    from deebot_client.event_bus import EventBus
    from deebot_client.models import ApiDeviceInfo

_LOGGER = logging.getLogger(__name__)

_FAILED = HandlingResult(HandlingState.FAILED)


class _InfoRequest(JsonCommand):
    """The request itself: ``getInfo`` with a list of getter names.

    Only ever asked for its payload and sent with ``_execute_api_request``.
    ``GetInfo`` reads the answer, so nothing is routed through this class's
    own ``_handle_response``.
    """

    NAME = "getInfo"

    def __init__(self, names: list[str]) -> None:
        """Initialize the request with the getters to name."""
        super().__init__(names)

    def _handle_response(
        self, event_bus: EventBus, response: dict[str, Any]
    ) -> HandlingResult:
        # Unreachable, and concrete because Command declares it abstract.
        return HandlingResult.analyse()


class _Member:
    """One getter of a batch: the command it stands for and what goes out.

    The two differ for a command whose name depends on the mower's dialect
    (issue #42). The batch names the delegate of the family in use, and that
    delegate parses the reply; the wrapper is what is sent when the batch
    left the getter unanswered, so that it can still learn the other family.
    """

    def __init__(self, command: CommandWithMessageHandling, did: str) -> None:
        self.command = command
        self.wire: CommandWithMessageHandling = (
            command._delegate(command._first_family(did))  # type: ignore[assignment]
            if isinstance(command, _AdaptiveFamily)
            else command
        )

    @property
    def name(self) -> str:
        """The getter's name on the wire."""
        return self.wire.NAME


class GetInfo(JsonCommandWithMessageHandling):
    """Several getters asked for in one ``getInfo``, each parsed by itself.

    The commands are the ones a refresh would send separately, without
    arguments: a batch names getters and has no place for anything else.
    Each reply is handed to its command's own handler, in the order the
    commands were given, so whatever a handler records or publishes it still
    does, and a handler that relies on another having run first can say so by
    its place in the list. The state poll does: the charge state is read
    before the clean info is interpreted (issue #67).

    A getter the batch did not answer is asked alone, as the command it is,
    at its place in that same order. That covers a name this firmware does
    not know in a batch, a getter whose reply carries a failure code, and
    the clean-info getter on a mower whose dialect has not been learned yet,
    which only the plain command can find out.

    Nothing is retried when a request itself gets no answer: an offline
    mower or a timeout would only be waited out again, once per getter. The
    replies that had arrived by then are still handled.

    The device counts as reached when at least one getter was answered.
    """

    NAME = "getInfo"

    def __init__(self, commands: Sequence[CommandWithMessageHandling]) -> None:
        """Initialize the batch with the getters to ask for, in order."""
        super().__init__()
        self._commands = tuple(commands)

    def __repr__(self) -> str:
        """Name the getters: ``_args`` is empty and would say nothing."""
        return f"<{self.__class__.__name__} commands={list(self._commands)}>"

    def __eq__(self, obj: object) -> bool:
        """Two batches are equal when they ask for the same getters."""
        return isinstance(obj, GetInfo) and self._commands == obj._commands

    def __hash__(self) -> int:
        """Hash by the getters' names: a command with arguments has no hash."""
        return hash((self.NAME, *(command.NAME for command in self._commands)))

    @classmethod
    def _handle_body(cls, event_bus: EventBus, body: dict[str, Any]) -> HandlingResult:
        # Unreachable, and concrete because MessageBody declares it abstract:
        # the replies are handed to the getters' own handlers in _execute.
        return HandlingResult.analyse()

    async def _execute(
        self,
        authenticator: Authenticator,
        device_info: ApiDeviceInfo,
        event_bus: EventBus,
    ) -> tuple[HandlingResult, dict[str, Any]]:
        """Ask for every getter, splitting a batch that comes back empty."""
        members = [
            _Member(command, device_info["did"]) for command in self._commands
        ]
        if not members:
            return _FAILED, {}

        replies: dict[str, dict[str, Any]] = {}
        header: dict[str, Any] = {}
        last_response: dict[str, Any] = {}
        # Set once a request got no answer: what it came back with, and the
        # end of asking, in batches or alone.
        unanswered: tuple[HandlingResult, dict[str, Any]] | None = None
        pending = [members]
        while pending:
            batch = pending.pop(0)
            try:
                response = await _InfoRequest(
                    [member.name for member in batch]
                )._execute_api_request(authenticator, device_info)
            except ApiTimeoutError:
                _LOGGER.warning(
                    "Could not execute command %s for %s: Timeout reached",
                    self.NAME,
                    device_info["class"],
                )
                unanswered = (_FAILED, {})
                break

            if response.get("ret") != "ok":
                # Offline, or no answer at all. The library's own reading of
                # that applies, availability included, and asking again for
                # each half or each getter would only wait it out again.
                unanswered = (
                    CommandWithMessageHandling._handle_response(
                        self, event_bus, response
                    ),
                    response,
                )
                break

            last_response = response
            answered, header = _answers(response)
            found = {
                member.name: answered[member.name]
                for member in batch
                if member.name in answered
            }
            if not found and len(batch) > 1:
                _LOGGER.debug(
                    "getInfo answered none of %s; asking again in two halves",
                    [member.name for member in batch],
                )
                middle = len(batch) // 2
                pending[:0] = [batch[:middle], batch[middle:]]
                continue
            replies.update(found)

        reached = False
        for member in members:
            reply = replies.get(member.name)
            if reply is not None and reply.get("code", 0) == 0:
                member.wire.handle(event_bus, {"header": header, "body": reply})
                reached = True
                continue

            if unanswered is not None:
                continue
            _LOGGER.debug(
                "getInfo did not answer %s (%s); asking for it alone",
                member.name,
                reply,
            )
            result, response = await member.command._execute(
                authenticator, device_info, event_bus
            )
            if response.get("errno") == 4200:
                # Offline by now: the rest would each buy a timeout.
                unanswered = (result, response)
            elif response.get("ret") == "ok":
                reached = True

        if reached:
            return HandlingResult.success(), last_response
        return unanswered if unanswered is not None else (_FAILED, last_response)


def _answers(
    response: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """The replies of one ``getInfo`` answer, by getter name, and its header.

    No replies for anything that is not the expected shape: the caller reads
    a missing key as "not answered", which is also the right reading of an
    answer it cannot take apart.
    """
    resp = response.get("resp")
    if not isinstance(resp, dict):
        return {}, {}
    header = resp.get("header")
    if not isinstance(header, dict):
        header = {}
    body = resp.get("body")
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return {}, header
    return (
        {name: reply for name, reply in data.items() if isinstance(reply, dict)},
        header,
    )


def state_and_stats() -> GetInfo:
    """What the five-minute tick asks: charge state, clean info and stats.

    The charge state comes first for the reason ``MowerStateRefresh`` awaits
    it first: the clean info is interpreted against the dock record the
    charge handler writes (issue #67). One answer cannot race itself, and
    the replies are handled in this order.
    """
    return GetInfo([GetChargeStateMower(), GetCleanInfoMower(), GetStatsMower()])
