-- GhostCarGenerator Hotlap app for CSP.
--
-- Renders a second, visual-only KN5 car in a normal Hotlap session (no CSP
-- new-mode required). The body pose is driven by the `ghost-runtime-v1.json`
-- timeline sitting next to this file; when that file is missing the app falls
-- back to a static 5 m offset that follows the player so a second car still
-- renders in an otherwise empty install.
--
-- v0.5 additions (see README "v0.5 protocol"): motion smoothing.
--   * THE BUG: the ghost pose was applied from the app tick only. `applyPose()`
--     (the sole `setPosition` on the runtime path) was reachable only through
--     `sampleRuntimeFrame()` <- `updateGhost()` <- `script.update()`, so the
--     node was re-posed once per app tick, at whatever `sim.gameTime` delta had
--     accumulated by then. The timeline sample itself was interpolated
--     (position lerp + shortest-arc yaw), but a ~55 m/s ghost moves ~0.9 m per
--     60 Hz tick, and that is the visible stepping.
--   * THE FIX: the pose is now applied from the existing `render.on()`
--     callback, once per drawn frame, from a *continuous* playback time that is
--     phase-locked to the app-tick timeline phase (`state.runtimeTime`) with a
--     small per-frame gain. A coarse or jittery `sim.gameTime` therefore only
--     nudges the phase; it can no longer quantise what is on screen.
--   * Wheel roll: `step * 5` fed `rotate()` a constant 5 rad/s regardless of
--     speed (~30x too slow at 55 m/s) and inherited every app-tick delta
--     irregularity. It is now derived from the timeline segment speed and the
--     playback interval actually consumed, and applied on the same render
--     frame path.
--   * Proof: one `SMOOTH windowS=... maxStepM=... maxYawDeg=... renderHz=...
--     tickHz=... resets=...` line per 5 s (worst case per-frame displacement
--     between pose applications, in metres) plus one `SMOOTHPATH pose=render|tick`
--     line at ACTIVE.
--
-- v0.2 additions (see README "Verification"):
--   * Machine-readable render oracle: every data row now carries
--       ghostNdcX,ghostNdcY,inFrustum01,camDistM,ghostT
--     appended after the v0.1 eight-field prefix. NDC comes from
--     `render.projectPoint()` sampled inside a `render.on()` callback
--     (render context), the frustum bit from `ac.isVisibleInMainCamera()`
--     (documented as callable from anywhere).
--   * One-time NODE introspection line (valid handles, child counts, model
--     AABB, wheels) so "loaded but empty" is distinguishable from
--     "loaded and correctly sized".
--   * Playback clock is locked to `ac.getSim().gameTime` when possible, so the
--     re-verify timing rule is exact instead of drifting with app frame time.
--   * KN5 path resolved through `ac.findFile()` (documented portable form)
--     instead of a raw concatenation of an absolute folder path.
--
-- v0.4 additions (see README "v0.4 protocol"):
--   * Lap synchronisation: when the player's completed-lap counter increments
--     (`ac.getCar(0).lapCount`, the same signal `ac-tracer` keys off at
--     apps/lua/ac-tracer/lib/core/state.lua:1263 and lib/windows/delta_bar.lua:79)
--     the playback cursor jumps back to the start of the timeline, so the ghost
--     and the player cross the start/finish line together on every lap.
--     Teleports, pit stops and invalidated laps are deliberately NOT lap
--     boundaries for this purpose (see README "Policy for the awkward cases").
--   * Toggle in the settings window, default ON, persisted through
--     `ac.storage` (apps/lua/ac-tracer/lib/core/settings.lua:50). Its state is
--     logged as `syncLaps=1/0` on the INIT line, and every restart writes one
--     `LAPSYNC ...` line. With the toggle OFF the app is byte-for-byte v0.3
--     free-run behaviour.
--   * `GHOST_START_OFFSET_S` tunable for the constant (if any) between
--     timeline frame 0 and the track's start/finish line.
--
-- v0.3 additions (see README "v0.3 protocol"):
--   * Transparency: the loaded ghost KN5 is drawn with `setTransparent(true)` +
--     `setBlendMode(render.BlendMode.AlphaBlend)` and a flat translucent
--     `txDiffuse` color carrying GHOST_ALPHA. The SDK has no per-material
--     opacity call, so alpha is carried by the diffuse texture slot, which is
--     the channel AC's blend path already uses for windscreens. Materials are
--     made unique first (`ensureUniqueMaterials()`), which is what keeps the
--     edit off the player car even when both use the same pooled KN5.
--   * Single-instance guard: a session-shared lease in `ac.store()`/`ac.load()`
--     elects exactly one writer. A duplicate app instance logs one
--     `INSTANCE standby, another writer active ...` line and never creates a
--     node or appends a data row.
--
-- Determinism notes (verified conclusions, see docs/assetto-corsa-replay-notes.md):
--   * `rotationRad` is {pitch, yaw, roll}; yaw is element 2 and is wrapped into
--     [-pi, pi] before use.
--   * World forward for a yaw value is (-sin(yaw), 0, cos(yaw)).
--   * The KN5 is hard-coded to the Initial G GR86 Premium so playback never
--     depends on the model naming of the car the player is currently driving.
--
-- Observability:
--   * Every second a CSV row is appended to
--     `%USERPROFILE%\Documents\Assetto Corsa\logs\ghostcar_hotlap_verify.log`:
--       time,ghostX,ghostY,ghostZ,playerX,playerY,playerZ,dist,\
--       ghostNdcX,ghostNdcY,inFrustum01,camDistM,ghostT
--     `time` is `ac.getSim().gameTime` in seconds; coordinates are metres; the
--     ghost coordinates are the values handed to `setPosition`.
--     `nan` means "not available" (oracle hook never ran, or the point projects
--     to infinity because it is behind the camera).
--   * On load a single `INIT ok ...` or `INIT missing-runtime ...` line is
--     written by the elected writer, followed by `NODE ok ...` once the scene
--     node exists and `GHOST ...` with the transparency actually applied.
--     The INIT line ends with `alpha=<value>`; everything before it keeps the
--     v0.2 shape. Failures also produce an `ERROR ...` line and an `ac.warn`.
--   * Standby instances write only `INSTANCE standby, another writer active ...`
--     (plus `SHUTDOWN role=standby`), so a duplicate app cannot interleave rows.
--   * The window text starts with `GhostCarGenerator Hotlap: ACTIVE` or
--     `GhostCarGenerator Hotlap: ERROR: ...` (or `... STANDBY ...`) so
--     screenshots can be asserted on without OCR of the numbers.

local APP_VERSION = '0.7'
-- Default tunable alpha for the ghost body, 0..1 (1 = opaque look).
-- Persisted via ac.storage and live-adjustable in the UI.
local GHOST_ALPHA = 0.5
-- Default lead time in seconds so the ghost runs just ahead of the player
-- (~11-25 m depending on speed), matching AC's built-in 0.5 s ghost advantage.
local GHOST_DEFAULT_LEAD_S = 0.5
local LAPSYNC_DEBOUNCE_S = 5.0
local LAUNCH_SPEED_KMH = 5.0
local NEAREST_MAX_DIST_M = 40.0
-- Timeline steerAngleDeg is steering-WHEEL degrees straight from the LD
-- channel (about +/-90 on track). Wheel nodes need ROAD angle, hence the
-- steering ratio (2nd-gen GR86: 13.5:1). Visual only; tune freely.
local GHOST_STEER_RATIO = 13.5
-- v0.5 smoothing. Everything below is about *when* the pose is applied, not
-- about the data: the timeline stays `ghost-runtime-v1` with its 15 ms frames.
--
-- The render callback (`render.on('main.track.transparent')`) already fires
-- once per drawn frame (the v0.2 oracle proves it: `RENDERHOOK ok ... frames`).
-- The pose now lives there, driven by `os.preciseClock()` (documented as
-- "time in seconds from script start (with high precision)",
-- ac_apps/lib.lua:4427), so per-frame motion does not depend on the granularity
-- of `ac.getSim().gameTime` (a millisecond snapshot of the game step, which is
-- why the separate `os.preciseGameTime()` accessor exists at lib.lua:4432).
--
-- PHASE_GAIN: fraction of the remaining phase error applied per render frame
-- when the display time is *behind* the authoritative phase. With the deadband
-- below, normal jitter is never corrected at all -- only real drift is.
-- LEAD_MAX_S: the display may never run further ahead of the authoritative
-- phase than this. That single clamp is also what makes a pause self-limiting:
-- `runtimeTime` freezes, the display walks into the clamp and stops, and when
-- playback resumes it waits out the remaining milliseconds instead of drifting
-- and snapping back.
-- PHASE_DEADBAND_S: phase lag smaller than this is left alone, so a coarse
-- `sim.gameTime` (game-step chunks) cannot modulate the on-screen speed.
-- PHASE_SNAP_S: larger error = deliberate re-anchor (first frame, session
-- reset, lap restart), counted in `resets=` instead of being smoothed.
local PHASE_GAIN = 0.05
local PHASE_SPEED_CAP = 0.08
local PHASE_DEADBAND_S = 0.05
local LEAD_MAX_S = 0.03
local PHASE_SNAP_S = 0.5
-- One long frame (window drag, load hitch) must not lurch the ghost; the phase
-- lock pulls it back over the following frames instead.
local MAX_RENDER_WALL_S = 0.05
-- Playback seconds per wall second as measured at the app tick: 1 at normal
-- speed, ~0 while paused, following replay slow motion. Measured over a whole
-- window, not per tick, so a quantised `sim.gameTime` cannot make the estimated
-- rate noisy (which would show up as speed modulation).
local PLAYBACK_RATE_GAIN = 0.25
local PLAYBACK_RATE_WINDOW_S = 0.5
local MAX_PLAYBACK_RATE = 3
-- If `render.on` returns a subscription but the callback never fires, the ghost
-- would freeze at its first pose; after this many app ticks the app falls back
-- to app-tick posing (logged once as `SMOOTHPATH pose=tick reason=...`).
local RENDER_HOOK_GRACE_TICKS = 120
-- ...and if the callback stops arriving for this long, the app tick takes the
-- pose back (and hands it over again when the hook resumes).
local RENDER_HOOK_STALE_S = 0.25
-- GR86 road-wheel radius, metres. Only turns timeline speed into wheel roll; a
-- silhouette detail, not a physics claim.
local GHOST_WHEEL_RADIUS_M = 0.32
local MAX_PLAUSIBLE_SPEED_MPS = 120
local SMOOTH_LOG_WINDOW_S = 5.0
-- Flat tints written into txDiffuse; the ghost keeps its shape/normals without
-- its livery. Splitting body vs dark-trim/tyre/glass tints avoids bright milky
-- tyres or distracting underbody bleed-through, giving a clean GT/iRacing-style
-- translucent ghost silhouette.
local GHOST_TINT_BODY = { 0.52, 0.68, 0.86 }
local GHOST_TINT_DARK = { 0.16, 0.20, 0.26 }
local GHOST_TINT_GLASS = { 0.12, 0.18, 0.26 }
-- v0.4 lap sync. `GHOST_START_OFFSET_S` is how far the timeline runs AHEAD of
-- the player's lap at the start/finish crossing, in seconds: playback starts at
-- (duration - offset) when the player's lap counter increments. 0.0 shows the
-- raw file phase (frame 0) and is the only safe default for an arbitrary
-- `ghost-runtime-v1.json`.
--
-- Offline measurement for the bundled threemilebypass timeline
-- (`ghost-runtime-v1.json`, 9131 frames / 136.95 s) says frame 0 sits 66.3 m
-- PAST the AI-spline start point, i.e. ~1.41 s ahead of the line at racing
-- speed, so 1.41 is the value that puts that file's frame on the line. It is
-- not the default because the app historically ships with data it does not
-- own; see README "Aligning frame 0 to the start line" for the derivation and
-- for the in-game calibration procedure.
local GHOST_START_OFFSET_S = 0.0
-- v0.4/v0.6 persisted user settings. `ac.storage` is the SDK's auto-saving
-- script store used by the installed ac-tracer app
-- (apps/lua/ac-tracer/lib/core/settings.lua:50: `ac.storage({...}, "ac_tracer/")`)
-- and by PureConfig; assignment persists, no explicit save call.
local SETTINGS_PREFIX = 'ghostcar_hotlap/'
local settings
do
  local defaults = {
    syncLaps = true,
    visible = true,
    alpha = GHOST_ALPHA,
    heightOffsetM = 0.0,
    startOffsetS = GHOST_START_OFFSET_S,
    leadS = GHOST_DEFAULT_LEAD_S,
    debugCross = false,
  }
  settings = defaults
  if type(ac.storage) == 'function' then
    local ok, store = pcall(ac.storage, defaults, SETTINGS_PREFIX)
    if ok and type(store) == 'table' then
      settings = store
    end
  end
  -- An older/partial settings file must not leave nil fields.
  if settings.syncLaps == nil then settings.syncLaps = true end
  if settings.visible == nil then settings.visible = true end
  if type(settings.alpha) ~= 'number' then settings.alpha = GHOST_ALPHA end
  if type(settings.heightOffsetM) ~= 'number' then settings.heightOffsetM = 0.0 end
  if type(settings.startOffsetS) ~= 'number' then settings.startOffsetS = GHOST_START_OFFSET_S end
  if type(settings.leadS) ~= 'number' then settings.leadS = GHOST_DEFAULT_LEAD_S end
  if settings.debugCross == nil then settings.debugCross = false end
