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

local APP_VERSION = '0.22'
local SETTINGS_VERSION = 9
-- Default tunable alpha for the ghost body, 0..1 (1 = opaque look).
-- Persisted via ac.storage and live-adjustable in the UI.
-- 0.25 with depth-write (`DEPTH_MODE = NORMAL`) + interior meshes hidden gives
-- a single-layer, see-through silhouette without multi-surface white stacking.
local GHOST_ALPHA = 0.25
-- Near-distance fade (metres): smoothly interpolates ghost alpha from full
-- (`currentAlpha()`) at GHOST_FADE_FAR_M down to a floor of GHOST_FADE_MIN_ALPHA
-- (0.10) at GHOST_FADE_NEAR_M so the ghost never pops out or vanishes abruptly.
local GHOST_FADE_NEAR_M = 4.5
local GHOST_FADE_FAR_M = 18.0
local GHOST_FADE_MIN_ALPHA = 0.10
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
-- GR86 road-wheel radius, metres. Only turns timeline speed into wheel roll; a
-- silhouette detail, not a physics claim.
local GHOST_WHEEL_RADIUS_M = 0.32
local MAX_PLAUSIBLE_SPEED_MPS = 120
local SMOOTH_LOG_WINDOW_S = 5.0
-- Flat tints written into txDiffuse; the ghost keeps its shape/normals without
-- its livery. Neutral silver-white body + charcoal tyres/trim + smoked glass
-- matches AC's native ghost look without a blue cast or milky tyres.
local GHOST_TINT_BODY = { 0.78, 0.80, 0.82 }
local GHOST_TINT_DARK = { 0.28, 0.29, 0.30 }
local GHOST_TINT_GLASS = { 0.24, 0.25, 0.27 }
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
    settingsVersion = 0,
    syncLaps = true,
    syncSectors = true,
    visible = true,
    alpha = GHOST_ALPHA,
    distanceFade = true,
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
  -- One-time upgrade from earlier versions where default alpha was 0.50.
  local savedVer = type(settings.settingsVersion) == 'number' and settings.settingsVersion or 0
  if savedVer < SETTINGS_VERSION then
    if type(settings.alpha) ~= 'number' or math.abs(settings.alpha - 0.5) < 0.02 then
      settings.alpha = GHOST_ALPHA
    end
    settings.settingsVersion = SETTINGS_VERSION
  end
  -- An older/partial settings file must not leave nil fields.
  if settings.syncLaps == nil then settings.syncLaps = true end
  if settings.syncSectors == nil then settings.syncSectors = true end
  if settings.visible == nil then settings.visible = true end
  if type(settings.alpha) ~= 'number' then settings.alpha = GHOST_ALPHA end
  if settings.distanceFade == nil then settings.distanceFade = true end
  if type(settings.heightOffsetM) ~= 'number' then settings.heightOffsetM = 0.0 end
  if type(settings.startOffsetS) ~= 'number' then settings.startOffsetS = GHOST_START_OFFSET_S end
  if type(settings.leadS) ~= 'number' then settings.leadS = GHOST_DEFAULT_LEAD_S end
  if settings.debugCross == nil then settings.debugCross = false end
  if settings.showRacingLine == nil then settings.showRacingLine = true end
  if settings.showCarHud == nil then settings.showCarHud = true end
  if settings.showBrakeGlow == nil then settings.showBrakeGlow = true end
  if settings.showRunningLights == nil then settings.showRunningLights = true end
end

local function racingLineEnabled()
  return settings.showRacingLine ~= false
end

local function carHudEnabled()
  return settings.showCarHud ~= false
end

local function brakeGlowEnabled()
  return settings.showBrakeGlow ~= false
end

local function runningLightsEnabled()
  return settings.showRunningLights ~= false
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

local function syncSectorsEnabled()
  return settings.syncSectors ~= false
end

local function ghostVisibleEnabled()
  return settings.visible ~= false
end

local function currentAlpha()
  return clamp01(settings.alpha)
end

local function distanceFadeEnabled()
  return settings.distanceFade ~= false
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
-- Render-context hook for the projection oracle and optional debug cross.
-- Pose updates run inside `script.update(dt)` (once per rendered frame, before
-- AC builds render passes / TAA motion vectors, locked to `sim.dt`).
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
  interiorMeshes = 0,
  materialError = nil,
  ghostMeshSets = {},
  interiorMeshSets = {},
  brakeLightSets = {},
  headlightSets = {},
  brakeDiscMeshes = nil,
  brake = 0,
  throttle = 0,
  gear = 0,
  brakeLit = false,
  brakeDiscTemp = 0,
  appliedBrakeGlow = -1,
  appliedAlpha = nil,
  appliedVisible = nil,
  fadeScale = 1.0,
  fadeHidden = false,
  fadeDistM = nil,
  -- Sidekick-style spline-interpolated delta vs ghost car + iRacing rate.
  deltaTable = nil,
  deltaTableBuilt = false,
  deltaS = nil,
  deltaEstLapS = nil,
  deltaRateSps = 0,
  deltaPrevRaw = nil,
  deltaTrend = 0,
  deltaPrev1 = nil,
  deltaPrev2 = nil,
  deltaTrendAccum = 0,
  -- v0.4/v0.7 lap sync. `lapBaseline` is the last player `lapCount` seen; nil
  -- means "not tracking yet". `prevSpline` tracks `splinePosition` so Lap 1
  -- from HOTLAP_START (where `lapCount` stays 0 across S/F) also syncs cleanly.
  lapBaseline = nil,
  lapRestarts = 0,
  lapSyncUnavailable = false,
  prevSpline = nil,
  prevSector = nil,
  prevResetCounter = nil,
  lastLapSyncWallS = -1e9,
  lastSectorSyncWallS = -1e9,
  playerLaunched = false,
  holdingForLaunch = false,
  isPaused = false,
  -- v0.8 unified frame-locked simulation clock (`sim.dt` inside `script.update`).
  -- Eliminates dual-clock sawtooth, pauses instantly on Escape/Menu, and keeps
  -- the ghost 100% phase-locked to AC's player car and chase camera.
  simDtAccS = 0,
  simGameTimeAccS = 0,
  activeTicks = 0,
  prevPose = nil,
  steerRad = 0,
  wheelRollRad = 0,
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
local runtimeSmoothedTag = 'none'
local runtimeTrackMismatch = nil
local runtimeCandidates = {}
local activeRuntimeIndex = 0
local activeRuntimePath = nil
local activeRuntimeLabel = 'none'
local importState = { running = false, status = nil }
local renderSubscription = nil

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

local function trimLower(value)
  if type(value) ~= 'string' then return '' end
  return (value:gsub('^%s+', ''):gsub('%s+$', ''):lower())
end

local function getCurrentTrackAndLayout()
  local curTrack, curLayout = '', ''
  if type(ac.getTrackID) == 'function' then
    local ok, t = pcall(ac.getTrackID)
    if ok and type(t) == 'string' then curTrack = trimLower(t) end
  end
  if type(ac.getTrackLayout) == 'function' then
    local ok, l = pcall(ac.getTrackLayout)
    if ok and type(l) == 'string' then curLayout = trimLower(l) end
  end
  local combined = curTrack
  if curTrack ~= '' and curLayout ~= '' then
    combined = curTrack..'/'..curLayout
  end
  return curTrack, curLayout, combined
end

local function trackMatchesRuntime(expectedTrackId)
  local curTrack, curLayout, curCombined = getCurrentTrackAndLayout()
  if curTrack == '' then
    return true, 'unknown'
  end
  local expRaw = trimLower(expectedTrackId)
  if expRaw == '' then
    return false, curCombined
  end
  local expTrack, expLayout = expRaw:match('^([^/]+)/(.+)$')
  if not expTrack then
    expTrack, expLayout = expRaw, ''
  end
  if curTrack ~= expTrack then
    return false, curCombined
  end
  if expLayout ~= '' and curLayout ~= '' and curLayout ~= expLayout then
    return false, curCombined
  end
  return true, curCombined
end

local function basenameOf(path)
  if type(path) ~= 'string' then return 'unknown' end
  return (path:gsub('\\', '/'):match('([^/]+)$') or path)
end

local function formatLapShort(sec)
  if type(sec) ~= 'number' or sec <= 0 then return '--' end
  local mins = math.floor(sec / 60)
  local rem = sec - mins * 60
  if mins > 0 then
    return string.format('%d:%06.3f', mins, rem)
  end
  return string.format('%.3fs', rem)
end

