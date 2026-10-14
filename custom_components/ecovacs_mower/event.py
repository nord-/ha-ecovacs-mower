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

The entity has a sibling, *Job started* (issue #120): the start and resume
edges of the same bury points, fired with the trigger word the mower sent and
the job type the topic names, so an automation can act when the app's
schedule starts the mower. Like *Last mowing job* it is a class rather than a
description entry: ``MowerJobEdgeEvent`` is no library capability, so there is
no ``capability_fn`` to describe it with.
"""

import logging
from typing import override

from deebot_client.capabilities import CapabilityEvent, DeviceType
from deebot_client.device import Device
from deebot_client.events import CleanJobStatus, ReportStatsEvent, StateEvent
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

# The trigger words a start or resume has been seen to carry, used verbatim as
# event types (issue #120). Raw rather than mapped: the G1-800's
# mow-schedule-resume says ``schedule`` where the O1200's says ``continue``,
# so one "scheduled" type would be a guess, and ``job_type`` in the event
# data already names the schedule's job on every firmware. A start from Home
# Assistant may fire nothing yet: its word is unobserved, and so is the
# mow-auto-start edge itself. Unconfirmed guess: ``app``, the word on the
# only observed edge of such a job (``mow-auto-stop``, issue #74), which
# would fire with ``job_type: auto``. Any word not listed here gives a debug
# line until it is seen and added.
_JOB_START_TRIGGERS = ("schedule", "app", "continue", "reborn")


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: EcovacsMowerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add entities for passed config_entry in HA."""
    controller = config_entry.runtime_data
    entities: list[EventEntity] = [
        EcovacsLastJobEventEntity(device) for device in controller.devices
    ]
    entities.extend(
        EcovacsJobStartedEventEntity(device)
        for device in controller.devices
        if device.capabilities.device_type is DeviceType.MOWER
    )
    async_add_entities(entities)


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


class EcovacsJobStartedEventEntity(
    EcovacsEntity[CapabilityEvent[StateEvent]],
    EventEntity,
):
    """What started or resumed the mower's job, as the mower announces it (issue #120).

    An event entity, not a sensor (Rickard, 2026-10-13): Home Assistant writes
    no ``state_changed`` for a sensor that writes the value it already has, so
    after the first scheduled start a trigger on a sensor would never fire
    again. This fires once per edge, whatever the previous one was.

    It fires at the announcement, before the departure is confirmed: on the
    G1-800 about 3 s before the state reads mowing, on the O1200 about 13 s
    after. So a condition on the mower mowing, checked when this fires, is
    always false on the G1-800; an automation that must act once the mower
    is out waits for it in its actions. ``continue`` is a resume after a
    charge break and ``reborn`` repeats within one run on the O800 RTK, so
    an event is not always a new job; ``phase`` in the event data says
    which.

    ``capabilities.state`` is passed only because ``EcovacsEntity`` wants a
    capability; nothing subscribes to its event here.
    """

    entity_description = EventEntityDescription(
        key="job_started",
        translation_key="job_started",
        event_types=list(_JOB_START_TRIGGERS),
    )

    def __init__(self, device: Device) -> None:
        """Initialize entity."""
        super().__init__(device, device.capabilities.state)
        self._logged_triggers: set[str] = set()
        # The edge already fired, held by identity for the reason
        # EcovacsLastJobEventEntity._reported_ending gives: the bus replays
        # its last event to every new subscriber, and an entity-ID change
        # re-adds this object.
        self._reported_start: MowerJobEdgeEvent | None = None

    @override
    async def async_added_to_hass(self) -> None:
        """Set up the event listener now that hass is ready."""
        await super().async_added_to_hass()

        self._subscribe(MowerJobEdgeEvent, self._on_job_edge)

    async def _on_job_edge(self, event: MowerJobEdgeEvent) -> None:
        """Fire on a start or resume; a pause or stop is not this entity's."""
        if event.phase not in ("start", "resume"):
            return

        if event.trigger not in _JOB_START_TRIGGERS:
            if event.trigger not in self._logged_triggers:
                self._logged_triggers.add(event.trigger)
                _LOGGER.debug(
                    "Job %s with trigger %r, which is no event type",
                    event.phase,
                    event.trigger,
                )
            return

        if event is self._reported_start:
            return
        self._reported_start = event

        self._trigger_event(
            event.trigger, {"phase": event.phase, "job_type": event.job_type}
        )
        self.async_write_ha_state()
