"""Dynamic Home Assistant entities for mower area parameters.

Area identity and state are owned by ``deebot_patch``. This module only turns
that state into the four model-specific area parameter views. The entities are
dynamic because the mower reports its area IDs at runtime, just like the
existing beacon sensors.

Model- and firmware-specific conversion from raw device values to
human-sensible Home Assistant values lives exclusively in this HA layer. The
A1600 LiDAR Pro findings that established these mappings are deliberately scoped
to that model: the values have not been verified on the other mower classes
supported by this integration.

The four parameter views are writable on the validated A1600 model. Each write
is converted back to raw protocol values and merged with the other three values
from the authoritative area snapshot before one complete ``setAreaParameter``
command is sent. The entity state is not updated optimistically; the mower must
report the resulting raw values through the normal area refresh.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, override

from deebot_client.capabilities import DeviceType
from deebot_client.device import Device

from homeassistant.components.number import NumberEntity, NumberEntityDescription
from homeassistant.const import DEGREE, EntityCategory, UnitOfLength, UnitOfSpeed
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EcovacsMowerConfigEntry
from .deebot_patch.areas import (
    MowerArea,
    MowerAreaEvent,
    area_for,
)
from .deebot_patch.commands import SetAreaParameter
from .deebot_patch.hardware import profile_for_class
from .entity import EcovacsDescriptionEntity


@dataclass(frozen=True)
class AreaParameterLookup:
    """Validated raw-value lookup for one mower model parameter.

    ``values`` is ordered by raw protocol value, starting at ``raw_start``.
    Add a lookup here only when the complete raw-to-HA representation has been
    independently verified on the specific mower model. Do not reuse a lookup
    for another model merely because the Ecovacs field has the same name.
    """

    values: tuple[float | int, ...]
    raw_start: int = 1

    def __post_init__(self) -> None:
        """Reject lookup tables that cannot be exposed as a HA number step."""
        if not self.values:
            raise ValueError("Area parameter lookup must contain at least one value")
        if len(self.values) < 2:
            return
        steps = {
            round(float(b) - float(a), 10)
            for a, b in zip(self.values, self.values[1:])
        }
        if len(steps) != 1 or not steps or next(iter(steps)) == 0:
            raise ValueError("Area parameter lookup values must have one non-zero step")

    @property
    def raw_end(self) -> int:
        """Return the highest raw value represented by this lookup."""
        return self.raw_start + len(self.values) - 1

    @property
    def native_min_value(self) -> float:
        """Return the lowest Home Assistant value represented."""
        return float(min(self.values))

    @property
    def native_max_value(self) -> float:
        """Return the highest Home Assistant value represented."""
        return float(max(self.values))

    @property
    def native_step(self) -> float:
        """Return the HA step represented by this lookup."""
        if len(self.values) < 2:
            return 1.0
        return abs(round(float(self.values[1]) - float(self.values[0]), 10))

    def to_native(self, raw_value: int) -> float | int | None:
        """Convert a raw mower value to its HA representation."""
        index = raw_value - self.raw_start
        if index < 0 or index >= len(self.values):
            return None
        return self.values[index]

    def to_raw(self, native_value: float) -> int | None:
        """Convert an HA value to its raw mower representation."""
        for index, value in enumerate(self.values):
            if float(value) == native_value:
                return self.raw_start + index
        return None


@dataclass(frozen=True)
class AreaParameterFormula:
    """Validated formula conversion for one mower model parameter.

    Use this when the verified representation is a mathematical conversion
    rather than a finite raw-value lookup. A formula is still model-specific:
    create a separate one when another mower is independently verified to use a
    different formula. Keep HA range and step metadata here so the generic
    entity-description builder contains no model-specific values.
    """

    to_native: Callable[[int], float | int | None]
    to_raw: Callable[[float], int | None]
    native_min_value: float
    native_max_value: float
    native_step: float


@dataclass(frozen=True)
class AreaParameterMapping:
    """HA representation mappings for one verified mower model.

    The first three parameters are finite raw-value lookups. The angle is kept
    as a formula because its verified representation is a coordinate transform
    rather than a simple value table. When adding another mower model, add its
    own ``AreaParameterMapping`` entry and provide independently verified
    mappings for all four parameters; do not inherit A1600 values by default.
    """

    mow_height: AreaParameterLookup
    cut_speed: AreaParameterLookup
    obstacle_height: AreaParameterLookup
    cut_angle: AreaParameterFormula


# The following three tables are the A1600 LiDAR Pro's validated HA
# representations. Each tuple position corresponds to a raw value beginning at
# one. The tuple is therefore the single source of truth for conversion in both
# directions and for the HA number entity's min/max/step metadata.
_A1600_MOW_HEIGHT = AreaParameterLookup(
    values=(9, 8, 7, 6, 5, 4, 3),
)

_A1600_CUT_SPEED = AreaParameterLookup(
    values=(0.70, 0.65, 0.60, 0.55, 0.50, 0.45, 0.40),
)

_A1600_OBSTACLE_HEIGHT = AreaParameterLookup(
    values=(10, 15, 20),
)


def _a1600_cut_angle(wire_angle: int) -> int | None:
    """Convert the A1600 wire-space angle to the app-space angle.

    Confirmed on the A1600 LiDAR Pro specifically. The observed symmetric
    conversion is ``app = (270 - wire) mod 360``; the same formula converts the
    app value back to wire space.
    """
    if wire_angle not in range(360):
        return None
    return (270 - wire_angle) % 360


def _a1600_cut_angle_to_raw(value: float) -> int | None:
    """Convert an A1600 app-space cutting direction to wire-space angle."""
    if value not in range(360):
        return None
    return int((270 - value) % 360)


_A1600_CUT_ANGLE = AreaParameterFormula(
    to_native=_a1600_cut_angle,
    to_raw=_a1600_cut_angle_to_raw,
    native_min_value=0,
    native_max_value=359,
    native_step=1,
)


# These mappings are presentation semantics, not protocol semantics. Keep them
# in the HA layer and add a class only after its raw-value representation has
# been verified independently. Do not infer that another GOAT class shares the
# A1600 representation merely because its protocol fields have the same names.
#
# To support another model, add one explicit entry here. Populate each lookup
# from that model's independently verified raw values, in raw-value order, and
# provide its verified angle formula (or a separate formula definition). The
# generic entity code below derives conversion and number limits from these
# mappings; no model-specific ranges belong in ``area_sensor_descriptions``.
AREA_PARAMETER_MAPPINGS: dict[str, AreaParameterMapping] = {
    "e4gqia": AreaParameterMapping(
        mow_height=_A1600_MOW_HEIGHT,
        cut_speed=_A1600_CUT_SPEED,
        obstacle_height=_A1600_OBSTACLE_HEIGHT,
        cut_angle=_A1600_CUT_ANGLE,
    ),
}


def build_set_area_parameter(
    area: MowerArea, raw_field: str, raw_value: int
) -> SetAreaParameter | None:
    """Merge one raw change into a complete authoritative area write.

    The mower requires all five raw values. Returning no command for an
    incomplete snapshot prevents a writable HA entity from inventing defaults
    for fields that have not been reported by the mower yet.
    """
    if any(
        parameter is None
        for parameter in (
            area.mow_height_level,
            area.cut_mode,
            area.obstacle_height,
            area.angle,
        )
    ):
        return None

    raw_values = {
        "mow_height_level": area.mow_height_level,
        "cut_mode": area.cut_mode,
        "obstacle_height": area.obstacle_height,
        "angle": area.angle,
    }
    if raw_field not in raw_values:
        return None
    raw_values[raw_field] = raw_value
    return SetAreaParameter(
        area_id=area.area_id,
        mow_height_level=raw_values["mow_height_level"],
        cut_mode=raw_values["cut_mode"],
        obstacle_height=raw_values["obstacle_height"],
        angle=raw_values["angle"],
    )


@dataclass(kw_only=True, frozen=True)
class EcovacsAreaNumberEntityDescription(NumberEntityDescription):
    """Describe one dynamic writable view of one mower area."""

    value_fn: Callable[[MowerArea], float | int | None]
    to_raw_fn: Callable[[float], int | None]
    raw_field: str
    parameter_name: str
    suggested_object_id: str | None = None


def area_number_description(
    area_id: str,
    key_suffix: str,
    parameter_name: str,
    raw_field: str,
    value_fn: Callable[[MowerArea], float | int | None],
    to_raw_fn: Callable[[float], int | None],
    **kwargs: object,
) -> EcovacsAreaNumberEntityDescription:
    """Describe one dynamic number entity for a mower area."""
    return EcovacsAreaNumberEntityDescription(
        key=f"area_{area_id}_{key_suffix}",
        name=parameter_name,
        value_fn=value_fn,
        to_raw_fn=to_raw_fn,
        raw_field=raw_field,
        parameter_name=parameter_name,
        # Keep the numeric area ID in the suggested object ID so newly created
        # entities use a stable area-ID-based object ID instead of depending on
        # the mower's user-editable friendly name.
        suggested_object_id=f"{area_id}_{key_suffix}",
        entity_category=EntityCategory.CONFIG,
        **kwargs,
    )


def area_sensor_descriptions(
    area_id: str,
    *,
    area_mapping: AreaParameterMapping,
) -> tuple[EcovacsAreaNumberEntityDescription, ...]:
    """Return the four model-specific writable area parameter views."""
    return (
        area_number_description(
            area_id,
            "cutting_height",
            "Cutting height",
            "mow_height_level",
            lambda area: area_mapping.mow_height.to_native(area.mow_height_level)
            if area.mow_height_level is not None
            else None,
            area_mapping.mow_height.to_raw,
            native_min_value=area_mapping.mow_height.native_min_value,
            native_max_value=area_mapping.mow_height.native_max_value,
            native_step=area_mapping.mow_height.native_step,
            native_unit_of_measurement=UnitOfLength.CENTIMETERS,
            icon="mdi:grass",
        ),
        area_number_description(
            area_id,
            "mowing_speed",
            "Mowing speed",
            "cut_mode",
            lambda area: area_mapping.cut_speed.to_native(area.cut_mode)
            if area.cut_mode is not None
            else None,
            area_mapping.cut_speed.to_raw,
            native_min_value=area_mapping.cut_speed.native_min_value,
            native_max_value=area_mapping.cut_speed.native_max_value,
            native_step=area_mapping.cut_speed.native_step,
            native_unit_of_measurement=UnitOfSpeed.METERS_PER_SECOND,
            icon="mdi:speedometer",
        ),
        area_number_description(
            area_id,
            "obstacle_height",
            "Obstacle height",
            "obstacle_height",
            lambda area: area_mapping.obstacle_height.to_native(area.obstacle_height)
            if area.obstacle_height is not None
            else None,
            area_mapping.obstacle_height.to_raw,
            native_min_value=area_mapping.obstacle_height.native_min_value,
            native_max_value=area_mapping.obstacle_height.native_max_value,
            native_step=area_mapping.obstacle_height.native_step,
            native_unit_of_measurement=UnitOfLength.CENTIMETERS,
            icon="mdi:format-vertical-align-top",
        ),
        area_number_description(
            area_id,
            "cut_direction",
            "Cutting direction",
            "angle",
            lambda area: area_mapping.cut_angle.to_native(area.angle)
            if area.angle is not None
            else None,
            area_mapping.cut_angle.to_raw,
            native_min_value=area_mapping.cut_angle.native_min_value,
            native_max_value=area_mapping.cut_angle.native_max_value,
            native_step=area_mapping.cut_angle.native_step,
            native_unit_of_measurement=DEGREE,
            icon="mdi:angle-acute",
        ),
    )


class EcovacsAreaNumber(EcovacsDescriptionEntity, NumberEntity):
    """Expose one writable parameter from one mower area."""

    entity_description: EcovacsAreaNumberEntityDescription

    def __init__(
        self,
        device: Device,
        area_id: str,
        description: EcovacsAreaNumberEntityDescription,
        area_name: str,
    ) -> None:
        """Initialize the dynamic area entity."""
        super().__init__(device, device.capabilities, description)
        self._area_id = area_id
        self._set_area_name(area_name)
        self._attr_icon = description.icon

    def _set_area_name(self, area_name: str) -> None:
        """Set the integration-provided name without changing identity."""
        # The friendly area name is deliberately part of the integration's
        # original name. HA users can override the entity name in the registry.
        self._attr_name = f"{area_name} - {self.entity_description.parameter_name}"

    def set_area_name(self, area_name: str) -> None:
        """Update the integration-provided name after the mower reports it."""
        name = f"{area_name} - {self.entity_description.parameter_name}"
        self._set_area_name(area_name)
        if self.hass is None or self.entity_id is None:
            return
        registry = er.async_get(self.hass)
        if registry.async_get(self.entity_id) is None:
            return
        # Only original_name is changed. A user's entity-name override must
        # survive a rename made in the Ecovacs app.
        registry.async_update_entity(self.entity_id, original_name=name)
        self.async_write_ha_state()

    @property
    @override
    def suggested_object_id(self) -> str | None:
        """Use the numeric area ID, never the mutable friendly name."""
        return self.entity_description.suggested_object_id

    @override
    async def async_added_to_hass(self) -> None:
        """Subscribe to the authoritative area snapshot."""
        await super().async_added_to_hass()
        self._subscribe(MowerAreaEvent, self._on_area_state)

    async def _on_area_state(self, event: MowerAreaEvent) -> None:
        """Project this area's parameter into the number state."""
        area = next(
            (area for area in event.areas if area.area_id == self._area_id), None
        )
        if area is None:
            self._attr_native_value = None
        else:
            self._attr_native_value = self.entity_description.value_fn(area)
            if area.name:
                self.set_area_name(area.name)
        self.async_write_ha_state()

    @override
    async def async_set_native_value(self, value: float) -> None:
        """Set one area parameter using the mower's complete raw state."""
        raw_value = self.entity_description.to_raw_fn(value)
        if raw_value is None:
            raise HomeAssistantError(
                f"{self.entity_description.parameter_name} value {value} "
                "cannot be represented by this mower"
            )

        area = area_for(self._device.events, self._area_id)
        if area is None:
            raise HomeAssistantError(
                f"Area {self._area_id} has not reported its parameters yet"
            )

        command = build_set_area_parameter(
            area, self.entity_description.raw_field, raw_value
        )
        if command is None:
            raise HomeAssistantError(
                f"Area {self._area_id} has incomplete parameters; wait for "
                "the mower to report all area settings"
            )

        await self._execute_command(command)
        self._device.events.request_refresh(MowerAreaEvent)