end

local function clamp01(value)
  if type(value) ~= 'number' or value ~= value then return 0.5 end
  if value < 0 then return 0 end
  if value > 1 then return 1 end
  return value
end

local function syncLapsEnabled()
  return settings.syncLaps ~= false
end

local function ghostVisibleEnabled()
  return settings.visible ~= false
end

local function currentAlpha()
  return clamp01(settings.alpha)
end

local function currentHeightOffsetM()
  local v = settings.heightOffsetM
  if type(v) ~= 'number' or v ~= v then return 0.0 end
  if v < -2.0 then return -2.0 end
  if v > 2.0 then return 2.0 end
  return v
end

local function currentStartOffsetS()
  local v = settings.startOffsetS
  if type(v) ~= 'number' or v ~= v then return GHOST_START_OFFSET_S end
  return v
end

local function currentLeadS()
  local v = settings.leadS
  if type(v) ~= 'number' or v ~= v then return GHOST_DEFAULT_LEAD_S end
  if v < -2.0 then return -2.0 end
  if v > 5.0 then return 5.0 end
  return v
end

local function debugCrossEnabled()
  return settings.debugCross == true
end

local GHOST_MODEL_RELATIVE = 'content/cars/ig_toyota_gr86_premium/toyota_gr86_premium.kn5'
local RUNTIME_FILENAME = __dirname .. '/ghost-runtime-v1.json'
local LOG_RELATIVE_DIR = 'Documents/Assetto Corsa/logs'
local LOG_FILENAME = 'ghostcar_hotlap_verify.log'
local LOG_INTERVAL_S = 1.0
local FALLBACK_OFFSET = vec3(5, 0, 0)
local GHOST_NODE_NAME = 'GhostCarGenerator.Hotlap'
-- Half-diagonal of a 4.2 x 1.8 m car is ~2.3 m; keep headroom so the bounding
-- sphere never culls the model at screen edges (SDK ac_apps/lib.lua:9788).
local GHOST_NODE_RADIUS = 4
-- Render-context hooks: pose runs in `main.root.opaque` (before both opaque and
-- transparent geometry passes), while the projection oracle and optional debug
-- cross run in `main.track.transparent`.
local POSE_RENDER_EVENT = 'main.root.opaque'
local RENDER_EVENT = 'main.track.transparent'
-- Single-instance lease. `ac.store`/`ac.load` are the SDK's session-shared
-- script storage (ac_apps/lib.lua:3953-3966); the value is
--   <gen>|<instanceId>|<wallClockSeconds>|<sessionToken>
-- An empty value means "free". A writer is replaced only after LEASE_S of
-- silence, so a crashed/hidden instance cannot keep the ghost off forever.
local INSTANCE_KEY = 'ghostcar_hotlap.writer'
local INSTANCE_LEASE_S = 8
local INSTANCE_SETTLE_S = 0.6
local INSTANCE_HEARTBEAT_S = 1

-- ---------------------------------------------------------------------------
-- Logging
-- ---------------------------------------------------------------------------

local LOG_PATH = nil
do
  -- ac.FolderID.Logs resolves to the real Documents folder even when it is
  -- redirected (OneDrive, localized profile); keep a plain path as fallback.
  local ok, logsFolder = pcall(function() return ac.getFolder(ac.FolderID.Logs) end)
  if ok and type(logsFolder) == 'string' and logsFolder ~= '' then
    LOG_PATH = logsFolder:gsub('[/\\]+$', '')..'\\'..LOG_FILENAME
  else
    local profile = os.getenv('USERPROFILE') or os.getenv('HOME')
    if profile then
      LOG_PATH = profile .. '\\' .. LOG_RELATIVE_DIR:gsub('/', '\\') .. '\\' .. LOG_FILENAME
    end
  end
end

local logDirReady = false
local logWarned = false

local function appendLog(line)
  if not LOG_PATH then return false end
  if not logDirReady then
    -- io.createFileDir() creates the parent directories of its argument.
    pcall(io.createFileDir, LOG_PATH)
    logDirReady = true
  end
  local file = io.open(LOG_PATH, 'a')
  if file then
    file:write(line..'\n')
    file:close()
    return true
  end
  -- Some sandboxes restrict append mode; fall back to read-modify-write with
  -- the CSP io helpers (used by the installed CSP tools as well).
  local existing = io.load(LOG_PATH, '') or ''
  if io.save(LOG_PATH, existing..line..'\n') then
    return true
  end
  logDirReady = false
  if not logWarned then
    logWarned = true
    ac.warn('GhostCarGenerator Hotlap: cannot write '..tostring(LOG_PATH))
  end
  return false
end

-- `nan`/`inf` are emitted verbatim: they are valid Python float literals and
-- keep "unknown" distinguishable from a real 0.
local function fmtNumber(value)
  if type(value) ~= 'number' then return 'nan' end
  if value ~= value then return 'nan' end
  if value == math.huge then return 'inf' end
  if value == -math.huge then return '-inf' end
  return string.format('%.4f', value)
end

-- Tokens land in a space-separated key=value line (not in the CSV row), so
-- only whitespace has to be neutralised; paths and names stay readable.
local function sanitizeToken(value)
  return (tostring(value):gsub('%s+', '_'))
end

-- ---------------------------------------------------------------------------
-- State
-- ---------------------------------------------------------------------------

local state = {
  status = 'INIT', -- INIT | ACTIVE | ERROR
  mode = nil,      -- runtime | fallback
  errorMessage = nil,
  failed = false,
  sessionToken = nil,
  root = nil,
  model = nil,
  modelPath = nil,
  wheels = {},
  runtimeTime = 0,
  runtimeCursor = 1,
  logAccum = 0,
  ghostX = 0, ghostY = 0, ghostZ = 0,
  playerX = 0, playerY = 0, playerZ = 0,
  clock = 0,
  simGameTime = nil,
  clockSource = nil,
  nextInitAttemptAt = 0,
  initFailures = 0,
  poseApplied = false,
  materialMode = nil,
  materialMeshes = 0,
  materialError = nil,
  ghostMeshSets = {},
  appliedAlpha = nil,
  appliedVisible = nil,
  -- v0.4/v0.7 lap sync. `lapBaseline` is the last player `lapCount` seen; nil
  -- means "not tracking yet". `prevSpline` tracks `splinePosition` so Lap 1
  -- from HOTLAP_START (where `lapCount` stays 0 across S/F) also syncs cleanly.
  lapBaseline = nil,
  lapRestarts = 0,
  lapSyncUnavailable = false,
  prevSpline = nil,
  prevResetCounter = nil,
  lastLapSyncWallS = -1e9,
  playerLaunched = false,
  holdingForLaunch = false,
  -- v0.5 motion smoothing. `runtimeTime` stays the authoritative timeline phase
  -- (advanced by `sim.gameTime` deltas at the app tick, lap-synced); the render
  -- callback turns it into `displayTime`, which is what gets sampled and posed.
  displayTime = 0,
  playRate = 1,
  rateStepAcc = 0,
  rateWallAcc = 0,
  lastTickPrecise = nil,
  renderLastPrecise = nil,
  renderPoseActive = false,
  renderPoseFrames = 0,
  lastRenderPoseWall = nil,
  renderFallbackLogged = false,
  activeTicks = 0,
  poseErrorLogged = false,
  reanchor = true,
  prevPose = nil,
  steerRad = 0,
  steerApplied = nil,
  speedMps = 0,
  smooth = {
    windowS = 0,
    maxStepM = 0,
    sumStepM = 0,
    stepCount = 0,
    maxYawRad = 0,
    frames = 0,
    ticks = 0,
    resets = 0,
  },
}

-- Render-context oracle cache; filled by the `render.on()` callback above and
-- read by writeSample().
local oracle = {
  ndcX = nil,
  ndcY = nil,
  hookFrames = 0,
  hookAnnounced = false,
}

-- Retry scene setup once a second (the 3D scene can still be loading when the
-- app script runs) and only report a hard error after this many failures.
local MAX_INIT_FAILURES = 15

local runtime = nil
local runtimeDurationS = 0
local renderSubscription = nil
local poseSubscription = nil
local poseRenderEvent = POSE_RENDER_EVENT
-- Forward declaration: `installRenderOracle()` (below) installs the render
-- callback before the pose code is defined, and the callback has to call it.
local updatePoseInRender

-- ---------------------------------------------------------------------------
-- JSON helpers (CSP exposes both `json` and `JSON`; accept either)
-- ---------------------------------------------------------------------------

local function decodeJson(text)
  if json and json.decode then
    return json.decode(text)
  end
  if JSON and JSON.parse then
    return JSON.parse(text)
  end
  return nil, 'no JSON decoder available'
end

local function isVector3(value)
  return type(value) == 'table' and type(value[1]) == 'number'
    and type(value[2]) == 'number' and type(value[3]) == 'number'
end

