"""Per-device state used to decide docked over plan-paused (issue #67).

The mower reports two orthogonal facts at once — charging, and a paused plan —
and ``capabilities.state`` collapses them into a single ``StateEvent``. This
module holds the little state needed to prefer the first over the second, plus
the registry that says which event buses belong to a patched mower at all.

Keyed by ``EventBus`` rather than by ``did`` because message handlers are
classmethods that receive nothing else. An ``EventBus`` is per device, which
makes it both the natural key and self-cleaning: a ``WeakKeyDictionary`` entry
dies with its device, so nothing has to unregister on unload.

``MowerStateRecord`` must never hold a reference back to its bus. That would
make the value keep the key alive and the entry would never be collected.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary

if TYPE_CHECKING:
    from deebot_client.event_bus import EventBus
    from deebot_client.models import State


@dataclass
class MowerStateRecord:
    """What this device is doing, as far as the precedence rule is concerned."""

    docked: bool = False
    suppressed: State | None = None

    def dock(self) -> None:
        """The mower is on its charger."""
        self.docked = True

    def move(self) -> None:
        """The mower reported working or returning.

        The suppressed plan state goes with it: from here the bus's own last
        ``StateEvent`` is the honest answer, and keeping a stale ``PAUSED``
        would let the mow command resume a plan that has since ended.
        """
        self.docked = False
        self.suppressed = None


_RECORDS: WeakKeyDictionary[EventBus, MowerStateRecord] = WeakKeyDictionary()


def register(event_bus: EventBus) -> MowerStateRecord:
    """Mark *event_bus* as a patched mower's, and return its record.

    Registration is also the marker that separates a mower from an ordinary
    Deebot vacuum on the same account: ``MESSAGES`` is global, so the message
    handlers this integration registers are reached for every JSON device, and
    they use this to tell whose payload they are looking at.
    """
    return _RECORDS.setdefault(event_bus, MowerStateRecord())


def record_for(event_bus: EventBus) -> MowerStateRecord | None:
    """The record for *event_bus*, or ``None`` if it is not a patched mower's."""
    return _RECORDS.get(event_bus)


def reset() -> None:
    """Forget every record. Tests only."""
    _RECORDS.clear()
