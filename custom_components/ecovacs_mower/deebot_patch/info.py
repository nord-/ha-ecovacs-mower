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

So a missing key means "not answered", whatever the reason, and that
refusal, every key missing with ``code`` 0, is read as "too large": the batch
is split in half and each half is asked again. That needs no sizes in
advance, which matters because they are not constant: the beacon list alone
grows with every beacon. A getter still unanswered when it is alone in its
batch, or left out of an answer that carries others, is sent as the plain
command it is, and so is every getter of a batch refused any other way.

The sizes still save requests where they are known: a batch is cut before it
is asked, where the replies measured on the issue say its answer would pass
the cap, so a list that would certainly be refused is not sent whole first.
The split stays behind the estimate for whatever it gets wrong.

Two callers send it. The five-minute tick asks for the state and the stats
(``state_and_stats``), and ``RefreshBatch`` carries the event bus's own
refreshes: it holds the getters among them for as long as a burst of
refreshes lasts, at startup and when the mower is reached again after being
unavailable, and sends them together.

No other class has been checked, so a class gets this only through the
``get_info`` flag on its ``MowerProfile``.
"""

from __future__ import annotations

import asyncio
import logging
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, TypeGuard

from deebot_client.command import Command, CommandWithMessageHandling
from deebot_client.commands.json.common import (
    ExecuteCommand,
    JsonCommand,
    JsonCommandWithMessageHandling,
)
from deebot_client.exceptions import ApiTimeoutError
from deebot_client.message import HandlingResult, HandlingState

from .commands import (
    GetCleanInfoMower,
    GetStatsMower,
    MowerStateRefresh,
    _AdaptiveFamily,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from deebot_client.authentication import Authenticator
    from deebot_client.device import DeviceCommandExecute
    from deebot_client.event_bus import EventBus
    from deebot_client.models import ApiDeviceInfo

_LOGGER = logging.getLogger(__name__)

_FAILED = HandlingResult(HandlingState.FAILED)

# How large a getInfo answer will be, estimated before it is asked for, in
# bytes of the whole devmanager.do answer as compact JSON: what an answer
# with no replies comes to, and what each getter's reply adds on top of its
# name, two quotes, a colon and a comma. Measured on a G1-800 (77atlz,
# firmware 1.36.208) from the app's own batches on the issue, for the
# getters this integration refreshes on that class.
_EMPTY_ANSWER = 196
_REPLY_SIZES: Final = MappingProxyType(
    {
        "getAnimProtect": 68,
        "getBattery": 52,
        "getBorderSwitch": 50,
        "getChargeState": 59,
        "getChildLock": 37,
        "getCleanInfo_V2": 62,
        "getCrossMapBorderWarning": 41,
        "getCutDirection": 49,
        "getError": 39,
        "getMoveupWarning": 41,
        "getProtectState": 107,
        "getRainDelay": 53,
        "getSafeProtect": 41,
        "getStats": 88,
        "getUWBOta": 58,
        "getVolume": 52,
    }
)
# A getter nobody measured counts as a reply larger than all but one of the
# measured ones: the cost of guessing too large is a batch cut early, the
# cost of guessing too small an answer refused and asked again in halves.
_UNMEASURED_REPLY = 100
# An answer of 1069 bytes came back and one of about 1081 did not, so a
# batch is closed before its estimate passes 1000.
_ANSWER_BUDGET = 1000


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
    does. A command stands for one getter, except ``MowerStateRefresh``,
    which stands for its two halves, handled in turn: the charge state is
    read before the clean info is interpreted (issue #67). Two commands that
    read the same getter share its name in the request and its reply.

    A getter the batch did not answer is asked alone, as the command it is,
    once every reply that did arrive has been handled, and the halves of its
    command that come after it wait with it. So a slow one holds back
    nothing else: the clean info of a mower whose dialect has not been
    learned yet waits out an ``errno 500`` of about 20 seconds before the
    other family is tried (issue #42). That covers a name this firmware does
    not know in a batch, a getter whose reply carries a failure code, and
    that same clean-info getter, whose dialect only the plain command can
    find out.

    Nothing is retried when a request itself gets no answer: an offline
    mower or a timeout would only be waited out again, once per getter. The
    replies that had arrived by then are still handled. An answer that has
    no replies and is not the too-large refusal ends the batching instead:
    the getters it left are sent as plain commands, so a firmware that
    refused ``getInfo`` outright would cost one request more than sending
    them alone, not one more per half.

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
        did = device_info["did"]
        groups = [
            [_Member(part, did) for part in _parts(command)]
            for command in self._commands
        ]
        names = list(
            dict.fromkeys(member.name for group in groups for member in group)
        )
        if not names:
            return _FAILED, {}

        replies: dict[str, dict[str, Any]] = {}
        header: dict[str, Any] = {}
        last_response: dict[str, Any] = {}
        # Set once a request got no answer: what it came back with, and the
        # end of asking, in batches or alone.
        unanswered: tuple[HandlingResult, dict[str, Any]] | None = None
        pending = _chunks(names)
        while pending:
            batch = pending.pop(0)
            try:
                response = await _InfoRequest(batch)._execute_api_request(
                    authenticator, device_info
                )
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
            answered, header, too_large = _answers(response)
            found = {name: answered[name] for name in batch if name in answered}
            if not found and too_large and len(batch) > 1:
                _LOGGER.debug(
                    "getInfo answered none of %s; asking again in two halves",
                    batch,
                )
                middle = len(batch) // 2
                pending[:0] = [batch[:middle], batch[middle:]]
                continue
            if not found and not too_large:
                # An empty answer other than the one measured for a batch
                # too large: the request itself is being refused, and every
                # further batch would only be refused the same way. Each
                # getter not answered yet goes out as the plain command.
                _LOGGER.debug(
                    "getInfo answered none of %s: %s; asking for each alone",
                    batch,
                    response.get("resp"),
                )
                break
            replies.update(found)

        reached = False
        # Every reply that arrived is handled first; a getter that has to be
        # asked alone comes after, with the halves of its own command that
        # follow it.
        later: list[_Member] = []
        for group in groups:
            for index, member in enumerate(group):
                reply = replies.get(member.name)
                if not _usable(reply):
                    later.extend(group[index:])
                    break
                member.wire.handle(event_bus, {"header": header, "body": reply})
                reached = True

        for member in later:
            reply = replies.get(member.name)
            if _usable(reply):
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


def _parts(
    command: CommandWithMessageHandling,
) -> tuple[CommandWithMessageHandling, ...]:
    """The getters *command* stands for, in the order they are handled."""
    if isinstance(command, MowerStateRefresh):
        return command.halves()
    return (command,)


def _usable(reply: dict[str, Any] | None) -> TypeGuard[dict[str, Any]]:
    """Whether *reply* is one a getter's handler can take: there, and ``code`` 0."""
    return reply is not None and reply.get("code", 0) == 0


