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

Scene recipe: `ac.findNodes('carsRoot:yes')` → `createBoundingSphereNode` →
`setVirtualCarFlag(true)` → `loadKN5` of the hard-coded Initial G GR86 Premium →
`setPosition` / `setOrientation` (yaw + pitch, Catmull-Rom cubic interpolation)
inside `script.update(dt)` locked to `sim.dt` (once per rendered frame, before
render passes / TAA motion vectors; `0` when paused).

**Status: v0.9 (in-game proven).**
- **v0.9** refined the ghost tint from cyan-blue to neutral silver-white
  (`GHOST_TINT_BODY = {0.78, 0.80, 0.82}`, charcoal tyres/trim, smoked glass)
  to match AC's built-in ghost look, and added `fit_pavement_offset()` in
  `ld_replay.py` to remove constant receiver/session GPS horizontal offsets
  against the track's KN5 `road-pit.gcsurface` pavement mesh (boosting Saurabh's
  `2:07.321` Thunderhill lap from 91.9% to 96.6% centerline-on-pavement with
  zero speed or lap-time distortion).
- **v0.8** fixed all four real-drive feedback items from v0.7:
  1. **True high transparency**: switched ghost meshes (after `ensureUniqueMaterials()`)
     to `SHADER = ksPerPixel` (`useDetail=0`, `ksAlphaRef=0`, `sunSpecular=0`,
     alpha-scaled specular/Fresnel), hid 223 interior clutter meshes (`int_*`) while
     translucent so alpha never stacks 4-6 layers deep inside the cabin, and lowered
     default `GHOST_ALPHA` to `0.25` (slider `0.05-1.00`, one-time `settingsVersion=8`
     migration).
  2. **Silky-smooth motion**: added `smooth_runtime_ghost()` (double periodic cubic
     Savitzky-Golay in `runtime_ghost.py`, reducing GPS 10/25 Hz stair-step `|dv|` p95
     88x from `10.66 m/s` to `0.12 m/s` and eliminating the `i=91` seam jump) +
     automatic Lua load-time jitter guard + Catmull-Rom cubic interpolation + unified
     `sim.dt` clock inside `script.update(dt)` + 4-wheel spin.
  3. **Instant pause freeze**: checks `sim.isPaused` and `sim.isInMainMenu` and uses
     `sim.dt`, freezing the ghost completely during Escape pause and pre-drive menus
     (`PAUSED state=1/0`).
  4. **Exact lap-time fidelity**: reads `lapTimeS` (`136.960 s` = `2:16.960`, matching
     the MoTeC LDX Fastest Lap 2 beacons `392.640 -> 529.600 s` to `0.000 s`) and
     interpolates seamlessly across the loop wrap `frames[count] -> frames[1]`.
     Verified across a 47-minute (18+ lap) live soak with `driftS=-0.0000`.

### v0.8 in-game proof (2026-11-23, Thunderhill threemilebypass, GR86)

- **Gates:** `INSTANCE writer`, `INIT ok app=0.8 frames=9131 durationS=136.960
  alpha=0.250 timeline=clean(p95=0.11)`, `GHOST alpha=0.250 mode=alpha meshes=317
  hiddenInt=223 shader=ok transparent=ok blend=ok shadows=ok tint=ok unique=ok`,
  `SMOOTHPATH pose=sim-dt event=script.update`, `CLOCK sim.dt`, zero `ERROR`.
- **Pause & timing proof:** `PAUSED state=1 ghostT=0.000` held throughout pre-drive
  menu; `PAUSED state=1 ghostT=130.174 -> PAUSED state=0 ghostT=130.174` and
  `PAUSED state=1 ghostT=1.030 -> PAUSED state=0 ghostT=1.030` froze with `0.000 s`
  drift; full-lap wrap `316.097 -> 453.115` and 47-minute soak (`2858 s`) maintained
  `driftS=-0.0000` (`1.0000x` exact match to `136.960 s`).
- **Visual proof:** live chase-cam screenshot at `Opacity: 0.25` shows a hollow,
  crystal-clear ice-blue ghost GR86 grounded on the curb with the blue/yellow curb
  and white track edge clearly visible right through the body and glass, while the
  player silver GR86 remains completely untouched.

### Prior milestones (v0.5–v0.7)
- **v0.5** moved pose application into the render callback with a phase-locked
  `os.preciseClock()` playback clock and speed-derived wheel roll.
- **v0.6** expanded the shader filter list to all 11 shaders used by the GR86
  KN5 (**317 / 317** meshes translucent, split body / dark-trim-tyre / glass
  ghost tint palette), applied timeline pitch and interpolated wheel steering,
  moved posing to `main.root.opaque`, disabled the red debug cross by default,
  and added persisted live UI controls (visibility, opacity, height offset,
  lap start offset, debug cross toggle).
- **v0.7** fixed the Hotlap UX gap where the ghost ran away during the pre-drive
  menu and missed the first flying-lap crossing (where AC keeps `lapCount=0`):
  - **Pre-launch ready hold**: holds the ghost ready right ahead of the player
    at `nearestTimelineTime(player) + leadS` (default `0.50 s`, ~11 m at
    `HOTLAP_START`) until the player accelerates (`>= 5 km/h`), then launches
    smoothly ahead.
  - **Lap 1 + every-lap S/F crossing sync**: triggers on both `lapCount`
    increments and forward `splinePosition` wraps (`>0.85 → <0.15`, reset- and
    pit-filtered, 5 s debounce).
  - **Live controls**: `Sync to my position` / `Sync to me` button, `Ghost lead`
    slider (`0.0–3.0 s`), `Reset defaults`, and settings gear button on the main
    window.

### v0.7 in-game proof (2026-11-23, Thunderhill threemilebypass, GR86 via Content Manager)

- **Gates (same session):** `INSTANCE writer`, `INIT ok app=0.7 frames=9131`,
  `NODE ok ... aabbMeshes=317 wheels=4`, `GHOST alpha=0.500 mode=alpha
  meshes=317 transparent=ok blend=ok shadows=ok tint=ok unique=ok` (100% mesh
  coverage across all 11 GR86 shaders), `SMOOTHPATH pose=render
  event=main.root.opaque`, `RENDERHOOK ok event=main.track.transparent`,
  `CLOCK sim`, `ACTIVE mode=runtime`, zero `ERROR`.
- **Visual & grounding proof (cockpit + chase cam screenshots):** with the
  ground-calibrated timeline (`-0.464 m` road datum), the ice-blue / smoked-dark
  translucent GR86 sits directly on the curb/tarmac (~11 m ahead of the player
  at `HOTLAP_START`, `LAPSYNC ready cursorS=121.235 distM=3.3 leadS=0.50`), no
  floating gap, no bright white tyres, no red debug cross, front wheels visibly
  steered into the right-hand corner, and the `GhostCarGenerator Hotlap Info`
  panel fits cleanly with all controls visible.
- **Motion & UI proof:** clicking `Sync to my position` logged
  `LAPSYNC snap ... cursorS=121.235 distM=3.4 leadS=0.50 reason=ui-settings`,
  launching the ghost smoothly down the straight (`dist` 20 → 121 m,
  `inFrustum01=1`, centered NDC) with `SMOOTH windowS=5.00 maxStepM=0.5853
  maxYawDeg=0.4 renderHz=113.3 tickHz=113.3 resets=1 avgStepM=0.2371`.

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

## Machine-readable log

Appended to

```
%USERPROFILE%\Documents\Assetto Corsa\logs\ghostcar_hotlap_verify.log
```

(`ac.FolderID.Logs`, with a `%USERPROFILE%\Documents\...` fallback path).

Line shapes (v0.5; v0.3/v0.4 lines are unchanged unless a new token is appended):

