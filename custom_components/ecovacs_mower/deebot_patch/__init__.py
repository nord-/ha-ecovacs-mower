"""Isolated coupling to deebot-client's internal registries.

This is the only module in the integration allowed to touch private parts of
deebot-client. If the library is swapped for a vendored client, this folder is
the only one that needs rewriting.
"""

from __future__ import annotations

from importlib.metadata import version
import logging
from typing import NoReturn

from deebot_client.capabilities import Capabilities
from deebot_client.events import StateEvent
from deebot_client.hardware import _DEVICES
from deebot_client.messages.json import MESSAGES

from .authentication import AccountAuthenticator
from .commands import CleanMower, GetCleanInfoMower, MowerStateRefresh, has_family
from .families import attempted_family_name
from .hardware import SUPPORTED_CLASSES, ZONE_AREA_CLASSES, patch_device_info
from .map_messages import (
    OnArI,
    OnMapTrace,
    OnMapTrack,
    OnMI,
    OnSpecialContour,
)
from .messages import (
    OnChargeInfo,
    OnChargeState,
    OnCleanInfo,
    OnMowBorderStart,
    OnMowBorderStop,
    OnMowScheduleStart,
    OnMowScheduleStop,
    OnMowSpotAreaStart,
    OnMowSpotAreaStop,
    OnPos,
    OnProtectState,
    OnRainDelay,
    OnScheduleTaskInfo,
    OnStatsMower,
    OnUwb,
)
from .state_precedence import register as register_mower_bus
from .zonal import MowArea

__all__ = [
    "SUPPORTED_CLASSES",
    "AccountAuthenticator",
    "CleanMower",
    "GetCleanInfoMower",
    "MowerStateRefresh",
    "PatchContractError",
    "apply",
    "attempted_family_name",
    "has_family",
    "patch_device_info",
    "register_mower_bus",
    "verify_capabilities",
]

_LOGGER = logging.getLogger(__name__)


class PatchContractError(Exception):
    """deebot-client does not look like the patch layer expects."""


def _fail(what: str) -> NoReturn:
    installed = version("deebot-client")
    raise PatchContractError(
        f"deebot-client {installed} does not match what ecovacs_mower expects: "
        f"{what}. The integration refuses to start rather than silently stop "
        f"reporting the mower's state. Report at "
        f"https://github.com/nord-/ha-ecovacs-mower/issues"
    )


def apply() -> None:
    """Register our message handlers. Idempotent."""
    if not isinstance(_DEVICES, dict):
        _fail("deebot_client.hardware._DEVICES is not a dict")
    if not isinstance(MESSAGES, dict):
        _fail("deebot_client.messages.json.MESSAGES is not a dict")

    # Mutated in place: messages/__init__.py holds a reference to the same
    # object, so a rebinding would not be visible in get_message().
    for message in (
        OnChargeInfo,
        OnChargeState,
        OnCleanInfo,
        OnMowBorderStart,
        OnMowBorderStop,
        OnMowScheduleStart,
        OnMowScheduleStop,
        OnMowSpotAreaStart,
        OnMowSpotAreaStop,
        OnPos,
        OnProtectState,
        OnRainDelay,
        OnScheduleTaskInfo,
        OnStatsMower,
        OnUwb,
        OnArI,
        OnMapTrace,
        OnMapTrack,
        OnMI,
        OnSpecialContour,
    ):
        MESSAGES[message.NAME] = message
        if MESSAGES.get(message.NAME) is not message:
            _fail(f"registration of {message.NAME} did not take")

    _LOGGER.debug("Message handlers registered")


def verify_capabilities(capabilities: Capabilities, class_: str) -> None:
    """Confirm that the capabilities a device actually got are the patched ones.

    The check runs against the object in ``DeviceInfo.static``, not against the
    cache. That is the only check that proves the patch got in before
    ``get_devices()`` — a cache lookup would look correct even if the device was
    built from an unpatched definition.
    """
    if capabilities.clean.action.command is not CleanMower:
        _fail(
            f"device {class_} was built with {capabilities.clean.action.command.__name__} "
            f"instead of CleanMower — the patch ran too late"
        )

    if class_ in ZONE_AREA_CLASSES and capabilities.clean.action.area is not MowArea:
        _fail(
            f"device {class_} was built without the patched MowArea capability "
            f"— the patch ran too late"
        )

    # Exact type comparison, not isinstance: both GetCleanInfoV2 and our own
    # classes inherit from GetCleanInfo, so isinstance() would accept exactly
    # the unpatched set we want to catch. The check would be toothless.
    #
    # The length is pinned as well. A second command here is the race in issue
    # #67 — the two answers land in one TaskGroup and the last one wins — so a
    # GetChargeState() finding its way back into the list must fail loudly
    # rather than quietly reintroduce the flapping.
    commands = capabilities.get_refresh_commands(StateEvent)
    if [type(command) for command in commands] != [MowerStateRefresh]:
        _fail(
            f"the state commands for {class_} are "
            f"{[type(c).__name__ for c in commands]} instead of "
            f"[MowerStateRefresh]"
        )
