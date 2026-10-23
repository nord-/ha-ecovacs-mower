# Area-parameter capability

This integration provides read/write support for per-area mowing parameters on the A1600 LiDAR Pro (`e4gqia`), the A3000 LiDAR Pro (`51rcxt`) and the O1200 LiDAR Pro (`2i0fns`). The mower identifies an area by `areaID`; its friendly name and its parameter values arrive through separate protocol responses.

## Functional flow

At refresh time, the patch sends `getAreaParameter` and `getAreaSet`. The first supplies the raw parameters for each `areaID`; the second supplies the mower's current area inventory and names for the map in use. That map's id is learned at runtime from the envelope of the mower's map messages (`state_precedence.map_id_for`), so `getAreaSet` is sent only once the id is known, and learning it requests the area refresh again. The patch combines those responses into one authoritative per-device area snapshot and publishes a `MowerAreaEvent` when it changes.

The mower also sends `onAreaParameter` unsolicited, to every connected MQTT client, whenever an area's parameters change — confirmed both for writes issued from Home Assistant and for changes made in the Ecovacs app, typically arriving within a couple hundred milliseconds, well before an explicit `getAreaParameter` refresh would land. It carries the identical `areaParameters` list as `getAreaParameter`'s answer and is parsed by the same code (`apply_area_parameters`), so either path updates the same snapshot. `setAreaParameter` itself does not support deebot-client's usual MQTT p2p echo handling (the `SetCommand`/`get_command` pairing other settings use), which is why this relies on registering the push directly instead of that mechanism.

The snapshot is kept in `deebot_patch/areas.py`, keyed by the device's event bus. This keeps protocol state separate from Home Assistant entities and, importantly, gives a write operation access to the complete last-known raw parameter set. A parameter entity must not overwrite the other fields with guessed defaults: a write sends every raw value the mower reported for the area, the changed one replaced. If a value the model's mapping needs is not known, the write is refused rather than risking unintended mower settings.

Area names and IDs are also kept in that same snapshot because `getAreaSet` is the authoritative source for the current area inventory. Areas no longer reported by the mower are removed from the snapshot, except when `getAreaSet` lists no area at all: that is also what the mower answers for a map it does not have, so it is not taken as the inventory being empty.

## What `deebot_patch` owns

The patch layer owns only the missing Ecovacs protocol pieces and their raw values:

- `GetAreaParameter` parses `getAreaParameter` and stores the raw value of each known key — `AREA_PARAMETER_KEYS` in `areas.py`: `mowHeightLevel`, `cutMode`, `obstacleHeight` and `angle` — in `MowerArea.parameters`, keyed by wire key. A key the mower does not report is absent, so a model without a parameter simply lacks it. A key not in that list is not stored: a field a firmware adds reaches Home Assistant only once it is added there and mapped per model.
- `OnAreaParameter` parses the mower's unsolicited `onAreaParameter` push through the same `apply_area_parameters` helper `GetAreaParameter` uses, registered in `deebot_patch/messages.py` and `apply()` the same way as this integration's other unsolicited-message handlers (`OnProtectState`, `OnRainDelay`, etc.).
- `GetAreaSet` reassembles the mower's chunked `ar` response in a per-device fragment buffer, decodes it using the existing deebot-client decompressor, and extracts the area ID and name.
- `SetAreaParameter` sends the raw values it is given for one `areaID`, by wire key; the HA layer gives it the area's whole stored set. It extends `ExecuteCommand`, so a reply with a non-zero `code` fails the command instead of passing for success.
- The two read commands are registered together as the area refresh so the snapshot can be populated from both responses.

These commands live in `deebot_patch/commands.py`, alongside the other patched protocol commands. `deebot_patch` does not translate the values into units or user-facing meanings.

## What Home Assistant owns

`area_numbers.py` turns the raw `MowerArea` state into one Home Assistant entity per parameter the model has. It holds two tables:

- `AREA_PARAMETERS` describes each known parameter as Home Assistant shows it on every model: name, unit, icon, and the suffix in the entity's unique ID.
- `AREA_PARAMETER_VALUE_MAPPINGS` has one self-contained entry per validated class: the parameters that model has, by wire key, each with its conversion between raw and HA values. A parameter the model does not have is left out and gets no entity.

The model-specific conversions are defined there because they are part of the validated behavior of each mower model, not generic Ecovacs protocol knowledge. Each entry is written out in full, also where two models' values are the same, as the A3000 LiDAR Pro's are the A1600's: a change made for one model never changes another.