local function loadRuntime()
  if not io.fileExists(RUNTIME_FILENAME) then
    return nil, 'missing-runtime'
  end
  local text = io.load(RUNTIME_FILENAME)
  if not text or text == '' then
    return nil, 'runtime-unreadable'
  end
  local ok, decoded = pcall(decodeJson, text)
  if not ok then
    return nil, 'runtime-decode-threw: '..tostring(decoded)
  end
  if type(decoded) ~= 'table' then
    return nil, 'runtime-decode-failed'
  end
  if decoded.schema ~= 'ghost-runtime-v1' then
    return nil, 'runtime-schema-mismatch: '..tostring(decoded.schema)
  end
  local frames = decoded.frames
  if type(frames) ~= 'table' or #frames == 0 then
    return nil, 'runtime-has-no-frames'
  end
  local first, last = frames[1], frames[#frames]
  if not isVector3(first.positionM) or not isVector3(first.rotationRad) then
    return nil, 'runtime-frame-shape-invalid'
  end
  if type(last.timeS) ~= 'number' or last.timeS <= 0 then
    return nil, 'runtime-duration-invalid'
  end
  return decoded, nil
end

local runtimeError = nil
runtime, runtimeError = loadRuntime()
if runtime then
  runtimeDurationS = runtime.frames[#runtime.frames].timeS
end

-- The INIT line is emitted only once the single-instance guard has made this
-- instance the writer, so a duplicate app instance cannot add a second
-- `INIT ok` to the log. Everything before ` alpha=` keeps the v0.2 grammar.
local initLogged = false
local function announceInit()
  if initLogged then return end
  initLogged = true
  if runtime then
    appendLog(string.format(
      'INIT ok app=%s schema=%s frames=%d durationS=%.3f carId=%s trackId=%s log=%s alpha=%.3f syncLaps=%d syncOffsetS=%.3f',
      APP_VERSION, tostring(runtime.schema), #runtime.frames, runtimeDurationS,
      tostring(runtime.carId), tostring(runtime.trackId), tostring(LOG_PATH),
      currentAlpha(), syncLapsEnabled() and 1 or 0, currentStartOffsetS()))
  else
    appendLog(string.format(
      'INIT %s app=%s fallback=5m-follow player log=%s alpha=%.3f syncLaps=%d syncOffsetS=%.3f',
      tostring(runtimeError), APP_VERSION, tostring(LOG_PATH), currentAlpha(),
      syncLapsEnabled() and 1 or 0, currentStartOffsetS()))
    ac.warn('GhostCarGenerator Hotlap: '..tostring(runtimeError)..'; using 5 m fallback')
  end
end

-- ---------------------------------------------------------------------------
-- Render oracle
-- ---------------------------------------------------------------------------

-- `render.projectPoint()` is only reliable inside render context, so the
-- projection is sampled from a render callback and cached for the 1 Hz log row.
-- The pose update runs in `main.root.opaque` (before both opaque and
-- transparent geometry passes) when available, falling back to `RENDER_EVENT`.
local function installRenderOracle()
  if type(render) ~= 'table' or type(render.on) ~= 'function' then
    appendLog('RENDERHOOK unavailable render.on')
    return
  end
  local function runRenderPose()
    if not state.root or not state.renderPoseActive then return end
    local okPose = pcall(updatePoseInRender)
    if not okPose and not state.poseErrorLogged then
      state.poseErrorLogged = true
      appendLog('SMOOTH pose-error render-context')
    end
  end

  local okPoseHook, pSub = pcall(render.on, POSE_RENDER_EVENT, function()
    runRenderPose()
  end)
  if okPoseHook and pSub then
    poseSubscription = pSub
    poseRenderEvent = POSE_RENDER_EVENT
  else
    poseRenderEvent = RENDER_EVENT
  end

  local ok, subscription = pcall(render.on, RENDER_EVENT, function()
    oracle.hookFrames = oracle.hookFrames + 1
    if not state.root then return end
    if not oracle.hookAnnounced then
      oracle.hookAnnounced = true
      appendLog(string.format('RENDERHOOK ok event=%s frames=%d',
        RENDER_EVENT, oracle.hookFrames))
    end
    if not poseSubscription then
      runRenderPose()
    end
    local position = vec3(state.ghostX, state.ghostY, state.ghostZ)
    local point = nil
    pcall(function() point = render.projectPoint(position) end)
    if point then
      oracle.ndcX, oracle.ndcY = point.x, point.y
    end
    if debugCrossEnabled() and ghostVisibleEnabled() then
      pcall(render.debugCross, position, GHOST_NODE_RADIUS * 0.5, rgbm(2, 0, 0, 1))
    end
  end)
  if ok and subscription then
    renderSubscription = subscription
  else
    appendLog('RENDERHOOK failed '..tostring(subscription))
  end
end

installRenderOracle()

-- ---------------------------------------------------------------------------
-- Math
-- ---------------------------------------------------------------------------

local function wrapAngle(angle)
  local wrapped = angle % (2 * math.pi)
  if wrapped > math.pi then
    wrapped = wrapped - 2 * math.pi
  end
  return wrapped
end

local function dist3(ax, ay, az, bx, by, bz)
  local dx, dy, dz = ax - bx, ay - by, az - bz
  return math.sqrt(dx * dx + dy * dy + dz * dz)
end

-- `os.preciseClock()` (ac_apps/lib.lua:4427) is "time in seconds from script
-- start (with high precision)" and has no per-frame granularity. `os.clock()`
-- (CPU seconds) is only a last resort; `sim.gameTime` is deliberately NOT used
-- here because it is the quantised value this smoothing exists to bypass.
local HAS_PRECISE_CLOCK = type(os.preciseClock) == 'function'
local function preciseNow()
  if HAS_PRECISE_CLOCK then
    local ok, value = pcall(os.preciseClock)
    if ok and type(value) == 'number' and value == value then return value end
  end
  return os.clock()
end

-- ---------------------------------------------------------------------------
-- Scene setup
-- ---------------------------------------------------------------------------

local function setError(stage, message)
  if state.status == 'ERROR' and state.errorMessage == tostring(message) then
    return
  end
  state.status = 'ERROR'
  state.errorMessage = tostring(message)
  ac.warn(string.format('GhostCarGenerator Hotlap [%s]: %s', stage, tostring(message)))
  appendLog(string.format('ERROR %s %s', stage, tostring(message)))
end

local function disposeGhost()
  local root = state.root
  state.root = nil
  state.model = nil
  state.wheels = {}
  state.ghostMeshSets = {}
  state.appliedAlpha = nil
  state.appliedVisible = nil
  state.poseApplied = false
  -- v0.5: the render path must stop posing a node that no longer exists, and the
  -- next node starts from a clean clock (its first pose is a re-anchor, not a
  -- 1 km step).
  state.renderPoseActive = false
  state.renderPoseFrames = 0
  state.lastRenderPoseWall = nil
  state.renderFallbackLogged = false
  state.activeTicks = 0
  state.prevPose = nil
  state.renderLastPrecise = nil
  state.displayTime = 0
  state.reanchor = true
  state.playRate = 1
  state.rateStepAcc = 0
  state.rateWallAcc = 0
  state.lastTickPrecise = nil
  state.steerApplied = nil
  if root then
    pcall(function() root:dispose() end)
  end
end

-- ---------------------------------------------------------------------------
-- Single-instance guard
-- ---------------------------------------------------------------------------
-- Two copies of this app in one session each keep their own playback clock and
-- append to the same CSV, which is the v0.2 interleaved-row bug. The SDK's
-- session-shared storage is documented for exactly this ("a way for different
-- scripts to exchange data", ac_apps/lib.lua:3953-3966), so the writer role is
-- leased there. Election is claim -> settle -> re-read:
--
--   1. write `<gen>|<id>|<wallSeconds>|<sessionToken>`;
--   2. wait INSTANCE_SETTLE_S;
--   3. re-read. If our id survived we are the writer; if a fresher foreign
--      lease survived we stand by BEFORE creating a node or logging a row.
--
-- The writer refreshes the lease every second, re-asserts it when the storage
-- was cleared by a session restart, and stands down if a duplicate holds a
-- fresher lease. A dead writer is replaced after INSTANCE_LEASE_S of silence.
--
-- Why `ac.store` and not a real lock: the SDK exposes no mutex/atomic CAS.
-- This is a lease protocol, so the residual race window is INSTANCE_SETTLE_S
-- at a simultaneous cold start, and the shared store is per-session, not
-- per-machine. The file backend (same record format, next to the log) is used
-- only when `ac.store` is missing; it extends coverage to a separate process
-- but has the same settle-window caveat. Backend switch: force this expression
-- to 'file' to arbitrate across processes.
local INSTANCE_BACKEND = (type(ac.store) == 'function' and type(ac.load) == 'function')
  and 'shared' or 'file'
local INSTANCE_FILE = nil
if LOG_PATH then
  INSTANCE_FILE = LOG_PATH:gsub('%.log$', '')..'.owner'
end

local function wallNow()
  local ok, value = pcall(os.time)
  if ok and type(value) == 'number' then return value end
  -- Fallbacks keep the lease monotonic when os.time is unavailable.
  local sim = nil
  pcall(function() sim = ac.getSim() end)
  local system = sim and sim.systemTime
  if type(system) == 'number' then return system end
  return math.floor(state.clock)
end

local function makeInstanceId()
  -- Wall second + sub-second CPU stamp + Lua state address. Two instances in
  -- the same process have distinct heaps, so the address disambiguates even a
  -- same-second load.
  local address = tostring({}):match('(%x+)$') or '0'
  local cpu = 0
  pcall(function() cpu = os.clock() end)
  local micro = math.floor((cpu % 1) * 1000000)
  return string.format('%d-%06d-%s', wallNow(), micro, address)
end

local instance = {
  id = makeInstanceId(),
  gen = 0,
  role = 'claiming',
  sessionToken = nil,
  claimedAt = 0,
  claimAttempts = 0,
  lastBeatSecond = nil,
  lastPollSecond = nil,
  available = INSTANCE_BACKEND == 'shared' or INSTANCE_FILE ~= nil,
}

local function readOwnerRecord()
  if INSTANCE_BACKEND == 'shared' then
    local ok, value = pcall(ac.load, INSTANCE_KEY)
    if ok then return value end
    return nil
  end
  if not INSTANCE_FILE then return nil end
  local ok, value = pcall(io.load, INSTANCE_FILE, '')
  if ok and type(value) == 'string' then
    return (value:gsub('%s+$', ''))
  end
  return nil
end

local function writeOwnerRecord(record)
  if INSTANCE_BACKEND == 'shared' then
    return (pcall(ac.store, INSTANCE_KEY, record))
  end
  if not INSTANCE_FILE then return false end
  local file = io.open(INSTANCE_FILE, 'w')
  if not file then return false end
  file:write(record..'\n')
  file:close()
  return true
end

local function parseOwnerRecord(record)
  if type(record) ~= 'string' or record == '' then return nil end
  local gen, id, stamp, token = record:match('^(%d+)|([^|]+)|(%d+)|(.*)$')
  if not gen then return nil end
  return {
    gen = tonumber(gen) or 0,
    id = id,
    stamp = tonumber(stamp) or 0,
    token = token,
  }
end

local function logStandby(record, extra)
  appendLog(string.format(
    'INSTANCE standby, another writer active id=%s gen=%d ageS=%d backend=%s%s',
    tostring(record and record.id or '?'), record and record.gen or 0,
    record and math.max(0, wallNow() - record.stamp) or -1,
    INSTANCE_BACKEND, extra and (' '..extra) or ''))
end

local function beginClaim(reason, previous)
  local record = previous or parseOwnerRecord(readOwnerRecord())
  instance.gen = (record and record.gen or 0) + 1
  instance.claimedAt = state.clock + INSTANCE_SETTLE_S
  instance.claimAttempts = instance.claimAttempts + 1
  instance.role = 'claiming'
  local wrote = writeOwnerRecord(string.format('%d|%s|%d|%s',
    instance.gen, instance.id, wallNow(), tostring(instance.sessionToken or '0')))
  if not wrote then
    instance.available = false
    instance.role = 'writer'
    appendLog('INSTANCE unavailable store-write-failed; running unguarded')
    announceInit()
    return
  end
  if reason == 'takeover' then
    appendLog(string.format(
      'INSTANCE takeover id=%s genOld=%d ageS=%d gen=%d',
      tostring(record and record.id or 'free'), record and record.gen or 0,
      record and math.max(0, wallNow() - record.stamp) or -1, instance.gen))
  end
end

-- Returns the current role: 'claiming' | 'writer' | 'standby'. Only 'writer'
-- may create/pose the node or append data rows.
local function guardTick()
  if not instance.available then
    instance.role = 'writer'
    announceInit()
    return 'writer'
  end

  local sim = ac.getSim()
  instance.sessionToken = tostring(sim and sim.currentSessionIndex or 0)
  local wall = wallNow()

  if instance.role == 'claiming' then
    if instance.gen == 0 then
      -- First tick: never disturb a writer that already holds a fresh lease
      -- (this is the "session restart loaded a second app" case), otherwise
      -- write our claim and let the settle window arbitrate a cold start.
      local existing = parseOwnerRecord(readOwnerRecord())
      if existing and existing.id ~= instance.id
        and (wall - existing.stamp) <= INSTANCE_LEASE_S then
        instance.role = 'standby'
        logStandby(existing)
        return 'standby'
      end
      beginClaim('initial', existing)
      return instance.role
    end
    if state.clock < instance.claimedAt then return 'claiming' end
    local record = parseOwnerRecord(readOwnerRecord())
    if record and record.id == instance.id then
      instance.role = 'writer'
      instance.lastBeatSecond = wall
      appendLog(string.format('INSTANCE writer id=%s gen=%d backend=%s attempts=%d',
        instance.id, instance.gen, INSTANCE_BACKEND, instance.claimAttempts))
      announceInit()
      return 'writer'
    end
    if record and (wall - record.stamp) <= INSTANCE_LEASE_S then
      instance.role = 'standby'
      logStandby(record)
      return 'standby'
    end
    if instance.claimAttempts >= 3 then
      -- The store keeps losing our write (e.g. it is per-instance, not
      -- shared). Prefer a working ghost over a silent app, and say so.
      instance.available = false
      instance.role = 'writer'
      appendLog('INSTANCE unavailable store-unverified; running unguarded')
      announceInit()
      return 'writer'
    end
    beginClaim('takeover', record)
    return instance.role
  end

  if instance.role == 'writer' then
    if not instance.lastBeatSecond
      or wall - instance.lastBeatSecond >= INSTANCE_HEARTBEAT_S then
      instance.lastBeatSecond = wall
      local record = parseOwnerRecord(readOwnerRecord())
      if record and record.id ~= instance.id
        and (wall - record.stamp) <= INSTANCE_LEASE_S then
        -- A duplicate won a newer claim: retire this writer immediately.
        disposeGhost()
        state.status = 'INIT'
        state.mode = nil
        state.materialMode = nil
        instance.role = 'standby'
        logStandby(record)
        return 'standby'
      end
      -- Re-assert even when the record is missing: a session restart may have
      -- cleared the shared store while this instance kept running.
      writeOwnerRecord(string.format('%d|%s|%d|%s',
        instance.gen, instance.id, wall, instance.sessionToken))
    end
    return 'writer'
  end

  -- standby: poll for a dead/free lease and take over.
  if not instance.lastPollSecond
    or wall - instance.lastPollSecond >= INSTANCE_HEARTBEAT_S then
    instance.lastPollSecond = wall
    local record = parseOwnerRecord(readOwnerRecord())
    if not record or (wall - record.stamp) > INSTANCE_LEASE_S then
      instance.claimAttempts = 0
      beginClaim('takeover', record)
      return instance.role
    end
  end
  return 'standby'
end


-- `ac.findFile()` (SDK ac_apps/lib.lua:3160) searches the script folder, then
-- the CSP folder, then the AC root, and returns an absolute path; a path
-- relative to the AC root is the documented form for `loadKN5()`.
local function resolveModelFilename()
  local resolved = nil
  pcall(function() resolved = ac.findFile(GHOST_MODEL_RELATIVE) end)
  if type(resolved) == 'string' and resolved ~= '' and io.fileExists(resolved) then
    return resolved
  end
  local ok, carsFolder = pcall(function() return ac.getFolder(ac.FolderID.ContentCars) end)
  if ok and type(carsFolder) == 'string' and carsFolder ~= '' then
    local fallback = carsFolder:gsub('[/\\]+$', '')..'/'
      ..GHOST_MODEL_RELATIVE:gsub('^content/cars/', '')
    if io.fileExists(fallback) then
      return fallback
    end
  end
  return nil
end

-- One-shot introspection so "node exists but renders nothing" is visible in
-- the log without a camera: valid handles (`isActive`), child counts, and the
-- mesh AABB (proves geometry was attached, at the expected metre scale, and
-- still flagged visible).
local function logNodeIntrospection(node, model)
  local parts = { 'NODE ok app='..APP_VERSION }
  local function add(key, fn)
    local ok, value = pcall(fn)
    if not ok then
      parts[#parts + 1] = key..'=err'
      return
    end
    parts[#parts + 1] = key..'='..sanitizeToken(value)
  end
  add('rootActive', function() return node:isActive() and 1 or 0 end)
  add('rootChildren', function() return node:getChildrenCount() end)
  add('modelEmpty', function() return model:empty() and 1 or 0 end)
  add('modelName', function() return model:name() end)
  add('modelActive', function() return model:isActive() and 1 or 0 end)
  add('modelChildren', function() return model:getChildrenCount() end)

  -- AABB/visibility are mesh-only calls, so resolve the model's meshes first
  -- (the same filter-style lookup the traffic tool uses on a loaded KN5).
  local meshes = nil
  local okMeshes, found = pcall(function() return model:findMeshes('*') end)
  if okMeshes and found then
    local okSize, size = pcall(function() return found:size() end)
    if okSize and type(size) == 'number' and size > 0 then
      meshes = found
      meshCount = size
      parts[#parts + 1] = 'meshes='..sanitizeToken(size)
    end
  end
  if meshes then
    local okVisible, visible = pcall(function() return meshes:isVisible(1) end)
    parts[#parts + 1] = 'meshVisible='..(okVisible and sanitizeToken(visible and 1 or 0) or 'err')
  end

  local target = meshes or model
  local aabbOk, minPos, maxPos, meshCount = pcall(function()
    return target:getLocalAABB()
  end)
  if aabbOk and minPos and maxPos then
    parts[#parts + 1] = string.format('aabbSize=%.2fx%.2fx%.2f',
      maxPos.x - minPos.x, maxPos.y - minPos.y, maxPos.z - minPos.z)
    parts[#parts + 1] = 'aabbMeshes='..sanitizeToken(meshCount)
  else
    parts[#parts + 1] = 'aabb=unavailable'
  end
  parts[#parts + 1] = 'wheels='..sanitizeToken(#state.wheels)
  parts[#parts + 1] = 'model='..sanitizeToken(state.modelPath)
  appendLog(table.concat(parts, ' '))
end

-- Ghost look. The SDK has no per-material opacity (`ac.SceneReference` exposes
-- setMaterialProperty/setMaterialTexture but nothing like setOpacity; see
-- ac_apps/lib.lua:9610-9681), so alpha has to come from the diffuse texture
-- slot: that is the channel AC's transparent materials (windscreens) use, and
-- the SDK's own solid-color example is lib.lua:9641-9650.
-- We write one flat translucent texel into txDiffuse, mark the meshes as
-- transparent so they are drawn in the transparent pass (lib.lua:10064), and
-- switch the blend mode (lib.lua:9874). setShadows(false) keeps the ghost from
-- casting a solid shadow through its own transparency (lib.lua:9858).
--
-- Scope: ensureUniqueMaterials() (lib.lua:9677) is the guarantee that this
-- never touches the player car or another car. loadKN5 pulls from a shared
-- pool (lib.lua:9797-9800), so editing a pooled material without it could leak.
-- Mesh filters. findMeshes() takes `key:value` filters ('*' alone matches
-- nothing); there is no documented match-all, so cover the car by the shader
-- slots cars actually use. The traffic tool proves
-- 'shader:ksPerPixelMultiMap_damage_dirt' for paint; the plain variants cover
-- converted/modded models like this GR86. Per-filter counts are logged, so
-- coverage vs aabbMeshes is auditable in a single run.
local GHOST_MESH_FILTERS = {
  { filter = 'shader:ksPerPixelMultiMap_damage_dirt', tint = GHOST_TINT_BODY, alphaScale = 1.0 },
  { filter = 'shader:ksPerPixelMultiMap_damage',      tint = GHOST_TINT_BODY, alphaScale = 1.0 },
  { filter = 'shader:ksPerPixelMultiMap_NMDetail',    tint = GHOST_TINT_BODY, alphaScale = 1.0 },
  { filter = 'shader:ksPerPixelMultiMap',             tint = GHOST_TINT_BODY, alphaScale = 1.0 },
  { filter = 'shader:ksPerPixelReflection',           tint = GHOST_TINT_BODY, alphaScale = 0.9 },
  { filter = 'shader:ksPerPixel',                     tint = GHOST_TINT_BODY, alphaScale = 1.0 },
  { filter = 'shader:ksPerPixelNM',                   tint = GHOST_TINT_DARK, alphaScale = 0.85 },
  { filter = 'shader:ksPerPixelAT_NM',                tint = GHOST_TINT_DARK, alphaScale = 0.75 },
  { filter = 'shader:ksPerPixelAT',                   tint = GHOST_TINT_DARK, alphaScale = 0.75 },
  { filter = 'shader:ksWindscreen',                   tint = GHOST_TINT_GLASS, alphaScale = 0.85 },
  { filter = 'shader:ksGlass',                        tint = GHOST_TINT_GLASS, alphaScale = 0.85 },
  { filter = 'shader:ksBrakeDisc',                    tint = GHOST_TINT_DARK, alphaScale = 0.80 },
  { filter = 'shader:ksTyres',                        tint = GHOST_TINT_DARK, alphaScale = 0.95 },
  { filter = 'shader:ksTyre',                         tint = GHOST_TINT_DARK, alphaScale = 0.95 },
  { filter = 'shader:ksWheel',                        tint = GHOST_TINT_DARK, alphaScale = 0.95 },
}

local function applyAlphaToTarget(target, alpha, tint, alphaScale)
  local effective = clamp01(alpha * (alphaScale or 1.0))
  local translucent = alpha < 0.999
  local blendMode = translucent and render.BlendMode.AlphaBlend
    or (render.BlendMode.Opaque or 0)
  local c = tint or GHOST_TINT_BODY
  pcall(function() target:setTransparent(translucent) end)
  pcall(function() target:setBlendMode(blendMode) end)
  pcall(function() target:setShadows(not translucent) end)
  pcall(function()
    target:setMaterialTexture('txDiffuse', rgbm(c[1], c[2], c[3], effective))
  end)
end

local function updateGhostAlpha(alpha)
  local clamped = clamp01(alpha)
  if state.appliedAlpha and math.abs(clamped - state.appliedAlpha) < 1e-3 then
    return
  end
  if not state.model or type(render) ~= 'table' or not render.BlendMode then
    return
  end
  applyAlphaToTarget(state.model, clamped, GHOST_TINT_BODY, 1.0)
  for _, entry in ipairs(state.ghostMeshSets) do
    applyAlphaToTarget(entry.meshes, clamped, entry.tint, entry.alphaScale)
  end
  state.appliedAlpha = clamped
  state.materialMode = clamped < 0.999 and 'alpha' or 'opaque'
end

local function applyGhostVisibility(visible)
  local want = visible ~= false
  if state.appliedVisible == want then return end
  if not state.root then return end
  pcall(function() state.root:setVisible(want, false) end)
  if state.model then
    pcall(function() state.model:setVisible(want, false) end)
  end
  state.appliedVisible = want
end

local function applyGhostMaterials(model)
  local alpha = currentAlpha()
  state.ghostMeshSets = {}
  if type(render) ~= 'table' or not render.BlendMode then
    return nil, 'render.BlendMode-missing'
  end
  local translucent = alpha < 0.999
  local blendMode = translucent and render.BlendMode.AlphaBlend
    or (render.BlendMode.Opaque or 0)

  -- Root pass first (base fallback for any uncategorised shader).
  local rsteps = {}
  local function rstep(name, fn)
    local okStep, err = pcall(fn)
    rsteps[name] = okStep and 'ok' or ('failed:'..tostring(err))
  end
  rstep('unique', function() model:ensureUniqueMaterials() end)
  rstep('transparent', function() model:setTransparent(translucent) end)
  rstep('blend', function() model:setBlendMode(blendMode) end)
  rstep('shadows', function() model:setShadows(not translucent) end)
  rstep('tint', function()
    model:setMaterialTexture('txDiffuse', rgbm(
      GHOST_TINT_BODY[1], GHOST_TINT_BODY[2], GHOST_TINT_BODY[3], alpha))
  end)
  appendLog(string.format(
    'GHOSTROOT unique=%s transparent=%s blend=%s shadows=%s tint=%s',
    sanitizeToken(rsteps.unique or '?'), sanitizeToken(rsteps.transparent or '?'),
    sanitizeToken(rsteps.blend or '?'), sanitizeToken(rsteps.shadows or '?'),
    sanitizeToken(rsteps.tint or '?')))

  local total, merged, detail = 0, {}, {}
  for _, spec in ipairs(GHOST_MESH_FILTERS) do
    local filter = spec.filter
    local okFind, found = pcall(function() return model:findMeshes(filter) end)
    local n = 0
    if okFind and found then
      local okSize, count = pcall(function() return found:size() end)
      if okSize and type(count) == 'number' then n = count end
    end
    detail[#detail + 1] = filter..'='..n
    if n > 0 then
      state.ghostMeshSets[#state.ghostMeshSets + 1] = {
        meshes = found,
        tint = spec.tint,
        alphaScale = spec.alphaScale,
      }
      local steps = {}
      local function step(name, fn)
        local okStep, err = pcall(fn)
        steps[name] = okStep and 'ok' or ('failed:'..tostring(err))
      end
      local c = spec.tint or GHOST_TINT_BODY
      local effAlpha = clamp01(alpha * (spec.alphaScale or 1.0))
      step('unique', function() found:ensureUniqueMaterials() end)
      step('transparent', function() found:setTransparent(translucent) end)
      step('blend', function() found:setBlendMode(blendMode) end)
      step('shadows', function() found:setShadows(not translucent) end)
      step('tint', function()
        found:setMaterialTexture('txDiffuse', rgbm(c[1], c[2], c[3], effAlpha))
      end)
      for k, v in pairs(steps) do
        if merged[k] == nil then merged[k] = v
        elseif v ~= 'ok' then merged[k] = v end
      end
      total = total + n
    end
  end
  state.appliedAlpha = alpha
  if total <= 0 then return nil, 'no-meshes' end
  return { count = total, mode = translucent and 'alpha' or 'opaque',
    steps = merged, filters = table.concat(detail, ',') }, nil
end

local function logGhostMaterials(info, err)
  local alpha = currentAlpha()
  if not info then
    state.materialMode = nil
    state.materialMeshes = 0
    state.materialError = tostring(err)
    appendLog('GHOST failed '..sanitizeToken(err))
    ac.warn('GhostCarGenerator Hotlap: transparency not applied ('..tostring(err)..')')
    return
  end
  state.materialMode = info.mode
  state.materialMeshes = info.count
  state.materialError = nil
  if info.mode == 'opaque' then
    appendLog(string.format('GHOST alpha=%.3f mode=opaque meshes=%d',
      alpha, info.count))
    return
  end
  local steps = info.steps or {}
  appendLog(string.format(
    'GHOST alpha=%.3f mode=alpha meshes=%d transparent=%s blend=%s shadows=%s tint=%s unique=%s filters=%s',
    alpha, info.count,
    sanitizeToken(steps.transparent or '?'), sanitizeToken(steps.blend or '?'),
    sanitizeToken(steps.shadows or '?'), sanitizeToken(steps.tint or '?'),
    sanitizeToken(steps.unique or '?'), sanitizeToken(info.filters or '?')))
  if steps.blend ~= 'ok' or steps.transparent ~= 'ok' then
    ac.warn('GhostCarGenerator Hotlap: alpha blend not fully applied (see GHOST line)')
  end
end

local function createGhostNode()
  local ok, node, modelOrError = pcall(function()
    local carsRoot = ac.findNodes('carsRoot:yes')
    if not carsRoot then
      return nil, 'carsRoot node is unavailable'
    end
    local created = carsRoot:createBoundingSphereNode(GHOST_NODE_NAME, GHOST_NODE_RADIUS)
    if not created then
      return nil, 'createBoundingSphereNode returned nil'
    end
    -- Only marks the node as car-like for car-specific code (BLIND_SPOT input
    -- and similar); it does not affect rendering (SDK ac_apps/lib.lua:10217).
    created:setVirtualCarFlag(true)
    local modelFilename = resolveModelFilename()
    if not modelFilename then
      pcall(function() created:dispose() end)
      return nil, 'model-not-found '..GHOST_MODEL_RELATIVE
    end
    local loaded = created:loadKN5(modelFilename)
    if not loaded then
      pcall(function() created:dispose() end)
      return nil, 'could not load '..modelFilename
    end
    -- Explicit visibility request; documented as a no-op when the object was
    -- already visible, so it is a cheap guard for "loaded but never drawn".
    pcall(function() loaded:setVisible(true, false) end)
    state.modelPath = modelFilename
    return created, loaded
  end)
  if not ok then
    return nil, nil, 'scene setup threw: '..tostring(node)
  end
  if not node then
    return nil, nil, tostring(modelOrError)
  end
  return node, modelOrError, nil
end

local function collectWheels(model)
  local wheels = {}
  if not model then return wheels end
  local names = { 'WHEEL_LF', 'WHEEL_RF', 'WHEEL_LR', 'WHEEL_RR' }
  for index, name in ipairs(names) do
    local ok, ref = pcall(function() return model:findNodes(name) end)
    if ok and ref then
      wheels[index] = ref
    end
  end
  return wheels
end

local function initialize()
  if state.failed then return false end
  if state.root then return true end
  if state.clock < state.nextInitAttemptAt then return false end

  local player = ac.getCar(0)
  if not player then return false end

  local node, model, err = createGhostNode()
  if not node then
    state.initFailures = state.initFailures + 1
    state.nextInitAttemptAt = state.clock + 1
    state.errorMessage = tostring(err or 'unknown scene error')
    if state.initFailures >= MAX_INIT_FAILURES then
      state.failed = true
      disposeGhost()
      setError('init', state.errorMessage)
    elseif state.initFailures == 1 then
      -- Log the first failure only; the retries stay silent to avoid spam.
      appendLog('WARN init '..state.errorMessage)
    end
    return false
  end

  state.root = node
  state.model = model
  state.wheels = collectWheels(model)
  state.status = 'ACTIVE'
  state.mode = runtime and 'runtime' or 'fallback'
  state.errorMessage = nil
  state.initFailures = 0
  state.runtimeTime = 0
  state.runtimeCursor = 1
  state.simGameTime = nil
  state.logAccum = LOG_INTERVAL_S -- log the first sample immediately
  -- v0.5: a new node/clock starts with a re-anchor and a clean smoothness window
  -- (otherwise the first SMOOTH line would report the load teleport as a step).
  state.displayTime = 0
  state.renderLastPrecise = nil
  state.prevPose = nil
  state.playRate = 1
  state.rateStepAcc = 0
  state.rateWallAcc = 0
  state.lastTickPrecise = nil
  state.activeTicks = 0
  state.renderPoseFrames = 0
  state.lastRenderPoseWall = nil
  state.reanchor = true
  state.smooth.windowS = 0
  state.smooth.maxStepM = 0
  state.smooth.sumStepM = 0
  state.smooth.stepCount = 0
  state.smooth.maxYawRad = 0
  state.smooth.frames = 0
  state.smooth.ticks = 0
  state.smooth.resets = 0
  logNodeIntrospection(node, model)
  local materialInfo, materialError = applyGhostMaterials(model)
  logGhostMaterials(materialInfo, materialError)
  applyGhostVisibility(ghostVisibleEnabled())
  appendLog(string.format('ACTIVE mode=%s frames=%d hook=%s alpha=%.3f log=%s',
    tostring(state.mode), runtime and #runtime.frames or 0,
    oracle.hookAnnounced and 'ok' or 'none', currentAlpha(), tostring(LOG_PATH)))
  appendLog(string.format('SMOOTHPATH pose=%s event=%s windowS=%.1f',
    (poseSubscription or renderSubscription) and 'render' or 'tick',
    poseRenderEvent, SMOOTH_LOG_WINDOW_S))
  return true
end

local function resetForSession(token)
  disposeGhost()
  state.sessionToken = token
  state.status = 'INIT'
  state.mode = nil
  state.errorMessage = nil
  state.failed = false
  state.initFailures = 0
  state.nextInitAttemptAt = 0
  state.runtimeTime = 0
  state.runtimeCursor = 1
  state.simGameTime = nil
  state.logAccum = 0
  state.materialMode = nil
  state.materialMeshes = 0
  state.materialError = nil
  -- v0.4: the session token change means a new session, so any lap baseline
  -- belongs to the old one. Clearing it guarantees the first crossing after the
  -- restart re-syncs once (instead of comparing against a stale lap number and
  -- either double-firing or missing the crossing).
  state.lapBaseline = nil
  state.lapRestarts = 0
  state.lapSyncUnavailable = false
  state.prevSpline = nil
  state.prevResetCounter = nil
  state.lastLapSyncWallS = -1e9
  state.playerLaunched = false
  state.holdingForLaunch = false
  -- v0.5: `disposeGhost()` already dropped the pose path; the phase clock restarts
  -- from zero with the new session.
  state.displayTime = 0
  state.reanchor = true
  appendLog(string.format('SESSION token=%s syncLaps=%d',
    tostring(token), syncLapsEnabled() and 1 or 0))
end

-- ---------------------------------------------------------------------------
-- Pose application
-- ---------------------------------------------------------------------------
-- `applyPose` is the only place the ghost node is moved, and since v0.5 it is
-- called from the render callback (once per drawn frame) or, if `render.on`
-- could not be installed, from the app tick. It also measures the displacement
-- between consecutive applications, which is what the SMOOTH line reports: a
-- smooth ghost has maxStepM ≈ speed / renderHz, a stepped or teleporting one
-- shows a value far above that.
local function applyPose(x, y, z, yaw, pitch)
  local wrappedYaw = wrapAngle(yaw)
  local adjustedY = y + currentHeightOffsetM()
  local p = (type(pitch) == 'number' and pitch == pitch) and pitch or 0
  local cosP, sinP = math.cos(p), math.sin(p)
  local sinY, cosY = math.sin(wrappedYaw), math.cos(wrappedYaw)
  state.root:setPosition(vec3(x, adjustedY, z))
  state.root:setOrientation(
    vec3(-sinY * cosP, sinP, cosY * cosP),
    vec3(sinY * sinP, cosP, -cosY * sinP))
  if not state.poseApplied then
    state.poseApplied = true
    -- The first pose teleports the node hundreds of metres away from the scene
    -- origin; without this the interpolated motion state is garbage for a
    -- frame (SDK ac_apps/lib.lua:9687).
    pcall(function() state.root:clearMotion() end)
  end
  local previous = state.prevPose
  if previous then
    local stepM = dist3(previous.x, previous.y, previous.z, x, adjustedY, z)
    if stepM > state.smooth.maxStepM then state.smooth.maxStepM = stepM end
    state.smooth.sumStepM = state.smooth.sumStepM + stepM
    state.smooth.stepCount = state.smooth.stepCount + 1
    local yawStep = math.abs(wrapAngle(wrappedYaw - previous.yaw))
    if yawStep > state.smooth.maxYawRad then state.smooth.maxYawRad = yawStep end
  end
  state.prevPose = { x = x, y = adjustedY, z = z, yaw = wrappedYaw }
  state.ghostX, state.ghostY, state.ghostZ = x, adjustedY, z
end

-- Returns the interpolated pose at `elapsed` timeline seconds plus the segment
-- speed and pitch. The interpolation itself is position lerp + shortest-arc
-- yaw across the ±pi seam + linear pitch and steer angle.
local function sampleRuntimeFrame(elapsed)
  local frames = runtime.frames
  local count = #frames
  local t = elapsed % runtimeDurationS

  if state.runtimeCursor > count or frames[state.runtimeCursor].timeS > t then
    state.runtimeCursor = 1
  end
  local index = state.runtimeCursor
  while index < count and frames[index + 1].timeS <= t do
    index = index + 1
  end
  state.runtimeCursor = index

  local a = frames[index]
  local b = frames[math.min(index + 1, count)]
  local span = b.timeS - a.timeS
  local k = span > 1e-6 and math.saturate((t - a.timeS) / span) or 0

  local x = a.positionM[1] + (b.positionM[1] - a.positionM[1]) * k
  local y = a.positionM[2] + (b.positionM[2] - a.positionM[2]) * k
  local z = a.positionM[3] + (b.positionM[3] - a.positionM[3]) * k

  -- Pitch (positive = uphill nose up) and yaw (shortest arc into [-pi, pi]).
  local pitchA = a.rotationRad[1] or 0
  local pitchB = b.rotationRad[1] or 0
  local pitch = pitchA + (pitchB - pitchA) * k
  local yawDelta = wrapAngle(b.rotationRad[2] - a.rotationRad[2])
  local yaw = wrapAngle(a.rotationRad[2] + yawDelta * k)

  -- Segment speed from the timeline itself (no schema change: the JSON carries
  -- positions and times). A jump of more than 50 m over one 15 ms frame is the
  -- loop seam, not motion, so keep the previous estimate.
  local speed = state.speedMps
  if span > 1e-6 then
    local segmentM = dist3(a.positionM[1], a.positionM[2], a.positionM[3],
      b.positionM[1], b.positionM[2], b.positionM[3])
    if segmentM < 50 then speed = segmentM / span end
  end
  if speed > MAX_PLAUSIBLE_SPEED_MPS then speed = MAX_PLAUSIBLE_SPEED_MPS end
  state.speedMps = speed

  local steerA = a.steerAngleDeg or 0
  local steerB = b.steerAngleDeg or 0
  local steerDeg = steerA + (steerB - steerA) * k
  state.steerRad = -math.rad(steerDeg) / GHOST_STEER_RATIO
  return x, y, z, yaw, speed, pitch
end

-- Steering is absolute, so only push it when it actually changed (four native
-- calls per frame would otherwise run at the render rate for nothing).
local function setSteerAngle(steerRad)
  if state.steerApplied and math.abs(steerRad - state.steerApplied) < 1e-4 then
    return
  end
  state.steerApplied = steerRad
  local wheels = state.wheels
  if wheels[1] and wheels[2] then
    local sx, sz = math.sin(steerRad), math.cos(steerRad)
    pcall(function()
      wheels[1]:setOrientation(vec3(sx, 0, sz))
      wheels[2]:setOrientation(vec3(sx, 0, sz))
    end)
  end
end

-- Wheel roll used to be fed a constant `step * 5` rad/s (≈30x too slow at race
-- speed, and it inherited every app-tick delta irregularity). It is now the
-- physical integral over the playback interval just consumed: the same wheel
-- angle whatever the frame rate, and no visible judder when tick deltas vary.
local function advanceWheels(speedMps, dtPlayback)
  local wheels = state.wheels
  if not (wheels[3] and wheels[4]) then return end
  if type(dtPlayback) ~= 'number' or dtPlayback <= 0 then return end
  if type(speedMps) ~= 'number' or speedMps ~= speedMps then
    speedMps = state.speedMps
  end
  if speedMps > MAX_PLAUSIBLE_SPEED_MPS then speedMps = MAX_PLAUSIBLE_SPEED_MPS end
  local rollDelta = speedMps * dtPlayback / GHOST_WHEEL_RADIUS_M
  -- Cap a single step at half a turn: past that the rim reads as jitter rather
  -- than rotation anyway, and one long frame must not spin it a whole turn.
  if rollDelta > math.pi then rollDelta = math.pi end
  if rollDelta <= 0 then return end
  pcall(function()
    wheels[3]:rotate(vec3(1, 0, 0), rollDelta)
    wheels[4]:rotate(vec3(1, 0, 0), rollDelta)
  end)
end

-- v0.5: the per-render-frame pose path. `state.runtimeTime` is the authoritative
-- timeline phase (app tick + lap sync); this turns it into a continuous time and
-- applies the interpolated pose once per drawn frame.
--
-- Two reasons this is smooth where the app tick was not:
--   * the sample rate is the render rate (100+ Hz), not the app tick rate;
--   * the time advanced per frame comes from `os.preciseClock()`, so a
--     `sim.gameTime` that only moves in game-step chunks cannot quantise it.
-- The phase lock is what keeps it honest: the display may lead the authoritative
-- phase by at most LEAD_MAX_S, smaller lags than PHASE_DEADBAND_S are ignored
-- (that is what makes a coarse `sim.gameTime` harmless), real lag is corrected
-- at a capped rate, and anything past PHASE_SNAP_S is a deliberate re-anchor
-- (first frame, session reset, lap restart) counted as `resets=` instead of
-- being smoothed into a slow slide.
updatePoseInRender = function()
  if not state.renderPoseActive or not state.root or not runtime then return end

  local now = preciseNow()
  local last = state.renderLastPrecise
  state.renderLastPrecise = now
  if last == nil then return end

  local wall = now - last
  if wall < 0 then wall = 0 end
  if wall > MAX_RENDER_WALL_S then wall = MAX_RENDER_WALL_S end

  local previousDisplay = state.displayTime
  local display = previousDisplay + wall * state.playRate
  local snapped = false
  local stepPlayback = 0

  if state.holdingForLaunch then
    display = state.runtimeTime
    stepPlayback = 0
    state.prevPose = nil
  elseif state.reanchor then
    state.reanchor = false
    snapped = true
  else
    -- Shortest signed distance on the timeline circle (loop wrap included).
    local lead = display - state.runtimeTime
    local half = runtimeDurationS * 0.5
    if lead > half then lead = lead - runtimeDurationS
    elseif lead < -half then lead = lead + runtimeDurationS end

    if lead > LEAD_MAX_S then
      -- Never run ahead of the authoritative phase (see LEAD_MAX_S).
      display = state.runtimeTime + LEAD_MAX_S
    elseif lead < -PHASE_SNAP_S then
      snapped = true
    elseif lead < -PHASE_DEADBAND_S then
      -- Real lag only: nudge forward at a capped rate so the correction itself
      -- can never look like a speed change.
      local fix = -lead * PHASE_GAIN
      local cap = wall * PHASE_SPEED_CAP
      if fix > cap then fix = cap end
      display = display + fix
    end
  end

  if snapped then
    display = state.runtimeTime
    state.smooth.resets = state.smooth.resets + 1
    -- Do not measure a step across a deliberate re-anchor.
    state.prevPose = nil
  else
    stepPlayback = display - previousDisplay
    if stepPlayback < 0 then stepPlayback = stepPlayback + runtimeDurationS end
    if stepPlayback > runtimeDurationS * 0.5 then
      -- A clamp or correction must never move the ghost backwards mid-lap.
      display = previousDisplay
      stepPlayback = 0
    end
  end
  display = display % runtimeDurationS
  state.displayTime = display

  local x, y, z, yaw, speed, pitch = sampleRuntimeFrame(display)
  applyPose(x, y, z, yaw, pitch)
  setSteerAngle(state.steerRad)
  advanceWheels(speed, stepPlayback)
  state.lastRenderPoseWall = now
  state.renderPoseFrames = state.renderPoseFrames + 1
  state.smooth.frames = state.smooth.frames + 1
end

-- ---------------------------------------------------------------------------
-- Per-frame update
-- ---------------------------------------------------------------------------

-- Playback must track real session time, not app frame time: app `dt` drops
-- frames and made the ghost clock lag game time by ~1 s per lap, which breaks
-- any "screenshot when ghost-time == X" rule.
local function advanceClock(fallback)
  local sim = ac.getSim()
  local now = sim and sim.gameTime
  if type(now) == 'number' then
    local previous = state.simGameTime
    state.simGameTime = now
    if previous and now >= previous then
      local delta = now - previous
      if delta <= 1.0 then
        if state.clockSource ~= 'sim' then
          state.clockSource = 'sim'
          appendLog('CLOCK sim')
        end
        return delta
      end
    end
  end
  -- Skip the first couple of frames: there is no previous sample to diff yet.
  if state.clockSource ~= 'app' and state.clock > 2 then
    state.clockSource = 'app'
    appendLog('CLOCK app')
  end
  return fallback
end

-- ---------------------------------------------------------------------------
-- Lap synchronisation (v0.4)
-- ---------------------------------------------------------------------------
-- The player's lap boundary is `ac.getCar(0).lapCount`, an integer that AC
-- increments on the start/finish crossing. This is exactly the signal the
-- installed ac-tracer app uses:
--   * apps/lua/ac-tracer/lib/core/state.lua:1263
--       `if car.lapCount > state.lapNumber then` -> lap completed, then
--       `state.lapNumber = car.lapCount` (line 1322);
--   * apps/lua/ac-tracer/lib/windows/delta_bar.lua:79
--       `if car.lapCount ~= prevLapCount then` -> same counter, UI-side.
-- The surrounding fields ac-tracer reads live on the same `ac.StateCar` object:
-- `car.lapTimeMs`, `car.previousLapTimeMs`, `car.isLastLapValid`,
-- `car.splinePosition`, `car.isInPitlane`/`car.isInPit`, `car.resetCounter`,
-- and `ac.onCarJumped(0, fn)` for teleports (state.lua:1225). Only `lapCount`
-- is used for the restart trigger here, on purpose (policy below).
--
-- Policy:
--   * Increment -> restart playback. Forward-only: `count > baseline`.
--   * Teleport / respawn / back to pits -> NO restart. Those do not change
--     `lapCount` and do not put the player at t=0 of a lap, so re-zeroing the
--     ghost there would make it lead or lag by the distance already driven on
--     the current lap. The ghost is anchored to the timing line, nothing else.
--   * Invalidated lap (track limits, cut, contact) -> still restart. The ghost
--     is a visual reference line, not a lap validator; a cut lap still crosses
--     the line and the next lap must start with both cars together.
--   * `lapCount` going backwards (session restart, car/session change) -> rebase
--     the baseline without restarting, and let the next increment fire once.
--     `resetForSession()` clears the baseline for the same reason.
--   * A jump of more than one (teleport across the line inside one frame) is
--     still one restart; the skipped count is logged as `jumps=` because the
--     intermediate crossings cannot be separated.
local function findNearestTimelineTime(px, py, pz)
  if not runtime then return nil, math.huge end
  local frames = runtime.frames
  local count = #frames
  local bestIdx = 1
  local bestD2 = math.huge
  for i = 1, count, 5 do
    local p = frames[i].positionM
    local dx, dz = p[1] - px, p[3] - pz
    local d2 = dx * dx + dz * dz
    if d2 < bestD2 then
      bestD2 = d2
      bestIdx = i
    end
  end
  local lo = math.max(1, bestIdx - 5)
  local hi = math.min(count, bestIdx + 5)
  for i = lo, hi do
    local p = frames[i].positionM
    local dx, dz = p[1] - px, p[3] - pz
    local d2 = dx * dx + dz * dz
    if d2 < bestD2 then
      bestD2 = d2
      bestIdx = i
    end
  end
  return frames[bestIdx].timeS, math.sqrt(bestD2)
end

local function lapStartCursorS()
  local offset = currentStartOffsetS() % runtimeDurationS
  -- offset > 0 pulls playback back by that many seconds: the frame shown at the
  -- crossing is the one that sits ON the line, not frame 0. leadS puts the
  -- ghost slightly ahead of the player (default 0.5 s, like AC's built-in ghost).
  return (runtimeDurationS - offset + currentLeadS()) % runtimeDurationS
end

-- Same code path as the loop wrap: `runtimeTime` stays a time inside
-- `[0, duration)`, and the frame cursor is re-armed (`= 1`) rather than left
-- pointing at a later frame, so `ghostT` in the next data row is honest.
local function restartPlaybackForLap(playerLap, nowS, jumps, reason)
  local cursor = lapStartCursorS()
  state.runtimeTime = cursor
  state.runtimeCursor = 1
  state.holdingForLaunch = false
  state.playerLaunched = true
  state.lastLapSyncWallS = nowS
  -- v0.5: this is a deliberate timeline jump, so the render clock must re-anchor
  -- on it instead of phase-locking (which would slide the ghost backwards
  -- through the whole lap) and the jump must not be measured as a SMOOTH step.
  state.reanchor = true
  state.lapRestarts = state.lapRestarts + 1
  appendLog(string.format(
    'LAPSYNC playerLap=%s ghostRestarted t=0 wallS=%s cursorS=%.3f offsetS=%.3f leadS=%.2f jumps=%d reason=%s',
    tostring(playerLap), fmtNumber(nowS), cursor, currentStartOffsetS(),
    currentLeadS(), jumps, tostring(reason or 'lapCount')))
end

local function snapGhostToPlayer(nowS, reason)
  if not runtime then return false end
  local nearestT, distM = findNearestTimelineTime(state.playerX, state.playerY, state.playerZ)
  if not nearestT then return false end
  local cursor = (nearestT + currentLeadS()) % runtimeDurationS
  state.runtimeTime = cursor
  state.runtimeCursor = 1
  state.reanchor = true
  state.holdingForLaunch = false
  state.playerLaunched = true
  appendLog(string.format(
    'LAPSYNC snap wallS=%s cursorS=%.3f distM=%.1f leadS=%.2f reason=%s',
    fmtNumber(nowS), cursor, distM, currentLeadS(), tostring(reason or 'manual')))
  return true
end

local function lapSyncTick(player, nowS)
  if not runtime then return end
  if not syncLapsEnabled() then
    -- Toggled off: forget the baseline so re-enabling rebases instead of
    -- firing a restart for a crossing that happened while free-running.
    state.lapBaseline = nil
    state.prevSpline = nil
    state.holdingForLaunch = false
    return
  end

  local speedKmh = (player and type(player.speedKmh) == 'number') and player.speedKmh or 0
  local resetCounter = player and player.resetCounter
  local spline = (player and type(player.splinePosition) == 'number') and player.splinePosition or nil
  local inPit = (player and (player.isInPit or player.isInPitlane)) and true or false
  local resetChanged = (state.prevResetCounter ~= nil and resetCounter ~= nil
    and resetCounter ~= state.prevResetCounter)
  if resetCounter ~= nil then
    state.prevResetCounter = resetCounter
  end

  -- If the player respawned stationary (e.g. Restart / Back to Hotlap Start),
  -- re-arm launch-hold so the ghost waits right ahead of them instead of
  -- running hundreds of metres away while they sit in the menu.
  if resetChanged and speedKmh < LAUNCH_SPEED_KMH then
    state.playerLaunched = false
    state.prevSpline = spline
  end

  -- Pre-launch hold: before the player starts rolling from HOTLAP_START (or
  -- after a stationary respawn), hold the ghost ready right ahead of the player
  -- on the recorded line so it is immediately visible on screen.
  if not state.playerLaunched then
    if speedKmh >= LAUNCH_SPEED_KMH then
      state.playerLaunched = true
      if state.holdingForLaunch then
        state.holdingForLaunch = false
        state.reanchor = true
        appendLog(string.format(
          'LAPSYNC launch wallS=%s cursorS=%.3f speedKmh=%.1f',
          fmtNumber(nowS), state.runtimeTime, speedKmh))
      end
    else
      local nearestT, distM = findNearestTimelineTime(state.playerX, state.playerY, state.playerZ)
      if nearestT and distM <= NEAREST_MAX_DIST_M then
        local targetCursor = (nearestT + currentLeadS()) % runtimeDurationS
        if not state.holdingForLaunch then
          state.holdingForLaunch = true
          state.runtimeTime = targetCursor
          state.runtimeCursor = 1
          state.reanchor = true
          appendLog(string.format(
            'LAPSYNC ready wallS=%s cursorS=%.3f distM=%.1f leadS=%.2f',
            fmtNumber(nowS), targetCursor, distM, currentLeadS()))
        else
          state.runtimeTime = targetCursor
        end
      else
        state.holdingForLaunch = false
      end
    end
  end

  local count = player and player.lapCount
  if type(count) ~= 'number' or count ~= count then
    if not state.lapSyncUnavailable then
      state.lapSyncUnavailable = true
      appendLog('LAPSYNC unavailable lapCount=nil free-run')
      ac.warn('GhostCarGenerator Hotlap: ac.getCar(0).lapCount unavailable; '
        ..'lap sync disabled for this session')
    end
    state.prevSpline = spline
    return
  end
  state.lapSyncUnavailable = false

  if state.lapBaseline == nil then
    state.lapBaseline = count
    state.prevSpline = spline
    appendLog(string.format('LAPSYNC baseline playerLap=%s wallS=%s',
      tostring(count), fmtNumber(nowS)))
    return
  end

  local debounced = (nowS - state.lastLapSyncWallS) < LAPSYNC_DEBOUNCE_S

  if count > state.lapBaseline then
    local jumps = count - state.lapBaseline
    state.lapBaseline = count
    state.prevSpline = spline
    if not debounced then
      restartPlaybackForLap(count, nowS, jumps, 'lapCount')
    end
    return
  end

  if count < state.lapBaseline then
    state.lapBaseline = count
    state.prevSpline = spline
    appendLog(string.format(
      'LAPSYNC rebase playerLap=%s wallS=%s reason=lapcount-decrease',
      tostring(count), fmtNumber(nowS)))
    return
  end

  -- Forward start/finish crossing via spline wrap (0.85..1.0 -> 0.0..0.15):
  -- catches Lap 1 from HOTLAP_START (where AC keeps lapCount=0) and cut laps.
  if spline ~= nil and state.prevSpline ~= nil and not resetChanged and not inPit
    and not debounced and state.prevSpline > 0.85 and spline < 0.15 then
    state.prevSpline = spline
    restartPlaybackForLap(count, nowS, 1, 'sf-crossing')
    return
  end

  state.prevSpline = spline
end

local function writeSample()
  local sim = ac.getSim()
  local time = (sim and sim.gameTime) or 0
  local position = vec3(state.ghostX, state.ghostY, state.ghostZ)

  local inFrustum = 0
  local okFrustum, visible = pcall(ac.isVisibleInMainCamera, position,
    GHOST_NODE_RADIUS, false, true)
  if okFrustum and visible then inFrustum = 1 end

  local camDist = nil
  local okCamera, camPos = pcall(ac.getCameraPosition)
  if okCamera and camPos then
    camDist = dist3(state.ghostX, state.ghostY, state.ghostZ, camPos.x, camPos.y, camPos.z)
  end

  appendLog(string.format('%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%s,%s,%d,%s,%.3f',
    time,
    state.ghostX, state.ghostY, state.ghostZ,
    state.playerX, state.playerY, state.playerZ,
    dist3(state.ghostX, state.ghostY, state.ghostZ,
      state.playerX, state.playerY, state.playerZ),
    fmtNumber(oracle.ndcX), fmtNumber(oracle.ndcY), inFrustum, fmtNumber(camDist),
    state.runtimeTime))
end

-- v0.5 smoothness proof: worst per-frame displacement between two pose
-- applications in the window, plus the rates it was produced at. A smooth run
-- shows `maxStepM ≈ speed / renderHz` (≈0.4 m at 55 m/s and 144 Hz); anything
-- near a timeline frame (0.83 m) or above means the pose is being applied at
-- the app tick, and a value of tens of metres is a teleport.
local function writeSmooth()
  local window = state.smooth.windowS
  if window <= 0 then return end
  local tickHz = state.smooth.ticks / window
  local renderHz = state.smooth.frames / window
  local avgStepM = state.smooth.stepCount > 0
    and (state.smooth.sumStepM / state.smooth.stepCount) or 0
  appendLog(string.format(
    'SMOOTH windowS=%.2f maxStepM=%s maxYawDeg=%.1f renderHz=%.1f tickHz=%.1f resets=%d avgStepM=%s',
    window, fmtNumber(state.smooth.maxStepM),
    math.deg(state.smooth.maxYawRad), renderHz, tickHz, state.smooth.resets,
    fmtNumber(avgStepM)))
  state.smooth.windowS = 0
  state.smooth.maxStepM = 0
  state.smooth.sumStepM = 0
  state.smooth.stepCount = 0
  state.smooth.maxYawRad = 0
  state.smooth.frames = 0
  state.smooth.ticks = 0
  state.smooth.resets = 0
end

local function updateGhost(dt)
  local player = ac.getCar(0)
  if not player then return end
  if not initialize() then return end

  local playerPosition = player.position
  state.playerX, state.playerY, state.playerZ =
    playerPosition.x, playerPosition.y, playerPosition.z

  local step = dt
  if type(step) ~= 'number' or step < 0 then step = 0 end
  if step > 1 then step = 1 end
  step = advanceClock(step)

  -- v0.5: playback rate for the render-side clock (playback seconds per wall
  -- second): 1 at normal speed, ~0 while paused, follows slow motion. Measured
  -- over PLAYBACK_RATE_WINDOW_S, because a per-tick ratio of a quantised
  -- `sim.gameTime` is noise (0, 0, 2x, 0, ...) and would bend the ghost's speed.
  local wall = preciseNow()
  if state.lastTickPrecise then
    local wallDelta = wall - state.lastTickPrecise
    if wallDelta > 0 then
      state.rateStepAcc = state.rateStepAcc + step
      state.rateWallAcc = state.rateWallAcc + wallDelta
      if state.rateWallAcc >= PLAYBACK_RATE_WINDOW_S then
        local instant = state.rateStepAcc / state.rateWallAcc
        if instant > MAX_PLAYBACK_RATE then instant = MAX_PLAYBACK_RATE end
        state.playRate = state.playRate
          + (instant - state.playRate) * PLAYBACK_RATE_GAIN
        state.rateStepAcc = 0
        state.rateWallAcc = 0
      end
    end
  end
  state.lastTickPrecise = wall
  state.smooth.ticks = state.smooth.ticks + 1
  state.activeTicks = state.activeTicks + 1

  if runtime then
    -- Lap boundary / launch-hold first, then advance: a restart or launch-hold
    -- this frame is visible in the pose applied this frame.
    local simNow = ac.getSim()
    lapSyncTick(player, (simNow and simNow.gameTime) or state.clock)
    if not state.holdingForLaunch then
      state.runtimeTime = (state.runtimeTime + step) % runtimeDurationS
    end

    -- Render-callback posing needs the hook to actually fire. If `render.on`
    -- returned a subscription but nothing ever arrived, fall back to the app
    -- tick instead of leaving the ghost frozen at its first pose.
    local hookAlive = state.renderPoseFrames > 0
    local hookFresh = state.lastRenderPoseWall ~= nil
      and (wall - state.lastRenderPoseWall) <= RENDER_HOOK_STALE_S
    local wasActive = state.renderPoseActive
    state.renderPoseActive = (poseSubscription ~= nil or renderSubscription ~= nil)
      and (hookFresh or state.activeTicks <= RENDER_HOOK_GRACE_TICKS)
    if wasActive ~= state.renderPoseActive then
      -- Handover between the render clock and the app tick: neither direction
      -- may be measured (or rendered) as a step.
      state.prevPose = nil
      state.reanchor = true
      state.renderLastPrecise = nil
      state.displayTime = state.runtimeTime
      if not state.renderPoseActive and not state.renderFallbackLogged then
        state.renderFallbackLogged = true
        local reason = hookAlive and 'render-hook-stalled' or 'no-render-frames'
        appendLog('SMOOTHPATH pose=tick reason='..reason)
        ac.warn('GhostCarGenerator Hotlap: render hook '..reason
          ..'; falling back to app-tick posing (motion may step)')
      end
    end

    -- Fallback path (no render hook), plus the very first writer tick: pose from
    -- the tick so the node is never left at the scene origin while the grace
    -- window waits for a render callback. Same interpolation, sampled at the
    -- app-tick rate.
    if not state.renderPoseActive or state.activeTicks == 1 then
      local x, y, z, yaw, speed, pitch = sampleRuntimeFrame(state.runtimeTime)
      applyPose(x, y, z, yaw, pitch)
      setSteerAngle(state.steerRad)
      advanceWheels(speed, step)
    end
  else
    -- Fallback: keep a second car visible 5 m to the side of the player so the
    -- render path can be verified even without a runtime timeline. Orientation
    -- follows the player body directly, as the original spike did.
    local sim = ac.getSim()
    local timeS = (sim and sim.gameTime) or 0
    local x = playerPosition.x + FALLBACK_OFFSET.x
    local y = playerPosition.y + FALLBACK_OFFSET.y
    local z = playerPosition.z + FALLBACK_OFFSET.z
    state.root:setPosition(vec3(x, y, z))
    if player.look and player.up then
      state.root:setOrientation(player.look, player.up)
    end
    state.ghostX, state.ghostY, state.ghostZ = x, y, z
    setSteerAngle(math.sin(timeS * 0.5) * 0.25)
  end

  state.logAccum = state.logAccum + step
  while state.logAccum >= LOG_INTERVAL_S do
    state.logAccum = state.logAccum - LOG_INTERVAL_S
    writeSample()
  end

  if runtime then
    state.smooth.windowS = state.smooth.windowS + step
    if state.smooth.windowS >= SMOOTH_LOG_WINDOW_S then
      writeSmooth()
    end
  end
end

function script.update(dt)
  local ok, err = pcall(function()
    local sim = ac.getSim()
    local step = type(dt) == 'number' and dt or 0
    if step < 0 then step = 0 end
    state.clock = state.clock + step

    -- Only the elected writer may create/pose the node or append rows. The
    -- guard itself runs for standby instances too, so they can take over when
    -- the writer dies or the session restarts.
    local role = guardTick()
    if role ~= 'writer' then return end

    local token = tostring(sim and sim.currentSessionIndex or 0)
    if token ~= state.sessionToken then
      resetForSession(token)
    end
    updateGhost(dt)
  end)
  if not ok then
    state.failed = true
    disposeGhost()
    setError('update', err)
  end
end

local function toggleLapSync()
  settings.syncLaps = not syncLapsEnabled()
  state.lapBaseline = nil
  local sim = ac.getSim()
  local nowS = (sim and sim.gameTime) or state.clock
  appendLog(string.format('LAPSYNC toggle syncLaps=%d wallS=%s cursorS=%.3f',
    syncLapsEnabled() and 1 or 0, fmtNumber(nowS), state.runtimeTime))
end

function script.windowMain(dt)
  if instance.role == 'standby' then
    ui.text('GhostCarGenerator Hotlap: STANDBY (another writer active)')
    ui.text('instance '..tostring(instance.id)..' - no node, no rows')
    return
  end
  if instance.role == 'claiming' then
    ui.text('GhostCarGenerator Hotlap: INIT claiming writer lease')
    return
  end
  if state.status == 'ACTIVE' then
    ui.text('GhostCarGenerator Hotlap: ACTIVE ['..tostring(state.mode)..']')
    local distM = dist3(state.ghostX, state.ghostY, state.ghostZ,
      state.playerX, state.playerY, state.playerZ)
    if runtime then
      local tag = state.holdingForLaunch and ' (READY)' or ''
      ui.text(string.format('Lap %.1f / %.1f s   Dist %.1f m%s',
        state.runtimeTime, runtimeDurationS, distM, tag))
    else
      ui.text(string.format('Fallback 5 m follow   Dist %.1f m', distM))
    end
    if type(ui.checkbox) == 'function' then
      local vis = ghostVisibleEnabled()
      if ui.checkbox('Show ghost', vis) then
        settings.visible = not vis
        applyGhostVisibility(settings.visible)
      end
      if type(ui.sameLine) == 'function' then ui.sameLine(0, 12) end
      if ui.checkbox('Sync laps', syncLapsEnabled()) then
        toggleLapSync()
      end
      if runtime and type(ui.button) == 'function' then
        if type(ui.sameLine) == 'function' then ui.sameLine(0, 12) end
        if ui.button('Sync to me') then
          local sim = ac.getSim()
          snapGhostToPlayer((sim and sim.gameTime) or state.clock, 'ui-main')
        end
      end
    end
    if type(ui.slider) == 'function' then
      local newAlpha, changed = ui.slider('##ghost_alpha_main',
        currentAlpha(), 0.1, 1.0, 'Opacity: %.2f')
      if changed then
        settings.alpha = clamp01(newAlpha)
        updateGhostAlpha(settings.alpha)
      end
    else
      ui.text(string.format('alpha %.2f mode %s  meshes %d  sync %s',
        currentAlpha(), tostring(state.materialMode or 'pending'), state.materialMeshes,
        syncLapsEnabled() and 'on' or 'off'))
    end
  elseif state.status == 'ERROR' then
    ui.text('GhostCarGenerator Hotlap: ERROR: '..tostring(state.errorMessage))
  else
    ui.text('GhostCarGenerator Hotlap: INIT waiting for car/scene')
  end
end

function script.shutdown()
  disposeGhost()
  if poseSubscription then
    pcall(function() poseSubscription:dispose() end)
    poseSubscription = nil
  end
  if renderSubscription then
    pcall(function() renderSubscription:dispose() end)
    renderSubscription = nil
  end
  local released = false
  if instance.role == 'writer' and instance.available then
    local record = parseOwnerRecord(readOwnerRecord())
    if record and record.id == instance.id then
      writeOwnerRecord('')
      released = true
    end
  end
  appendLog(string.format('SHUTDOWN role=%s%s', tostring(instance.role),
    released and ' release=ok' or ''))
end

function script.windowSettings(dt)
  ui.text('GhostCarGenerator Hotlap '..APP_VERSION)
  ui.text('status: '..tostring(state.status)..' / '..tostring(state.mode or '-')
    ..'  ('..tostring(state.materialMeshes)..' meshes)')
  if state.materialError then
    ui.text('alpha error: '..tostring(state.materialError))
  end

  if type(ui.checkbox) == 'function' then
    local vis = ghostVisibleEnabled()
    if ui.checkbox('Show ghost car', vis) then
      settings.visible = not vis
      applyGhostVisibility(settings.visible)
    end
    if ui.checkbox('Sync ghost to my laps', syncLapsEnabled()) then
      toggleLapSync()
    end
    local cross = debugCrossEnabled()
    if ui.checkbox('Show debug cross', cross) then
      settings.debugCross = not cross
    end
  end

  if type(ui.slider) == 'function' then
    local newAlpha, alphaChanged = ui.slider('##ghost_alpha',
      currentAlpha(), 0.1, 1.0, 'Opacity: %.2f')
    if alphaChanged then
      settings.alpha = clamp01(newAlpha)
      updateGhostAlpha(settings.alpha)
    end
    local newLead, leadChanged = ui.slider('##ghost_lead',
      currentLeadS(), 0.0, 3.0, 'Ghost lead: %.2f s')
    if leadChanged then
      settings.leadS = newLead
    end
    local newHeight, heightChanged = ui.slider('##ghost_height',
      currentHeightOffsetM(), -1.0, 1.0, 'Height offset: %.2f m')
    if heightChanged then
      settings.heightOffsetM = newHeight
    end
    local newOffset, offsetChanged = ui.slider('##ghost_start_offset',
      currentStartOffsetS(), -5.0, 5.0, 'Lap start offset: %.2f s')
    if offsetChanged then
      settings.startOffsetS = newOffset
    end
  end

  if type(ui.button) == 'function' then
    if runtime and ui.button('Sync to my position') then
      local sim = ac.getSim()
      snapGhostToPlayer((sim and sim.gameTime) or state.clock, 'ui-settings')
    end
    if runtime and type(ui.sameLine) == 'function' then ui.sameLine(0, 8) end
    if ui.button('Reset defaults') then
      settings.visible = true
      settings.syncLaps = true
      settings.alpha = GHOST_ALPHA
      settings.leadS = GHOST_DEFAULT_LEAD_S
      settings.heightOffsetM = 0.0
      settings.startOffsetS = GHOST_START_OFFSET_S
      settings.debugCross = false
      applyGhostVisibility(true)
      updateGhostAlpha(GHOST_ALPHA)
    end
    if type(ui.sameLine) == 'function' then ui.sameLine(0, 8) end
    if ui.button('Copy log path') then
      ui.setClipboardText(LOG_PATH or '')
    end
  end

  ui.text(string.format('sync: %s  lap=%s  restarts=%d  frames=%s  clock=%s',
    syncLapsEnabled() and 'on' or 'off', tostring(state.lapBaseline),
    state.lapRestarts, runtime and tostring(#runtime.frames) or '0',
    tostring(state.clockSource or '-')))
  ui.text(string.format('pose: %s  %.0f Hz  maxStep %.2f m',
    state.renderPoseActive and 'render' or 'tick',
    state.smooth.windowS > 0 and (state.smooth.frames / math.max(0.001, state.smooth.windowS)) or 0,
    state.smooth.maxStepM))
end
