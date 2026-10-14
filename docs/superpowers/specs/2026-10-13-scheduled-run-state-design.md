# Scheduled runs that send no state message (issue #120)

## Problem

On a GOAT G1-800 (`77atlz`, firmware 1.36.208, integration 0.10.0) a run started by the Ecovacs app's schedule stays **Docked** in Home Assistant for its whole length. The reporter's log covers two schedule windows (13:00–13:30 and 14:00–15:30) of a job continued from the day before.

The mower sends no state message at all in those two hours: no `onCleanInfo_V2`, no `onScheduleTaskInfo`, and no `getCleanInfo` is ever asked. What it does send is task bury points and `onStats`:

```
13:00:23.762  onFwBuryPoint-bd_task-mow-schedule-resume   triggerType: schedule
13:00:26.410  onFwBuryPoint-bd_task-charge-stop
13:00:26.501  onChargeState                               isCharging: 0
13:30:23.452  onFwBuryPoint-bd_task-mow-schedule-pause    cuttedArea: 199.29
13:30:23.801  onFwBuryPoint-bd_task-return-normal-start   triggerType: schedule
13:31:35.849  onFwBuryPoint-bd_task-charge-start
13:31:35.850  onChargeState                               isCharging: 1   -> DOCKED (dropped as a repeat)
13:31:38.394  onFwBuryPoint-bd_task-return-normal-stop    triggerType: workcomplete
14:00:23.824  onFwBuryPoint-bd_task-mow-schedule-resume   triggerType: schedule
14:00:27.022  onChargeState                               isCharging: 0
15:01:59.695  onFwBuryPoint-bd_task-mow-schedule-stop     cuttedArea: 280.20
15:01:59.802  onFwBuryPoint-bd_task-return-normal-start   triggerType: schedule
```

`onStats` arrives about every two seconds inside both windows and not between them.

Two things combine:

1. **Nothing moves the state off DOCKED.** `mow-schedule-resume` and `-pause` are not registered (`Unknown message`). `mow-schedule-start` and `-stop` are, but `_OnMowJobEdge` only publishes `MowerJobEdgeEvent`, which feeds the progress sensor and the last-job event entity, never `StateEvent`. `return-normal-start` is not registered.
2. **The poll is off while docked.** `EcovacsController._setup_polling` stops polling on DOCKED, on the premise that leaving the dock is the one transition the mower has not been seen to drop. On this firmware a scheduled resume is exactly that transition, dropped.

The O1200 LiDAR Pro (`2i0fns`, firmware 1.13.10) is not affected: on 2026-10-12 a scheduled edge job pushed `onScheduleTaskInfo` with `state: clean` 13 seconds before its `mow-schedule-start` bury point, and Home Assistant went to *mowing* on that push.

The reporter's use case is to keep the schedule in the app and let Home Assistant add a weather check when a scheduled run starts. That needs the state to change and a way to tell a scheduled start from another one.

## Goals

- A scheduled start or resume on a mower that sends only bury points shows as *mowing* once the mower has left its charger, never on the announcement alone; the trip home after a pause or stop shows as *returning*, and docking shows as *docked*.
- Leaving DOCKED starts the poll, as it does for every other start.
- A new entity says what started or resumed the current job and which job it is, so an automation can tell the schedule's job from any other (the *Job started* event entity, §2).
- A start or resume bury point never overrides a state the mower pushed: on a mower that pushes its state, the push comes first and the pair writes nothing (see the ordering note in §1); where a pair would complete before a push, the push is a dropped duplicate. `return-normal-start` never overrides a dock or a pushed ERROR; over any other state away from the dock it writes RETURNING, because driving home is what the mower is then doing.

## Non-goals

- The brief *Unavailable* the reporter sees at the end of a run. The excerpt has no `AvailabilityEvent(available=False)` at all, so it needs its own log; it stays open on #120.
- Mapping trigger words to a single "scheduled" flag. The same bury point carries different words on different firmware (see §2), so the entity reports the words as they are and carries the job type beside them.
- `onStats` as a mowing signal. It is reliable on this G1-800 but unconfirmed on the O1200, and the bury points are already there.
- The G1-800's stop word. Its `mow-schedule-stop` says `triggerType: "workcomplete"` (lower-case `c`, see Fixtures), while `_OnMowJobEdge`'s `end_job()` check, `event.py`'s `_TRIGGER_EVENT_TYPES` and the progress sensor's completion all compare against `"workComplete"`. That is a separate bug in the existing stop handling and gets its own issue.

