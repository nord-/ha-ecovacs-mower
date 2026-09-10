# Area-parameter capability

This PR adds read/write support for per-area mowing parameters on the A1600 LiDAR Pro (`e4gqia`). The mower identifies an area by `areaID`; its friendly name and its parameter values arrive through separate protocol responses.

## Functional flow

At refresh time, the patch sends `getAreaParameter` and `getAreaSet`. The first supplies the raw parameters for each `areaID`; the second supplies the mower's current area inventory and names. The patch combines those responses into one authoritative per-device area snapshot and publishes a `MowerAreaEvent` when it changes.

The snapshot is kept in `deebot_patch/areas.py`, keyed by the device's event bus. This keeps protocol state separate from Home Assistant entities and, importantly, gives a write operation access to the complete last-known raw parameter set. A parameter entity must not overwrite the other fields with guessed defaults: writes send the complete `setAreaParameter` payload. If the required raw values are not known, the write is refused rather than risking unintended mower settings.

Area names and IDs are also kept in that same snapshot because `getAreaSet` is the authoritative source for the current area inventory. Areas no longer reported by the mower are removed from the snapshot.

## What `deebot_patch` owns

The patch layer owns only the missing Ecovacs protocol pieces and their raw values:

- `GetAreaParameter` parses `getAreaParameter` and stores `mowHeightLevel`, `cutMode`, `obstacleHeight`, and `angle` as raw values.
- `GetAreaSet` reassembles the mower's chunked `ar` response, decodes it using the existing deebot-client decompressor, and extracts the area ID and name.
- `SetAreaParameter` sends all four raw parameter fields for one `areaID`.
- The two read commands are registered together as the area refresh so the snapshot can be populated from both responses.

These commands live in `deebot_patch/commands.py`, alongside the other patched protocol commands. `deebot_patch` does not translate the values into units or user-facing meanings.

## What Home Assistant owns

`area_sensors.py` turns the raw `MowerArea` state into the four Home Assistant parameter entities. The A1600-specific mappings between raw protocol values and HA values are defined there because those mappings are part of the validated behavior of that mower model, not generic Ecovacs protocol knowledge.

For a write, HA converts the requested value back to its raw representation, combines it with the other three raw values from the authoritative snapshot, and sends one complete `SetAreaParameter`. The entity does not update its state optimistically; the mower must report the resulting raw values through the normal refresh path.

The capability is exposed only for `e4gqia`. Other supported mower models do not receive these entities merely because they use the same protocol field names.

## Adding another mower model

A future model should be added only after its area protocol has been independently verified. In particular, verify:

1. that the model actually supports the area commands and the expected `areaID`/area inventory behavior;
2. the raw meaning and valid range of each parameter;
3. the mapping of those raw values to Home Assistant values and back; and
4. that complete parameter writes are safe with the model's observed payload format.

Keep the raw protocol handling shared where the wire format is genuinely the same. Add model-specific value mappings in `area_sensors.py` only when they have been validated for that model, and gate the HA capability to that model. Do not assume that an identically named Ecovacs field has identical units, ranges, or semantics on another mower.

This keeps the area-parameter capability narrowly scoped: protocol parsing and raw state in `deebot_patch`, model-specific interpretation and HA exposure in `area_sensors.py`, with no unrelated restructuring of existing mower capabilities.