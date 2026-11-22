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
per-frame `setPosition` / `setOrientation`.

**Status: v0.2.** The data path is proven in-game. The render path is *not*
proven either way — v0.1's falsification was produced with screenshots taken
181–392 m away from the ghost (see "Falsification of the v0.1 render check"),
so v0.2 adds a machine-readable render oracle instead of another blind
screenshot pass.

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

Line shapes (v0.2):

```
INIT ok app=0.2 schema=ghost-runtime-v1 frames=9131 durationS=136.950 carId=... trackId=... log=...
INIT missing-runtime fallback=5m-follow player app=0.2 log=...
NODE ok app=0.2 rootActive=1 rootChildren=1 modelEmpty=0 modelName=... modelActive=1 modelChildren=89 meshes=317 meshVisible=1 aabbSize=4.20x1.30x1.80 aabbMeshes=317 wheels=4 model=E:/.../toyota_gr86_premium.kn5
RENDERHOOK ok event=main.track.transparent frames=1
RENDERHOOK unavailable render.on | RENDERHOOK failed <reason>
CLOCK sim | CLOCK app
ACTIVE mode=runtime frames=9131 hook=none|ok log=...
SESSION token=0
<time>,<ghostX>,<ghostY>,<ghostZ>,<playerX>,<playerY>,<playerZ>,<dist>,<ghostNdcX>,<ghostNdcY>,<inFrustum01>,<camDistM>,<ghostT>
WARN init <message>
ERROR <stage> <message>
SHUTDOWN
```

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

## Verification

### v0.2 protocol (use this one)

Start **Hotlap** at `thunderhill_raceway_park / threemilebypass` with the
`ig_toyota_gr86_premium`, leave the player car stationary in the pit box, and
enable the app. Then:

1. **App loaded / data ok**

   ```powershell
   $log = "$env:USERPROFILE\Documents\Assetto Corsa\logs\ghostcar_hotlap_verify.log"
   Select-String -Path $log -Pattern 'INIT ok|NODE ok|ACTIVE mode=runtime|ERROR'
   ```

   * `INIT ok app=0.2 ... frames=9131 durationS=136.950` — timeline loaded.
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

![ghost in chase view](../../../docs/assets/ghostcar_hotlap_proof_wide.jpg)
![ghost close-up with debug cross](../../../docs/assets/ghostcar_hotlap_proof_close.png)

Caveats carried forward: a duplicate app instance was observed writing
interleaved rows (second `INIT ok` after a session restart while the first
instance kept running); v0.3 should add a single-instance guard. The
v0.2 build renders opaque; transparency is a separate v0.3 item.

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
  `ac.findFile`, `ac.getCameraPosition`, `ac.isVisibleInMainCamera`,
  `render.on`, `render.projectPoint`, `render.debugCross`
  (`ac_apps/lib.lua:3158`, `3383`, `3596`, `12447`, `12499`, `12409`),
  `io.fileExists` / `io.load` / `io.open` / `io.createFileDir`
  (`apps/lua/ac-tracer/lib/core/files.lua`, `paths.lua`), `json.decode`
  (`csp-traffic-tool/tool.lua`), with `JSON.parse` as fallback.
* UNKNOWN: whether `script.update` keeps running when the app window is closed
  in-session. Installed apps (`map_display`, `ac-tracer`) update state from
  `script.update` unconditionally, which is the assumption here; the v0.2 log is
  written from `script.update`, so a silent log after a long drive would mean
  that assumption is wrong.
* No native replay/template/private telemetry is distributed with this app.