## Design

### 1. State from the task bury points (`deebot_patch/messages.py`, `deebot_patch/state_precedence.py`)

**New job edges.** Four more `_OnMowJobEdge` subclasses, one per bury point, each a `NAME`, a `PHASE` and a `JOB_TYPE` like the existing eight:

| Class | `NAME` | `PHASE` | `JOB_TYPE` |
|---|---|---|---|
| `OnMowScheduleResume` | `onFwBuryPoint-bd_task-mow-schedule-resume` | `resume` | `schedule` |
| `OnMowSchedulePause` | `onFwBuryPoint-bd_task-mow-schedule-pause` | `pause` | `schedule` |
| `OnMowSpotAreaResume` | `onFwBuryPoint-bd_task-mow-spotarea-resume` | `resume` | `spotarea` |
| `OnMowSpotAreaPause` | `onFwBuryPoint-bd_task-mow-spotarea-pause` | `pause` | `spotarea` |

All four are observed on real hardware (`MowerJobEdgeEvent`'s docstring, issue #73, the #120 log, and Geten's log of 2026-10-12). `MowerJobEdgeEvent.phase` widens from `"start"`/`"stop"` to those four values, and its docstring says so. The existing consumers already name the phases they act on explicitly (`sensor.py`'s `_on_job_edge` and `event.py`'s `_on_job_edge`), so a `pause` or `resume` reaches them and changes nothing there; tests pin that.

**The job type travels with the edge.** `JOB_TYPE` is a new `ClassVar[str]` on `_OnMowJobEdge`, set on all twelve subclasses to the bury point's middle segment (`schedule`, `spotarea`, `border`, `auto`), and `MowerJobEdgeEvent` gains a required field `job_type: str`, placed directly after `trigger` (before the defaulted `mowed_area`, `work_area` and `_seq`), that the handler fills from it. The three places in the tests that build the event without it (the helper at `tests/test_event.py` line 137, the inline construction at line 205 of the same file, and the helper at `tests/test_sensor.py` line 1085) gain the argument. The middle segment is the job type, not the trigger (`MowerJobEdgeEvent`'s docstring), and it is the one signal that says "this is the schedule's job" the same way on every firmware: the O1200's `trigger: continue` and the G1-800's `triggerType: schedule` both arrive on `mow-schedule-resume`. It is not the `cleanState.content.type` that `MowerStateRecord.job_type` holds for the mow command (`spotArea` there, `spotarea` here); the two are not merged.

