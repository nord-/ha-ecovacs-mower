# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

Home Assistant custom integration (`custom_components/ecovacs_mower`) for Ecovacs GOAT lawn mowers. It's a **fork of HA core's `ecovacs` integration** with XMPP/legacy support cut out (MQTT-only), plus a patch layer that fixes three bugs in `deebot-client` that make the GOAT uncontrollable. See README.md for the bug descriptions and links to the upstream PRs.

## Commands

```bash
# Full suite — requires Linux/CI
python -m pytest tests/ -v

# Single file / test
python -m pytest tests/test_sensor.py -v
python -m pytest tests/test_sensor.py::test_name -v

# Locally on Windows: only the protocol layer can run
python -m pytest tests/deebot_patch/ -p no:homeassistant -v

pip install -r requirements-test.txt
```

**Home Assistant can't be imported on Windows** (`homeassistant/runner.py` does an unguarded `import fcntl`). Therefore:

- Anything that imports HA lives in a file marked `pytestmark = requires_ha` (from `tests/__init__.py`).
- `-p no:homeassistant` is required locally because `pytest_homeassistant_custom_component` auto-loads as an entry point — the flag does **not** belong in `pytest.ini`, CI needs the plugin.
- The source of truth for test results is CI (`.github/workflows/test.yml`, ubuntu-latest, Python 3.14). Never claim the suite is green based on a Windows run.

CI also runs hassfest and HACS validation (`.github/workflows/hassfest.yml`). The HACS job's `topics` error is expected — it only applies to listing in the default store. The `brands` check is explicitly ignored in the workflow for the same reason.

Releases are cut by `.github/workflows/release.yml`, which runs after the test suite succeeds on `master`: if the version in `manifest.json` has no matching `v<version>` tag, it creates the tag and publishes a release with generated notes. A push whose version is already tagged is a no-op, so the bump commit is what triggers a release — never a hand-made tag.

Cutting a release is therefore three steps, and the bump is the one commit that goes straight onto `master` without a PR — the maintainer pushes it with the admin bypass, the way every bump so far has landed: (1) list what has merged since the last tag with `git log v<last>..origin/master --merges` and pick the level from it — a `feat:` in there means a minor bump, only fixes means a patch; (2) change `version` in `manifest.json`, the only place the version lives, in a commit titled `chore: bump to <version>` with nothing else in it; (3) push, report the commit hashes, and stop — the maintainer watches the `Release` run produce the `v<version>` tag, so do not poll CI after a bump. Anything else that should ship in the release — docs included — must be on `master` before the bump commit, since the generated notes cover exactly the commits between the two tags.

## Architecture

### The patch layer is the only connection to deebot-client's internals

`deebot_patch/` is the boundary: **no other module may touch private parts of `deebot_client`** (`_DEVICES`, `MESSAGES`, `_AuthClient`). If the library is swapped for a vendored client, only that folder needs rewriting.