def _chunks(names: list[str]) -> list[list[str]]:
    """*names* in their order, cut into batches whose answers should fit.

    Greedy: a batch takes getters until the next one would carry its
    estimated answer past ``_ANSWER_BUDGET``. A getter estimated over the
    budget on its own still gets a batch of its own, which the mower then
    answers or refuses; either way it ends up answered or sent alone.
    """
    chunks: list[list[str]] = []
    size = 0
    for name in names:
        reply = _reply_size(name)
        if chunks and size + reply <= _ANSWER_BUDGET:
            chunks[-1].append(name)
            size += reply
        else:
            chunks.append([name])
            size = _EMPTY_ANSWER + reply
    return chunks


def _reply_size(name: str) -> int:
    """What the reply of the getter *name* is expected to add to an answer."""
    return len(name) + 4 + _REPLY_SIZES.get(name, _UNMEASURED_REPLY)


def _answers(
    response: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any], bool]:
    """One ``getInfo`` answer: its replies by getter name, its header, and
    whether it is the refusal of a batch whose answer would be too large.

    That refusal is the one measured on the issue: a body with ``code`` 0
    and ``data`` an empty object. Any other answer without replies (a
    non-zero ``code``, no ``data``, a shape this cannot take apart) is not
    it, and splitting the batch would only ask again for what is refused.

    No replies for anything that is not the expected shape: the caller reads
    a missing key as "not answered", which is also the right reading of an
    answer it cannot take apart.
    """
    resp = response.get("resp")
    if not isinstance(resp, dict):
        return {}, {}, False
    header = resp.get("header")
    if not isinstance(header, dict):
        header = {}
    body = resp.get("body")
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return {}, header, False
    if not data:
        return {}, header, body.get("code") == 0
    return (
        {name: reply for name, reply in data.items() if isinstance(reply, dict)},
        header,
        False,
    )


def state_and_stats() -> GetInfo:
    """What the five-minute tick asks: charge state, clean info and stats.

    The state is ``MowerStateRefresh``, the patched classes' state refresh,
    so its two halves go out in its order: the clean info is interpreted
    against the dock record the charge handler writes (issue #67). One
    answer cannot race itself, and the replies are handled in this order.
    """
    return GetInfo([MowerStateRefresh(), GetStatsMower()])


