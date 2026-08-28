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

``GetStatsMower`` is a third kind again: the command works and is answered, the
library just discards one of the three numbers it answers with (issue #39). Its
counterpart for the unsolicited half, ``OnStatsMower``, is in ``messages.py``.

``GetLifeSpanMower`` is a fourth: the command works, is answered in full, and
one component of the answer makes the library abandon the rest of it (issue
#40).

``GetRainDelay`` and ``SetRainDelay`` are the same kind as ``GetProtectState``
— commands the library does not have at all — with the difference that this
setting is writable, so it needs both halves (issue #54).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from deebot_client.command import Command
from deebot_client.commands.json.charge_state import GetChargeState
from deebot_client.commands.json.clean import (
    Clean,
    CleanV2,
    GetCleanInfo,
    GetCleanInfoV2,
)
from deebot_client.commands.json.common import (
    ExecuteCommand,
    JsonCommandWithMessageHandling,
)
from deebot_client.commands.json.life_span import GetLifeSpan
from deebot_client.commands.json.stats import GetStats
from deebot_client.const import DataType
from deebot_client.events import LifeSpan, StateEvent
from deebot_client.message import HandlingResult, HandlingState
from deebot_client.models import CleanAction, CleanMode, State

from .families import Family, commit, note_attempt, selected
from .messages import (
    BEACON_COMPONENT,
    OnProtectState,
    OnRainDelay,
    handle_clean_info,
    notify_mower_beacons,
    notify_mower_stats,
)
from .state_precedence import record_for

if TYPE_CHECKING:
    from deebot_client.authentication import Authenticator
    from deebot_client.event_bus import EventBus
    from deebot_client.models import ApiDeviceInfo

_LOGGER = logging.getLogger(__name__)

# Every component string the library has an enum member for. Anything else in an
# answer is dropped rather than parsed, so the entry cannot abort the message.
_KNOWN_COMPONENTS = frozenset(member.value for member in LifeSpan)


class GetChargeStateMower(GetChargeState):
    """``getChargeState``, recording that the mower is on its charger.

    The record is written here, synchronously, before the answer is published.
    Nothing subscribes for it: ``EventBus.notify`` drops an event equal to the
    previous one before any subscriber runs, and ``notify`` also rewrites
    ``IDLE`` to ``DOCKED`` after a ``DOCKED``, so what subscribers see is not a
    faithful account of what was published.

    ``isCharging: 0`` deliberately does not clear the record. A mower parked on
    a full battery stops charging without having moved, and clearing there
    would hand the decision back to the plan state — issue #67 again. Only
    ``CLEANING`` or ``RETURNING`` clears it.
    """

    @classmethod
    def _handle_body_data_dict(
        cls, event_bus: EventBus, data: dict[str, Any]
    ) -> HandlingResult:
        """Handle message->body->data."""
        if data.get("isCharging") == 1 and (
            record := record_for(event_bus)
        ) is not None:
            record.dock()
        return super()._handle_body_data_dict(event_bus, data)

    @classmethod
    def _handle_body(cls, event_bus: EventBus, body: dict[str, Any]) -> HandlingResult:
        """Handle message->body.

        A response shaped ``{"msg": "fail", "code": "30007"}`` — "already
        charging" answered as a failure — never reaches
        ``_handle_body_data_dict`` above: upstream's own ``_handle_body``
        branches on the code before descending into ``body->data`` at all, and
        its ``if status:`` branch there always notifies ``DOCKED`` directly,
        even for the codes ("3", "5") it maps to ``State.ERROR`` internally —
        upstream's own quirk, not something to correct here.
        ``HandlingState.SUCCESS`` together with a non-zero code happens only
        inside that branch, so checking the outcome rather than re-listing
        "30007"/"3"/"5" stays correct even if upstream adds another code to the
        list, and the record ends up agreeing with what was actually
        published rather than second-guessing upstream's mapping.
        """
        result = super()._handle_body(event_bus, body)
        if (
            result.state is HandlingState.SUCCESS
            and body.get("code", 0) != 0
            and (record := record_for(event_bus)) is not None
        ):
            record.dock()
        return result


class _MowerCleanInfoHandling:
    """Clean-info parsing shared by every command family and both directions.

    ``getCleanInfo`` with the answer that is always the same left out.

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

    This deliberately does not change what an ``idle`` *push* means. A
    registered mower's ``onCleanInfo`` resolves through ``OnCleanInfo`` in
    ``messages.py`` to ``handle_clean_info`` below, not through the library's
    own ``GetCleanInfo`` via ``get_legacy_message()`` — that fallback is only
    reached for an unregistered bus, i.e. an ordinary vacuum. Either way an
    ``idle`` there is a real event: the device chose to send it, at the moment
    something stopped. It is the polled answer that carries no information.

    The charge gate (issue #67) lives here too, in ``handle_clean_info``, so
    the polled answer and the pushed message cannot disagree. Five entities and
    background jobs subscribe to ``StateEvent``; arbitrating in only one of
    them would leave the others contradicting it.
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
            # Dropped before the gate, so a polled idle never reaches the
            # record. On 1.13.x that costs nothing, since the poll answers idle
            # unconditionally — that is why this branch exists. On a firmware
            # where the poll answers truthfully, the update arrives by push.
            return HandlingResult.success()
        return handle_clean_info(event_bus, data)


class _AdaptiveFamily:
    """Sends whichever family the mower answers on, and learns which that is.

    ``Command.execute()`` is ``@final``, but ``_execute()`` is not, so the
    library's ``requested_commands`` handling and exception logging are
    inherited unchanged.

    The raw response is read rather than the ``HandlingResult``, because
    ``CommandWithMessageHandling._handle_response`` collapses two very
    different situations into ``HandlingState.FAILED``: ``errno 4200``, the
    mower being offline, and ``errno 500``, which the library itself glosses as
    "network issues or does not support the command". Switching on ``FAILED``
    would mean a mower that is merely offline changed dialect.

    The switch is committed only on positive evidence that the other family
    answered — ``response.get("ret") == "ok"`` — never merely on the absence
    of ``errno 500``. ``errno 4200`` (the mower going offline mid-attempt) and
    an ``ApiTimeoutError`` (which leaves ``response`` empty) both satisfy "not
    500" without the mower having said anything at all; committing on either
    would assert a dialect change that no answer evidenced, and do so with a
    false log line at the exact moment a user is looking at their log to
    debug the mower. The first check above is exempt from this: ``errno ==
    500`` is what decides whether to try the other family at all, and that
    read stays correct regardless of what the second attempt does.

    Both silent also means the network, not the dialect — ``getStats`` has
    been seen failing intermittently with the same ``errno 500`` on this
    firmware — and a commit-on-first-failure would log a false dialect change
    for every blip.

    A fallback costs a second cloud timeout on every genuine ``errno 500``,
    since the second attempt waits it out again before answering. Acceptable
    for the five-minute poll, but ``CleanMower`` runs inside
    ``lawn_mower.start_mowing``, so a user pressing Start while the mower is
    briefly out of Wi-Fi range now waits for two timeouts before
    ``_execute_command`` logs "did not confirm" — worth knowing before
    "fixing" the slow service call by removing this fallback.
    """

    def _delegate(self, family: Family) -> Command:
        """The command that goes out for *family*."""
        raise NotImplementedError

    async def _execute(
        self,
        authenticator: Authenticator,
        device_info: ApiDeviceInfo,
        event_bus: EventBus,
    ) -> tuple[HandlingResult, dict[str, Any]]:
        """Execute on the selected family, falling back to the other once."""
        did = device_info["did"]
        current = selected(did)
        result, response = await self._delegate(current)._execute(
            authenticator, device_info, event_bus
        )
        if response.get("errno") != 500:
            note_attempt(did, current)
            return result, response

        other = current.other()
        result, response = await self._delegate(other)._execute(
            authenticator, device_info, event_bus
        )
        note_attempt(did, current, other)
        if response.get("ret") == "ok":
            commit(did, other)
        return result, response


def has_family(command: Command) -> bool:
    """Whether *command* is one of the two whose wire format depends on the
    mower's dialect (issue #42) — the only commands ``family_name()`` means
    anything for.

    Used by ``entity.py`` to decide whether to name the family in the
    unconfirmed-command warning: doing so unconditionally would print it next
    to a command like ``Charge`` too, which has no V2/non-V2 pair at all, and
    claim a dialect for something that does not have one.
    """
    return isinstance(command, _AdaptiveFamily)


class _NoActionRewrite:
    """Sends the action it was given, without ``Clean._execute``'s rewrite.

    ``Clean._execute`` second-guesses its own arguments from
    ``event_bus.get_last_event(StateEvent)``, turning ``start`` into ``resume``
    and back. That cannot stay in the delegates: the precedence gate (#67)
    hides the ``PAUSED`` that rewrite reads while the mower is on its dock, so
    a wrapper that correctly chose ``resume`` from the suppressed plan state
    would have it rewritten straight back to ``start``.

    ``Clean`` extends ``ExecuteCommand``, and neither it nor
    ``JsonCommandWithMessageHandling`` overrides ``_execute``, so
    ``Command._execute`` is exactly what ``Clean._execute``'s own
    ``super()._execute`` reaches. Calling it directly drops the rewrite and
    nothing else. ``tests/deebot_patch/test_contract.py`` pins that, so the day
    upstream moves the rewrite down the chain this fails loudly instead of
    quietly resurrecting the bug.
    """

    async def _execute(
        self,
        authenticator: Authenticator,
        device_info: ApiDeviceInfo,
        event_bus: EventBus,
    ) -> tuple[HandlingResult, dict[str, Any]]:
        """Execute without consulting the last state."""
        return await Command._execute(self, authenticator, device_info, event_bus)


class _CleanNonV2(_NoActionRewrite, Clean):
    """Mow on the ``clean`` topic with a V2 payload, as the app does."""

    def _get_args(self, action: CleanAction) -> dict[str, Any]:
        return {"act": action.value, "content": {"type": CleanMode.AUTO.value}}


class _CleanV2Mower(_NoActionRewrite, CleanV2):
    """Mow on ``clean_V2``, with the library's own payload shape.

    Not overridden: the ``{"act": "resume", "content": {}}`` the reporter
    captured being acknowledged in 526 ms on firmware 1.36.208 is precisely
    what ``CleanV2._get_args`` produces.
    """


class CleanMower(_AdaptiveFamily, Clean):
    """The mow command, on whichever clean command the mower answers.

    Takes the ``START``/``RESUME`` decision itself, from the effective state:
    the plan state the precedence gate suppressed if there is one, and
    otherwise the bus's own last ``StateEvent`` — which is what the library
    would have used.

    Still a ``Clean`` subclass, for two reasons. ``ExecuteCommand`` up that
    chain supplies the concrete ``_handle_body`` that makes the class
    instantiable, and ``test_commands.py`` asserts the subclass relationship —
    the mow command being a ``Clean`` is part of what the patch layer promises.
    ``Clean._execute`` is inherited and shadowed: ``_AdaptiveFamily`` comes
    first in the MRO, so the argument rewrite never runs on the wrapper either.
    """

    def __init__(self, action: CleanAction) -> None:
        """Build both delegates for *action*."""
        self._action = action
        self._delegates: dict[Family, Command] = {}
        super().__init__(action)

    def _get_args(self, action: CleanAction) -> dict[str, Any]:
        # Inert as a payload. Clean.__init__ calls this and stores the result
        # in _args, but nothing sends it: each delegate builds its own payload
        # from its own library base. Not empty, though: Command.__eq__
        # compares NAME and _args, and an empty dict here would make every
        # CleanMower(...) instance equal regardless of action, so
        # mock.assert_called_with(CleanMower(CleanAction.PAUSE)) would pass
        # for a call actually made with CleanAction.START. Keying on the
        # action keeps equality — and the wrapper's repr() — meaningful
        # without this ever being a shape a delegate would recognise.
        return {"act": action.value}

    def _delegate(self, family: Family) -> Command:
        return self._delegates[family]

    async def _execute(
        self,
        authenticator: Authenticator,
        device_info: ApiDeviceInfo,
        event_bus: EventBus,
    ) -> tuple[HandlingResult, dict[str, Any]]:
        """Decide the action, then send it on the family that answers."""
        action = self._effective_action(event_bus)
        self._delegates = {
            Family.NON_V2: _CleanNonV2(action),
            Family.V2: _CleanV2Mower(action),
        }
        return await super()._execute(authenticator, device_info, event_bus)

    def _effective_action(self, event_bus: EventBus) -> CleanAction:
        """``START`` or ``RESUME``, from the state the mower is really in.

        A pause the gate suppressed still has to reach this decision: the
        entity reads docked, but the plan is paused and the mower wants
        ``resume``. Nothing else reads ``record.suppressed``.
        """
        if self._action not in (CleanAction.START, CleanAction.RESUME):
            return self._action

        state = None
        if (record := record_for(event_bus)) is not None:
            state = record.suppressed
        if state is None and (last := event_bus.get_last_event(StateEvent)):
            state = last.state

        if state is State.PAUSED:
            return CleanAction.RESUME
        return CleanAction.START


class _GetCleanInfoNonV2(_MowerCleanInfoHandling, GetCleanInfo):
    """``getCleanInfo``: what every confirmed firmware answers."""


class _GetCleanInfoV2(_MowerCleanInfoHandling, GetCleanInfoV2):
    """``getCleanInfo_V2``: what firmware 1.36.208 answers instead.

    The mixin is what matters here. Delegating to the library's
    ``GetCleanInfoV2`` unchanged would give this path neither the idle drop nor
    the charge gate, because a command's handler notifies the event bus from
    inside ``handle()`` and a wrapper never sees the value.
    """


class GetCleanInfoMower(_AdaptiveFamily, GetCleanInfo):
    """The state poll, on whichever clean-info command the mower answers.

    Extends ``GetCleanInfo`` rather than ``JsonCommandWithMessageHandling``
    directly, and not for tidiness: ``MessageBody._handle_body`` is abstract and
    ``JsonCommandWithMessageHandling`` mixes it in, so a wrapper deriving
    straight from it cannot be instantiated at all. ``GetCleanInfo`` supplies a
    concrete one — unreachable here, since ``_execute`` is overridden and no
    response is ever routed through this class's ``handle()``, but it makes the
    class real. "Unreachable" is not the same as "harmless if reached": that
    inherited method is upstream's own unmodified parsing, without either this
    file's idle drop (issue #48) or its charge gate (issue #67), unlike the
    mixin-based delegates below that this wrapper actually sends. It also
    keeps ``NAME`` at the non-V2 spelling, which is the default and what the
    library's log lines should say most of the time; the delegates carry the
    name that actually goes out, and ``entity._execute_command`` names the
    family alongside it so a user sending logs is not misled.
    """

    def __init__(self) -> None:
        """Build both delegates once; they are stateless."""
        super().__init__()
        self._delegates = {
            Family.NON_V2: _GetCleanInfoNonV2(),
            Family.V2: _GetCleanInfoV2(),
        }

    def _delegate(self, family: Family) -> Command:
        return self._delegates[family]


class MowerStateRefresh(JsonCommandWithMessageHandling):
    """The state refresh, as one command instead of two concurrent ones.

    ``capabilities.state`` used to hold ``[GetChargeState(), GetCleanInfo…()]``,
    and ``EventBus._call_refresh_function`` runs a multi-command refresh inside
    an ``asyncio.TaskGroup``. The two answers therefore raced, and whichever
    landed last decided whether a mower parked on its charger read as docked or
    as paused — 21 milliseconds apart in issue #67.

    Awaiting the charge half first removes the race at the source: the record is
    written before the clean-info answer is interpreted, so the gate in
    ``_MowerCleanInfoHandling`` cannot read a record its concurrent partner has
    not written yet.

    This command does not arbitrate and does not publish. Its two halves notify
    from inside ``handle()`` like every other command, and the arbitration is
    the gate — which is what lets the pushed messages share it.

    ``NAME`` and ``DATA_TYPE`` exist because ``Command.__init_subclass__``
    requires them; this name never goes out on the wire, since the halves carry
    their own. They serve the library's log lines and ``__eq__``, so do not
    delete them for looking unused — and the name is deliberately not
    ``getChargeState``: ``Command.__eq__`` compares ``NAME`` plus ``_args``, and
    both this command and ``GetChargeStateMower()`` carry empty args, so sharing
    the name would make them compare equal.

    Only the clean half's ``(result, response)`` is returned. ``execute()``
    marks the device reached from ``result.device_reached``, so a poll where
    ``getChargeState`` answers but both ``getCleanInfo`` families come back
    ``errno 500`` no longer marks it reached, where a lone ``GetChargeState``
    did before this command replaced it. Drift, not a regression this command
    introduces on its own — the only visible effect is an extra availability
    check 60 seconds later — but worth knowing before assuming this command's
    availability behaviour is identical to what it replaced.
    """

    NAME = "getMowerState"
    DATA_TYPE = DataType.JSON

    def _get_payload(self) -> dict[str, Any]:
        # Inert, not required: JsonCommand._get_payload already has a concrete
        # implementation, so nothing forces this override to exist. It is here
        # so an empty payload is explicit rather than inherited — _execute
        # below never calls _execute_api_request at all, since the two halves
        # build and send their own, and inheriting a real payload-builder here
        # would quietly make this command look sendable when it is not.
        return {}

    @classmethod
    def _handle_body(cls, event_bus: EventBus, body: dict[str, Any]) -> HandlingResult:
        # Unreachable, and concrete on purpose. MessageBody._handle_body is
        # abstract and JsonCommandWithMessageHandling mixes it in, so without
        # this the class cannot be instantiated at all and patch_device_info
        # raises TypeError before the integration ever starts. Nothing calls it,
        # because _execute never routes a response through handle() — the two
        # halves handle their own. analyse() rather than success() so that if
        # some future path does reach it, the log says so.
        return HandlingResult.analyse()

    async def _execute(
        self,
        authenticator: Authenticator,
        device_info: ApiDeviceInfo,
        event_bus: EventBus,
    ) -> tuple[HandlingResult, dict[str, Any]]:
        """Ask for the charge state, then for the clean info."""
        result, response = await GetChargeStateMower()._execute(
            authenticator, device_info, event_bus
        )
        if response.get("errno") == 4200:
            # Offline. The library's handler has already published
            # AvailabilityEvent(False); a second request buys another timeout.
            #
            # An ApiTimeoutError on this same half also means no answer
            # arrived — response is {} rather than carrying errno 4200 — and
            # running the clean half next would buy a second timeout inside
            # this one Device semaphore slot, same as here. Left alone on
            # purpose: that is a behaviour change, and a whole-branch review
            # scoped it out rather than folding it in unreviewed. This
            # asymmetry is therefore a decision, not an oversight.
            return result, response

        # The charge half's own `result` — and with it, any
        # result.requested_commands — is discarded here on every path except
        # the offline one above: only the clean half's (result, response) is
        # returned, and Command.execute() dispatches requested_commands from
        # what _execute returns, not from an intermediate result. Silent today
        # because neither GetChargeState nor GetChargeStateMower ever
        # populates that field. If a future charge handler starts requesting
        # follow-up commands, they would be swallowed here without a trace.
        return await GetCleanInfoMower()._execute(
            authenticator, device_info, event_bus
        )


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


class GetRainDelay(JsonCommandWithMessageHandling, OnRainDelay):
    """Ask for the rain sensor's setting instead of waiting for a push.

    The same shape as ``GetProtectState`` one setting over, and the same trap:
    ``onRainDelay`` is sent when somebody changes the setting and never
    otherwise, so without this the switch and the number would read "unknown"
    from startup until the owner next opened the app and touched the rain
    sensor (issue #31 is the identical failure on the protection flags).

    ``OnRainDelay`` is inherited for its handler: the answer carries the same
    payload as the push, so both entry points must parse it the same way. Only
    ``NAME`` differs, which is also why the pair cannot be one class — the
    message registry and the command topic are keyed on that one string.

    Evidence that the command exists on the wire, since the library has no
    definition to copy: ``Janverhu/ecovacs-goat-g1`` requests ``getRainDelay``
    in its startup group against a GOAT G1 and parses the answer as an
    ``onRainDelay`` payload. It takes no arguments.
    """

    NAME = "getRainDelay"


class SetRainDelay(ExecuteCommand):
    """Write the rain sensor's setting and its post-rain hold.

    The device wants the pair, not a field at a time: the same integration that
    establishes ``getRainDelay`` reads the other half out of its own state
    before every write, for both the toggle and the duration. That is why the
    switch and the number entities each hold the whole last event and send the
    field they do not own unchanged.

    ``ExecuteCommand`` rather than the library's ``JsonSetCommand``: that base
    exists to link a set to its get so an answer can update the sensors, and it
    drags ``CommandMqttP2P`` along with it. Neither buys anything here — the
    device pushes ``onRainDelay`` on every change, including its own answer to
    this command, which is how the entities learn the new value. What
    ``ExecuteCommand`` does give is the part that matters: a non-zero ``code``
    in the reply is reported as a failure instead of passing for success.
    """

    NAME = "setRainDelay"

    def __init__(self, *, enable: bool, delay: int) -> None:
        # 0/1, not JSON booleans: that is what the app sends and what every
        # observed payload of this message carries.
        super().__init__({"enable": 1 if enable else 0, "delay": delay})


class GetStatsMower(GetStats):
    """``getStats``, keeping the field the library throws away.

    The answer carries three numbers — ``area``, ``time`` and ``mowedArea`` —
    and upstream's handler builds a ``StatsEvent`` from the first two. On GOAT
    the dropped one is the interesting one: ``area`` is the target area of the
    running job and holds still, ``mowedArea`` is the part already cut and
    climbs (issue #39).

    ``NAME`` is inherited on purpose. The wire command is unchanged, so this
    replaces the parsing of an existing request rather than adding a second one,
    and ``super()`` still runs so ``StatsEvent`` keeps being published for the
    area and time sensors that were already built on it.
    """

    @classmethod
    def _handle_body_data_dict(
        cls, event_bus: EventBus, data: dict[str, Any]
    ) -> HandlingResult:
        """Handle message->body->data, then publish the mower's own pair.

        Shared with ``OnStatsMower``, which parses the same payload arriving as
        a push rather than as an answer.
        """
        result = super()._handle_body_data_dict(event_bus, data)
        notify_mower_stats(event_bus, data)
        return result


class GetLifeSpanMower(GetLifeSpan):
    """``getLifeSpan``, without the component that makes the library give up.

    A beacon-guided GOAT reports one ``uwbCell`` entry per UWB beacon, keyed by
    the serial the app prints on its maintenance page, alongside the blade and
    the lens brush. ``LifeSpan`` has no member for it and no ``_missing_`` hook,
    so upstream's ``LifeSpan(component["type"])`` raises on the first beacon.
    ``Message.handle`` catches that and logs "Could not parse getLifeSpan", but
    the loop notifies as it goes: everything before the first beacon is
    published and everything after it is lost. On a G1-800 the order is blade,
    four beacons, lens brush — so the blade sensor works, the lens brush reads
    a value from before the beacons were paired and never moves again, and the
    beacons themselves are invisible (issue #40).

    ``NAME`` is inherited on purpose, as in ``GetStatsMower``: the request is
    unchanged. The device answers with every component it has whatever the
    request lists — ``9bts2s`` and its siblings ask for ``blade`` and
    ``lensBrush`` only, and the beacons come back regardless — so there is
    nothing to add to the query, only something to stop dropping.
    """

    @classmethod
    def _handle_body_data_list(
        cls, event_bus: EventBus, data: list[dict[str, Any]]
    ) -> HandlingResult:
        """Publish the beacons, then let upstream parse what it recognises.

        Beacons first for the same reason ``OnStatsMower`` publishes first: the
        components handed to ``super()`` are parsed with upstream's own
        arithmetic, which raises on a non-positive total, and a beacon reading
        should not be lost to a blade entry the library cannot divide.
        """
        notify_mower_beacons(event_bus, data)

        reported = {component.get("type") for component in data}
        if unhandled := reported - _KNOWN_COMPONENTS - {BEACON_COMPONENT}:
            # Debug rather than a warning: this would fire on every poll for as
            # long as the firmware keeps sending the component, and the users
            # asked for a component string are the ones already running debug
            # logging. The reading is lost either way — but silently, and the
            # rest of the answer arrives, which is the whole point.
            _LOGGER.debug("Life span components without a handler: %s", unhandled)

        return super()._handle_body_data_list(
            event_bus,
            [
                component
                for component in data
                if component.get("type") in _KNOWN_COMPONENTS
            ],
        )
