-- GhostCarGenerator CSP spike.
--
-- This intentionally reads the small ghost-runtime-v1 JSON timeline rather
-- than .acreplay bytes. It answers the next go/no-go question: can CSP create
-- a second visual car and move its root independently of the player's car?

local player = nil
local root = nil
local wheels = {}
local errorMessage = nil

-- Initial G GR86 Premium model. Keep this explicit for the first spike so the
-- test is deterministic and does not depend on the active car's model naming.
local offset = vec3(5, 0, 0)
local initialized = false
local runtime = nil
local runtimeTime = 0

local function initialize()
  if initialized then return true end
  player = ac.getCar(0)
  if not player then return false end
  local carsRoot = ac.findNodes('carsRoot:yes')
  if not carsRoot then
    errorMessage = 'carsRoot node is unavailable'
    return false
  end
  root = carsRoot:createBoundingSphereNode('GhostCarGenerator.Spike', 3)
  root:setVirtualCarFlag(true)
  local modelFilename = ac.getFolder(ac.FolderID.ContentCars)
    ..'/ig_toyota_gr86_premium/toyota_gr86_premium.kn5'
  local model = root:loadKN5(modelFilename)
  if not model then
    errorMessage = 'could not load Initial G GR86 KN5'
    return false
  end
  wheels = {
    model:findNodes('WHEEL_LF'),
    model:findNodes('WHEEL_RF'),
    model:findNodes('WHEEL_LR'),
    model:findNodes('WHEEL_RR'),
  }
  initialized = true
  return true
end

local runtimeFilename = __dirname..'/ghost-runtime-v1.json'
if io.fileExists(runtimeFilename) then
  local ok, decoded = pcall(json.decode, io.load(runtimeFilename, '{}'))
  if ok and decoded and decoded.schema == 'ghost-runtime-v1' and #decoded.frames > 0 then
    runtime = decoded
  else
    ac.warn('GhostCarGenerator: invalid ghost-runtime-v1.json; using spike animation')
  end
end

local function applyRuntimeFrame(frame)
  local p = frame.positionM
  local r = frame.rotationRad
  root:setPosition(vec3(p[1], p[2], p[3]))
  root:setOrientation(vec3(-math.sin(r[2]), 0, math.cos(r[2])), vec3(0, 1, 0))
end

local function update(dt)
  if not initialize() then return end
  if runtime then
    runtimeTime = runtimeTime + dt
    local frames = runtime.frames
    local last = frames[#frames]
    local t = runtimeTime % last.timeS
    local nextIndex = 1
    while nextIndex < #frames and frames[nextIndex + 1].timeS < t do
      nextIndex = nextIndex + 1
    end
    local a = frames[nextIndex]
    local b = frames[math.min(nextIndex + 1, #frames)]
    local span = math.max(1e-6, b.timeS - a.timeS)
    local k = math.saturate((t - a.timeS) / span)
    local p = {
      a.positionM[1] + (b.positionM[1] - a.positionM[1]) * k,
      a.positionM[2] + (b.positionM[2] - a.positionM[2]) * k,
      a.positionM[3] + (b.positionM[3] - a.positionM[3]) * k,
    }
    local r = {a.rotationRad[1], a.rotationRad[2], a.rotationRad[3]}
    applyRuntimeFrame({positionM = p, rotationRad = r})
  else
  local position = player.position + offset
  root:setPosition(position)
  root:setOrientation(player.look, player.up)

  -- Exercise one steering transform and one rolling transform.  The exact
  -- values are intentionally obvious in-game and are not telemetry-derived.
  local steer = math.sin(ac.getSim().time * 0.001) * 0.25
  wheels[1]:setOrientation(vec3(steer, 0, 1))
  wheels[2]:setOrientation(vec3(steer, 0, 1))
  wheels[3]:rotate(vec3(1, 0, 0), ac.getSim().time * 0.001)
  wheels[4]:rotate(vec3(1, 0, 0), ac.getSim().time * 0.001)
  end
end

function script.simUpdate(dt)
  update(dt)
end

function script.drawUI()
  if initialized then
    ui.text('GhostCarGenerator CSP spike: second KN5 car active')
  elseif errorMessage then
    ui.text('GhostCarGenerator: '..errorMessage)
  end
end

function script.shutdown()
  if root then root:dispose() end
end
