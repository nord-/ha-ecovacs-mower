"""Ecovacs sensor module.

Forked from Home Assistant core (``homeassistant/components/ecovacs/sensor.py``).
Everything related to vacuum stations (dust bag, mop drying) and the legacy
XMPP-connected class (``EcovacsLegacy*``) has been removed: this integration only
supports GOAT lawn mowers over MQTT, which have no station at all.

``EcovacsActivitySensor`` and ``EcovacsMowingProgressSensor`` at the bottom are
the two additions core has no counterpart for.

The activity sensor exists because HA's ``LawnMowerActivity`` enum cannot say
*why* the mower stopped — see the comment above ``_STATE_TO_ACTIVITY``. The
protection flags from ``onProtectState`` are deliberately *not* what it reads;
``deebot_patch.messages`` explains why.

The progress sensor is the one entity in this platform backed by a poll —
``EcovacsController``'s, not one of its own. The poll follows the job rather
than the clock: it starts when the mower starts mowing and stops when it parks,
so a rainy week costs nothing. On the classes that push ``onStats`` the reading
follows the mower instead and the poll is the floor rather than the source; on
the one it was built against, not known to push it, the poll is what moves the
number mid-run.

Neither of them draws the job's boundaries, though. Both only describe whatever
job the mower happens to be in, and ``State`` reports a finished job and one
parked to charge identically. Those two edges come from the mower's own task
bury points instead — a new job clears the reading, a completion finishes it —
which is why between jobs the last job's figure stands rather than reading
unknown (issue #73).
"""

from collections.abc import Callable
from dataclasses import dataclass
import logging
from typing import Any, override

from deebot_client.capabilities import CapabilityEvent, CapabilityLifeSpan, DeviceType
from deebot_client.device import Device
from deebot_client.events import (
    BatteryEvent,
    ErrorEvent,
    Event,
    LifeSpan,
    LifeSpanEvent,
    NetworkInfoEvent,
    StateEvent,
    StatsEvent,
    TotalStatsEvent,
)
from deebot_client.models import State

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    ATTR_BATTERY_LEVEL,
    CONF_DESCRIPTION,
    PERCENTAGE,
    EntityCategory,
    UnitOfArea,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from . import EcovacsMowerConfigEntry
from .const import SUPPORTED_LIFESPANS
from .deebot_patch import SUPPORTED_CLASSES
from .deebot_patch.messages import (
    MowerBeaconsEvent,
    MowerJobEdgeEvent,
    MowerStatsEvent,
    MowerTriggerEvent,
)
from .entity import (
    EcovacsCapabilityEntityDescription,
    EcovacsDescriptionEntity,
    EcovacsEntity,
)
from .errors import error_description
from .util import get_supported_entities

_LOGGER = logging.getLogger(__name__)


@dataclass(kw_only=True, frozen=True)
class EcovacsSensorEntityDescription[EventT: Event](
    EcovacsCapabilityEntityDescription,
    SensorEntityDescription,
):
    """Ecovacs sensor entity description."""

    value_fn: Callable[[EventT], StateType]
    native_unit_of_measurement_fn: Callable[[DeviceType], str | None] | None = None


@callback
def get_area_native_unit_of_measurement(device_type: DeviceType) -> str | None:
    """Get the area native unit of measurement based on device type."""
    if device_type is DeviceType.MOWER:
        return UnitOfArea.SQUARE_CENTIMETERS
    return UnitOfArea.SQUARE_METERS


