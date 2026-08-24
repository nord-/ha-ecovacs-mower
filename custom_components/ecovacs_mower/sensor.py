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

The progress sensor is the one entity in this platform whose reading depends on
a poll — ``EcovacsController``'s, not one of its own. The number it reports only
exists while a job is running and the device never pushes it (issue #39), so
asking is the only way to see it move. The poll follows the job rather than the
clock: it starts when the mower starts mowing and stops when it parks, so a
rainy week costs nothing.
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
from .deebot_patch.messages import MowerStatsEvent, MowerTriggerEvent
from .entity import (
    EcovacsCapabilityEntityDescription,
    EcovacsDescriptionEntity,
    EcovacsEntity,
)
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

    async_add_entities(entities)


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


# Error codes the mower reports that deebot-client has no text for. Its
# ERROR_CODES table (const.py) predates the mower line entirely, so a GOAT code
# it does not know falls through as a bare number — issue #37.
#
# This table only fills that gap, it never overrides the library: a code that
# means one thing on a vacuum and another on a mower would be settled upstream,
# not silently here. One entry per code, added only from an observed pairing of
# the code with the Ecovacs app's own wording for it.
_MOWER_ERROR_CODES = {
    422: "Weak signal. Return to the station.",
}

_REPORT_URL = "https://github.com/nord-/ha-ecovacs-mower/issues/37"

# The mower re-sends onError for as long as the condition lasts, so warning on
# every event would fill the log with the same line. The codes already asked
# about are remembered for the lifetime of the process; a restart asks again,
# which is what makes the warning reappear for someone who never saw it.
_UNKNOWN_CODES_REPORTED: set[int] = set()


def _error_description(code: int, description: str | None) -> str | None:
    """The library's text for the code, ours where it has none.

    Returns ``None`` for a code neither table knows, and asks — once per code —
    for the pairing that would let it be added. That request is the whole
    cataloging mechanism: the same convention this integration already uses for
    an unsupported device class.
    """
    if description is not None:
        return description

    if (text := _MOWER_ERROR_CODES.get(code)) is not None:
        return text

    if code not in _UNKNOWN_CODES_REPORTED:
        _UNKNOWN_CODES_REPORTED.add(code)
        _LOGGER.warning(
            "No description for error code %s. Please report the code "
            "together with what the Ecovacs app shows for it at %s",
            code,
            _REPORT_URL,
        )
    return None


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
                CONF_DESCRIPTION: _error_description(event.code, event.description)
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


def _progress(area: int | None, mowed_area: int | None) -> int | None:
    """Percent of the running job that is done, or None when there is no job.

    A zero ``area`` is the absence of a job, not a job that is zero percent
    done: the device reports ``{"area": 0, "time": 0, "mowedArea": 0}`` whenever
    nothing is running. Reporting 0 there would make every automation that waits
    for 100 look, between jobs, exactly like one that has just started.

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

    A calculation on top of one getStats answer, nothing more. The device never
    pushes that answer — ``onStats`` did not arrive once in 38 hours of logging
    (issue #39) — so somebody has to ask, and ``EcovacsController`` already does
    on exactly the trigger and interval this needs. That same answer refreshes
    ``StatsEvent``, which the area and time sensors are built on, so they stop
    being frozen too.
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
        """
        if self._last_state is None or self._last_state is State.DOCKED:
            return
        self._attr_native_value = _progress(event.area, event.mowed_area)
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
        elif event.state is State.DOCKED:
            self._attr_native_value = None
            self.async_write_ha_state()
