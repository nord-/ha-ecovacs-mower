"""Dynamic Home Assistant entities for mower area parameters.

Area identity and state are owned by ``deebot_patch``. This module only turns
that state into the four model-specific area parameter views. The entities are
dynamic because the mower reports its area IDs at runtime, just like the
existing beacon sensors.

Model- and firmware-specific conversion from raw device values to
human-sensible Home Assistant values lives exclusively in this HA layer. The
A1600 LiDAR Pro findings that established these mappings are deliberately scoped
to that model and to the A3000 LiDAR Pro, whose own readings matched them; the
O1200 LiDAR Pro has its own height and speed tables. The values have not been
verified on the other mower classes supported by this integration.

The four parameter views are writable on the validated models. Each write
is converted back to raw protocol values and merged with the other three
values — from the area's last reported snapshot, or from this area's own
accepted write that the mower has not reported back yet (see
``_PendingAreaWrite``) — before one complete ``setAreaParameter`` command is
sent. The entity state is
not updated optimistically; the mower must report the resulting raw values
back, whether through the unsolicited ``onAreaParameter`` push a write
normally triggers within milliseconds, or through the polled area refresh.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from itertools import pairwise
from types import MappingProxyType
from typing import Callable, override

from deebot_client.capabilities import DeviceType
from deebot_client.device import Device

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import DEGREE, EntityCategory, UnitOfLength, UnitOfSpeed
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EcovacsMowerConfigEntry
from .deebot_patch.areas import (
    MowerArea,
    MowerAreaEvent,
    area_for,
    parameter_answers,
)
from .deebot_patch.commands import SetAreaParameter
from .deebot_patch.hardware import profile_for_class
from .entity import EcovacsDescriptionEntity


@dataclass(frozen=True)
class AreaParameterLookup:
    """Validated raw-value lookup for one mower model parameter.

    ``values_by_raw`` maps each raw protocol value the app sends to its HA
    value, written out pair by pair so a table reads like the readings it was
    built from. A raw value missing from the table reads as unknown and is
    never written, so a model whose app offers only some raw values gets only
    those. Add a lookup here only when the complete raw-to-HA representation
    has been independently verified on the specific mower model. Do not reuse
    a lookup for another model merely because the Ecovacs field has the same
    name.
    """

    # Excluded from the hash: a mapping is not hashable, and the frozen
    # dataclass would otherwise generate a __hash__ that fails on first use.
    values_by_raw: Mapping[int, float | int] = field(hash=False)

    def __post_init__(self) -> None:
        """Reject tables that cannot be exposed as a HA number step.

        HA's number entity has a single step, so the values must be evenly
        spaced once sorted; the raw values themselves need not be consecutive.
        """
        # Read-only from here on: the table is validated data, shared between
        # models, and must not be changed by accident after the check below.
        object.__setattr__(
            self, "values_by_raw", MappingProxyType(dict(self.values_by_raw))
        )
        if not self.values_by_raw:
            raise ValueError("Area parameter lookup must contain at least one value")
        values = self._sorted_values()
        if len(set(values)) != len(values):
            raise ValueError("Area parameter lookup values must be distinct")
        if len(values) < 2:
            return
        # Rounded as native_step is, so distinct values a rounding apart are
        # refused here rather than give a zero step that matches nothing.
        steps = {round(b - a, 10) for a, b in pairwise(values)}
        if len(steps) != 1 or 0 in steps:
            raise ValueError("Area parameter lookup values must have one non-zero step")

    def _sorted_values(self) -> list[float]:
        """The values as floats, in order: what the step is checked and taken on."""
        return sorted(float(value) for value in self.values_by_raw.values())

    @property
    def native_min_value(self) -> float:
        """Return the lowest Home Assistant value represented."""
        return float(min(self.values_by_raw.values()))

    @property
    def native_max_value(self) -> float:
        """Return the highest Home Assistant value represented."""
        return float(max(self.values_by_raw.values()))

    @property
    def native_step(self) -> float:
        """Return the HA step represented by this lookup."""
        if len(self.values_by_raw) < 2:
            return 1.0
        values = self._sorted_values()
        return round(values[1] - values[0], 10)

    def to_native(self, raw_value: int) -> float | int | None:
        """Convert a raw mower value to its HA representation."""
        return self.values_by_raw.get(raw_value)

    def to_raw(self, native_value: float) -> int | None:
        """Convert an HA value to its raw mower representation.

        Within half a step, not by equality: the frontend slider sends
        ``min + step * n``, and ``0.40 + 0.05 * 4`` is ``0.6000000000000001``.
        """
        tolerance = self.native_step / 2
        for raw_value, value in self.values_by_raw.items():
            if abs(float(value) - native_value) < tolerance:
                return raw_value
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
# representations. The A3000 LiDAR Pro shares all of them, and the angle
# formula below, through ``_A1600_MAPPING``; the O1200 LiDAR Pro shares the
# obstacle height and the angle formula. A change made on new A1600 evidence
# therefore needs evidence from every model that shares what it changes. Each
# pair is a raw value and its HA value, so each table is the single source of
# truth for conversion in both directions and for the HA number entity's
# min/max/step metadata.
_A1600_MOW_HEIGHT = AreaParameterLookup(
    {1: 9, 2: 8, 3: 7, 4: 6, 5: 5, 6: 4, 7: 3},
)

_A1600_CUT_SPEED = AreaParameterLookup(
    {1: 0.70, 2: 0.65, 3: 0.60, 4: 0.55, 5: 0.50, 6: 0.45, 7: 0.40},
)

_A1600_OBSTACLE_HEIGHT = AreaParameterLookup(
    {1: 10, 2: 15, 3: 20},
)


def _a1600_cut_angle(wire_angle: int) -> int | None:
    """Convert the A1600 wire-space angle to the app-space angle.

    Confirmed on the A1600 LiDAR Pro and the A3000 LiDAR Pro. The observed
    symmetric conversion is ``app = (270 - wire) mod 360``; the same formula
    converts the app value back to wire space.

    Any wire angle is shown, also one whose app value lies above the 180 the
    app can set (see ``_a1600_cut_angle_to_raw``): an area set to 181 before
    that limit existed should read 181, not be quietly folded to 1.
    """
    if wire_angle not in range(360):
        return None
    return (270 - wire_angle) % 360


def _a1600_cut_angle_to_raw(value: float) -> int | None:
    """Convert an A1600 app-space cutting direction to wire-space angle.

    Whole degrees from 0 to 180 only, as in the Ecovacs app on every model
    that shares this formula: on the A3000 LiDAR Pro every angle the app sent
    lay within 0-180 (issue #131), and on the O1200 LiDAR Pro the raw values of
    its left, straight and right, and those set in between, all land inside it
    (issue #23). A cutting direction is a line, not a bearing, so 0-180 covers
    every stripe pattern; the device-wide ``cut_direction`` in ``number.py``
    has the same range. The A1600 accepts a higher value written here, and the
    app then shows it, but the app never sends one and what the mower does
    with it is untested.
    """
    if value not in range(181):
        return None
    return int((270 - value) % 360)


_A1600_CUT_ANGLE = AreaParameterFormula(
    to_native=_a1600_cut_angle,
    to_raw=_a1600_cut_angle_to_raw,
    native_min_value=0,
    native_max_value=180,
    native_step=1,
)

# The O1200 LiDAR Pro's own readings (issue #23). Its height runs in half
# centimetres, from raw 1 = 8 cm down to raw 11 = 3 cm: raw 1 and 8-11 were
# read, raw 2-7 follow the same steps. Its app offers only two speeds, so only
# those two raw values exist here: raw 5 and 6, in between, are never written.
_O1200_MOW_HEIGHT = AreaParameterLookup(
    {
        1: 8.0, 2: 7.5, 3: 7.0, 4: 6.5, 5: 6.0, 6: 5.5,
        7: 5.0, 8: 4.5, 9: 4.0, 10: 3.5, 11: 3.0,
    },
)

_O1200_CUT_SPEED = AreaParameterLookup(
    {4: 0.50, 7: 0.35},
)

_A1600_MAPPING = AreaParameterMapping(
    mow_height=_A1600_MOW_HEIGHT,
    cut_speed=_A1600_CUT_SPEED,
    obstacle_height=_A1600_OBSTACLE_HEIGHT,
    cut_angle=_A1600_CUT_ANGLE,
)


# These mappings are presentation semantics, not protocol semantics. Keep them
# in the HA layer and add a class only after its raw-value representation has
# been verified independently. Do not infer that another GOAT class shares the
# A1600 representation merely because its protocol fields have the same names.
#
# To support another model, add one explicit entry here. Populate each lookup
# from that model's independently verified raw values, one pair per value, and
# provide its verified angle formula (or a separate formula definition). The
# generic entity code below derives conversion and number limits from these
# mappings; no model-specific ranges belong in ``area_number_descriptions``.
AREA_PARAMETER_MAPPINGS: dict[str, AreaParameterMapping] = {
    "e4gqia": _A1600_MAPPING,
    # The A3000 LiDAR Pro's own readings (issue #131) matched the A1600's at
    # every raw value, and writes from Home Assistant landed as the app shows
    # them (PR #137), so it shares the A1600's mapping rather than a copy.
    "51rcxt": _A1600_MAPPING,
    # The O1200 LiDAR Pro's obstacle heights matched the A1600's (issue #23). Its
    # app labels the direction left, straight and right, whose raw 270, 180 and
    # 90 fall on the A1600 formula's 0, 90 and 180; the in-between raw values
    # set in the app (141, 177) land inside 0-180 too. Its heights and speeds
    # did not match, so those are its own.
    "2i0fns": AreaParameterMapping(
        mow_height=_O1200_MOW_HEIGHT,
        cut_speed=_O1200_CUT_SPEED,
        obstacle_height=_A1600_OBSTACLE_HEIGHT,
        cut_angle=_A1600_CUT_ANGLE,
    ),
}


def _raw_values_of(area: MowerArea) -> dict[str, int | None]:
    """Return one area's four raw parameter values as a plain mapping."""
    return {
        "mow_height_level": area.mow_height_level,
        "cut_mode": area.cut_mode,
        "obstacle_height": area.obstacle_height,
        "angle": area.angle,
    }


def build_set_area_parameter(
    area_id: str, raw_values: dict[str, int | None], raw_field: str, raw_value: int
) -> SetAreaParameter | None:
    """Merge one raw change into a complete area write.

    ``raw_values`` is either the mower's last reported snapshot or this
    area's accepted, not yet reported write — see ``_PendingAreaWrite``. The
    mower requires all four raw values. Returning no command for an
    incomplete snapshot prevents a writable HA entity from inventing defaults
    for fields that have not been reported by the mower yet.
    """
    if raw_field not in raw_values:
        return None
    if any(parameter is None for parameter in raw_values.values()):
        return None

    merged = {**raw_values, raw_field: raw_value}
    return SetAreaParameter(
        area_id=area_id,
        mow_height_level=merged["mow_height_level"],
        cut_mode=merged["cut_mode"],
        obstacle_height=merged["obstacle_height"],
        angle=merged["angle"],
    )


@dataclass
class _PendingAreaWrite:
    """Tracks one area's most recent write the mower has not reported back.

    Shared by all four ``EcovacsAreaNumber`` entities of one area. Once set,
    ``raw_values`` holds the complete four-value payload the mower accepted;
    the next write merges against it instead of the reported snapshot, which
    may not show it yet — otherwise a second fast write to a different field
    would silently revert the first.

    ``lock`` serializes the area's writes, so a second one that overlaps the
    first (``parallel:`` in a script, two automations) waits for it and merges
    against its result instead of reading the same snapshot. Only a write the
    mower accepted is recorded: ``_execute_command`` warns rather than raises
    on an unconfirmed command, and values the mower never took must not be
    re-sent with the next change.

    ``answers`` is this area's ``parameter_answers()`` from before the write
    went out. The write stops being the better base as soon as the mower
    reports this area's parameters again — through its ``onAreaParameter`` push, normally within
    roughly 150ms, or the refresh that follows every write — even when that
    report repeats the last one exactly and no ``MowerAreaEvent`` is
    published for it. Counting from before the send also covers a push that
    arrives while the write is still awaiting its answer.
    """

    raw_values: dict[str, int | None] | None = None
    answers: int = -1
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


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


def area_number_descriptions(
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
            # A direction is usually a specific number of degrees matched to
            # the lawn. With 181 steps HA's automatic mode would draw a
            # slider, which makes an exact value fiddly to set.
            mode=NumberMode.BOX,
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
        pending: _PendingAreaWrite,
    ) -> None:
        """Initialize the dynamic area entity."""
        super().__init__(device, device.capabilities, description)
        self._area_id = area_id
        self._area_present = True
        self._pending = pending
        self._set_area_name(area_name)
        self._attr_icon = description.icon

    @property
    @override
    def available(self) -> bool:
        """Combine device availability with this area still being reported.

        A removed area must not remain a live, writable control just because
        the device itself is still online.
        """
        return self._attr_available and self._area_present

    def _set_area_name(self, area_name: str) -> None:
        """Set the integration-provided name without changing identity."""
        # The friendly area name is deliberately part of the integration's
        # original name. HA users can override the entity name in the registry.
        self._attr_name = f"{area_name} - {self.entity_description.parameter_name}"

    def set_area_name(self, area_name: str) -> None:
        """Update the integration-provided name after the mower reports it.

        Every area report passes the name, and it rarely changes, so an
        unchanged one touches nothing. The caller writes the state.
        """
        name = f"{area_name} - {self.entity_description.parameter_name}"
        if name == self._attr_name:
            return
        self._set_area_name(area_name)
        if self.hass is None or self.entity_id is None:
            return
        registry = er.async_get(self.hass)
        if registry.async_get(self.entity_id) is None:
            return
        # Only original_name is changed. A user's entity-name override must
        # survive a rename made in the Ecovacs app.
        registry.async_update_entity(self.entity_id, original_name=name)

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
        """Project this area's parameter, and whether the area still exists.

        The only place an area entity's state is written. A disabled entity
        never subscribes, so nothing ever writes the state of one HA did not
        add — which ``async_write_ha_state`` refuses with a ``RuntimeError``.
        """
        area = next(
            (area for area in event.areas if area.area_id == self._area_id), None
        )
        # An area removed from the mower becomes unavailable rather than being
        # silently deleted from HA. Numeric IDs remain stable, and entity
        # removal is intentionally a user-visible lifecycle action.
        self._area_present = area is not None
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

        pending = self._pending
        async with pending.lock:
            answers = parameter_answers(self._device.events, self._area_id)
            if pending.raw_values is not None and pending.answers == answers:
                current = pending.raw_values
            else:
                area = area_for(self._device.events, self._area_id)
                if area is None:
                    raise HomeAssistantError(
                        f"Area {self._area_id} has not reported its parameters yet"
                    )
                current = _raw_values_of(area)

            command = build_set_area_parameter(
                self._area_id, current, self.entity_description.raw_field, raw_value
            )
            if command is None:
                raise HomeAssistantError(
                    f"Area {self._area_id} has incomplete parameters; wait for "
                    "the mower to report all area settings"
                )

            if await self._execute_command(command):
                pending.raw_values = {
                    **current,
                    self.entity_description.raw_field: raw_value,
                }
                pending.answers = answers

        self._device.events.request_refresh(MowerAreaEvent)


async def async_setup_area_numbers(
    config_entry: EcovacsMowerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add dynamic area number entities from the HA number platform."""
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
        _setup_device_area_numbers(
            device, config_entry, async_add_entities, area_mapping
        )


def _setup_device_area_numbers(
    device: Device,
    config_entry: EcovacsMowerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    area_mapping: AreaParameterMapping,
) -> None:
    """Create the four entities of every area the mower reports.

    Discovery only: each entity projects its own area from the same event, see
    ``EcovacsAreaNumber._on_area_state``.
    """
    known: set[str] = set()

    def add_area(area: MowerArea) -> None:
        """Create the four entities for a newly discovered area."""
        if area.area_id in known:
            return
        name = area.name or f"Area {area.area_id}"
        area_pending = _PendingAreaWrite()
        area_entities = [
            EcovacsAreaNumber(device, area.area_id, description, name, area_pending)
            for description in area_number_descriptions(
                area.area_id, area_mapping=area_mapping
            )
        ]
        known.add(area.area_id)
        async_add_entities(area_entities)
        for entity in area_entities:
            entity._attr_native_value = entity.entity_description.value_fn(area)

    async def on_area_state(event: MowerAreaEvent) -> None:
        """Create the entities of every area not seen before."""
        for area in event.areas:
            add_area(area)

    config_entry.async_on_unload(
        device.events.subscribe(MowerAreaEvent, on_area_state)
    )
