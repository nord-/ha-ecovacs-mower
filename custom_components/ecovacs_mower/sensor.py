"""Ecovacs sensor module.

Forked from Home Assistant core (``homeassistant/components/ecovacs/sensor.py``).
Everything related to vacuum stations (dust bag, mop drying) and the legacy
XMPP-connected class (``EcovacsLegacy*``) has been removed: this integration only
supports GOAT lawn mowers over MQTT, which have no station at all.

``EcovacsActivitySensor`` is not from core. It is the lawn_mower entity's state
with the rain flag folded in, because ``LawnMowerActivity`` is a closed enum
owned by Home Assistant — "returning" cannot become "returning, because of
rain" there without breaking the frontend's translations and every
``lawn_mower.is_returning`` condition.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, override

from deebot_client.capabilities import (
    Capabilities,
    CapabilityEvent,
    CapabilityLifeSpan,
    DeviceType,
)
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
from .deebot_patch.messages import MowerProtectStateEvent
from .entity import (
    EcovacsCapabilityEntityDescription,
    EcovacsDescriptionEntity,
    EcovacsEntity,
)
from .util import get_supported_entities


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
        suggested_unit_of_measurement=UnitOfTime.MINUTES,
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


# The same reading of the states as lawn_mower._STATE_TO_MOWER_STATE, kept as a
# separate map on purpose: this one is a set of translation keys this integration
# owns, the other is HA's LawnMowerActivity enum. Tying them together would mean
# a new activity here needs HA to add an enum member.
_STATE_TO_ACTIVITY = {
    State.IDLE: "paused",
    State.CLEANING: "mowing",
    State.RETURNING: "returning",
    State.DOCKED: "docked",
    State.ERROR: "error",
    State.PAUSED: "paused",
}

# Only the three activities a rained-off run actually passes through get a rain
# wording. "mowing" is not among them (the mower does not mow in the rain) and
# neither is "error": a fault is worth reporting over the weather.
_RAIN_ACTIVITY = {
    "paused": "paused_rain",
    "returning": "returning_rain",
    "docked": "docked_rain_delay",
}

# A set, not a list: IDLE and PAUSED both map to "paused", and
# SensorDeviceClass.ENUM rejects a duplicated option. Sorted so the order the
# frontend shows does not depend on dict iteration order.
ACTIVITY_OPTIONS = sorted({*_STATE_TO_ACTIVITY.values(), *_RAIN_ACTIVITY.values()})


def activity_key(state: State | None, *, raining: bool) -> str | None:
    """Return the translation key for a state, rain folded in."""
    if state is None:
        return None
    activity = _STATE_TO_ACTIVITY.get(state)
    if activity is None:
        return None
    if raining:
        return _RAIN_ACTIVITY.get(activity, activity)
    return activity


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
    EcovacsEntity[Capabilities],
    SensorEntity,
):
    """What the mower is doing, and whether rain is the reason.

    A rained-off scheduled run is indistinguishable from a completed one in the
    lawn_mower entity: both end up "docked". This sensor is the same state with
    the rain flag applied, so "docked because it is raining" reads differently
    from "docked because the lawn is done".
    """

    entity_description: SensorEntityDescription = SensorEntityDescription(
        key="activity",
        translation_key="activity",
        device_class=SensorDeviceClass.ENUM,
        options=ACTIVITY_OPTIONS,
    )

    def __init__(self, device: Device) -> None:
        """Initialize the activity sensor."""
        super().__init__(device, device.capabilities)
        self._state: State | None = None
        self._raining = False

    @override
    async def async_added_to_hass(self) -> None:
        """Set up the event listeners now that hass is ready."""
        await super().async_added_to_hass()

        self._subscribe(self._capability.state.event, self._on_state)
        self._subscribe(MowerProtectStateEvent, self._on_protect_state)

    async def _on_state(self, event: StateEvent) -> None:
        self._state = event.state
        self._update()

    async def _on_protect_state(self, event: MowerProtectStateEvent) -> None:
        self._raining = event.raining
        self._update()

    def _update(self) -> None:
        # Both inputs arrive as separate events, so the value is recomputed from
        # the pair rather than derived from whichever event came last.
        self._attr_native_value = activity_key(self._state, raining=self._raining)
        self.async_write_ha_state()
