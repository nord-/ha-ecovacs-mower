"""Seeds deebot-client's device cache with corrected capabilities.

``get_static_device_info()`` reads the ``_DEVICES`` cache before importing the
device module. By letting the library build its own definition, swapping out
the broken parts and putting the result back, we avoid monkeypatching any
function — we use the same mechanism the library itself uses.
"""

from __future__ import annotations

from dataclasses import replace
import logging
from types import MappingProxyType

from deebot_client.capabilities import CapabilityEvent
from deebot_client.commands.json.charge_state import GetChargeState
from deebot_client.commands.json.clean import GetCleanInfo
from deebot_client.events import StateEvent
from deebot_client.hardware import _DEVICES, get_static_device_info

from .commands import CleanMower, GetProtectState
from .messages import MowerProtectStateEvent

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
#   e4gqia — GOAT A1600 LiDAR Pro (confirmed, PR #29, firmware 1.11.31).
#            Upstream names this A3000 LiDAR Pro; its module is byte-identical
#            to 9bts2s.py apart from the docstring, so the O800's patch
#            applies unchanged.
#   xmp9ds — GOAT A1600 RTK (reported in issue #43, firmware 1.17.9 — the
#            reporter has not confirmed the patch yet). A different machine
#            from e4gqia above, not a second class string for it: the RTK and
#            LiDAR Pro variants of the A1600 ship separately. Upstream's
#            xmp9ds.py is byte-identical to 9bts2s.py apart from the docstring,
#            which here names the model outright ("DEEBOT GOAT A1600 RTK
#            Capabilities"), so the O800 RTK's patch applies unchanged.
SUPPORTED_CLASSES = ("2i0fns", "9bts2s", "2px96q", "77atlz", "e4gqia", "xmp9ds")


async def patch_device_info(class_: str) -> None:
    """Replace the cached device definition with one where the mow bugs are fixed.

    Three corrections:

    * ``clean.action.command``: ``CleanV2`` publishes on ``clean_V2``, which
      GOAT firmware ignores. Swapped for ``CleanMower`` on ``clean``.
    * ``state``: ``GetCleanInfoV2`` is not answered by GOAT. Swapped for
      ``GetCleanInfo``.
    * ``MowerProtectStateEvent``: given the refresh command it had none of.

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
    # The protection flags are not a library capability, so there is no field
    # to hang a CapabilityEvent on and nothing to hand dataclasses.replace.
    # get_refresh_commands() reads one mapping, built once in __post_init__ from
    # the dataclass fields, so the entry goes straight in there — the same
    # object.__setattr__ on the same frozen instance that __post_init__ does.
    #
    # Without it the event bus finds no command when the first binary sensor
    # subscribes, and the device only pushes onProtectState when a flag flips:
    # rain protection that is simply left switched on never gets reported, so
    # the entity reads "unknown" for good (issue #31).
    #
    # This has to stay below the replace() above and cannot move up: replace()
    # re-runs __post_init__, which rebuilds the mapping from the fields, and an
    # entry that no field describes would be dropped without a word. A future
    # correction goes above this one for the same reason.
    object.__setattr__(
        patched,
        "_events",
        MappingProxyType(
            {**patched._events, MowerProtectStateEvent: [GetProtectState()]}
        ),
    )

    _DEVICES[class_] = replace(base, capabilities=patched)
    _LOGGER.debug("Patched capabilities for %s", class_)