```
INIT ok app=0.5 schema=ghost-runtime-v1 frames=9131 durationS=136.950 carId=... trackId=... log=... alpha=0.500 syncLaps=1 syncOffsetS=0.000
INIT missing-runtime fallback=5m-follow player app=0.5 log=... alpha=0.500 syncLaps=1 syncOffsetS=0.000
INSTANCE writer id=<instanceId> gen=<n> backend=shared|file attempts=<n>
INSTANCE standby, another writer active id=<instanceId> gen=<n> ageS=<n> backend=shared|file
INSTANCE takeover id=<instanceId|free> genOld=<n> ageS=<n> gen=<n>
INSTANCE unavailable <store-write-failed|store-unverified>; running unguarded
NODE ok app=0.5 rootActive=1 rootChildren=1 modelEmpty=0 modelName=... modelActive=1 modelChildren=89 meshes=317 meshVisible=1 aabbSize=4.20x1.30x1.80 aabbMeshes=317 wheels=4 model=E:/.../toyota_gr86_premium.kn5
GHOST alpha=0.500 mode=alpha meshes=317 transparent=ok blend=ok shadows=ok tint=ok unique=ok
GHOST alpha=1.000 mode=opaque meshes=0
GHOST failed <reason>
RENDERHOOK ok event=main.track.transparent frames=1
RENDERHOOK unavailable render.on | RENDERHOOK failed <reason>
SMOOTHPATH pose=render|tick event=main.track.transparent windowS=5.0
SMOOTHPATH pose=tick reason=no-render-frames|render-hook-stalled
SMOOTH windowS=5.00 maxStepM=0.3819 maxYawDeg=0.1 renderHz=144.0 tickHz=60.0 resets=0
SMOOTH pose-error render-context
CLOCK sim | CLOCK app
ACTIVE mode=runtime frames=9131 hook=none|ok alpha=0.500 log=...
SESSION token=0 syncLaps=1
LAPSYNC baseline playerLap=<n> wallS=<gameTime>
LAPSYNC playerLap=<n> ghostRestarted t=0 wallS=<gameTime> cursorS=<s> offsetS=<s> jumps=<n>
LAPSYNC rebase playerLap=<n> wallS=<gameTime> reason=lapcount-decrease
LAPSYNC unavailable lapCount=nil free-run
LAPSYNC toggle syncLaps=1|0 wallS=<gameTime> cursorS=<s>
<time>,<ghostX>,<ghostY>,<ghostZ>,<playerX>,<playerY>,<playerZ>,<dist>,<ghostNdcX>,<ghostNdcY>,<inFrustum01>,<camDistM>,<ghostT>
WARN init <message>
ERROR <stage> <message>
SHUTDOWN role=writer|standby [release=ok]
```

v0.5 protocol deltas (motion smoothing):

* `SMOOTHPATH` is written once per `ACTIVE` and says where the pose comes from:
  `pose=render` means the render callback is moving the node (one sample per
  drawn frame — the intended path); `pose=tick` means the fallback (no
  `render.on`, or the callback never fired / stalled) and motion may step, with
  the cause in `reason=`.
* `SMOOTH` is written every 5 s of playback while the ghost is being posed, and
  is the smoothness proof:
  * `windowS` — length of the measurement window (playback seconds).
  * `maxStepM` — largest straight-line displacement in **metres** between two
    consecutive pose applications (i.e. between two rendered frames) in that
    window. For reference at the bundled timeline's 55 m/s peak this is ~0.38 m
    at 144 Hz and ~0.46 m at 120 Hz; a value near a timeline frame (0.83 m)
    means it is being posed at the app tick, and tens of metres means a
    teleport.
  * `maxYawDeg` — largest per-frame yaw change, in degrees. It stays in the
    low tenths; a value near 180 means a ±π seam snap.
  * `renderHz` / `tickHz` — pose applications per second vs app ticks per
    second over the window. `renderHz` must exceed `tickHz` on the real path.
  * `resets` — deliberate re-anchors in the window (first frame after `ACTIVE`,
    session reset, lap restart). These are excluded from `maxStepM`/`maxYawDeg`,
    so a restart shows up as `resets=1` and not as a huge `maxStepM`.
* `SMOOTH pose-error render-context` — one line, only if moving the node from the
  render callback throws on this CSP build. The app then logs
  `SMOOTHPATH pose=tick reason=no-render-frames` within ~2 s and keeps working at
  the older app-tick rate (smoothness gate fails, ghost still correct).
* No existing line changed shape; the 13-field data row is untouched.

v0.4 protocol deltas:

* `INIT` (both variants) now ends with `syncLaps=1|0` and `syncOffsetS=<s>`.
  `syncLaps` is the persisted toggle state (`1` = ON, the shipped default);
  `syncOffsetS` is `GHOST_START_OFFSET_S`.
* `SESSION` now also carries `syncLaps=1|0`. It is still written once per
  session-token change and still zeroes the playback cursor.
* `LAPSYNC` lines are new and are the only proof of lap sync:
  * `baseline` — first observation of `ac.getCar(0).lapCount`, or the first
    observation after a session reset / re-enable. **Never** a restart.
  * `playerLap=<n> ghostRestarted t=0` — one line per real start/finish
    crossing. `t=0` is the lap-start token (the ghost playback cursor is put
    back to the start of the lap); `cursorS=` is the actual timeline second now
    playing — `(duration − syncOffsetS) mod duration` — and `jumps=` is
    `newLapCount − previousLapCount` (normally `1`).
  * `rebase ... reason=lapcount-decrease` — the counter went **down**
    (session/car change, or a counter reset). Baseline is re-armed, no restart.
  * `unavailable lapCount=nil free-run` — the car object did not expose
    `lapCount`; sync is off for the session and the ghost free-runs.
  * `toggle syncLaps=1|0` — written by the settings-window checkbox the moment
    it is clicked.
* `LAPSYNC` restarts are **not** written for teleports/respawns, pit entry, or
  invalidated laps (policy below). Their absence at the right moments is part
  of the protocol.

v0.3 deltas (still current):

* `INIT` is written by the **elected writer** after the instance lease settles
  (≤ 1 s after load), not at script load. `alpha=` is the clamped `GHOST_ALPHA`.
  Everything before `alpha=` keeps the v0.2 grammar.
* A standby instance writes one `INSTANCE standby, another writer active ...`
  line and **no** `INIT`, `NODE`, `ACTIVE`, `SESSION` or data rows.
* `GHOST` reports the transparency actually applied: `mode=alpha` with
  `transparent=ok blend=ok tint=ok` is the expected line; `GHOST failed ...`
  means the app fell back to the opaque look and says why.
* Data rows are still exactly 13 comma-separated fields, so every v0.2 parser
  keeps working on the row prefix.

Data row fields (13, comma separated, `nan` = not available):

| # | field | meaning |
| --- | --- | --- |
| 1 | `time` | `ac.getSim().gameTime`, seconds |
| 2–4 | `ghostX/Y/Z` | pose handed to `setPosition`, metres |
| 5–7 | `playerX/Y/Z` | `ac.getCar(0).position`, metres |
| 8 | `dist` | 3D ghost→player distance, metres |
| 9–10 | `ghostNdcX/Y` | `render.projectPoint(ghostPos)` screen position in 0…1 UV; `inf` when the point projects to infinity (behind the camera), `nan` if the render hook never ran |
| 11 | `inFrustum01` | `ac.isVisibleInMainCamera(ghostPos, 4, false, true)` → `1`/`0` |
| 12 | `camDistM` | 3D ghost→`ac.getCameraPosition()` distance, metres |
| 13 | `ghostT` | playback time in the timeline, `0…136.95`, seconds |

The first eight fields are byte-compatible with v0.1, so older parsers keep
working on the prefix.

`ghostT` is the authoritative screenshot clock (field 13). Playback is advanced
from `ac.getSim().gameTime` deltas when available (`CLOCK sim`), which removes
the ~0.7 % drift v0.1 had from accumulating app frame `dt` (v0.1 lost about 1 s
of ghost time per 137 s lap).

## Motion smoothing (v0.5)

In-game report: "the ghost motion is choppy, visible stepping". Reproduced in
the code, not in the data.

### Root cause

