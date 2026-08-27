"""The fault latch: hold an error until something explicitly clears it.

Issue #53. On firmware 1.36.208 a blade-disc jam pushes ``onError code:[406]``
exactly once and is followed 89 ms later by ``code:[0]`` — and then by another
3076 of them over the 38 minutes the mower sat stuck on the lawn draining its
battery. Every entity that mirrors the device's last word therefore read as
healthy: ``sensor.<device>_error`` back to ``0``, ``lawn_mower.<device>`` back to
``paused``, which is indistinguishable from a pause by hand. Nothing an
automation could fire on had existed for longer than 89 ms.

``code:[0]`` on this firmware is a heartbeat that happens to contain a zero, not
a statement that the mower recovered — it kept arriving at ~1.4 Hz while the
cutting disc was jammed. So the latch never lets a zero clear it, and it does
not debounce either: no amount of waiting turns that heartbeat into evidence.
Neither a delay nor a count of consecutive zeros can work, because the flood is
unbounded — it lasted as long as it took the owner to walk out to the mower.

What clears it is a union, deliberately, so no single firmware quirk can wedge
it: ``DOCKED``, ``CLEANING``, or a press of the clear button. The last one needs
no cooperation from the device at all, which is what guarantees the latch cannot
get stuck permanently. Whether ``DOCKED`` and ``CLEANING`` actually arrive on
1.36.208 was never established — the log that would have said so is gone — so
every set and clear is logged with its reason at INFO. That line is the evidence
the next report will carry, without asking anyone to run ``deebot_client`` at
DEBUG and keep 3000 lines.

The latch is owned by ``EcovacsController``, not by an entity, for the reason
``_setup_polling`` gives: entities can each be disabled in the entity registry,
and state that only one of them happened to own must not silently stop existing.

Deliberately not Home Assistant-aware — the state machine is the part worth
testing, and keeping the import out means ``tests/test_fault.py`` runs on
Windows instead of only in CI.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Final

from deebot_client.events import ErrorEvent, StateEvent
from deebot_client.events.base import Event
from deebot_client.models import State

from .errors import error_description

if TYPE_CHECKING:
    from deebot_client.device import Device

_LOGGER = logging.getLogger(__name__)

# Docked means the mower got home, mowing means it is cutting grass again.
# Neither is compatible with an unresolved fault, and both are things the device
# reports about itself rather than something we infer from the absence of a
# code.
#
# CLEANING is the library's name for mowing. It is included even though a
# firmware that keeps reporting CLEANING through a fault would clear the latch
# immediately: on the one firmware where this was observed the state did leave
# CLEANING (the entity log in issue #53 shows it going to paused), and a mower
# genuinely cutting grass is not stuck. The button is the backstop if some
# firmware proves otherwise.
_RECOVERY_STATES: Final = frozenset({State.DOCKED, State.CLEANING})


@dataclass(frozen=True)
class MowerFaultEvent(Event):
    """The fault that is currently unresolved, or none.

    ``code is None`` means nothing is latched. Published on the device's own
    event bus so the entities subscribe to it through the ordinary
    ``EcovacsEntity._subscribe`` path, exactly like a message-backed event —
    no second notification mechanism, and the bus's suppression of an event
    equal to the previous one keeps a re-latched identical code off the state
    machine for free.
    """

    code: int | None
    description: str | None


class FaultLatch:
    """One mower's unresolved fault."""

    def __init__(self, device: Device) -> None:
        """Initialize the latch, cleared."""
        self._device = device
        self._code: int | None = None

    @property
    def code(self) -> int | None:
        """The latched error code, or ``None`` when nothing is latched."""
        return self._code

    def subscribe(self) -> None:
        """Start following the device's errors and states.

        Eager, and not tied to any entity: ``EventBus.notify`` drops an event
        that has no subscribers, so a fault raised before the binary sensor was
        added would otherwise be lost. The bus does record it as ``last_event``
        first, which is what replays the current fault to an entity that
        subscribes later.
        """
        self._device.events.subscribe(ErrorEvent, self._on_error)
        self._device.events.subscribe(StateEvent, self._on_state)

    async def _on_error(self, event: ErrorEvent) -> None:
        """Latch a non-zero code. A zero is not evidence of anything."""
        if event.code:
            self._latch(event.code)

    async def _on_state(self, event: StateEvent) -> None:
        """Clear on a state that cannot coexist with an unresolved fault."""
        if event.state in _RECOVERY_STATES:
            self._clear(f"state is {event.state.name}")

    def clear_by_request(self) -> None:
        """Clear on the user's say-so, from the clear button."""
        self._clear("cleared by request")

    def _latch(self, code: int) -> None:
        """Hold *code*, replacing any earlier one.

        The newest non-zero code is the current diagnosis, so it wins. Nothing
        is lost that the log does not keep: each latch logs its own line.
        """
        if code == self._code:
            return

        description = error_description(code)
        _LOGGER.info(
            "Fault latched for %s: code %s (%s). It stays until the mower "
            "docks, starts mowing, or the fault is cleared by hand",
            self._device.device_info["did"],
            code,
            description or "no description",
        )
        self._code = code
        self._publish(description)

    def _clear(self, reason: str) -> None:
        """Release the latch, if anything is held."""
        if self._code is None:
            return

        _LOGGER.info(
            "Fault cleared for %s: code %s, because %s",
            self._device.device_info["did"],
            self._code,
            reason,
        )
        self._code = None
        self._publish(None)

    def _publish(self, description: str | None) -> None:
        """Tell the entities."""
        self._device.events.notify(
            MowerFaultEvent(code=self._code, description=description)
        )
