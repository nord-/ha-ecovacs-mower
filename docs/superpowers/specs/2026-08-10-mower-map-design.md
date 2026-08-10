# Mower map design (issue #13)

Date: 2026-08-10
Status: approved design, pending implementation plan

## Goal

Show the mower's map in Home Assistant: lawn boundary, mowing coverage,
no-go zones, detected obstacles, the dock, the position track and the mower
itself. Rendered server-side as an SVG `image` entity — no custom Lovelace
card, no frontend resources.

This supersedes the assumption in #13/#18 that the map blobs were undecoded.
They were decoded on 2026-08-10 against a live GOAT O1200 (`2i0fns`,
firmware 1.11.31) edge-cutting session and verified pixel-for-pixel against
the official Ecovacs app. The full format is in the appendix — it is the
irreplaceable part of this document.

## Architecture

```
MQTT ──► deebot-client Device
           │
           ├─ onPos ──────────────► PositionsEvent (already handled upstream,
           │                        via the getPos legacy fallback)
           │
           └─ onMapTrack / onMI / onArI / onSpecialContour
                  │  new message classes in deebot_patch (OnChargeInfo
                  │  pattern, registered in apply())
                  ▼
              multipart reassembly + LZMA + chain-code decoding
                  │
                  ▼
              custom events carrying decoded geometry (polygons in mm)
                  │
                  ▼
          MowerMap model (per device) ──► image entity (lazy SVG)
                  │
                  ▼
          helpers.storage.Store (debounced persistence)
```

The patch layer remains the only module touching deebot-client internals.
Everything downstream of the custom events sees polygons in millimetres,
never the wire format.

## Components

### `deebot_patch/geometry.py` — pure decoding module

No deebot-client imports, no HA imports; runnable and testable on Windows.

- `decompress(data: str) -> bytes` — base64 + LZMA_ALONE with the 4 missing
  uncompressed-size bytes re-inserted at offset 8 (same scheme as upstream's
  `decompress_7z_base64_data`).
- **Multipart reassembly** — a pure fragment buffer (dict of
  `index -> fragment` per `batid`; returns decoded bytes when the LZMA
  attempt succeeds at `infoSize`, else `None`). Lives here, not in the
  message classes, so it is testable without deebot-client.
- Chain code → list of `(x, y)` mm points (see appendix for the encoding).
  Unknown digits (`0` occurs as a marker in traces) are skipped.
- Parsers for the four blob structures, returning plain dataclasses
  (boundary, zones, corridors, obstacles, no-go polygons, coverage lanes).
  Perimeter-trace records (onMapTrack kind 2) are parsed past but not
  returned — they are not rendered (see the layer list) and the live
  position track covers that visual.

### `deebot_patch/map_messages.py` — message classes and events

- Four message classes following the `OnChargeInfo` pattern: `OnMapTrack`,
  `OnMI`, `OnArI`, `OnSpecialContour`. Registered by `apply()` alongside the
  existing two. Exact name match in `MESSAGES` wins before the legacy
  fallback, so `map=None` on our device classes is irrelevant. The classes
  are thin wrappers: fragment buffering and decoding delegate to
  `geometry.py`.
- Custom event dataclasses carrying decoded geometry, one per category:
  `MowerMapInfoEvent` (boundary/zones/corridors from `onMI` + `onArI`),
  `MowerObstaclesEvent` (`onArI` section 3), `MowerCoverageEvent`
  (`onMapTrack`), `MowerNoGoZonesEvent` (`onSpecialContour`). All inherit
  `deebot_client.events.base.Event` — `EventBus` keys on the event type and
  `entity._subscribe` is typed `[EventT: Event]`.
- **Update semantics are per section, never wholesale.** `onMI` carries
  only the boundary, and both messages have "no update" variants (`onMI`
  idle `s1;0;`, `onArI` sections with payload `"0"`). Event fields are
  therefore `| None` where `None` means "unchanged"; producers set `None`
  for absent/`"0"` sections, and a message decoding to no geometry at all
  notifies nothing. `MowerMap` merges field-wise and ignores `None`.
- **Multipart:** large blobs arrive split (`index` 0, 1, …) with no
  total-parts field; the buffer is capped (~16 batids per message type,
  oldest evicted) so a lost part cannot leak memory.
- **New contract assumption:** custom event types are notified on
  deebot-client's `EventBus`, whose refresh-command lookup must degrade to a
  no-op for unknown event types. Guarded by a new test in
  `tests/deebot_patch/test_contract.py`.

