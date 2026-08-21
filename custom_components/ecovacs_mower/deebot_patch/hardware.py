"""Seeds deebot-client's device cache with corrected capabilities.

``get_static_device_info()`` reads the ``_DEVICES`` cache before importing the
device module. By letting the library build its own definition, swapping out
the broken parts and putting the result back, we avoid monkeypatching any
function — we use the same mechanism the library itself uses.
"""

from __future__ import annotations

from dataclasses import replace
import logging

from deebot_client.capabilities import CapabilityEvent
from deebot_client.commands.json.charge_state import GetChargeState
from deebot_client.commands.json.clean import GetCleanInfo
from deebot_client.events import StateEvent
from deebot_client.hardware import _DEVICES, get_static_device_info

from .commands import CleanMower

_LOGGER = logging.getLogger(__name__)

# Device classes this integration patches, and how each one was confirmed:
#   2i0fns — GOAT O1200 LiDAR Pro (owner-verified)
#   9bts2s — GOAT O800 RTK (user-verified, issue #8)
#   2px96q — GOAT O800 RTK (user-verified, issue #24). A second class string
#            for the same hardware: upstream's 2px96q.py is byte-identical to
#            9bts2s.py.
#   77atlz — GOAT G1-800 (reported in issue #30, firmware 1.36.208 — the
#            reporter has not confirmed the patch yet). Upstream's 77atlz.py is
#            byte-identical to 9bts2s.py, docstring included, so the O800 RTK's
#            patch applies unchanged. Its firmware is on a different branch than
#            the 1.13.x we have seen, which is where a surprise would come from.
SUPPORTED_CLASSES = ("2i0fns", "9bts2s", "2px96q", "77atlz")


async def patch_device_info(class_: str) -> None:
    """Replace the cached device definition with one where the mow bugs are fixed.

    Two corrections:

    * ``clean.action.command``: ``CleanV2`` publishes on ``clean_V2``, which
      GOAT firmware ignores. Swapped for ``CleanMower`` on ``clean``.
    * ``state``: ``GetCleanInfoV2`` is not answered by GOAT. Swapped for
      ``GetCleanInfo``.

    The call is idempotent and does nothing for classes outside
    ``SUPPORTED_CLASSES``.

    **Must be called before ``ApiClient.get_devices()``.** That method calls
    ``get_static_device_info()`` and bakes the result into ``DeviceInfo.static``,
    which is a frozen dataclass. Patching the cache afterwards means the devices
    already got the unpatched capabilities.
    """
    if class_ not in SUPPORTED_CLASSES:
        _LOGGER.debug("Device class %s not supported, not patching", class_)
        return

    base = await get_static_device_info(class_)
    if base is None:
        # Upstream returns None for unknown classes; no fallback definition
        # exists, so there is nothing to patch here.
        _LOGGER.debug("No device definition for %s, skipping patch", class_)
        return

    capabilities = base.capabilities
    if capabilities.clean.action.command is CleanMower:
        return

    patched = replace(
        capabilities,
        clean=replace(
            capabilities.clean,
            action=replace(capabilities.clean.action, command=CleanMower),
        ),
        state=CapabilityEvent(StateEvent, [GetChargeState(), GetCleanInfo()]),
    )
    _DEVICES[class_] = replace(base, capabilities=patched)
    _LOGGER.debug("Patched capabilities for %s", class_)