local function listJsonFilesInDir(dir)
  local found = {}
  local seen = {}
  local function addPath(p)
    local key = p:gsub('\\', '/'):lower()
    if not seen[key] and io.fileExists(p) then
      seen[key] = true
      found[#found + 1] = p
    end
  end
  if type(io.scanDir) == 'function' then
    local names = {}
    pcall(function()
      io.scanDir(dir, '*.json', function(fileName)
        if type(fileName) == 'string' and fileName:lower():sub(-5) == '.json' then
          names[#names + 1] = fileName
        end
      end)
    end)
    table.sort(names)
    for _, name in ipairs(names) do
      addPath(dir..'/'..name)
    end
  end
  addPath(dir..'/ghost-runtime-v1.json')
  return found
end

local function decodeRuntimeFile(path)
  local text = io.load(path)
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

local function computeDurationS(decoded)
  if not decoded or type(decoded.frames) ~= 'table' or #decoded.frames == 0 then
    return 0
  end
  local count = #decoded.frames
  local lastTime = decoded.frames[count].timeS
  local dtNom = (type(decoded.frameIntervalS) == 'number' and decoded.frameIntervalS > 0)
    and decoded.frameIntervalS or 0.015
  if type(decoded.lapTimeS) == 'number' and decoded.lapTimeS > lastTime then
    return decoded.lapTimeS
  elseif count >= 2 then
    return lastTime + dtNom
  end
  return lastTime
end

local function scanRuntimeCandidates()
  local curTrack, curLayout, curCombined = getCurrentTrackAndLayout()
  local paths = {}
  local seen = {}
  local function appendPaths(list)
    for _, p in ipairs(list) do
      local key = p:gsub('\\', '/'):lower()
      if not seen[key] then
        seen[key] = true
        paths[#paths + 1] = p
      end
    end
  end
  if curTrack ~= '' and curLayout ~= '' then
    appendPaths(listJsonFilesInDir(__dirname..'/tracks/'..curTrack..'/'..curLayout))
  end
  if curTrack ~= '' then
    appendPaths(listJsonFilesInDir(__dirname..'/tracks/'..curTrack))
  end
  if io.fileExists(RUNTIME_FILENAME) then
    appendPaths({ RUNTIME_FILENAME })
  end

  local matches = {}
  local firstMismatch = nil
  local firstError = nil
  for _, p in ipairs(paths) do
    local decoded, err = decodeRuntimeFile(p)
    if decoded then
      local matched, cur = trackMatchesRuntime(decoded.trackId)
      if matched then
        local dur = computeDurationS(decoded)
        local name = basenameOf(p)
        matches[#matches + 1] = {
          path = p,
          name = name,
          label = string.format('%s (%s)', name, formatLapShort(dur)),
          carId = tostring(decoded.carId or 'unknown'),
          trackId = tostring(decoded.trackId or 'unknown'),
          lapTimeS = dur,
        }
      elseif not firstMismatch then
        firstMismatch = {
          current = cur,
          expected = tostring(decoded.trackId or 'unknown'),
        }
      end
    elseif not firstError then
      firstError = err
    end
  end
  return matches, firstMismatch, firstError, curCombined
end

local function loadRuntime(preferredPath)
  runtimeTrackMismatch = nil
  local matches, firstMismatch, firstError, curCombined = scanRuntimeCandidates()
  runtimeCandidates = matches
  activeRuntimeIndex = 0
  activeRuntimePath = nil
  activeRuntimeLabel = 'none'

  if #matches == 0 then
    if firstMismatch then
      runtimeTrackMismatch = firstMismatch
      return nil, 'track-mismatch'
    end
    return nil, firstError or 'missing-runtime'
  end

  local wantPath = preferredPath
  if (not wantPath or wantPath == '') and curCombined ~= '' and type(ac.load) == 'function' then
    local ok, saved = pcall(ac.load, 'gcg_ghost_sel_'..curCombined)
    if ok and type(saved) == 'string' and saved ~= '' then
      wantPath = saved
    end
  end

  local chosenIdx = 1
  if wantPath and wantPath ~= '' then
    local wantNorm = wantPath:gsub('\\', '/'):lower()
    for i, item in ipairs(matches) do
      if item.path:gsub('\\', '/'):lower() == wantNorm then
        chosenIdx = i
        break
      end
    end
  end

  local chosen = matches[chosenIdx]
  local decoded, err = decodeRuntimeFile(chosen.path)
  if not decoded then
    return nil, err or 'runtime-unreadable'
  end
  activeRuntimeIndex = chosenIdx
  activeRuntimePath = chosen.path
  activeRuntimeLabel = chosen.label
  return decoded, nil
end

-- Zero-phase circular smoothing guard for legacy unsmoothed GPS timelines:
-- if the p95 inter-frame speed step exceeds 1.5 m/s per 15 ms frame (10 Hz
-- GPS stair-stepping), run two circular triangular passes (zero phase shift,
-- preserves exact lapTimeS) and recompute tangent yaw/pitch.
local function smoothTimelineIfNeeded(decoded)
  local frames = decoded and decoded.frames
  if type(frames) ~= 'table' or #frames < 20 then return 'short' end
  local n = #frames
  local dt = (type(decoded.frameIntervalS) == 'number' and decoded.frameIntervalS > 0)
    and decoded.frameIntervalS or 0.015
  local dvs = {}
  local stride = math.max(1, math.floor((n - 2) / 600))
  for i = 1, n - 2, stride do
    local a = frames[i].positionM
    local b = frames[i + 1].positionM
    local c = frames[i + 2].positionM
    local span1 = frames[i + 1].timeS - frames[i].timeS
    local span2 = frames[i + 2].timeS - frames[i + 1].timeS
    if span1 > 1e-6 and span2 > 1e-6 then
      local dx1, dy1, dz1 = b[1] - a[1], b[2] - a[2], b[3] - a[3]
      local dx2, dy2, dz2 = c[1] - b[1], c[2] - b[2], c[3] - b[3]
      local spd1 = math.sqrt(dx1 * dx1 + dy1 * dy1 + dz1 * dz1) / span1
      local spd2 = math.sqrt(dx2 * dx2 + dy2 * dy2 + dz2 * dz2) / span2
      dvs[#dvs + 1] = math.abs(spd2 - spd1)
    end
  end
  if #dvs < 10 then return 'clean' end
  table.sort(dvs)
  local p95 = dvs[math.max(1, math.floor(#dvs * 0.95))]
  if p95 <= 1.5 then
    return string.format('clean(p95=%.2f)', p95)
  end

  local xs, ys, zs = {}, {}, {}
  for i = 1, n do
    local p = frames[i].positionM
    xs[i], ys[i], zs[i] = p[1], p[2], p[3]
  end
  local function circularBoxPass(arr, radius)
    local out = {}
    local w = 2 * radius + 1
    local sum = 0
    for k = -radius, radius do
      local idx = ((k) % n) + 1
      sum = sum + arr[idx]
    end
    for i = 1, n do
      out[i] = sum / w
      local removeIdx = ((i - 1 - radius) % n) + 1
      local addIdx = ((i + radius) % n) + 1
      sum = sum - arr[removeIdx] + arr[addIdx]
    end
    return out
  end
  -- Three circular box passes approximate a zero-phase Gaussian filter
  -- (radius=8 -> sigma ≈ 8 frames ≈ 0.12 s, 0 ms phase shift).
  for _ = 1, 3 do
    xs = circularBoxPass(xs, 8)
    ys = circularBoxPass(ys, 8)
    zs = circularBoxPass(zs, 8)
  end
  for i = 1, n do
    local prevI = (i == 1) and n or (i - 1)
    local nextI = (i == n) and 1 or (i + 1)
    local vx = (xs[nextI] - xs[prevI]) / (2 * dt)
    local vy = (ys[nextI] - ys[prevI]) / (2 * dt)
    local vz = (zs[nextI] - zs[prevI]) / (2 * dt)
    local horiz = math.sqrt(vx * vx + vz * vz)
    local yaw = math.atan2(-vx, vz)
    local pitch = math.atan2(vy, math.max(horiz, 1e-6))
    local roll = (frames[i].rotationRad and frames[i].rotationRad[3]) or 0
    frames[i].positionM = { xs[i], ys[i], zs[i] }
    frames[i].rotationRad = { pitch, yaw, roll }
  end
  return string.format('auto(p95=%.2f)', p95)
end

local function applyLoadedRuntime(decoded)
  if decoded then
    runtimeSmoothedTag = smoothTimelineIfNeeded(decoded)
    runtimeDurationS = computeDurationS(decoded)
  else
    runtimeSmoothedTag = 'none'
    runtimeDurationS = 0
  end
end

local runtimeError = nil
runtime, runtimeError = loadRuntime(nil)
applyLoadedRuntime(runtime)

-- The INIT line is emitted only once the single-instance guard has made this
-- instance the writer, so a duplicate app instance cannot add a second
-- `INIT ok` to the log. Everything before ` alpha=` keeps the v0.2 grammar.
local initLogged = false
local function announceInit()
  if initLogged then return end
  initLogged = true
  if runtime then
    appendLog(string.format(
      'INIT ok app=%s schema=%s frames=%d durationS=%.3f carId=%s trackId=%s log=%s alpha=%.3f syncLaps=%d syncOffsetS=%.3f timeline=%s',
      APP_VERSION, tostring(runtime.schema), #runtime.frames, runtimeDurationS,
      tostring(runtime.carId), tostring(runtime.trackId), tostring(LOG_PATH),
      currentAlpha(), syncLapsEnabled() and 1 or 0, currentStartOffsetS(),
      tostring(runtimeSmoothedTag)))
  elseif runtimeTrackMismatch then
    appendLog(string.format(
      'IDLE track-mismatch app=%s current=%s expected=%s log=%s',
      APP_VERSION, sanitizeToken(runtimeTrackMismatch.current),
      sanitizeToken(runtimeTrackMismatch.expected), tostring(LOG_PATH)))
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

local function drawGhostRacingLine()
  if not runtime or not runtime.frames or #runtime.frames < 10 or runtimeDurationS <= 0 then return end
  if not racingLineEnabled() or not ghostVisibleEnabled() then return end
  if type(render.quad) ~= 'function' then return end

  local frames = runtime.frames
  local count = #frames
  local dtNom = (type(runtime.frameIntervalS) == 'number' and runtime.frameIntervalS > 0)
    and runtime.frameIntervalS or 0.015
  local curIdx = state.runtimeCursor
  if curIdx < 1 or curIdx > count then curIdx = 1 end

  local backSteps = math.floor(0.6 / dtNom)
  local fwdSteps = math.floor(2.2 / dtNom)
  local stride = math.max(1, math.floor(0.075 / dtNom))
  local halfW = 0.34
  local yLift = 0.50 + currentHeightOffsetM()

  local prevL, prevR = nil, nil
  local totalSpan = backSteps + fwdSteps
  for offset = -backSteps, fwdSteps, stride do
    local idx = ((curIdx - 1 + offset) % count) + 1
    local fr = frames[idx]
    local p = fr.positionM
    local yaw = (fr.rotationRad and fr.rotationRad[2]) or 0
    -- Right vector in AC XZ plane (forward is (-sin(yaw), 0, cos(yaw)))
    local rx = math.cos(yaw) * halfW
    local rz = math.sin(yaw) * halfW
    local py = p[2] + yLift
    local curL = vec3(p[1] - rx, py, p[3] - rz)
    local curR = vec3(p[1] + rx, py, p[3] + rz)

    if prevL and prevR then
      local frac = (offset + backSteps) / math.max(1, totalSpan)
      local edgeFade = math.min(1.0, math.min(frac * 5.0, (1.0 - frac) * 3.5))
      local a = 0.48 * edgeFade
      if a > 0.02 then
        local brk = type(fr.brake) == 'number' and fr.brake or 0
        local thr = type(fr.throttle) == 'number' and fr.throttle or 0
        local col
        if brk >= 0.05 then
          col = rgbm(0.95, 0.14, 0.14, a)
        elseif thr >= 0.55 then
          col = rgbm(0.14, 0.85, 0.28, a)
        elseif thr >= 0.15 then
          col = rgbm(0.74, 0.82, 0.18, a)
        else
          col = rgbm(0.88, 0.72, 0.22, a * 0.85)
        end
        pcall(render.quad, prevL, prevR, curR, curL, col)
      end
    end
    prevL, prevR = curL, curR
  end
end

local function drawGhostCarHud()
  if not runtime or not carHudEnabled() or not ghostVisibleEnabled() then return end
  if state.fadeHidden then return end
  local okCam, camPos = pcall(ac.getCameraPosition)
  if not okCam or not camPos then return end
  local cdx = state.ghostX - camPos.x
  local cdy = state.ghostY - camPos.y
  local cdz = state.ghostZ - camPos.z
  local d = math.sqrt(cdx * cdx + cdy * cdy + cdz * cdz)
  if d < 5.0 or d > 140.0 then return end

  local hudPos = vec3(state.ghostX, state.ghostY + 2.15, state.ghostZ)
  local brk = state.brake or 0
  local thr = state.throttle or 0
  local gear = state.gear or 0
  local gearPrefix = ''
  if gear > 0 then
    gearPrefix = string.format('G%d  ', gear)
  elseif gear == -1 then
    gearPrefix = 'R  '
  end

  local label, col
  if brk >= 0.04 then
    label = string.format('%sBRK %.0f%%', gearPrefix, brk * 100)
    col = rgbm(1.0, 0.28, 0.24, 0.95)
  elseif thr >= 0.10 then
    label = string.format('%sTHR %.0f%%', gearPrefix, thr * 100)
    col = rgbm(0.24, 0.95, 0.42, 0.95)
  else
    label = gearPrefix..'COAST'
    col = rgbm(0.95, 0.82, 0.35, 0.90)
  end

  if type(render.debugText) == 'function' then
    pcall(render.debugText, hudPos, label, col, 0.95)
  end

  -- Compact 3D horizontal pedal bars facing the camera right under the text
  if type(render.quad) == 'function' then
    local dx = camPos.x - hudPos.x
    local dz = camPos.z - hudPos.z
    local hlen = math.sqrt(dx * dx + dz * dz)
    if hlen > 1e-3 then
      local rx = -dz / hlen
      local rz = dx / hlen
      local barW = 1.05
      local barH = 0.085
      local baseY = state.ghostY + 1.94
      local half = barW * 0.5
      local function drawBar(yOff, frac, fillCol)
        local y0 = baseY + yOff
        local y1 = y0 + barH
        local pL0 = vec3(hudPos.x - rx * half, y0, hudPos.z - rz * half)
        local pR0 = vec3(hudPos.x + rx * half, y0, hudPos.z + rz * half)
        local pR1 = vec3(hudPos.x + rx * half, y1, hudPos.z + rz * half)
        local pL1 = vec3(hudPos.x - rx * half, y1, hudPos.z - rz * half)
        pcall(render.quad, pL0, pR0, pR1, pL1, rgbm(0.06, 0.07, 0.09, 0.55))
        local f = math.max(0, math.min(1, frac))
        if f > 0.01 then
          local endX = -half + barW * f
          local fR0 = vec3(hudPos.x + rx * endX, y0, hudPos.z + rz * endX)
          local fR1 = vec3(hudPos.x + rx * endX, y1, hudPos.z + rz * endX)
          pcall(render.quad, pL0, fR0, fR1, pL1, fillCol)
        end
      end
      drawBar(0.0, thr, rgbm(0.18, 0.92, 0.35, 0.90))
      drawBar(-0.11, brk, rgbm(0.95, 0.18, 0.18, 0.90))
    end
  end
end

-- `render.projectPoint()` is only reliable inside render context, so the
-- projection is sampled from a render callback and cached for the 1 Hz log row.
local function installRenderOracle()
  if type(render) ~= 'table' or type(render.on) ~= 'function' then
    appendLog('RENDERHOOK unavailable render.on')
    return
  end

  local ok, subscription = pcall(render.on, RENDER_EVENT, function()
    oracle.hookFrames = oracle.hookFrames + 1
    if not state.root then return end
    if not oracle.hookAnnounced then
      oracle.hookAnnounced = true
      appendLog(string.format('RENDERHOOK ok event=%s frames=%d',
        RENDER_EVENT, oracle.hookFrames))
    end
    local position = vec3(state.ghostX, state.ghostY, state.ghostZ)
    local point = nil
    pcall(function() point = render.projectPoint(position) end)
    if point then
      oracle.ndcX, oracle.ndcY = point.x, point.y
    end
    if state.status == 'ACTIVE' then
      drawGhostRacingLine()
      drawGhostCarHud()
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
  state.interiorMeshSets = {}
  state.brakeLightSets = {}
  state.headlightSets = {}
  state.brakeDiscMeshes = nil
  state.brake = 0
  state.throttle = 0
  state.gear = 0
  state.brakeLit = false
  state.brakeDiscTemp = 0
  state.appliedBrakeGlow = -1
  state.interiorMeshes = 0
  state.appliedAlpha = nil
  state.appliedVisible = nil
  state.poseApplied = false
  state.activeTicks = 0
  state.prevPose = nil
  state.wheelRollRad = 0
  state.simDtAccS = 0
  state.simGameTimeAccS = 0
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
-- Interior/clutter materials hidden when translucent so alpha does not stack
-- 4-6 layers deep through seats, dashboard, carpet, and cabin trim.
local GHOST_INTERIOR_FILTERS = {
  'material:int_*',
  'material:EXT_OBJECTS_IN_MIRROR',
  'material:ext_window_dots*',
  'material:ext_rear_window_heater',
}

local GHOST_SHADER_REPLACEMENT =
  'SHADER = ksPerPixel\nIS_TRANSPARENT = 1\nBLEND_MODE = ALPHA_BLEND\nDEPTH_MODE = NORMAL\nCAST_SHADOWS = 0'

local GHOST_MESH_FILTERS = {
  { filter = 'shader:ksPerPixelMultiMap_damage_dirt', tint = GHOST_TINT_BODY,  alphaScale = 1.00 },
  { filter = 'shader:ksPerPixelMultiMap_damage',      tint = GHOST_TINT_BODY,  alphaScale = 1.00 },
  { filter = 'shader:ksPerPixelMultiMap_NMDetail',    tint = GHOST_TINT_BODY,  alphaScale = 1.00 },
  { filter = 'shader:ksPerPixelMultiMap',             tint = GHOST_TINT_BODY,  alphaScale = 1.00 },
  { filter = 'shader:ksPerPixelReflection',           tint = GHOST_TINT_GLASS, alphaScale = 0.65 },
  { filter = 'shader:ksPerPixel',                     tint = GHOST_TINT_BODY,  alphaScale = 0.90 },
  { filter = 'shader:ksPerPixelNM',                   tint = GHOST_TINT_DARK,  alphaScale = 0.80 },
  { filter = 'shader:ksPerPixelAT_NM',                tint = GHOST_TINT_DARK,  alphaScale = 0.70 },
  { filter = 'shader:ksPerPixelAT',                   tint = GHOST_TINT_DARK,  alphaScale = 0.70 },
  { filter = 'shader:ksWindscreen',                   tint = GHOST_TINT_GLASS, alphaScale = 0.65 },
  { filter = 'shader:ksGlass',                        tint = GHOST_TINT_GLASS, alphaScale = 0.65 },
  { filter = 'shader:ksBrakeDisc',                    tint = GHOST_TINT_DARK,  alphaScale = 0.75 },
  { filter = 'shader:ksTyres',                        tint = GHOST_TINT_DARK,  alphaScale = 0.85 },
  { filter = 'shader:ksTyre',                         tint = GHOST_TINT_DARK,  alphaScale = 0.85 },
  { filter = 'shader:ksWheel',                        tint = GHOST_TINT_DARK,  alphaScale = 0.85 },
}

local function applyAlphaToTarget(target, alpha, tint, alphaScale)
  local effective = clamp01(alpha * (alphaScale or 1.0))
  local translucent = alpha < 0.999
  local blendMode = translucent and render.BlendMode.AlphaBlend
    or (render.BlendMode.Opaque or 0)
  local c = tint or GHOST_TINT_BODY
  pcall(function() target:setTransparent(translucent) end)
  pcall(function() target:setBlendMode(blendMode) end)
  if render.DepthMode and render.DepthMode.Normal ~= nil then
    pcall(function() target:setDepthMode(render.DepthMode.Normal) end)
  end
  pcall(function() target:setShadows(not translucent) end)
  pcall(function() target:excludeFromCubemap(true) end)
  pcall(function() target:excludeFromSecondary(true) end)
  -- Disable detail-map alpha hijacking (`useDetail=1` on ksPerPixelMultiMap
  -- ignores txDiffuse.a for transparency), kill alpha-test cutoff (`ksAlphaRef`),
  -- and scale additive specular/fresnel reflections with alpha so low opacity
  -- stays genuinely see-through instead of reflecting an opaque bright sky.
  pcall(function() target:setMaterialProperty('useDetail', 0) end)
  pcall(function() target:setMaterialProperty('ksAlphaRef', 0) end)
  pcall(function() target:setMaterialProperty('ksEmissive', vec3(0, 0, 0)) end)
  pcall(function() target:setMaterialProperty('ksAmbient', 0.55) end)
  pcall(function() target:setMaterialProperty('ksDiffuse', 0.75) end)
  pcall(function() target:setMaterialProperty('ksSpecular', translucent and (0.18 * effective) or 0.5) end)
  pcall(function() target:setMaterialProperty('sunSpecular', translucent and 0 or 1.0) end)
  pcall(function() target:setMaterialProperty('fresnelC', translucent and (0.01 * effective) or 0.08) end)
  pcall(function() target:setMaterialProperty('fresnelMaxLevel', translucent and (0.08 * effective) or 0.4) end)
  pcall(function()
    target:setMaterialTexture('txDiffuse', rgbm(c[1], c[2], c[3], effective))
  end)
end

local function updateInteriorVisibility(visible, translucent)
  local showInterior = (visible ~= false) and (not translucent)
  for _, meshSet in ipairs(state.interiorMeshSets) do
    pcall(function() meshSet:setVisible(showInterior, false) end)
  end
end

local function parseRgb3(raw, dr, dg, db)
  if type(raw) == 'table' then
    local r = tonumber(raw[1] or raw.x or raw.r)
    local g = tonumber(raw[2] or raw.y or raw.g)
    local b = tonumber(raw[3] or raw.z or raw.b)
    if r and g and b then return vec3(r, g, b) end
  elseif type(raw) == 'string' then
    local r, g, b = raw:match('([%+%-]?%d*%.?%d+)%s*,%s*([%+%-]?%d*%.?%d+)%s*,%s*([%+%-]?%d*%.?%d+)')
    if r and g and b then return vec3(tonumber(r), tonumber(g), tonumber(b)) end
  end
  return vec3(dr, dg, db)
end

local function applyBrakeLightState(lit, force)
  local want = lit == true
  if not force and want == state.brakeLit then return end
  state.brakeLit = want
  local sets = state.brakeLightSets
  if type(sets) ~= 'table' or #sets == 0 then return end
  local zero = vec3(0, 0, 0)
  local running = runningLightsEnabled()
  for _, entry in ipairs(sets) do
    local col = want and entry.onColor or (running and entry.offColor or zero)
    pcall(function() entry.meshes:setVisible(true, false) end)
    pcall(function() entry.meshes:setMaterialProperty('ksEmissive', col) end)
  end
end

local function applyHeadlightState()
  local sets = state.headlightSets
  if type(sets) ~= 'table' or #sets == 0 then return end
  local zero = vec3(0, 0, 0)
  local on = runningLightsEnabled()
  for _, entry in ipairs(sets) do
    local col = on and entry.onColor or zero
    pcall(function() entry.meshes:setVisible(true, false) end)
    pcall(function() entry.meshes:setMaterialProperty('ksEmissive', col) end)
  end
end

local function updateBrakeDiscGlow(dtStep, force)
  if type(dtStep) == 'number' and dtStep > 0 then
    local brk = state.brake or 0
    local spd = state.speedMps or 0
    local heat = (brk >= 0.05 and spd > 8.0) and (brk * math.min(1.0, spd / 45.0) * 0.65 * dtStep) or 0
    local cool = (0.10 + 0.08 * math.min(1.0, spd / 50.0)) * dtStep
    state.brakeDiscTemp = clamp01((state.brakeDiscTemp or 0) + heat - cool)
  end
  local meshes = state.brakeDiscMeshes
  if not meshes then return end
  local glow = 0
  if brakeGlowEnabled() and state.brakeDiscTemp > 0.18 then
    glow = (state.brakeDiscTemp - 0.18) / 0.82
  end
  if not force and math.abs(glow - (state.appliedBrakeGlow or -1)) < 0.02 then
    return
  end
  state.appliedBrakeGlow = glow
  local col = glow > 0.01
    and vec3(glow * 18.0, glow * 3.2, glow * 0.25)
    or vec3(0, 0, 0)
  pcall(function() meshes:setMaterialProperty('ksEmissive', col) end)
end

local function isInteriorLightName(name)
  local low = tostring(name or ''):lower()
  return low:find('int_', 1, true) ~= nil
    or low:find('arrow_', 1, true) ~= nil
    or low:find('dash', 1, true) ~= nil
    or low:find('gauge', 1, true) ~= nil
    or low:find('cockpit', 1, true) ~= nil
    or low:find('interior', 1, true) ~= nil
    or low:find('display', 1, true) ~= nil
end

local function collectBrakeLights(model)
  state.brakeLightSets = {}
  state.headlightSets = {}
  state.brakeDiscMeshes = nil
  state.brakeLit = false
  state.brakeDiscTemp = 0
  state.appliedBrakeGlow = -1
  if not model or type(model.findMeshes) ~= 'function' then return end

  local entries = {}
  local headEntries = {}
  local seen = {}
  local seenHead = {}
  local source = 'none'
  local function tryAddBrakeMesh(name, onColor, offColor)
    if type(name) ~= 'string' or name == '' then return end
    local key = name:lower()
    if seen[key] then return end
    local ok, m = pcall(function() return model:findMeshes(name) end)
    if ok and m and (type(m.empty) ~= 'function' or not m:empty()) then
      seen[key] = true
      entries[#entries + 1] = {
        name = name,
        meshes = m,
        onColor = onColor or vec3(180, 2, 1),
        offColor = offColor or vec3(0, 0, 0),
      }
    end
  end
  local function tryAddHeadMesh(name, onColor)
    if type(name) ~= 'string' or name == '' or isInteriorLightName(name) then return end
    local key = name:lower()
    if seenHead[key] or seen[key] then return end
    local ok, m = pcall(function() return model:findMeshes(name) end)
    if ok and m and (type(m.empty) ~= 'function' or not m:empty()) then
      seenHead[key] = true
      local c = onColor or vec3(60, 58, 55)
      -- Keep headlights clearly visible in mirrors without excessive bloom
      headEntries[#headEntries + 1] = {
        name = name,
        meshes = m,
        onColor = vec3(c.x * 0.35, c.y * 0.35, c.z * 0.35),
      }
    end
  end

  local cfg = nil
  local carId = (runtime and runtime.carId) or ''
  if type(ac.INIConfig) == 'table' then
    local playerCarId = (type(ac.getCarID) == 'function') and select(2, pcall(ac.getCarID, 0)) or nil
    if type(ac.INIConfig.carData) == 'function'
      and (carId == '' or not playerCarId or tostring(playerCarId):lower() == tostring(carId):lower()) then
      local ok, c = pcall(ac.INIConfig.carData, 0, 'lights.ini')
      if ok and c then cfg = c; source = 'carData' end
    end
    if not cfg and carId ~= '' and type(ac.INIConfig.load) == 'function'
      and ac.FolderID and ac.FolderID.ContentCars and type(ac.getFolder) == 'function' then
      local carsDir = ac.getFolder(ac.FolderID.ContentCars)
      if type(carsDir) == 'string' and carsDir ~= '' then
        local iniPath = carsDir:gsub('[/\\]+$', '')..'/'..carId..'/data/lights.ini'
        if io.fileExists(iniPath) then
          local ok, c = pcall(ac.INIConfig.load, iniPath)
          if ok and c then cfg = c; source = 'lights.ini' end
        end
      end
    end
  end

  if cfg and type(cfg.iterate) == 'function' then
    pcall(function()
      for _, section in cfg:iterate('BRAKE') do
        local name = cfg:get(section, 'NAME', '')
        local rawColor = cfg:get(section, 'COLOR', '')
        local rawOff = cfg:get(section, 'OFF_COLOR', '')
        tryAddBrakeMesh(name, parseRgb3(rawColor, 180, 2, 1), parseRgb3(rawOff, 0, 0, 0))
      end
      for _, section in cfg:iterate('LIGHT') do
        local name = cfg:get(section, 'NAME', '')
        local rawColor = cfg:get(section, 'COLOR', '')
        tryAddHeadMesh(name, parseRgb3(rawColor, 120, 115, 110))
      end
    end)
  elseif cfg and type(cfg.get) == 'function' then
    for i = 0, 15 do
      local sec = 'BRAKE_'..i
      local name = cfg:get(sec, 'NAME', '')
      if type(name) == 'string' and name ~= '' then
        local rawColor = cfg:get(sec, 'COLOR', '')
        local rawOff = cfg:get(sec, 'OFF_COLOR', '')
        tryAddBrakeMesh(name, parseRgb3(rawColor, 180, 2, 1), parseRgb3(rawOff, 0, 0, 0))
      end
      local lsec = 'LIGHT_'..i
      local lname = cfg:get(lsec, 'NAME', '')
      if type(lname) == 'string' and lname ~= '' then
        local rawColor = cfg:get(lsec, 'COLOR', '')
        tryAddHeadMesh(lname, parseRgb3(rawColor, 120, 115, 110))
      end
    end
  end

  if #entries == 0 then
    source = 'fallback'
    for _, pattern in ipairs({ 'RearLight_0', 'RearLight_1', 'CENTRAL_light', '{?brake?}', '{?stop?}' }) do
      tryAddBrakeMesh(pattern, vec3(180, 2, 1), vec3(6, 0, 0))
    end
  end

  local okDisc, discM = pcall(function() return model:findMeshes('shader:ksBrakeDisc') end)
  if okDisc and discM and (type(discM.empty) ~= 'function' or not discM:empty()) then
    state.brakeDiscMeshes = discM
  end

  state.brakeLightSets = entries
  state.headlightSets = headEntries
  applyBrakeLightState(false, true)
  applyHeadlightState()
  updateBrakeDiscGlow(0, true)
  appendLog(string.format('BRAKELIGHTS count=%d headlights=%d discs=%d source=%s',
    #entries, #headEntries, state.brakeDiscMeshes and 1 or 0, source))
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
  applyBrakeLightState(state.brakeLit, true)
  applyHeadlightState()
  updateBrakeDiscGlow(0, true)
  updateInteriorVisibility(ghostVisibleEnabled(), clamped < 0.999)
end

local function applyGhostVisibility(visible)
  local want = (visible ~= false) and (not state.fadeHidden)
  if state.appliedVisible == want then return end
  if not state.root then return end
  pcall(function() state.root:setVisible(want, false) end)
  if state.model then
    pcall(function() state.model:setVisible(want, false) end)
  end
  state.appliedVisible = want
  updateInteriorVisibility(want, (state.appliedAlpha or currentAlpha()) < 0.999)
end

local function computeDistanceFadeScale()
  local playerDist = dist3(state.ghostX, state.ghostY, state.ghostZ,
    state.playerX, state.playerY, state.playerZ)
  local minDist = playerDist
  local okCam, camPos = pcall(ac.getCameraPosition)
  if okCam and camPos then
    local camDist = dist3(state.ghostX, state.ghostY, state.ghostZ,
      camPos.x, camPos.y, camPos.z)
    if camDist < minDist then minDist = camDist end
  end
  state.fadeDistM = minDist
  if not distanceFadeEnabled() then
    return 1.0
  end
  if minDist <= GHOST_FADE_NEAR_M then return 0.0 end
  if minDist >= GHOST_FADE_FAR_M then return 1.0 end
  local t = (minDist - GHOST_FADE_NEAR_M) / (GHOST_FADE_FAR_M - GHOST_FADE_NEAR_M)
  return t * t * (3.0 - 2.0 * t)
end

local function refreshGhostOpacity(force)
  local base = currentAlpha()
  local t = computeDistanceFadeScale()
  state.fadeScale = t
  local minAlpha = math.min(base, GHOST_FADE_MIN_ALPHA)
  local eff = clamp01(minAlpha + (base - minAlpha) * t)
  if state.fadeHidden then
    state.fadeHidden = false
    applyGhostVisibility(ghostVisibleEnabled())
  end
  local threshold = force and 1e-3 or 0.004
  if not state.appliedAlpha or math.abs(eff - state.appliedAlpha) >= threshold then
    updateGhostAlpha(eff)
  end
end

local function isInteriorMaterialName(matName)
  if type(matName) ~= 'string' then return false end
  local lower = matName:lower()
  return lower:sub(1, 4) == 'int_'
    or lower:find('cockpit', 1, true) ~= nil
    or lower:find('interior', 1, true) ~= nil
    or lower == 'ext_objects_in_mirror'
    or lower:find('window_dots', 1, true) ~= nil
    or lower == 'ext_rear_window_heater'
end

local function applyGhostMaterials(model)
  local alpha = currentAlpha()
  state.ghostMeshSets = {}
  state.interiorMeshSets = {}
  state.brakeLightSets = {}
  state.headlightSets = {}
  state.brakeDiscMeshes = nil
  state.brake = 0
  state.throttle = 0
  state.gear = 0
  state.brakeLit = false
  state.brakeDiscTemp = 0
  state.appliedBrakeGlow = -1
  state.interiorMeshes = 0
  if type(render) ~= 'table' or not render.BlendMode then
    return nil, 'render.BlendMode-missing'
  end
  local translucent = alpha < 0.999
  local blendMode = translucent and render.BlendMode.AlphaBlend
    or (render.BlendMode.Opaque or 0)

  -- First make materials unique relative to the rest of the scene so nothing
  -- ever leaks onto the player car, then collect mesh sets by their original
  -- shaders BEFORE switching shaders to ksPerPixel.
  local rsteps = {}
  local function rstep(name, fn)
    local okStep, err = pcall(fn)
    rsteps[name] = okStep and 'ok' or ('failed:'..tostring(err))
  end
  rstep('unique', function() model:ensureUniqueMaterials() end)
  rstep('transparent', function() model:setTransparent(translucent) end)
  rstep('blend', function() model:setBlendMode(blendMode) end)
  rstep('shadows', function() model:setShadows(not translucent) end)

  -- Phase 1: collect all mesh sets by their ORIGINAL shaders BEFORE replacing
  -- shaders (otherwise earlier filters that switch to ksPerPixel would be
  -- matched again by the later `shader:ksPerPixel` filter).
  local collected = {}
  local total, hiddenInt, detail = 0, 0, {}
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
      collected[#collected + 1] = {
        meshes = found,
        count = n,
        tint = spec.tint,
        alphaScale = spec.alphaScale,
      }
      total = total + n
    end
  end

  -- Phase 2: hide interior clutter, make materials unique, switch to clean
  -- ksPerPixel alpha blend, and apply per-group tint/alpha.
  local merged = {}
  for _, item in ipairs(collected) do
    local found = item.meshes
    for idx = 1, item.count do
      local okMat, matName = pcall(function() return found:materialName(idx) end)
      if okMat and isInteriorMaterialName(matName) then
        local okAt, single = pcall(function() return found:at(idx) end)
        if okAt and single then
          state.interiorMeshSets[#state.interiorMeshSets + 1] = single
          hiddenInt = hiddenInt + 1
        end
      end
    end

    state.ghostMeshSets[#state.ghostMeshSets + 1] = {
      meshes = found,
      tint = item.tint,
      alphaScale = item.alphaScale,
    }
    local steps = {}
    local function step(name, fn)
      local okStep, err = pcall(fn)
      steps[name] = okStep and 'ok' or ('failed:'..tostring(err))
    end
    step('unique', function() found:ensureUniqueMaterials() end)
    step('shader', function()
      if type(found.applyShaderReplacements) == 'function' then
        found:applyShaderReplacements(GHOST_SHADER_REPLACEMENT)
      end
    end)
    step('transparent', function() found:setTransparent(translucent) end)
    step('blend', function() found:setBlendMode(blendMode) end)
    step('shadows', function() found:setShadows(not translucent) end)
    step('tint', function()
      applyAlphaToTarget(found, alpha, item.tint, item.alphaScale)
    end)
    for k, v in pairs(steps) do
      if merged[k] == nil then merged[k] = v
      elseif v ~= 'ok' then merged[k] = v end
    end
  end

  appendLog(string.format(
    'GHOSTROOT unique=%s shader=%s transparent=%s blend=%s shadows=%s tint=ok',
    sanitizeToken(rsteps.unique or '?'), sanitizeToken(merged.shader or '?'),
    sanitizeToken(rsteps.transparent or '?'), sanitizeToken(rsteps.blend or '?'),
    sanitizeToken(rsteps.shadows or '?')))

  state.interiorMeshes = hiddenInt
  updateInteriorVisibility(ghostVisibleEnabled(), translucent)

  state.appliedAlpha = alpha
  if total <= 0 then return nil, 'no-meshes' end
  return { count = total, hiddenInt = hiddenInt, mode = translucent and 'alpha' or 'opaque',
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
    'GHOST alpha=%.3f mode=alpha meshes=%d hiddenInt=%d shader=%s transparent=%s blend=%s shadows=%s tint=%s unique=%s filters=%s',
    alpha, info.count, tonumber(info.hiddenInt) or 0,
    sanitizeToken(steps.shader or '?'),
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
  if runtimeTrackMismatch then
    state.status = 'IDLE'
    return false
  end
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
  state.prevPose = nil
  state.activeTicks = 0
  state.wheelRollRad = 0
  state.simDtAccS = 0
  state.simGameTimeAccS = 0
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
  collectBrakeLights(model)
  applyGhostVisibility(ghostVisibleEnabled())
  appendLog(string.format('ACTIVE mode=%s frames=%d hook=%s alpha=%.3f log=%s',
    tostring(state.mode), runtime and #runtime.frames or 0,
    oracle.hookAnnounced and 'ok' or 'none', currentAlpha(), tostring(LOG_PATH)))
  appendLog(string.format('SMOOTHPATH pose=sim-dt event=script.update windowS=%.1f',
    SMOOTH_LOG_WINDOW_S))
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
  state.interiorMeshes = 0
  state.materialError = nil
  state.lapBaseline = nil
  state.lapRestarts = 0
  state.lapSyncUnavailable = false
  state.prevSpline = nil
  state.prevResetCounter = nil
  state.lastLapSyncWallS = -1e9
  state.playerLaunched = false
  state.holdingForLaunch = false
  state.isPaused = false
  appendLog(string.format('SESSION token=%s syncLaps=%d',
    tostring(token), syncLapsEnabled() and 1 or 0))
end

local function reloadRuntime(preferredPath, reason)
  disposeGhost()
  runtime, runtimeError = loadRuntime(preferredPath)
  applyLoadedRuntime(runtime)
  local _, _, curCombined = getCurrentTrackAndLayout()
  if activeRuntimePath and curCombined ~= '' and type(ac.store) == 'function' then
    pcall(ac.store, 'gcg_ghost_sel_'..curCombined, activeRuntimePath)
  end
  state.status = runtimeTrackMismatch and 'IDLE' or 'INIT'
  state.mode = nil
  state.errorMessage = nil
  state.failed = false
  state.initFailures = 0
  state.nextInitAttemptAt = 0
  state.runtimeTime = 0
  state.runtimeCursor = 1
  state.simGameTime = nil
  state.logAccum = 0
  state.prevPose = nil
  state.activeTicks = 0
  state.lapBaseline = nil
  state.prevSpline = nil
  state.deltaS = nil
  state.deltaRateSps = 0
  if runtime then
    appendLog(string.format(
      'RELOAD reason=%s status=ok file=%s carId=%s trackId=%s lapTimeS=%.3f candidates=%d',
      sanitizeToken(reason or 'manual'), sanitizeToken(basenameOf(activeRuntimePath)),
      sanitizeToken(runtime.carId), sanitizeToken(runtime.trackId),
      runtimeDurationS, #runtimeCandidates))
  elseif runtimeTrackMismatch then
    appendLog(string.format(
      'RELOAD reason=%s status=IDLE current=%s expected=%s candidates=%d',
      sanitizeToken(reason or 'manual'),
      sanitizeToken(runtimeTrackMismatch.current),
      sanitizeToken(runtimeTrackMismatch.expected), #runtimeCandidates))
  else
    appendLog(string.format(
      'RELOAD reason=%s status=%s candidates=%d',
      sanitizeToken(reason or 'manual'), sanitizeToken(runtimeError or 'missing-runtime'),
      #runtimeCandidates))
  end
end

local function sanitizeGhostStem(name)
  local s = tostring(name or ''):gsub('%.[^%.]+$', '')
  s = s:gsub('[^%w%._%-]+', '_'):gsub('^[%._%-]+', ''):gsub('[%._%-]+$', '')
  return (s ~= '' and s) or 'imported_ghost'
end

local function getPlayerCarId()
  if type(ac.getCarID) == 'function' then
    local ok, cid = pcall(ac.getCarID, 0)
    if ok and type(cid) == 'string' and cid ~= '' then
      return cid
    end
  end
  return 'ig_toyota_gr86_premium'
end

local function lastNonEmptyLine(text)
  if type(text) ~= 'string' or text == '' then return '' end
  local last = ''
  for line in text:gmatch('[^\r\n]+') do
    local trimmed = line:gsub('^%s+', ''):gsub('%s+$', '')
    if trimmed ~= '' then last = trimmed end
  end
  return last
end

local function importTelemetryFile(filename)
  if type(filename) ~= 'string' or filename == '' then return end
  local base = basenameOf(filename)
  local ext = (base:match('(%.[^%.]+)$') or ''):lower()
  local curTrack, curLayout = getCurrentTrackAndLayout()
  local stem = sanitizeGhostStem(base)

  -- Direct .json import: copy into current track directory and hot-reload
  if ext == '.json' then
    local text = io.load(filename)
    if not text or text == '' then
      importState.status = 'Import failed: unreadable json'
      return
    end
    local targetDir = __dirname
    if curTrack ~= '' and curLayout ~= '' then
      targetDir = __dirname..'/tracks/'..curTrack..'/'..curLayout
    elseif curTrack ~= '' then
      targetDir = __dirname..'/tracks/'..curTrack
    end
    local destPath = targetDir..'/'..stem..'.json'
    if type(io.createFileDir) == 'function' then pcall(io.createFileDir, destPath) end
    if io.save(destPath, text) then
      importState.status = 'Loaded '..basenameOf(destPath)
      appendLog('IMPORT ok mode=json file='..sanitizeToken(base)..' deployed='..sanitizeToken(destPath))
      reloadRuntime(destPath, 'ui-import-json')
    else
      importState.status = 'Import failed: cannot write '..basenameOf(destPath)
      appendLog('IMPORT failed mode=json file='..sanitizeToken(base))
    end
    return
  end

  if type(os.runConsoleProcess) ~= 'function' then
    importState.status = 'Import unavailable: os.runConsoleProcess missing'
    return
  end

  local acRoot = (ac.FolderID and ac.FolderID.Root and type(ac.getFolder) == 'function')
    and ac.getFolder(ac.FolderID.Root) or nil
  if not acRoot or acRoot == '' then
    importState.status = 'Import failed: cannot resolve AC root folder'
    return
  end
  acRoot = acRoot:gsub('[/\\]+$', '')

  local args = {
    '-m', 'ghost_car_generator', 'replay', 'export-runtime', filename,
  }
  if ext ~= '.acreplay' then
    if curTrack == '' then
      importState.status = 'Import failed: unknown current track'
      return
    end
    args[#args + 1] = '--ac-track-dir'
    args[#args + 1] = acRoot..'/content/tracks/'..curTrack
    if curLayout ~= '' then
      args[#args + 1] = '--ac-layout'
      args[#args + 1] = curLayout
    end
  end
  args[#args + 1] = '--car-id'
  args[#args + 1] = getPlayerCarId()
  args[#args + 1] = '--ghost-name'
  args[#args + 1] = stem
  args[#args + 1] = '--deploy-ac'
  args[#args + 1] = acRoot
  args[#args + 1] = '--no-sync-lua'

  importState.running = true
  importState.status = 'Importing '..base..'...'
  appendLog('IMPORT start file='..sanitizeToken(base)..' track='..sanitizeToken(curTrack..'/'..curLayout))

  os.runConsoleProcess({
    filename = 'python',
    arguments = args,
    inheritEnvironment = true,
    separateStderr = true,
    timeout = 120000,
    terminateWithScript = true,
  }, function(err, data)
    importState.running = false
    local exitCode = (type(data) == 'table' and data.exitCode) or -1
    local stdout = (type(data) == 'table' and data.stdout) or ''
    local stderr = (type(data) == 'table' and data.stderr) or ''
    if not err and exitCode == 0 then
      local deployed = stdout:match('Deployed to AC:%s*([^\r\n]+)')
      if deployed then
        deployed = deployed:gsub('^%s+', ''):gsub('%s+$', '')
      end
      importState.status = 'Imported '..basenameOf(deployed or base)
      appendLog('IMPORT ok file='..sanitizeToken(base)..' deployed='..sanitizeToken(deployed or stem))
      reloadRuntime(deployed, 'ui-import')
    else
      local msg = lastNonEmptyLine(stderr)
      if msg == '' then msg = lastNonEmptyLine(stdout) end
      if msg == '' then msg = tostring(err or ('exit='..tostring(exitCode))) end
      if #msg > 90 then msg = msg:sub(1, 87)..'...' end
      importState.status = 'Import failed: '..msg
      appendLog('IMPORT failed file='..sanitizeToken(base)..' code='..tostring(exitCode)..' err='..sanitizeToken(msg))
    end
  end)
end

local function openImportTelemetryDialog()
  if importState.running then return end
  if type(os.openFileDialog) ~= 'function' then
    importState.status = 'File dialog unavailable in this CSP build'
    return
  end
  os.openFileDialog({
    title = 'Select telemetry or ghost file for this track',
    fileTypes = {
      { name = 'Telemetry & Ghost Files', mask = '*.ld;*.ibt;*.csv;*.rcz;*.xrk;*.xrz;*.acreplay;*.json' },
      { name = 'MoTeC LD', mask = '*.ld' },
      { name = 'iRacing IBT', mask = '*.ibt' },
      { name = 'CSV / RaceChrono', mask = '*.csv;*.rcz' },
      { name = 'AiM XRK', mask = '*.xrk;*.xrz' },
      { name = 'Ghost Runtime JSON', mask = '*.json' },
    },
    addAllFilesFileType = true,
  }, function(err, filename)
    if err then
      importState.status = 'Dialog error: '..tostring(err)
      return
    end
    if type(filename) == 'string' and filename ~= '' then
      importTelemetryFile(filename)
    end
  end)
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

-- Catmull-Rom cubic spline interpolation over 4 evenly-spaced control points.
local function catmullRom(p0, p1, p2, p3, k)
  local k2 = k * k
  local k3 = k2 * k
  return 0.5 * (
    (2 * p1)
    + (-p0 + p2) * k
    + (2 * p0 - 5 * p1 + 4 * p2 - p3) * k2
    + (-p0 + 3 * p1 - 3 * p2 + p3) * k3
  )
end

-- Returns the C1-smooth Catmull-Rom interpolated pose at `elapsed` timeline
-- seconds plus segment speed and pitch, wrapping seamlessly from frames[count]
-- back to frames[1] over the exact lap duration `runtimeDurationS`.
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

  local nextIdx = (index < count) and (index + 1) or 1
  local prevIdx = (index > 1) and (index - 1) or count
  local next2Idx = (nextIdx < count) and (nextIdx + 1) or 1

  local p0 = frames[prevIdx]
  local a = frames[index]
  local b = frames[nextIdx]
  local p3 = frames[next2Idx]

  local span = (index < count) and (b.timeS - a.timeS) or (runtimeDurationS - a.timeS)
  local k = span > 1e-6 and math.saturate((t - a.timeS) / span) or 0

  local x, y, z
  if count >= 4 then
    x = catmullRom(p0.positionM[1], a.positionM[1], b.positionM[1], p3.positionM[1], k)
    y = catmullRom(p0.positionM[2], a.positionM[2], b.positionM[2], p3.positionM[2], k)
    z = catmullRom(p0.positionM[3], a.positionM[3], b.positionM[3], p3.positionM[3], k)
  else
    x = a.positionM[1] + (b.positionM[1] - a.positionM[1]) * k
    y = a.positionM[2] + (b.positionM[2] - a.positionM[2]) * k
    z = a.positionM[3] + (b.positionM[3] - a.positionM[3]) * k
  end

  -- Pitch (positive = uphill nose up) and yaw (shortest arc into [-pi, pi]).
  local pitchA = a.rotationRad[1] or 0
  local pitchB = b.rotationRad[1] or 0
  local pitch = pitchA + (pitchB - pitchA) * k
  local yawDelta = wrapAngle(b.rotationRad[2] - a.rotationRad[2])
  local yaw = wrapAngle(a.rotationRad[2] + yawDelta * k)

  -- Segment speed from the timeline itself.
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
  local brakeA = type(a.brake) == 'number' and a.brake or 0
  local brakeB = type(b.brake) == 'number' and b.brake or 0
  state.brake = brakeA + (brakeB - brakeA) * k
  local thrA = type(a.throttle) == 'number' and a.throttle or 0
  local thrB = type(b.throttle) == 'number' and b.throttle or 0
  state.throttle = thrA + (thrB - thrA) * k
  local gearVal = (k < 0.5) and a.gear or b.gear
  state.gear = type(gearVal) == 'number' and math.floor(gearVal + 0.5) or 0
  return x, y, z, yaw, speed, pitch
end

-- Update all 4 wheels: front wheels steer and roll; rear wheels roll.
-- Uses accumulated absolute `wheelRollRad` so steering + rolling compose
-- cleanly without drift (same pattern as CSP's TrafficCarFullLOD.lua:155).
local function updateWheels(steerRad, speedMps, dtPlayback)
  local wheels = state.wheels
  if type(dtPlayback) == 'number' and dtPlayback > 0 then
    if type(speedMps) ~= 'number' or speedMps ~= speedMps then
      speedMps = state.speedMps
    end
    if speedMps > MAX_PLAUSIBLE_SPEED_MPS then speedMps = MAX_PLAUSIBLE_SPEED_MPS end
    local rollDelta = speedMps * dtPlayback / GHOST_WHEEL_RADIUS_M
    if rollDelta > math.pi then rollDelta = math.pi end
    if rollDelta > 0 then
      state.wheelRollRad = (state.wheelRollRad + rollDelta) % (2 * math.pi)
    end
  end
  local sx, sz = math.sin(steerRad or 0), math.cos(steerRad or 0)
  local roll = state.wheelRollRad
  if wheels[1] and wheels[2] then
    pcall(function()
      wheels[1]:setOrientation(vec3(sx, 0, sz)):rotate(vec3(1, 0, 0), roll)
      wheels[2]:setOrientation(vec3(sx, 0, sz)):rotate(vec3(1, 0, 0), roll)
    end)
  end
  if wheels[3] and wheels[4] then
    pcall(function()
      wheels[3]:setOrientation(vec3(0, 0, 1)):rotate(vec3(1, 0, 0), roll)
      wheels[4]:setOrientation(vec3(0, 0, 1)):rotate(vec3(1, 0, 0), roll)
    end)
  end
end

-- ---------------------------------------------------------------------------
-- Per-frame update
-- ---------------------------------------------------------------------------

-- v0.8 unified simulation clock:
--   * Freezes immediately (`0`) when `sim.isPaused` or `sim.isInMainMenu`.
--   * Uses `sim.dt` (the exact simulation delta time driving AC's player car
--     and chase camera; 0 when paused, honors replay slow-mo/fast-forward).
--   * Also tracks `sim.gameTime` deltas while unpaused so `SMOOTH` logs
--     cumulative `driftS` to prove 1.000x exact lap-time fidelity.
local function advanceClock(fallback)
  local sim = ac.getSim()
  local paused = sim ~= nil and (sim.isPaused == true or sim.isInMainMenu == true)
  local nowGameTime = sim and sim.gameTime
  if paused then
    if type(nowGameTime) == 'number' then
      state.simGameTime = nowGameTime
    end
    return 0, true
  end

  local gameDelta = nil
  if type(nowGameTime) == 'number' then
    local previous = state.simGameTime
    state.simGameTime = nowGameTime
    if previous and nowGameTime >= previous then
      local d = nowGameTime - previous
      if d <= 0.25 then
        gameDelta = d
      end
    end
  end

  local simDt = sim and sim.dt
  if type(simDt) == 'number' and simDt == simDt and simDt >= 0 and simDt <= 0.25 then
    if state.clockSource ~= 'sim.dt' then
      state.clockSource = 'sim.dt'
      appendLog('CLOCK sim.dt')
    end
    state.simDtAccS = state.simDtAccS + simDt
    if gameDelta then
      state.simGameTimeAccS = state.simGameTimeAccS + gameDelta
    end
    return simDt, false
  end

  if gameDelta then
    if state.clockSource ~= 'sim' then
      state.clockSource = 'sim'
      appendLog('CLOCK sim')
    end
    return gameDelta, false
  end

  if state.clockSource ~= 'app' and state.clock > 2 then
    state.clockSource = 'app'
    appendLog('CLOCK app')
  end
  local step = (type(fallback) == 'number' and fallback >= 0) and fallback or 0
  if step > 0.25 then step = 0.25 end
  return step, false
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
  state.prevPose = nil
  state.smooth.resets = state.smooth.resets + 1
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
  state.prevPose = nil
  state.smooth.resets = state.smooth.resets + 1
  state.holdingForLaunch = false
  state.playerLaunched = true
  appendLog(string.format(
    'LAPSYNC snap wallS=%s cursorS=%.3f distM=%.1f leadS=%.2f reason=%s',
    fmtNumber(nowS), cursor, distM, currentLeadS(), tostring(reason or 'manual')))
  return true
end

local function lookupGhostTimeBySpline(s)
  local tbl = state.deltaTable
  if not tbl or type(s) ~= 'number' or s ~= s then return nil end
  local pos = tbl.pos
  local tim = tbl.tim
  local lo, hi = 1, tbl.count
  while lo < hi do
    local mid = math.floor((lo + hi) * 0.5)
    if pos[mid] <= s then
      lo = mid + 1
    else
      hi = mid
    end
  end
  local i = math.max(1, math.min(tbl.count - 1, lo - 1))
  local ds = pos[i + 1] - pos[i]
  local slope = ds > 1e-9 and ((tim[i + 1] - tim[i]) / ds) or 0
  return tim[i] + slope * (s - pos[i])
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
        state.prevPose = nil
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
          state.prevPose = nil
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
    state.prevSector = player and player.currentSector
    restartPlaybackForLap(count, nowS, 1, 'sf-crossing')
    return
  end

  -- Per-sector ghost reset: when entering a new track sector (e.g. S1 -> S2,
  -- S2 -> S3), snap the ghost right ahead of the player (+leadS) so the player
  -- always gets a fresh visual reference in every sector.
  local sector = player and player.currentSector
  if type(sector) == 'number' and sector == sector then
    if state.prevSector ~= nil and sector ~= state.prevSector then
      local sectorDebounced = (nowS - state.lastSectorSyncWallS) < 2.0
      if syncSectorsEnabled() and state.playerLaunched and not resetChanged
        and not inPit and not debounced and not sectorDebounced and sector > 0 then
        local baseT = lookupGhostTimeBySpline(spline)
        if baseT == nil then
          local nearestT, distM = findNearestTimelineTime(state.playerX, state.playerY, state.playerZ)
          if nearestT and distM <= NEAREST_MAX_DIST_M then
            baseT = nearestT
          end
        end
        if baseT ~= nil then
          local offsetS = currentStartOffsetS()
          local leadS = currentLeadS()
          local target = (baseT + offsetS + leadS) % runtimeDurationS
          state.runtimeTime = target
          state.runtimeCursor = 1
          state.prevPose = nil
          state.smooth.resets = state.smooth.resets + 1
          state.lapRestarts = state.lapRestarts + 1
          state.lastSectorSyncWallS = nowS
          appendLog(string.format(
            'LAPSYNC sector playerSector=%d prevSector=%d wallS=%s cursorS=%.3f leadS=%.2f reason=sector-change',
            sector, state.prevSector, fmtNumber(nowS), target, leadS))
        end
      end
    end
    state.prevSector = sector
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

local function writeSmooth()
  local window = state.smooth.windowS
  if window <= 0 then return end
  local tickHz = state.smooth.ticks / window
  local renderHz = state.smooth.frames / window
  local avgStepM = state.smooth.stepCount > 0
    and (state.smooth.sumStepM / state.smooth.stepCount) or 0
  local driftS = state.simDtAccS - state.simGameTimeAccS
  appendLog(string.format(
    'SMOOTH windowS=%.2f maxStepM=%s maxYawDeg=%.1f renderHz=%.1f tickHz=%.1f resets=%d avgStepM=%s driftS=%+.4f',
    window, fmtNumber(state.smooth.maxStepM),
    math.deg(state.smooth.maxYawRad), renderHz, tickHz, state.smooth.resets,
    fmtNumber(avgStepM), driftS))
  state.smooth.windowS = 0
  state.smooth.maxStepM = 0
  state.smooth.sumStepM = 0
  state.smooth.stepCount = 0
  state.smooth.maxYawRad = 0
  state.smooth.frames = 0
  state.smooth.ticks = 0
  state.smooth.resets = 0
end

local function buildGhostDeltaTable()
  state.deltaTableBuilt = true
  state.deltaTable = nil
  local count = (runtime and runtime.frames) and #runtime.frames or 0
  if count < 10 or runtimeDurationS <= 0 then return end
  if type(ac.worldCoordinateToTrackProgress) ~= 'function' then return end

  -- Sample up to ~2000 points around the ghost lap into AC's exact
  -- NormalizedSplinePosition space (same space as player.splinePosition).
  local step = math.max(1, math.floor(count / 2000))
  local raw = {}
  local minS, minIdx = 2.0, 1
  for i = 1, count, step do
    local fr = runtime.frames[i]
    local p = fr.positionM
    local ok, s = pcall(ac.worldCoordinateToTrackProgress, vec3(p[1], p[2], p[3]))
    if not ok or type(s) ~= 'number' or s < 0 or s ~= s then
      return
    end
    local idx = #raw + 1
    raw[idx] = { s = s, t = fr.timeS }
    if s < minS then
      minS = s
      minIdx = idx
    end
  end
  local nRaw = #raw
  if nRaw < 10 then return end

  -- Rotate so table starts right after S/F (minS -> 0.0) and enforce strict
  -- monotonicity in spline progress, matching Sidekick's delta harvesting.
  local pos = { 0.0 }
  local tim = { 0.0 }
  local t0 = raw[minIdx].t
  local prevS = 0.0
  local prevT = 0.0
  for k = 0, nRaw - 1 do
    local item = raw[((minIdx - 1 + k) % nRaw) + 1]
    local relT = (item.t - t0) % runtimeDurationS
    if item.s > prevS + 1e-6 and item.s < 0.9999 and relT >= prevT then
      pos[#pos + 1] = item.s
      tim[#tim + 1] = relT
      prevS = item.s
      prevT = relT
    end
  end
  pos[#pos + 1] = 1.0
  tim[#tim + 1] = runtimeDurationS
  if #pos < 10 then return end
  state.deltaTable = { pos = pos, tim = tim, count = #pos }
end

local function updateGhostDelta(player, step)
  if not state.deltaTableBuilt then
    buildGhostDeltaTable()
  end
  local tbl = state.deltaTable
  if not tbl or not player then
    state.deltaS = nil
    state.deltaEstLapS = nil
    state.deltaTrend = 0
    return
  end

  local lapMs = player.lapTimeMs
  local s = player.splinePosition
  local inPit = player.isInPit or player.isInPitlane
  if inPit or state.holdingForLaunch
    or type(lapMs) ~= 'number' or lapMs <= 1500
    or type(s) ~= 'number' or s ~= s or s <= 0.003 or s >= 0.997
    or (lapMs * 0.001) > (runtimeDurationS * 2.5) then
    state.deltaS = nil
    state.deltaEstLapS = nil
    state.deltaRateSps = 0
    state.deltaPrevRaw = nil
    state.deltaTrend = 0
    state.deltaPrev1 = nil
    state.deltaPrev2 = nil
    state.deltaTrendAccum = 0
    return
  end

  -- Sidekick (Erwin Schmidt) bisect_right + linear interpolation on spline progress.
  local pos = tbl.pos
  local tim = tbl.tim
  local lo, hi = 1, tbl.count
  while lo < hi do
    local mid = math.floor((lo + hi) * 0.5)
    if pos[mid] <= s then
      lo = mid + 1
    else
      hi = mid
    end
  end
  local i = math.max(1, math.min(tbl.count - 1, lo - 1))
  local ds = pos[i + 1] - pos[i]
  local slope = ds > 1e-9 and ((tim[i + 1] - tim[i]) / ds) or 0
  local ghostTimeAtPos = tim[i] + slope * (s - pos[i])
  local delta = (lapMs * 0.001) - ghostTimeAtPos

  -- Fast-response smoothed instantaneous d(delta)/dt (s/s) for iRacing bar color.
  if step and step > 0 then
    if state.deltaPrevRaw ~= nil and math.abs(delta - state.deltaPrevRaw) < 2.0 then
      local instRate = (delta - state.deltaPrevRaw) / math.max(0.005, step)
      local alpha = math.min(1.0, step / 0.12)
      state.deltaRateSps = state.deltaRateSps + (instRate - state.deltaRateSps) * alpha
    else
      state.deltaRateSps = 0
    end
  end
  state.deltaPrevRaw = delta

  state.deltaS = delta
  state.deltaEstLapS = math.max(0, runtimeDurationS + delta)

  -- Sidekick trend indicator (`ttb = 2*t - prevt - prevt2`), sampled at 5 Hz.
  state.deltaTrendAccum = state.deltaTrendAccum + (step or 0)
  if state.deltaTrendAccum >= 0.2 then
    state.deltaTrendAccum = 0
    if state.deltaPrev2 ~= nil and state.deltaPrev1 ~= nil then
      state.deltaTrend = 2.0 * delta - state.deltaPrev1 - state.deltaPrev2
    end
    state.deltaPrev2 = state.deltaPrev1
    state.deltaPrev1 = delta
  end
end

local function updateGhost(dt)
  local player = ac.getCar(0)
  if not player then return end
  if not initialize() then return end
  if runtime and not state.deltaTableBuilt then
    buildGhostDeltaTable()
  end

  local playerPosition = player.position
  state.playerX, state.playerY, state.playerZ =
    playerPosition.x, playerPosition.y, playerPosition.z

  local step, paused = advanceClock(dt)
  if paused ~= state.isPaused then
    state.isPaused = paused
    appendLog(string.format('PAUSED state=%d ghostT=%.3f',
      paused and 1 or 0, state.runtimeTime))
    if not paused then
      state.prevPose = nil
    end
  end

  -- When AC is paused (Escape / main menu), freeze the ghost completely at its
  -- current pose (no timeline advancement, no wheel spin, no step glitches).
  if paused then
    if runtime and not state.poseApplied then
      local x, y, z, yaw, speed, pitch = sampleRuntimeFrame(state.runtimeTime)
      applyPose(x, y, z, yaw, pitch)
      updateWheels(state.steerRad, 0, 0)
    end
    return
  end

  state.smooth.ticks = state.smooth.ticks + 1
  state.activeTicks = state.activeTicks + 1

  if runtime then
    local simNow = ac.getSim()
    lapSyncTick(player, (simNow and simNow.gameTime) or state.clock)
    local advanceStep = state.holdingForLaunch and 0 or step
    if advanceStep > 0 then
      state.runtimeTime = (state.runtimeTime + advanceStep) % runtimeDurationS
    end

    local x, y, z, yaw, speed, pitch = sampleRuntimeFrame(state.runtimeTime)
    applyPose(x, y, z, yaw, pitch)
    updateWheels(state.steerRad, speed, advanceStep)
    refreshGhostOpacity(false)
    applyBrakeLightState((state.brake or 0) >= 0.03, false)
    updateBrakeDiscGlow(advanceStep, false)
    updateGhostDelta(player, step)
    state.smooth.frames = state.smooth.frames + 1
  else
    -- Fallback: keep a second car visible 5 m to the side of the player so the
    -- render path can be verified even without a runtime timeline.
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
    updateWheels(math.sin(timeS * 0.5) * 0.25, 0, 0)
    refreshGhostOpacity(false)
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

local function formatLapTimeS(seconds)
  if type(seconds) ~= 'number' or seconds ~= seconds or seconds <= 0 then
    return '-:--.---'
  end
  local mins = math.floor(seconds / 60)
  local rem = seconds - mins * 60
  return string.format('%d:%06.3f', mins, rem)
end

local function drawTranslucentPanelBg()
  if type(ui.drawRectFilled) == 'function' and type(ui.windowSize) == 'function' then
    local sz = ui.windowSize()
    ui.drawRectFilled(vec2(0, 0), sz, rgbm(0.05, 0.06, 0.08, 0.48), 6)
    if type(ui.drawRect) == 'function' then
      ui.drawRect(vec2(0, 0), sz, rgbm(1.0, 1.0, 1.0, 0.10), 6, nil, 1)
    end
  end
end

local function iRacingDeltaColor(value, span, nr, ng, nb)
  local r0, g0, b0 = nr or 0.88, ng or 0.90, nb or 0.92
  if type(value) ~= 'number' or value ~= value then
    return rgbm(r0, g0, b0, 1.0)
  end
  local n = math.max(-1.0, math.min(1.0, value / span))
  local a = math.abs(n)
  -- Neutral -> vivid iRacing neon green (0.08, 0.98, 0.28) or bright red (1.0, 0.16, 0.14)
  if n < 0 then
    return rgbm(r0 + (0.08 - r0) * a, g0 + (0.98 - g0) * a, b0 + (0.28 - b0) * a, 1.0)
  elseif n > 0 then
    return rgbm(r0 + (1.00 - r0) * a, g0 + (0.16 - g0) * a, b0 + (0.14 - b0) * a, 1.0)
  end
  return rgbm(r0, g0, b0, 1.0)
end

local function drawGhostDeltaBar()
  if type(ui.drawRectFilled) ~= 'function' or type(ui.getCursor) ~= 'function' then
    return
  end
  local availW = (type(ui.availableSpaceX) == 'function' and math.max(180, ui.availableSpaceX())) or 280
  local barW = math.max(140, availW - 12)
  local barH = 11
  local cur = ui.getCursor()
  local barX = cur.x + (availW - barW) * 0.5
  local barY = cur.y + 2
  local midX = barX + barW * 0.5
  ui.drawRectFilled(vec2(barX, barY), vec2(barX + barW, barY + barH), rgbm(0.10, 0.11, 0.14, 0.80), 3)
  local d = state.deltaS
  if type(d) == 'number' and d == d then
    local norm = math.max(-1.0, math.min(1.0, -d / 2.0))
    local fillW = math.abs(norm) * (barW * 0.5)
    local barCol = iRacingDeltaColor(state.deltaRateSps or 0, 0.30, 0.32, 0.34, 0.38)
    if fillW >= 1 then
      if norm >= 0 then
        ui.drawRectFilled(vec2(midX, barY + 1), vec2(midX + fillW, barY + barH - 1), barCol, 2)
      else
        ui.drawRectFilled(vec2(midX - fillW, barY + 1), vec2(midX, barY + barH - 1), barCol, 2)
      end
    end
  end
  if type(ui.drawRect) == 'function' then
    ui.drawRect(vec2(barX, barY), vec2(barX + barW, barY + barH), rgbm(1.0, 1.0, 1.0, 0.25), 3, nil, 1)
  end
  if type(ui.drawLine) == 'function' then
    ui.drawLine(vec2(midX, barY - 1), vec2(midX, barY + barH + 1), rgbm(1.0, 1.0, 1.0, 0.85), 1.5)
  end
  if type(ui.dummy) == 'function' then
    ui.dummy(vec2(availW, barH + 5))
  end
end

local function drawGhostDeltaRow()
  local d = state.deltaS
  local text
  local col = rgbm(0.88, 0.90, 0.92, 1.0)
  if type(d) == 'number' and d == d then
    local dStr = string.format('%+.3f', d)
    text = string.format('Delta vs Ghost: %ss  (Est %s)',
      dStr, formatLapTimeS(state.deltaEstLapS))
    col = iRacingDeltaColor(d, 0.15, 0.88, 0.90, 0.92)
  else
    text = string.format('Delta vs Ghost: --.---  (Ref %s)', formatLapTimeS(runtimeDurationS))
  end
  if type(ui.textColored) == 'function' then
    ui.textColored(text, col)
  else
    ui.text(text)
  end
end

local function drawGhostDeltaHero(distM)
  local d = state.deltaS
  local bigStr = '--.---'
  local col = rgbm(0.88, 0.90, 0.92, 1.0)
  local subStr
  if type(d) == 'number' and d == d then
    bigStr = string.format('%+.3fs', d)
    col = iRacingDeltaColor(d, 0.15, 0.88, 0.90, 0.92)
    subStr = string.format('Est %s   Ref %s   %.1fm',
      formatLapTimeS(state.deltaEstLapS), formatLapTimeS(runtimeDurationS), distM or 0)
  else
    local tag = state.isPaused and 'PAUSED'
      or (state.holdingForLaunch and 'READY' or 'vs Ghost')
    subStr = string.format('Ref %s   %.1fm   (%s)',
      formatLapTimeS(runtimeDurationS), distM or 0, tag)
  end

  local availW = (type(ui.availableSpaceX) == 'function' and math.max(180, ui.availableSpaceX())) or 280
  if type(ui.dwriteTextAligned) == 'function' and ui.Alignment and ui.Alignment.Center ~= nil then
    local bigSize = 26
    ui.dwriteTextAligned(bigStr, bigSize, ui.Alignment.Center, ui.Alignment.Center,
      vec2(availW, bigSize + 6), false, col)
    ui.dwriteTextAligned(subStr, 13, ui.Alignment.Center, ui.Alignment.Center,
      vec2(availW, 18), false, rgbm(0.76, 0.78, 0.82, 1.0))
  elseif type(ui.textColored) == 'function' then
    ui.textColored('Delta vs Ghost: '..bigStr, col)
    ui.text(subStr)
  else
    ui.text('Delta vs Ghost: '..bigStr)
    ui.text(subStr)
  end
end

function script.windowMain(dt)
  drawTranslucentPanelBg()
  if instance.role == 'standby' then
    ui.text('GhostCarGenerator Delta: STANDBY')
    return
  end
  if instance.role == 'claiming' then
    ui.text('GhostCarGenerator Delta: INIT')
    return
  end
  if state.status == 'ACTIVE' then
    local distM = dist3(state.ghostX, state.ghostY, state.ghostZ,
      state.playerX, state.playerY, state.playerZ)
    if runtime then
      drawGhostDeltaBar()
      drawGhostDeltaHero(distM)
    else
      ui.text(string.format('Ghost Fallback (5m)   Dist %.1fm', distM))
    end
  elseif state.status == 'IDLE' and runtimeTrackMismatch then
    ui.text('GhostCarGenerator: IDLE (wrong track)')
    ui.text('Track: '..tostring(runtimeTrackMismatch.current))
    ui.text('Ghost: '..tostring(runtimeTrackMismatch.expected))
    if type(ui.button) == 'function' then
      if not importState.running and ui.button('Import telemetry...') then
        openImportTelemetryDialog()
      end
      if type(ui.sameLine) == 'function' then ui.sameLine(0, 6) end
      if ui.button('Reload') then
        reloadRuntime(nil, 'ui-idle-reload')
      end
    end
    if importState.status then
      ui.text(tostring(importState.status))
    end
  elseif state.status == 'ERROR' then
    ui.text('GhostCarGenerator Hotlap: ERROR: '..tostring(state.errorMessage))
  else
    ui.text('GhostCarGenerator Delta: waiting for scene')
  end
end

function script.shutdown()
  disposeGhost()
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
  drawTranslucentPanelBg()
  ui.text('GhostCarGenerator Hotlap '..APP_VERSION)
  drawGhostDeltaBar()
  drawGhostDeltaRow()
  ui.text(string.format('status: %s / %s  (%d ext, %d int hidden)',
    tostring(state.status), tostring(state.mode or '-'),
    math.max(0, state.materialMeshes - state.interiorMeshes), state.interiorMeshes))
  if state.materialError then
    ui.text('alpha error: '..tostring(state.materialError))
  end

  if #runtimeCandidates > 0 then
    ui.text(string.format('Ghost [%d/%d]: %s',
      activeRuntimeIndex, #runtimeCandidates, tostring(activeRuntimeLabel)))
    if type(ui.combo) == 'function' and type(ui.selectable) == 'function' then
      ui.combo('##ghost_picker', tostring(activeRuntimeLabel), nil, function()
        for idx, item in ipairs(runtimeCandidates) do
          if ui.selectable(item.label, idx == activeRuntimeIndex) then
            reloadRuntime(item.path, 'ui-combo')
          end
        end
      end)
    end
  elseif runtimeTrackMismatch then
    ui.text('Ghost: IDLE (wrong track: '..tostring(runtimeTrackMismatch.current)..')')
  else
    ui.text('Ghost: none (5m fallback)')
  end
  if type(ui.button) == 'function' then
    if not importState.running and ui.button('Import telemetry...') then
      openImportTelemetryDialog()
    elseif importState.running then
      ui.text('Importing telemetry...')
    end
    if type(ui.sameLine) == 'function' then ui.sameLine(0, 6) end
    if ui.button('Reload ghosts') then
      reloadRuntime(activeRuntimePath, 'ui-reload')
    end
    if #runtimeCandidates > 1 then
      if type(ui.sameLine) == 'function' then ui.sameLine(0, 6) end
      if ui.button('< Prev') then
        local prevIdx = activeRuntimeIndex - 1
        if prevIdx < 1 then prevIdx = #runtimeCandidates end
        reloadRuntime(runtimeCandidates[prevIdx].path, 'ui-prev')
      end
      if type(ui.sameLine) == 'function' then ui.sameLine(0, 4) end
      if ui.button('Next >') then
        local nextIdx = activeRuntimeIndex + 1
        if nextIdx > #runtimeCandidates then nextIdx = 1 end
        reloadRuntime(runtimeCandidates[nextIdx].path, 'ui-next')
      end
    end
  end
  if importState.status then
    ui.text(tostring(importState.status))
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
    local syncSec = syncSectorsEnabled()
    if ui.checkbox('Sync ghost at every sector', syncSec) then
      settings.syncSectors = not syncSec
    end
    local fade = distanceFadeEnabled()
    if ui.checkbox('Near-distance fade (4.5-18m)', fade) then
      settings.distanceFade = not fade
      refreshGhostOpacity(true)
    end
    local lineOn = racingLineEnabled()
    if ui.checkbox('3D brake/throttle racing line', lineOn) then
      settings.showRacingLine = not lineOn
    end
    local hudOn = carHudEnabled()
    if ui.checkbox('3D gear & pedal HUD above ghost', hudOn) then
      settings.showCarHud = not hudOn
    end
    local glowOn = brakeGlowEnabled()
    if ui.checkbox('Glowing brake discs', glowOn) then
      settings.showBrakeGlow = not glowOn
      updateBrakeDiscGlow(0, true)
    end
    local runOn = runningLightsEnabled()
    if ui.checkbox('Headlights & tail running lights', runOn) then
      settings.showRunningLights = not runOn
      applyBrakeLightState(state.brakeLit, true)
      applyHeadlightState()
    end
    local cross = debugCrossEnabled()
    if ui.checkbox('Show debug cross', cross) then
      settings.debugCross = not cross
    end
  end

  if type(ui.slider) == 'function' then
    local newAlpha, alphaChanged = ui.slider('##ghost_alpha',
      currentAlpha(), 0.05, 1.0, 'Opacity: %.2f')
    if alphaChanged then
      settings.alpha = clamp01(newAlpha)
      refreshGhostOpacity(true)
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
      settings.syncSectors = true
      settings.alpha = GHOST_ALPHA
      settings.distanceFade = true
      settings.leadS = GHOST_DEFAULT_LEAD_S
      settings.heightOffsetM = 0.0
      settings.startOffsetS = GHOST_START_OFFSET_S
      settings.showRacingLine = true
      settings.showCarHud = true
      settings.showBrakeGlow = true
      settings.showRunningLights = true
      settings.debugCross = false
      state.fadeHidden = false
      applyGhostVisibility(true)
      refreshGhostOpacity(true)
      applyBrakeLightState(state.brakeLit, true)
      applyHeadlightState()
      updateBrakeDiscGlow(0, true)
    end
    if type(ui.sameLine) == 'function' then ui.sameLine(0, 8) end
    if ui.button('Copy log path') then
      ui.setClipboardText(LOG_PATH or '')
    end
  end

  ui.text(string.format('sync: %s  lap=%.3fs  restarts=%d  clock=%s%s',
    syncLapsEnabled() and 'on' or 'off', runtimeDurationS,
    state.lapRestarts, tostring(state.clockSource or '-'),
    state.isPaused and ' (PAUSED)' or ''))
  ui.text(string.format('pose: sim.dt  %.0f Hz  maxStep %.2f m  drift %+.3fs',
    state.smooth.windowS > 0 and (state.smooth.frames / math.max(0.001, state.smooth.windowS)) or 0,
    state.smooth.maxStepM, state.simDtAccS - state.simGameTimeAccS))
end