### `map.py` — `MowerMap` model

One instance per device, created by `EcovacsController` alongside the device
list. Plain data holder — no HA or deebot coupling. The controller owns the
event subscriptions that feed it and subscribes **eagerly at setup** —
`EventBus.notify` drops events without subscribers, so lazy subscription
from the entity would lose data. The controller also owns the `Store`
(keeping `MowerMap` itself HA-free).

- **Geometry:** latest value per category, merged field-wise from events;
  `None` fields leave the previous value untouched (see update semantics
  above).
- **Coverage:** dict keyed `(zone, lane_row)` → segments. `onMapTrack`
  resends the same row with growing extents; replace-per-key gives
  last-state-wins and lets a re-mowed row overwrite the previous session.
- **Position track:** bounded deque fed by `PositionsEvent` (cap ~2000
  points; dense recent tail, older points thinned). Invalid points never
  reach the event — the library's `GetPos` filters them before notifying.
- **Heading:** latest `a` retained for the mower marker's direction.

### Persistence

Map data is push-only: without persistence the map is empty after a restart
until the next mowing session. Geometry + coverage are saved via
`helpers.storage.Store` (one file per config entry, keyed by device id),
owned by the controller, using `Store.async_delay_save` with ~30 s delay —
it flushes automatically at HA stop, so a clean restart loses nothing. The
position track is **not** persisted, and neither is the mower's position
(see marker rules). Corrupt store → log a warning, start with an empty map
— never `ConfigEntryError` for map data.

### `image.py` — the entity

New platform following the `lawn_mower.py` pattern: filtered on
`device_type is DeviceType.MOWER`, no capability to hang `capability_fn` on
(deliberate exception from the `ENTITY_DESCRIPTIONS` table, like
`lawn_mower`).

- `EcovacsMowerMapImage(EcovacsEntity, ImageEntity)`,
  `content_type = "image/svg+xml"`.
- SVG is built **lazily** in `async_image()` when the frontend asks; events
  only decide when the image counts as new: geometry events bump
  `image_last_updated` immediately, position events bump it throttled
  (max once per ~2 s).
- Rendering lives in a pure module `map_svg.py`: `MowerMap` in, SVG string
  out. Layers: filled boundary, coverage lanes, no-go zones, obstacles,
  corridors, position track, dock glyph, mower marker with heading arrow.
  Bounding box + margin as viewBox, y axis flipped.
- Empty map (fresh install, nothing persisted): `async_image()` returns a
  "no map data yet" placeholder so cards don't show a broken image.
- Availability follows `AvailabilityEvent` like every other platform.
- Translation/icon key `map` in `strings.json`, `translations/en.json` and
  `icons.json` (the platform tests guard both directions).

### Marker rules

The map frame's origin is the dock: the first valid `deebotPos` of a
session is exactly `(0, 0)` and `chargePos` is `invalid: 1` in all 1211
captured samples, so the origin is the only dock source on this hardware.
(`chargePos` is used as first-hand source if a device ever reports it
valid.)

- The dock glyph is always drawn at the dock position.
- The mower marker defaults to the dock at startup and follows `onPos` from
  the first message on. No last-known-position is persisted (KISS — decided
  2026-08-10): if the mower is out, the next `onPos` corrects the marker
  within seconds. The only stale case is a restart while the mower is
  paused out on the lawn, which self-corrects when it moves.

## Error handling

Map data is best effort; mower control is sacred.

- Decode failures (broken LZMA, unexpected structure, unknown section) →
  `HandlingResult.analyse()` + DEBUG log with `batid`, and that batid's
  buffer is cleared. A broken map message must never disturb `lawn_mower`
  control — no new `PatchContractError` surface in the runtime path.
- The only hard failure is the apply-time contract test (CI, not the user).

## Testing

- `tests/deebot_patch/test_geometry.py` — pure Python, runs on Windows:
  chain code → polygon (closure test with the real boundary), LZMA
  decompression, multipart reassembly, all four parsers. Fixtures are real
  payloads from the 2026-08-10 capture (local-frame mm coordinates; verify
  `batid`/`ts` leak nothing before committing).
- `tests/test_image.py` — `requires_ha`: entity created for MOWER devices,
  SVG contains expected layers, `image_last_updated` throttling, empty-map
  placeholder, marker-at-dock on `DOCKED` state.
