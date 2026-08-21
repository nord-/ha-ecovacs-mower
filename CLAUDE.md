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

## Architecture

### The patch layer is the only connection to deebot-client's internals

`deebot_patch/` is the boundary: **no other module may touch private parts of `deebot_client`** (`_DEVICES`, `MESSAGES`, `_AuthClient`). If the library is swapped for a vendored client, only that folder needs rewriting.

- `commands.py` — `CleanMower`, inherits from `Clean` (topic `clean`) with a V2 payload. Replaces `CleanV2`, which publishes on `clean_V2`, which GOAT firmware ignores.
- `hardware.py` — `patch_device_info()` seeds the `_DEVICES` cache with corrected `Capabilities` (`CleanMower` + `GetCleanInfo` instead of `GetCleanInfoV2`). Uses the library's own caching mechanism instead of monkeypatching. `SUPPORTED_CLASSES` lists the device classes to patch (`2i0fns` = O1200 LiDAR Pro, `9bts2s` and `2px96q` = O800 RTK, `77atlz` = G1-800). Membership means "we patch it", not "someone confirmed it works" — `77atlz` was added on a report of the class string alone, and the comment block above the tuple records which is which.
- `messages.py` — `OnChargeInfo` and `OnScheduleTaskInfo`, the two unsolicited messages the library lacks a handler for.
- `authentication.py` — `AccountAuthenticator`, which renews the session from the `uid`/`accessToken` pair a login or a device verification returns instead of re-posting the password. Backport of the still-open DeebotUniverse/client.py#1743. It wraps two name-mangled privates of `_AuthClient` on the instance; the pair is persisted in `entry.data[CONF_CREDENTIALS]` by the config flow and read back by the controller. Without it, Ecovacs' `1013` answer to the password login sends the entry into an endless reauth loop (issue #21).
- `__init__.py` — `apply()` (registers the messages, idempotent) and `verify_capabilities()`.

### The order in `EcovacsController.initialize()` is a hard invariant

```
apply() → patch_device_info(each SUPPORTED_CLASS) → get_devices() → verify_capabilities()
```

`get_devices()` bakes the capabilities into `DeviceInfo.static`, a frozen dataclass. Patching afterwards means the devices already got the unpatched ones. `verify_capabilities()` therefore checks **the object the device actually received**, not the cache — a cache lookup would look correct regardless.

Failing fast is intentional: if `deebot-client` doesn't look like the patch layer expects, it raises `PatchContractError` → `ConfigEntryError`, and the integration refuses to start rather than silently stop reporting the mower's state. `tests/deebot_patch/test_contract.py` catches the same assumptions in CI.

### Entity platforms

`lawn_mower` filters on `device_type is DeviceType.MOWER`. The others (`sensor`, `switch`, `number`, `button`, `event`) are built declaratively: an `ENTITY_DESCRIPTIONS` tuple of `EcovacsCapabilityEntityDescription` subclasses with `capability_fn`, fed through `util.get_supported_entities()`. New entities are added as an entry in that tuple — not as a new class.

`entity.py` has the base classes (`EcovacsEntity`, `EcovacsDescriptionEntity`); subscribing to events happens via `_subscribe()` in `async_added_to_hass`. Commands go out through `_execute_command()`, never `self._device.execute_command()` directly — the wrapper is what logs an unconfirmed command under this integration's own logger instead of leaving it to `deebot_client` (issue #26).

## Conventions

- **This is a public repo — all outward-facing text is English**: docstrings, comments, commit messages, PR descriptions, issue/discussion replies. Code identifiers are English too. Forked modules open their docstring with what was removed compared to core.
- Comments explain *why*, especially where the code looks needlessly convoluted (exact type comparison instead of `isinstance`, in-place mutation instead of rebinding). Don't remove them to "clean up".
- `strings.json` and `translations/en.json` must be **identical** — `test_translations.py` guards this, nothing syncs them automatically. Never create an `sv.json`; the HA frontend's language here is English.
- Every translation key and `icons.json` key must belong to a real entity — the platform tests check both directions.
- New hardware is supported by adding the device class to `SUPPORTED_CLASSES`. Unsupported MOWER classes log a warning with the class string; that's the string users are asked to report.
- Version is bumped in `manifest.json`, and that bump is what publishes a release once it lands on `master` (see above). The bump belongs in its own commit by the maintainer after the work has landed, never in a feature PR: `release.yml` tags whatever version it finds without checking that it is newer than the last tag, so two branches bumping in parallel publish releases out of order. `deebot-client` is pinned there and in `requirements-test.txt` — keep them in sync.
- Conventional commits, no AI attribution.
