"""Dynamic Home Assistant entities for mower area parameters.

Area identity and state are owned by ``deebot_patch``. This module only turns
that state into model-specific area parameter views. The entities are
dynamic because the mower reports its area IDs at runtime, just like the
existing beacon sensors.

Two tables decide what a mower gets, and nothing else here is specific to a
parameter or a model:

- ``AREA_PARAMETERS`` says what each known parameter is in Home Assistant
  on every model: its name, unit, icon and entity key.
- ``AREA_PARAMETER_VALUE_MAPPINGS`` has one self-contained entry per validated
  mower class, a ``ModelValueMappings``. It names the parameters that model
  has and the conversion of each between raw values and Home Assistant
  values. A model without a parameter leaves it out, and gets no entity for
  it.

Model- and firmware-specific conversion from raw device values to
human-sensible Home Assistant values lives exclusively in this HA layer. Each
class's entry is scoped to that class: its values have not been verified on
any other mower class supported by this integration.

A model may also keep the same settings for the whole mower, in the app's
Auto mode, each read and written with its own command
(``deebot_patch/commands.py``). Those are treated as one more area,
``auto``, with its own entities (``EcovacsAutoNumber``) from the same
parameter descriptions and value conversions, and validated per model in the
same entry, under ``auto``.

The parameter views are writable. Each write is converted back to a raw
value and merged with the area's other raw values — from the area's last
reported snapshot, or from this area's own accepted write that the mower has
not reported back yet (see ``_PendingAreaWrite``) — before one
``setAreaParameter`` command carrying all of them is sent. The entity state
is not updated optimistically; the mower must report the resulting raw values
back, whether through the unsolicited ``onAreaParameter`` push a write
normally triggers within milliseconds, or through the polled area refresh.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from itertools import pairwise
from types import MappingProxyType
from typing import override

from deebot_client.capabilities import DeviceType
from deebot_client.device import Device
from deebot_client.events.base import Event

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
from .deebot_patch.commands import AUTO_PARAMETERS, AutoParameter, SetAreaParameter
from .deebot_patch.hardware import profile_for_class
from .entity import EcovacsDescriptionEntity


@dataclass(frozen=True)
class AreaParameterLookup:
    """Validated raw-value lookup for one parameter of one mower model.

    ``values_by_raw`` maps each raw protocol value the app sends to its HA
    value, written out pair by pair so a table reads like the readings it was
    built from. A raw value missing from the table reads as unknown and is
    never written, so a model whose app offers only some raw values gets only
    those. Write a lookup only when the complete raw-to-HA representation has
    been independently verified on the specific mower model.
    """

    # Excluded from the hash: a mapping is not hashable, and the frozen
    # dataclass would otherwise generate a __hash__ that fails on first use.
    values_by_raw: Mapping[int, float | int] = field(hash=False)

    def __post_init__(self) -> None:
        """Reject tables that cannot be exposed as a HA number step.

        HA's number entity has a single step, so the values must be evenly
        spaced once sorted; the raw values themselves need not be consecutive.
        """
        # Read-only from here on: the table is validated data and must not be
        # changed by accident after the check below.
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
class AreaParameterAngle:
    """Validated cutting-direction conversion for one mower model.

    A formula rather than a lookup: the app's angle is a mirror of the wire
    angle, ``app = (wire_at_zero - wire) mod 360``, and the same formula
    converts the app's value back. ``wire_at_zero`` is the wire angle the app
    shows as 0°.

    Any wire angle is shown, also one whose app value lies above
    ``native_max_value``: an area set above it from Home Assistant before the
    limit existed should read as what it is, not be quietly folded back.

    Writes take whole degrees from 0 to ``native_max_value`` only, the range
    the model's app offers. A cutting direction is a line, not a bearing, so
    0-180 covers every stripe pattern; the device-wide ``cut_direction`` in
    ``number.py`` has the same range.
    """

    wire_at_zero: int
    native_max_value: int

    native_min_value = 0
    native_step = 1

    def to_native(self, wire_angle: int) -> int | None:
        """Convert a wire-space angle to the app-space angle."""
        if wire_angle not in range(360):
            return None
        return (self.wire_at_zero - wire_angle) % 360

    def to_raw(self, value: float) -> int | None:
        """Convert an app-space angle to the wire-space angle."""
        if value not in range(self.native_max_value + 1):
            return None
        return int((self.wire_at_zero - value) % 360)


type AreaParameterValueMapping = AreaParameterLookup | AreaParameterAngle


@dataclass(frozen=True, kw_only=True)
class AreaParameter:
    """What one known area parameter is in Home Assistant, on every model.

    ``key`` is the wire key, one of ``deebot_patch.areas.AREA_PARAMETER_KEYS``.
    ``suffix`` ends the entity key and the suggested object ID: it is part of
    every existing entity's unique ID, so it never changes.
    """

    key: str
    suffix: str
    name: str
    unit: str
    icon: str
    mode: NumberMode = NumberMode.AUTO


# Every parameter a model may have, in the order the entities are created.
# A parameter here gives no model an entity: only a model's own value
# mapping does.
AREA_PARAMETERS: tuple[AreaParameter, ...] = (
    AreaParameter(
        key="mowHeightLevel",
        suffix="cutting_height",
        name="Cutting height",
        unit=UnitOfLength.CENTIMETERS,
        icon="mdi:grass",
    ),
    AreaParameter(
        key="cutMode",
        suffix="mowing_speed",
        name="Mowing speed",
        unit=UnitOfSpeed.METERS_PER_SECOND,
        icon="mdi:speedometer",
    ),
    AreaParameter(
        key="obstacleHeight",
        suffix="obstacle_height",
        name="Obstacle height",
        unit=UnitOfLength.CENTIMETERS,
        icon="mdi:format-vertical-align-top",
    ),
    AreaParameter(
        key="angle",
        suffix="cut_direction",
        name="Cutting direction",
        unit=DEGREE,
        icon="mdi:angle-acute",
        # A direction is usually a specific number of degrees matched to the
        # lawn. With 181 steps HA's automatic mode would draw a slider, which
        # makes an exact value fiddly to set.
        mode=NumberMode.BOX,
    ),
)


@dataclass(frozen=True, kw_only=True)
class ModelValueMappings:
    """The value mappings of one validated mower class.

    ``areas`` maps the parameters the model has per saved area, by wire key,
    to their conversion. A parameter the model does not have is left out, and
    an empty ``areas`` gives the model no area entities.

    ``auto`` does the same for the mower-wide settings of the app's Auto
    mode, by the area wire key each corresponds to. It is validated on its
    own: the commands that read and write it differ from the area ones, so a
    model's ``areas`` says nothing about its ``auto``, and the other way
    round. An empty ``auto`` gives the model no Auto entities.
    """

    # Excluded from the hash: a mapping is not hashable, and the frozen
    # dataclass would otherwise generate a __hash__ that fails on first use.
    areas: Mapping[str, AreaParameterValueMapping] = field(
        default_factory=dict, hash=False
    )
    auto: Mapping[str, AreaParameterValueMapping] = field(
        default_factory=dict, hash=False
    )

    def __post_init__(self) -> None:
        """Freeze the tables: they are validated data, shared by every entity."""
        object.__setattr__(self, "areas", MappingProxyType(dict(self.areas)))
        object.__setattr__(self, "auto", MappingProxyType(dict(self.auto)))


# One entry per validated mower class, complete in itself: the parameters
# that model has, by wire key, and each one's conversion. Each entry is
# written out in full, also where two models' values are the same, so a
# model's owner reads one entry and a change to one model's values can never
# change another's.
#
# These mappings are presentation semantics, not protocol semantics. Add a
# class only after its raw-value representation has been verified on that
# class, one pair per value, and leave out a parameter the model does not
# have. Do not infer that another GOAT class shares a representation merely
# because its protocol fields have the same names. The generic entity code
# below derives conversion and number limits from these entries; no
# model-specific values belong anywhere else.
AREA_PARAMETER_VALUE_MAPPINGS: dict[str, ModelValueMappings] = {
    # GOAT A1600 LiDAR Pro.
    "e4gqia": ModelValueMappings(
        areas={
            # Cutting height, cm.
            "mowHeightLevel": AreaParameterLookup(
                {1: 9, 2: 8, 3: 7, 4: 6, 5: 5, 6: 4, 7: 3}
            ),
            # Mowing speed, m/s.
            "cutMode": AreaParameterLookup(
                {1: 0.70, 2: 0.65, 3: 0.60, 4: 0.55, 5: 0.50, 6: 0.45, 7: 0.40}
            ),
            # Obstacle height, cm.
            "obstacleHeight": AreaParameterLookup({1: 10, 2: 15, 3: 20}),
            # Cutting direction. The mower accepts an app value above 180 written
            # from Home Assistant, and the app then shows it, but the app never
            # sends one and what the mower does with it is untested.
            "angle": AreaParameterAngle(wire_at_zero=270, native_max_value=180),
        },
    ),
    # GOAT A3000 LiDAR Pro. Its own readings (issue #131) matched the A1600's
    # at every raw value, and writes from Home Assistant landed as the app
    # shows them (PR #137).
    "51rcxt": ModelValueMappings(
        areas={
            # Cutting height, cm.
            "mowHeightLevel": AreaParameterLookup(
                {1: 9, 2: 8, 3: 7, 4: 6, 5: 5, 6: 4, 7: 3}
            ),
            # Mowing speed, m/s.
            "cutMode": AreaParameterLookup(
                {1: 0.70, 2: 0.65, 3: 0.60, 4: 0.55, 5: 0.50, 6: 0.45, 7: 0.40}
            ),
            # Obstacle height, cm.
            "obstacleHeight": AreaParameterLookup({1: 10, 2: 15, 3: 20}),
            # Cutting direction. Every angle the app sent lay within 0-180
            # (issue #131).
            "angle": AreaParameterAngle(wire_at_zero=270, native_max_value=180),
        },
    ),
    # GOAT O1200 LiDAR Pro, read with the app (issue #23).
    "2i0fns": ModelValueMappings(
        areas={
            # Cutting height, cm, in half centimetres from raw 1 = 8 cm down to
            # raw 11 = 3 cm. Raw 1 and 7-11 were read, 5.5 cm = raw 6 was written
            # from Home Assistant and confirmed (PR #151); raw 2-5 follow the
            # steps.
            "mowHeightLevel": AreaParameterLookup(
                {
                    1: 8.0, 2: 7.5, 3: 7.0, 4: 6.5, 5: 6.0, 6: 5.5,
                    7: 5.0, 8: 4.5, 9: 4.0, 10: 3.5, 11: 3.0,
                }
            ),
            # Mowing speed, m/s. The app offers only Fine (raw 7) and Efficient
            # (raw 4); raw 5 and 6, in between, are never written.
            "cutMode": AreaParameterLookup({4: 0.50, 7: 0.35}),
            # Obstacle height, cm.
            "obstacleHeight": AreaParameterLookup({1: 10, 2: 15, 3: 20}),
            # Cutting direction. The app labels it left, straight and right, as
            # seen from the mower leaving the dock: raw 270, 180 and 90, so 0, 90
            # and 180 here. The in-between raw values set in the app (141, 177)
            # land inside 0-180 too.
            "angle": AreaParameterAngle(wire_at_zero=270, native_max_value=180),
        },
    ),
    # GOAT A1600 RTK, read with the app (issue #23, firmware 1.17.11). Its
    # areas have no cutting direction: they report no angle, the app's own
    # setAreaParameter carries none, and the direction is set for the whole
    # mower instead. So it gets no per-area direction entity, and its writes
    # carry no angle.
    #
    # The per-area values in the owner's log are height 1 and 6, speed 1 and
    # 2, obstacle height 1 and 3. Their app values come from the same app
    # settings read in Auto mode on this mower, whose levels run the same
    # way: height 9 cm = 1, 4 cm = 6, 3 cm = 7; Quick = 1, Delicate = 2;
    # 10, 15, 20 cm = 1, 2, 3.
    #
    # The Auto mode settings were read back with the Log area parameters
    # button while the owner stepped through every value the app offers
    # (#163): each one reached Home Assistant through the followed app write
    # within a second, and the reads answered with the same raw values.
    "xmp9ds": ModelValueMappings(
        areas={
            # Cutting height, cm. Raw 2-5 follow the one-centimetre steps.
            "mowHeightLevel": AreaParameterLookup(
                {1: 9, 2: 8, 3: 7, 4: 6, 5: 5, 6: 4, 7: 3}
            ),
            # Mowing speed, m/s. The app offers two only: Quick and Delicate.
            "cutMode": AreaParameterLookup({1: 0.70, 2: 0.40}),
            # Obstacle height, cm.
            "obstacleHeight": AreaParameterLookup({1: 10, 2: 15, 3: 20}),
        },
        auto={
            # Cutting height, cm, every value read.
            "mowHeightLevel": AreaParameterLookup(
                {1: 9, 2: 8, 3: 7, 4: 6, 5: 5, 6: 4, 7: 3}
            ),
            # Mowing speed, m/s. The app offers two only: Quick and Delicate.
            "cutMode": AreaParameterLookup({1: 0.70, 2: 0.40}),
            # Obstacle height, cm: the app's short grass, normal and high
            # grass environments.
            "obstacleHeight": AreaParameterLookup({1: 10, 2: 15, 3: 20}),
            # Cutting direction. The app offers left, straight and right, raw
            # 270, 180 and 90: 0, 90 and 180 here, as on the O1200 LiDAR Pro's
            # areas. An angle in between is offered as there.
            "angle": AreaParameterAngle(wire_at_zero=270, native_max_value=180),
        },
    ),
}

# The id the mower-wide (Auto mode) settings are shown under, as one more
# area. Not numeric, so it can never be one of the mower's own area ids.
AUTO_AREA_ID = "auto"

# Each mower-wide parameter's commands and event, by wire key.
_AUTO_PARAMETERS: dict[str, AutoParameter] = {
    parameter.key: parameter for parameter in AUTO_PARAMETERS
}


def build_set_area_parameter(
    area_id: str,
    raw_values: Mapping[str, int],
    required: tuple[str, ...],
    key: str,
    raw_value: int,
) -> SetAreaParameter | None:
    """Merge one raw change into a write of the area's raw values.

    ``raw_values`` is either the mower's last reported snapshot or this
    area's accepted, not yet reported write — see ``_PendingAreaWrite``.

    The write carries every value in ``raw_values``, the changed one
    replaced: the area's whole stored set, as the Ecovacs app sends it, so a
    parameter the mower reports but this model's mapping leaves out goes back
    as the mower reported it instead of being dropped. ``required`` is the
    model's mapped keys. Returning no command while one of them is unknown
    keeps a writable HA entity from inventing a default for a value the
    mower has not reported yet.
    """
    if any(required_key not in raw_values for required_key in required):
        return None
    return SetAreaParameter(
        area_id=area_id, parameters={**raw_values, key: raw_value}
    )


@dataclass
class _PendingAreaWrite:
    """Tracks one area's most recent write the mower has not reported back.

    Shared by all ``EcovacsAreaNumber`` entities of one area. Once set,
    ``raw_values`` holds the complete payload the mower accepted; the next
    write merges against it instead of the reported snapshot, which may not
    show it yet — otherwise a second fast write to a different parameter
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

    raw_values: dict[str, int] | None = None
    answers: int = -1
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(kw_only=True, frozen=True)
class EcovacsAreaNumberEntityDescription(NumberEntityDescription):
    """Describe one dynamic writable view of one mower area.

    ``area_keys`` are all the keys the model maps, which a write needs to
    know before it may be sent; ``parameter_key`` is this view's own.
    """

    parameter_key: str
    parameter_name: str
    value_mapping: AreaParameterValueMapping
    area_keys: tuple[str, ...]
    suggested_object_id: str | None = None


