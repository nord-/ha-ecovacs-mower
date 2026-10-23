"""Seeds deebot-client's device cache with corrected capabilities.

``get_static_device_info()`` reads the ``_DEVICES`` cache before importing the
device module. By letting the library build its own definition, swapping out
the broken parts and putting the result back, we avoid monkeypatching any
function — we use the same mechanism the library itself uses.

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
    GetAnimProtect,
    GetAreaParameter,
    GetAreaSet,
    GetLifeSpanMower,
    GetMapInfoMower,
    GetProtectState,
    GetRainDelay,
    GetStatsMower,
    MowerStateRefresh,
)
from .map_messages import MowerMapInfoEvent
from .messages import (
    MowerAnimProtectEvent,
    MowerBeaconsEvent,
    MowerProtectStateEvent,
    MowerRainDelayEvent,
    MowerStatsEvent,
)
from .zonal import MowArea

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class MowerProfile:
    """Validated integration capabilities for one supported mower class.

    Every flag defaults to off. Turning one on is a claim that the behaviour
    behind it has been confirmed on that class, not that the protocol fields
    look the same as on a class where it was.

    ``area_parameters`` gives the class the ``MowerAreaEvent`` refresh and the
    per-area number entities. ``area_names`` gives it the same refresh without
    the entities: the names and their label points come from ``getAreaSet``,
    while the entities need the raw parameter semantics validated on the
    class, which is a separate claim. ``zone_mowing`` gives it ``MowArea`` as
    its ``clean.action.area`` capability, which is what the ``mow_area``
    service checks for. ``border_mowing`` gives it the ``mow_border`` button;
    that one gates an entity rather than a capability, because
    ``Capabilities`` has no field for a border job, and ``button.py`` reads it
    from the profile. ``uwb_beacons`` makes ``getLifeSpan`` ask for every
    component instead of naming the library's, because the class reports its
    beacons only in the answer to that request. Without it, the request names
    the library's components, because a class without beacons may answer the
    empty list with the blades alone (issue #146).
    """

    device_class: str
    area_parameters: bool = False
    area_names: bool = False
    zone_mowing: bool = False
    border_mowing: bool = False
    uwb_beacons: bool = False


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
#   xmp9ds — GOAT A1600 RTK (reported in issue #43, firmware 1.17.9; controls
#            user-verified in issue #104 by two owners on firmware 1.17.11:
#            start, dock, and start again from the dock all obeyed, while
#            testing PR #110). A different machine
#            from e4gqia above, not a second class string for it: the RTK and
#            LiDAR Pro variants of the A1600 ship separately. Upstream's
#            xmp9ds.py is byte-identical to 9bts2s.py apart from the docstring,
#            which here names the model outright ("DEEBOT GOAT A1600 RTK
#            Capabilities"), so the O800 RTK's patch applies unchanged.
#   o4kvvk — GOAT A3000 LiDAR (reported in issue #106, firmware 1.13.31 — the
#            reporter ran it unpatched, so the patch itself is not confirmed
#            yet). Upstream's docstring calls it "GOAT G1", the same kind of
#            misnaming as e4gqia above; its module is byte-identical to
#            9bts2s.py, docstring aside, so the O800 RTK's patch applies
#            unchanged.
#   6n9pcz — GOAT O600 RTK (reported in issue #103 with the unpatched
#            symptoms: start and stop only, state lagging). Upstream's
#            6n9pcz.py is byte-identical to 9bts2s.py, docstring included.
#   0jbd6s — GOAT O1000 LiDAR Pro (reported in issue #102, firmware 2.13.10).
#            The cloud reports its internal model as
#            GOAT_INT_O1200_LIDAR_PLUS_NA, and upstream's 0jbd6s.py is
#            byte-identical to 2i0fns.py, docstring included — so the O1200's
#            patch applies unchanged, not the O800's.
#   51rcxt — GOAT A3000 LiDAR Pro (issue #135, firmware 1.13.10 — start,
#            pause and dock user-verified). Upstream's module is
#            byte-identical to e4gqia.py, docstring included, so the O800
#            RTK's patch applies unchanged.
#   qhq6i0 — GOAT O1200 RTK (issue #140, firmware 1.17.11 — zone mowing
#            user-verified). A different machine from the O1200 LiDAR Pro
#            (2i0fns). Upstream's docstring calls it "GOAT G1"; its module
#            is byte-identical to 9bts2s.py apart from that docstring, so the
#            O800 RTK's patch applies unchanged.
#
# Presence in this mapping means the class is supported by the integration.
# Capability flags are deliberately narrower: they are enabled only where the
# corresponding behavior or raw-value semantics have been independently
# validated on that class. Similar protocol field names on another class are
# not sufficient evidence to enable a capability there.
#
# How each flag was validated:
#   area_parameters — e4gqia: raw-to-HA mappings confirmed on the A1600 LiDAR
#                     Pro; see docs/area-parameter-capability.md.
#                     51rcxt: the A3000 LiDAR Pro's raw values, read for
#                     every app setting (issue #131), match the A1600's.
#                     2i0fns: the O1200 LiDAR Pro's raw values, read with the
#                     app (issue #23): obstacle height and direction fit the
#                     A1600's, height and speed do not; its own entry in
#                     area_numbers.py has all four. Height raw 2-7 are
#                     inferred from the half-centimetre steps, not read, and
#                     no write from Home Assistant has run on this class yet.
#   area_names      — 9bts2s: the ar response captured on a three-lawn O800
#                     RTK, firmware 1.13.8 (issue #41), after renaming the
#                     areas in the app:
#                     [["1","1","Hinten","","-16900","13900","0-0"], …] —
#                     ids, the new names, and a point inside each area.
#   zone_mowing     — e4gqia: the spotArea payload confirmed on the A1600
#                     LiDAR Pro, firmware 1.11.31 (PR #78); see zonal.py.
#                     77atlz: two zone runs on the G1-800, firmware 1.36.208,
#                     each with a single area id; the target area and the
#                     progress matched the area asked for, and the mower
#                     docked normally (PR #114).
#                     2i0fns: the app's spotArea start captured on the O1200
#                     LiDAR Pro, firmware 1.13.10 (issue #11, 2026-09-10):
#                     {"act": "start", "content": {"type": "spotArea",
#                     "value": "3"}} on clean, byte for byte what MowArea's
#                     non-V2 delegate builds, and pause, resume and stop sent
#                     from Home Assistant against that job were all obeyed.
#                     A start sent from Home Assistant itself has not been
#                     run yet; it is the same payload on the same topic.
#                     51rcxt: a zone run started from Home Assistant on the
#                     A3000 LiDAR Pro mowed the area asked for (issue #135).
#                     9bts2s: a zone start sent from Home Assistant on the
#                     O800 RTK, firmware 1.13.8 (PR #142): spotArea with
#                     value "1" went out on clean, was answered code 0, and
#                     the mower left for that area — onCleanInfo reported
#                     state clean with content type spotArea and value "1"
#                     while it ran, and getChargeState went from charging to
#                     not. The non-V2 family was chosen first try, so no
#                     clean_V2 attempt was needed.
#                     qhq6i0: a mow_area call on the O1200 RTK, firmware
#                     1.17.11, after adding the class locally (issue #140);
#                     the user reported it working, with no detail beyond that.
#   border_mowing   — 77atlz: the app's border-job request captured on the
#                     G1-800, firmware 1.36.208 (issue #12): clean_V2 with
#                     {"type": "border", "value": "mid:<mid>"}, acknowledged
#                     code 0.
#                     51rcxt: the first non-V2 class with the flag. Started
#                     from Home Assistant on the A3000 LiDAR Pro, firmware
#                     1.13.10 (issue #135), the border job set its target
#                     area and duration, its progress reached 100 and the
#                     "Last mowing job" event fired finished. The app's own
#                     job starts go out on clean and its state arrives as
#                     onCleanInfo, so the start went out in the non-V2 shape
#                     — see border.py. The app offers no border job on this
#                     model, only its trimming job (borderrotate, issue #138).
#   uwb_beacons     — 77atlz: the app's getLifeSpan request captured on the
#                     G1-800, firmware 1.36.208 (issue #100): {"data": []},
#                     answered with blade, one uwbCell per beacon and lens
#                     brush, where the library's named request came back with
#                     blade and lens brush only. Off everywhere else: the
#                     O1200, firmware 1.13.10, answers the empty list with
#                     blade alone, and the app names blade, weedRope and
#                     trimmerBrush to it (issue #146).
SUPPORTED_CLASSES: dict[str, MowerProfile] = {
    "2i0fns": MowerProfile("2i0fns", area_parameters=True, zone_mowing=True),
    "9bts2s": MowerProfile("9bts2s", area_names=True, zone_mowing=True),
    "2px96q": MowerProfile("2px96q"),
    "77atlz": MowerProfile(
        "77atlz", zone_mowing=True, border_mowing=True, uwb_beacons=True
    ),
    "e4gqia": MowerProfile("e4gqia", area_parameters=True, zone_mowing=True),
    "xmp9ds": MowerProfile("xmp9ds"),
    "o4kvvk": MowerProfile("o4kvvk"),
    "6n9pcz": MowerProfile("6n9pcz"),
    "0jbd6s": MowerProfile("0jbd6s"),
    "51rcxt": MowerProfile(
        "51rcxt", area_parameters=True, zone_mowing=True, border_mowing=True
    ),
    "qhq6i0": MowerProfile("qhq6i0", zone_mowing=True),
}


def profile_for_class(class_: str) -> MowerProfile | None:
    """Return the validated integration profile for a device class."""
    return SUPPORTED_CLASSES.get(class_)


async def patch_device_info(class_: str) -> None:
    """Replace the cached device definition with one where the mow bugs are fixed.

    Six corrections:

    * ``clean.action.command``: ``CleanV2`` publishes on ``clean_V2``, which
      GOAT firmware ignores. Swapped for ``CleanMower`` on ``clean``.
    * ``clean.action.area``: expose the verified GOAT ``spotArea`` area-clean
      command for classes whose profile sets ``zone_mowing``. Existing
      library area commands are preserved for other classes.
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
    * ``life_span.get``: ``GetLifeSpan`` asks only for the components the
      library has entities for, and the mower answers only what it is asked,
      so the beacons never arrive (issue #100); when they do, it raises on the
      ``uwbCell`` entries and loses every component listed after them. Swapped
      for ``GetLifeSpanMower``, which asks a class with ``uwb_beacons`` for
      everything and names the library's components to every other class,
      since an O1200 answers the empty list with blade alone (issue #146).
    * ``MowerProtectStateEvent``, ``MowerRainDelayEvent``,
      ``MowerAnimProtectEvent``, ``MowerStatsEvent`` and ``MowerBeaconsEvent``:
      given the refresh commands they had none of.
    * ``MowerMapInfoEvent``: given ``GetMapInfoMower``, without which a V2
      mower never sends the lawn boundary at all — it answers the request and
      pushes at no other time (issue #81) — and a non-V2 mower does not name
      the map in use until a map message happens to arrive.

    The call is idempotent and does nothing for classes outside
    ``SUPPORTED_CLASSES``.

    **Must be called before ``ApiClient.get_devices()``.** That method calls
    ``get_static_device_info()`` and bakes the result into ``DeviceInfo.static``,
    which is a frozen dataclass. Patching the cache afterwards means the devices
    already got the unpatched capabilities.
    """
    profile = profile_for_class(class_)
    if profile is None:
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

    # The empty list is the only request a beacon class answers with its
    # beacons, and a request some other classes answer with blade alone.
    life_span_components = () if profile.uwb_beacons else capabilities.life_span.types

    patched = replace(
        capabilities,
        clean=replace(
            capabilities.clean,
            action=replace(
                capabilities.clean.action,
                command=CleanMower,
                area=(
                    MowArea
                    if profile.zone_mowing
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
        # library's own and are carried through by replace(). On a beacon class
        # the request does not follow types: the mower answers only the
        # components a request lists, and types never lists the beacons, so the
        # command asks for everything and leaves the choosing to the entity
        # platforms (issue #100).
        life_span=replace(
            capabilities.life_span,
            get=[GetLifeSpanMower(life_span_components)],
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
    # until the owner next opened the app (issue #54). MowerAnimProtectEvent is
    # the same trap again, for the animal-protection switch (issue #45).
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
    #
    # MowerMapInfoEvent is a different case from all of the above: it is not a
    # push the mower may forget to send, it is a push the mower never sends
    # unasked. Firmware 1.36 answers getMapInfo_V2 with the lawn outline on
    # the atr topic and sends it at no other time, so without an entry here
    # the boundary never arrives at all and the map stays a coverage patch
    # with no field around it (issue #81). A non-V2 mower answers getMI
    # instead, preceded by an appping, with an onMI whose envelope is also the
    # first word on which map is in use, which GetAreaSet waits for;
    # GetMapInfoMower asks under whichever name the mower's family answers.
    # controller._setup_map subscribes MowerMapInfoEvent eagerly, so this
    # alone gets the request sent at setup and again on every reconnect — no
    # new lifecycle code, and no risk of a command firing before the device
    # exists. The answer is an ack; the payload lands separately in OnMI or
    # OnMapInfo, which is why this refresh publishes no event of its own and
    # why that is fine — see GetMapInfoMower.
    events = {
        **patched._events,
        MowerProtectStateEvent: [GetProtectState()],
        MowerRainDelayEvent: [GetRainDelay()],
        MowerAnimProtectEvent: [GetAnimProtect()],
        MowerStatsEvent: [GetStatsMower()],
        MowerBeaconsEvent: [GetLifeSpanMower(life_span_components)],
        MowerMapInfoEvent: [GetMapInfoMower()],
    }
    if profile.area_parameters:
        # One area event represents the whole area capability. The two protocol
        # reads populate one authoritative raw snapshot before notifying it.
        events[MowerAreaEvent] = [GetAreaParameter(), GetAreaSet()]
    elif profile.area_names:
        # Names and label points only: getAreaParameter has no validated
        # reading on this class, and nothing here needs one.
        events[MowerAreaEvent] = [GetAreaSet()]

    object.__setattr__(patched, "_events", MappingProxyType(events))

    _DEVICES[class_] = replace(base, capabilities=patched)
    _LOGGER.debug("Patched capabilities for %s", class_)
