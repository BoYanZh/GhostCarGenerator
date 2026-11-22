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

**Status: v0.3.** The data path and the *opaque* render path are proven in-game
(v0.2 proof below). v0.3 adds the two remaining features: a semi-transparent
ghost body (`GHOST_ALPHA`, default `0.5`) and a single-instance writer lease so
a duplicate app instance cannot interleave log rows. Both shipped without a game
launch; "v0.3 protocol" below states exactly what to look for in-game.

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

Line shapes (v0.3; v0.2 lines are unchanged unless a new token is appended):

```
INIT ok app=0.3 schema=ghost-runtime-v1 frames=9131 durationS=136.950 carId=... trackId=... log=... alpha=0.500
INIT missing-runtime fallback=5m-follow player app=0.3 log=... alpha=0.500
INSTANCE writer id=<instanceId> gen=<n> backend=shared|file attempts=<n>
INSTANCE standby, another writer active id=<instanceId> gen=<n> ageS=<n> backend=shared|file
INSTANCE takeover id=<instanceId|free> genOld=<n> ageS=<n> gen=<n>
INSTANCE unavailable <store-write-failed|store-unverified>; running unguarded
NODE ok app=0.3 rootActive=1 rootChildren=1 modelEmpty=0 modelName=... modelActive=1 modelChildren=89 meshes=317 meshVisible=1 aabbSize=4.20x1.30x1.80 aabbMeshes=317 wheels=4 model=E:/.../toyota_gr86_premium.kn5
GHOST alpha=0.500 mode=alpha meshes=317 transparent=ok blend=ok shadows=ok tint=ok unique=ok
GHOST alpha=1.000 mode=opaque meshes=0
GHOST failed <reason>
RENDERHOOK ok event=main.track.transparent frames=1
RENDERHOOK unavailable render.on | RENDERHOOK failed <reason>
CLOCK sim | CLOCK app
ACTIVE mode=runtime frames=9131 hook=none|ok alpha=0.500 log=...
SESSION token=0
<time>,<ghostX>,<ghostY>,<ghostZ>,<playerX>,<playerY>,<playerZ>,<dist>,<ghostNdcX>,<ghostNdcY>,<inFrustum01>,<camDistM>,<ghostT>
WARN init <message>
ERROR <stage> <message>
SHUTDOWN role=writer|standby [release=ok]
```

v0.3 protocol deltas:

* `INIT` is written by the **elected writer** after the instance lease settles
  (≤ 1 s after load), not at script load. `alpha=` is the clamped
  `GHOST_ALPHA`. Everything before `alpha=` keeps the v0.2 grammar.
* A standby instance writes one `INSTANCE standby, another writer active ...`
  line and **no** `INIT`, `NODE`, `ACTIVE`, `SESSION` or data rows. That is the
  fix for the v0.2 "second `INIT ok`, two interleaved writers" bug.
* `GHOST` reports the transparency actually applied. `mode=alpha` with
  `transparent=ok blend=ok tint=ok` is the expected line; `GHOST failed ...`
  means the app fell back to the opaque look and says why.
* `ACTIVE` now also carries `alpha=`, and `SHUTDOWN` carries `role=`.
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

## Verification

### v0.3 protocol (use this one)

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

![ghost in chase view](../../../docs/assets/ghostcar_hotlap_proof_wide.jpg)
![ghost close-up with debug cross](../../../docs/assets/ghostcar_hotlap_proof_close.png)

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
* Transparency is one constant, `GHOST_ALPHA = 0.5`, at the top of
  `ghostcar_hotlap.lua`. `GHOST_TINT` is the flat texel color; the livery is
  intentionally not preserved because the SDK has no alpha-only material
  override (see "How v0.3 draws transparency").
* The single-instance lease lives in the shared store; it is a lease with a
  0.6 s settle window, not a mutex (see "How v0.3 elects a single writer" for
  the limits and the file fallback).
* UNKNOWN: whether `script.update` keeps running when the app window is closed
  in-session. Installed apps (`map_display`, `ac-tracer`) update state from
  `script.update` unconditionally, which is the assumption here; the v0.2 log is
  written from `script.update`, so a silent log after a long drive would mean
  that assumption is wrong.
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

![translucent ghost with debug cross](../../../docs/assets/ghostcar_hotlap_proof_translucent.jpg)