The timeline sampling was already interpolated — nothing in v0.2–v0.4 rounded
positions or jumped whole frames. `sampleRuntimeFrame()` (v0.5 line 1190, this
file) picks the bracketing frames, lerps the position with
`k = (t − a.timeS) / span`, and interpolates yaw along the shortest arc into
`[−π, π]`. The defect was the **rate at which that sample was applied**:

* `applyPose()` (the only `setPosition` on the runtime path) had exactly one
  caller chain: `sampleRuntimeFrame()` → `updateGhost()` → `script.update()`.
  The `render.on('main.track.transparent')` callback only *read* the pose (it
  projected it for the oracle and drew the debug cross). So the node was
  re-posed once per app tick, and the on-screen motion was quantised to the
  app-tick rate.
* The per-tick amount was the `ac.getSim().gameTime` delta. That value is a
  millisecond snapshot of the game step (`ac.getSim().time`/1000, SDK
  `ac_apps/lib.lua:5098`) — the same reason the SDK ships a separate
  `os.preciseGameTime()` "at the time of the call" (lib.lua:4432) and
  `os.preciseClock()` (lib.lua:4427). Advancing playback from it means the pose
  advances in game-step chunks: 0 for a frame, then 2–3× the average for the
  next one.
* At the timeline's 55 m/s peak a 0.83 m timeline frame is 15 ms, so a 60 Hz app
  tick moves the ghost ~0.9 m per update — that is the stepping. The offline
  harness measures the v0.4 path at **maxStepM = 1.10 m** against its own
  144 Hz bound of 0.66 m.
* `steerWheels(steer, step * 5)` fed `rotate()` a constant **5 rad/s** wheel
  roll on the rear wheels: ~30× too slow at racing speed, and it inherited every
  app-tick delta irregularity, so the rims juddered on their own.

Two other suspects were ruled out with code evidence: there is **no**
interpolation bypass or snap branch on the runtime path (the fallback that snaps
is the "no timeline" 5 m-follow mode), and nothing rounds a position before
`setPosition` (the only `%` is on time). The single-writer lease was audited
again: the only overlap window is one heartbeat (≤1 s) at a simultaneous cold
start, it is logged twice (`INSTANCE writer` twice), and it ends in `standby` —
it cannot produce a persistent chop.

### Fix (smallest change that removes the stepping)

1. **Apply the pose once per drawn frame.** `updatePoseInRender()` (v0.5 line
   1286) is called from the existing render callback before the oracle samples,
   so the node moves on every frame the game draws. `state.renderPoseActive`
   records which path is live, and the app tick still poses whenever the hook
   is missing, has never fired, or has stalled (`SMOOTHPATH ... reason=`).
2. **Do not chase a quantised clock.** The render path advances a separate
   display time by `wall × playRate`, with `wall` from `os.preciseClock()` and
   `playRate` measured over `PLAYBACK_RATE_WINDOW_S = 0.5 s` (so a 0/0/2×
   gameTime pattern cannot become speed modulation). Phase error against
   `state.runtimeTime` is corrected only outside a 50 ms deadband, at ≤8 % of
   the frame's advance, and the display may never lead the authoritative phase
   (`LEAD_MAX_S = 0.03`) — which is also what makes a pause self-limiting
   without a drift-then-snap.
3. **Wheel roll from timeline speed.** `advanceWheels()` integrates
   `segmentSpeed × playbackStep / 0.32 m`, i.e. the physical angle for the
   distance actually travelled, and caps a single step at half a turn.
4. **Deliberate jumps stay jumps.** Session reset, first frame and lap restart
   set `state.reanchor`; the render clock snaps instead of sliding, and the
   discontinuity is excluded from `maxStepM` and counted in `resets=`.

Nothing about lap sync, heights or the timeline schema changed: `runtimeTime`
is still advanced/lap-restarted exactly as in v0.4, `ghostT` is still
`state.runtimeTime`, and the JSON format is untouched.

### In-game gate

Drive (or watch the ghost) for ≥20 s, then:

```powershell
$log = "$env:USERPROFILE\Documents\Assetto Corsa\logs\ghostcar_hotlap_verify.log"
Select-String -Path $log -Pattern '^SMOOTHPATH|^SMOOTH ' | Select-Object -Last 6
```

* `SMOOTHPATH pose=render` — the pose is coming from the render callback.
  `pose=tick reason=...` means the render hook did not fire and motion may still
  step; that line is the only acceptable failure mode.
* Every `SMOOTH` line has `maxStepM` consistent with the ghost's speed divided
  by `renderHz`: ~0.38 m at 55 m/s and 144 Hz, ~0.46 m at 120 Hz. **Pass rule:**
  `maxStepM` stays within a few percent of `speed / renderHz`, never approaches a
  15 ms timeline frame (0.83 m) at a render rate above 66 Hz, and never reaches
  metres. A `maxStepM` of ~1 m with `renderHz` ≈ 60 means the pose fell back to
  the app tick (check `SMOOTHPATH`).
* `renderHz` > `tickHz` (typically 100–165 vs 60). `resets` is `0` in steady
  state, `1` in the window that contains `ACTIVE`, and `1` in the window that
  contains a `LAPSYNC ... ghostRestarted`.
* `maxYawDeg` stays in the low tenths; anything near 180 is a ±π seam snap and
  a regression.
* If `SMOOTH pose-error render-context` appears, scene mutation is not allowed
  from the render callback on this CSP build; the app has already fallen back to
  app-tick posing (`SMOOTHPATH pose=tick reason=no-render-frames`). Report that
  line instead of the smoothness numbers: it is an environment limit, not a
  timeline/data problem.

## Lap synchronisation (v0.4)

The ghost is a *lap* reference, not a wall-clock recording: its timeline restarts
whenever the **player** starts a new lap, so both cars cross the start/finish
line together on every lap instead of the ghost free-running its 136.95 s loop
from session `gameTime`.

### What "player lap boundary" means (API citations)

The boundary is detected from the player car's completed-lap counter:

```lua
local player = ac.getCar(0)
if player.lapCount > baseline then  -- one real start/finish crossing
  -- restart ghost playback
  baseline = player.lapCount
end
```

This mirrors the installed `ac-tracer` app, which is the reference
implementation on this machine:

* `apps/lua/ac-tracer/lib/core/state.lua:1263` —
  `if car.lapCount > state.lapNumber then` treats the increment as "lap
  completed", finalises the lap with `car.previousLapTimeMs` (line 1272) and
  validity with `car.isLastLapValid` (line 1271), then re-baselines
  `state.lapNumber = car.lapCount` (line 1322).
* `apps/lua/ac-tracer/lib/windows/delta_bar.lua:79` — the UI keys off the same
  counter (`if car.lapCount ~= prevLapCount then`).
* The other fields ac-tracer reads on the same `ac.StateCar` object, and why
  this app does **not** need them: `car.lapTimeMs` / `car.previousLapTimeMs`
  (lap timing), `car.splinePosition` (used there for teleport heuristics),
  `car.isInPitlane` / `car.isInPit`, `car.resetCounter`, and
  `ac.onCarJumped(0, fn)` (`state.lua:1225`). Only `lapCount` is a lap boundary;
  the rest describe teleports, which must not restart the ghost (below).

`script.update` is the only clock that matters: the counter is read once per
frame, before the playback clock advances, so a restart is already visible in
the pose written that frame.

### Restart semantics

* Restart == the existing loop-wrap path: `runtimeTime` is set to a time inside
  `[0, duration)` and the frame cursor is re-armed to frame 1. Nothing else in
  the playback math changes, so `ghostT` (field 13) stays exact: the next row is
  `cursorS + elapsed`, never a stale index scan.
* The cursor is `(duration − GHOST_START_OFFSET_S) mod duration`. With the
  default `0.0` that is frame 0, i.e. the documented `t=0` restart.
* One line per crossing:
  `LAPSYNC playerLap=<n> ghostRestarted t=0 wallS=<gameTime> cursorS=<s>
  offsetS=<s> jumps=<n>`.