For a write, HA converts the requested value back to its raw representation and combines it with the area's other raw values — see "Write consistency" below for where those values come from and why. The write carries every value the mower reported for the area, as the Ecovacs app's own `setAreaParameter` does, and is refused while one the model maps is unknown. The entity does not update its state optimistically.

The capability is exposed only for `e4gqia`, `51rcxt`, whose own readings matched the A1600's, and `2i0fns`, whose readings gave its own height and speed tables (issue #23). Other supported mower models do not receive these entities merely because they use the same protocol field names.

## Write consistency

Sending a write and immediately reading the area snapshot back is not safe: the snapshot only reflects a write once the mower has confirmed it, and confirmation is not instantaneous. Two defenses exist, at different layers, deliberately kept independent:

- `OnAreaParameter` (above) closes most of the gap by applying the mower's own push within roughly 150ms of a write.
- `area_numbers._PendingAreaWrite` closes what remains. Writes to one area are serialized by a per-area lock, so two that overlap cannot both merge against the same snapshot. A write the mower accepted becomes the next write's merge base until the mower next reports its parameters — by the push, or by the refresh that follows every write — so a second fast change cannot silently revert the first. That report is counted per area by the handler (`areas.parameter_answers`), so a push for another area does not retire the write, and not observed through `MowerAreaEvent`, because the event bus drops a report that repeats the last one. A write the mower did not confirm is never used as a base. This does not depend on the push arriving — it is what protects a mower or firmware that never sends `onAreaParameter`.

Neither layer updates HA entity state optimistically; `_PendingAreaWrite` only affects what the *next write* merges against, not what is displayed.

## Availability

An area removed from the mower's inventory (per `getAreaSet`/`GetAreaSet`) is not deleted from Home Assistant — its entities become unavailable (`EcovacsAreaNumber.available` combines device availability with area presence) rather than showing a stale or blank value. Each entity tracks its own area's presence from its own `MowerAreaEvent` subscription; the platform-level handler only discovers new areas. If the area reappears with the same `areaID`, the existing entities recover; nothing is re-created.

## Adding another mower model

A future model should be added only after its area protocol has been independently verified. In particular, verify:

1. that the model actually supports the area commands and the expected `areaID`/area inventory behavior;
2. the raw meaning and valid range of each parameter;
3. the mapping of those raw values to Home Assistant values and back;
4. that a write carrying every value the model reports for an area is safe, compared with the app's own `setAreaParameter` request; and
5. whether the model sends `onAreaParameter` at all — it has been observed on `e4gqia`, firmware 1.11.31. On `51rcxt`, firmware 1.13.10, it is inferred rather than captured: a change made in the app showed in Home Assistant immediately (PR #137). On `2i0fns` it is likewise inferred: changes made in the app showed up seconds apart, faster than the 15-second poll of the area-parameter log (issue #23). `_PendingAreaWrite` does not depend on it, but a model that never sends it will feel slower to confirm writes.

The **Log area parameters** button (`debug.py`, disabled by default) collects the raw side of point 2 and 3 from an owner: it reads the areas once, then for 15 minutes shows every raw value that changes while the owner steps through the settings in the Ecovacs app, in a persistent notification the owner can copy into an issue. It relies on `onAreaParameter` where the model sends it and asks again every 15 seconds for one that does not (point 5). It is read-only and exists on every mower, so it can be used before a model has the profile flag or a mapping. See "Helping to support another model" in the README for the steps a user is asked to follow.

Keep the raw protocol handling shared where the wire format is genuinely the same. Add a model's entry to `AREA_PARAMETER_VALUE_MAPPINGS` only when its values have been validated for that model, naming only the parameters it has, and gate the HA capability to that model with the `area_parameters` profile flag. A parameter no model has had before needs its wire key in `AREA_PARAMETER_KEYS` and an entry in `AREA_PARAMETERS` first; neither gives any model an entity by itself. Do not assume that an identically named Ecovacs field has identical units, ranges, or semantics on another mower.

This keeps the area-parameter capability narrowly scoped: protocol parsing and raw state in `deebot_patch`, model-specific interpretation and HA exposure in `area_numbers.py`, with no unrelated restructuring of existing mower capabilities.

## Known gaps

`getAreaSet` (area names/inventory) has no confirmed unsolicited-push equivalent, unlike `getAreaParameter`. A capture of the phone app polling `getAreaSet` while idle showed only `iot/p2p/...` request/response traffic — the same "does not support p2p handling (yet)" path `setAreaParameter` hits — and no `iot/atr/onAreaSet`-style topic. This does not rule one out: the capture did not include an actual area rename or re-map, which is the case that would produce it if it exists. Area names and inventory therefore still only update through the polled refresh, reload, or a manual entity update.
