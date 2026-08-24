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
four independent bugs in that path add up to a mower that can't be
controlled and barely reports its own state:

| Defect | Effect |
|---|---|
| Control commands are sent to the MQTT topic `iot/p2p/clean_V2` | GOAT firmware listens on `iot/p2p/clean` and ignores `clean_V2` entirely. `start_mowing` and `pause` do nothing. |
| State refresh uses the command `getCleanInfo_V2` | GOAT mowers never answer it. Polled state refreshes silently fail. |
| The unsolicited messages `onChargeInfo` and `onScheduleTaskInfo` have no handler | They're dropped as unknown messages. In practice this means the mower's state in Home Assistant only updates once a day, when it happens to reconnect and get polled. |
| Every login goes through the password endpoint, which answers `1013` for some accounts even after the device was verified | The config entry loops on "Device verification required": the emailed code is accepted, the reload logs in with the password, is refused, and another code is requested. The integration never finishes setting up. |

Net effect on the upstream integration: state lags by close to a day, and
the controls that are supposed to fix that don't work either.

This integration patches all four at the protocol layer (correct command,
correct refresh call, handlers for both missing messages, and a session renewal
that does not touch the password endpoint) and exposes a `lawn_mower` entity
that reflects real state within seconds and responds to start / pause / dock.

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
| **Ecovacs GOAT O800 RTK** | `2px96q` | a user ([#24](https://github.com/nord-/ha-ecovacs-mower/issues/24)) |
| **Ecovacs GOAT G1-800** | `77atlz` | reported, patch not yet confirmed — firmware 1.36.208 ([#30](https://github.com/nord-/ha-ecovacs-mower/issues/30)) |
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

Thirty-seven entities on the mower's device page, across eight platforms:

| Platform | Count | What |
|---|---|---|
| `lawn_mower` | 1 | Real state (`mowing`, `paused`, `returning`, `docked`, `error`) that updates within seconds, plus working `start_mowing`, `pause`, and `dock` |
| `sensor` | 15 | Activity (the mower's state with the reason folded in — `returning_rain`, `docked_rain_delay`; see below), battery, error code (disabled by default — see below), mowed area, mowing time, three lifetime totals (area, time, session count), four consumable-lifespan percentages (blade, lens brush, trimmer brush, weed rope), IP address, Wi-Fi signal strength, Wi-Fi network name |
| `binary_sensor` | 5 | Rain protection, rain delay, emergency stop, locked, animal protection — the mower's raw protection flags, from the `onProtectState` message the library drops (see below) |
| `switch` | 7 | Advanced mode, TrueDetect obstacle avoidance, edge cutting, child lock, lift warning, boundary crossing warning, safety protection |
| `number` | 2 | Notification volume, cutting direction |
| `button` | 5 | Reset each of the four consumable lifespans, plus "Locate mower" (plays a sound on the device) |
| `event` | 1 | Last mowing job (finished / finished with warnings / manually stopped) |
| `image` | 1 | The mower's map — lawn boundary, mowed coverage, no-go zones, detected obstacles, the dock and the mower's live position track. Add it to a dashboard with a `picture-entity` card. Decoded from the GOAT's own map messages (`onMI`/`onArI`/`onMapTrack`/`onSpecialContour`); the format is documented in `docs/superpowers/specs/2026-08-10-mower-map-design.md`. Geometry survives restarts; the position track is live-only |

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
per flag, with no interpretation on top. In particular `rain_protect`
(`isRainProtect`) is **not** presented as a live rain reading: in the one
payload captured so far it is `1`, but the settings message in the same log has
`RainDetect: 1` and `isAnimProtect: 0` likewise matches `ProtectAnimal.enable:
0` — so "this protection is switched on" fits the data as well as "it is
raining right now". A sample from a dry period settles it:

```
grep onProtectState home-assistant.log | tail -1
```

If `isRainProtect` is `0` while the mower is out mowing, the flag is a live
state and the entity can be renamed and given the `moisture` device class. Until
then the rain-aware states come from `trigger`, which needs no interpretation.

All five `binary_sensor` flags are asked for with `getProtectState` when Home
Assistant starts, and updated from the `onProtectState` push after that. The
device only pushes when a flag flips, so before that command was wired up
(issue #31) the flags stayed `unknown` for anyone whose protection settings
simply stayed as they were.

The rain *reason* on `sensor.activity` is not restored by a restart, though — it
comes from `trigger`, which nothing can ask for, so a restart during an active
rain delay reads as plain `docked` until the mower's next scheduled run.

### Entities disabled by default

**17 of the 37 entities** ship with `entity_registry_enabled_default=False`,
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
| `sensor` | 4 of 15 | IP address, Wi-Fi signal strength, Wi-Fi network name, and **error code** |
| `button` | 4 of 5 | the four consumable-lifespan resets (blade, lens brush, trimmer brush, weed rope) — "Locate mower" is enabled by default |

**If you're planning anything on the error sensor** — an alarm, a
notification, a dashboard card — note that it does not exist as an
enabled entity out of the box. `sensor.<device>_error` is created disabled
and reports nothing until you enable it by hand, same as the other 16
above. This is the one entity in this list someone is likely to go
looking for by name, so it's worth repeating here rather than only in the
table.

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
`onSpecialContour` at all — that is the log to attach to a map issue.

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