* A single frame that sees the counter jump by more than one (e.g. a teleport
  across the line while the counter catches up) is **one** restart with
  `jumps=<n>`; the intermediate crossings cannot be separated, and firing the
  restart twice in the same frame would be a visible double jump.

### Toggle

`Sync ghost to my laps` in the settings window, **default ON, persisted**
through `ac.storage({ syncLaps = true }, 'ghostcar_hotlap/')` — the SDK's
auto-saving script store used by the installed ac-tracer
(`apps/lua/ac-tracer/lib/core/settings.lua:50`, pattern at lines 267-268 for the
checkbox flip). CSP writes it under
`Documents/Assetto Corsa/cfg/extension/state/lua/` next to the other app state
files; deleting that store entry restores the ON default. If `ac.storage` is
missing, the toggle still works for the running session but is not persisted.

* ON (default): restart on every `lapCount` increment; log `syncLaps=1`.
* OFF: **exactly v0.3 free-run** — no restart, no baseline tracking,
  `LAPSYNC` stays silent, and the timeline keeps advancing from session start.
  Log `syncLaps=0` at INIT, and `LAPSYNC toggle syncLaps=0|1` on each click.
  Re-enabling rebases the counter (`LAPSYNC baseline ...`) so a crossing that
  happened while OFF does not retro-fire.

### Policy for the awkward cases (and why)

| event | restart? | reason |
| --- | --- | --- |
| start/finish crossing (`lapCount` +1) | **yes, once** | the only real lap boundary; both cars then cross together |
| teleport: respawn, ESC → pits, back to pits (`ac.onCarJumped`, `resetCounter`, big `splinePosition` jump) | **no** | a teleport does not change `lapCount` and does not put the player at t=0 of a lap. Re-zeroing the ghost there would make it lead/lag by the distance already driven this lap, and pit exits would silently shift the anchor off the timing line. The ghost is anchored to the line, not to the player's position. |
| invalidated lap (track limits, cut, contact) | **yes, once** | the ghost is a visual reference line, not a referee. A cut lap still crosses the line; the next lap must start with both cars together, so validity (`car.isLastLapValid`) is deliberately ignored |
| session restart (session token change: `sim.currentSessionIndex`) | cursor already zeroed by `resetForSession()`; **no** `LAPSYNC` restart at that moment | the existing reset path re-creates the node and zeroes the cursor. v0.4 additionally clears the lap baseline and restart counter there, so the first crossing of the new session re-syncs once instead of comparing against a stale lap number (which would either double-fire or miss the first crossing) |
| counter goes **backwards** without a session change | no restart, one `LAPSYNC rebase ... reason=lapcount-decrease` | defensive: AC can reset the counter on car/session swaps that do not bump the session index; the next forward increment is then the real crossing |
| `lapCount` missing on the car object | no restart, one `LAPSYNC unavailable ... free-run` | degrade to v0.3 rather than erroring; logged once, not per frame |

### Aligning frame 0 to the start line

The restart is only as good as the timeline's phase: if frame 0 does not sit on
the start/finish line, both cars still restart together but the ghost will
constantly lead or lag the player by that fixed distance. This was checked
offline rather than assumed:

* AC counts laps on the AI spline: `splinePosition` 0 (and `lapCount`
  increments) at the **first point of `content/tracks/<track>/<layout>/ai/fast_lane.ai`**.
* `threemilebypass` `fast_lane.ai` (installed): 2922 points, 4544.6 m closed
  loop, first point `(x,z) = (68.52, 164.60)`. The repo's own calibration
  package (`src/ghost_car_generator/resources/tracks/assetto_corsa/
  thunderhill_raceway_park/threemilebypass/calibration.json`) records
  `startLineAc = (69.57, 166.18)` — 1.9 m from that point, an independent
  corroboration within the track's own fit error.
* The bundled `ghost-runtime-v1.json` (9131 frames, 136.95 s, 15 ms) was
  projected onto that spline: **frame 0 sits 66.3 m past the line** (~1.41 s at
  the 53.0 m/s speed at frame 0), and the timeline reaches the line at
  `t ≈ 135.54 s` before wrapping to frame 0 again.

So frame 0 is **not** on the line for the bundled timeline, by a constant that
comes from how the source lap was cut. The tunable exists for exactly that:

```lua
-- ghostcar_hotlap.lua
local GHOST_START_OFFSET_S = 0.0
```

`GHOST_START_OFFSET_S` is *how far the timeline runs ahead of the player's lap,
in seconds*: playback starts at `(duration − offset)` at a crossing. `0.0` is the
only safe default because the app must work with any `ghost-runtime-v1.json`
(each file has its own phase). For the bundled file the offline value is
**`1.41`** (`cursorS` becomes `135.54`); it is documented rather than baked in so
the app stays correct for a timeline cut crossing-to-crossing.

**In-game calibration (3 steps, driver-verified):**

1. Leave `GHOST_START_OFFSET_S = 0.0`, sync ON, and drive two laps. At the
   second crossing note whether the ghost is ahead or behind and by roughly how
   many metres `D` (pit wall / braking board are good references).
2. Convert to seconds: `offset ≈ D / (speed_at_line_kmh / 3.6)`. If the ghost is
   **ahead**, increase `GHOST_START_OFFSET_S` by that amount; if it is
   **behind**, decrease it. Reload the app (edit `syncOffsetS` in the new
   `INIT` line to confirm the value took effect, and `cursorS` on the next
   `LAPSYNC` line to confirm the arithmetic).
3. Repeat once with a ±0.05 s nudge. A correct value shows the ghost's nose
   arriving at the line with the player's on successive laps, and the
   `LAPSYNC ... cursorS=` second barely changing between laps (it is a constant
   of the file, not of your driving).

A different timeline file needs its own value; re-measure with the same steps.
If the source lap was cut crossing-to-crossing from the same `fast_lane.ai`
start point, the correct value is `0.0` and nothing needs changing.

### Offline harness

v0.4 ships with no in-repo test for the Lua app (like v0.3), but the sync logic
was exercised by an offline harness in temp files only: it stubs the CSP API
(car, sim, scene nodes, `ac.storage`, `ui.checkbox`, JSON decoder), writes a
synthetic 20 s timeline, drives `script.update(dt)` and asserts on the app's own
log. 54 assertions cover: INIT/`syncLaps` state, baseline line, one restart per
crossing, no double fire while the counter is unchanged, `jumps=` on a skipped
count, no restart on teleport, restart on invalidated lap, session-reset rebase
with exactly one later restart, toggle OFF staying free-run and re-enable
rebasing, non-zero `GHOST_START_OFFSET_S` cursor arithmetic
(`cursorS = duration − offset`), and fallback mode staying silent. `luac -p` is
clean on the shipped script.

v0.5 adds a second offline harness (same temp-file method, no repo test) that
targets the smoothing path: it loads the **real** script against the stubbed
API, writes a 13.5 s / 900-frame synthetic timeline (15..55 m/s, yaw that
deliberately crosses the ±π seam, sinusoid steering), and then drives the app
with `script.update` at 30/60 Hz while invoking the captured render callback at
120/144 Hz, recording every `setPosition` / `setOrientation` the app hands to
the node. 27 assertions:

| scenario | what it proves | result |
| --- | --- | --- |
| 144 Hz render / 60 Hz tick | max per-frame displacement within `speed/renderHz` | `maxStepM=0.3819` m (bound 0.66) |
| 120 Hz render / 60 Hz tick | same at a second rate | `0.4572` m (bound 0.78) |
| 144 Hz / 60 Hz, `sim.gameTime` quantised to 30 Hz | a coarse sim clock cannot quantise motion | `0.3820` m, `p99=0.3819` |
| 144 Hz render / **30 Hz** tick | the app-tick rate no longer matters | `0.3811` m |
| no `render.on` (v0.4 path) | the old per-tick posing steps | `1.1033` m → fails the same bound |
| yaw across the ±π seam | no seam snap | `maxYawDeg=0.28` |
| pause (gameTime frozen 0.4 s) | no lurch, no catch-up jump | <0.6 m total |
| lap restart | exactly one re-anchor, smooth after | 1 step, tail within bound |
| wheel roll | integral of speed/R, not constant 5 rad/s | ratio 1.003 vs path/radius |

