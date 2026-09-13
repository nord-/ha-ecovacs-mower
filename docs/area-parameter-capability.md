# Area-parameter capability

This integration provides read/write support for per-area mowing parameters on the A1600 LiDAR Pro (`e4gqia`). The mower identifies an area by `areaID`; its friendly name and its parameter values arrive through separate protocol responses.

## Functional flow

At refresh time, the patch sends `getAreaParameter` and `getAreaSet`. The first supplies the raw parameters for each `areaID`; the second supplies the mower's current area inventory and names. The patch combines those responses into one authoritative per-device area snapshot and publishes a `MowerAreaEvent` when it changes.

The mower also sends `onAreaParameter` unsolicited, to every connected MQTT client, whenever an area's parameters change — confirmed both for writes issued from Home Assistant and for changes made in the Ecovacs app, typically arriving within a couple hundred milliseconds, well before an explicit `getAreaParameter` refresh would land. It carries the identical `areaParameters` list as `getAreaParameter`'s answer and is parsed by the same code (`apply_area_parameters`), so either path updates the same snapshot. `setAreaParameter` itself does not support deebot-client's usual MQTT p2p echo handling (the `SetCommand`/`get_command` pairing other settings use), which is why this relies on registering the push directly instead of that mechanism.

The snapshot is kept in `deebot_patch/areas.py`, keyed by the device's event bus. This keeps protocol state separate from Home Assistant entities and, importantly, gives a write operation access to the complete last-known raw parameter set. A parameter entity must not overwrite the other fields with guessed defaults: writes send the complete `setAreaParameter` payload. If the required raw values are not known, the write is refused rather than risking unintended mower settings.

Area names and IDs are also kept in that same snapshot because `getAreaSet` is the authoritative source for the current area inventory. Areas no longer reported by the mower are removed from the snapshot.

## What `deebot_patch` owns

The patch layer owns only the missing Ecovacs protocol pieces and their raw values:

- `GetAreaParameter` parses `getAreaParameter` and stores `mowHeightLevel`, `cutMode`, `obstacleHeight`, and `angle` as raw values.
- `OnAreaParameter` parses the mower's unsolicited `onAreaParameter` push through the same `apply_area_parameters` helper `GetAreaParameter` uses, registered in `deebot_patch/messages.py` and `apply()` the same way as this integration's other unsolicited-message handlers (`OnProtectState`, `OnRainDelay`, etc.).
- `GetAreaSet` reassembles the mower's chunked `ar` response, decodes it using the existing deebot-client decompressor, and extracts the area ID and name.
- `SetAreaParameter` sends all four raw parameter fields for one `areaID`.
- The two read commands are registered together as the area refresh so the snapshot can be populated from both responses.

These commands live in `deebot_patch/commands.py`, alongside the other patched protocol commands. `deebot_patch` does not translate the values into units or user-facing meanings.

## What Home Assistant owns

`area_sensors.py` turns the raw `MowerArea` state into the four Home Assistant parameter entities. The A1600-specific mappings between raw protocol values and HA values are defined there because those mappings are part of the validated behavior of that mower model, not generic Ecovacs protocol knowledge.

For a write, HA converts the requested value back to its raw representation and combines it with the other three raw values — see "Write consistency" below for where those three values come from and why. The entity does not update its state optimistically.

The capability is exposed only for `e4gqia`. Other supported mower models do not receive these entities merely because they use the same protocol field names.

## Write consistency

Sending a write and immediately reading the area snapshot back is not safe: the snapshot only reflects a write once the mower has confirmed it, and confirmation is not instantaneous. Two defenses exist, at different layers, deliberately kept independent:

- `OnAreaParameter` (above) closes most of the gap by applying the mower's own push within roughly 150ms of a write.
- `area_sensors._PendingAreaWrite` closes what remains: while a write is unconfirmed, the next write to the same area merges against the values just sent rather than the last confirmed snapshot, so a second fast change cannot silently revert the first. This does not depend on the push arriving — it is what protects a mower or firmware that never sends `onAreaParameter`.

Neither layer updates HA entity state optimistically; `_PendingAreaWrite` only affects what the *next write* merges against, not what is displayed.

## Availability

An area removed from the mower's inventory (per `getAreaSet`/`GetAreaSet`) is not deleted from Home Assistant — its four entities become unavailable (`EcovacsAreaNumber.available` combines device availability with area presence) rather than showing a stale or blank value. If the area reappears with the same `areaID`, the existing entities recover; nothing is re-created.

## Adding another mower model

A future model should be added only after its area protocol has been independently verified. In particular, verify:

1. that the model actually supports the area commands and the expected `areaID`/area inventory behavior;
2. the raw meaning and valid range of each parameter;
3. the mapping of those raw values to Home Assistant values and back;
4. that complete parameter writes are safe with the model's observed payload format; and
5. whether the model sends `onAreaParameter` at all — it has only been observed on `e4gqia`, firmware 1.11.31. `_PendingAreaWrite` does not depend on it, but a model that never sends it will feel slower to confirm writes.

Keep the raw protocol handling shared where the wire format is genuinely the same. Add model-specific value mappings in `area_sensors.py` only when they have been validated for that model, and gate the HA capability to that model. Do not assume that an identically named Ecovacs field has identical units, ranges, or semantics on another mower.

This keeps the area-parameter capability narrowly scoped: protocol parsing and raw state in `deebot_patch`, model-specific interpretation and HA exposure in `area_sensors.py`, with no unrelated restructuring of existing mower capabilities.

## Known gaps

`getAreaSet` (area names/inventory) has no confirmed unsolicited-push equivalent, unlike `getAreaParameter`. A capture of the phone app polling `getAreaSet` while idle showed only `iot/p2p/...` request/response traffic — the same "does not support p2p handling (yet)" path `setAreaParameter` hits — and no `iot/atr/onAreaSet`-style topic. This does not rule one out: the capture did not include an actual area rename or re-map, which is the case that would produce it if it exists. Area names and inventory therefore still only update through the polled refresh, reload, or a manual entity update.