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
three independent bugs in that path add up to a mower that can't be
controlled and barely reports its own state:

| Defect | Effect |
|---|---|
| Control commands are sent to the MQTT topic `iot/p2p/clean_V2` | GOAT firmware listens on `iot/p2p/clean` and ignores `clean_V2` entirely. `start_mowing` and `pause` do nothing. |
| State refresh uses the command `getCleanInfo_V2` | GOAT mowers never answer it. Polled state refreshes silently fail. |
| The unsolicited messages `onChargeInfo` and `onScheduleTaskInfo` have no handler | They're dropped as unknown messages. In practice this means the mower's state in Home Assistant only updates once a day, when it happens to reconnect and get polled. |

Net effect on the upstream integration: state lags by close to a day, and
the controls that are supposed to fix that don't work either.

This integration patches all three at the protocol layer (correct command,
correct refresh call, handlers for both missing messages) and exposes a
`lawn_mower` entity that reflects real state within seconds and responds to
start / pause / dock.

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

Verified on two devices:

| Model | Device class | Verified by |
| --- | --- | --- |
| **Ecovacs GOAT O1200 LiDAR Pro** | `2i0fns` | the author's own hardware |
| **Ecovacs GOAT O800 RTK** | `9bts2s` | a user, firmware 1.13.8 ([#8](https://github.com/nord-/ha-ecovacs-mower/issues/8)) |

Other GOAT models (A1600 RTK and the rest of the GOAT line) share the same
three upstream defects described above, and would very likely work with the
same fix. They are not verified, because nobody has reported back on one.

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
verified model to `SUPPORTED_CLASSES` is a small, low-risk change — this is
one of the more useful ways to contribute without touching Python.

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

## What you get

Thirty entities on the mower's device page, across six platforms:

| Platform | Count | What |
|---|---|---|
| `lawn_mower` | 1 | Real state (`mowing`, `paused`, `returning`, `docked`, `error`) that updates within seconds, plus working `start_mowing`, `pause`, and `dock` |
| `sensor` | 14 | Battery, error code (disabled by default — see below), mowed area, mowing time, three lifetime totals (area, time, session count), four consumable-lifespan percentages (blade, lens brush, trimmer brush, weed rope), IP address, Wi-Fi signal strength, Wi-Fi network name |
| `switch` | 7 | Advanced mode, TrueDetect obstacle avoidance, edge cutting, child lock, lift warning, boundary crossing warning, safety protection |
| `number` | 2 | Notification volume, cutting direction |
| `button` | 5 | Reset each of the four consumable lifespans, plus "Locate mower" (plays a sound on the device) |
| `event` | 1 | Last mowing job (finished / finished with warnings / manually stopped) |

Not included yet: **RTK diagnostics** (position and satellite data), zone
control, and maps. RTK is planned for the next release; the other two have
no committed date.

### Entities disabled by default

**17 of the 30 entities** ship with `entity_registry_enabled_default=False`,
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
| `sensor` | 4 of 14 | IP address, Wi-Fi signal strength, Wi-Fi network name, and **error code** |
| `button` | 4 of 5 | the four consumable-lifespan resets (blade, lens brush, trimmer brush, weed rope) — "Locate mower" is enabled by default |

**If you're planning anything on the error sensor** — an alarm, a
notification, a dashboard card — note that it does not exist as an
enabled entity out of the box. `sensor.<device>_error` is created disabled
and reports nothing until you enable it by hand, same as the other 16
above. This is the one entity in this list someone is likely to go
looking for by name, so it's worth repeating here rather than only in the
table.

## Current status

The `lawn_mower` entity is confirmed working against a real O1200: state
tracks the mower within seconds, and `start_mowing`, `pause`, and `dock` all
do what they should. Its start command is also confirmed on an O800 RTK by
the user who reported that model.

The `sensor`, `switch`, `number`, `button`, and `event` platforms listed above
were added in 0.2.0. The test suite, hassfest and HACS validation all pass in
CI. The HACS job skips the `brands` check, which requires an icon in the Home
Assistant brands repository — a requirement for listing in the HACS default
store that does not affect installation as a custom repository.

But **none of those entities have been verified against real hardware yet.**
That verification hasn't happened. Install with that in mind. If something
doesn't match what's documented here, an issue report is useful — include
your device class from the warning above if you're not on one of the models
in the table.

## License and credit

GPL-3.0. This project contains code derived from Home Assistant core's
`ecovacs` integration (Apache-2.0) and depends on `deebot-client`
(GPL-3.0). See [`NOTICE`](NOTICE) for the full attribution.
