"""Ecovacs sensor module.

Forked from Home Assistant core (``homeassistant/components/ecovacs/sensor.py``).
Everything related to vacuum stations (dust bag, mop drying) and the legacy
XMPP-connected class (``EcovacsLegacy*``) has been removed: this integration only
supports GOAT lawn mowers over MQTT, which have no station at all.

``EcovacsActivitySensor`` at the bottom is the one addition core has no
counterpart for. It exists because HA's ``LawnMowerActivity`` enum cannot say
*why* the mower stopped — see the comment above ``_STATE_TO_ACTIVITY``. The
protection flags from ``onProtectState`` are deliberately *not* what it reads;
``deebot_patch.messages`` explains why.
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
from .deebot_patch.messages import MowerTriggerEvent
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
            self._attr_extra_state_attributes = {CONF_DESCRIPTION: event.description}

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
