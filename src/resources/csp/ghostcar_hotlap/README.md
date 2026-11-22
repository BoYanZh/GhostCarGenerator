# GhostCarGenerator Hotlap app

A resident CSP **Lua app** (not a CSP new-mode) that draws a second, visual-only
car in any session, so the ghost can be checked from a plain **Hotlap** start.

It is a port of the `ghostcar_spike` new-mode probe:

| spike (`mode.lua`) | this app (`ghostcar_hotlap.lua`) |
| --- | --- |
| `extension/lua/new-modes/.../mode.lua` + `[RULES]` manifest | `apps/lua/ghostcar_hotlap/` + `[WINDOW_...]` manifest |
| `script.simUpdate(dt)` | `script.update(dt)` (runs every frame while the app is loaded) |
| `script.drawUI()` | `script.windowMain(dt)` (manifest `FUNCTION_MAIN`) |
| `__dirname` = mode folder | `__dirname` = app folder |

Everything else is the same proven scene recipe: `ac.findNodes('carsRoot:yes')`
→ `createBoundingSphereNode` → `setVirtualCarFlag(true)` → `loadKN5` of the
hard-coded Initial G GR86 Premium → per-frame `setPosition` / `setOrientation`.

## Install

1. Copy this folder to the AC installation:

   ```powershell
   $ac = 'E:\Games\Steam\steamapps\common\assettocorsa'
   Copy-Item -Recurse -Force '<repo>\src\resources\csp\ghostcar_hotlap' "$ac\apps\lua\ghostcar_hotlap"
   ```

2. The runtime timeline is **not** stored in this repo (2.2 MB generated data).
   Copy the already generated file next to the app script:

   ```powershell
   $src = "$ac\extension\lua\new-modes\ghostcargenerator-spike\ghost-runtime-v1.json"
   Copy-Item -Force $src "$ac\apps\lua\ghostcar_hotlap\ghost-runtime-v1.json"
   ```

   Expected source file: 2 220 695 bytes, `schema = ghost-runtime-v1`,
   `carId = ig_toyota_gr86_premium`,
   `trackId = thunderhill_raceway_park/threemilebypass`, 9131 frames,
   136.95 s. Any other file produced by `runtime_ghost.py` with the same schema
   works.

   Without the JSON the app still runs, but the ghost just sits 5 m to the side
   of the player (`mode = fallback`). That is deliberate: a second car must be
   visible even in an empty install, so a "nothing renders" failure cannot be
   confused with "no data".

3. In Content Manager: `Settings → Assetto Corsa → Apps` (or the in-game app
   strip) enable **GhostCarGenerator Hotlap**. The app shows one window
   (`GhostCarGenerator Hotlap: ...`); the second, hidden window is an info
   panel (`VISIBLE = 0`).

## Verification

Start **Hotlap** at `thunderhill_raceway_park / threemilebypass` with the
`ig_toyota_gr86_premium` and check the three signals:

1. **UI text** — the app window reads
   `GhostCarGenerator Hotlap: ACTIVE [runtime]` (or `[fallback]`).
   Failure text is `GhostCarGenerator Hotlap: ERROR: <reason>`.
   This is the OCR/pixel hook: assert on the `... Hotlap: ACTIVE` prefix.
2. **Second car visible** — the GR86 KN5 appears next to/aside the player car.
3. **Motion** — the ghost drives the exported lap after `ACTIVE`: distance to
   the player changes over time (see the log below). `fallback` mode instead
   stays a constant ~5 m offset.

### Machine-readable log

The app appends to

```
%USERPROFILE%\Documents\Assetto Corsa\logs\ghostcar_hotlap_verify.log
```

(`ac.FolderID.Logs`, with a `%USERPROFILE%\Documents\...` fallback path).

Line shapes:

```
INIT ok schema=ghost-runtime-v1 frames=9131 durationS=136.950 carId=... trackId=... log=...
INIT missing-runtime fallback=5m-follow player log=...
ACTIVE mode=runtime frames=9131 log=...
SESSION token=3
<time>,<ghostX>,<ghostY>,<ghostZ>,<playerX>,<playerY>,<playerZ>,<dist>
ERROR <stage> <message>
WARN init <message>
SHUTDOWN
```

* `time` is `ac.getSim().gameTime` in seconds.
* Coordinates are metres, 3 decimals; ghost coordinates are the values handed
  to `setPosition` (i.e. the applied pose, not a scene read-back).
* `dist` is the 3D Euclidean distance between ghost and player, metres.
* One data row per second of app time; the first row is written on the first
  frame after `ACTIVE`.

Useful assertions: `INIT ok` exists; `ACTIVE mode=runtime` exists; at least
~5 data rows with varying `ghostX/ghostZ` and non-zero, changing `dist`;
no `ERROR` line.

## Notes / deliberate choices

* `rotationRad` is `{pitch, yaw, roll}` — yaw is element 2, wrapped into
  `[-pi, pi]`, and world forward is `(-sin(yaw), 0, cos(yaw))`. This follows the
  verified conclusions in `docs/assetto-corsa-replay-notes.md` and is unchanged
  from the spike.
* The GR86 KN5 path is hard-coded so playback never depends on the model
  naming of the car being driven.
* Yaw is interpolated along the shortest arc between frames; the frame cursor
  advances monotonically instead of rescanning all 9131 frames per tick.
* The new-mode `[RULES]` (`BASE_MODE`, `PENALTIES`, physics flags) are **not**
  carried over; an app must not alter session rules.
* CSP APIs used, with evidence: `ac.findNodes`, `createBoundingSphereNode`,
  `setVirtualCarFlag`, `loadKN5`, `setPosition`, `setOrientation`, `dispose`
  (SDK `ac_wfx_impl/lib.lua`, `csp-traffic-tool/src/simulation/TrafficCar.lua`),
  `io.fileExists` / `io.load` / `io.open` / `io.createFileDir`
  (`apps/lua/ac-tracer/lib/core/files.lua`, `paths.lua`), `json.decode`
  (`csp-traffic-tool/tool.lua`), with `JSON.parse` as fallback.
* UNKNOWN: whether `script.update` keeps running when the app window is closed
  in-session. Installed apps (`map_display`, `ac-tracer`) update state from
  `script.update` unconditionally, which is the assumption here; if the ghost
  only moves while the window is open, move the update call into
  `script.windowMain` as a fallback (the window is visible by default).
* No native replay/template/private telemetry is distributed with this app.