- Existing translation/icon tests pick up the `map` key automatically.
- CI remains the source of truth; locally `-p no:homeassistant` runs the
  geometry part.

## Out of scope

- Interactive card, zone selection / "mow here" (future issue, builds on
  the same data).
- `device_tracker` (buried with #16 — no GNSS on this hardware).
- Blob formats from device classes other than the verified ones.

---

## Appendix: the decoded GOAT map protocol

Everything below was reverse-engineered 2026-08-10 from a live O1200
(`2i0fns`, fw 1.11.31) session and verified against the official app's map.

### Container format

All four messages carry `body.data` with:

| field | meaning |
| --- | --- |
| `mid` | map id |
| `batid` | batch id — groups multipart fragments |
| `index` | fragment index within the batch (`"0"`, `"1"`, …) |
| `infoSize` | decompressed size in bytes of the complete blob |
| `info` | base64 fragment (concatenate all fragments in index order first) |

Decoding: concatenate `info` fragments per `batid` sorted by `index`, then
base64-decode, insert 4 zero bytes at offset 8 (LZMA_ALONE header repair),
LZMA-decompress. Result is an ASCII JSON array of string records. There is
no total-fragments field; completion is detected by successful
decompression to `infoSize` bytes.

### Chain code

Geometry is `x,y;chaincode` where `x,y` is the absolute start in mm and the
chain code is a run-length-encoded sequence of king moves on a **50 mm**
grid:

- Digits `1`–`8` are compass directions **clockwise from north-west**:
  1=NW, 2=N, 3=NE, 4=E, 5=SE, 6=S, 7=SW, 8=W.
- `(n)` repeats the **immediately preceding digit** n times total
  (`56(27)` = one SE step, then 27 S steps).
- Digit `0` occurs as a marker in some traces and moves nothing.

Verified by the boundary polygon (2415 steps) closing with exactly zero
error — only one of the 16 possible direction mappings closes it, and that
mapping also matches the absolute lane coordinates.

### `onMI` — map info

`[["1","s1;1;<x,y>;<chaincode>"],["2","1"]]` — `s1` is the lawn boundary
outline. An idle variant `[["1","s1;0;"],["2","0"]]` carries no geometry.
Metadata includes `centerX`/`centerY` (map centre in mm).

### `onArI` — area info

Sections keyed by the second field:

| section | content |
| --- | --- |
| 1, 2 | mowing zones — `<id>;<x,y>;<chaincode>` polygons |
| 3 | **obstacles** — ids from 100 up, small chain-coded polygons, updated live as the mower detects/discards them |
| 4 | unobserved (empty in capture) |
| 5 | boundary outline (same polygon as `onMI` s1) |
| 6 | corridors/paths between zones |

A section with payload `"0"` instead of geometry means "no update".

### `onMapTrack` — coverage and traces

Records within the array:

- `<zone>;1;<row>;<x,y1>;<x,y2>[;…]` — coverage lane: mowed span(s) on a
  100 mm-spaced row; the same row is resent with growing extents while
  mowing. A record with just `<zone>;1;<row>` clears/skips that row.
- `<zone>;2;0;<x,y>;<chaincode>` — perimeter trace per zone.

Metadata `totalWidth`/`totalHeight`/`resolution` were all 0 in capture.

### `onSpecialContour` — no-go zones

Compressed like the rest, but the decoded content is plain coordinates
(no chain code):
`[["1","<id>","1","<ts1,ts2>","x1,y1;x2,y2;…;"], …]` — coordinates in mm,
trailing semicolon.

### `onPos` — position stream (already handled upstream)

`{"deebotPos": {"x","y","a","invalid"}, "chargePos": [{…,"invalid":1}]}` at
~2 Hz while active. `x`/`y` mm, `a` heading in degrees. `deebotPos` starts
at exactly `(0, 0)` when leaving the dock → **the map origin is the dock**.
`chargePos` was invalid in every captured sample. Handled by deebot-client
via the `getPos` legacy fallback (`_LEGACY_USE_GET_COMMAND`, not blocked by
`has_map=False`), notifying `PositionsEvent` — no patch needed.

### Reference tooling

The scripts that produced and verified this decoding (log capture →
decoded text, chain-code brute force, SVG rendering) live in the session
scratchpad; the geometry module in this design reimplements them properly.
Capture method: `ssh <ha-host> 'ha core logs -f'` with
`logger: deebot_client: debug`.