ENTITY_DESCRIPTIONS: tuple[EcovacsSensorEntityDescription, ...] = (
    # Stats
    #
    # Named "Job target area"/"Job target duration", not "Mowed area"/"Mowing
    # time": on GOAT, StatsEvent.area/.time are the running job's *target* —
    # the area held still for the whole run while MowerStatsEvent.mowed_area
    # climbs towards it (see its docstring), and time likewise held at a
    # constant estimate across four captured samples before dropping to 0
    # between jobs. Now that this platform's tick refreshes StatsEvent every
    # five minutes (see EcovacsController), a "Mowed area" label would read
    # the whole job's target from the first second of a run, not what has
    # actually been cut — this PR's own mowed_area analysis is what surfaced
    # that the two numbers differ. No fix for stats_time's semantics is
    # possible the same way stats_area's could be repointed at mowed_area:
    # getStats has no elapsed-time counterpart to it, only the target.
    EcovacsSensorEntityDescription[StatsEvent](
        key="stats_area",
        capability_fn=lambda caps: caps.stats.clean,
        value_fn=lambda e: e.area,
        translation_key="stats_area",
        device_class=SensorDeviceClass.AREA,
        native_unit_of_measurement_fn=get_area_native_unit_of_measurement,
        suggested_unit_of_measurement=UnitOfArea.SQUARE_METERS,
    ),
    EcovacsSensorEntityDescription[StatsEvent](
        key="stats_time",
        capability_fn=lambda caps: caps.stats.clean,
        value_fn=lambda e: e.time,
        translation_key="stats_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        # Hours, not minutes: the frontend's duration formatter switches on the
        # displayed unit and expands exactly one step down — "min" gives
        # "241m 30s" for a four-hour run, "h" gives "4h 1m". There is no
        # three-part form to ask for. The cost is that a short run reads
        # "0h 20m"; a mowing session is usually hours, so that is the better
        # end to look silly at.
        suggested_unit_of_measurement=UnitOfTime.HOURS,
    ),
    # TotalStats
    EcovacsSensorEntityDescription[TotalStatsEvent](
        capability_fn=lambda caps: caps.stats.total,
        value_fn=lambda e: e.area,
        key="total_stats_area",
        translation_key="total_stats_area",
        device_class=SensorDeviceClass.AREA,
        # m² is correct, even though stats_area above gets cm² via
        # get_area_native_unit_of_measurement. The two fields really do use
        # different units on the wire: the device reports the per-run area in cm²
        # and the total area in m². Verified against the hardware and against the
        # Ecovacs app — see issue #3. The asymmetry looks like a slip but is
        # correct; do not change it to get_area_native_unit_of_measurement.
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    EcovacsSensorEntityDescription[TotalStatsEvent](
        capability_fn=lambda caps: caps.stats.total,
        value_fn=lambda e: e.time,
        key="total_stats_time",
        translation_key="total_stats_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    EcovacsSensorEntityDescription[TotalStatsEvent](
        capability_fn=lambda caps: caps.stats.total,
        value_fn=lambda e: e.cleanings,
        key="total_stats_cleanings",
        translation_key="total_stats_cleanings",
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    EcovacsSensorEntityDescription[BatteryEvent](
        capability_fn=lambda caps: caps.battery,
        value_fn=lambda e: e.value,
        key=ATTR_BATTERY_LEVEL,
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EcovacsSensorEntityDescription[NetworkInfoEvent](
        capability_fn=lambda caps: caps.network,
        value_fn=lambda e: e.ip,
        key="network_ip",
        translation_key="network_ip",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EcovacsSensorEntityDescription[NetworkInfoEvent](
        capability_fn=lambda caps: caps.network,
        value_fn=lambda e: e.rssi,
        key="network_rssi",
        translation_key="network_rssi",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EcovacsSensorEntityDescription[NetworkInfoEvent](
        capability_fn=lambda caps: caps.network,
        value_fn=lambda e: e.ssid,
        key="network_ssid",
        translation_key="network_ssid",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


@dataclass(kw_only=True, frozen=True)
class EcovacsLifespanSensorEntityDescription(SensorEntityDescription):
    """Ecovacs lifespan sensor entity description."""

    component: LifeSpan
    value_fn: Callable[[LifeSpanEvent], int | float]


LIFESPAN_ENTITY_DESCRIPTIONS = tuple(
    EcovacsLifespanSensorEntityDescription(
        component=component,
        value_fn=lambda e: e.percent,
        key=f"lifespan_{component.name.lower()}",
        translation_key=f"lifespan_{component.name.lower()}",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
    )
    for component in SUPPORTED_LIFESPANS
)


@dataclass(kw_only=True, frozen=True)
class EcovacsBeaconSensorEntityDescription(SensorEntityDescription):
    """Ecovacs beacon sensor entity description."""

    serial: str


def beacon_entity_description(serial: str) -> EcovacsBeaconSensorEntityDescription:
    """Describe the sensor for the beacon with *serial*.

    A factory rather than an entry in a tuple, which is the one place this
    platform departs from the declarative pattern: how many beacons a mower has
    — and what they are called — is not knowable until it has answered
    ``getLifeSpan`` at least once.

    The serial keys the entity because it is the only stable identifier the
    payload carries. There is no index and no guaranteed order, so numbering
    the beacons by arrival would reshuffle four entities the day an answer came
    back in a different order. It is also the code the app's own maintenance
    page prints next to each beacon, so the entity and the app agree on which
    one is which.
    """
    return EcovacsBeaconSensorEntityDescription(
        serial=serial,
        key=f"beacon_{serial}",
        translation_key="beacon",
        # Not just a unit: device_class is what makes HA treat the reading as a
        # battery, which is where the "warn me before it dies" behaviour the
        # request asked for comes from without anyone writing a template.
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    )


# The lawn_mower entity can only ever report HA's five LawnMowerActivity
# members, so "paused because it started raining" is indistinguishable there
# from "paused by hand", and a run cut short by rain ends up looking exactly
# like one that finished. This sensor is the same state with the reason folded
# in. IDLE is PAUSED here for the same reason as in lawn_mower.py: it means
# "standing still", not "standing in the dock".
_STATE_TO_ACTIVITY = {
    State.IDLE: "paused",
    State.CLEANING: "mowing",
    State.RETURNING: "returning",
    State.DOCKED: "docked",
    State.ERROR: "error",
    State.PAUSED: "paused",
}

# Only the states where rain changes the meaning. Mowing is deliberately not
# here: the mower keeps cutting for a few seconds after the sensor gets wet, and
# a "mowing (rain)" that exists for three seconds is noise. ERROR is not here
# either — an error is the more important fact.
_RAIN_ACTIVITY = {
    "paused": "paused_rain",
    "returning": "returning_rain",
    "docked": "docked_rain_delay",
}

# Derived, never hand-written: HA rejects a value the enum sensor did not
# declare, so the options and the states _activity() can return must be the
# same set by construction. Sorted only to keep the order stable.
ACTIVITY_OPTIONS = sorted({*_STATE_TO_ACTIVITY.values(), *_RAIN_ACTIVITY.values()})

# The device's own word for it, in onScheduleTaskInfo and onChargeInfo. This is
# the whole reason the rain handling reads the trigger and not the protection
# flags: the flags need interpreting, "trigger": "rain" does not.
RAIN_TRIGGER = "rain"


def _activity(state: State, *, interrupted_by_rain: bool) -> str:
    """Combine the mower's state with the reason it stopped."""
    activity = _STATE_TO_ACTIVITY[state]
    if interrupted_by_rain:
        return _RAIN_ACTIVITY.get(activity, activity)
    return activity


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: EcovacsMowerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add entities for passed config_entry in HA."""
    controller = config_entry.runtime_data

    entities: list[EcovacsEntity] = get_supported_entities(
        controller, EcovacsSensor, ENTITY_DESCRIPTIONS
    )
    entities.extend(
        EcovacsLifespanSensor(device, device.capabilities.life_span, description)
        for device in controller.devices
        for description in LIFESPAN_ENTITY_DESCRIPTIONS
        if description.component in device.capabilities.life_span.types
    )
    entities.extend(
        EcovacsErrorSensor(device, capability)
        for device in controller.devices
        if (capability := device.capabilities.error)
    )
    entities.extend(
        EcovacsActivitySensor(device)
        for device in controller.devices
        if device.capabilities.device_type is DeviceType.MOWER
    )
    entities.extend(
        EcovacsMowingProgressSensor(device)
        for device in controller.devices
        if device.capabilities.device_type is DeviceType.MOWER
        # MowerStatsEvent's refresh command only exists for patched classes
        # (deebot_patch/hardware.py); without this an unsupported mower class
        # gets an entity that can never have a value.
        and device.device_info["class"] in SUPPORTED_CLASSES
    )

    for device in controller.devices:
        if device.capabilities.device_type is not DeviceType.MOWER:
            continue
        # The same gate as the progress sensor above, for the same reason: the
        # refresh command behind MowerBeaconsEvent only exists for the classes
        # deebot_patch has patched, and an unpatched one would never answer.
        if device.device_info["class"] not in SUPPORTED_CLASSES:
            continue
        _async_setup_beacons(device, config_entry, async_add_entities)

    async_add_entities(entities)


@callback
def _async_setup_beacons(
    device: Device,
    config_entry: EcovacsMowerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a sensor for each beacon as the mower first reports it.

    Every other entity in this integration is known at setup time. These are
    not: the beacons arrive in the ``getLifeSpan`` answer, and until one comes
    back there is nothing to say how many there are. Subscribing here is also
    what asks for that answer — the event bus refreshes an event type for its
    first subscriber, and this is it. The entities that follow subscribe in
    turn and are handed the same last event straight away.

    A beacon is only ever added, never removed. A serial that stops being
    reported leaves an entity behind that reads unknown (see ``_on_beacons``);
    deleting it is the user's call in the entity registry, because a beacon
    missing from one answer on a firmware whose polls fail intermittently
    (issue #42) is not proof that it is gone.
    """
    known: set[str] = set()

    async def on_beacons(event: MowerBeaconsEvent) -> None:
        if not (new := [b.sn for b in event.beacons if b.sn not in known]):
            return
        known.update(new)
        async_add_entities(EcovacsBeaconSensor(device, serial) for serial in new)

    config_entry.async_on_unload(device.events.subscribe(MowerBeaconsEvent, on_beacons))


class EcovacsSensor(
    EcovacsDescriptionEntity[CapabilityEvent],
    SensorEntity,
):
    """Ecovacs sensor."""

    entity_description: EcovacsSensorEntityDescription

    def __init__(
        self,
        device: Device,
        capability: CapabilityEvent,
        entity_description: EcovacsSensorEntityDescription,
        **kwargs: Any,
    ) -> None:
        """Initialize entity."""
        super().__init__(device, capability, entity_description, **kwargs)
        if (
            entity_description.native_unit_of_measurement_fn
            and (
                native_unit_of_measurement
                := entity_description.native_unit_of_measurement_fn(
                    device.capabilities.device_type
                )
            )
            is not None
        ):
            self._attr_native_unit_of_measurement = native_unit_of_measurement

    @override
    async def async_added_to_hass(self) -> None:
        """Set up the event listeners now that hass is ready."""
        await super().async_added_to_hass()

        async def on_event(event: Event) -> None:
            value = self.entity_description.value_fn(event)
            if value is None:
                return

            self._attr_native_value = value
            self.async_write_ha_state()

        self._subscribe(self._capability.event, on_event)


class EcovacsLifespanSensor(
    EcovacsDescriptionEntity[CapabilityLifeSpan],
    SensorEntity,
):
    """Lifespan sensor."""

    entity_description: EcovacsLifespanSensorEntityDescription

    @override
    async def async_added_to_hass(self) -> None:
        """Set up the event listeners now that hass is ready."""
        await super().async_added_to_hass()

        async def on_event(event: LifeSpanEvent) -> None:
            if event.type == self.entity_description.component:
                self._attr_native_value = self.entity_description.value_fn(event)
                self.async_write_ha_state()

        self._subscribe(self._capability.event, on_event)


class EcovacsBeaconSensor(
    EcovacsDescriptionEntity[CapabilityLifeSpan],
    SensorEntity,
):
    """What is left of one UWB beacon's dry cell.

    The device reports these as ``uwbCell`` entries inside the same
    ``getLifeSpan`` answer that carries the blade and the lens brush, which is
    why the capability behind this entity is the life-span one — but not as a
    ``LifeSpanEvent``: the library's enum has no member for the component and
    raises on it, so ``deebot_patch`` parses them out into their own event.
    """

    entity_description: EcovacsBeaconSensorEntityDescription

    def __init__(self, device: Device, serial: str) -> None:
        """Initialize entity."""
        super().__init__(
            device, device.capabilities.life_span, beacon_entity_description(serial)
        )
        # The name is "Beacon <serial>"; strings.json holds the shape and this
        # fills in the one part that differs between the four of them.
        self._attr_translation_placeholders = {"serial": serial}

    @override
    async def async_added_to_hass(self) -> None:
        """Set up the event listeners now that hass is ready."""
        await super().async_added_to_hass()

        # Not the capability's own event: that one is LifeSpanEvent, which
        # cannot describe a beacon at all. Both come out of the same answer.
        self._subscribe(MowerBeaconsEvent, self._on_beacons)

    async def _on_beacons(self, event: MowerBeaconsEvent) -> None:
        """Take this beacon's reading out of the set, or clear it.

        An answer that lists other beacons but not this one means the device
        has stopped reporting it — a swapped cell, a beacon taken off the lawn.
        The reading becomes unknown rather than keeping the last value, which
        would leave a ghost at 0 % that nothing can clear and a low-battery
        automation firing on a beacon that is no longer there.

        An answer with no beacons at all never reaches this handler: the parser
        publishes nothing in that case, so a mower that stops reporting the
        whole set does not silently blank all four.
        """
        self._attr_native_value = next(
            (
                beacon.percent
                for beacon in event.beacons
                if beacon.sn == self.entity_description.serial
            ),
            None,
        )
        self.async_write_ha_state()


class EcovacsErrorSensor(
    EcovacsEntity[CapabilityEvent[ErrorEvent]],
    SensorEntity,
):
    """Error sensor."""

    _always_available = True
    _unrecorded_attributes = frozenset({CONF_DESCRIPTION})
    entity_description: SensorEntityDescription = SensorEntityDescription(
        key="error",
        translation_key="error",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    )

    @override
    async def async_added_to_hass(self) -> None:
        """Set up the event listeners now that hass is ready."""
        await super().async_added_to_hass()

        async def on_event(event: ErrorEvent) -> None:
            self._attr_native_value = event.code
            self._attr_extra_state_attributes = {
                CONF_DESCRIPTION: error_description(event.code, event.description)
            }

            self.async_write_ha_state()

        self._subscribe(self._capability.event, on_event)


class EcovacsActivitySensor(
    EcovacsEntity[CapabilityEvent[StateEvent]],
    SensorEntity,
):
    """The mower's state with the reason it is being held back folded in."""

    entity_description: SensorEntityDescription = SensorEntityDescription(
        key="activity",
        translation_key="activity",
        device_class=SensorDeviceClass.ENUM,
        options=ACTIVITY_OPTIONS,
    )

    def __init__(self, device: Device) -> None:
        """Initialize entity."""
        super().__init__(device, device.capabilities.state)
        # Two events feed one state, so both have to be remembered: whichever
        # arrives second must be able to recombine with the first. Until a
        # StateEvent has been seen there is nothing to report and the entity
        # stays unknown; a trigger on its own is not an activity.
        self._state: State | None = None
        self._interrupted_by_rain = False

    @override
    async def async_added_to_hass(self) -> None:
        """Set up the event listeners now that hass is ready."""
        await super().async_added_to_hass()

        self._subscribe(self._capability.event, self._on_state)
        self._subscribe(MowerTriggerEvent, self._on_trigger)

    async def _on_state(self, event: StateEvent) -> None:
        if event.state not in _STATE_TO_ACTIVITY:
            # Same handling as in lawn_mower.py: keep the last known value
            # rather than reporting a state that is not one of our options,
            # which HA would reject for an enum sensor.
            _LOGGER.warning("Unhandled state from device: %s", event.state)
            return
        if event.state is State.CLEANING:
            # Cutting grass again is the one thing that unambiguously ends a
            # rain stop, and the only signal that does: the delay expiring is
            # not announced, and the mower reports no trigger when it resumes.
            self._interrupted_by_rain = False
        self._state = event.state
        self._write_state()

    async def _on_trigger(self, event: MowerTriggerEvent) -> None:
        # Only rain sets the flag; nothing else clears it. The device sends
        # "workComplete" when it reaches the dock even when rain is what sent it
        # there — 56 seconds after the "rain" trigger in the captured log — so
        # letting any later trigger overwrite the reason would throw it away at
        # exactly the moment the user wants to read it.
        if event.trigger == RAIN_TRIGGER and not self._interrupted_by_rain:
            self._interrupted_by_rain = True
            self._write_state()

    def _write_state(self) -> None:
        if self._state is None:
            return
        self._attr_native_value = _activity(
            self._state, interrupted_by_rain=self._interrupted_by_rain
        )
        self.async_write_ha_state()


# The states in which a stats payload is worth believing. Everything else —
# docked, idle, error, and not yet known — means the numbers on the wire belong
# to a job that is over, and on the firmware branch that never zeroes them
# there is nothing in the payload itself to say so.
#
# Not "the states a job runs in", which is what an earlier version of this set
# named and why RETURNING was in it: on the drive home the firmware has already
# zeroed the stats, so the tick's getStats answers {"area": 0, "mowedArea": 0}
# and _progress() reads that as no job at all. Measured on the captured run
# (issue #73): the completion wrote 100 % at 13:42:30, RETURNING arrived two
# seconds later, and the poll at 13:43:12 erased it. Nothing is lost by
# distrusting it — no mowing happens on the way home.
#
# PAUSED has the same shape of hazard and it is unquantified: no poll landed in
# the three seconds the captured run spent paused before turning for the dock,
# so whether the firmware zeroes there too is unknown. If it does, the fix is
# to latch "job over" until the next start rather than to widen this set.
_STATS_TRUSTED_STATES = frozenset({State.CLEANING, State.PAUSED})

# The start triggers that mean a *new* job rather than the same one carrying
# on. ``reborn`` is the reason this is a whitelist and not "any start": it
# arrived seven times in twelve minutes on one 2px96q run, each time with a
# fresh jobId, which is also what rules out job identity as the signal here.
_NEW_JOB_TRIGGERS = frozenset({"schedule", "app"})


def _progress(area: float | None, mowed_area: float | None) -> int | None:
    """Percent of the running job that is done, or None when there is no job.

    A zero ``area`` is the absence of a job, not a job that is zero percent
    done: on the firmware this was built against, the device reports
    ``{"area": 0, "time": 0, "mowedArea": 0}`` whenever nothing is running.
    Reporting 0 there would make every automation that waits for 100 look,
    between jobs, exactly like one that has just started. That convention is
    not universal — see ``EcovacsMowingProgressSensor._on_stats`` for the
    firmware branch that never zeroes the stats at all, which is why the state
    gate exists and this check is not relied on alone.

    Capping at 100 has not been needed on the verified hardware — a completed
    scheduled run reports ``mowedArea`` exactly equal to its target — but a
    percentage above 100 in a dashboard is worse than one call to ``min``.

    Floats as well as ints: the same ratio is computed from ``MowerStatsEvent``,
    which carries square centimetres as ints, and from ``MowerJobEdgeEvent``,
    which carries square metres as floats. A ratio does not care about the
    unit, so neither does this.
    """
    if not area or mowed_area is None:
        return None
    return min(round(mowed_area / area * 100), 100)


class EcovacsMowingProgressSensor(
    EcovacsEntity[CapabilityEvent[StatsEvent]],
    SensorEntity,
):
    """How far the mower's current job got, or its last one, in percent.

    A calculation on top of two numbers, from wherever they last arrived.

    #39 built this on a poll alone, on the finding that the device never pushes
    them. It does: an O800 RTK and a G1-800 both send ``onStats`` several times
    a second while cutting (issue #55). The reading now comes from
    ``OnStatsMower`` where it is pushed and from ``EcovacsController``'s tick
    where it is not, and either way the same pair of events is notified —
    ``StatsEvent`` with them, so the area and time sensors stop being frozen
    too.

    A third source draws the edges neither of those can, since the numbers
    alone cannot say which job they belong to: the mower's own job bury points
    clear the reading when a new job starts and write the final percentage when
    one completes (issue #73). That is what lets the figure survive a charge
    break instead of going unknown for the length of it.
    """

    entity_description: SensorEntityDescription = SensorEntityDescription(
        key="mowing_progress",
        translation_key="mowing_progress",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    )

    def __init__(self, device: Device) -> None:
        """Initialize entity."""
        super().__init__(device, device.capabilities.stats.clean)
        self._last_state: State | None = None
        self._logged_edges: set[tuple[str, str]] = set()
        # Whether the figure now showing belongs to a job that is over. Set by
        # a stop bury point, cleared when the next job begins. The state cannot
        # answer this — that is the whole of issue #73 — and without it the
        # completion is erasable twice over: while ``_last_state`` is still
        # CLEANING because the firmware dropped the parking push, and while it
        # is PAUSED because a stale paused-plan clean-info landed before the
        # mower reached its dock. Both windows contain the tick's zeroed
        # getStats.
        #
        # The repeat guard in ``_on_state`` is safe against this latch sticking
        # past its window for two independent reasons: the tick bounds a
        # CLEANING-stuck ``_last_state`` to one poll interval, and the start
        # bury point is a second, independent exit that does not depend on
        # ``_last_state`` at all.
        self._job_over = False

    @override
    async def async_added_to_hass(self) -> None:
        """Set up the event listeners now that hass is ready."""
        await super().async_added_to_hass()

        # Not the capability's own event: that one is StatsEvent, which is the
        # library's and has no field for the mowed area. Both events come out of
        # the same GetStatsMower answer. Subscribing is also what buys the one
        # sync at startup — the bus refreshes an event type for its first
        # subscriber.
        self._subscribe(MowerStatsEvent, self._on_stats)
        self._subscribe(StateEvent, self._on_state)
        # _on_job_edge derives _job_over/_last_state, which looks like the
        # violation the project rule against deriving state in a subscription
        # is meant to catch — but it is not: both fields are entity-local and
        # read by nothing else, MowerJobEdgeEvent._seq defeats the bus's
        # dedup, and notify() dispatches callbacks via create_task in notify
        # order, so a stop followed by a stats answer still runs in that
        # order.
        self._subscribe(MowerJobEdgeEvent, self._on_job_edge)

    async def _on_stats(self, event: MowerStatsEvent) -> None:
        """Publish the percentage, but only while a job might be running.

        Gated on the mower's own state rather than trusting the payload's zero
        convention: a GOAT G1-800 on firmware 1.36.208 reported ``mowedArea``
        still equal to the last job's ``area`` six hours after that job ended
        — this firmware branch never zeroes the stats between jobs at all
        (reported against issue #39). ``_progress()``'s zero check alone would
        read that as a job stuck at 100 %, permanently, for as long as the
        mower sits parked.

        The gate names the states whose telemetry is worth believing, not the
        states a job runs in — the comment above ``_STATS_TRUSTED_STATES`` has
        why ``RETURNING`` is a job state and excluded anyway. Naming only
        ``docked`` was the older mistake in the other direction: a job that
        ended away from the dock — a fault out on the lawn, or a plain ``idle``
        push — left the payload trusted, so the never-zeroing firmware kept
        re-asserting the finished job's numbers (issue #55). The figure standing
        between jobs is deliberate now and belongs to ``_on_job_edge``; what
        this gate stops is the payload being read again while nothing runs.

        An unchanged percentage is not written again. HA's state machine already
        short-circuits an unchanged state — no ``state_changed`` event, just a
        bumped ``last_reported`` — so this guard is not saving recorder rows; it
        saves the state-machine round trip and that timestamp bump at the ~2 Hz
        ``onStats`` arrives at on the classes that push it. Whole percents do
        not change nearly that often: one percent of the captured O800 RTK job
        is 2089 cm² against a few hundred per push, so most pushes would
        otherwise round to the number already showing.
        """
        if self._job_over or self._last_state not in _STATS_TRUSTED_STATES:
            return
        value = _progress(event.area, event.mowed_area)
        if value == self._attr_native_value:
            return
        self._attr_native_value = value
        self.async_write_ha_state()

    async def _on_state(self, event: StateEvent) -> None:
        """Take one look when the mower starts cutting, and reset a finished figure.

        Starting is worth a look so the first reading of a run does not wait
        for the next tick. Only entering the state counts: a repeated push of
        the same state (the captured telemetry has two CLEANING pushes 16
        seconds apart) must not trigger a second, redundant getStats.

        Clearing used to live here, on the way *out* of a job state, and that
        was the bug in issue #73: ``State`` cannot tell a job that has finished
        from one that has parked to charge and will resume, because both report
        ``IDLE`` and then ``DOCKED``. A run that needs two batteries therefore
        spent its charge break at unknown — 74 minutes of it on the captured
        run — and a finished job cleared before anything could show how far it
        got.

        Resetting on the way *in* on the strength of the state alone would be
        no better: this handler cannot tell a new job from a resume either, so
        it would blip to zero at every rain pause and every charge resume. Nor
        does the refresh below rescue it — at 09:00:01.757 on the captured run
        it answered ``{"area": 0, "mowedArea": 0}`` 0.2 s into the job, the
        event was deduped away, and the first real number arrived five minutes
        later from the tick.

        What makes the entry edge usable is not the state but ``_job_over``,
        which a stop bury point sets and a resume never does. The edge is then
        only the moment to act on something already known, and the mower having
        announced nothing at all leaves the figure alone.
        """
        if event.state == self._last_state:
            return
        self._last_state = event.state

        if event.state is State.CLEANING:
            # The one reset this handler does, and it is not a guess about
            # which kind of edge this is: the latch already knows the standing
            # figure belongs to a finished job, so a new one is genuinely
            # starting at 0 % rather than at some unknowable in-between value.
            # A resume cannot reach it, because a charge break publishes a
            # pause and a resume, never a stop. Needed because the start bury
            # point arrives 13 seconds into a run — and on a class that never
            # sends it, not at all — so without this a new job opened showing
            # the last one's completion.
            if self._job_over:
                self._job_over = False
                self._attr_native_value = 0
                self.async_write_ha_state()
            self._device.events.request_refresh(MowerStatsEvent)

    async def _on_job_edge(self, event: MowerJobEdgeEvent) -> None:
        """Reset on a new job; publish and latch the final percentage on a completion.

        The mower's own task bury points, which say what ``State`` cannot
        (issue #73). Neither branch is gated on the state: a bury point is the
        device announcing this instant, not telemetry that might be stale, and
        the completion arrives 0.26 s *after* the state edge it belongs to.

        A stop is not necessarily a completion — ``mow-schedule-stop`` carries
        ``app`` as often as ``workComplete``, meaning someone pressed stop —
        and a start is not necessarily a new job. Anything this does not act on
        leaves the reading exactly where it was, and is logged once so a
        trigger nobody has seen yet surfaces without filling the log.

        A stop also latches ``_job_over``, and that latch is what makes both
        clears safe and what keeps the completion from being erased. Writing
        the final percentage is not enough on its own: the state can still read
        CLEANING when it arrives, because this firmware drops parking pushes,
        and it can read PAUSED before the mower reaches its dock — and the
        tick's zeroed ``getStats`` lands inside both windows. See ``__init__``
        and ``_on_stats``.

        The reset is therefore conditional in both places it happens. The start
        announcement lands 13 seconds into a run, by which time a class pushing
        ``onStats`` twice a second has already reported the new job's own
        progress; whichever of the two edges notices the new job first resets,
        and the other finds the latch already down and does nothing.

        ``EventBus.subscribe`` replays the last event of a type to a new
        subscriber, so disabling and re-enabling this entity within one
        config-entry lifetime re-delivers the last stop, sets the latch again,
        and rewrites the completion on top of a job that may already be
        running. Rare enough — the entity has to be toggled mid-job — that it
        is recorded here rather than guarded against.

        No hard 100 on a completion: a zone's ``workArea`` is the polygon's
        estimate, and the captured zone job finished at 24.287498 of 32.162498
        m². That the mower considers itself done after 76 % is information, not
        an error — and writing 100 anyway would make the scheduled run's exact
        equality indistinguishable from a fabricated one. There is no dedicated
        "job finished" signal for other entities to watch yet: ``lawn_mower.
        <device>`` going to ``paused`` also covers a charge break and a manual
        or rain pause (see ``_on_state`` above), so it cannot stand in for one.
        ``event.<device>_last_mowing_job`` is that signal (issue #74).
        """
        # Both phases named explicitly rather than leaning on "not a start
        # means a stop": registering the pause and resume edges is meant to be
        # one subclass each, and a pause carrying workComplete would then write
        # a final value halfway through a job.
        if event.phase == "start" and event.trigger in _NEW_JOB_TRIGGERS:
            # Gated on the latch for the same reason the CLEANING edge is, and
            # it is what keeps the two from fighting: whichever notices the new
            # job first does the reset, and the other becomes a no-op. On a
            # pushing class that means the reading the mower has already sent
            # for the new job survives the start announcement.
            if self._job_over:
                self._job_over = False
                self._attr_native_value = 0
                self.async_write_ha_state()
            return

        if event.phase == "stop":
            # Any stop ends the job, whatever its trigger and whether or not
            # there is a number to publish.
            self._job_over = True

        if event.phase == "stop" and event.trigger == "workComplete":
            # A completion nobody can put a number on is not a job at zero
            # percent, and clearing the reading here is the one thing this
            # handler must never do — it is what the old state-edge clearing
            # did. Every capture so far carries the pair as floats, so this is
            # a guard rather than an observed case.
            if (value := _progress(event.work_area, event.mowed_area)) is None:
                _LOGGER.debug(
                    "Job completed with unusable areas (%r of %r); "
                    "leaving the mowing progress at %r",
                    event.mowed_area,
                    event.work_area,
                    self._attr_native_value,
                )
                return
            self._attr_native_value = value
            self.async_write_ha_state()
            return

        edge = (event.phase, event.trigger)
        if edge not in self._logged_edges:
            self._logged_edges.add(edge)
            _LOGGER.debug(
                "Job %s with trigger %r leaves the mowing progress alone",
                event.phase,
                event.trigger,
            )
