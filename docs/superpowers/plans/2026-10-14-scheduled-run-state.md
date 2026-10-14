# Scheduled-run state from the task bury points — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A scheduled run on a mower that sends only task bury points (the G1-800 in issue #120) shows *mowing* once it has left its charger, *returning* on the trip home and *docked* at the charger, starts the poll on leaving, and a new *Job started* event entity tells an automation what started or resumed the job.

**Architecture:** The patch layer's handlers write the state, never a subscription. A start or resume bury point and the mower's own `onChargeState isCharging: 0` push are the two halves of a departure; `MowerStateRecord` holds whichever half arrived first, and the second half within 60 s, in either order, writes CLEANING. A pause or stop edge or any reported state cancels a waiting half. `return-normal-start` writes RETURNING. The job type (the bury point's middle segment) travels on `MowerJobEdgeEvent` and feeds the new event entity.

**Tech Stack:** Python 3.14, Home Assistant custom integration, `deebot-client==18.6.0` (pinned, do not change), pytest + pytest-asyncio (`asyncio_mode = auto`).

**Spec:** `docs/superpowers/specs/2026-10-13-scheduled-run-state-design.md` (approved by Rickard 2026-10-14 at `cf17717`; §2's "What an event means" paragraph was amended in `3fbacf2`, and that amendment is confirmed together with this plan). Read the current version on the branch before your task; its Fixtures section is the source of every payload below.

## Global Constraints

- All code, comments, docstrings and commit messages in English. Comments explain *why*. Match the surrounding docstring density: this codebase documents evidence and reasons at length.
- State derived from the event stream is written inside the handlers, before they notify, never by a subscription (CLAUDE.md, "State derived from the event stream").
- `messages.py` binds `time` to `datetime.time`; import the clock as `from time import monotonic` and call `monotonic()`. Tests monkeypatch `messages.monotonic`.
- `strings.json` and `translations/en.json` must stay byte-identical in content (`test_translations.py`). Never create `sv.json`.
- Do not bump `manifest.json` and do not touch the `deebot-client` pin.
- Conventional commits (`feat:`, `fix:`, `docs:`, `test:`), subject ending in `(#120)`. No AI attribution of any kind, no `Co-Authored-By`, no `Claude-Session:` trailer.
- Edit with the Edit/Write tools. The working copy is CRLF and the repository stores LF (`core.autocrlf=true`); do not convert line endings by hand.
- Line numbers in this plan are as of `e3b6a93` and shift as earlier tasks add code; find each place by the symbol or text quoted next to it.
- Local test runner (Windows, from the worktree root):
  - Patch layer: `"D:/private-projects/ha-ecovacs-mower/v314/Scripts/python.exe" -m pytest tests/deebot_patch/ -p no:homeassistant -q`
  - HA-layer files (`requires_ha`): `PYTHONPATH=.claude/unskip "D:/private-projects/ha-ecovacs-mower/v314/Scripts/python.exe" -m pytest <files> -p no:homeassistant -p unskip_win32 -q`. The plugin `.claude/unskip/unskip_win32.py` already exists in the worktree and is git-ignored; never commit it. Tests that need the `hass` fixture error with `fixture 'hass' not found` locally; that is expected, CI runs them. New tests in this plan avoid that fixture.
  - Baseline before Task 1: patch layer 634 passed.
- CI (`.github/workflows/test.yml`) is the source of truth; never claim the suite is green from a local run alone.

## Review Focus

Inputs the spec implies but its own test list does not pin. Each has a test in the task named.

1. A Deebot vacuum on the same account pushes `onChargeState isCharging: 0` (MESSAGES is global): no `StateEvent` of our own and success returned. → Task 3, `test_an_unregistered_bus_gets_no_state_from_a_departure`.
2. A repeated `reborn` start out on the lawn (O800 RTK, mower undocked, last state CLEANING) must not leave a departure half armed for a later unplug. → Task 3, `test_an_edge_after_a_pushed_state_away_from_the_dock_does_not_arm` (asserts the stamp stays `None`).
3. A redelivered `isCharging: 0` push right after a completed pair must not notify a second CLEANING or flip anything. → Task 3, `test_a_repeated_unplug_after_a_completed_pair_changes_nothing`.
4. *Job started* and *Last mowing job* live on the same device and platform; their unique ids must differ or HA drops one. → Task 5, `test_the_two_event_entities_have_distinct_unique_ids`.
5. A paused plan withheld on the dock (#67) between the edge and the unplug must not survive the departure, or the mow command would later resume against a stale PAUSED. → Task 3, `test_a_withheld_paused_plan_does_not_cancel_the_departure` (asserts `record.suppressed is None`).

## Tracks

- **Main track:** Task 1 → Task 2 → Task 3 → Task 4 (Tasks 3 and 4 grow `messages.py` and `test_messages.py`).
- **Side track:** Task 5 consumes only Task 1 (`MowerJobEdgeEvent.job_type`). Start it as soon as Task 1 is committed, in an isolated worktree reset onto Task 1's commit. The unskip plugin is git-ignored and therefore absent there: in that worktree use `PYTHONPATH="D:/private-projects/ha-ecovacs-mower/.claude/worktrees/fix-120-scheduled-run-state/.claude/unskip"` instead of `PYTHONPATH=.claude/unskip` (never copy it into a tracked path, never commit it). Cherry-pick it back after Task 4; a conflict in `CLAUDE.md` is possible though the hunks are far apart (Task 4 edits two bullets in "The patch layer…", Task 5 the "Entity platforms" paragraph and the one after it); keep both edits.

---

### Task 1: The job type on every job edge, and the pause and resume edges

**Files:**
- Modify: `custom_components/ecovacs_mower/deebot_patch/messages.py` (module docstring lines 29-39, `MowerJobEdgeEvent` lines 125-171, `_OnMowJobEdge` lines 179-218, the eight subclasses lines 221-274)
- Modify: `custom_components/ecovacs_mower/deebot_patch/__init__.py` (import block lines 44-64, `apply()` tuple lines 114-144)
- Modify: `tests/deebot_patch/test_messages.py` (imports, new fixtures and tests after line 1269)
- Modify: `tests/deebot_patch/test_contract.py:318-319` (comment only)
- Modify: `tests/test_event.py:134-139` and `:205`
- Modify: `tests/test_sensor.py:1082-1087`, plus one new test

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `MowerJobEdgeEvent(phase: str, trigger: str, job_type: str, mowed_area: float | None = None, work_area: float | None = None)`; `phase` is one of `"start"`, `"pause"`, `"resume"`, `"stop"`; `job_type` one of `"schedule"`, `"spotarea"`, `"border"`, `"auto"`.
  - `_OnMowJobEdge.JOB_TYPE: ClassVar[str]` on all twelve subclasses.
  - New classes in `messages.py`: `OnMowScheduleResume`, `OnMowSchedulePause`, `OnMowSpotAreaResume`, `OnMowSpotAreaPause`, all registered by `apply()`.

- [ ] **Step 1: Write the failing tests**

In `tests/deebot_patch/test_messages.py`, the schedule and spot-area part of the import from `...deebot_patch.messages` becomes:

```python
    OnMowSchedulePause,
    OnMowScheduleResume,
    OnMowScheduleStart,
    OnMowScheduleStop,
    OnMowSpotAreaPause,
    OnMowSpotAreaResume,
    OnMowSpotAreaStart,
    OnMowSpotAreaStop,
```

Then, directly after `test_the_schedule_and_spotarea_job_edge_names_are_registered` (line 1263-1269), add:

```python
# Issue #120. The G1-800 (77atlz, fw 1.36.208) bodies are verbatim from the
# reporter's log, ids anonymised by the reporter; the O1200 (2i0fns, fw
# 1.13.10) bodies are from the maintainer's own mower on 2026-10-12. See the
# spec's Fixtures section for the full sequences.
_G1800_RESUME_1300 = {
    "bid": "7101791802821723",
    "gid": "G1791648417330",
    "index": "0000000435",
    "mapid": "0809159361",
    "pauseId": "9211791733414102",
    "resumeId": "2261791802821723",
    "scheduleId": "1791725416643502",
    "sid": "6211791802821723",
    "triggerType": "schedule",
    "ts": "1791802821723",
}
_G1800_PAUSE_1330 = {
    "bid": "9831791804621723",
    "cuttedArea": 199.292500,
    "gid": "G1791648417330",
    "index": "0000000454",
    "mapid": "0809159361",
    "pauseId": "4491791804621723",
    "resumeId": "2261791802821723",
    "scheduleId": "1791725416643502",
    "sid": "4301791804621723",
    "time": 0,
    "triggerType": "schedule",
    "ts": "1791804621723",
    "workArea": 280.177500,
}
_G1800_RESUME_1400 = {
    "bid": "7931791806421723",
    "gid": "G1791648417330",
    "index": "0000000491",
    "mapid": "0809159361",
    "pauseId": "4491791804621723",
    "resumeId": "1241791806421724",
    "scheduleId": "1791725416643502",
    "sid": "0781791806421724",
    "triggerType": "schedule",
    "ts": "1791806421724",
}
_G1800_STOP_1501 = {
    "bid": "0341791810117742",
    "cuttedArea": 280.202525,
    "gid": "G1791648417330",
    "index": "0000000521",
    "mapid": "0809159361",
    "pauseId": "4491791804621723",
    "resumeId": "1241791806421724",
    "scheduleId": "1791725416643502",
    "sid": "4061791810117742",
    "time": 194.183334,
    # Lower-case c, unlike every other firmware's workComplete. A separate
    # bug in the stop handling, out of scope here (see the spec's Non-goals).
    "triggerType": "workcomplete",
    "ts": "1791810117742",
    "workArea": 280.202525,
}
_O1200_RESUME_1147 = {
    "bid": "7081791798470484",
    "gid": "G1790503263492",
    "index": "0000133051",
    "jobId": "5251791788453307",
    "mapId": "511702305",
    "sid": "4161791798470484",
    "trigger": "continue",
    "ts": "1791798470485",
    "workType": 18,
}
_O1200_START_0901 = {
    "bid": "7301791788466359",
    "gid": "G1790503263492",
    "index": "0000126501",
    "jobId": "5251791788453307",
    "mapId": "511702305",
    "sid": "8781791788466360",
    "trigger": "schedule",
    "ts": "1791788466360",
    "workType": 18,
}


@pytest.mark.parametrize(
    ("message", "phase", "job_type"),
    [
        (OnMowAutoStart, "start", "auto"),
        (OnMowAutoStop, "stop", "auto"),
        (OnMowBorderStart, "start", "border"),
        (OnMowBorderStop, "stop", "border"),
        (OnMowSchedulePause, "pause", "schedule"),
        (OnMowScheduleResume, "resume", "schedule"),
        (OnMowScheduleStart, "start", "schedule"),
        (OnMowScheduleStop, "stop", "schedule"),
        (OnMowSpotAreaPause, "pause", "spotarea"),
        (OnMowSpotAreaResume, "resume", "spotarea"),
        (OnMowSpotAreaStart, "start", "spotarea"),
        (OnMowSpotAreaStop, "stop", "spotarea"),
    ],
)
def test_every_edge_carries_its_phase_and_job_type(
    message, phase: str, job_type: str
) -> None:
    """The middle topic segment is the job type, and it travels with the edge.

    It is the one signal that says "the schedule's job" the same way on every
    firmware: the O1200's trigger: continue and the G1-800's triggerType:
    schedule both arrive on mow-schedule-resume (issue #120).
    """
    (event,) = _notified_edges(message, {"trigger": "app"})

    assert event.phase == phase
    assert event.job_type == job_type


def test_a_g1800_scheduled_resume_is_a_resume_of_the_schedules_job() -> None:
    """The window opening at 13:00 in issue #120, in the triggerType dialect."""
    (event,) = _notified_edges(OnMowScheduleResume, _G1800_RESUME_1300)

    assert event.phase == "resume"
    assert event.trigger == "schedule"
    assert event.job_type == "schedule"
    assert event.mowed_area is None


def test_a_g1800_scheduled_pause_carries_its_areas() -> None:
    """The window closing at 13:30 in issue #120: cuttedArea, not mowedArea."""
    (event,) = _notified_edges(OnMowSchedulePause, _G1800_PAUSE_1330)

    assert event.phase == "pause"
    assert event.trigger == "schedule"
    assert event.mowed_area == 199.2925
    assert event.work_area == 280.1775


def test_an_o1200_charge_break_resume_says_continue() -> None:
    """The O1200's resume after a charge break, on the same topic (2026-10-12)."""
    (event,) = _notified_edges(OnMowScheduleResume, _O1200_RESUME_1147)

    assert event.phase == "resume"
    assert event.trigger == "continue"
    assert event.job_type == "schedule"


def test_the_pause_and_resume_job_edge_names_are_registered() -> None:
    """Logged as 'Unknown message' until issue #120."""
    apply()

    assert MESSAGES["onFwBuryPoint-bd_task-mow-schedule-pause"] is OnMowSchedulePause
    assert MESSAGES["onFwBuryPoint-bd_task-mow-schedule-resume"] is OnMowScheduleResume
    assert MESSAGES["onFwBuryPoint-bd_task-mow-spotarea-pause"] is OnMowSpotAreaPause
    assert MESSAGES["onFwBuryPoint-bd_task-mow-spotarea-resume"] is OnMowSpotAreaResume
```

In `tests/test_event.py`, change the `_stop` helper (lines 134-139) to:

```python
def _stop(trigger: str):
    from custom_components.ecovacs_mower.deebot_patch.messages import MowerJobEdgeEvent

    return MowerJobEdgeEvent(
        phase="stop",
        trigger=trigger,
        job_type="schedule",
        mowed_area=320.567505,
        work_area=320.567505,
    )
```

change line 205 to:

```python
    await entity._on_job_edge(
        MowerJobEdgeEvent(phase="start", trigger="app", job_type="spotarea")
    )
```

and add after `test_a_start_fires_nothing`:

```python
async def test_a_pause_or_resume_fires_nothing() -> None:
    """Issue #120 registers the pause and resume edges; they end no job.

    ``app`` for the reason ``test_a_start_fires_nothing`` gives: it is mapped
    for a stop, so only the phase guard keeps these from firing.
    """
    from custom_components.ecovacs_mower.deebot_patch.messages import MowerJobEdgeEvent

    entity = _bare_event_entity()

    for phase in ("pause", "resume"):
        await entity._on_job_edge(
            MowerJobEdgeEvent(phase=phase, trigger="app", job_type="schedule")
        )

    assert entity.state_attributes["event_type"] is None
    entity.async_write_ha_state.assert_not_called()
```

In `tests/test_sensor.py`, change the `_edge` helper (lines 1082-1087) to:

```python
def _edge(
    phase: str,
    trigger: str,
    mowed: float | None = None,
    work: float | None = None,
    job_type: str = "schedule",
):
    from custom_components.ecovacs_mower.deebot_patch.messages import MowerJobEdgeEvent

    return MowerJobEdgeEvent(
        phase=phase, trigger=trigger, job_type=job_type, mowed_area=mowed, work_area=work
    )
```

and add after `test_a_new_job_resets_the_reading_to_zero` (find it by name):

```python
async def test_pause_and_resume_edges_leave_the_reading_alone() -> None:
    """Issue #120 registers them; _on_job_edge names the phases it acts on.

    ``schedule`` on the resume is a new-job trigger on a start, and
    ``workComplete`` on the pause is a completion on a stop, so only the
    phase checks keep these from resetting or finalising the reading.
    """
    sensor = _bare_progress_sensor()
    sensor._attr_native_value = 55
    sensor._job_over = True

    await sensor._on_job_edge(_edge("resume", "schedule"))

    assert sensor._attr_native_value == 55
    assert sensor._job_over is True

    sensor._job_over = False
    await sensor._on_job_edge(_edge("pause", "workComplete", mowed=1.0, work=2.0))

    assert sensor._attr_native_value == 55
    assert sensor._job_over is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `"D:/private-projects/ha-ecovacs-mower/v314/Scripts/python.exe" -m pytest tests/deebot_patch/test_messages.py -p no:homeassistant -q`
Expected: collection ERROR, `ImportError: cannot import name 'OnMowSchedulePause'`.

- [ ] **Step 3: Implement**

In `messages.py`, `MowerJobEdgeEvent`: add the field directly after `trigger`:

```python
    phase: str
    trigger: str
    job_type: str
    mowed_area: float | None = None
    work_area: float | None = None
    _seq: int = field(default_factory=itertools.count().__next__, repr=False)
```

and in its docstring replace

```
    the type the library's start command sends. Only the two edges this
    integration acts on are registered; adding a ``pause`` or ``resume`` is
    one subclass each (issue #73).
```

with

```
    the type the library's start command sends. ``start`` and ``stop`` are
    registered for all four types, and ``pause`` and ``resume`` for
    ``schedule`` and ``spotarea``, the two types they have been observed on
    (issue #120); another is one subclass each.
```

and replace

```
    ``phase`` is ``"start"`` or ``"stop"``. ``trigger`` is the raw string, and
    what it means is the consumer's business — see ``MowerTriggerEvent`` for
    the same rule stated at length. The vocabulary observed so far is
    ``schedule``/``app``/``reborn`` on a start and ``workComplete``/``app`` on
    a stop; a ``stop`` is therefore not necessarily a completion, and a
    ``start`` is not necessarily a new job.
```

with

```
    ``phase`` is ``"start"``, ``"pause"``, ``"resume"`` or ``"stop"``.
    ``trigger`` is the raw string, and what it means is the consumer's
    business — see ``MowerTriggerEvent`` for the same rule stated at length.
    The vocabulary observed so far is ``schedule``/``app``/``reborn`` on a
    start, ``continue``/``schedule`` on a resume, ``lowBattery``/``schedule``
    on a pause and ``workComplete``/``app`` on a stop; a ``stop`` is therefore
    not necessarily a completion, and a ``start`` is not necessarily a new
    job.

    ``job_type`` is the topic's middle segment — ``schedule``, ``spotarea``,
    ``border`` or ``auto`` — taken from the handler class, not the payload.
    It is the one signal that names the schedule's job the same way on every
    firmware: the O1200's ``trigger: continue`` and the G1-800's
    ``triggerType: schedule`` both arrive on ``mow-schedule-resume`` (issue
    #120). It is not the ``cleanState.content.type`` that
    ``MowerStateRecord.job_type`` keeps for the mow command (``spotArea``
    there, ``spotarea`` here), and the two are not merged.
```

and, further down the same docstring, replace

```
    ``mowed_area`` and ``work_area`` are square metres as floats, present on a
    stop and absent on a start. They are not converted to the centimetre
```

with

```
    ``mowed_area`` and ``work_area`` are square metres as floats, present on a
    stop and, on the firmware that sends them there, on a pause (issue #120),
    and absent on a start or resume. They are not converted to the centimetre
```

In `_OnMowJobEdge`, add the class variable and a docstring paragraph, and pass it on:

```python
class _OnMowJobEdge(MessageBody, ABC):
    """One job edge from a task bury point.

    ``MessageBody``, not ``MessageBodyDataDict`` like most of this module: the
    bury point's fields sit directly under ``body``, with no ``data`` wrapper.
    ``OnChargeState._handle_body`` overrides at the same level for its own
    reason.

    ``ABC`` is load-bearing, not decoration: ``Message.__init_subclass__``
    demands a ``NAME`` on every subclass and exempts only classes with ``ABC``
    directly in ``__bases__``. Without it this base itself fails to define,
    at import time. Pinned in ``test_contract.py``.

    ``PHASE`` and ``JOB_TYPE`` are the topic's last two segments, set per
    subclass: the payload names neither.
    """

    PHASE: ClassVar[str]
    JOB_TYPE: ClassVar[str]
```

and in `_handle_body`, the constructor call becomes:

```python
        event_bus.notify(
            MowerJobEdgeEvent(
                phase=cls.PHASE,
                trigger=trigger,
                job_type=cls.JOB_TYPE,
                # ``cuttedArea`` is the border dialect's ``mowedArea`` (issue #74).
                mowed_area=_as_area(body.get("mowedArea", body.get("cuttedArea"))),
                work_area=_as_area(body.get("workArea")),
            )
        )
```

Add `JOB_TYPE = "<type>"` under `PHASE` in each of the eight existing subclasses (`schedule` for `OnMowScheduleStart`/`Stop`, `spotarea` for `OnMowSpotAreaStart`/`Stop`, `border` for `OnMowBorderStart`/`Stop`, `auto` for `OnMowAutoStart`/`Stop`), e.g.:

```python
class OnMowScheduleStart(_OnMowJobEdge):
    """A scheduled job begins."""

    NAME = "onFwBuryPoint-bd_task-mow-schedule-start"
    PHASE = "start"
    JOB_TYPE = "schedule"
```

Then add the four new subclasses, placed with their type's siblings (schedule ones after `OnMowScheduleStop`, spot-area ones after `OnMowSpotAreaStop`):

```python
class OnMowSchedulePause(_OnMowJobEdge):
    """A scheduled job pauses: a schedule window closing, or a charge break."""

    NAME = "onFwBuryPoint-bd_task-mow-schedule-pause"
    PHASE = "pause"
    JOB_TYPE = "schedule"


class OnMowScheduleResume(_OnMowJobEdge):
    """A scheduled job resumes: a schedule window opening, or after a charge break.

    On a G1-800 on firmware 1.36.208 this is the only word that a scheduled
    window has opened: no state message follows (issue #120).
    """

    NAME = "onFwBuryPoint-bd_task-mow-schedule-resume"
    PHASE = "resume"
    JOB_TYPE = "schedule"
```

```python
class OnMowSpotAreaPause(_OnMowJobEdge):
    """A zone job pauses (issue #73)."""

    NAME = "onFwBuryPoint-bd_task-mow-spotarea-pause"
    PHASE = "pause"
    JOB_TYPE = "spotarea"


class OnMowSpotAreaResume(_OnMowJobEdge):
    """A zone job resumes (issue #73)."""

    NAME = "onFwBuryPoint-bd_task-mow-spotarea-resume"
    PHASE = "resume"
    JOB_TYPE = "spotarea"
```

In the module docstring, replace

```
``MowerJobEdgeEvent`` republishes the task bury points that mark a job's own
boundaries — ``onFwBuryPoint-bd_task-mow-<type>-{start,stop}`` for the four
types ``auto``, ``schedule``, ``spotarea`` and ``border``, eight more messages
the library has no handler for. They are the only thing on the wire that
```

with

```
``MowerJobEdgeEvent`` republishes the task bury points that mark a job's own
boundaries — ``onFwBuryPoint-bd_task-mow-<type>-{start,stop}`` for the four
types ``auto``, ``schedule``, ``spotarea`` and ``border``, plus the ``pause``
and ``resume`` edges of ``schedule`` and ``spotarea``, twelve more messages
the library has no handler for. They are the only thing on the wire that
```

In `deebot_patch/__init__.py`, add the four names to the `from .messages import (...)` block and to the `apply()` tuple, in the same alphabetical position the existing `OnMow*` entries use (`OnMowSchedulePause`, `OnMowScheduleResume` before `OnMowScheduleStart`; `OnMowSpotAreaPause`, `OnMowSpotAreaResume` before `OnMowSpotAreaStart`).

In `tests/deebot_patch/test_contract.py` lines 318-319, change `each of the four` to `each of the twelve`:

```python
    # _OnMowJobEdge is a deliberately NAME-less base: each of the twelve
    # bury-point handlers under it carries its own NAME, and there is no name
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `"D:/private-projects/ha-ecovacs-mower/v314/Scripts/python.exe" -m pytest tests/deebot_patch/ -p no:homeassistant -q`
Expected: all pass, none skipped.

Run: `PYTHONPATH=.claude/unskip "D:/private-projects/ha-ecovacs-mower/v314/Scripts/python.exe" -m pytest tests/test_event.py tests/test_sensor.py -p no:homeassistant -p unskip_win32 -q`
Expected: all pass (84 on the baseline, plus the two new ones).

- [ ] **Step 5: Commit**

```bash
git add custom_components/ecovacs_mower/deebot_patch/messages.py custom_components/ecovacs_mower/deebot_patch/__init__.py tests/deebot_patch/test_messages.py tests/deebot_patch/test_contract.py tests/test_event.py tests/test_sensor.py
git commit -m "feat: carry the job type on every job edge, register the pause and resume edges (#120)"
```

---

### Task 2: The two halves of a departure in the state record

**Files:**
- Modify: `custom_components/ecovacs_mower/deebot_patch/state_precedence.py`
- Modify: `tests/deebot_patch/test_state_precedence.py`

**Interfaces:**
- Consumes: nothing.
- Produces (all in `state_precedence.py`):
  - `DEPARTURE_WINDOW: float = 60.0`
  - `MowerStateRecord.departure_edge_at: float | None = None`, `MowerStateRecord.charge_left_at: float | None = None`
  - `MowerStateRecord.note_departure_edge(now: float) -> bool`
  - `MowerStateRecord.note_charge_left(now: float) -> bool`
  - `MowerStateRecord.cancel_departure() -> None`
  - `MowerStateRecord.dock()` now also clears `charge_left_at`; `move()` unchanged and touches neither stamp.

- [ ] **Step 1: Write the failing tests**

In `tests/deebot_patch/test_state_precedence.py`, extend the import and add `pytest`:

```python
import gc
from unittest.mock import AsyncMock, Mock

import pytest
from deebot_client.event_bus import EventBus
from deebot_client.models import State

from custom_components.ecovacs_mower.deebot_patch.state_precedence import (
    DEPARTURE_WINDOW,
    MowerStateRecord,
    map_id_for,
    record_for,
    register,
)
```

Append:

```python
def test_a_new_record_has_no_departure_half() -> None:
    record = MowerStateRecord()
    assert record.departure_edge_at is None
    assert record.charge_left_at is None


def test_an_edge_then_an_unplug_at_the_window_pairs() -> None:
    """Issue #120: the G1-800's resume edge, then isCharging 0 three seconds later."""
    record = MowerStateRecord()

    assert record.note_departure_edge(100.0) is False
    assert record.departure_edge_at == 100.0
    assert record.note_charge_left(100.0 + DEPARTURE_WINDOW) is True
    assert record.departure_edge_at is None
    assert record.charge_left_at is None


def test_an_unplug_then_an_edge_at_the_window_pairs() -> None:
    """The O1200's charge-break order: the unplug comes 8 s before the edge."""
    record = MowerStateRecord()

    assert record.note_charge_left(100.0) is False
    assert record.charge_left_at == 100.0
    assert record.note_departure_edge(100.0 + DEPARTURE_WINDOW) is True
    assert record.departure_edge_at is None
    assert record.charge_left_at is None


@pytest.mark.parametrize("edge_first", [True, False])
def test_two_halves_past_the_window_do_not_pair(edge_first: bool) -> None:
    """The stale half is cleared, not kept; the newer one waits for its partner."""
    record = MowerStateRecord()
    first, second = record.note_departure_edge, record.note_charge_left
    stale, fresh = "departure_edge_at", "charge_left_at"
    if not edge_first:
        first, second = second, first
        stale, fresh = fresh, stale

    first(100.0)
    late = 100.0 + DEPARTURE_WINDOW + 0.001

    assert second(late) is False
    assert getattr(record, stale) is None
    assert getattr(record, fresh) == late


def test_a_second_edge_replaces_the_first() -> None:
    """The window runs from the newest announcement, not the oldest."""
    record = MowerStateRecord()
    record.note_departure_edge(100.0)
    record.note_departure_edge(150.0)

    assert record.departure_edge_at == 150.0
    assert record.note_charge_left(150.0 + DEPARTURE_WINDOW) is True


def test_cancel_departure_clears_both_halves() -> None:
    record = MowerStateRecord(departure_edge_at=1.0, charge_left_at=2.0)

    record.cancel_departure()

    assert record.departure_edge_at is None
    assert record.charge_left_at is None


def test_dock_clears_only_the_unplug() -> None:
    """Charging again refutes the unplug. It does not refute the announcement:
    dock() also runs on every repeat confirmation of a dock already known, and
    being on the charger a second after a resume edge says nothing about a
    departure three seconds away."""
    record = MowerStateRecord(departure_edge_at=1.0, charge_left_at=2.0)

    record.dock()

    assert record.departure_edge_at == 1.0
    assert record.charge_left_at is None


def test_move_clears_neither_half() -> None:
    """OnPos calls move() for a sample away from the dock and publishes no
    state; a position between the two halves must not break the pair."""
    record = MowerStateRecord(departure_edge_at=1.0, charge_left_at=2.0)

    record.move()

    assert record.departure_edge_at == 1.0
    assert record.charge_left_at == 2.0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `"D:/private-projects/ha-ecovacs-mower/v314/Scripts/python.exe" -m pytest tests/deebot_patch/test_state_precedence.py -p no:homeassistant -q`
Expected: collection ERROR, `ImportError: cannot import name 'DEPARTURE_WINDOW'`.

- [ ] **Step 3: Implement**

In `state_precedence.py`, replace the module docstring's first paragraph

```
Two of them. The mower reports charging and a paused plan as two orthogonal
facts, and ``capabilities.state`` collapses them into a single ``StateEvent``;
this module holds the little state needed to prefer the first over the second
(issue #67). It also remembers the id of the map the mower is using, read from
the envelope of every map message, because a border job has to name it
(issue #12), and the type of the running job, because pause, resume and stop
have to name that (issue #94). Plus the registry that says which event buses
belong to a patched mower at all.
```

with

```
The mower reports charging and a paused plan as two orthogonal facts, and
``capabilities.state`` collapses them into a single ``StateEvent``; this
module holds the little state needed to prefer the first over the second
(issue #67). It also remembers the id of the map the mower is using, read from
the envelope of every map message, because a border job has to name it
(issue #12), the type of the running job, because pause, resume and stop have
to name that (issue #94), and the two halves of a departure from the dock,
because a scheduled run on some firmware leaves without a state message
(issue #120). Plus the registry that says which event buses belong to a
patched mower at all.
```

Add the constant and a helper after the `TYPE_CHECKING` block, before the dataclass:

```python
# How far apart, in monotonic seconds, a start or resume bury point and the
# mower's own "stopped charging" push may arrive and still be one departure
# from the dock (issue #120). The observed gaps are 3 to 8 s, in both orders;
# a minute leaves room for a slower departure (a longer boot, a wait for a
# positioning fix) while keeping a false pair, an unplug on a full battery
# near an announcement that never left, to one minute around it.
DEPARTURE_WINDOW = 60.0


def _within_window(stamp: float | None, now: float) -> bool:
    """Whether the other half, stamped at *stamp*, is close enough to pair."""
    return stamp is not None and now - stamp <= DEPARTURE_WINDOW
```

Replace the dataclass header, docstring and fields with:

```python
@dataclass
class MowerStateRecord:
    """What this device is doing, as far as the handlers need to remember.

    ``departure_edge_at`` and ``charge_left_at`` are the two halves of a
    departure from the dock (issue #120), in monotonic seconds: a start or
    resume bury point, and the mower's own ``onChargeState`` push saying it
    stopped charging. Neither alone proves the mower left — an announcement
    can stay on the charger in a rain delay, and an unplug can be a full
    battery — so CLEANING is written only when both arrive within
    ``DEPARTURE_WINDOW``, in either order. The handlers that report a state
    cancel both, since a reported state is better evidence than either half,
    and so does a pause or stop bury point: the job it announced paused or
    ended before the mower left. ``dock()`` clears only the unplug, and
    ``move()`` neither.
    """

    docked: bool = False
    suppressed: State | None = None
    map_id: str | None = None
    job_type: str | None = None
    departure_edge_at: float | None = None
    charge_left_at: float | None = None
```

Add the three methods after `end_job()`:

```python
    def note_departure_edge(self, now: float) -> bool:
        """A start or resume bury point arrived at *now*; True if that completes a departure.

        Completing one clears both halves. Otherwise this becomes the waiting
        half, and an unplug older than the window is forgotten.
        """
        paired = _within_window(self.charge_left_at, now)
        self.departure_edge_at = None if paired else now
        self.charge_left_at = None
        return paired

    def note_charge_left(self, now: float) -> bool:
        """The mower pushed that it stopped charging at *now*; True if that completes a departure.

        The mirror of ``note_departure_edge()``.
        """
        paired = _within_window(self.departure_edge_at, now)
        self.charge_left_at = None if paired else now
        self.departure_edge_at = None
        return paired

    def cancel_departure(self) -> None:
        """Forget both halves: the mower reported a state, or its job paused or ended."""
        self.departure_edge_at = None
        self.charge_left_at = None
```

Change `dock()` to:

```python
    def dock(self) -> None:
        """The mower is on its charger.

        Charging again refutes an unplug, so that half goes. The
        announcement half stays: this also runs on every repeat confirmation
        of a dock already known (a ``getChargeState`` answer, a refresh, a
        repeated push), and the window already bounds it (issue #120).
        """
        self.docked = True
        self.charge_left_at = None
```

Add one sentence to the end of `move()`'s docstring:

```
        Neither half of a departure is touched: ``OnPos`` calls this for a
        position sample away from the dock and publishes no state, and a
        sample between the two halves must not break the pair (issue #120).
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `"D:/private-projects/ha-ecovacs-mower/v314/Scripts/python.exe" -m pytest tests/deebot_patch/ -p no:homeassistant -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add custom_components/ecovacs_mower/deebot_patch/state_precedence.py tests/deebot_patch/test_state_precedence.py
git commit -m "feat: record the two halves of a departure from the dock (#120)"
```

---

### Task 3: Write mowing when the charger confirms a departure

**Files:**
- Modify: `custom_components/ecovacs_mower/deebot_patch/messages.py` (imports; new helpers before `_OnMowJobEdge`; `_OnMowJobEdge._handle_body`; `handle_clean_info` lines 664-692; `OnChargeInfo._handle_body_data_dict` lines 709-738; `OnChargeState` lines 741-779)
- Modify: `custom_components/ecovacs_mower/deebot_patch/commands.py:137-141` (docstring only)
- Modify: `custom_components/ecovacs_mower/controller.py:364-366` (comment only)
- Modify: `tests/deebot_patch/test_messages.py`
- Modify: `tests/test_controller.py`

**Interfaces:**
- Consumes: Task 1's `OnMowScheduleResume`, `OnMowSchedulePause`, the `_G1800_*`/`_O1200_*` fixtures; Task 2's `DEPARTURE_WINDOW`, `note_departure_edge()`, `note_charge_left()`, `cancel_departure()`.
- Produces: `messages.monotonic` (module-level name the tests patch); in `test_messages.py`: the `clock` fixture, `_Clock`, `_push(bus, message, body)`, `_last_state(bus)`, `_docked_mower()`, `_states(published)`, the fixtures `_UNPLUGGED` and `_PLUGGED_IN` (all used by Task 4), and, used only here, `_O1200_PUSH_0900`, `_O1200_PUSH_1147`, `_POS_AWAY_FROM_THE_DOCK`, `_report_paused`, `_report_go_charging`, `_report_alert`, `_answer_charging`, `_push_charging`, `_report_docked`.

- [ ] **Step 1: Write the failing tests**

In `tests/deebot_patch/test_messages.py`, extend the imports:

```python
from custom_components.ecovacs_mower.deebot_patch import apply
from custom_components.ecovacs_mower.deebot_patch import messages as messages_module
```

```python
from custom_components.ecovacs_mower.deebot_patch.state_precedence import (
    DEPARTURE_WINDOW,
    MowerStateRecord,
    register,
)
```

Append at the end of the file:

```python
# The charge pushes and the O1200's own state pushes around a departure, from
# the same two logs as the job edges above.
_UNPLUGGED = {"data": {"isCharging": 0, "mode": "slot"}}
_PLUGGED_IN = {"data": {"isCharging": 1, "mode": "slot"}}
_O1200_PUSH_0900 = {
    "data": {
        "trigger": "schedule",
        "other": "loading",
        "rotationPause": 0,
        "ssid": "0",
        "state": "clean",
        "cleanState": {
            "motionState": "working",
            "content": {"type": "auto", "subContent": {"type": "idle"}},
        },
    }
}
_O1200_PUSH_1147 = {
    "data": {
        "trigger": "continue",
        "other": "",
        "rotationPause": 0,
        "ssid": "1",
        "state": "clean",
        "cleanState": {
            "motionState": "working",
            "content": {"type": "auto", "subContent": {"type": "back"}},
        },
    }
}
_POS_AWAY_FROM_THE_DOCK = {
    "deebotPos": {"x": 5000, "y": 0, "a": 0, "invalid": 0},
    "chargePos": _CHARGER_UNKNOWN,
    "mid": "0",
}


class _Clock:
    """A monotonic clock the test steps by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    """Replace the clock the departure pairing reads (issue #120)."""
    stepped = _Clock()
    monkeypatch.setattr(messages_module, "monotonic", stepped)
    return stepped


def _push(bus: EventBus, message, body: dict[str, Any]) -> HandlingResult:
    """Deliver *body* the way the wire does, through ``handle()``."""
    return message.handle(bus, {"body": body})


def _last_state(bus: EventBus) -> State | None:
    event = bus.get_last_event(StateEvent)
    return None if event is None else event.state


def _docked_mower() -> tuple[EventBus, MowerStateRecord, list[StateEvent]]:
    """A registered mower on its charger, as the #120 run starts.

    Subscribed before the dock push, so the list holds every state notified
    from here on, the opening DOCKED included.
    """
    bus = _bus()
    record = register(bus)
    published = _collect(bus, StateEvent)
    _push(bus, OnChargeState, _PLUGGED_IN)
    return bus, record, published


def _states(published: list[StateEvent]) -> list[State]:
    return [event.state for event in published]


async def test_a_resume_from_the_dock_waits_for_the_charger(clock: _Clock) -> None:
    """Issue #120. An announcement alone is not proof that the mower left.

    A resume announced while the mower stays on its charger (a rain delay, a
    fault) would otherwise read mowing, and nothing would correct it: the
    poll publishes DOCKED only on isCharging 1, and the polled idle is
    discarded (#48).
    """
    bus, record, _ = _docked_mower()

    _push(bus, OnMowScheduleResume, _G1800_RESUME_1300)

    assert _last_state(bus) is State.DOCKED
    assert record.departure_edge_at == clock.now


async def test_the_unplug_after_a_resume_edge_writes_cleaning(clock: _Clock) -> None:
    """The #120 order: resume edge at 13:00:23.762, isCharging 0 at 13:00:26.501."""
    bus, record, published = _docked_mower()

    _push(bus, OnMowScheduleResume, _G1800_RESUME_1300)
    clock.now += 2.739
    _push(bus, OnChargeState, _UNPLUGGED)
    await asyncio.sleep(0)

    assert _states(published) == [State.DOCKED, State.CLEANING]
    assert record.docked is False
    assert record.departure_edge_at is None
    assert record.charge_left_at is None


async def test_an_unplug_before_the_resume_edge_pairs_too(clock: _Clock) -> None:
    """The O1200's charge-break order, without its own push: isCharging 0 at
    11:47:42.141, the resume edge at 11:47:50.636 (2026-10-12)."""
    bus, record, published = _docked_mower()

    _push(bus, OnChargeState, _UNPLUGGED)
    assert _last_state(bus) is State.DOCKED
    clock.now += 8.495
    _push(bus, OnMowScheduleResume, _O1200_RESUME_1147)
    await asyncio.sleep(0)

    assert _states(published) == [State.DOCKED, State.CLEANING]
    assert record.docked is False


@pytest.mark.parametrize("edge_first", [True, False])
async def test_neither_order_pairs_across_the_window(
    clock: _Clock, edge_first: bool
) -> None:
    bus, _, published = _docked_mower()
    steps = [
        (OnMowScheduleResume, _G1800_RESUME_1300),
        (OnChargeState, _UNPLUGGED),
    ]
    if not edge_first:
        steps.reverse()

    _push(bus, *steps[0])
    clock.now += DEPARTURE_WINDOW + 0.001
    _push(bus, *steps[1])
    await asyncio.sleep(0)

    assert _states(published) == [State.DOCKED]


async def test_charging_again_cancels_an_unplug(clock: _Clock) -> None:
    """dock() refutes the unplug: the mower is back on its charger."""
    bus, record, published = _docked_mower()

    _push(bus, OnChargeState, _UNPLUGGED)
    clock.now += 1
    _push(bus, OnChargeState, _PLUGGED_IN)
    clock.now += 1
    _push(bus, OnMowScheduleResume, _G1800_RESUME_1300)
    await asyncio.sleep(0)

    assert _states(published) == [State.DOCKED]
    assert record.charge_left_at is None


def _report_paused(bus: EventBus) -> None:
    handle_clean_info(
        bus,
        {"trigger": "none", "state": "clean", "cleanState": {"motionState": "pause"}},
    )


def _report_go_charging(bus: EventBus) -> None:
    OnChargeInfo._handle_body_data_dict(bus, {"trigger": "app", "state": "goCharging"})


def _report_alert(bus: EventBus) -> None:
    OnChargeInfo._handle_body_data_dict(bus, {"trigger": "alert", "state": "idle"})


@pytest.mark.parametrize(
    ("report", "reported"),
    [
        (_report_paused, State.PAUSED),
        (_report_go_charging, State.RETURNING),
        (_report_alert, State.ERROR),
    ],
    ids=["clean-info-paused", "charge-info-go-charging", "charge-info-alert"],
)
@pytest.mark.parametrize("edge_first", [True, False])
async def test_a_reported_state_between_the_two_halves_cancels_both(
    clock: _Clock, report, reported: State, edge_first: bool
) -> None:
    """A reported state is better evidence than either half, and a stale half
    must not re-assert CLEANING over a PAUSED pushed in between."""
    bus, record, published = _docked_mower()
    steps = [
        (OnMowScheduleResume, _G1800_RESUME_1300),
        (OnChargeState, _UNPLUGGED),
    ]
    if not edge_first:
        steps.reverse()

    _push(bus, *steps[0])
    # Off the charger as far as the record knows, as OnPos leaves it, so the
    # PAUSED is published rather than withheld (#67).
    record.move()
    report(bus)

    assert record.departure_edge_at is None
    assert record.charge_left_at is None

    clock.now += 3
    _push(bus, *steps[1])
    await asyncio.sleep(0)

    assert _states(published) == [State.DOCKED, reported]


async def test_a_withheld_paused_plan_does_not_cancel_the_departure(
    clock: _Clock,
) -> None:
    """A paused-plan push on the dock (#67) says the mower is still docked,
    which is what the pair is waiting to see resolved, not evidence against it."""
    bus, record, published = _docked_mower()

    _push(bus, OnMowScheduleResume, _G1800_RESUME_1300)
    _report_paused(bus)
    assert record.suppressed is State.PAUSED
    clock.now += 2.739
    _push(bus, OnChargeState, _UNPLUGGED)
    await asyncio.sleep(0)

    assert _states(published) == [State.DOCKED, State.CLEANING]
    # move() forgets the withheld plan: the mower is out, and a stale PAUSED
    # would let the mow command resume a plan that has moved on.
    assert record.suppressed is None


@pytest.mark.parametrize(
    ("message", "body"),
    [
        (OnMowSchedulePause, _G1800_PAUSE_1330),
        (OnMowScheduleStop, _G1800_STOP_1501),
        (OnMowSchedulePause, {"mapid": "0809159361"}),
    ],
    ids=["pause", "stop", "pause-without-a-trigger"],
)
async def test_a_pause_or_stop_edge_cancels_a_pending_departure(
    clock: _Clock, message, body: dict[str, Any]
) -> None:
    """The job paused or ended before the mower left, so a later unplug is not
    that job leaving. The cancel runs before the trigger parsing, so an edge
    the parsing gives up on still cancels."""
    bus, _, published = _docked_mower()

    _push(bus, OnMowScheduleResume, _G1800_RESUME_1300)
    _push(bus, message, body)
    clock.now += 2.739
    _push(bus, OnChargeState, _UNPLUGGED)
    await asyncio.sleep(0)

    assert _states(published) == [State.DOCKED]


def _answer_charging(bus: EventBus) -> None:
    GetChargeStateMower._handle_body(bus, {"code": 0, "data": {"isCharging": 1}})


def _push_charging(bus: EventBus) -> None:
    _push(bus, OnChargeState, _PLUGGED_IN)


def _report_docked(bus: EventBus) -> None:
    OnChargeInfo._handle_body_data_dict(bus, {"trigger": "workComplete", "state": "idle"})


@pytest.mark.parametrize(
    "report",
    [_answer_charging, _push_charging, _report_docked],
    ids=["get-charge-state-answer", "charge-state-push", "charge-info-idle"],
)
async def test_a_repeat_dock_report_does_not_stop_the_pair(
    clock: _Clock, report
) -> None:
    """Edge first, as at 13:00:23 -> 13:00:26. A dock already known is
    confirmed again by update_entity, by the refreshes and by repeated
    pushes; being on the charger one second after an announcement does not
    refute a departure three seconds away."""
    bus, _, published = _docked_mower()

    _push(bus, OnMowScheduleResume, _G1800_RESUME_1300)
    clock.now += 1
    report(bus)
    clock.now += 1.739
    _push(bus, OnChargeState, _UNPLUGGED)
    await asyncio.sleep(0)

    assert _states(published) == [State.DOCKED, State.CLEANING]


async def test_a_position_sample_between_the_two_halves_does_not_stop_the_pair(
    clock: _Clock,
) -> None:
    """OnPos calls move() for a sample away from the dock and publishes no
    state; a getPos from the map image's update_entity (#115) can land
    between the two halves."""
    bus, record, published = _docked_mower()

    _push(bus, OnMowScheduleResume, _G1800_RESUME_1300)
    OnPos._handle_body_data_dict(bus, _POS_AWAY_FROM_THE_DOCK)
    assert record.docked is False
    clock.now += 2.739
    _push(bus, OnChargeState, _UNPLUGGED)
    await asyncio.sleep(0)

    assert _states(published) == [State.DOCKED, State.CLEANING]


async def test_a_get_charge_state_answer_never_completes_a_pair(clock: _Clock) -> None:
    """Only the push counts. An answer is a snapshot, asked while the mower is
    docked, and pairing it would write CLEANING for a mower that never left."""
    bus, record, published = _docked_mower()

    _push(bus, OnMowScheduleResume, _G1800_RESUME_1300)
    clock.now += 2.739
    GetChargeStateMower._handle_body(bus, {"code": 0, "data": {"isCharging": 0}})
    await asyncio.sleep(0)

    assert _states(published) == [State.DOCKED]
    assert record.charge_left_at is None


async def test_an_edge_arms_after_a_restart_with_no_state_yet(clock: _Clock) -> None:
    """After a restart the record starts undocked, and a mower that has said
    nothing since must still be able to leave."""
    bus = _bus()
    register(bus)
    published = _collect(bus, StateEvent)

    _push(bus, OnMowScheduleResume, _G1800_RESUME_1300)
    clock.now += 2.739
    _push(bus, OnChargeState, _UNPLUGGED)
    await asyncio.sleep(0)

    assert _states(published) == [State.CLEANING]


async def test_an_edge_arms_when_only_the_record_has_left_the_dock(
    clock: _Clock,
) -> None:
    """OnPos can clear record.docked without publishing, leaving DOCKED as the
    bus's last state."""
    bus, record, published = _docked_mower()
    record.move()

    _push(bus, OnMowScheduleResume, _G1800_RESUME_1300)
    clock.now += 2.739
    _push(bus, OnChargeState, _UNPLUGGED)
    await asyncio.sleep(0)

    assert _states(published) == [State.DOCKED, State.CLEANING]


@pytest.mark.parametrize("pushed", [State.CLEANING, State.PAUSED, State.RETURNING])
async def test_an_edge_after_a_pushed_state_away_from_the_dock_does_not_arm(
    clock: _Clock, pushed: State
) -> None:
    """The O1200's start edge lands 13 s after its own CLEANING push, and a
    pause or a rain return in those 13 s must not be overwritten. The same
    rule keeps a repeated reborn start out on the lawn from leaving a half
    armed for a later unplug."""
    bus = _bus()
    record = register(bus)
    bus.notify(StateEvent(pushed))

    _push(bus, OnMowScheduleResume, _G1800_RESUME_1300)
    assert record.departure_edge_at is None
    clock.now += 2.739
    _push(bus, OnChargeState, _UNPLUGGED)

    assert _last_state(bus) is pushed


@pytest.mark.parametrize(
    "body",
    [{"msg": "fail", "code": "30007"}, _PLUGGED_IN],
    ids=["fail-shaped", "charging"],
)
async def test_a_charge_push_other_than_an_unplug_stamps_nothing(
    clock: _Clock, body: dict[str, Any]
) -> None:
    """Checked through the pair, not the stamp: both bodies reach dock(),
    which clears charge_left_at anyway, so only a missing CLEANING proves
    nothing was stamped."""
    bus, record, published = _docked_mower()

    _push(bus, OnMowScheduleResume, _G1800_RESUME_1300)
    clock.now += 1
    _push(bus, OnChargeState, body)
    await asyncio.sleep(0)

    assert _states(published) == [State.DOCKED]
    assert record.departure_edge_at is not None


async def test_a_start_edge_without_a_trigger_still_counts(clock: _Clock) -> None:
    """The departure is settled before the trigger parsing gives up."""
    bus, _, published = _docked_mower()

    result = _push(bus, OnMowScheduleStart, {"mapId": "511702305"})
    clock.now += 7
    _push(bus, OnChargeState, _UNPLUGGED)
    await asyncio.sleep(0)

    # Through handle(), the library's _handle_error_or_analyse wrapper turns
    # the handler's ANALYSE into ANALYSE_LOGGED on the way out.
    assert result.state is HandlingState.ANALYSE_LOGGED
    assert _states(published) == [State.DOCKED, State.CLEANING]


async def test_an_unregistered_bus_gets_no_state_from_a_departure(
    clock: _Clock,
) -> None:
    """A Deebot vacuum on the same account. MESSAGES is global, so these
    handlers see its pushes too, and must publish nothing of their own."""
    bus = _bus()
    published = _collect(bus, StateEvent)

    _push(bus, OnMowScheduleResume, _G1800_RESUME_1300)
    clock.now += 2.739
    result = _push(bus, OnChargeState, _UNPLUGGED)
    await asyncio.sleep(0)

    assert published == []
    assert result.state is HandlingState.SUCCESS


async def test_pause_and_stop_edges_publish_no_state() -> None:
    """What follows them says what the mower does: return-normal-start, then
    isCharging 1. A PAUSED of their own would flash for 0.35 s."""
    bus = _bus()
    register(bus)
    bus.notify(StateEvent(State.CLEANING))

    _push(bus, OnMowSchedulePause, _G1800_PAUSE_1330)
    _push(bus, OnMowScheduleStop, _G1800_STOP_1501)

    assert _last_state(bus) is State.CLEANING


@pytest.mark.parametrize(
    ("push", "steps"),
    [
        (
            _O1200_PUSH_0900,
            [(OnMowScheduleStart, _O1200_START_0901), (OnChargeState, _UNPLUGGED)],
        ),
        (
            _O1200_PUSH_1147,
            [(OnChargeState, _UNPLUGGED), (OnMowScheduleResume, _O1200_RESUME_1147)],
        ),
    ],
    ids=["scheduled-start", "charge-break-resume"],
)
async def test_an_o1200_departure_notifies_cleaning_once(
    clock: _Clock, push: dict[str, Any], steps: list
) -> None:
    """The O1200 pushes its own CLEANING before both halves, so the pair
    writes nothing there (2026-10-12: 09:00:53, 11:47:35)."""
    bus, _, published = _docked_mower()

    _push(bus, OnScheduleTaskInfo, push)
    for message, body in steps:
        clock.now += 7
        _push(bus, message, body)
    await asyncio.sleep(0)

    assert _states(published) == [State.DOCKED, State.CLEANING]


async def test_a_repeated_unplug_after_a_completed_pair_changes_nothing(
    clock: _Clock,
) -> None:
    """An MQTT redelivery of the unplug must not notify a second CLEANING."""
    bus, record, published = _docked_mower()

    _push(bus, OnMowScheduleResume, _G1800_RESUME_1300)
    clock.now += 2.739
    _push(bus, OnChargeState, _UNPLUGGED)
    clock.now += 0.1
    _push(bus, OnChargeState, _UNPLUGGED)
    await asyncio.sleep(0)

    assert _states(published) == [State.DOCKED, State.CLEANING]
    assert record.docked is False
```

In `tests/test_controller.py`, add after `test_setup_polling_starts_on_leaving_and_stops_on_docking`:

```python
async def test_a_paired_departure_starts_the_poll_and_docking_stops_it() -> None:
    """Issue #120. A G1-800 leaving on its schedule sends no state message, so
    the CLEANING the departure pair writes is what has to start the poll.

    No ``hass`` fixture, so this also runs outside CI.
    """
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, Mock, patch

    from deebot_client.event_bus import EventBus

    from custom_components.ecovacs_mower.controller import EcovacsController
    from custom_components.ecovacs_mower.deebot_patch import register_mower_bus
    from custom_components.ecovacs_mower.deebot_patch.messages import (
        OnChargeState,
        OnMowScheduleResume,
    )

    controller = EcovacsController.__new__(EcovacsController)
    controller._hass = MagicMock()
    controller._unsub_polls = {}

    bus = EventBus(AsyncMock(), Mock(get_refresh_commands=lambda _event: []))
    register_mower_bus(bus)
    device = MagicMock()
    device.device_info = {"did": "did-1"}
    device.events = bus

    def charging(is_charging: int) -> dict:
        return {"body": {"data": {"isCharging": is_charging, "mode": "slot"}}}

    with patch(
        "custom_components.ecovacs_mower.controller.async_track_time_interval"
    ) as track:
        controller._setup_polling(device)

        OnChargeState.handle(bus, charging(1))
        await asyncio.sleep(0)
        track.assert_not_called()

        OnMowScheduleResume.handle(bus, {"body": {"triggerType": "schedule"}})
        await asyncio.sleep(0)
        track.assert_not_called()

        OnChargeState.handle(bus, charging(0))
        await asyncio.sleep(0)
        track.assert_called_once()

        OnChargeState.handle(bus, charging(1))
        await asyncio.sleep(0)
        track.return_value.assert_called_once()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `"D:/private-projects/ha-ecovacs-mower/v314/Scripts/python.exe" -m pytest tests/deebot_patch/test_messages.py -p no:homeassistant -q`
Expected: every test using the `clock` fixture errors with `AttributeError: <module ...messages> has no attribute 'monotonic'`; `test_pause_and_stop_edges_publish_no_state` passes already (it pins behaviour that must not change).

Run: `PYTHONPATH=.claude/unskip "D:/private-projects/ha-ecovacs-mower/v314/Scripts/python.exe" -m pytest tests/test_controller.py -p no:homeassistant -p unskip_win32 -q`
Expected: `test_a_paired_departure_starts_the_poll_and_docking_stops_it` FAILS at `track.assert_called_once()` after the unplug; the six `fixture 'hass' not found` errors are the baseline.

- [ ] **Step 3: Implement**

In `messages.py` imports, add after `from datetime import time`:

```python
from time import monotonic
```

and extend the state-precedence import:

```python
from .state_precedence import record_for

if TYPE_CHECKING:
    from deebot_client.event_bus import EventBus

    from .state_precedence import MowerStateRecord
```

Add these two helpers directly before `class _OnMowJobEdge`:

```python
def _may_be_on_charger(event_bus: EventBus, record: MowerStateRecord) -> bool:
    """Whether a start or resume edge may still be announcing a departure.

    Docked as far as the record knows, or nothing but DOCKED has been
    published: after a restart the record starts undocked, and ``OnPos`` can
    clear ``record.docked`` without publishing anything. A pushed CLEANING,
    PAUSED or RETURNING says the mower is already out, and an edge arriving
    after it — the O1200's start edge lands 13 s after its own push — must not
    arm anything (issue #120).
    """
    if record.docked:
        return True
    last = event_bus.get_last_event(StateEvent)
    return last is None or last.state is State.DOCKED


def _confirm_departure(event_bus: EventBus, record: MowerStateRecord) -> None:
    """Both halves of a departure arrived: the mower has left its charger.

    ``move()`` is what ``handle_clean_info`` calls on CLEANING, so the charge
    gate (#67) then sees the mower as off its charger.
    """
    record.move()
    event_bus.notify(StateEvent(State.CLEANING))
```

In `_OnMowJobEdge`, add a docstring paragraph after the one Task 1 added:

```
    A start or resume edge is also one half of a departure from the dock
    (issue #120): on a mower that may still be on its charger it is noted on
    the record, and the mower's own ``onChargeState`` unplug push within
    ``DEPARTURE_WINDOW``, in either order, completes it into CLEANING. A
    pause or stop cancels a departure still waiting for its other half. Both
    run before the trigger parsing, so an edge that parsing gives up on still
    counts.
```

and start `_handle_body` with the departure, before the trigger parsing:

```python
    @classmethod
    def _handle_body(cls, event_bus: EventBus, body: dict[str, Any]) -> HandlingResult:
        """Handle message->body."""
        if (record := record_for(event_bus)) is not None:
            if cls.PHASE in ("start", "resume"):
                if _may_be_on_charger(
                    event_bus, record
                ) and record.note_departure_edge(monotonic()):
                    _confirm_departure(event_bus, record)
            else:
                # The job paused or ended before the mower left, so a later
                # unplug is not this job leaving.
                record.cancel_departure()

        trigger = body.get("trigger")
        ...  # unchanged from here
```

In `handle_clean_info`, add the cancel after the withhold branch (inside `if record is not None:`), so it runs only when the status is published:

```python
        if status in (State.CLEANING, State.RETURNING):
            record.move()
        elif record.docked and status in (State.PAUSED, State.IDLE):
            # Remembered, not just dropped: the mow command still has to tell
            # resume from start, and it is the only reader of this.
            _LOGGER.debug("Withholding %s: mower is on its charger", status)
            record.suppressed = status
            return HandlingResult.success()

        # Issue #120. A state the mower reported is better evidence than
        # either half of a departure, and a stale half must not re-assert
        # CLEANING over it. Not on the withheld path above: a paused plan on
        # the dock says the mower is still docked, which is what a waiting
        # departure expects.
        record.cancel_departure()

    event_bus.notify(StateEvent(status))
    return HandlingResult.success()
```

In `OnChargeInfo._handle_body_data_dict`, cancel on the two non-dock paths:

```python
        if data.get("trigger") == "alert":
            # Issue #120: a reported state cancels a waiting departure, see
            # handle_clean_info.
            if (record := record_for(event_bus)) is not None:
                record.cancel_departure()
            event_bus.notify(StateEvent(State.ERROR))
            return HandlingResult.success()

        match data.get("state"):
            case "goCharging":
                status = State.RETURNING
            case "idle":
                status = State.DOCKED
            case _:
                return HandlingResult.analyse()

        # A mid-session docking. on_status stops the poll loop on DOCKED, so
        # nothing asks getChargeState again; without this the record would stay
        # unset until something restarted polling — one flap per docking.
        #
        # The dock does not cancel a waiting departure, only dock()'s unplug
        # half, the same as a repeat isCharging 1 (issue #120).
        if (record := record_for(event_bus)) is not None:
            if status is State.DOCKED:
                record.dock()
            elif status is State.RETURNING:
                record.cancel_departure()
                record.move()
```

In `OnChargeState`, add to the class docstring:

```
    The push is also the charger's half of a departure (issue #120): an
    ``isCharging: 0`` pairs with a start or resume bury point within
    ``DEPARTURE_WINDOW``, in either order, into CLEANING. Only the push does:
    a ``getChargeState`` answer is a snapshot, asked while the mower is
    docked, and pairing it would write CLEANING for a mower that never left.
```

and pair before forwarding in `_handle_body` (after the docstring, before the local import):

```python
        data = body.get("data")
        if (
            isinstance(data, dict)
            and data.get("isCharging") == 0
            and (record := record_for(event_bus)) is not None
            and record.note_charge_left(monotonic())
        ):
            _confirm_departure(event_bus, record)

        # Imported here, not at module level: GetChargeStateMower lives in
        # commands.py, which already imports from this module, and a top-level
        # import back the other way would make the two files a cycle.
        from .commands import GetChargeStateMower

        return GetChargeStateMower._handle_body(event_bus, body)
```

`OnChargeState._handle_body_data_dict` and `GetChargeStateMower`'s code stay unchanged.

In `commands.py`, `GetChargeStateMower`'s docstring, replace

```
    ``isCharging: 0`` deliberately does not clear the record. A mower parked on
    a full battery stops charging without having moved, and clearing there
    would hand the decision back to the plan state — issue #67 again. Only
    ``CLEANING`` or ``RETURNING`` clears it.
```

with

```
    ``isCharging: 0`` deliberately does not clear the record. A mower parked on
    a full battery may stop charging without having moved, and clearing there
    would hand the decision back to the plan state — issue #67 again. Only
    ``CLEANING`` or ``RETURNING`` clears it. Not every mower does stop: on
    2026-10-12 an O1200 at battery 100 reported itself charging until it
    left, in two departures (issue #120). Nor does an answer here pair with a
    start or resume bury point; only the ``onChargeState`` push does.
```

In `controller.py`, `on_status`, replace the comment

```python
            # There is nothing to ask about in the dock — the battery reports
            # itself — and leaving is the one transition the mower has not
            # been seen to drop.
```

with

```python
            # There is nothing to ask about in the dock — the battery reports
            # itself. A departure the mower does not push itself, as on a
            # G1-800's scheduled run, is written from the task bury points in
            # messages.py (issue #120), so it reaches this as well.
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `"D:/private-projects/ha-ecovacs-mower/v314/Scripts/python.exe" -m pytest tests/deebot_patch/ -p no:homeassistant -q`
Expected: all pass.

Run: `PYTHONPATH=.claude/unskip "D:/private-projects/ha-ecovacs-mower/v314/Scripts/python.exe" -m pytest tests/test_controller.py -p no:homeassistant -p unskip_win32 -q`
Expected: the new test passes; only the six `fixture 'hass' not found` errors remain, as on the baseline.

- [ ] **Step 5: Commit**

```bash
git add custom_components/ecovacs_mower/deebot_patch/messages.py custom_components/ecovacs_mower/deebot_patch/commands.py custom_components/ecovacs_mower/controller.py tests/deebot_patch/test_messages.py tests/test_controller.py
git commit -m "fix: write mowing once the charger confirms a departure from the dock (#120)"
```

---

### Task 4: The trip home, the #120 replay, and the docs

**Files:**
- Modify: `custom_components/ecovacs_mower/deebot_patch/messages.py` (module docstring lines 3-11; new class after `OnMowAutoStop`)
- Modify: `custom_components/ecovacs_mower/deebot_patch/__init__.py`
- Modify: `tests/deebot_patch/test_messages.py` (rename at line 1272; new tests)
- Modify: `CLAUDE.md` (the `messages.py` and `state_precedence.py` bullets)

**Interfaces:**
- Consumes: Task 3's `clock`, `_push`, `_last_state`, `_docked_mower`, `_states`, `_UNPLUGGED`, `_PLUGGED_IN`; Task 1's `_G1800_*` fixtures; Task 2's `cancel_departure()`.
- Produces: `OnReturnNormalStart` in `messages.py`, registered by `apply()`.

- [ ] **Step 1: Write the failing tests**

In `tests/deebot_patch/test_messages.py`, add `OnReturnNormalStart` to the `messages` import (after `OnRainDelay`). Rename `test_the_return_trip_is_not_registered` and update its docstring:

```python
def test_the_end_of_the_return_trip_is_not_registered() -> None:
    """onFwBuryPoint-bd_task-return-normal-stop also carries trigger workComplete.

    There it means the drive home finished, not the job. Registering it would
    make the sensor write a final value on a docking (issue #73). Its sibling
    return-normal-start is registered (issue #120); the docking this one
    announces already arrives as onChargeState isCharging 1.
    """
    apply()

    assert "onFwBuryPoint-bd_task-return-normal-stop" not in MESSAGES
```

Append at the end of the file:

```python
_G1800_RETURN_1330 = {
    "bid": "0031791804622040",
    "gid": "G1791648417330",
    "index": "0000000457",
    "mapid": "0809159361",
    "returnId": "1791804622039907",
    "sid": "2531791804622040",
    "triggerType": "schedule",
    "ts": "1791804622040",
    "workstate": 1,
}
_G1800_RETURN_1501 = {
    "bid": "4931791810117951",
    "gid": "G1791648417330",
    "index": "0000000526",
    "mapid": "0809159361",
    "mowId": "",
    "returnId": "1791810117949218",
    "sid": "9401791810117951",
    "triggerType": "schedule",
    "ts": "1791810117951",
    "workstate": 1,
}


def test_the_trip_home_is_registered() -> None:
    """Logged as 'Unknown message' until issue #120."""
    apply()

    assert MESSAGES["onFwBuryPoint-bd_task-return-normal-start"] is OnReturnNormalStart


async def test_the_trip_home_writes_returning() -> None:
    """On the G1-800 the bury point is the only word that it is driving home."""
    bus = _bus()
    record = register(bus)
    bus.notify(StateEvent(State.CLEANING))

    result = _push(bus, OnReturnNormalStart, _G1800_RETURN_1330)

    assert result.state is HandlingState.SUCCESS
    assert _last_state(bus) is State.RETURNING
    assert record.docked is False


async def test_a_late_trip_home_after_docking_writes_nothing() -> None:
    """It must not flip DOCKED back to RETURNING, or clear the paused plan
    #67 and the mow command rely on."""
    bus, record, published = _docked_mower()
    record.suppressed = State.PAUSED

    result = _push(bus, OnReturnNormalStart, _G1800_RETURN_1330)
    await asyncio.sleep(0)

    assert result.state is HandlingState.SUCCESS
    assert _states(published) == [State.DOCKED]
    assert record.docked is True
    assert record.suppressed is State.PAUSED


async def test_a_trip_home_after_a_pushed_error_keeps_the_error() -> None:
    """A mower stuck on its way home: goCharging, then an error, then the late
    bury point. The error stands until the mower reports something else."""
    bus = _bus()
    register(bus)
    OnChargeInfo._handle_body_data_dict(bus, {"trigger": "app", "state": "goCharging"})
    OnChargeInfo._handle_body_data_dict(bus, {"trigger": "alert", "state": "goCharging"})

    _push(bus, OnReturnNormalStart, _G1800_RETURN_1330)

    assert _last_state(bus) is State.ERROR


async def test_an_unregistered_bus_gets_no_state_from_the_trip_home() -> None:
    bus = _bus()
    published = _collect(bus, StateEvent)

    result = _push(bus, OnReturnNormalStart, _G1800_RETURN_1330)
    await asyncio.sleep(0)

    assert result.state is HandlingState.SUCCESS
    assert published == []


async def test_the_trip_home_is_not_a_job_edge() -> None:
    """A return is not a job boundary, and the progress sensor must not see one."""
    bus = _bus()
    register(bus)
    bus.notify(StateEvent(State.CLEANING))
    edges = _collect(bus, MowerJobEdgeEvent)

    _push(bus, OnReturnNormalStart, _G1800_RETURN_1330)
    await asyncio.sleep(0)

    assert edges == []


async def test_the_trip_home_cancels_a_pending_departure(clock: _Clock) -> None:
    bus, record, published = _docked_mower()

    _push(bus, OnMowScheduleResume, _G1800_RESUME_1300)
    record.move()  # as OnPos leaves it, so the trip home is published
    _push(bus, OnReturnNormalStart, _G1800_RETURN_1330)

    assert record.departure_edge_at is None
    assert record.charge_left_at is None

    clock.now += 3
    _push(bus, OnChargeState, _UNPLUGGED)
    await asyncio.sleep(0)

    assert _states(published) == [State.DOCKED, State.RETURNING]


async def test_the_issue_120_scheduled_run_replays_to_mowing_returning_and_docked(
    clock: _Clock,
) -> None:
    """The reporter's two schedule windows (13:00-13:30, 14:00-15:01) through
    the real handlers, at the logged intervals. Before #120 the state read
    docked throughout."""
    bus, _, published = _docked_mower()

    for step, (seconds, message, body, expected) in enumerate(
        [
            (0.0, OnMowScheduleResume, _G1800_RESUME_1300, State.DOCKED),
            (2.739, OnChargeState, _UNPLUGGED, State.CLEANING),
            (1796.951, OnMowSchedulePause, _G1800_PAUSE_1330, State.CLEANING),
            (0.349, OnReturnNormalStart, _G1800_RETURN_1330, State.RETURNING),
            (72.049, OnChargeState, _PLUGGED_IN, State.DOCKED),
            (1727.974, OnMowScheduleResume, _G1800_RESUME_1400, State.DOCKED),
            (3.198, OnChargeState, _UNPLUGGED, State.CLEANING),
            (3692.673, OnMowScheduleStop, _G1800_STOP_1501, State.CLEANING),
            (0.107, OnReturnNormalStart, _G1800_RETURN_1501, State.RETURNING),
        ]
    ):
        clock.now += seconds
        _push(bus, message, body)
        assert _last_state(bus) is expected, (step, message.NAME)
    await asyncio.sleep(0)

    assert _states(published) == [
        State.DOCKED,
        State.CLEANING,
        State.RETURNING,
        State.DOCKED,
        State.CLEANING,
        State.RETURNING,
    ]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `"D:/private-projects/ha-ecovacs-mower/v314/Scripts/python.exe" -m pytest tests/deebot_patch/test_messages.py -p no:homeassistant -q`
Expected: collection ERROR, `ImportError: cannot import name 'OnReturnNormalStart'`.

- [ ] **Step 3: Implement**

In `messages.py`, add after `class OnMowAutoStop`:

```python
class OnReturnNormalStart(MessageBody):
    """The mower sets off for its dock (issue #120).

    ``onFwBuryPoint-bd_task-return-normal-start``. On a G1-800 on firmware
    1.36.208 it is the only word that the trip home has begun: a scheduled run
    there sends no state message at all, so after its ``mow-schedule-pause``
    or ``-stop`` nothing else says RETURNING. No field of the body is read.

    Not a job edge, and not an ``_OnMowJobEdge``: a return is not a job
    boundary, and the progress sensor must not see it as one. Its sibling
    ``return-normal-stop`` stays unregistered — it carries ``workComplete``
    for the end of the drive, not of the job (issue #73), and the docking it
    announces already arrives as ``onChargeState isCharging: 1``.

    Two gates, because a bury point can arrive after the state has moved on.
    A docked mower writes nothing, so a late one cannot flip DOCKED back to
    RETURNING or clear the paused plan #67 and ``CleanMower`` rely on. A
    pushed ERROR writes nothing either: a mower stuck on its way home must not
    be hidden behind a late announcement, and the ERROR stands until the
    mower reports something else. Over any other state away from the dock
    RETURNING is right, because driving home is what the mower is doing.
    """

    NAME = "onFwBuryPoint-bd_task-return-normal-start"

    @classmethod
    def _handle_body(cls, event_bus: EventBus, body: dict[str, Any]) -> HandlingResult:
        """Handle message->body.

        Success either way, gated or not: the message was understood.
        """
        record = record_for(event_bus)
        if record is not None and not record.docked:
            last = event_bus.get_last_event(StateEvent)
            if last is None or last.state is not State.ERROR:
                # A reported state cancels a waiting departure, see
                # handle_clean_info.
                record.cancel_departure()
                record.move()
                event_bus.notify(StateEvent(State.RETURNING))
        return HandlingResult.success()
```

In the module docstring, replace

```
Without the latter two the entity never leaves "docked" during a scheduled run,
and never returns to "returning"/"docked" once the work is finished.
```

with

```
Without the latter two the entity never leaves "docked" during a scheduled run,
and never returns to "returning"/"docked" once the work is finished. A G1-800
on firmware 1.36.208 sends neither during a scheduled run (issue #120), so the
task bury points below are a third way out of "docked": a start or resume edge
paired with the mower's own ``onChargeState`` unplug push writes CLEANING, and
``OnReturnNormalStart`` writes RETURNING for the trip home.
```

and at the end of the ``MowerJobEdgeEvent`` paragraph of the module docstring, after `the library's own start command sends (issue #74).`, add:

```
``OnReturnNormalStart`` handles one more task bury point,
``onFwBuryPoint-bd_task-return-normal-start``. It is not a job edge and
publishes no ``MowerJobEdgeEvent``; it writes RETURNING for the trip home
(issue #120).
```

In `deebot_patch/__init__.py`, add `OnReturnNormalStart` to the `from .messages import (...)` block and to the `apply()` tuple, after `OnRainDelay` in both.

In `CLAUDE.md`, the `messages.py` bullet (line 45) becomes:

```markdown
- `messages.py` — `OnChargeInfo` and `OnScheduleTaskInfo`, the two unsolicited messages the library lacks a handler for, and `OnAreaParameter`, the push the mower broadcasts to every client whenever an area's parameters change, from Home Assistant or from the app. The task bury points (`mow-*-start/resume/pause/stop`, `_OnMowJobEdge`) publish `MowerJobEdgeEvent` with their job type; a start or resume edge paired with an `onChargeState isCharging: 0` push within 60 s, in either order, writes CLEANING, and `OnReturnNormalStart` (`return-normal-start`) writes RETURNING — on a G1-800's scheduled run they are the only signals there are (issue #120).
```

and in the `state_precedence.py` bullet (line 50), insert after `and what the suppressed state was (issue #67);`:

```markdown
 the two halves of a departure from the dock, `departure_edge_at` and `charge_left_at`, which the bury-point and charge handlers pair within `DEPARTURE_WINDOW`, a reported state or a pause or stop edge cancels, and a dock clears the unplug half of (issue #120);
```

so the sentence reads: "…that the mower is docked, which beats a paused plan, and what the suppressed state was (issue #67); the two halves of a departure from the dock, … (issue #120); and the id of the map the mower is using, …". Keep the bullet one line.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `"D:/private-projects/ha-ecovacs-mower/v314/Scripts/python.exe" -m pytest tests/deebot_patch/ -p no:homeassistant -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add custom_components/ecovacs_mower/deebot_patch/messages.py custom_components/ecovacs_mower/deebot_patch/__init__.py tests/deebot_patch/test_messages.py CLAUDE.md
git commit -m "fix: write returning from the trip-home bury point (#120)"
```

---

### Task 5: The *Job started* event entity (side track)

**Files:**
- Modify: `custom_components/ecovacs_mower/event.py`
- Modify: `custom_components/ecovacs_mower/strings.json` and `custom_components/ecovacs_mower/translations/en.json` (identically)
- Modify: `custom_components/ecovacs_mower/icons.json`
- Modify: `tests/test_event.py`
- Modify: `README.md` (platform table line 240; new section after "When a job ends", before "How far along the mower is")
- Modify: `CLAUDE.md` ("Entity platforms" paragraph)

**Interfaces:**
- Consumes: Task 1's `MowerJobEdgeEvent(phase, trigger, job_type, ...)` with phases `start`/`resume`.
- Produces: `EcovacsJobStartedEventEntity` in `event.py`, key and translation key `job_started`, event types `schedule`, `app`, `continue`, `reborn`, event data `{"phase", "job_type"}`; unique id `<did>_job_started`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_event.py`, add `import pytest` below the module docstring's imports (`from tests import requires_ha` stays). Widen `test_no_stale_event_translations_or_icons`:

```python
def test_no_stale_event_translations_or_icons() -> None:
    """Every key in strings.json/icons.json must belong to a real event entity.

    The converse of the tests above: they check description -> string/icon, not
    the other way around. Without this, a leftover key for a removed event entity would
    go unnoticed.
    """
    import json
    from pathlib import Path

    from custom_components.ecovacs_mower.event import (
        EcovacsJobStartedEventEntity,
        EcovacsLastJobEventEntity,
    )

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    strings = json.loads((root / "strings.json").read_text(encoding="utf-8"))
    icons = json.loads((root / "icons.json").read_text(encoding="utf-8"))
    keys = {
        EcovacsLastJobEventEntity.entity_description.translation_key,
        EcovacsJobStartedEventEntity.entity_description.translation_key,
    }

    assert set(strings["entity"]["event"]) <= keys
    assert set(icons["entity"]["event"]) <= keys
```

Append:

```python
def _bare_job_started_entity():
    """Built through its real ``__init__``, for the reason ``_bare_event_entity`` gives."""
    from unittest.mock import MagicMock, Mock

    from custom_components.ecovacs_mower.event import EcovacsJobStartedEventEntity

    device = Mock()
    device.device_info = MagicMock()
    device.device_info.__getitem__ = lambda _self, _key: "did-1"

    entity = EcovacsJobStartedEventEntity(device)
    entity.async_write_ha_state = Mock()
    return entity


def _edge(phase: str, trigger: str, job_type: str = "schedule"):
    from custom_components.ecovacs_mower.deebot_patch.messages import MowerJobEdgeEvent

    return MowerJobEdgeEvent(phase=phase, trigger=trigger, job_type=job_type)


def test_job_started_translations_and_icon() -> None:
    """Name, one state per event type, and an icon of its own."""
    import json
    from pathlib import Path

    from custom_components.ecovacs_mower.event import EcovacsJobStartedEventEntity

    root = Path(__file__).parent.parent / "custom_components" / "ecovacs_mower"
    strings = json.loads((root / "strings.json").read_text(encoding="utf-8"))
    icons = json.loads((root / "icons.json").read_text(encoding="utf-8"))
    description = EcovacsJobStartedEventEntity.entity_description
    entry = strings["entity"]["event"][description.translation_key]

    assert entry["name"] == "Job started"
    states = entry["state_attributes"]["event_type"]["state"]
    for event_type in description.event_types:
        assert event_type in states, event_type
    assert description.translation_key in icons["entity"]["event"]


def test_job_started_is_not_a_diagnostic() -> None:
    """It is meant for automations, so it belongs on the device's main card."""
    from custom_components.ecovacs_mower.event import EcovacsJobStartedEventEntity

    assert EcovacsJobStartedEventEntity.entity_description.entity_category is None


async def test_a_scheduled_resume_fires_with_its_phase_and_job_type() -> None:
    """Issue #120: the G1-800's schedule window opening, mow-schedule-resume
    with triggerType schedule. The reporter's weather check triggers on this."""
    entity = _bare_job_started_entity()

    await entity._on_job_edge(_edge("resume", "schedule"))

    attributes = entity.state_attributes
    assert attributes["event_type"] == "schedule"
    assert attributes["phase"] == "resume"
    assert attributes["job_type"] == "schedule"
    entity.async_write_ha_state.assert_called_once()


@pytest.mark.parametrize(
    ("phase", "trigger", "job_type"),
    [
        ("start", "schedule", "schedule"),
        ("resume", "continue", "schedule"),
        ("start", "app", "spotarea"),
        ("start", "reborn", "spotarea"),
    ],
)
async def test_every_observed_start_word_fires_verbatim(
    phase: str, trigger: str, job_type: str
) -> None:
    """The words stay raw: the O1200's resume says continue where the G1-800's
    says schedule, and job_type is what names the schedule's job."""
    entity = _bare_job_started_entity()

    await entity._on_job_edge(_edge(phase, trigger, job_type))

    assert entity.state_attributes["event_type"] == trigger
    assert entity.state_attributes["phase"] == phase
    assert entity.state_attributes["job_type"] == job_type


@pytest.mark.parametrize("phase", ["pause", "stop"])
async def test_a_pause_or_stop_fires_no_job_started(phase: str) -> None:
    """``app`` is a declared event type, so only the phase guard stops these."""
    entity = _bare_job_started_entity()

    await entity._on_job_edge(_edge(phase, "app"))

    assert entity.state_attributes["event_type"] is None
    entity.async_write_ha_state.assert_not_called()


async def test_an_unknown_start_word_fires_nothing_and_is_logged_once(caplog) -> None:
    """_trigger_event raises on a type outside event_types, so an unseen word
    is dropped, and surfaces once in the debug log."""
    import logging

    entity = _bare_job_started_entity()

    with caplog.at_level(logging.DEBUG):
        await entity._on_job_edge(_edge("start", "lowBattery"))
        await entity._on_job_edge(_edge("start", "lowBattery"))

    assert entity.state_attributes["event_type"] is None
    assert sum("is no event type" in record.message for record in caplog.records) == 1


async def test_a_replayed_start_does_not_fire_again() -> None:
    """EventBus.subscribe replays the last event to a new subscriber, and an
    entity-ID change re-adds the entity: held by identity, as _reported_ending
    is. Two genuine, equal-looking starts are distinct objects (_seq), so both
    fire."""
    entity = _bare_job_started_entity()
    start = _edge("start", "schedule")

    await entity._on_job_edge(start)
    await entity._on_job_edge(start)

    assert entity.async_write_ha_state.call_count == 1

    await entity._on_job_edge(_edge("start", "schedule"))

    assert entity.async_write_ha_state.call_count == 2


async def test_job_started_subscribes_to_the_job_edges() -> None:
    from unittest.mock import Mock

    from custom_components.ecovacs_mower.deebot_patch.messages import MowerJobEdgeEvent

    entity = _bare_job_started_entity()
    entity._subscribe = Mock()

    await entity.async_added_to_hass()

    calls = {call.args for call in entity._subscribe.call_args_list}
    assert (MowerJobEdgeEvent, entity._on_job_edge) in calls


def test_the_two_event_entities_have_distinct_unique_ids() -> None:
    """Same device, same platform: equal ids would make HA drop one of them."""
    started = _bare_job_started_entity()
    last_job = _bare_event_entity()

    assert started.unique_id == "did-1_job_started"
    assert started.unique_id != last_job.unique_id


async def test_job_started_exists_only_on_a_mower() -> None:
    """The gate EcovacsActivitySensor uses; Last mowing job stays on every device."""
    from unittest.mock import MagicMock

    from deebot_client.capabilities import DeviceType

    from custom_components.ecovacs_mower.event import (
        EcovacsJobStartedEventEntity,
        EcovacsLastJobEventEntity,
        async_setup_entry,
    )

    def _device(did: str, device_type: DeviceType):
        device = MagicMock()
        device.capabilities.device_type = device_type
        device.device_info = {"did": did}
        return device

    mower = _device("did-mower", DeviceType.MOWER)
    vacuum = _device("did-vacuum", DeviceType.VACUUM)
    config_entry = MagicMock()
    config_entry.runtime_data.devices = [mower, vacuum]

    added: list = []
    await async_setup_entry(MagicMock(), config_entry, added.extend)

    assert {
        e._device for e in added if isinstance(e, EcovacsJobStartedEventEntity)
    } == {mower}
    assert {
        e._device for e in added if isinstance(e, EcovacsLastJobEventEntity)
    } == {mower, vacuum}
```

- [ ] **Step 2: Run the tests to verify they fail**

This task runs in the side-track worktree, where `.claude/unskip` does not exist (see Tracks), so its `PYTHONPATH` points at the main worktree's copy.

Run: `PYTHONPATH="D:/private-projects/ha-ecovacs-mower/.claude/worktrees/fix-120-scheduled-run-state/.claude/unskip" "D:/private-projects/ha-ecovacs-mower/v314/Scripts/python.exe" -m pytest tests/test_event.py -p no:homeassistant -p unskip_win32 -q`
Expected: the new tests fail with `ImportError: cannot import name 'EcovacsJobStartedEventEntity'`.

- [ ] **Step 3: Implement**

In `event.py`, extend the imports:

```python
from deebot_client.capabilities import CapabilityEvent, DeviceType
from deebot_client.device import Device
from deebot_client.events import CleanJobStatus, ReportStatsEvent, StateEvent
from deebot_client.events.base import Event
```

Add to the module docstring, as its last paragraph:

```
The entity has a sibling, *Job started* (issue #120): the start and resume
edges of the same bury points, fired with the trigger word the mower sent and
the job type the topic names, so an automation can act when the app's
schedule starts the mower. Like *Last mowing job* it is a class rather than a
description entry: ``MowerJobEdgeEvent`` is no library capability, so there is
no ``capability_fn`` to describe it with.
```

Below `_TRIGGER_EVENT_TYPES`, add:

```python
# The trigger words a start or resume has been seen to carry, used verbatim as
# event types (issue #120). Raw rather than mapped: the G1-800's
# mow-schedule-resume says ``schedule`` where the O1200's says ``continue``,
# so one "scheduled" type would be a guess, and ``job_type`` in the event
# data already names the schedule's job on every firmware. A start from Home
# Assistant may fire nothing yet: its word is unobserved, and so is the
# mow-auto-start edge itself. Unconfirmed guess: ``app``, the word on the
# only observed edge of such a job (``mow-auto-stop``, issue #74), which
# would fire with ``job_type: auto``. Any word not listed here gives a debug
# line until it is seen and added.
_JOB_START_TRIGGERS = ("schedule", "app", "continue", "reborn")
```

Replace `async_setup_entry` with:

```python
async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: EcovacsMowerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add entities for passed config_entry in HA."""
    controller = config_entry.runtime_data
    entities: list[EventEntity] = [
        EcovacsLastJobEventEntity(device) for device in controller.devices
    ]
    entities.extend(
        EcovacsJobStartedEventEntity(device)
        for device in controller.devices
        if device.capabilities.device_type is DeviceType.MOWER
    )
    async_add_entities(entities)
```

Append the class:

```python
class EcovacsJobStartedEventEntity(
    EcovacsEntity[CapabilityEvent[StateEvent]],
    EventEntity,
):
    """What started or resumed the mower's job, as the mower announces it (issue #120).

    An event entity, not a sensor (Rickard, 2026-10-13): Home Assistant writes
    no ``state_changed`` for a sensor that writes the value it already has, so
    after the first scheduled start a trigger on a sensor would never fire
    again. This fires once per edge, whatever the previous one was.

    It fires at the announcement, before the departure is confirmed: on the
    G1-800 about 3 s before the state reads mowing, on the O1200 about 13 s
    after. So a condition on the mower mowing, checked when this fires, is
    always false on the G1-800; an automation that must act once the mower
    is out waits for it in its actions. ``continue`` is a resume after a
    charge break and ``reborn`` repeats within one run on the O800 RTK, so
    an event is not always a new job; ``phase`` in the event data says
    which.

    ``capabilities.state`` is passed only because ``EcovacsEntity`` wants a
    capability; nothing subscribes to its event here.
    """

    entity_description = EventEntityDescription(
        key="job_started",
        translation_key="job_started",
        event_types=list(_JOB_START_TRIGGERS),
    )

    def __init__(self, device: Device) -> None:
        """Initialize entity."""
        super().__init__(device, device.capabilities.state)
        self._logged_triggers: set[str] = set()
        # The edge already fired, held by identity for the reason
        # EcovacsLastJobEventEntity._reported_ending gives: the bus replays
        # its last event to every new subscriber, and an entity-ID change
        # re-adds this object.
        self._reported_start: MowerJobEdgeEvent | None = None

    @override
    async def async_added_to_hass(self) -> None:
        """Set up the event listener now that hass is ready."""
        await super().async_added_to_hass()

        self._subscribe(MowerJobEdgeEvent, self._on_job_edge)

    async def _on_job_edge(self, event: MowerJobEdgeEvent) -> None:
        """Fire on a start or resume; a pause or stop is not this entity's."""
        if event.phase not in ("start", "resume"):
            return

        if event.trigger not in _JOB_START_TRIGGERS:
            if event.trigger not in self._logged_triggers:
                self._logged_triggers.add(event.trigger)
                _LOGGER.debug(
                    "Job %s with trigger %r, which is no event type",
                    event.phase,
                    event.trigger,
                )
            return

        if event is self._reported_start:
            return
        self._reported_start = event

        self._trigger_event(
            event.trigger, {"phase": event.phase, "job_type": event.job_type}
        )
        self.async_write_ha_state()
```

In `strings.json` **and** `translations/en.json`, add inside `"entity": {"event": {...}}`, before `"last_job"`:

```json
      "job_started": {
        "name": "Job started",
        "state_attributes": {
          "event_type": {
            "state": {
              "schedule": "Schedule",
              "app": "App",
              "continue": "Continued",
              "reborn": "Restarted"
            }
          }
        }
      },
```

In `icons.json`, inside `"entity": {"event": {...}}`, before `"last_job"`:

```json
      "job_started": {
        "default": "mdi:play-circle-outline"
      },
```

In `README.md`, the intro above the platform table (lines 227-228) counts the fixed entities; one more on every mower makes it:

```markdown
Forty-five fixed entities on the mower's device page, across eight platforms —
forty-six on the G1-800, which alone gets the "Mow border" button — plus four
```

(only those two lines change; the sentence continues as before). Replace the `event` row of the platform table (line 240) with:

```markdown
| `event` | 2 | Last mowing job (finished / finished with warnings / manually stopped — see below) and Job started (what started or resumed the job — see below) |
```

and insert this section after the "When a job ends" section, before `### How far along the mower is`, wrapped at about 80 columns like the rest of the file (the README keeps its own wrapping; the rule against hard-wrapped markdown covers issue, PR and thread text, not this file):

````markdown
### When a job starts

`event.<device>_job_started` fires each time the mower announces that
something started or resumed its job. The event type is the word the mower
sent, unchanged: `schedule`, `app`, `continue` (a resume after a charge
break) or `reborn`. The event data carries `phase`, `start` or `resume`, and
`job_type`, the kind of job: `schedule` for the app's schedule, `spotarea`
for a zone job, `border` for an edge cut and `auto` for a plain start.

Use `job_type` to recognise the schedule's job, not the event type. The same
announcement carries different words on different firmware: a G1-800 says
`schedule` when a schedule window opens, while an O1200 says `continue` when
its schedule resumes after a charge break. A start from Home Assistant may
fire nothing yet, because nobody has seen which word it carries.

The event fires at the announcement: on a G1-800 about three seconds before
the mower leaves its charger and reads `mowing`, on an O1200 about 13 seconds
after it started. A condition on `lawn_mower.<device>` being `mowing` is
checked when the event fires, so on a G1-800 it is always false. If an
automation must only act once the mower is out, wait for it in the actions
instead.

For example, sending the mower back when the schedule starts it in the rain.
The wait matters on a G1-800: a dock command sent while it is still on its
charger may well do nothing, and the mower leaves a few seconds later anyway.

```yaml
automation:
  - alias: No scheduled mowing in the rain
    triggers:
      - trigger: state
        entity_id: event.my_goat_job_started
        not_from: [unknown, unavailable]
    conditions:
      - condition: template
        value_template: "{{ trigger.to_state.attributes.job_type == 'schedule' }}"
      - condition: state
        entity_id: weather.home
        state: [rainy, pouring, lightning-rainy]
    actions:
      - wait_template: "{{ is_state('lawn_mower.my_goat', 'mowing') }}"
        timeout: "00:02:00"
        continue_on_timeout: false
      - action: lawn_mower.dock
        target:
          entity_id: lawn_mower.my_goat
```
````

In `CLAUDE.md`, the "Entity platforms" paragraph (line 74): change `The others (`sensor`, `switch`, `number`, `button`, `event`) are built declaratively:` to `The others (`sensor`, `switch`, `number`, `button`) are built declaratively:`, and append at the end of that paragraph:

```markdown
 `event` is not declarative at all: its two classes, *Last mowing job* and *Job started*, both subscribe to `MowerJobEdgeEvent`, which no `capability_fn` can describe.
```

and in the next paragraph change `The per-area parameter entities in `area_numbers.py` are the other exception:` to `The per-area parameter entities in `area_numbers.py` are another exception:`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH="D:/private-projects/ha-ecovacs-mower/.claude/worktrees/fix-120-scheduled-run-state/.claude/unskip" "D:/private-projects/ha-ecovacs-mower/v314/Scripts/python.exe" -m pytest tests/test_event.py tests/test_translations.py -p no:homeassistant -p unskip_win32 -q`
Expected: all pass.

Run: `"D:/private-projects/ha-ecovacs-mower/v314/Scripts/python.exe" -c "import json; a=json.load(open('custom_components/ecovacs_mower/strings.json',encoding='utf-8')); b=json.load(open('custom_components/ecovacs_mower/translations/en.json',encoding='utf-8')); print(a==b)"`
Expected: `True`.

- [ ] **Step 5: Commit**

```bash
git add custom_components/ecovacs_mower/event.py custom_components/ecovacs_mower/strings.json custom_components/ecovacs_mower/translations/en.json custom_components/ecovacs_mower/icons.json tests/test_event.py README.md CLAUDE.md
git commit -m "feat: add the Job started event entity (#120)"
```

---

## After the last task

- Cherry-pick Task 5's commit onto the main track after Task 4, resolve the `CLAUDE.md` conflict by keeping both edits, and rerun both local runners over everything: `tests/deebot_patch/` and `PYTHONPATH=.claude/unskip … -m pytest tests/ -p no:homeassistant -p unskip_win32 -q` (only the `hass`-fixture errors may remain).
- Report to Rickard and stop. Pushing the branch (CI is the source of truth), opening a PR and replying on #120 each wait for his word; the version bump is the maintainer's own commit after the merge.