Plus grammar checks on the app's own log (`SMOOTHPATH pose=render`,
`SMOOTH ... maxStepM/renderHz/tickHz/resets`, no `nan`/`inf`).

## Verification

**Start with the v0.5 smoothing gate** — it needs no driving, only watching the
ghost for ~20 s: `SMOOTHPATH pose=render` + `SMOOTH maxStepM` ≈ speed /
`renderHz` (see "Motion smoothing (v0.5)" → "In-game gate"). The v0.4 section
below is the lap-sync procedure and is additive to it.

### v0.4 protocol (driver-verified)

Sync cannot be validated without **driving a lap**, so this section is the
driver's procedure, not a screenshot gate. It assumes the v0.5 gate above and
the v0.3 gates below already pass (one writer, `GHOST ... mode=alpha`,
`ACTIVE mode=runtime`).

**Step 0 — pre-flight (no driving needed).**

```powershell
$log = "$env:USERPROFILE\Documents\Assetto Corsa\logs\ghostcar_hotlap_verify.log"
Select-String -Path $log -Pattern '^INIT ok|^SESSION |^LAPSYNC ' | Select-Object -Last 8
```

* `INIT ok app=0.5 ... syncLaps=1 syncOffsetS=0.000` — sync enabled at the
  shipped defaults. `syncLaps=0` means the toggle was persisted OFF; turn it on
  in the settings window and check for `LAPSYNC toggle syncLaps=1`.
* exactly one `LAPSYNC baseline playerLap=<n> ...` appears shortly after
  `ACTIVE`, with no `ghostRestarted` yet.
* no `ERROR`, and (if you want the bundled file aligned immediately) set
  `GHOST_START_OFFSET_S = 1.41` before this run, per "Aligning frame 0".

**Step 1 — drive one lap, then cross the line (the actual sync test).**

Drive a full lap from the pits and cross the start/finish line. Immediately
after the crossing the log must gain **exactly one** line for that crossing:

```
LAPSYNC playerLap=<n+1> ghostRestarted t=0 wallS=<gameTime> cursorS=<s> offsetS=<s> jumps=1
```

What to look at on track *at* the crossing: the ghost is at the same point of
its lap as the player — i.e. it is crossing the line with you (within
`GHOST_START_OFFSET_S` if you have not calibrated), not 60–70 m down the road.
Then drive the next lap: at the *next* crossing there must be exactly one new
`LAPSYNC` line with `playerLap` incremented by exactly 1, and the ghost must
again be with you at the line. That is the proof: two consecutive crossings,
one line each, ghost together both times.

**Step 2 — negative controls (same session).**

| action | expected log | expected on track |
| --- | --- | --- |
| ESC → back to pits (respawn, no lap counted) | **no** new `LAPSYNC` line at all | ghost keeps playing its current lap (it does not jump back to t=0) |
| invalidated lap (cut the chicane), then cross | exactly one `LAPSYNC ... ghostRestarted` | both cars still start the next lap together |
| checkbox "Sync ghost to my laps" off, drive and cross | `LAPSYNC toggle syncLaps=0`, then no restarts; `ghostT` keeps rising across the crossing | v0.3 free-run: the ghost drifts by roughly the lap-time difference per lap |
| re-enable the toggle | `LAPSYNC toggle syncLaps=1` + `LAPSYNC baseline playerLap=<n>` (no restart) | next crossing re-syncs once |
| restart the session (ESC → restart) | `SESSION token=<new> syncLaps=1` then `LAPSYNC baseline ...`; no restart at the reset itself | first crossing of the new session re-syncs once, not twice |

**Alignment check for the record:** with the calibrated offset, at the crossing
compare the ghost's nose with the line (or with your car) — the residual should
be well under one car length. If it is a constant several car lengths, the file
phase differs from your `fast_lane.ai` cut: re-run the 3-step calibration above.

**Expected summary line count after a clean 3-lap run with one pit teleport:**
three `LAPSYNC ... ghostRestarted` lines (one per crossing), zero lines for the
teleport, and one `LAPSYNC baseline` at the start.

### v0.3 protocol (historical; still the transparency/single-writer gate)

> The sample lines below say `app=0.3`. v0.4 wrote `app=0.4`, v0.5 writes `app=0.5`; v0.4 appended
> `syncLaps=`/`syncOffsetS=` to `INIT` and `syncLaps=` to `SESSION`; every gate
> in this section is otherwise unchanged and still required.


Same session/car/track as v0.2 (`thunderhill_raceway_park / threemilebypass`,
`ig_toyota_gr86_premium`, player stationary in the pit box). Steps 2–4 of the
v0.2 protocol (pass window, screenshot clock, framing) are unchanged; the
deltas are the gates and the appearance.

1. **One writer, transparency applied**

   ```powershell
   $log = "$env:USERPROFILE\Documents\Assetto Corsa\logs\ghostcar_hotlap_verify.log"
   Select-String -Path $log -Pattern '^INSTANCE |^INIT ok|^GHOST |^NODE ok|^ACTIVE |^ERROR' |
     Select-Object -Last 12
   ```

   Expected, in this order:

   * `INSTANCE writer id=... gen=... backend=shared|file` — exactly one for the
     session's lease epoch;
   * `INIT ok app=0.3 ... alpha=0.500` — `alpha` is the clamped `GHOST_ALPHA`;
   * `NODE ok` — unchanged v0.2 gate (`modelEmpty=0`, `aabbMeshes>0`,
     `wheels=4`);
   * `GHOST alpha=0.500 mode=alpha meshes=<n> transparent=ok blend=ok
     shadows=ok tint=ok unique=ok` — the transparency mechanism ran on the
     ghost's own materials. `GHOST failed ...` means it did **not** run;
     `blend=failed:...`/`transparent=failed:...` means the ghost will look
     opaque;
   * `ACTIVE mode=runtime ... alpha=0.500` and **no** `ERROR`.

2. **Duplicate-instance gate (the v0.2 bug)**

   Trigger it the same way it was seen: restart the session / re-add the app
   while a first copy is running. Then:

   ```powershell
   Select-String -Path $log -Pattern '^INIT ok|^INSTANCE '
   Select-String -Path $log -Pattern '^INSTANCE standby'
   ```

   * exactly one `INSTANCE writer` line per lease epoch (a `takeover` line is
     legitimate after the old writer shut down or went silent for 8 s);
   * the duplicate logs one `INSTANCE standby, another writer active ...` and
     **no** `INIT ok`/`ACTIVE`/data rows of its own;
   * data rows stay a single stream: `ghostT` advances monotonically inside one
     lap (no two row populations ~19 s apart in the same wall-clock second).

3. **What the ghost must look like**

   * same second-row-gated screenshot rule as v0.2 (`ghostT mod 136.95` in the
     pass windows);
   * the second GR86 is **semi-transparent** at ~50 %: the pit surface is
     visible through the body while the ghost's shading/normals stay readable;
     the red `render.debugCross` still marks the pose (it is drawn from the
     render hook, independent of the ghost materials);
   * the body is a flat cool-grey tint (`GHOST_TINT`), not the car's livery.
     That is the deliberate trade-off (see "How v0.3 draws transparency");
   * the app window shows `GhostCarGenerator Hotlap: ACTIVE [runtime]` and an
     `alpha 0.50 mode alpha  meshes <n>` line. A duplicate shows
     `GhostCarGenerator Hotlap: STANDBY (another writer active)`.

4. **Transparency negative controls** (how to tell the failure modes apart)

   | log | scene | conclusion |
   | --- | --- | --- |
   | `GHOST ... blend=ok transparent=ok tint=ok` | car still fully opaque | the material shader ignores `txDiffuse` alpha; note the shader name in `NODE` and this is a real API limit, not a gate failure |
   | `GHOST failed <reason>` | car opaque (v0.2 look) | the material edit did not run at all; the reason is in the line and in `ac.warn` |
   | no `GHOST` line | car missing/opaque | node creation or `findMeshes` failed; check `NODE`/`ERROR` first |
   | `GHOST ... transparent=failed` | blend ordering wrong (self-occlusion) | the transparent-pass flag did not apply; alpha may still show but sorting is wrong |

