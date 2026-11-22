-- GhostCarGenerator Hotlap app for CSP.
--
-- Renders a second, visual-only KN5 car in a normal Hotlap session (no CSP
-- new-mode required). The body pose is driven by the `ghost-runtime-v1.json`
-- timeline sitting next to this file; when that file is missing the app falls
-- back to a static 5 m offset that follows the player so a second car still
-- renders in an otherwise empty install.
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
--     written, followed by `NODE ok ...` once the scene node exists.
--     Failures also produce an `ERROR ...` line and an `ac.warn`.
--   * The window text starts with `GhostCarGenerator Hotlap: ACTIVE` or
--     `GhostCarGenerator Hotlap: ERROR: ...` so screenshots can be asserted
--     on without OCR of the numbers.

local APP_VERSION = '0.2'
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
-- Render-context hook used to sample the projection oracle. Installed apps use
-- the same event in an app script: apps/lua/ac-tracer/lib/reference_line.lua:301.
local RENDER_EVENT = 'main.track.transparent'
-- Draws a cross at the computed ghost pose during verification. Remove/disable
-- for production use; it only draws inside the render hook.
local ORACLE_DEBUG_CROSS = true

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
  appendLog(string.format(
    'INIT ok app=%s schema=%s frames=%d durationS=%.3f carId=%s trackId=%s log=%s',
    APP_VERSION, tostring(runtime.schema), #runtime.frames, runtimeDurationS,
    tostring(runtime.carId), tostring(runtime.trackId), tostring(LOG_PATH)))
else
  appendLog(string.format(
    'INIT %s app=%s fallback=5m-follow player log=%s',
    tostring(runtimeError), APP_VERSION, tostring(LOG_PATH)))
  ac.warn('GhostCarGenerator Hotlap: '..tostring(runtimeError)..'; using 5 m fallback')
end

-- ---------------------------------------------------------------------------
-- Render oracle
-- ---------------------------------------------------------------------------

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
    if ORACLE_DEBUG_CROSS then
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
  state.poseApplied = false
  if root then
    pcall(function() root:dispose() end)
  end
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
  logNodeIntrospection(node, model)
  appendLog(string.format('ACTIVE mode=%s frames=%d hook=%s log=%s',
    tostring(state.mode), runtime and #runtime.frames or 0,
    oracle.hookAnnounced and 'ok' or 'none', tostring(LOG_PATH)))
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
  appendLog('SESSION token='..tostring(token))
end

-- ---------------------------------------------------------------------------
-- Pose application
-- ---------------------------------------------------------------------------

local function applyPose(x, y, z, yaw)
  local wrappedYaw = wrapAngle(yaw)
  state.root:setPosition(vec3(x, y, z))
  state.root:setOrientation(
    vec3(-math.sin(wrappedYaw), 0, math.cos(wrappedYaw)), vec3(0, 1, 0))
  if not state.poseApplied then
    state.poseApplied = true
    -- The first pose teleports the node hundreds of metres away from the scene
    -- origin; without this the interpolated motion state is garbage for a
    -- frame (SDK ac_apps/lib.lua:9687).
    pcall(function() state.root:clearMotion() end)
  end
  state.ghostX, state.ghostY, state.ghostZ = x, y, z
end

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

  -- Yaw is interpolated along the shortest arc and wrapped into [-pi, pi].
  local yawDelta = wrapAngle(b.rotationRad[2] - a.rotationRad[2])
  local yaw = wrapAngle(a.rotationRad[2] + yawDelta * k)

  applyPose(x, y, z, yaw)
  return a
end

local function steerWheels(steerRad, rollDeltaRad)
  local wheels = state.wheels
  if wheels[1] and wheels[2] then
    pcall(function()
      wheels[1]:setOrientation(vec3(steerRad, 0, 1))
      wheels[2]:setOrientation(vec3(steerRad, 0, 1))
    end)
  end
  if wheels[3] and wheels[4] and rollDeltaRad ~= 0 then
    -- rotate() is relative to the current orientation, so feed it a per-frame
    -- delta instead of an absolute time (the spike's absolute value crept).
    pcall(function()
      wheels[3]:rotate(vec3(1, 0, 0), rollDeltaRad)
      wheels[4]:rotate(vec3(1, 0, 0), rollDeltaRad)
    end)
  end
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

  if runtime then
    state.runtimeTime = (state.runtimeTime + step) % runtimeDurationS
    local frame = sampleRuntimeFrame(state.runtimeTime)
    local steer = math.rad(frame and frame.steerAngleDeg or 0)
    steerWheels(steer, step * 5)
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
    steerWheels(math.sin(timeS * 0.5) * 0.25, step * 5)
  end

  state.logAccum = state.logAccum + step
  while state.logAccum >= LOG_INTERVAL_S do
    state.logAccum = state.logAccum - LOG_INTERVAL_S
    writeSample()
  end
end

function script.update(dt)
  local ok, err = pcall(function()
    local sim = ac.getSim()
    local step = type(dt) == 'number' and dt or 0
    if step < 0 then step = 0 end
    state.clock = state.clock + step

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

function script.windowMain(dt)
  if state.status == 'ACTIVE' then
    ui.text('GhostCarGenerator Hotlap: ACTIVE ['..tostring(state.mode)..']')
    ui.text(string.format('ghost %.1f %.1f %.1f  dist %.1f m  t %.1f s',
      state.ghostX, state.ghostY, state.ghostZ,
      dist3(state.ghostX, state.ghostY, state.ghostZ,
        state.playerX, state.playerY, state.playerZ),
      state.runtimeTime))
    ui.text(string.format('oracle ndc %s %s  hook %s',
      fmtNumber(oracle.ndcX), fmtNumber(oracle.ndcY),
      oracle.hookAnnounced and ('ok/'..oracle.hookFrames) or 'never'))
  elseif state.status == 'ERROR' then
    ui.text('GhostCarGenerator Hotlap: ERROR: '..tostring(state.errorMessage))
  else
    ui.text('GhostCarGenerator Hotlap: INIT waiting for car/scene')
  end
end

function script.shutdown()
  disposeGhost()
  if renderSubscription then
    pcall(function() renderSubscription:dispose() end)
    renderSubscription = nil
  end
  appendLog('SHUTDOWN')
end

function script.windowSettings(dt)
  ui.text('GhostCarGenerator Hotlap '..APP_VERSION)
  ui.text('status: '..tostring(state.status)..' / '..tostring(state.mode or '-'))
  ui.text('runtime: '..(runtime and (tostring(#runtime.frames)..' frames') or tostring(runtimeError)))
  ui.text('model: '..tostring(state.modelPath or GHOST_MODEL_RELATIVE))
  ui.text('clock: '..tostring(state.clockSource or '-'))
  ui.text('oracle: '..(oracle.hookAnnounced and ('hook ok, ndc '..fmtNumber(oracle.ndcX)
    ..' '..fmtNumber(oracle.ndcY)) or 'hook not fired'))
  ui.text('log: '..tostring(LOG_PATH))
  if ui.button('Copy log path') then
    ui.setClipboardText(LOG_PATH or '')
  end
end
