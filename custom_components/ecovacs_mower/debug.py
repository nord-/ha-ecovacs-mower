"""Debug logs a user can start from a button, for reporting what a mower sends.

Supporting another model's area parameters needs a table of what each raw
value means: set a value in the Ecovacs app, see which raw number the mower
reports for it. Every model has its own ranges, so there is no fixed list of
steps to ask for, only a record of what changed while the owner tries.

``AreaParameterLog`` keeps that record. Started from the mower's "Log area
parameters" button, it reads the areas once, then follows every change for
``LOG_WINDOW``. Read-only on every class: nothing here writes to the mower, so
it is safe on a model whose values nobody has mapped yet. Changes made in the
app arrive in two ways. The mower pushes ``onAreaParameter`` to every client,
and that push is handled for every patched mower, flag or no flag; but it has
only been seen on the A1600 LiDAR Pro, and this is for models nobody has
looked at. So the areas are also asked for again every ``POLL_INTERVAL`` while
the window is open. On a mower that pushes, a poll answer that repeats the
last report is dropped by the bus and costs nothing more than the request.

The same table has one more row, *Auto*: the settings some models keep for
the whole mower, in the app's Auto mode, read with their own commands
(``deebot_patch/commands.py``). Every model is asked, because which of those reads
a model answers is itself what an owner can report; an unanswered read stays
``?``. The mower is not known to push these, so the app's writes to them are
followed instead, and only the reads that were answered are polled. Each
setting arrives as its own event; the row is assembled here.

The record is a persistent notification, one per mower, rewritten on every
change: the current raw values as a table and every change with its time.
Not log lines, because HA's log viewer groups warnings by the line of code
that logged them, so a run of snapshot and change lines collapsed into one
entry showing only the last. Each change is still logged at DEBUG, and the
start and the end at WARNING, all under ``[area parameters]``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from datetime import datetime, timedelta
from typing import Any, Protocol

from deebot_client.command import Command
from deebot_client.device import Device
from homeassistant.components import persistent_notification
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .deebot_patch.areas import AREA_PARAMETER_KEYS, MowerArea, MowerAreaEvent
from .deebot_patch.commands import (
    AUTO_PARAMETERS,
    AutoParameter,
    GetAreaParameter,
    GetAreaSet,
)
from .deebot_patch.state_precedence import map_id_for

_LOGGER = logging.getLogger(__name__)

LOG_WINDOW = timedelta(minutes=15)
POLL_INTERVAL = timedelta(seconds=15)

# Enough for stepping every setting through its range; past it the oldest
# changes give way so the notification stays readable.
MAX_CHANGES = 50

_TAG = "[area parameters]"

type Execute = Callable[[Command], Awaitable[bool]]


class DebugLog(Protocol):
    """What a debug button needs from the log it starts."""

    async def async_start(self) -> None:
        """Start, or restart, logging."""

    @callback
    def async_shutdown(self) -> None:
        """Stop logging for good. Called when the button is removed."""


class AreaParameterLog:
    """Record one mower's raw area parameters, and every change, for a while."""

    def __init__(self, hass: HomeAssistant, device: Device, execute: Execute) -> None:
        """Keep what a press needs; nothing is subscribed until then."""
        self._hass = hass
        self._device = device
        self._execute = execute
        info = device.device_info
        self._label = f"{info.get('nick') or info['class']} ({info['class']})"
        self._notification_id = f"{DOMAIN}_area_parameters_{info['did']}"
        self._areas: dict[str, MowerArea] | None = None
        # The mower-wide settings, the "Auto" row, keyed by the area wire key.
        # Shown once they have been asked for; only answered reads are polled.
        self._auto: dict[str, int] = {}
        self._auto_asked = False
        self._auto_reads: tuple[AutoParameter, ...] = ()
        self._changes: list[str] = []
        self._notes: list[str] = []
        self._until: datetime | None = None
        self._unsub_event: CALLBACK_TYPE | None = None
        self._unsub_auto: list[CALLBACK_TYPE] = []
        self._unsub_timer: CALLBACK_TYPE | None = None
        self._unsub_poll: CALLBACK_TYPE | None = None

    async def async_start(self) -> None:
        """Show the current values now, then every change until the window closes.

        A second press starts over: a fresh table and an empty change list. The
        subscription is renewed because the bus hands a new subscriber its last
        event, which is the current table when nothing has changed since.
        """
        self._async_stop()
        self._areas = None
        self._auto = {}
        self._auto_asked = False
        self._auto_reads = ()
        self._changes = []
        self._notes = []
        self._until = dt_util.now() + LOG_WINDOW
        self._unsub_timer = async_call_later(self._hass, LOG_WINDOW, self._on_window_end)
        _LOGGER.warning(
            "%s %s: started; see the notification for the values", _TAG, self._label
        )
        self._publish()
        # Subscribed before asking, so the answer cannot arrive unheard.
        self._unsub_event = self._device.events.subscribe(MowerAreaEvent, self._on_areas)
        self._unsub_auto = [
            self._device.events.subscribe(parameter.event, self._follow(parameter))
            for parameter in AUTO_PARAMETERS
        ]
        self._unsub_poll = async_track_time_interval(
            self._hass, self._poll, POLL_INTERVAL
        )

        if not await self._execute(GetAreaParameter()):
            self._note(
                "The mower did not answer getAreaParameter. This model may not "
                "support area parameters."
            )
        # Asked explicitly rather than left to GetAreaSet, which sends nothing
        # without a map id, so the notification can say why names are missing.
        if (map_id := map_id_for(self._device.events)) is None:
            self._note(
                "Area names are unknown until the mower names its map; the areas "
                "are shown by id."
            )
        else:
            await self._execute(GetAreaSet(map_id))
        await self._ask_auto()

    async def _ask_auto(self) -> None:
        """Read the mower-wide settings once, and note which reads went unanswered.

        Asked of every model: reading changes nothing, and which of the four
        reads a model answers is part of what the log is for. The reads go out
        together, so a model that answers none of them costs one round trip,
        not four. Only the answered ones are polled afterwards.
        """
        answered = await asyncio.gather(
            *(self._execute(parameter.read()) for parameter in AUTO_PARAMETERS)
        )
        if self._until is None:
            # The window closed, or the button went, while the reads were out.
            return
        self._auto_reads = tuple(
            parameter
            for parameter, ok in zip(AUTO_PARAMETERS, answered, strict=True)
            if ok
        )
        self._auto_asked = True
        missing = [p.read.NAME for p in AUTO_PARAMETERS if p not in self._auto_reads]
        if len(missing) == len(AUTO_PARAMETERS):
            self._note(
                "The mower answered none of "
                + ", ".join(missing)
                + ". This model may have no mower-wide (Auto) settings."
            )
        elif missing:
            self._note(
                "The mower did not answer "
                + ", ".join(missing)
                + "; their Auto values stay ? unless the app changes them."
            )
        else:
            self._publish()

    @callback
    def async_shutdown(self) -> None:
        """Stop for good, and say so if a window was open.

        Called when the button is removed: an integration reload, HA stopping,
        the entity disabled. Without the rewrite the notification would keep
        saying it is following changes.
        """
        if self._until is not None:
            self._end("stopped early")

    @callback
    def _async_stop(self) -> None:
        """Stop following changes, silently; the notification stays."""
        for unsub in (
            self._unsub_event,
            *self._unsub_auto,
            self._unsub_timer,
            self._unsub_poll,
        ):
            if unsub is not None:
                unsub()
        self._unsub_event = self._unsub_timer = self._unsub_poll = None
        self._unsub_auto = []

    @callback
    def _on_window_end(self, _now: datetime) -> None:
        # async_call_later has fired and removed itself; the rest is left.
        self._unsub_timer = None
        self._end("stopped")

    @callback
    def _end(self, how: str) -> None:
        self._async_stop()
        self._until = None
        self._publish()
        _LOGGER.warning("%s %s: %s", _TAG, self._label, how)

    async def _poll(self, _now: datetime) -> None:
        """Ask again, for a mower that does not push its changes."""
        # A failure here is not noted: the first answer already said whether
        # the mower answers at all, and one lost poll is made up by the next.
        await self._execute(GetAreaParameter())
        for parameter in self._auto_reads:
            await self._execute(parameter.read())

    async def _on_areas(self, event: MowerAreaEvent) -> None:
        """Take the first report as the table, record what changes after it."""
        areas = {area.area_id: area for area in event.areas}
        previous, self._areas = self._areas, areas
        if previous is not None:
            time = dt_util.now().strftime("%H:%M:%S")
            for area_id in sorted(areas.keys() | previous.keys(), key=_area_order):
                before, after = previous.get(area_id), areas.get(area_id)
                if after is None:
                    self._change(f"{time} {_name(before)}: no longer reported")
                elif before is None:
                    self._change(f"{time} {_name(after)}: new, {_values(after)}")
                else:
                    for key in AREA_PARAMETER_KEYS:
                        old, new = before.parameters.get(key), after.parameters.get(key)
                        if old != new:
                            self._change(
                                f"{time} {_name(after)}: {key} {_raw(old)} → {_raw(new)}"
                            )
        self._publish()

    def _follow(
        self, parameter: AutoParameter
    ) -> Callable[[Any], Coroutine[Any, Any, None]]:
        """The bus callback for one mower-wide setting's event."""

        async def on_event(event: Any) -> None:
            self._on_auto(parameter, getattr(event, parameter.field))

        return on_event

    @callback
    def _on_auto(self, parameter: AutoParameter, value: int) -> None:
        """Record one mower-wide setting, and list it if it changed.

        A setting's first value is only taken in, whether from the first read
        or from the last event the bus hands a new subscriber: a change is a
        difference from a value already shown.
        """
        key = parameter.key
        old = self._auto.get(key)
        self._auto[key] = value
        if old is not None and old != value:
            time = dt_util.now().strftime("%H:%M:%S")
            self._change(f"{time} Auto: {key} {old} → {value}")
        if self._auto_asked:
            self._publish()

    def _change(self, line: str) -> None:
        _LOGGER.debug("%s %s: %s", _TAG, self._label, line)
        self._changes.append(line)
        del self._changes[:-MAX_CHANGES]

    def _note(self, line: str) -> None:
        _LOGGER.debug("%s %s: %s", _TAG, self._label, line)
        self._notes.append(line)
        self._publish()

    @callback
    def _publish(self) -> None:
        """Rewrite the notification from the current state."""
        persistent_notification.async_create(
            self._hass,
            self._message(),
            title=f"Area parameters: {self._label}",
            notification_id=self._notification_id,
        )

    def _message(self) -> str:
        lines = [
            f"Following changes until {self._until:%H:%M}. Change the settings "
            "in the Ecovacs app; this updates as the mower reports each one."
            if self._until is not None
            else "Stopped. Press **Log area parameters** again to start over.",
            "",
        ]
        rows = []
        if self._areas is None:
            lines.append("Waiting for the mower to report its areas.")
        elif not self._areas:
            lines.append("The mower reported no areas.")
        else:
            rows = [
                (_name(self._areas[area_id]), self._areas[area_id].parameters)
                for area_id in sorted(self._areas, key=_area_order)
            ]
        if self._auto_asked:
            rows.append(("Auto", self._auto))
        if rows:
            # Every known key, also one this model may not have: a column of
            # "?" is itself what an owner of an unmapped model can report.
            lines.append("| Area | " + " | ".join(AREA_PARAMETER_KEYS) + " |")
            lines.append("|---" * (len(AREA_PARAMETER_KEYS) + 1) + "|")
            for name, values in rows:
                cells = (_raw(values.get(key)) for key in AREA_PARAMETER_KEYS)
                lines.append(f"| {name} | " + " | ".join(cells) + " |")
        if self._auto_asked:
            lines += [
                "",
                "_Auto is the mower-wide setting, read with "
                + ", ".join(parameter.read.NAME for parameter in AUTO_PARAMETERS)
                + "; a change made in the app is followed._",
            ]
        if self._changes:
            lines += ["", "**Changes**", *(f"- {line}" for line in self._changes)]
        if self._notes:
            lines += ["", *(f"_{note}_" for note in self._notes)]
        lines += [
            "",
            "Copy this into an issue, together with the values you set in the app.",
        ]
        return "\n".join(lines)


def _area_order(area_id: str) -> tuple[int, str]:
    """Numeric ids in numeric order, anything else after them."""
    return (0, f"{int(area_id):09d}") if area_id.isdecimal() else (1, area_id)


def _name(area: MowerArea) -> str:
    return f"Area {area.area_id}" + (f" ({area.name})" if area.name else "")


def _values(area: MowerArea) -> str:
    return ", ".join(
        f"{key}={_raw(area.parameters.get(key))}" for key in AREA_PARAMETER_KEYS
    )


def _raw(value: int | None) -> str:
    # None is a key the mower has not reported, not a value of zero.
    return "?" if value is None else str(value)
