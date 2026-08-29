# Ecovacs Mower for Home Assistant

A Home Assistant custom integration for Ecovacs GOAT robot mowers, built to
work around three defects in the upstream `ecovacs` integration that leave
GOAT mowers effectively unusable.

If you own a GOAT mower and its state in Home Assistant is stuck, and
start/pause do nothing, this is likely why:
[home-assistant/core#168621](https://github.com/home-assistant/core/issues/168621).

## What's broken, and what this fixes

Home Assistant's built-in `ecovacs` integration talks to Deebot vacuums and
GOAT mowers through the same library, `deebot-client`. For GOAT mowers,
five independent bugs in that path add up to a mower that can't be
controlled and barely reports its own state:

| Defect | Effect |
|---|---|
| Control commands are sent to the MQTT topic `iot/p2p/clean_V2` | GOAT firmware listens on `iot/p2p/clean` and ignores `clean_V2` entirely. `start_mowing` and `pause` do nothing. |
| State refresh uses the command `getCleanInfo_V2` | GOAT mowers never answer it. Polled state refreshes silently fail. |
| Plain `getCleanInfo` is answered — with `idle`, always | Every state refresh replaces the real state with `idle`, which the library reads as "standing still". A mower that is out cutting reports `mowing` for a minute or two after it starts and `paused` for the hours that follow. |
| The unsolicited messages `onChargeInfo` and `onScheduleTaskInfo` have no handler | They're dropped as unknown messages. In practice this means the mower's state in Home Assistant only updates once a day, when it happens to reconnect and get polled. |
| Every login goes through the password endpoint, which answers `1013` for some accounts even after the device was verified | The config entry loops on "Device verification required": the emailed code is accepted, the reload logs in with the password, is refused, and another code is requested. The integration never finishes setting up. |

Net effect on the upstream integration: state lags by close to a day, and
the controls that are supposed to fix that don't work either.

This integration patches all five at the protocol layer (correct command, a
refresh call that is both answered and believed, handlers for both missing
messages, and a session renewal that does not touch the password endpoint) and
exposes a `lawn_mower` entity that reflects real state within seconds and
responds to start / pause / dock.

## Why a separate integration, instead of a fix upstream

The underlying library, `deebot-client`, currently has eight open pull
requests touching GOAT/mower support, the oldest opened in April. None have
merged. In the same period, vacuum and authentication changes to the same
library have merged within days. Two Home Assistant core pull requests
adding mower features were auto-closed as stale by the triage bot while
waiting on those library PRs to land.

This is not a criticism of the maintainers — they're volunteers, and review
bandwidth is finite. It's the reason a fork is the pragmatic way to get a
working mower today rather than waiting on a queue with no visible movement.

Relevant links, so you can check the state of things yourself:

- [DeebotUniverse/client.py#1624](https://github.com/DeebotUniverse/client.py/pull/1624) — fixes the `clean_V2` command (open)
- [DeebotUniverse/client.py#1647](https://github.com/DeebotUniverse/client.py/pull/1647) — adds the two missing message handlers (open, community-approved, no maintainer response)
- [DeebotUniverse/client.py#1650](https://github.com/DeebotUniverse/client.py/issues/1650) — `getCleanInfo_V2` not answered by GOAT hardware
- [DeebotUniverse/client.py#1587](https://github.com/DeebotUniverse/client.py/pull/1587) — RTK support
- [home-assistant/core#168621](https://github.com/home-assistant/core/issues/168621) — the user-facing symptom report this integration exists to fix
- [home-assistant/core#169723](https://github.com/home-assistant/core/issues/169723) — mowers exposed with vacuum terminology
- [DeebotUniverse/client.py#1743](https://github.com/DeebotUniverse/client.py/pull/1743) — password-free session renewal, the fix for the verification loop (open)
- [home-assistant/core#178558](https://github.com/home-assistant/core/pull/178558) — the core side of that fix, blocked on the library release (open)

This integration does not depend on any of those merging. If they do,
the corresponding patch in this repo becomes dead code and gets deleted.

## Requirements

- **Home Assistant 2026.7 or later.** This is a hard floor, not a
  suggestion. `deebot-client==18.5.1` (what this integration pins) requires
  `cryptography>=48.0.1` for its device-verification flow. Home Assistant
  2026.4.4 pins `cryptography==46.0.7`. Those two requirements cannot
  coexist, so the integration cannot load at all on HA 2026.4.4 or older —
  it will fail to install, not fail at runtime. If you're on an older
  release, upgrade Home Assistant first.
- HACS, if installing that way (see below). Not required for manual install.

## Hardware support

Device classes the integration patches. "Confirmed" means someone has run
the mower on it and reported that controls, state and position work — not
merely that the class string was seen:

| Model | Device class | Confirmed by |
| --- | --- | --- |
| **Ecovacs GOAT O1200 LiDAR Pro** | `2i0fns` | the author's own hardware |
| **Ecovacs GOAT O800 RTK** | `9bts2s` | a user, firmware 1.13.8 ([#8](https://github.com/nord-/ha-ecovacs-mower/issues/8)) |
| **Ecovacs GOAT O800 RTK** | `2px96q` | a user, controls and state confirmed — start/pause in [#24](https://github.com/nord-/ha-ecovacs-mower/issues/24), state on firmware 1.17.11 in [#56](https://github.com/nord-/ha-ecovacs-mower/issues/56). Firmware 1.17 speaks a second map dialect, decoded from two users' logs but not yet confirmed on hardware ([#41](https://github.com/nord-/ha-ecovacs-mower/issues/41)) |
| **Ecovacs GOAT G1-800** | `77atlz` | patched, controls **not** confirmed — the protection-flag sensors work on firmware 1.36.208 ([#30](https://github.com/nord-/ha-ecovacs-mower/issues/30)); that firmware branch answers the `V2` command family instead of the one every other confirmed mower uses, and the integration now detects and switches to it automatically, so no manual configuration is needed ([#42](https://github.com/nord-/ha-ecovacs-mower/issues/42)) |
| **Ecovacs GOAT A1600 LiDAR Pro** | `e4gqia` | a user, firmware 1.11.31 ([#29](https://github.com/nord-/ha-ecovacs-mower/pull/29)) |
| **Ecovacs GOAT A1600 RTK** | `xmp9ds` | reported, patch not yet confirmed — firmware 1.17.9 ([#43](https://github.com/nord-/ha-ecovacs-mower/issues/43)) |

The A1600 ships as two machines, and they report different device classes:
the LiDAR Pro is `e4gqia`, the RTK is `xmp9ds`. Both rows above are real, and
neither is a second class string for the other.

The rest of the GOAT line shares the same three upstream defects described
above, and would very likely work with the same fix. Those models are not in
the list, because nobody has reported one.

If you install this on an unsupported model, the integration will still
load, but it will not patch that device's commands — meaning you'd be back
to the original symptoms (dead controls, stale state). It logs a warning
naming the device class it saw and didn't recognize, for example:

```
Mower class <class> is not supported by this integration and is used
unpatched: controls will likely not work and state will lag. Report the
model at <issue tracker> so it can be added
```

That device class string is exactly what to paste into a
[new issue](https://github.com/nord-/ha-ecovacs-mower/issues). Adding a
reported model to `SUPPORTED_CLASSES` is a small, low-risk change — this is
one of the more useful ways to contribute without touching Python. Saying
afterwards whether it actually worked is what turns the row into a confirmed
one.

## Installation

Remove Home Assistant's built-in `ecovacs` integration for this mower
first. Having both installed at once means two integrations racing to
control the same device.

### Via HACS (custom repository)

This integration is not in the HACS default store. Add it as a custom
repository:

1. HACS → the three-dot menu → **Custom repositories**
2. Repository: `nord-/ha-ecovacs-mower`, category: **Integration**
3. Install "Ecovacs Mower" from HACS
4. Restart Home Assistant
5. Settings → Devices & services → **Add integration** → search for
   "Ecovacs Mower"

### Manual

1. Copy `custom_components/ecovacs_mower` from this repository into your
   Home Assistant `config/custom_components/` directory
2. Restart Home Assistant
3. Settings → Devices & services → **Add integration** → search for
   "Ecovacs Mower"

### First-time setup: expect a verification code

Since July 2026, Ecovacs requires device verification for new client IDs
logging into an account. This integration registers as a new client,
separate from the built-in `ecovacs` integration, so the very first setup
will trigger it even if your account was already verified before.

During setup you may see error code `1013` ("Please update to the latest
version to continue") — this is that verification requirement, not a bug.
The config flow will prompt for a code emailed to your Ecovacs account.
Enter it and setup continues.

**You should only ever need one code.** For some accounts Ecovacs keeps
answering `1013` to the password login even after the device has been verified,
which in the built-in integration produces an endless loop: the code is
accepted, the entry reloads, the password login is refused again and another
code is requested ([home-assistant/core#177870][loop]). This integration stores
the account token the verification returns and renews its session with that
instead of the password, so a reload does not need a new code. If you are
already stuck in that loop on an older version of this integration, updating is
enough: Home Assistant will ask you to reauthenticate once, that one code gets
stored, and the entry stops asking. There is no need to delete and re-add it.

[loop]: https://github.com/home-assistant/core/issues/177870

## What you get

Forty-two entities on the mower's device page, across eight platforms —
plus one per UWB beacon on the models that use them:

| Platform | Count | What |
|---|---|---|
| `lawn_mower` | 1 | Real state (`mowing`, `paused`, `returning`, `docked`, `error`) that updates within seconds, plus working `start_mowing`, `pause`, and `dock` |
| `sensor` | 16 + one per beacon | Activity (the mower's state with the reason folded in — `returning_rain`, `docked_rain_delay`; see below), battery, error code (disabled by default — see below), mowing progress (see below), mowed area, mowing time, three lifetime totals (area, time, session count), four consumable-lifespan percentages (blade, lens brush, trimmer brush, weed rope), IP address, Wi-Fi signal strength, Wi-Fi network name, and on a beacon-guided mower one battery percentage per UWB beacon (see below) |
| `binary_sensor` | 6 | Fault — a latched problem that stays on until the mower recovers or you clear it (see below) — plus rain sensor, rain delay, emergency stop, locked, animal protection: the mower's raw protection flags, from the `onProtectState` message the library drops (see below) |
| `switch` | 8 | Advanced mode, TrueDetect obstacle avoidance, edge cutting, child lock, lift warning, boundary crossing warning, safety protection, rain detection (see below) |
| `number` | 3 | Notification volume, cutting direction, rain delay duration (see below) |
| `button` | 6 | Reset each of the four consumable lifespans, "Locate mower" (plays a sound on the device), and "Clear fault" (releases the latched fault; see below) |
| `event` | 1 | Last mowing job (finished / finished with warnings / manually stopped) |
| `image` | 1 | The mower's map — lawn boundary, mowed coverage, no-go zones, detected obstacles, the dock and the mower's live position track. Add it to a dashboard with a `picture-entity` card. Decoded from the GOAT's own map messages (`onMI`/`onArI`/`onMapTrack`/`onSpecialContour`, and `onMapTrace` on firmware 1.17); the format is documented in `docs/superpowers/specs/2026-08-10-mower-map-design.md`. Geometry survives restarts; the position track is live-only |

Not included yet: **RTK diagnostics** (position and satellite data) and
zone control. RTK is planned for the next release; the other has no
committed date.

### When a run stops because of rain

A scheduled run cut short by rain is the case where the mower's own state is
not enough. The `lawn_mower` entity can only report what Home Assistant's
`LawnMowerActivity` allows — `mowing`, `paused`, `returning`, `docked`,
`error` — so it goes `mowing → paused → returning → docked` in about a
minute and then looks exactly like a run that finished normally.

The device does say why, in a `trigger` field on the state messages:

```
onScheduleTaskInfo   trigger:"rain"  motionState:"pause"
onChargeInfo         trigger:"rain"  state:"goCharging"
onChargeInfo         trigger:"workComplete"  state:"idle"     <- 56 s later
```

`sensor.<device>_activity` is built on that field. It reports the same state as
the `lawn_mower` entity with the reason folded in: `paused_rain`,
`returning_rain` and `docked_rain_delay` alongside the plain `mowing`,
`paused`, `returning`, `docked` and `error`. Put that on a dashboard if you
want to see *why* it is parked.

Note the third line above: the mower reports `workComplete` when it reaches the
dock even though rain is what sent it there. The rain reason is therefore held
until the mower actually cuts grass again — otherwise it would be discarded at
the exact moment you want to read it.

The five `binary_sensor` entities are the raw flags from `onProtectState`, one
per flag, with no interpretation on top.

`rain_protect` (`isRainProtect`) is the exception: it is presented as a live
rain reading, named **Rain sensor** and given the `moisture` device class, so it
reads Wet/Dry. Two samples rule out the reading one could not, with the mower's
rain protection switched on in both:

| sample | setting | `isRainProtect` |
| --- | --- | --- |
| two seconds before a rain-stopped run | `RainDetect: 1` *(settings message)* | `1` |
| dry day, mower parked under cover | on in the app | `0` |

A flag that moves while the setting stands still is not the setting. The second
sample — a `getProtectState` answer from firmware 1.13.10 — also had
`isAnimProtect: 0` with animal protection switched on, which rules the
"protection is enabled" reading out a second time — on a sibling flag rather
than this one, so it leans on the five flags being the same kind of thing.

That second witness cuts both ways: it means `animal_protect` is not the
animal-protection setting either, so the entity still named **Animal
protection** is named for something it is not. It is left alone because no
positive reading is established — one sample cannot separate "an animal is
detected" from "the mower is holding for an animal" — and a name that is
merely imprecise beats one that is confidently wrong (issue #45).

What the samples do *not* separate is a wet sensor from a mower currently held
for rain: both read `1` two seconds before a rain-stopped run and `0` on a dry
day under cover. `moisture` fits either way, and telling them apart needs the
same rain event `rain_delay` needs, below.

`rain_delay` is deliberately left alone: no device class, no rename. The
working theory is that it covers the configured post-rain hold (three hours on
the verified hardware), which would explain why the device reports it
separately from the sensor — `isRainProtect` drops back to `0` as soon as the
sensor dries off in the dock, while the mower is still waiting. That is
**unconfirmed**. Confirming it takes a rain event:

```
grep onProtectState home-assistant.log | tail -5
```

`isRainDelay` should go to `1` when the run breaks, stay `1` after
`isRainProtect` has fallen back to `0`, and clear after the configured delay
rather than when the grass dries. "The configured delay" now has an entity of
its own to check against — see the next section — but reading the setting is
not the same as watching the flag follow it, so the theory stands unconfirmed.

The mower's own rain-aware *states* still come from `trigger`, which needs no
interpretation, not from these flags.

Note that the display name feeds the entity id at first registration only:
installs that predate the **Rain sensor** rename keep
`binary_sensor.<device>_rain_protection`, while fresh ones get
`binary_sensor.<device>_rain_sensor`. Nothing breaks, but an automation snippet
naming this entity is install-dependent.

All five `binary_sensor` flags are asked for with `getProtectState` when Home
Assistant starts, and updated from the `onProtectState` push after that. The
device only pushes when a flag flips, so before that command was wired up
(issue #31) the flags stayed `unknown` through any dry, uneventful spell — and
from every restart.

The rain *reason* on `sensor.activity` is not restored by a restart, though — it
comes from `trigger`, which nothing can ask for, so a restart during an active
rain delay reads as plain `docked` until the mower's next scheduled run.

### The rain sensor's setting, and the four entities named for rain

`switch.<device>_rain_detection` and `number.<device>_rain_delay_duration` are
the setting itself: whether the mower listens to its rain sensor at all, and
how many minutes it waits before resuming once a run has been cut short. Both
come from `onRainDelay`, a message the library has no handler for, which
carries the pair in one payload:

```
{"enable": 1, "delay": 180}
```

Nothing on the wire says what `delay` counts in. Minutes is read off the
reporter's app, which showed three hours against that 180 (issue #54). The
entity's ceiling of one day is a generous guess, not a firmware limit; the
range mirrors [Janverhu/ecovacs-goat-g1][goat-g1], which drives the same
command against a GOAT G1.

Both are disabled by default, like every other settings entity here.

That makes four entities with rain in the name, and they are four different
things:

| Entity | Kind | Answers |
| --- | --- | --- |
| `switch` **Rain detection** | setting | Is the rain sensor switched on? |
| `number` **Rain delay duration** | setting | How long a hold does it trigger? |
| `binary_sensor` **Rain sensor** | reading | Is the sensor wet right now? |
| `binary_sensor` **Rain delay** | reading | Is the mower holding right now? |

The two settings are read at startup with `getRainDelay` and updated from the
`onRainDelay` push after that — the same arrangement as the protection flags,
and for the same reason: the device pushes only when the setting changes, so
without the command they would read `unknown` until someone opened the app.

Writing either one sends `setRainDelay`, which carries **both** fields. Each
entity therefore resends the half it does not own, unchanged, from the last
value the mower reported. If that half has never arrived, the write is refused
rather than guessed: a default would silently overwrite a hold the owner chose,
or switch their rain sensor off.

[goat-g1]: https://github.com/Janverhu/ecovacs-goat-g1

### How far along the mower is

`sensor.<device>_mowing_progress` reports the percentage of the running job that
is cut — the same figure the Ecovacs app shows next to the zone size.

The device answers `getStats` with three numbers, of which `deebot-client` keeps
two. The third, `mowedArea`, is the only one that moves during a run: `area` is
the *target* area of the current job and holds still while `mowedArea` climbs
towards it. The sensor is the ratio of the two, so the unit cancels — which
matters for edge cutting, where the target is the strip along the boundary rather
than the lawn.

Two things worth knowing:

- **Between jobs it holds the last job's figure**, and it is unknown rather than
  0 before the first one. Most firmware reports zeros when nothing is running,
  and a 0 there would be indistinguishable from a job that has just started;
  some firmware never zeroes at all and keeps reporting the finished job's
  numbers, which is why the entity refuses to read the payload at all while the
  mower is parked. What clears the reading is the mower announcing that a job
  ended, acted on when the next one begins — not the mower parking, since a run
  that docks to charge and resumes is one job and keeping its figure through the
  break is the point. On a mower that never sends those announcements the figure
  simply stands until real numbers replace it, so the first minutes of a job can
  still show the previous one's.
- **"Finished" is not this entity's job.** A completed run publishes its final
  figure here, but that figure is not always 100: for a zone the target is the
  polygon's estimate, and a mower that considers itself done after 24 of 32 m²
  reports 76 %. There is no dedicated "job is done" signal to automate on yet —
  `lawn_mower.<device>` going from `mowing` to `paused` also covers a charge
  break and a manual or rain pause (see below), so it cannot tell a finished
  job from either of those.
- **It follows the push where there is one, and a poll where there is not.**
  Some mowers send `onStats` several times a second while cutting; others have
  been observed not to. Where it arrives, the reading tracks the mower, and a
  whole percent has to change before the entity writes a new state. Where it
  does not, a poll fills in: one request at startup to sync, then every five
  minutes while a run is in progress, stopping when the mower parks. A run
  interrupted by charging needs no special case — the mower docks, the poll
  stops, and it starts again when the job resumes. The poll is also why the
  final figure comes from elsewhere: its five-minute cadence rarely lands on the
  last percent of a run, so the reading is completed from the job-finished
  message the mower pushes at the same moment.

`paused` is deliberately not a reason to stop asking: it is a normal mid-run
state (rain, a manual pause), not a sign the job has ended, and the poll keeps
going regardless of which non-docked state the mower is currently in.

The same poll refreshes the job-target-area and job-target-duration sensors,
which the device otherwise leaves untouched between restarts. Named after what
they actually hold — GOAT's `getStats` reports the running job's target, not
what has been cut or elapsed so far; only `mowedArea` climbs during a run.

### The UWB beacons

On a beacon-guided GOAT, each beacon gets its own sensor:

```
sensor.<device>_beacon_<serial>    83 %
```

The serial is the code the Ecovacs app prints next to each beacon on its own
maintenance page, so the two agree on which one is which. They are batteries as
far as Home Assistant is concerned — `device_class: battery` — which means the
built-in low-battery handling applies without anyone writing a template.

Issue #40 proposed `sensor.<device>_beacon_<sn>_battery` with the serial as an
attribute; this drops the `_battery` suffix since `device_class: battery`
already says what the reading is, and the serial moved into the entity id and
name since it is already the unique identifier for the sensor.

They appear once the mower has answered `getLifeSpan` for the first time, not
at setup: the payload is what says how many beacons there are and what they are
called, and nothing else does. A beacon that stops being reported keeps its
entity and reads unknown rather than holding the dead cell's last charge;
deleting it is a manual step in the entity registry, deliberately, because a
poll that failed is not proof that a beacon is gone.

`deebot-client` drops these. Its `LifeSpan` enum has no member for the
`uwbCell` component, and it raises on one rather than skipping it — which took
the rest of the answer with it, since the parser publishes as it goes. On a
G1-800 the components arrive in the order blade, beacons, lens brush, so the
blade percentage worked, the beacons were invisible, **and the lens brush
reported a value from before the beacons were paired that could never change.**
That last one is fixed here too, as a side effect of not giving up on the
answer.

Two things this does not do yet:

- **There is no per-beacon reset button.** The existing lifespan resets each
  target a single component; targeting one beacon needs its serial, and the
  wire format for that has not been captured. Reset the cell in the Ecovacs app
  for now.
- **It rides the poll.** No mower has been observed pushing `onLifeSpan`, so
  the readings refresh on the same schedule as everything else that needs a
  round trip — and on the firmware where those round trips fail intermittently,
  they go stale for as long as the failures last.

### Entities disabled by default

**17 of the 40 entities** ship with `entity_registry_enabled_default=False`,
all inherited unchanged from upstream Home Assistant core's `ecovacs`
integration — they're advanced settings or diagnostics, off by default
there too. They appear in the mower's entity list right after setup, but
stay disabled and report no state until you turn them on by hand (device
page → the entity → the cog icon → enable). That's expected behaviour, not
a bug: if one of these looks blank or "unavailable," this is why.

| Platform | Disabled | Which |
|---|---|---|
| `switch` | 7 of 7 | all of them: advanced mode, TrueDetect, edge cutting, child lock, lift warning, boundary crossing warning, safety protection |
| `number` | 2 of 2 | both: volume, cutting direction |
| `sensor` | 4 of the 16 fixed ones | IP address, Wi-Fi signal strength, Wi-Fi network name, and **error code**. The per-beacon sensors are enabled |
| `button` | 4 of 6 | the four consumable-lifespan resets (blade, lens brush, trimmer brush, weed rope) — "Locate mower" and "Clear fault" are enabled by default |

**If you're planning anything on the error sensor** — an alarm, a
notification, a dashboard card — note that it does not exist as an
enabled entity out of the box. `sensor.<device>_error` is created disabled
and reports nothing until you enable it by hand, same as the other 16
above. This is the one entity in this list someone is likely to go
looking for by name, so it's worth repeating here rather than only in the
table.

For an alarm, though, **`binary_sensor.<device>_fault` is the one you
want** — it is enabled by default and, unlike the error sensor, it does
not go back to "fine" on its own. See the next section.

### A fault that stays until something actually clears it

`sensor.<device>_error` reports the last code the mower sent. On firmware
1.36.208 that turned out to be useless for alarming
([#53](https://github.com/nord-/ha-ecovacs-mower/issues/53)): a blade-disc
jam pushed `code:[406]` exactly once and was followed **89 milliseconds
later** by `code:[0]` — and then by another 3076 zeros over the 38 minutes
the mower sat stuck on the lawn draining its battery. The error sensor read
`0` the whole time, and `lawn_mower` read `paused`, which is
indistinguishable from a pause by hand. Nothing an automation could fire on
existed for longer than a tenth of a second.

The zeros are not the mower saying it recovered — they keep arriving at
about 1.4 per second whatever the machine is doing. So
`binary_sensor.<device>_fault` ignores them entirely. It goes `on` for any
non-zero code and goes `off` only when something says the fault is really
over:

- the mower **docks**, or
- the mower **starts mowing** again, or
- you press **Clear fault**.

The code and its text ride along as attributes, so a notification can name
the fault without reading the error sensor:

```yaml
automation:
  - alias: Mower is stuck
    triggers:
      - trigger: state
        entity_id: binary_sensor.goat_fault
        to: "on"
    actions:
      - action: notify.mobile_app_phone
        data:
          message: >
            Mower fault {{ state_attr('binary_sensor.goat_fault', 'code') }}:
            {{ state_attr('binary_sensor.goat_fault', 'description') }}
```

There is deliberately **no timer and no counter** here — no "clear after
five minutes of no error", no "clear after ten zeros in a row". The zero
flood is unbounded: it lasted exactly as long as it took the owner to walk
out to the mower, so no threshold can tell recovery from a heartbeat. If
you want a delay, put it in your own automation with `for:`, where you can
tune it.

**Clear fault** needs nothing from the mower — it releases local state — so
it works while the machine is unreachable, and it is what guarantees the
fault cannot get stuck if some firmware never reports docking or mowing.
Every latch and release is logged at INFO with the reason, which is the
evidence to attach to an issue if it ever behaves oddly.

The latch itself lives in memory only: restarting Home Assistant or
reloading the integration clears it, and it comes back only if the mower
still reports a non-zero code when the integration starts back up.

### Error codes without a description

The error sensor's state is the raw code; its `description` attribute is the
text for it. That text comes from `deebot-client`, whose table predates the
mower line, so a GOAT-only code arrives with no description at all
([#37](https://github.com/nord-/ha-ecovacs-mower/issues/37)). The integration
fills the gaps it knows about:

| Code | Description | Ecovacs app also suggests |
|---|---|---|
| 422 | Weak signal. Return to the station. | clean the panoramic camera, the AI front camera and the ToF sensor; mow in daylight, not in heavy rain |
| 406 | Blade-disc blocked! Blade-disc cannot rotate. | — |

A code in neither table leaves the attribute empty and logs a warning naming
the code, once per code per restart. **That warning is the ask:** report the
code together with what the Ecovacs app shows for the same alarm in
[#37](https://github.com/nord-/ha-ecovacs-mower/issues/37), and it can be
added here. The app is the only authoritative source — Ecovacs publishes no
code reference for GOAT.

## Collecting logs

The **Show logs** button on the integration page filters Home Assistant's log
for the literal string `ecovacs_mower`, and nothing else. Almost everything
worth reading — command timeouts, the MQTT traffic, the raw map messages — is
logged by `deebot_client`, and those lines do not contain that string. So the
panel reports "No issues found for the search term 'ecovacs_mower'" even when
the log is full of relevant lines ([#26](https://github.com/nord-/ha-ecovacs-mower/issues/26)).
The filter comes from the Home Assistant frontend, which builds it from the
integration's domain; it cannot be changed from here.

The integration does log its own warning when the mower does not confirm a
command it was sent, so a failed start or pause shows up under the domain
filter — but the reason for it is in `deebot_client`'s lines, not ours.

To get a usable log:

1. Turn on **Enable debug logging** on the integration page. That covers both
   `custom_components.ecovacs_mower`, which Home Assistant always adds for the
   integration itself, and `deebot_client`, which it adds because
   `manifest.json` lists it under `loggers`. The equivalent in
   `configuration.yaml`, if you prefer it to survive restarts:

   ```yaml
   logger:
     logs:
       custom_components.ecovacs_mower: debug
       deebot_client: debug
   ```

2. Reproduce the problem.
3. Go to **Settings → System → Logs**, clear the search box or search for
   `deebot`, and use **Download full log**. The search only looks at the lines
   already loaded in the browser, so downloading beats scrolling.

For anything about the map, `deebot_client` at debug level is the only thing
that shows whether the mower sends `onMI`/`onArI`/`onMapTrack`/
`onSpecialContour` — or `onMapTrace`, which firmware 1.17 sends instead of
`onMapTrack` — at all. That is the log to attach to a map issue, and the
`info` fields have to be left intact: they are the map geometry itself.

## Current status

The `lawn_mower` entity is confirmed working against a real O1200: state
tracks the mower within seconds, and `start_mowing`, `pause`, and `dock` all
do what they should. It is also the one entity here that polls, every five
minutes: the mower does not always announce that it has finished and docked,
and without a poll the entity keeps reporting the run it is no longer doing. Its start command is also confirmed on an O800 RTK by
the user who reported that model.

The `sensor`, `switch`, `number`, `button`, and `event` platforms listed above
were added in 0.2.0. The `image` entity (the mower's map) was added in 0.3.0;
0.5.2 fixed its position track, which firmware 1.13.10 broke by flagging most
position samples `invalid: 2`.
The test suite, hassfest and HACS validation all pass in CI. The HACS job
skips the `brands` check, which requires an icon in the Home Assistant brands
repository — a requirement for listing in the HACS default store that does
not affect installation as a custom repository.

But **none of those entities have been verified against real hardware yet.**
That verification hasn't happened. Install with that in mind. If something
doesn't match what's documented here, an issue report is useful — include
your device class from the warning above if you're not on one of the models
in the table.

## License and credit

GPL-3.0. This project contains code derived from Home Assistant core's
`ecovacs` integration (Apache-2.0) and depends on `deebot-client`
(GPL-3.0). See [`NOTICE`](NOTICE) for the full attribution.