### v0.2 protocol (historical; still the opaque-render gate)

Start **Hotlap** at `thunderhill_raceway_park / threemilebypass` with the
`ig_toyota_gr86_premium`, leave the player car stationary in the pit box, and
enable the app. Then:

1. **App loaded / data ok**

   ```powershell
   $log = "$env:USERPROFILE\Documents\Assetto Corsa\logs\ghostcar_hotlap_verify.log"
   Select-String -Path $log -Pattern 'INIT ok|NODE ok|ACTIVE mode=runtime|ERROR'
   ```

   * `INIT ok app=0.2 ... frames=9131 durationS=136.950` — timeline loaded.
     (On a v0.3 build the same line reads `app=0.3 ... alpha=0.500`; the gate
     is unchanged.)
   * `NODE ok ...` — the scene node exists **and geometry is attached**:
     `rootChildren=1`, `modelChildren>0`, `meshes>0`, `meshVisible=1`,
     `aabbSize` of car size (a few metres, e.g. `4.20x1.30x1.80`),
     `aabbMeshes>0`, `wheels=4`. `modelEmpty=1`, `meshes=` absent or
     `aabbMeshes=0` means the KN5 did not attach — that is a render-path
     failure visible without any camera.
   * `RENDERHOOK ok event=main.track.transparent` — the render-context hook
     fired, so `ghostNdcX/Y` will be real numbers. `RENDERHOOK unavailable` or
     `RENDERHOOK failed` means the oracle degrades to `nan` NDC and only
     `inFrustum01`/`camDistM` remain trustworthy.
   * `ACTIVE mode=runtime` and **no** `ERROR` line.

2. **Ghost passes the stationary player once per 136.95 s.** From the v0.1 log
   (player at `(-2.05, -0.66, -384.30)`), the ghost crosses the pit area at
   `ghostT ≈ 105.5 s` and `ghostT ≈ 119.35 s`, i.e. every loop at

   ```
   ghostT mod 136.95 ∈ [117.4, 121.4]   -- pass at 119.35 s (min distance 3.34 m)
   ghostT mod 136.95 ∈ [103.5, 107.5]   -- pass at 105.5 s  (8.3 m)
   ```

   Screenshot rule: **take the shot in the same second as the newest data row
   whose `ghostT` falls in one of those windows** (`ghostT mod 136.95 == 119.35`
   is the exact instant; the 1 Hz row before it is ~8 m away because sampling
   misses the minimum). Do not use the lap timer — v0.1 did and was off by more
   than 200 s.

   ```powershell
   Get-Content $log | Where-Object { $_ -match '^[\d.]+,' } |
     ForEach-Object { $f = $_ -split ','; [pscustomobject]@{
       row = $_; ghostT = [double]$f[12]; dist = [double]$f[7] } } |
     Where-Object { $_.ghostT -gt 117.4 -and $_.ghostT -lt 121.4 }
   ```

3. **Camera framing is proven by the row, not by the eye.** At the shot row
   require `inFrustum01=1` and `ghostNdcX/Y` in `0.05…0.95`. If the pass row
   shows `inFrustum01=0`, the camera was not looking at the ghost (framing bug,
   not a rendering bug) — re-frame and take the next loop. For a deterministic
   shot, use the free camera (F5) or a replay-friendly fixed camera aimed at the
   ghost coordinates printed in the row; the pass happens near
   `(-8.8, 0.0, -389.2)` (6.5 m left and ~5 m behind the player car), so a
   chase camera looking forward often has the ghost **behind the camera**.

4. **What must be visible** in that same-second screenshot:

   * a second grey `INITIAL G` GR86 at ~4–8 m from the player car, roughly the
     size of the player car, sitting on the pit-lane surface;
   * a red debug cross (`render.debugCross`, radius 2 m) at the same spot —
     disable with `ORACLE_DEBUG_CROSS = false` once verified;
   * the app window (`GhostCarGenerator Hotlap: ACTIVE [runtime]`, second line
     `oracle ndc <x> <y> hook ok/<n>`).

   Interpretation matrix:

   | car | cross | conclusion |
   | --- | --- | --- |
   | visible | visible | render path proven end-to-end |
   | missing | visible | node/pose are right, the KN5 attachment or materials are not drawn → inspect `NODE ok` (`modelChildren`, `aabbMeshes`) and the loaded path |
   | missing | missing | pose/coordinates or camera framing is wrong; `inFrustum01` and `camDistM` in the same row say which |
   | visible | missing | the render hook never ran (`RENDERHOOK` absent) |

### v0.2 in-game proof (2026-11-22, Thunderhill threemilebypass, GR86)

Render path **proven end-to-end** (car visible + cross visible):

* Gates, same session: `INIT ok app=0.2`, `NODE ok` (`modelEmpty=0`,
  `modelActive=1`, `aabbMeshes=317`, `wheels=4`), `RENDERHOOK ok
  event=main.track.transparent`, `CLOCK sim`, `ACTIVE mode=runtime`, zero
  `ERROR`.
* Two independent conditional screenshots (log-gated burst, chase cam,
  player stationary) each show a second light-grey GR86 on the recorded
  line with the red 2 m `ORACLE_DEBUG_CROSS` overlapping it exactly at the
  logged ghost pose. The car sits at different track positions in the two
  shots, following the timeline direction.
* The oracle rows bracketing the shots agree (`inFrustum01=1`,
  centered NDC, `dist` 20-65 m, ghost height within ~1 m of player
  height — not buried).


Caveats carried forward: a duplicate app instance was observed writing
interleaved rows (second `INIT ok` after a session restart while the first
instance kept running); v0.3 should add a single-instance guard. The
v0.2 build renders opaque; transparency is a separate v0.3 item.

Both caveats are addressed in v0.3; see the next two sections. The proof above
is kept verbatim because it is the evidence for the render path the ghost still
uses.

### How v0.3 draws transparency

There is no per-material opacity call in the app SDK: `ac.SceneReference`
exposes `setMaterialProperty` (`ac_apps/lib.lua:9639`), `setMaterialTexture`
(`:9675`), `setTransparent` (`:9870`) and `setBlendMode` (`:9874`), but nothing
like `setOpacity`. So alpha has to be carried by the diffuse texture slot,
which is exactly how AC's own transparent materials work (windscreens keep
their opacity in `txDiffuse`; the SDK's `setMaterialTexture` example passes an
`rgbm` color for a solid texture, `lib.lua:9641-9650`, and `rgbm`'s fourth
component is documented as a multiplier usable for alpha, `lib.lua:8872`).

The app therefore does, once per node creation, on the ghost's mesh reference
only:

1. `findMeshes()` with `key:value` shader filters over the ghost KN5's own meshes (bare `'*'` matches nothing — found empirically);
2. `ensureUniqueMaterials()` — **the scope guarantee**. `loadKN5` copies the
   model from a shared pool ("sharing of resources such as vertex buffers",
   `lib.lua:9797-9800`), so a pooled material could otherwise be shared with
   the player's identical GR86. The SDK documents `ensureUniqueMaterials()` as
   making materials unique "relative to the rest of the scene"
   (`lib.lua:9677-9681`), which is precisely "never touch the player car or
   other cars";
3. `setTransparent(true)` — mark the meshes for the transparent pass; the SDK
   documents the flag as "drawn last" (`lib.lua:10064`). This is a *render-pass
   flag*, not a visibility flag;
4. `setBlendMode(render.BlendMode.AlphaBlend)` — the enum used by installed
   scripts in a render callback (`apps/lua/ac-tracer/lib/reference_line.lua:302`);