def area_number_descriptions(
    area_id: str,
    *,
    value_mappings: Mapping[str, AreaParameterValueMapping],
) -> tuple[EcovacsAreaNumberEntityDescription, ...]:
    """Return one writable view per parameter the model's mapping names.

    In ``AREA_PARAMETERS`` order, whatever order the mapping lists them
    in, so every model's entities are created alike.
    """
    parameters = tuple(
        parameter for parameter in AREA_PARAMETERS if parameter.key in value_mappings
    )
    area_keys = tuple(parameter.key for parameter in parameters)
    return tuple(
        EcovacsAreaNumberEntityDescription(
            key=f"area_{area_id}_{parameter.suffix}",
            name=parameter.name,
            parameter_key=parameter.key,
            parameter_name=parameter.name,
            value_mapping=value_mappings[parameter.key],
            area_keys=area_keys,
            # Keep the numeric area ID in the suggested object ID so newly
            # created entities use a stable area-ID-based object ID instead of
            # depending on the mower's user-editable friendly name.
            suggested_object_id=f"{area_id}_{parameter.suffix}",
            entity_category=EntityCategory.CONFIG,
            native_min_value=value_mappings[parameter.key].native_min_value,
            native_max_value=value_mappings[parameter.key].native_max_value,
            native_step=value_mappings[parameter.key].native_step,
            native_unit_of_measurement=parameter.unit,
            icon=parameter.icon,
            mode=parameter.mode,
        )
        for parameter in parameters
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
            self._attr_native_value = self.native_value_of(area)
            if area.name:
                self.set_area_name(area.name)
        self.async_write_ha_state()

    def native_value_of(self, area: MowerArea) -> float | int | None:
        """This view's HA value in one area report, or None if it has none."""
        raw_value = area.parameters.get(self.entity_description.parameter_key)
        if raw_value is None:
            return None
        return self.entity_description.value_mapping.to_native(raw_value)

    @override
    async def async_set_native_value(self, value: float) -> None:
        """Set one area parameter using the mower's complete raw state."""
        description = self.entity_description
        raw_value = description.value_mapping.to_raw(value)
        if raw_value is None:
            raise HomeAssistantError(
                f"{description.parameter_name} value {value} "
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
                current = dict(area.parameters)

            command = build_set_area_parameter(
                self._area_id,
                current,
                description.area_keys,
                description.parameter_key,
                raw_value,
            )
            if command is None:
                raise HomeAssistantError(
                    f"Area {self._area_id} has incomplete parameters; wait for "
                    "the mower to report all area settings"
                )

            if await self._execute_command(command):
                pending.raw_values = {**current, description.parameter_key: raw_value}
                pending.answers = answers

        self._device.events.request_refresh(MowerAreaEvent)


def auto_parameter_keys(device_class: str) -> frozenset[str]:
    """The mower-wide parameters validated for one class, by wire key."""
    mappings = AREA_PARAMETER_VALUE_MAPPINGS.get(device_class)
    return frozenset(mappings.auto) if mappings is not None else frozenset()


class EcovacsAutoNumber(EcovacsDescriptionEntity, NumberEntity):
    """Expose one writable mower-wide (Auto mode) parameter.

    The same description and value conversion as an area's entity, but none
    of an area's machinery: the Auto settings always exist, have a fixed
    name, and each is written with its own command, so there is no area to
    disappear, no snapshot to merge into and nothing to serialise. Each
    setting arrives as its own event, which only this entity reads.
    """

    entity_description: EcovacsAreaNumberEntityDescription

    def __init__(
        self, device: Device, description: EcovacsAreaNumberEntityDescription
    ) -> None:
        """Initialize the mower-wide entity."""
        super().__init__(device, device.capabilities, description)
        self._attr_name = f"Auto - {description.parameter_name}"
        self._attr_icon = description.icon
        self._parameter = _AUTO_PARAMETERS[description.parameter_key]

    @property
    @override
    def suggested_object_id(self) -> str | None:
        """Use the fixed ``auto`` id, as an area entity uses its numeric one."""
        return self.entity_description.suggested_object_id

    @override
    async def async_added_to_hass(self) -> None:
        """Follow this setting's event, and ask for the setting once.

        Asked here rather than through the device's refresh: the refresh is
        wired per class in the patch layer, which cannot see this table.
        """
        await super().async_added_to_hass()
        self._subscribe(self._parameter.event, self._on_value)
        self.hass.async_create_task(self._execute_command(self._parameter.read()))

    async def _on_value(self, event: Event) -> None:
        raw_value = getattr(event, self._parameter.field)
        self._attr_native_value = self.entity_description.value_mapping.to_native(
            raw_value
        )
        self.async_write_ha_state()

    @override
    async def async_set_native_value(self, value: float) -> None:
        """Write the setting, then read it back.

        The state is not set here: it follows what the mower reports, through
        the read that confirms the write, or the mower's own echo of it.
        """
        description = self.entity_description
        raw_value = description.value_mapping.to_raw(value)
        if raw_value is None:
            raise HomeAssistantError(
                f"{description.parameter_name} value {value} "
                "cannot be represented by this mower"
            )
        if await self._execute_command(self._parameter.write(raw_value)):
            await self._execute_command(self._parameter.read())


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
        if profile is None:
            continue
        mappings = AREA_PARAMETER_VALUE_MAPPINGS.get(profile.device_class)
        if mappings is None:
            continue
        if profile.area_parameters and mappings.areas:
            _setup_device_area_numbers(
                device, config_entry, async_add_entities, mappings.areas
            )
        if mappings.auto:
            async_add_entities(
                EcovacsAutoNumber(device, description)
                for description in area_number_descriptions(
                    AUTO_AREA_ID, value_mappings=mappings.auto
                )
            )


def _setup_device_area_numbers(
    device: Device,
    config_entry: EcovacsMowerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    value_mappings: Mapping[str, AreaParameterValueMapping],
) -> None:
    """Create the entities of every area the mower reports.

    Discovery only: each entity projects its own area from the same event, see
    ``EcovacsAreaNumber._on_area_state``.
    """
    known: set[str] = set()

    def add_area(area: MowerArea) -> None:
        """Create the entities for a newly discovered area."""
        if area.area_id in known:
            return
        name = area.name or f"Area {area.area_id}"
        area_pending = _PendingAreaWrite()
        area_entities = [
            EcovacsAreaNumber(device, area.area_id, description, name, area_pending)
            for description in area_number_descriptions(
                area.area_id, value_mappings=value_mappings
            )
        ]
        known.add(area.area_id)
        async_add_entities(area_entities)
        for entity in area_entities:
            entity._attr_native_value = entity.native_value_of(area)

    async def on_area_state(event: MowerAreaEvent) -> None:
        """Create the entities of every area not seen before."""
        for area in event.areas:
            add_area(area)

    config_entry.async_on_unload(
        device.events.subscribe(MowerAreaEvent, on_area_state)
    )