**A start or resume from the dock is written only once the charger confirms the departure.** A bury point alone is not proof that the mower left: a resume announced while the mower stays on its charger (a rain delay, a fault) would read *mowing* with nothing to correct it. The charge half of the poll publishes DOCKED only on `isCharging: 1`, and the polled `idle` is discarded (#48), so a false CLEANING on a mower answering `isCharging: 0` would stand and the poll would never stop again. A false CLEANING also reaches every `StateEvent` consumer: it clears the fault latch (`fault.py`, CLEANING is in `_RECOVERY_STATES`), clears the activity sensor's rain flag (`EcovacsActivitySensor._on_state`), and makes the progress sensor trust a stats payload that firmware 1.36.208 does not zero between jobs (`_STATS_TRUSTED_STATES`).

So the CLEANING needs two facts that arrive within `DEPARTURE_WINDOW` (60 s, a module constant in `state_precedence.py`) of each other, **in either order**: the start or resume edge, and the mower's own `onChargeState` push saying it stopped charging. The observed gaps are 3 to 8 s (below); 60 s leaves room for a slower departure (a longer boot or a wait for a positioning fix) while keeping the false pair of accepted gap (5) to one minute around an announcement. Observed on hardware:

| Mower | Departure | First | Second |
|---|---|---|---|
| G1-800 `77atlz` (#120) | schedule window opens, 13:00 and 14:00 | resume edge | `isCharging: 0`, 3 s later |
| O1200 `2i0fns` (2026-10-12) | scheduled start after a night on the dock, battery 100 | start edge, 09:01:06 | `isCharging: 0`, 7 s later |
| O1200 `2i0fns` (2026-10-12) | app edge-cut start, battery 100 | `mow-border-rotate-start`, 18:14:15 | `isCharging: 0`, 7 s later |
| O1200 `2i0fns` (2026-10-12) | schedule resume after a charge break, 11:47 and 14:29 | `isCharging: 0` | resume edge, 8 s later |

On the O1200 the pair never decides anything: its own CLEANING push comes before both facts in all four departures, `onScheduleTaskInfo` for the three scheduled ones (09:00:53 before the 09:01:06 start edge, 11:47:35 before the 11:47:42 unplug, 14:29:12 before 14:29:19) and `onCleanInfo` for the app start (18:14:01 before the 18:14:15 edge), so the push has already moved the state and the pair writes nothing. The 18:14 edge is `mow-border-rotate-start`, which this design does not register (accepted gap (4)), so that row is evidence of the order and of the full-battery behaviour only. The O1200 rows are here as evidence of the order the two facts can arrive in, which the G1-800 may share on other departures.

The two O1200 starts at battery 100 also show that a mower parked on a full battery still reports itself charging until it leaves, so a full battery does not take the confirmation away. `GetChargeStateMower`'s docstring says a mower parked on a full battery stops charging without moving; neither log shows that. If it happens, it is accepted gap (5) below.

The record keeps both facts in two new fields of `MowerStateRecord`, `departure_edge_at: float | None` and `charge_left_at: float | None` (monotonic seconds), through two new methods that take the clock as an argument so tests can inject it:

- `note_departure_edge(now: float) -> bool` stores `now` as `departure_edge_at`. If `charge_left_at` is set and `now - charge_left_at <= DEPARTURE_WINDOW`, it clears both and returns True; otherwise it returns False. A stamp older than the window is cleared, not kept.
- `note_charge_left(now: float) -> bool` does the same with the roles swapped.
- `cancel_departure()` clears both fields. It is called by the paths that publish a state the mower reported, right before they notify: `handle_clean_info` when it publishes a status (the `alert` ERROR included), `OnChargeInfo._handle_body_data_dict` on its ERROR and RETURNING paths (the early `alert` branch and `goCharging`), and `OnReturnNormalStart` (below). `OnChargeInfo`'s `idle` → DOCKED path does not cancel: it calls `record.dock()` as today, which clears `charge_left_at` only, for the same reason as a repeat `isCharging: 1` (the `dock()` bullet below), so all three dock reports treat a pending departure alike. A reported state is better evidence than either stamp, and a stale stamp must not re-assert CLEANING over a PAUSED pushed in between. `handle_clean_info` does **not** cancel when it withholds a PAUSED or IDLE because the mower is on its charger (#67): a paused-plan push on the dock between the edge and the unplug says the mower is still docked, which is what the pair is waiting to see resolved, not evidence against it. A path that returns `analyse()` without notifying does not cancel either.
- A `pause` or `stop` edge also calls `cancel_departure()`, on a registered bus (see below).
- `dock()` clears `charge_left_at` only: charging again refutes the unplug. It keeps `departure_edge_at`, which the window already bounds, because `dock()` also runs on a repeat confirmation of a dock the record already knew (a `getChargeState` answer with `isCharging: 1` from `update_entity`, the first-subscriber refresh, the availability-flap refresh or the 30007 fail code, or a repeated push). Being on the charger one second after an announcement does not refute a departure three seconds away.
- `move()` does not touch either field. Besides `handle_clean_info` and `OnChargeInfo`, it is called by `OnPos._handle_body_data_dict` when a docked mower's position is clearly away from the dock, which publishes no state; a position sample between the two facts must not break the pair.

The two callers, both in handlers, per the rule in CLAUDE.md that state derived from the stream is never written by a subscription:

1. **The edge.** In `_OnMowJobEdge._handle_body`, for `PHASE in ("start", "resume")`, on a registered bus whose mower may be on its charger, call `record.note_departure_edge(monotonic())`. "May be on its charger" means `record.docked` is True, **or** the bus's last `StateEvent` is `None` or DOCKED (`event_bus.get_last_event(StateEvent)`): after a restart the record starts undocked and a mower that has said nothing since must still be able to arm, and `OnPos` can clear `record.docked` without publishing anything, leaving DOCKED as the last state. Arming then is safe because only a real push can complete the pair, and it is still refused in the adverse orders below, since a pushed CLEANING, PAUSED or RETURNING makes the last state something other than DOCKED. If the call returns True, call `record.move()` and notify `StateEvent(State.CLEANING)`. This runs before and independently of the trigger parsing: an edge with neither `trigger` nor `triggerType` still counts, and still returns `analyse()` for its `MowerJobEdgeEvent` as today.
2. **The push.** In `OnChargeState._handle_body`, before it forwards to `GetChargeStateMower._handle_body`: if the body's `data` is a dict whose `isCharging` is `0` and the bus is registered, call `record.note_charge_left(monotonic())`; if it returns True, call `record.move()` and notify `StateEvent(State.CLEANING)`; then forward as today. A body without a dict `data` (the fail shape), or with `isCharging` other than `0`, stamps nothing. The pairing lives in `_handle_body` only; `OnChargeState._handle_body_data_dict` is unchanged, so the replay tests drive `_handle_body` or `handle()`. Only the push counts. A `getChargeState` answer is a snapshot, not a change, and it is sent while the mower is docked by `update_entity`, by the refresh after an availability flap and by the first subscriber; letting it pair would bring the false CLEANING back. `GetChargeStateMower` therefore does not change.

`move()` is what `handle_clean_info` calls on CLEANING, so the charge gate (#67) then sees the mower as off its charger.

**The clock.** `messages.py` already binds `time` to `datetime.time` (`from datetime import time`, used by `_as_clock`), so it imports `from time import monotonic` and calls `monotonic()`; the handler tests monkeypatch `messages.monotonic` to step the clock.

A `pause` or `stop` sets no state, but on a registered bus it calls `record.cancel_departure()`: the mower announcing that the job paused or ended refutes a start or resume that has not been confirmed yet, so a later unplug must not complete it. Like the pairing for start and resume, the cancel runs before and independently of the trigger parsing, so a pause or stop edge without `trigger` or `triggerType` still cancels. What follows it already says what the mower does: `return-normal-start` (below) and then `onChargeState isCharging: 1`, which `GetChargeStateMower` turns into `record.dock()` and the library into DOCKED. Giving `pause` a PAUSED of its own would flash *paused* for 0.35 s before *returning*.

**The trip home.** A new handler `OnReturnNormalStart` for `onFwBuryPoint-bd_task-return-normal-start`, on the same `MessageBody` base as `_OnMowJobEdge` (the fields sit directly under `body`). It reads no field of the body. On a registered bus whose `record.docked` is False and whose last `StateEvent` is not ERROR, it calls `record.cancel_departure()`, `record.move()` and notifies `StateEvent(State.RETURNING)`; on a docked or unregistered bus, or after a pushed ERROR, it notifies nothing. The ERROR gate keeps a late bury point from hiding a mower that got stuck on its way home (`onChargeInfo goCharging`, then a pushed ERROR, then the late `return-normal-start`); the ERROR stands until the mower reports something else. `onFwBuryPoint-bd_task-return-normal-stop` stays unregistered: it carries `workComplete` and means the drive home finished, not the job (issue #73), and the docking it announces already arrives as `onChargeState isCharging: 1`. Either way it returns `success()`, since it understood the message. It is not a job edge: it publishes no `MowerJobEdgeEvent` and is not an `_OnMowJobEdge` subclass, because a return is not a job boundary and the progress sensor must not see it as one.

**Ordering on a mower that pushes its state.** Both gates exist because a bury point can arrive after the state has moved on. The O1200's `mow-schedule-start` lands 13 s after its `onScheduleTaskInfo` CLEANING push; a pause or a rain return in those 13 s must not be overwritten. That is covered because the push already moved the state: the mower is no longer docked, the last `StateEvent` is not DOCKED, and the late edge does not count. When `return-normal-start` arrives relative to `onChargeInfo goCharging` and docking on a pushing mower is unobserved; a late one after docking finds `record.docked` True and writes nothing, so it cannot flip DOCKED back to RETURNING or clear the `suppressed` plan state #67 and `CleanMower` rely on.

**Accepted gaps.** (1) A resume while the mower is out on the lawn writes no state: #120 is about leaving the dock, the only time the poll is off. (2) Only the observed pause and resume edges are registered (`schedule`, `spotarea`), so a charge-break resume of an `auto` or `border` job on a mower that sends only bury points still reads *docked* until a push or the next start; registering those is one subclass each once they are seen. (3) A `pause` out on the lawn that is not followed by `return-normal-start` leaves CLEANING standing until the next push or docking. (4) The O1200's `mow-border-rotate-*` edges are not registered here; they belong to #127. (5) A mower that stops charging on a full battery without moving would push an `isCharging: 0` with no departure behind it; if that lands within 60 s of a start or resume that never left (a rain delay), the pair completes and CLEANING and the poll stand until a push or an `isCharging: 1` arrives. Neither log shows such a push. (6) On the G1-800 the poll now runs during a scheduled run for the first time, and what its `getCleanInfo` answers mid-run is unobserved. A polled `idle` is discarded (#48) and a `clean`/`working` answer repeats the CLEANING, so neither changes anything; any other state it answers is published as the mower reported it, exactly as for a run started from Home Assistant. The reply on #120 asks the reporter for a debug log of a scheduled run on the fixed version. (7) A mower that leaves its charger more than `DEPARTURE_WINDOW` after its start or resume edge is not paired and reads *docked* through that run, as before this change, until a push or a later paired departure. Its `return-normal-start` helps only if `OnPos` has meanwhile cleared `record.docked`.

**Why the poll then starts.** `_setup_polling`'s `on_status` subscription starts the poll on any state other than DOCKED, so the paired CLEANING is what turns it on. The controller's code does not change; its comment that leaving the dock "is the one transition the mower has not been seen to drop" does (see Documentation).

**A poll cannot undo it.** The polled `getCleanInfo` answer `{"state": "idle"}` is already discarded for every command family (`_MowerCleanInfoHandling`, issue #48). On a mower that pushes its state before the pair completes, `cancel_departure()` has already cleared the stamps and nothing is written; if the pair completes first, the push's CLEANING equals the bus's last `StateEvent` and `EventBus.notify` drops it before any subscriber runs.

**Registration.** The five new classes are added to the tuple `apply()` registers in `deebot_patch/__init__.py`, next to the existing job edges. `OnChargeState` is already registered.

**No `MowerProfile` flag.** The flags gate behaviour whose raw values mean something different per class. The pair reads no raw value beyond the bury point's name and `isCharging: 0`, two messages every patched class already relies on (the edges feed the progress sensor and *Last mowing job* today, and `onChargeState` is the dock signal of #67), and on a mower that pushes its state the push comes first and the pair writes nothing (the ordering note above). A class that sends neither message is simply never paired. `return-normal-start` is new, and so far seen only on the G1-800, but it is the same kind of signal: it reads nothing but its name, it acts only on a registered bus that is away from its dock and has not pushed an ERROR, and a class that never sends it is unaffected.

### 2. The *Job started* event entity (`event.py`)

Decided by Rickard on 2026-10-13, after review round 1: an event entity, not a sensor. Rickard first chose a *Started by* enum sensor (2026-10-12); the review showed it cannot carry the use case, because Home Assistant writes no `state_changed` when a sensor writes the value it already has, so after the first scheduled start a trigger on it never fires again, and a condition on it read when `lawn_mower` turns *mowing* sees the previous job's word on the O1200, whose CLEANING push lands 13 s before the start edge. An event entity fires once per edge, whatever the previous one was.

A new entity class, `EcovacsJobStartedEventEntity`, in `event.py` next to `EcovacsLastJobEventEntity`. It is a class rather than an `ENTITY_DESCRIPTIONS` entry for the same reason as its sibling: `event.py` has no description tuple, and the entity subscribes to `MowerJobEdgeEvent`, which is not a library capability, so there is no `capability_fn` to describe. It is added in `async_setup_entry` for every device whose `device.capabilities.device_type is DeviceType.MOWER` (the gate `EcovacsActivitySensor` uses), with `device.capabilities.state` as the capability passed to `EcovacsEntity`. It subscribes to `MowerJobEdgeEvent` and, on `phase in ("start", "resume")`, calls `_trigger_event(event.trigger, {"phase": event.phase, "job_type": event.job_type})` and `async_write_ha_state()`.

- **Description:** `EventEntityDescription(key="job_started", translation_key="job_started", event_types=["schedule", "app", "continue", "reborn"])`, no `entity_category` (it is meant for automations).
- **Event types:** the trigger words observed on a start or resume, verbatim. Translations in `strings.json` and `translations/en.json` (kept identical): the entity name *Job started* under `entity.event.job_started.name`, and one state name per type under `entity.event.job_started.state_attributes.event_type.state`, proposed *Schedule*, *App*, *Continued*, *Restarted*. One `default` icon for the entity in `icons.json` (`mdi:play-circle-outline`), the same shape as `last_job`.
- **Event data:** `phase` (`"start"` or `"resume"`), so an automation can tell a new job from one continuing, and `job_type` (`schedule`, `spotarea`, `border`, `auto`), so it can tell the schedule's job from any other whatever the trigger word.
- **Unknown word:** logged once at debug level and dropped, the same pattern as `EcovacsLastJobEventEntity._logged_triggers`. `EventEntity._trigger_event` raises on a type outside `event_types`.
- **Replay guard:** the edge already fired is held by identity (`self._reported_start`), exactly as `_reported_ending` does in `EcovacsLastJobEventEntity` and for the same reason: `EventBus.subscribe` replays the last event to a new subscriber, and an entity-ID change re-adds the entity. `MowerJobEdgeEvent._seq` makes two genuine, equal-looking edges distinct objects.
- **Pause and stop:** ignored; the stop already feeds *Last mowing job*.
- **A start from Home Assistant:** its trigger word is unobserved (`mow-auto-start` is inferred, not captured), so it may fire no event, only a debug line, until the word is seen and added.

What an event means: the mower announced that something started or resumed it. It is not always a new job: `reborn` arrives repeatedly within one run on the O800 RTK (the reason `sensor.py`'s `_NEW_JOB_TRIGGERS` excludes it), and `continue` is a resume after a charge break. The event fires at the announcement, before the departure is confirmed (§1): on the G1-800 it precedes the CLEANING by about 3 s, on the O1200 it follows the push by 13 s. An automation that must act only once the mower is out waits in its actions for `lawn_mower` to read *mowing*; a condition is checked when the event fires, which on the G1-800 is before the mower reads *mowing*, so it would always be false there.

Why the words stay raw: the G1-800's `mow-schedule-resume` says `triggerType: schedule`, while the O1200's says `trigger: continue` (Geten's log of 2026-10-12, and issue #73). A schedule continuing after a charge break and a schedule window opening are not told apart by one word across firmware, so translating both to "scheduled" would be a guess. The entity reports what the mower said, and `job_type` answers "is this the schedule's job"; the README shows the #120 weather check as an automation triggered on the entity with the condition `job_type == "schedule"`.

## Testing

All new behaviour is in the patch layer or in `event.py`. The patch-layer tests run locally (`tests/deebot_patch/`, `-p no:homeassistant`); the entity and controller tests are `requires_ha` and CI is the source of truth for them.

The replay tests use the bodies listed under Fixtures below.

- **`tests/deebot_patch/test_messages.py`**
  - The #120 sequence replayed through the real handlers on a registered bus that starts docked: nothing at the 13:00 resume, CLEANING at the `onChargeState isCharging: 0` 3 s later, RETURNING at `return-normal-start`, DOCKED at `onChargeState isCharging: 1`, the same again from the 14:00 resume, RETURNING after the stop.
  - The O1200 charge-break order: `onChargeState isCharging: 0` first, the resume edge 8 s later, CLEANING at the edge.
  - Neither order pairs across more than 60 s; an `isCharging: 1` between an unplug and a later edge cancels the unplug; a reported state between the two facts cancels both, so a later fact does not re-assert CLEANING over it: a PAUSED that `handle_clean_info` publishes (mower not docked), an `onChargeInfo` `goCharging`, an `onChargeInfo` with `trigger: alert` (the early ERROR branch), and an `OnReturnNormalStart` on an undocked bus.
  - A PAUSED that `handle_clean_info` **withholds** because the mower is on its charger does not cancel: edge, withheld paused-plan push, unplug still yields CLEANING.
  - A `pause` or `stop` edge between a start or resume edge and the unplug cancels the departure: the unplug then notifies no CLEANING. The same holds for a pause edge with neither `trigger` nor `triggerType`.
  - An `onChargeInfo` `idle` (DOCKED) between the edge and the unplug does not stop the pair, like a repeat `isCharging: 1`.
  - A `return-normal-start` after a pushed ERROR on an undocked bus notifies nothing and the state stays ERROR.
  - `test_the_return_trip_is_not_registered` is renamed `test_the_end_of_the_return_trip_is_not_registered`, its docstring saying that `return-normal-start` is now registered and `return-normal-stop` is not.
  - A `getChargeState` answer with `isCharging: 1` between the edge and the unplug push does not stop the pair (edge-first, as at 13:00:23 → 13:00:26).
  - An `OnPos` away-from-dock sample between the two facts does not stop the pair.
  - A `getChargeState` answer with `isCharging: 0` does **not** complete a pair.
  - An edge on a bus with no `StateEvent` yet and `record.docked` False counts; so does one whose last `StateEvent` is DOCKED after `OnPos` cleared `record.docked`; an edge on a bus whose last `StateEvent` is PAUSED, CLEANING or RETURNING and that is not docked does not.
  - An `onChargeState` with a fail-shaped body, or with `isCharging: 1`, stamps nothing.
  - A start edge with neither `trigger` nor `triggerType` still counts.
  - A start or resume, or a `return-normal-start`, on an unregistered bus (a vacuum on the same account) notifies no `StateEvent`; `return-normal-start` returns success there.
  - Adverse orders: a resume edge after a pushed PAUSED (bus not docked) does not count and the state stays PAUSED; a `return-normal-start` after docking notifies nothing and leaves `record.docked` and `record.suppressed` as they were.
  - `pause` and `stop` notify no `StateEvent`; `return-normal-start` notifies no `MowerJobEdgeEvent`.
  - Every edge carries its class's `job_type`.
  - The O1200 sequence from 2026-10-12: `onScheduleTaskInfo` CLEANING, then `mow-schedule-start`, then `onChargeState isCharging: 0`, produces exactly one CLEANING notification to subscribers.
  - Registration: the five new names are registered by `apply()`, next to `test_the_schedule_and_spotarea_job_edge_names_are_registered`.
- **`tests/deebot_patch/test_state_precedence.py`:** `note_departure_edge()` and `note_charge_left()` in both orders, at and past the window; `cancel_departure()` clearing both stamps; `dock()` clearing only `charge_left_at`; `move()` clearing neither.
- **`tests/test_controller.py`:** a paired departure on a docked mower starts the poll, and the DOCKED at the next charge stops it.
- **`tests/test_event.py`:** *Job started* fires the trigger word with `phase` and `job_type` on a start and on a resume, fires nothing on pause and stop or on an unknown word (logged once), does not re-fire on a subscribe replay of the same edge object, does fire for a second equal-looking edge, and exists only on a MOWER device. *Last mowing job* fires nothing on a `pause` or `resume` edge. `test_no_stale_event_translations_or_icons` widens from `last_job` to both keys.
- **`tests/test_sensor.py`:** the progress sensor ignores `pause` and `resume` edges.
- **`tests/test_translations.py` and the platform tests:** the new translation keys and icon belong to a real entity, both directions.

## Documentation

- `CLAUDE.md`: the `messages.py` bullet does not mention the job edges today, so it gains a sentence: the task bury points (`mow-*-start/resume/pause/stop`) publish `MowerJobEdgeEvent` with their job type, a start or resume edge paired with an `onChargeState isCharging: 0` push within 60 s, in either order, writes CLEANING, and `return-normal-start` writes RETURNING. The `state_precedence.py` bullet gains the two departure stamps. The "Entity platforms" paragraph stops listing `event` among the declarative platforms and names its two classes, *Last mowing job* and *Job started*, as the exception: both subscribe to `MowerJobEdgeEvent`, which no `capability_fn` can describe.
- `controller.py`, `on_status`: the comment "leaving is the one transition the mower has not been seen to drop" is refuted by #120; it says instead that a departure the mower does not push is written from the bury points (`messages.py`).
- `messages.py` module docstring: the opening paragraph ("Without the latter two the entity never leaves 'docked' during a scheduled run") gains the task bury points as a third way out of DOCKED, and the list of task bury points ("`{start,stop}`", "eight more messages") grows by the four edges and `return-normal-start`.
- `state_precedence.py` module docstring and `MowerStateRecord` docstring: the record now also holds the two departure stamps, what each method does to them, and why only the state-reporting paths cancel them.
- `_OnMowJobEdge` docstring: the new `JOB_TYPE`, and that a start or resume edge may write CLEANING through the pair while a pause or stop cancels a pending one.
- `MowerJobEdgeEvent` docstring: "Only the two edges this integration acts on are registered", the `phase` description, the new `job_type` field, and the trigger vocabulary, which gains `continue` and `schedule` on a resume and `lowBattery` on a pause (already cited by `tests/test_sensor.py`).
- `tests/deebot_patch/test_contract.py`, the comment near line 318 ("each of the four bury-point handlers"): it counts the handlers that exist after this change.
- `OnChargeState` docstring: the push now also pairs with a start or resume edge. `GetChargeStateMower` docstring: its full-battery sentence gains that the two O1200 logs of 2026-10-12 show a mower at battery 100 reporting itself charging until it leaves.
- `event.py` module docstring: the entity gains a sibling, *Job started*.
- README, the platform table: the `event` row's count goes from 1 to 2 and lists *Job started*, plus a short section on its event types and event data, the firmware difference in trigger words, and the weather-check automation from #120 as the example.

## Fixtures

The `body` of each message the replay tests use, copied verbatim (the `header` carries nothing the handlers read). Sources: the reporter's log on #120 (`ecovacs_scheduled_run.log`, https://github.com/user-attachments/files/33343775/ecovacs_scheduled_run.log; G1-800 `77atlz`, firmware 1.36.208, device ids anonymized by the reporter), and the maintainer's own O1200 `2i0fns`, firmware 1.13.10, logged from Home Assistant on 2026-10-12. All arrive on `iot/atr/<name>/...`.

G1-800, #120:

```
13:00:23.762  onFwBuryPoint-bd_task-mow-schedule-resume
  {"bid":"7101791802821723","gid":"G1791648417330","index":"0000000435","mapid":"0809159361","pauseId":"9211791733414102","resumeId":"2261791802821723","scheduleId":"1791725416643502","sid":"6211791802821723","triggerType":"schedule","ts":"1791802821723"}
13:00:26.501  onChargeState
  {"data":{"isCharging":0,"mode":"slot"}}
13:30:23.452  onFwBuryPoint-bd_task-mow-schedule-pause
  {"bid":"9831791804621723","cuttedArea":199.292500,"gid":"G1791648417330","index":"0000000454","mapid":"0809159361","pauseId":"4491791804621723","resumeId":"2261791802821723","scheduleId":"1791725416643502","sid":"4301791804621723","time":0,"triggerType":"schedule","ts":"1791804621723","workArea":280.177500}
13:30:23.801  onFwBuryPoint-bd_task-return-normal-start
  {"bid":"0031791804622040","gid":"G1791648417330","index":"0000000457","mapid":"0809159361","returnId":"1791804622039907","sid":"2531791804622040","triggerType":"schedule","ts":"1791804622040","workstate":1}
13:31:35.850  onChargeState
  {"data":{"isCharging":1,"mode":"slot"}}
14:00:23.824  onFwBuryPoint-bd_task-mow-schedule-resume
  {"bid":"7931791806421723","gid":"G1791648417330","index":"0000000491","mapid":"0809159361","pauseId":"4491791804621723","resumeId":"1241791806421724","scheduleId":"1791725416643502","sid":"0781791806421724","triggerType":"schedule","ts":"1791806421724"}
14:00:27.022  onChargeState
  {"data":{"isCharging":0,"mode":"slot"}}
15:01:59.695  onFwBuryPoint-bd_task-mow-schedule-stop
  {"bid":"0341791810117742","cuttedArea":280.202525,"gid":"G1791648417330","index":"0000000521","mapid":"0809159361","pauseId":"4491791804621723","resumeId":"1241791806421724","scheduleId":"1791725416643502","sid":"4061791810117742","time":194.183334,"triggerType":"workcomplete","ts":"1791810117742","workArea":280.202525}
15:01:59.802  onFwBuryPoint-bd_task-return-normal-start
  {"bid":"4931791810117951","gid":"G1791648417330","index":"0000000526","mapid":"0809159361","mowId":"","returnId":"1791810117949218","sid":"9401791810117951","triggerType":"schedule","ts":"1791810117951","workstate":1}
```

O1200, charge-break resume (unplug first):

```
11:47:35.375  onScheduleTaskInfo
  {"data":{"trigger":"continue","other":"","rotationPause":0,"ssid":"1","state":"clean","cleanState":{"motionState":"working","content":{"type":"auto","subContent":{"type":"back"}}}}}
11:47:42.141  onChargeState
  {"data":{"isCharging":0,"mode":"slot"}}
11:47:50.636  onFwBuryPoint-bd_task-mow-schedule-resume
  {"bid":"7081791798470484","gid":"G1790503263492","index":"0000133051","jobId":"5251791788453307","mapId":"511702305","sid":"4161791798470484","trigger":"continue","ts":"1791798470485","workType":18}
```

The charge-break order test without the push uses the last two bodies alone; the "exactly one CLEANING" test uses all three.

O1200, scheduled start (push, edge, unplug):

```
09:00:53.527  onScheduleTaskInfo
  {"data":{"trigger":"schedule","other":"loading","rotationPause":0,"ssid":"0","state":"clean","cleanState":{"motionState":"working","content":{"type":"auto","subContent":{"type":"idle"}}}}}
09:01:06.627  onFwBuryPoint-bd_task-mow-schedule-start
  {"bid":"7301791788466359","gid":"G1790503263492","index":"0000126501","jobId":"5251791788453307","mapId":"511702305","sid":"8781791788466360","trigger":"schedule","ts":"1791788466360","workType":18}
09:01:13.221  onChargeState
  {"data":{"isCharging":0,"mode":"slot"}}
```
