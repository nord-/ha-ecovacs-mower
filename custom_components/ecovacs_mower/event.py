"""Ecovacs event module.

Forked from Home Assistant core (``homeassistant/components/ecovacs/event.py``).
The file needed no change for legacy devices: core's ``async_setup_entry`` already
only iterates over ``controller.devices``, without any reference to the
XMPP-connected devices this fork does not have. The only change is that
``EcovacsConfigEntry`` is called ``EcovacsMowerConfigEntry`` here.

``get_name_key`` was not in the fork's ``util.py`` — it was dropped in ``c9be9a8``
along with the select platform, its only user. It has been restored in util.py for
this entity.

Beyond that the entity has gained a second source. Core's runs on
``ReportStatsEvent`` alone, which on a GOAT never arrives: the library's
``reportStats`` does not appear once in any capture, on either patched class,
against 28 observed job endings — so the entity had never fired on a mower with
148 jobs behind it (issue #74). The job's own stop bury point says the same
thing and says *why*, which is the part ``onLastTimeStats`` cannot supply: it
reports every ending as ``stop: 1`` and no capture anywhere carries the
``stopReason`` the library classifies on.
"""

import logging
from typing import override

from deebot_client.capabilities import CapabilityEvent
from deebot_client.device import Device
from deebot_client.events import CleanJobStatus, ReportStatsEvent
from deebot_client.events.base import Event

from homeassistant.components.event import EventEntity, EventEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EcovacsMowerConfigEntry
from .deebot_patch.messages import MowerJobEdgeEvent
from .entity import EcovacsEntity
from .util import get_name_key

_LOGGER = logging.getLogger(__name__)

# The stop triggers that say how a job ended, mapped onto the event types the
# entity already declares. Only what has been observed: workComplete and app
# are the two triggers a mow stop has ever carried. ``finished_with_warnings``
# is deliberately unmapped — no trigger is known to mean it, and firing the
# wrong type would tell an automation a job finished cleanly when nobody knows
# that.
_TRIGGER_EVENT_TYPES = {
    "workComplete": "finished",
    "app": "manually_stopped",
}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: EcovacsMowerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add entities for passed config_entry in HA."""
    controller = config_entry.runtime_data
    async_add_entities(
        EcovacsLastJobEventEntity(device) for device in controller.devices
    )


class EcovacsLastJobEventEntity(
    EcovacsEntity[CapabilityEvent[ReportStatsEvent]],
    EventEntity,
):
    """Ecovacs last job event entity."""

    entity_description = EventEntityDescription(
        key="stats_report",
        translation_key="last_job",
        entity_category=EntityCategory.DIAGNOSTIC,
        event_types=["finished", "finished_with_warnings", "manually_stopped"],
    )

    def __init__(self, device: Device) -> None:
        """Initialize entity."""
        super().__init__(device, device.capabilities.stats.report)
        # Whether the library's own source has ever reported a job *ending*
        # for this device. Nothing observed says reportStats is dead
        # everywhere — only that it is dead on the two classes there are
        # captures for — so the first ending it reports stands the bury-point
        # path down permanently, rather than the other way round.
        #
        # It settles the steady state, not the first ending: on a class
        # carrying both, if the announcement happens to arrive before the
        # report, that one ending is reported twice, and the flag being
        # instance state means the same is true of the first ending after each
        # restart. Collapsing the two would need them correlated, and their
        # identifiers are from different spaces — the announcement carries a
        # jobId, the report a cid — never yet seen together on one device,
        # since no GOAT has been observed to send reportStats at all.
        self._library_source_seen = False
        self._logged_triggers: set[str] = set()
        # The ending already reported, held by identity so a replay of it does
        # not fire again. EventBus.subscribe hands its last event of a type to
        # every new subscriber, and changing the entity ID is enough to reach
        # that: HA removes and re-adds the same object against the same config
        # entry, Device and EventBus, so both subscriptions here replay. A
        # name-only rename does not — that hits entity.py's
        # registry_entry.entity_id == old.entity_id branch and just calls
        # async_write_ha_state(). Without this guard, an entity-ID change hours
        # after a job re-fired the ending with a fresh timestamp and every
        # automation on the entity ran again.
        #
        # Identity rather than equality, and the bus hands back the very object
        # it notified. A genuinely new ending is always a new instance —
        # MowerJobEdgeEvent._seq exists so two equal-looking ones cannot
        # collapse into one notification in the first place.
        self._reported_ending: Event | None = None

    @override
    async def async_added_to_hass(self) -> None:
        """Set up the event listeners now that hass is ready."""
        await super().async_added_to_hass()

        self._subscribe(self._capability.event, self._on_report)
        # Harmless on a device that never sends it: MowerJobEdgeEvent exists
        # only for the patched mower classes, and subscribing to an event with
        # no refresh command is a silent no-op.
        self._subscribe(MowerJobEdgeEvent, self._on_job_edge)

    async def _on_report(self, event: ReportStatsEvent) -> None:
        """Handle the library's own job report, where it arrives."""
        if event.status in (CleanJobStatus.NO_STATUS, CleanJobStatus.CLEANING):
            # we trigger only on job done
            return

        # Latched here rather than on arrival, so it means "the library
        # produced an ending" and not merely "the library spoke". reportStats
        # reports CLEANING while a job runs, and a class that sent those but
        # never the terminal one would otherwise silence the second source and
        # leave the entity as dead as issue #74 found it.
        self._library_source_seen = True

        if event is self._reported_ending:
            return
        self._reported_ending = event

        self._trigger_event(get_name_key(event.status))
        self.async_write_ha_state()

    async def _on_job_edge(self, event: MowerJobEdgeEvent) -> None:
        """Fire from the mower's own stop announcement, where the library is silent.

        A start carries no ending to report, and a stop is not necessarily a
        completion — the trigger is what says which, and the mapping covers
        only what has been observed. Anything else is logged once and dropped,
        so a trigger nobody has seen yet surfaces without filling the log.
        """
        if self._library_source_seen or event.phase != "stop":
            return

        if (event_type := _TRIGGER_EVENT_TYPES.get(event.trigger)) is None:
            if event.trigger not in self._logged_triggers:
                self._logged_triggers.add(event.trigger)
                _LOGGER.debug(
                    "Job stopped with trigger %r, which maps to no event type",
                    event.trigger,
                )
            return

        if event is self._reported_ending:
            return
        self._reported_ending = event

        self._trigger_event(event_type)
        self.async_write_ha_state()
