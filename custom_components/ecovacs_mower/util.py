"""Ecovacs util functions.

Forked from Home Assistant core (``homeassistant/components/ecovacs/util.py``).
``get_options`` is still removed — it is only used by the select platform, which
this integration does not have. ``get_supported_entities`` was restored in phase
2: the sensor, switch, number and button platforms use it to build their
entities from ``EcovacsCapabilityEntityDescription``. ``get_name_key`` was
restored in the same phase for the event platform, which maps ``CleanJobStatus``
to the state keys strings.json declares.
"""

from collections.abc import Mapping
from enum import Enum
import random
import string
from typing import TYPE_CHECKING, Any, cast

from homeassistant.const import CONF_DEVICE_ID
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import slugify

from .entity import (
    EcovacsCapabilityEntityDescription,
    EcovacsDescriptionEntity,
    EcovacsEntity,
)

if TYPE_CHECKING:
    from .controller import EcovacsController


def get_client_device_id(
    hass: HomeAssistant, self_hosted: bool, config: Mapping[str, Any]
) -> str:
    """Get client device id."""
    if device_id := config.get(CONF_DEVICE_ID):
        return cast(str, device_id)
    if self_hosted:
        return f"HA-{slugify(hass.config.location_name)}"

    return "".join(
        random.choice(string.ascii_uppercase + string.digits) for _ in range(8)
    )


def get_supported_entities(
    controller: "EcovacsController",
    entity_class: type[EcovacsDescriptionEntity],
    descriptions: tuple[EcovacsCapabilityEntityDescription, ...],
) -> list[EcovacsEntity]:
    """Return all supported entities for all devices."""
    return [
        entity_class(device, capability, description)
        for device in controller.devices
        for description in descriptions
        if (capability := description.capability_fn(device.capabilities))
    ]


@callback
def get_name_key(enum: Enum) -> str:
    """Return the lower case name of the enum."""
    return enum.name.lower()