def _batchable(command: Command) -> bool:
    """Whether *command* can be named in a ``getInfo``.

    A batch names getters and carries nothing else, so a getter qualifies
    when its request is its bare name: no arguments, the library's own
    payload, and no ``_execute`` of its own, which would mean it sends
    something other than that one request. Of what a G1-800 is refreshed
    with, two do not: ``GetLifeSpanMower``, whose payload is an explicit
    empty list (issue #101), which the probes on the issue found no way to
    batch, and ``GetMapInfoMower``, which names a map type and sends an
    ``appping`` first. Two commands with an ``_execute`` of their own do
    qualify, because
    ``GetInfo`` knows how to name them: the clean info, through the delegate
    of its family, and ``MowerStateRefresh``, through its halves. An action
    never does, whatever its arguments.
    """
    if isinstance(command, MowerStateRefresh | GetCleanInfoMower):
        return True
    return (
        isinstance(command, JsonCommandWithMessageHandling)
        and not isinstance(command, ExecuteCommand)
        and not command._args
        and type(command)._get_payload is JsonCommand._get_payload
        and type(command)._execute is Command._execute
    )


# A burst of refreshes is over once nothing has joined it for this long, and
# is sent after the longest wait at the latest, however many are still coming.
_QUIET = 0.25
_LONGEST = 2.0


class RefreshBatch:
    """Stands between a mower's event bus and the device, batching getters.

    The bus refreshes one event at a time, each with its own commands, and
    asks for nearly every refresh at once on two occasions: while the
    entities subscribe at startup, and when the mower is reached again after
    being unavailable. On a G1-800 that is twenty-odd requests within a
    second (issue #85). Held here for a moment, the getters among them go
    out together as one ``GetInfo``, which cuts them into batches where the
    answer would get too large.

    The first refresh of a burst waits for the burst to end and sends it.
    It runs in one of the bus's own tasks, as every refresh does, and the
    bus cancels those at teardown, so nothing here outlives the device.

    While a refresh waits here, the bus counts its event's refresh as
    running, as it does while a request is in flight, and skips a second one
    for that event in the meantime. Nothing waits longer than about
    ``_LONGEST``. A burst that turns out to hold a single command
    sends that command as it is, and so does a refresh that cannot be named
    in a batch (see ``_batchable``): nothing changes for a refresh that
    comes alone.
    """

    def __init__(self, execute: DeviceCommandExecute) -> None:
        """Initialize the batch in front of the device's *execute*."""
        self._execute = execute
        self._held: list[tuple[Command, asyncio.Future[dict[str, Any]]]] = []

    async def __call__(self, command: Command) -> dict[str, Any]:
        """Send *command* for the bus, held with the rest of its burst."""
        if not _batchable(command):
            return await self._execute(command)
        sent: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._held.append((command, sent))
        if len(self._held) == 1:
            await self._send_when_quiet()
        return await sent

    async def _send_when_quiet(self) -> None:
        """Wait for the burst to end, then send it and release its refreshes."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            while True:
                held = len(self._held)
                await asyncio.sleep(_QUIET)
                if len(self._held) == held or loop.time() - started >= _LONGEST:
                    break
        except asyncio.CancelledError:
            # Teardown: whatever joined is cancelled with it rather than
            # left waiting on a burst nobody will send.
            burst, self._held = self._held, []
            for _, sent in burst:
                sent.cancel()
            raise

        burst, self._held = self._held, []
        response: dict[str, Any] = {}
        try:
            response = await self._send([command for command, _ in burst])
        finally:
            for _, sent in burst:
                if not sent.done():
                    sent.set_result(response)

    async def _send(self, commands: list[Command]) -> dict[str, Any]:
        """Send *commands*, each once, as one ``GetInfo`` or as the one there is.

        Two refreshes with the same command are one: the bus refreshes
        ``StatsEvent`` and ``MowerStatsEvent`` with the same ``getStats``, and
        ``AvailabilityEvent`` and ``BatteryEvent`` with the same
        ``getBattery``. Same means the same class as well as the same name,
        which ``Command.__eq__`` does not check: the library's
        ``GetBorderSwitch`` and ``GetBorderSwitchMower`` are equal there and
        publish different events.
        """
        unique: dict[tuple[type[Command], str], Command] = {}
        for command in commands:
            unique.setdefault((type(command), command.NAME), command)
        if len(unique) == 1:
            (command,) = unique.values()
            return await self._execute(command)
        batch = GetInfo(list(unique.values()))  # type: ignore[arg-type]
        _LOGGER.debug("Refreshing %d commands in one getInfo: %s", len(unique), batch)
        return await self._execute(batch)
