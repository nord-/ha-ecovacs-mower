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
the one it was built against, not known to push it, the poll is still the only
way to see the number move.
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


# The states in which the stats payload describes a job that is actually
# running. Everything else — docked, idle, error, and not yet known — means the
# numbers on the wire belong to a job that is over, and on the firmware branch
# that never zeroes them there is nothing in the payload itself to say so.
_JOB_STATES = frozenset({State.CLEANING, State.PAUSED, State.RETURNING})


def _progress(area: int | None, mowed_area: int | None) -> int | None:
    """Percent of the running job that is done, or None when there is no job.

    A zero ``area`` is the absence of a job, not a job that is zero percent
    done: on the firmware this was built against, the device reports
    ``{"area": 0, "time": 0, "mowedArea": 0}`` whenever nothing is running.
    Reporting 0 there would make every automation that waits for 100 look,
    between jobs, exactly like one that has just started. That convention is
    not universal — see ``EcovacsMowingProgressSensor._on_stats`` for the
    firmware branch that never zeroes the stats at all, which is why the state
    gate exists and this check is not relied on alone.

    Capping at 100 has not been needed on the verified hardware — a completed run
    reports ``mowedArea`` exactly equal to its target — but a percentage above
    100 in a dashboard is worse than one call to ``min``.
    """
    if not area or mowed_area is None:
        return None
    return min(round(mowed_area / area * 100), 100)


class EcovacsMowingProgressSensor(
    EcovacsEntity[CapabilityEvent[StatsEvent]],
    SensorEntity,
):
    """How much of the running job the mower has cut, in percent.

    A calculation on top of two numbers, from wherever they last arrived.

    #39 built this on a poll alone, on the finding that the device never pushes
    them. It does: an O800 RTK and a G1-800 both send ``onStats`` several times
    a second while cutting (issue #55). The reading now comes from
    ``OnStatsMower`` where it is pushed and from ``EcovacsController``'s tick
    where it is not, and either way the same pair of events is notified —
    ``StatsEvent`` with them, so the area and time sensors stop being frozen
    too.
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

    async def _on_stats(self, event: MowerStatsEvent) -> None:
        """Publish the percentage, but only while a job might be running.

        Gated on the mower's own state rather than trusting the payload's zero
        convention: a GOAT G1-800 on firmware 1.36.208 reported ``mowedArea``
        still equal to the last job's ``area`` six hours after that job ended
        — this firmware branch never zeroes the stats between jobs at all
        (reported against issue #39). ``_progress()``'s zero check alone would
        read that as a job stuck at 100 %, permanently, for as long as the
        mower sits parked.

        The gate names the states a job runs *in* rather than the states it
        does not. Naming only ``docked`` left a job that ended away from the
        dock — a fault out on the lawn, or a plain ``idle`` push — holding the
        last percentage on that same never-zeroing firmware (issue #55).

        An unchanged percentage is not written again. HA's state machine already
        short-circuits an unchanged state — no ``state_changed`` event, just a
        bumped ``last_reported`` — so this guard is not saving recorder rows; it
        saves the state-machine round trip and that timestamp bump at the ~2 Hz
        ``onStats`` arrives at on the classes that push it. Whole percents do
        not change nearly that often: one percent of the captured O800 RTK job
        is 2089 cm² against a few hundred per push, so most pushes would
        otherwise round to the number already showing.
        """
        if self._last_state not in _JOB_STATES:
            return
        value = _progress(event.area, event.mowed_area)
        if value == self._attr_native_value:
            return
        self._attr_native_value = value
        self.async_write_ha_state()

    async def _on_state(self, event: StateEvent) -> None:
        """Take one look when a job starts; clear the reading when it parks.

        Starting is worth a look so the first reading of a run does not wait
        for the next tick. Docking clears the value immediately rather than
        asking again and waiting for a zeroed answer: whether the stats ever
        zero out at all is exactly the thing that differs by firmware branch
        (see _on_stats), so the state is the only signal both branches agree
        on.

        Only entering a state counts either way: a repeated push of the same
        state (the captured telemetry has two CLEANING pushes 16 seconds
        apart) must not trigger a second, redundant getStats.

        Nothing else is acted on. ``paused`` is left alone too: it is a normal
        mid-run state (rain, a manual pause) rather than a start or dock edge,
        and the periodic tick already keeps asking while the mower is out
        regardless of which of those it currently is.
        """
        if event.state == self._last_state:
            return
        self._last_state = event.state

        if event.state is State.CLEANING:
            self._device.events.request_refresh(MowerStatsEvent)
        elif event.state not in _JOB_STATES:
            self._attr_native_value = None
            self.async_write_ha_state()