5. `setShadows(false)` — a 50 % ghost must not cast a 100 % shadow;
6. `setMaterialTexture('txDiffuse', rgbm(tintR, tintG, tintB, GHOST_ALPHA))` —
   a solid translucent texel replaces the livery. The body keeps its normals,
   so shape and lighting remain readable, but the paint/livery is gone. That is
   the deliberate v0.3 look.

Consequences and limits, honestly stated:

* Alpha is only as good as the shader's alpha output. The GR86 KN5 uses
  `ksPerPixelNM`, `ksPerPixelMultiMap*`, `ksPerPixelAT*`, `ksPerPixelReflection`,
  `ksWindscreen`, `ksTyres`, `ksBrakeDisc`. The KS family samples `txDiffuse`
  and its alpha-test variants clip on it, so the texel alpha is the expected
  path; a custom shader that hard-codes alpha to 1 would stay opaque. The
  `GHOST` line proves the material edit ran, not the GPU result — the in-game
  screenshot is still the final oracle.
* `GHOST_ALPHA = 1.0` skips the whole path (`GHOST alpha=1.000 mode=opaque`) and
  restores the v0.2 appearance.
* Visibility interaction, resolved: `setTransparent(true)` does **not** hide or
  show anything (`isTransparent`, `lib.lua:10067`, is a pass/sorting flag), and
  the existing explicit `setVisible(true, false)` (`lib.lua:9854`, documented
  no-op when already visible) is untouched. The red `render.debugCross` is
  drawn from the render hook independent of the ghost materials, so it remains
  visible through the translucent body.
* No depth-mode change is made. The ghost relies on the same transparent-pass
  behaviour AC uses for windscreens; if self-occlusion looks wrong in-game, the
  next knob is `setDepthMode(render.DepthMode.ReadOnlyLessEqual)`, not a
  visibility change.

### How v0.3 elects a single writer

`ac.store()` / `ac.load()` are the SDK's session-shared Lua/Python storage,
documented as "a way for different scripts to exchange data"
(`ac_apps/lib.lua:3953-3966`); installed apps use the same pair
(`apps/lua/SetupExchange/SetupExchange.lua:1023`, `ac.load`;
`apps/lua-off/GT7HUD/helpers/MappedConfig.lua:13`, `ac.store`), and it is
explicitly cross-script: `extension/lua/new-modes/traffic/mode.lua:52` stores
`newmode.traffic.active` and `extension/lua/tools/csp-traffic-tool/tool.lua:5`
reads it from a different script. The guard keeps a lease under
`ghostcar_hotlap.writer`:

```
<gen>|<instanceId>|<wallClockSeconds>|<sessionToken>
```

Election is **claim → settle → re-read**:

1. On the first update, read the lease. If a *fresh* foreign lease exists
   (younger than `INSTANCE_LEASE_S = 8`), stand by immediately without writing
   anything. This is the observed "session restart loaded a second app" case.
2. Otherwise write our claim and wait `INSTANCE_SETTLE_S = 0.6` (app clock),
   then re-read. If our id survived we become the writer; a fresh foreign id
   means we stand by. The settle window resolves a simultaneous cold start,
   where both instances wrote before either could read.
3. A writer heartbeats every second, re-asserts the lease if the shared storage
   was cleared by a session restart, and stands down if a duplicate holds a
   fresher lease. A standby polls once a second and takes over a free lease or
   one older than 8 s (crashed/hidden writer).
4. The writer releases the lease (`''`) on `script.shutdown`, so a normal app
   reload hands over immediately.

Only the writer calls `initialize()`/`updateGhost()`/`writeSample()`, so the
losing instance never creates a node and never appends a row. Its only output
is one `INSTANCE standby, another writer active ...` line (plus
`SHUTDOWN role=standby`), and its window says `STANDBY`.

Limits:

* There is no mutex or atomic compare-and-swap in the SDK, so this is a lease,
  not a lock. The residual race is the 0.6 s settle window at a simultaneous
  cold start.
* `ac.store()` is session-scoped shared storage, not cross-process IPC, and an
  instance cannot *prove* that the value it reads was written by another script.
  If a future CSP build gave each app instance a private store, both would
  verify their own claim and two writers would run; the guard cannot detect
  that from inside. The file backend already implemented for the "no
  `ac.store`" case is the remedy: the owner file next to the log is shared by
  construction. The backend choice is the one-line `INSTANCE_BACKEND`
  expression at the top of the guard section; force it to `'file'` to
  arbitrate across processes. The record format is identical.
* If writing the lease fails outright, the instance logs
  `INSTANCE unavailable store-write-failed; running unguarded` instead of
  staying silent. A write that never reads back is retried twice, then the same
  fail-open path is taken (`store-unverified`). Fail-open is deliberate: a
  silent app is worse than a duplicate one, and the log states which mode ran.
* A writer that is killed without `script.shutdown` keeps the lease for up to
  8 s; the standby then logs `INSTANCE takeover ...` and starts its own stream.
  Rows can therefore show one discontinuity at takeover, but never two
  continuously interleaved writer populations.

### Falsification of the v0.1 render check (why the oracle exists)

v0.1 was reported as "render path falsified" from four screenshots. The app's
own log refutes the timing of all four: they were taken when the ghost was
hundreds of metres away, so "no second GR86 in frame" was expected even with a
perfect renderer.

| screenshot | HUD lap time | nearest logged row `time` | ghost→player `dist` at that row |
| --- | --- | --- | --- |
| `ghostcar_q1.png` | 6:10.365 | 369.98 s | 282.1 m |
| `ghostcar_q2.png` | 6:16.581 | 376.82 s | 392.3 m |
| `ghostcar_q3.png` | 6:22.789 | 382.99 s | 181.2 m |
| `ghostcar_free1.png` | 7:23.127 | 442.98 s | 203.0 m |

(The lap timer in the HUD matches the Sectors app's `t=` readout and the
app log's `time`, so the mapping is direct.)

Meanwhile the same log has the ghost within 20 m of the player at
`gameTime` 146.82, 160.97, 283.82, 297.98, 420.82, 434.98, 571.99, 708.98 s —
that is, about every 137 s, as expected from a 136.95 s loop. The
`screenshots` were therefore taken ~210 s away from the closest approach. The
v0.1 window text was also never captured; that is consistent with the app being
collapsed into the bottom app strip, and is not evidence about the 3D scene.

## Root-cause analysis of the v0.1 render evidence

No API evidence supports "the render call path is broken"; the four suspects
were checked against the installed CSP Lua SDK (v3103, `extension/internal/lua-sdk/*`)
and the installed tools:

1. **`setVirtualCarFlag(true)` is not a visibility flag — refuted.**
   `ac_apps/lib.lua:10217`: *"Only applicable to cars added to `carsRoot:yes`
   node. If flag is set, node will be treated as a car by some specialized code,
   such as `BLIND_SPOT` input of car instruments."* It only feeds car-semantics
   code. The traffic tool sets it on every spawned car and those cars are visible
   in the main view in normal practice sessions
   (`extension/lua/tools/csp-traffic-tool/src/simulation/TrafficCar.lua:49-52`;
   the tool refuses to run only in `newmode.traffic.active`, `tool.lua:6`).
2. **App-context vs new-mode-context scene permissions — no evidence of a
   difference.** The app SDK exports the full node API that the app calls
   (`ac_apps/lib.lua:9795` `createBoundingSphereNode`, `9803` `loadKN5`, `10221`
   `setVirtualCarFlag`, `10396` `ac.findNodes`). Diffing `ac_apps/lib.lua`
   against `ac_new_modes/lib.lua` shows the only functions new-modes has and
   apps lack are session/weather/UI overrides plus `ac.setReplayBasedGhost`,
   `ac.disableCar`, `physics.setUserControlledCar` — nothing about scene nodes.
   Installed apps also write into the world render pass from app scripts
   (`apps/lua/ac-tracer/lib/reference_line.lua:301` registers
   `render.on('main.track.transparent', ...)`; `apps/lua/PureConfig/PureConfig.lua:1197`
   uses `script.draw3D` + `render.projectPoint`). v0.2 relies on exactly that
   mechanism for the oracle, so it doubles as the in-game test of suspect 2.
3. **Bounding-sphere culling / explicit `setVisible` after `loadKN5` — unlikely,
   guarded anyway.** `ac_apps/lib.lua:9788`: the node's sphere exists so
   "children would skip bounding frustum test, and whole node would not get
   traversed during rendering if it's not in frustum". Radius 3 m already
   covered a 4.2 × 1.8 m car; v0.2 raises it to 4 m for margin and calls
   `loaded:setVisible(true, false)` once, which the SDK documents as a no-op when
   the object was already visible (`lib.lua:9851`).
4. **`loadKN5` readiness — not a factor for the failure signature, but the path
   contract was violated.** `loadKN5` is synchronous; `loadKN5Async` is the
   explicit async variant (`ac_apps/lib.lua:9803`, `9818`). v0.1 also passed a
   hand-built absolute path, while the documented form is a path "relative to
   script folder or AC root folder" (`lib.lua:9797`) and the installed tools pass
   `extension/...`-relative paths (`TrafficCarFullLOD.lua:34`,
   `TrafficCar.lua:52`). v0.2 resolves `content/cars/.../toyota_gr86_premium.kn5`
   through `ac.findFile()` (`lib.lua:3158`), verifies existence before loading,
   and logs the resolved `model=` path in the `NODE` line.

Conclusion: v0.1's render evidence is invalid, so the most likely "render
failure" cause is **the verification, not the renderer** (screenshots 181–392 m
off). The remaining, still-unproven possibility is that scene-node writes from
an app script are dropped; the v0.2 oracle is built so that one in-game run
decides it without relying on human eyes.