- `commands.py` — `CleanMower` and `GetCleanInfoMower`, family-adaptive wrappers that send whichever of the V2/non-V2 command pair the mower actually answers on (issue #42), and `MowerStateRefresh`, which replaces the library's two concurrent state commands with one sequential command so the charge half is recorded before the clean-info half is interpreted (issue #67). Also the area protocol commands: `GetAreaParameter` and `GetAreaSet`, the two reads that fill the raw area snapshot in `areas.py`, and `SetAreaParameter`, which writes all four raw parameters of one area at once. `GetAreaSet` asks about the map in use, read from `state_precedence.map_id_for()` when the request goes out, and sends nothing while that id is unknown.
- `hardware.py` — `patch_device_info()` seeds the `_DEVICES` cache with corrected `Capabilities` (`CleanMower` + `capabilities.state = [MowerStateRefresh()]`). Uses the library's own caching mechanism instead of monkeypatching. `SUPPORTED_CLASSES` maps each device class to patch to its `MowerProfile` (`2i0fns` = O1200 LiDAR Pro, `9bts2s` and `2px96q` = O800 RTK, `77atlz` = G1-800, `e4gqia` = A1600 LiDAR Pro, `xmp9ds` = A1600 RTK — the LiDAR Pro and RTK variants are different machines, not two strings for one — `o4kvvk` = A3000 LiDAR, `6n9pcz` = O600 RTK, `0jbd6s` = O1000 LiDAR Pro). Membership means "we patch it", not "someone confirmed it works" — `xmp9ds`, `o4kvvk`, `6n9pcz` and `0jbd6s` are the entries whose patched controls nobody has confirmed, and the comment block above the mapping records how each class was confirmed. A flag on the profile is the opposite sense from membership: it means that behaviour, raw-value semantics included, is validated on that class. `area_parameters` is the only flag so far (`e4gqia`), and it is what gives a class the `MowerAreaEvent` refresh and the per-area entities. `ZONE_AREA_CLASSES` is the opposite kind of tuple: membership means zone mowing *is* confirmed on that class, and only those receive `MowArea` as their `area` capability (`e4gqia` and `77atlz` so far). `BORDER_CLASSES` is the same kind as `ZONE_AREA_CLASSES`: membership means the border-job request shape is captured on that class, and only those get the `mow_border` button (`77atlz` so far). It gates a button rather than a capability, because `Capabilities` has no field for it — `button.py` checks the class string directly. These two tuples predate the profile and have not been migrated to profile flags.
- `messages.py` — `OnChargeInfo` and `OnScheduleTaskInfo`, the two unsolicited messages the library lacks a handler for, and `OnAreaParameter`, the push the mower broadcasts to every client whenever an area's parameters change, from Home Assistant or from the app.
- `areas.py` — the raw per-area snapshot (`MowerArea`, published as `MowerAreaEvent`), keyed by `EventBus`. `apply_area_parameters()` is the one parser for both `getAreaParameter`'s answer and the `onAreaParameter` push, merged per field so a short push never blanks a known value. `parameter_answers()` counts the reports per area inside that handler, because a repeat the bus drops must still retire a pending write. Also holds the per-bus `getAreaSet` fragment buffer. Values stay raw here; turning them into units is the HA layer's job.
- `zonal.py` — `MowArea`, the `spotArea` command that starts one or more saved mowing areas, built on the same `_AdaptiveFamily` machinery as `CleanMower` so `families.py` stays the only place that decides the topic. Exposed to HA as the `ecovacs_mower.mow_area` entity service, which `lawn_mower.py` gates on `area is MowArea` and answers with a clear error for every other class.
- `border.py` — `MowBorder`, the `border` task-start command (issue #12), built on the same `_AdaptiveFamily` machinery as `MowArea` and sharing `_TaskClean`, the `{"type", "value"}` payload builder in `commands.py`, with it. Needs the id of the map in use, which it does not fetch: it is handed one by `button.py`, which reads it from `state_precedence.map_id_for()`. Exposed as the `mow_border` button, alongside `end_task`, which is `CleanMower(CleanAction.STOP)` and needs nothing from this module (issue #51).
- `families.py` — which of the V2/non-V2 command pair a given mower answers on, keyed by `did` and learned at runtime rather than from the class string (issue #42).
- `state_precedence.py` — per-device record, keyed by `EventBus`, of the facts the handlers learn from the stream: that the mower is docked, which beats a paused plan, and what the suppressed state was (issue #67); and the id of the map the mower is using, written by `map_messages._MapMessage` from every map message's envelope and read by the border button (issue #12) and by `GetAreaSet`. A change of that id requests the area refresh, so the area names arrive once the first map message has named the map. `register()` is also the marker that says a bus belongs to a patched mower rather than a vacuum on the same account.
- `authentication.py` — `AccountAuthenticator`, which renews the session from the `uid`/`accessToken` pair a login or a device verification returns instead of re-posting the password. Backport of the still-open DeebotUniverse/client.py#1743. It wraps two name-mangled privates of `_AuthClient` on the instance; the pair is persisted in `entry.data[CONF_CREDENTIALS]` by the config flow and read back by the controller. Without it, Ecovacs' `1013` answer to the password login sends the entry into an endless reauth loop (issue #21).
- `__init__.py` — `apply()` (registers the messages, idempotent) and `verify_capabilities()`.

### The order in `EcovacsController.initialize()` is a hard invariant

```
apply() → patch_device_info(each SUPPORTED_CLASS) → get_devices() → verify_capabilities()
```

`get_devices()` bakes the capabilities into `DeviceInfo.static`, a frozen dataclass. Patching afterwards means the devices already got the unpatched ones. `verify_capabilities()` therefore checks **the object the device actually received**, not the cache — a cache lookup would look correct regardless.

Failing fast is intentional: if `deebot-client` doesn't look like the patch layer expects, it raises `PatchContractError` → `ConfigEntryError`, and the integration refuses to start rather than silently stop reporting the mower's state. `tests/deebot_patch/test_contract.py` catches the same assumptions in CI.

### State derived from the event stream is written by the handlers, never by a subscription

`EventBus.notify` drops an event equal to the previous one of the same type **before** any subscriber runs, and it dispatches subscribers through `create_task`. So a subscription is neither complete nor synchronous: it misses every repeat, and repeats are ordinary — the captured telemetry has two `CLEANING` pushes sixteen seconds apart.

Anything this integration needs to remember about what the mower reported is therefore written inside the handlers it owns, before they notify. `deebot_patch/state_precedence.py` is the worked example (issue #67), and `MowerTriggerEvent._seq` is the same hazard met from the other side — an event deliberately made unequal to its predecessor so the bus cannot swallow it.

Subscribing is still right for *reacting* to a state — `fault.py` and the entity platforms do exactly that. The rule is about deriving and holding state, not about consuming it.

### Entity platforms

`lawn_mower` filters on `device_type is DeviceType.MOWER`. The others (`sensor`, `switch`, `number`, `button`, `event`) are built declaratively: an `ENTITY_DESCRIPTIONS` tuple of `EcovacsCapabilityEntityDescription` subclasses with `capability_fn`, fed through `util.get_supported_entities()`. New entities are added as an entry in that tuple — not as a new class. The exception is a command that needs the device's runtime state or has no library capability — such a button is one more entry in `button.py`'s `MOWER_COMMAND_DESCRIPTIONS`, whose `command_fn` builds the command from the device at press time.

The per-area parameter entities in `area_numbers.py` are the other exception: dynamic, because the mower reports its area ids at runtime. `number.py` hands over to `async_setup_area_numbers()`, which only discovers new areas, and each `EcovacsAreaNumber` projects its own area from its own `MowerAreaEvent` subscription. The raw-to-HA mappings are per model in `AREA_PARAMETER_MAPPINGS`, and a class needs both the `area_parameters` profile flag and a mapping entry to get any entity. A write merges the one changed value into all four raw values and sends them as one `SetAreaParameter`; writes to one area are serialised, and a confirmed write stays the next write's merge base until the mower reports that area again.

`entity.py` has the base classes (`EcovacsEntity`, `EcovacsDescriptionEntity`); subscribing to events happens via `_subscribe()` in `async_added_to_hass`. Commands go out through `_execute_command()`, never `self._device.execute_command()` directly — the wrapper is what logs an unconfirmed command under this integration's own logger instead of leaving it to `deebot_client` (issue #26). It returns whether the command was confirmed, for a caller that must not build on a write the mower never took.

## Conventions

- **This is a public repo — all outward-facing text is English**: docstrings, comments, commit messages, PR descriptions, issue/discussion replies. Code identifiers are English too. Forked modules open their docstring with what was removed compared to core.
- Comments explain *why*, especially where the code looks needlessly convoluted (exact type comparison instead of `isinstance`, in-place mutation instead of rebinding). Don't remove them to "clean up".
- `strings.json` and `translations/en.json` must be **identical** — `test_translations.py` guards this, nothing syncs them automatically. Never create an `sv.json`; the HA frontend's language here is English.
- Every translation key and `icons.json` key must belong to a real entity — the platform tests check both directions.
- New hardware is supported by adding the device class to `SUPPORTED_CLASSES` as a `MowerProfile` with no flags. Unsupported MOWER classes log a warning with the class string; that's the string users are asked to report.
- A model-specific capability is enabled per class only once it is validated on that class: set the profile flag, and for the area parameters add that model's own entry to `AREA_PARAMETER_MAPPINGS`. A field with the same name on another GOAT is not evidence that its values mean the same. `docs/area-parameter-capability.md` lists what to verify for the area parameters.
- Version is bumped in `manifest.json`, and that bump is what publishes a release once it lands on `master` (see above). The bump belongs in its own commit by the maintainer after the work has landed, never in a feature PR: `release.yml` tags whatever version it finds without checking that it is newer than the last tag, so two branches bumping in parallel publish releases out of order. `deebot-client` is pinned there and in `requirements-test.txt` — keep them in sync.
- Conventional commits, no AI attribution.
