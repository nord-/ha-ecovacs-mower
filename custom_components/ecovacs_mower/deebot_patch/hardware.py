"""Seeds deebot-client's device cache with corrected capabilities.

``get_static_device_info()`` reads the ``_DEVICES`` cache before importing the
device module. By letting the library build its own definition, swapping out the
broken parts and putting the result back, we avoid monkeypatching any function —
we use the same mechanism the library itself uses.

This module also owns the supported mower-class profiles. The profile records
only integration capabilities that have been independently validated for a
specific class; raw protocol parsing remains in the patch layer and
human-facing interpretation remains in the HA layer.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import logging
from types import MappingProxyType

from deebot_client.capabilities import CapabilityEvent
from deebot_client.events import StateEvent, StatsEvent
from deebot_client.hardware import _DEVICES, get_static_device_info

from .areas import MowerAreaEvent
from .commands import (
    CleanMower,
    GetAreaParameter,
    GetAreaSet,
    GetLifeSpanMower,
    GetMapInfoV2,
    GetProtectState,
    GetRainDelay,
    GetStatsMower,
    MowerStateRefresh,
)
from .map_messages import MowerMapInfoEvent
from .messages import (
    MowerBeaconsEvent,
    MowerProtectStateEvent,
    MowerRainDelayEvent,
    MowerStatsEvent,
)
from .zonal import MowArea

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class MowerProfile:
    """Validated integration capabilities for one supported mower class."""

    device_class: str
    area_parameters: bool = False


# Device classes this integration patches, and how each one was confirmed:
#   2i0fns — GOAT O1200 LiDAR Pro (owner-verified)
#   9bts2s — GOAT O800 RTK (user-verified, issue #8)
#   2px96q — GOAT O800 RTK (user-verified, issue #24). A second class string
#            for the same hardware: upstream's 2px96q.py is byte-identical to
#            9bts2s.py.
#   77atlz — GOAT G1-800 (issue #30, firmware 1.36.208 — controls
#            user-verified from the lawn_mower entity, issue #74: start,
#            pause/resume and dock all obeyed on 0.7.2). Upstream's 77atlz.py
#            is byte-identical to 9bts2s.py, docstring included, so the O800
#            RTK's patch applies unchanged — but this firmware branch inverts
#            the quirk the patch exists for. Issue #42 has the A/B on one
#            install: patched, getCleanInfo answers errno 500 on every poll
#            and clean is never acknowledged; unpatched, getCleanInfo_V2
#            answers first try and clean_V2 is acked in 526 ms. The class
#            stays here because the family is now chosen at runtime rather
#            than by this tuple — see families.py.
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
#
# Presence in this mapping means the class is supported by the integration.
# Capability flags are deliberately narrower: they are enabled only where the
# corresponding behavior or raw-value semantics have been independently
# validated on that class. Similar protocol field names on another class are
# not sufficient evidence to enable a capability there.
SUPPORTED_CLASSES: dict[str, MowerProfile] = {
    "2i0fns": MowerProfile("2i0fns"),
    "9bts2s": MowerProfile("9bts2s"),
    "2px96q": MowerProfile("2px96q"),
    "77atlz": MowerProfile("77atlz"),
    "e4gqia": MowerProfile("e4gqia", area_parameters=True),
    "xmp9ds": MowerProfile("xmp9ds"),
}

# ``spotArea`` has only been verified on the A1600 LiDAR Pro. Keep it limited to
# that class until the payload shape has been verified on other firmware/classes.
ZONE_AREA_CLASSES = ("e4gqia",)

# Classes on which the border-job request shape has been captured from the
# app (issue #12). Like ZONE_AREA_CLASSES, membership means "confirmed", not
# "patched": the button is only built for these, because the non-V2 shape is
# a guess nobody has tested — see border.py. Widening this tuple is how a
# second class gains the button.
#   77atlz — GOAT G1-800, firmware 1.36.208: clean_V2 with
#            {"type": "border", "value": "mid:<mid>"}, acknowledged code 0.
BORDER_CLASSES = ("77atlz",)


def profile_for_class(class_: str) -> MowerProfile | None:
    """Return the validated integration profile for a device class."""
    return SUPPORTED_CLASSES.get(class_)


async def patch_device_info(class_: str) -> None:
    """Replace the cached device definition with one where the mow bugs are fixed.

    Six corrections:

    * ``clean.action.command``: ``CleanV2`` publishes on ``clean_V2``, which
      GOAT firmware ignores. Swapped for ``CleanMower`` on ``clean``.
    * ``clean.action.area``: expose the verified GOAT ``spotArea`` area-clean
      command for the A1600 LiDAR Pro. Existing library area commands are
      preserved for other classes.
    * ``state``: the clean-info answer is a constant ``idle`` regardless of
      what the mower is actually doing (issue #48), and the library ran the
      charge and clean-info answers concurrently in one ``TaskGroup`` — a
      race that let a mower parked on its charger read as docked or as paused
      depending on which answer landed last (issue #67). Swapped for
      ``MowerStateRefresh``, one sequential command that awaits the charge
      half before asking for clean info, so the record is written before the
      clean-info answer is interpreted; the clean-info half picks its own
      command name, ``getCleanInfo`` or ``getCleanInfo_V2``, from whichever
      the mower answers at runtime — see ``families.py``.
    * ``stats.clean``: ``GetStats`` drops ``mowedArea``, the one number that
      moves while a job runs. Swapped for ``GetStatsMower``.
    * ``life_span.get``: ``GetLifeSpan`` raises on the ``uwbCell`` entries a
      beacon-guided mower reports, which loses the beacons and every component
      listed after them. Swapped for ``GetLifeSpanMower``.
    * ``MowerProtectStateEvent``, ``MowerRainDelayEvent``, ``MowerStatsEvent``
      and ``MowerBeaconsEvent``: given the refresh commands they had none of.
    * ``MowerMapInfoEvent``: given ``GetMapInfoV2``, without which firmware
      1.36 never sends the lawn boundary at all — it answers the request and
      pushes at no other time (issue #81).

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
    profile = profile_for_class(class_)
    area_parameters = profile is not None and profile.area_parameters
    # Most classes can return early once the common patch is already present.
    # The A1600 area-parameter capability is different: it is an additional
    # event mapping, so it must still be installed if another patch path has
    # already supplied CleanMower.
    if capabilities.clean.action.command is CleanMower and not area_parameters:
        return

    patched = replace(
        capabilities,
        clean=replace(
            capabilities.clean,
            action=replace(
                capabilities.clean.action,
                command=CleanMower,
                area=(
                    MowArea
                    if class_ in ZONE_AREA_CLASSES
                    else capabilities.clean.action.area
                ),
            ),
        ),
        state=CapabilityEvent(StateEvent, [MowerStateRefresh()]),
        # Only stats.clean is replaced; total and report are the library's
        # own and are carried through by replace().
        stats=replace(
            capabilities.stats,
            clean=CapabilityEvent(StatsEvent, [GetStatsMower()]),
        ),
        # Only the get command is replaced. types decides which lifespan
        # entities are built and reset is the button behind them; both are the
        # library's own and are carried through by replace(). The request keeps
        # the same component list for the same reason the stats request keeps
        # its name: the device answers with everything it has regardless, so
        # widening it would buy nothing and diverge further from upstream.
        life_span=replace(
            capabilities.life_span,
            get=[GetLifeSpanMower(capabilities.life_span.types)],
        ),
    )
    # Neither the protection flags nor the mowing progress is a library
    # capability, so there is no field to hang a CapabilityEvent on and nothing
    # to hand dataclasses.replace.
    # get_refresh_commands() reads one mapping, built once in __post_init__ from
    # the dataclass fields, so the entry goes straight in there — the same
    # object.__setattr__ on the same frozen instance that __post_init__ does.
    #
    # Without it the event bus finds no command when the first binary sensor
    # subscribes, and the device only pushes onProtectState when a flag flips:
    # through a dry, uneventful spell nothing arrives at all, so the entities
    # read "unknown" until the weather changes (issue #31). MowerRainDelayEvent
    # is the same trap one setting over: onRainDelay arrives only when somebody
    # changes the rain sensor, so its switch and number would sit at "unknown"
    # until the owner next opened the app (issue #54).
    #
    # This has to stay below the replace() above and cannot move up: replace()
    # re-runs __post_init__, which rebuilds the mapping from the fields, and an
    # entry that no field describes would be dropped without a word. A future
    # correction goes above this one for the same reason.
    #
    # StatsEvent (via stats.clean, above) and MowerStatsEvent (here) are two
    # independent keys in that mapping, each carrying its own GetStatsMower.
    # Both are first-subscribed early — StatsEvent by Device.__init__ itself,
    # MowerStatsEvent by the progress sensor — so an unavailable->available
    # flap, which refreshes every registered event type, sends two identical
    # getStats requests instead of one. Accepted: deduping identical commands
    # across event types would mean changing the event bus itself, and the
    # cost is one extra request on a rare transition, not a wrong answer.
    #
    # LifeSpanEvent and MowerBeaconsEvent share the same pattern for the same
    # getLifeSpan command: LifeSpanEvent is first-subscribed by the blade
    # sensor, MowerBeaconsEvent by the beacon platform setup in sensor.py, so
    # every mower — beacon-equipped or not — asks twice at startup and on
    # every reconnect. Both parse the one answer correctly; only the extra
    # round trip is paid.
    events = {
        **patched._events,
        MowerProtectStateEvent: [GetProtectState()],
        MowerRainDelayEvent: [GetRainDelay()],
        MowerStatsEvent: [GetStatsMower()],
        MowerBeaconsEvent: [GetLifeSpanMower(capabilities.life_span.types)],
        # MowerMapInfoEvent is a different case from all of the above: it is not a
        # push the mower may forget to send, it is a push the mower never sends
        # unasked. Firmware 1.36 answers getMapInfo_V2 with the lawn outline on
        # the atr topic and sends it at no other time, so without an entry here
        # the boundary never arrives at all and the map stays a coverage patch
        # with no field around it (issue #81). controller._setup_map subscribes
        # MowerMapInfoEvent eagerly, so this alone gets the request sent at setup
        # and again on every reconnect — no new lifecycle code, and no risk of a
        # command firing before the device exists. The answer is an ack; the
        # payload lands separately in OnMapInfo, which is why this refresh
        # publishes no event of its own and why that is fine — see GetMapInfoV2.
        MowerMapInfoEvent: [GetMapInfoV2()],
    }
    if area_parameters:
        # One area event represents the whole area capability. The two protocol
        # reads populate one authoritative raw snapshot before notifying it.
        events[MowerAreaEvent] = [GetAreaParameter(), GetAreaSet()]

    object.__setattr__(patched, "_events", MappingProxyType(events))

    _DEVICES[class_] = replace(base, capabilities=patched)
    _LOGGER.debug("Patched capabilities for %s", class_)
