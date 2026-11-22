-- GhostCarGenerator Hotlap app for CSP.
--
-- Renders a second, visual-only KN5 car in a normal Hotlap session (no CSP
-- new-mode required). The body pose is driven by the `ghost-runtime-v1.json`
-- timeline sitting next to this file; when that file is missing the app falls
-- back to a static 5 m offset that follows the player so a second car still
-- renders in an otherwise empty install.
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
--       time,ghostX,ghostY,ghostZ,playerX,playerY,playerZ,dist
--     `time` is `ac.getSim().gameTime` in seconds; coordinates are metres; the
--     ghost coordinates are the values handed to `setPosition`.
--   * On load a single `INIT ok ...` or `INIT missing-runtime ...` line is
--     written. Failures also produce an `ERROR ...` line and an `ac.warn`.
--   * The window text always starts with `GhostCarGenerator Hotlap: ACTIVE`
--     or `GhostCarGenerator Hotlap: ERROR: ...` so screenshots can be asserted
--     on without OCR of the numbers.

local GHOST_MODEL = 'ig_toyota_gr86_premium/toyota_gr86_premium.kn5'
local RUNTIME_FILENAME = __dirname .. '/ghost-runtime-v1.json'
local LOG_RELATIVE_DIR = 'Documents/Assetto Corsa/logs'
local LOG_FILENAME = 'ghostcar_hotlap_verify.log'
local LOG_INTERVAL_S = 1.0
local FALLBACK_OFFSET = vec3(5, 0, 0)
local GHOST_NODE_NAME = 'GhostCarGenerator.Hotlap'
local GHOST_NODE_RADIUS = 3

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
  wheels = {},
  runtimeTime = 0,
  runtimeCursor = 1,
  logAccum = 0,
  ghostX = 0, ghostY = 0, ghostZ = 0,
  playerX = 0, playerY = 0, playerZ = 0,
  clock = 0,
  nextInitAttemptAt = 0,
  initFailures = 0,
}

-- Retry scene setup once a second (the 3D scene can still be loading when the
-- app script runs) and only report a hard error after this many failures.
local MAX_INIT_FAILURES = 15

local runtime = nil
local runtimeDurationS = 0

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
    'INIT ok schema=%s frames=%d durationS=%.3f carId=%s trackId=%s log=%s',
    tostring(runtime.schema), #runtime.frames, runtimeDurationS,
    tostring(runtime.carId), tostring(runtime.trackId), tostring(LOG_PATH)))
else
  appendLog(string.format(
    'INIT %s fallback=5m-follow player log=%s', tostring(runtimeError), tostring(LOG_PATH)))
  ac.warn('GhostCarGenerator Hotlap: '..tostring(runtimeError)..'; using 5 m fallback')
end

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
  if root then
    pcall(function() root:dispose() end)
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
    created:setVirtualCarFlag(true)
    local modelFilename = ac.getFolder(ac.FolderID.ContentCars)..'/'..GHOST_MODEL
    local loaded = created:loadKN5(modelFilename)
    if not loaded then
      pcall(function() created:dispose() end)
      return nil, 'could not load '..modelFilename
    end
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
  state.logAccum = LOG_INTERVAL_S -- log the first sample immediately
  appendLog(string.format('ACTIVE mode=%s frames=%d log=%s',
    tostring(state.mode), runtime and #runtime.frames or 0, tostring(LOG_PATH)))
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

local function writeSample()
  local sim = ac.getSim()
  local time = (sim and sim.gameTime) or 0
  appendLog(string.format('%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f',
    time,
    state.ghostX, state.ghostY, state.ghostZ,
    state.playerX, state.playerY, state.playerZ,
    dist3(state.ghostX, state.ghostY, state.ghostZ,
      state.playerX, state.playerY, state.playerZ)))
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
    ui.text(string.format('ghost %.1f %.1f %.1f  dist %.1f m',
      state.ghostX, state.ghostY, state.ghostZ,
      dist3(state.ghostX, state.ghostY, state.ghostZ,
        state.playerX, state.playerY, state.playerZ)))
  elseif state.status == 'ERROR' then
    ui.text('GhostCarGenerator Hotlap: ERROR: '..tostring(state.errorMessage))
  else
    ui.text('GhostCarGenerator Hotlap: INIT waiting for car/scene')
  end
end

function script.shutdown()
  disposeGhost()
  appendLog('SHUTDOWN')
end

function script.windowSettings(dt)
  ui.text('GhostCarGenerator Hotlap')
  ui.text('status: '..tostring(state.status)..' / '..tostring(state.mode or '-'))
  ui.text('runtime: '..(runtime and (tostring(#runtime.frames)..' frames') or tostring(runtimeError)))
  ui.text('model: '..GHOST_MODEL)
  ui.text('log: '..tostring(LOG_PATH))
  if ui.button('Copy log path') then
    ui.setClipboardText(LOG_PATH or '')
  end
end