## Notes / deliberate choices

* `rotationRad` is `{pitch, yaw, roll}` — yaw is element 2, wrapped into
  `[-pi, pi]`, and world forward is `(-sin(yaw), 0, cos(yaw))`. This follows the
  verified conclusions in `docs/assetto-corsa-replay-notes.md` and is unchanged
  from the spike.
* The GR86 KN5 path is hard-coded so playback never depends on the model
  naming of the car being driven.
* Yaw is interpolated along the shortest arc between frames; the frame cursor
  advances monotonically instead of rescanning all 9131 frames per tick.
* `clearMotion()` is called once after the first pose: the first update
  teleports the node hundreds of metres from the scene origin and CSP
  interpolates motion blur from the previous world position
  (`ac_apps/lib.lua:9687`).
* The new-mode `[RULES]` (`BASE_MODE`, `PENALTIES`, physics flags) are **not**
  carried over; an app must not alter session rules.
* CSP APIs used, with evidence: `ac.findNodes`, `createBoundingSphereNode`,
  `setVirtualCarFlag`, `loadKN5`, `setPosition`, `setOrientation`, `dispose`,
  `setVisible`, `clearMotion`, `isActive`, `empty`, `name`, `getChildrenCount`,
  `getLocalAABB` (SDK `ac_apps/lib.lua`),
  `findMeshes`, `ensureUniqueMaterials`, `setMaterialProperty`,
  `setMaterialTexture`, `setTransparent`, `setBlendMode`, `setShadows`
  (`ac_apps/lib.lua:9639`, `9675`, `9681`, `9858`, `9870`, `9874`, `10121`),
  `ac.store`, `ac.load` (session-shared storage, `ac_apps/lib.lua:3953-3966`),
  `ac.findFile`, `ac.getCameraPosition`, `ac.isVisibleInMainCamera`,
  `render.on`, `render.projectPoint`, `render.debugCross`
  (`ac_apps/lib.lua:3158`, `3383`, `3596`, `12447`, `12499`, `12409`),
  `io.fileExists` / `io.load` / `io.open` / `io.createFileDir`
  (`apps/lua/ac-tracer/lib/core/files.lua`, `paths.lua`), `json.decode`
  (`csp-traffic-tool/tool.lua`), with `JSON.parse` as fallback.
* Lap sync (v0.4) uses only `ac.getCar(0).lapCount` as the boundary, which is
  the counter the installed ac-tracer keys off
  (`apps/lua/ac-tracer/lib/core/state.lua:1263`, `windows/delta_bar.lua:79`).
  Teleports (`ac.onCarJumped`, `resetCounter`, `splinePosition` jumps) and lap
  validity (`isLastLapValid`) are deliberately not used to restart playback; see
  the policy table above for the reasoning.
* The sync toggle is persisted with `ac.storage` (the auto-saving store used by
  ac-tracer's `lib/core/settings.lua:50` and by map_display), flipped with
  `ui.checkbox` in the same click-to-invert pattern as
  `settings.lua:267-268`. Assignment persists; there is no save call.
* `GHOST_START_OFFSET_S` is a per-timeline phase constant, not a per-lap
  correction: the bundled threemilebypass file is measured ~1.41 s ahead of the
  line, but the shipped default stays `0.0` so the app is correct for a
  timeline cut crossing-to-crossing (see "Aligning frame 0").
* Transparency is one constant, `GHOST_ALPHA = 0.5`, at the top of
  `ghostcar_hotlap.lua`. `GHOST_TINT` is the flat texel color; the livery is
  intentionally not preserved because the SDK has no alpha-only material
  override (see "How v0.3 draws transparency").
* The single-instance lease lives in the shared store; it is a lease with a
  0.6 s settle window, not a mutex (see "How v0.3 elects a single writer" for
  the limits and the file fallback).
* UNKNOWN: whether `render.on()` callbacks may *mutate* a scene node on every
  supported CSP build. The SDK documents the callback as "called when AC is
  drawing something" and lists `main.track.transparent` for content added in the
  transparent pass (SDK `ac_apps/README.md` "Function render.on"), and the
  installed ac-tracer uses the same event, but only for drawing. v0.5 relies on
  it for the pose; if a build rejects it, the app logs one
  `SMOOTH pose-error render-context` and falls back to app-tick posing, so the
  worst case is v0.4 behaviour rather than a broken ghost.
* UNKNOWN: whether `script.update` keeps running when the app window is closed
  in-session. Installed apps (`map_display`, `ac-tracer`) update state from
  `script.update` unconditionally, which is the assumption here; the v0.2 log is
  written from `script.update`, so a silent log after a long drive would mean
  that assumption is wrong. Since v0.5 the *pose* is applied from the render
  callback, so if `script.update` did stop, the ghost would keep driving on the
  render path alone with the display clock free-running at `playRate` and no
  1 Hz rows / `SMOOTH` lines -- i.e. a frozen log with a moving ghost is now the
  signature of that case, not a frozen ghost.
* No native replay/template/private telemetry is distributed with this app.

## v0.3 in-game proof (2026-11-22, Thunderhill threemilebypass, GR86)

Transparency **proven visually**: the ghost body is semi-transparent
(`GHOST_ALPHA = 0.5`), track visible through the panels, wheels/trim stay
opaque (their shaders matched no filter), red 2 m debug cross still marks
the pose. Log gates, same session:

* `INSTANCE writer ... backend=shared attempts=1` (single writer),
* `INIT ok app=0.3 ... alpha=0.500`,
* `GHOSTROOT unique=ok transparent=ok blend=ok shadows=ok tint=ok` (root pass),
* `GHOST alpha=0.500 mode=alpha meshes=55 transparent=ok blend=ok shadows=ok
  tint=ok unique=ok` with
  `filters=shader:ksPerPixelMultiMap_damage_dirt=0,shader:ksPerPixelMultiMap=50,
  shader:ksPerPixel=4,shader:ksWindscreen=1,shader:ksGlass=0,shader:ksTyre=0,
  shader:ksWheel=0`,
* `ACTIVE mode=runtime`, zero `ERROR`.

Findings worth keeping: (a) this converted GR86 uses plain
`ksPerPixelMultiMap` (50 meshes, no `_damage_dirt` variant) — filter by
actual shader, not by assumption; (b) `findMeshes('*')` matches nothing,
use `key:value` filters; (c) only 55/317 meshes are covered — full-car
coverage would need the remaining interior/trim shader names.

