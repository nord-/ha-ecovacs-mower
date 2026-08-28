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

from deebot_client.commands.json.clean import Clean, GetCleanInfo
from deebot_client.commands.json.common import (
    ExecuteCommand,
    JsonCommandWithMessageHandling,
)
from deebot_client.commands.json.life_span import GetLifeSpan
from deebot_client.commands.json.stats import GetStats
from deebot_client.events import LifeSpan
from deebot_client.message import HandlingResult
from deebot_client.models import CleanMode

from .messages import (
    BEACON_COMPONENT,
    OnProtectState,
    OnRainDelay,
    notify_mower_beacons,
    notify_mower_stats,
)

if TYPE_CHECKING:
    from deebot_client.event_bus import EventBus
    from deebot_client.models import CleanAction

_LOGGER = logging.getLogger(__name__)

# Every component string the library has an enum member for. Anything else in an
# answer is dropped rather than parsed, so the entry cannot abort the message.
_KNOWN_COMPONENTS = frozenset(member.value for member in LifeSpan)


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