async def async_setup_area_sensors(
    config_entry: EcovacsMowerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    *,
    number_platform: bool = False,
) -> None:
    """Add dynamic area number entities from the HA number platform.

    The function name is retained because this dynamic entity module was
    introduced while the area values were read-only sensors. The sensor
    platform still imports it during the transition, but writable area entities
    must only be registered by the number platform.
    """
    if not number_platform:
        return

    controller = config_entry.runtime_data
    for device in controller.devices:
        if device.capabilities.device_type is not DeviceType.MOWER:
            continue
        profile = profile_for_class(device.device_info["class"])
        if profile is None or not profile.area_parameters:
            continue
        area_mapping = AREA_PARAMETER_MAPPINGS.get(profile.device_class)
        if area_mapping is None:
            continue
        _setup_device_area_sensors(
            device, config_entry, async_add_entities, area_mapping
        )


def _setup_device_area_sensors(
    device: Device,
    config_entry: EcovacsMowerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    area_mapping: AreaParameterMapping,
) -> None:
    """Project the patch-owned area state into dynamic HA entities."""
    entities: dict[str, list[EcovacsAreaNumber]] = {}

    def add_area(area: MowerArea) -> None:
        """Create the four entities for a newly discovered area."""
        if area.area_id in entities:
            return
        name = area.name or f"Area {area.area_id}"
        area_entities = [
            EcovacsAreaNumber(device, area.area_id, description, name)
            for description in area_sensor_descriptions(
                area.area_id, area_mapping=area_mapping
            )
        ]
        entities[area.area_id] = area_entities
        async_add_entities(area_entities)
        for entity in area_entities:
            entity._attr_native_value = entity.entity_description.value_fn(area)

    async def on_area_state(event: MowerAreaEvent) -> None:
        """Create missing entities and project the new authoritative state."""
        reported_ids = {area.area_id for area in event.areas}
        for area in event.areas:
            if area.area_id not in entities:
                add_area(area)
            else:
                for entity in entities[area.area_id]:
                    entity._attr_native_value = entity.entity_description.value_fn(area)
                    if area.name:
                        entity.set_area_name(area.name)
                    entity.async_write_ha_state()

        # An area removed from the mower becomes unavailable rather than being
        # silently deleted from HA. Numeric IDs remain stable, and entity
        # removal is intentionally a user-visible lifecycle action.
        for area_id, area_entities in entities.items():
            if area_id not in reported_ids:
                for entity in area_entities:
                    entity._attr_native_value = None
                    entity.async_write_ha_state()

    config_entry.async_on_unload(
        device.events.subscribe(MowerAreaEvent, on_area_state)
    )
    device.events.request_refresh(MowerAreaEvent)
