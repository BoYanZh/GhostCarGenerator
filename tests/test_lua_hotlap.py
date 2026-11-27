"""Static undefined-global check and headless Lua runtime smoke tests for ghostcar_hotlap.lua."""
from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LUA_SCRIPT = REPO_ROOT / "src" / "resources" / "csp" / "ghostcar_hotlap" / "ghostcar_hotlap.lua"

ALLOWED_LUA_GLOBALS = frozenset(
    {
        # Lua standard library
        "_G",
        "assert",
        "bit",
        "error",
        "getmetatable",
        "io",
        "ipairs",
        "math",
        "next",
        "os",
        "pairs",
        "pcall",
        "print",
        "rawget",
        "rawset",
        "require",
        "select",
        "setmetatable",
        "string",
        "table",
        "tonumber",
        "tostring",
        "type",
        "unpack",
        "xpcall",
        # CSP Lua app runtime globals
        "__dirname",
        "ac",
        "json",
        "JSON",
        "render",
        "rgb",
        "rgbm",
        "script",
        "ui",
        "vec2",
        "vec3",
        "vec4",
    }
)


def _find_luac() -> str | None:
    return shutil.which("luac")


def _find_lua() -> str | None:
    return shutil.which("lua")


@pytest.mark.skipif(_find_luac() is None, reason="luac not installed")
def test_lua_syntax_and_no_undefined_globals():
    """Verify Lua syntax and fail on any accidental undefined global symbol."""
    luac = _find_luac()
    assert luac is not None
    proc = subprocess.run(
        [luac, "-l", "-p", str(LUA_SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"luac syntax check failed:\n{proc.stderr}"

    # Lua 5.2+ emits:  GETTABUP ... ; _ENV "sym"
    # Lua 5.1 emits:   GETGLOBAL ... ; sym
    found_globals: set[str] = set()
    env_pattern = re.compile(r'GETTABUP\b.*;\s*_ENV\s+"([^"]+)"')
    global_pattern = re.compile(r"GETGLOBAL\b.*;\s*([A-Za-z_][A-Za-z0-9_]*)")
    for line in proc.stdout.splitlines():
        m = env_pattern.search(line)
        if m:
            found_globals.add(m.group(1))
            continue
        m2 = global_pattern.search(line)
        if m2:
            found_globals.add(m2.group(1))

    assert found_globals, "Failed to parse any global references from luac -l output"
    unexpected = sorted(found_globals - ALLOWED_LUA_GLOBALS)
    assert not unexpected, (
        f"Undefined or non-whitelisted Lua global(s) in {LUA_SCRIPT.name}: {unexpected}"
    )


@pytest.mark.skipif(_find_lua() is None, reason="lua interpreter not installed")
def test_lua_headless_runtime_smoke(tmp_path: Path):
    """Run ghostcar_hotlap.lua headlessly with a minimal CSP mock through launch, delta-t, and sector reset."""
    lua = _find_lua()
    assert lua is not None

    # Create a small 120-frame (1.8 s) synthetic runtime ghost timeline in tmp_path.
    frames = []
    count = 120
    dt = 0.015
    for i in range(count):
        theta = 2.0 * math.pi * i / count
        frames.append(
            {
                "timeS": round(i * dt, 6),
                "positionM": [round(50.0 * math.cos(theta), 4), 0.0, round(50.0 * math.sin(theta), 4)],
                "rotationRad": [0.0, round(theta, 6), 0.0],
                "steerAngleDeg": 10.0,
                "velocityMS": [0.0, 0.0, 30.0],
            }
        )
    runtime_json = {
        "schema": "ghost-runtime-v1",
        "carId": "ig_toyota_gr86_premium",
        "trackId": "synth_track/loop",
        "frameIntervalS": dt,
        "lapTimeS": round(count * dt, 3),
        "frames": frames,
    }
    (tmp_path / "ghost-runtime-v1.json").write_text(
        json.dumps(runtime_json, separators=(",", ":")), encoding="utf-8"
    )

    harness = tmp_path / "harness.lua"
    harness.write_text(
        f"""
__dirname = {json.dumps(str(tmp_path))}
script = {{}}
math.saturate = math.saturate or function(x)
  if x < 0 then return 0 elseif x > 1 then return 1 else return x end
end
math.atan2 = math.atan2 or math.atan

local function makeVec3(x, y, z)
  return {{ x = x or 0, y = y or 0, z = z or 0 }}
end
function vec2(x, y) return {{ x = x or 0, y = y or 0 }} end
function vec3(x, y, z) return makeVec3(x, y, z) end
function rgbm(r, g, b, m) return {{ r = r or 0, g = g or 0, b = b or 0, mult = m or 1 }} end

-- Minimal JSON decoder using Lua load() after converting JSON array/object syntax.
json = {{}}
function json.decode(text)
  local lua_src = text
    :gsub('%[', '{{')
    :gsub('%]', '}}')
    :gsub('"([^"]+)"%s*:', '["%1"]=')
    :gsub('=true', '=true')
    :gsub('=false', '=false')
    :gsub('=null', '=nil')
  local fn, err = load('return ' .. lua_src)
  assert(fn, err)
  return fn()
end

-- Extend standard io with CSP io helpers.
function io.fileExists(path)
  local f = io.open(path, 'r')
  if f then f:close(); return true end
  return false
end
function io.load(path, fallback)
  local f = io.open(path, 'r')
  if not f then return fallback end
  local data = f:read('*a')
  f:close()
  return data
end
function io.save(path, content)
  local f = io.open(path, 'w')
  if not f then return false end
  f:write(content)
  f:close()
  return true
end
function io.createDir(_) return true end

local function makeNode(name)
  local node = {{ _name = name or 'node', _active = true }}
  function node:createBoundingSphereNode(n, _) return makeNode(n) end
  function node:setVirtualCarFlag(_) end
  function node:loadKN5(_) return makeNode('model') end
  function node:empty() return false end
  function node:name() return self._name end
  function node:active() return self._active end
  function node:childrenCount() return 2 end
  function node:getBoundingBox() return makeVec3(-1, 0, -2), makeVec3(1, 1.5, 2) end
  function node:findNodes(_) return makeNode('wheel') end
  function node:findMeshes(_)
    local m = {{}}
    function m:size() return 2 end
    function m:materialName(i) return i == 1 and 'ext_body' or 'int_dash' end
    function m:at(_) return makeNode('single') end
    function m:ensureUniqueMaterials() end
    function m:applyShaderReplacements(_) end
    function m:setTransparent(_) end
    function m:setBlendMode(_) end
    function m:setDepthMode(_) end
    function m:setShadows(_) end
    function m:excludeFromCubemap(_) end
    function m:excludeFromSecondary(_) end
    function m:setMaterialProperty(_, _) end
    function m:setMaterialTexture(_, _) end
    function m:setVisible(_, _) end
    return m
  end
  function node:ensureUniqueMaterials() end
  function node:setTransparent(_) end
  function node:setBlendMode(_) end
  function node:setDepthMode(_) end
  function node:setShadows(_) end
  function node:excludeFromCubemap(_) end
  function node:excludeFromSecondary(_) end
  function node:setMaterialProperty(_, _) end
  function node:setMaterialTexture(_, _) end
  function node:setVisible(_, _) end
  function node:setPosition(_) end
  function node:setOrientation(_, _) end
  function node:setRotation(_, _) end
  function node:dispose() end
  return node
end

local store = {{}}
local sim = {{
  gameTime = 0.0,
  dt = 0.016,
  isPaused = false,
  isInMainMenu = false,
  currentSessionIndex = 0,
}}
local car0 = {{
  position = makeVec3(50, 0, 0),
  look = makeVec3(0, 0, 1),
  up = makeVec3(0, 1, 0),
  speedKmh = 0.0,
  lapCount = 0,
  lapTimeMs = 0,
  splinePosition = 0.0,
  currentSector = 0,
  isInPit = false,
  isInPitlane = false,
  resetCounter = 0,
}}

ac = {{
  FolderID = {{ Root = 1, Logs = 2, ACDocuments = 3, ContentCars = 4 }},
  getFolder = function(id)
    if id == 2 then return {json.dumps(str(tmp_path / "logs"))} end
    if id == 4 then return {json.dumps(str(tmp_path / "content" / "cars"))} end
    return {json.dumps(str(tmp_path))}
  end,
  getSim = function() return sim end,
  getCar = function(i) return i == 0 and car0 or nil end,
  getTrackID = function() return 'synth_track' end,
  getTrackLayout = function() return 'loop' end,
  findNodes = function(_) return makeNode('carsRoot') end,
  getCameraPosition = function() return makeVec3(50, 2, -5) end,
  isVisibleInMainCamera = function() return true end,
  worldCoordinateToTrackProgress = function(v)
    local a = math.atan2 and math.atan2(v.z, v.x) or math.atan(v.z, v.x)
    if a < 0 then a = a + 2 * math.pi end
    return a / (2 * math.pi)
  end,
  store = function(k, v) store[k] = v end,
  load = function(k) return store[k] end,
  storage = function(defaults, _) return defaults end,
  warn = function(_) end,
  log = function(_) end,
}}

render = {{
  BlendMode = {{ AlphaBlend = 1, Opaque = 0 }},
  DepthMode = {{ Normal = 0 }},
  PassID = {{ Main = 1 }},
  on = function(_, _) return {{ dispose = function() end }} end,
  project = function(_) return makeVec3(0.5, 0.5, 1) end,
  debugCross = function(_, _, _) end,
}}

local uiTexts = {{}}
local barFillColors = {{}}
ui = {{
  Alignment = {{ Center = 1 }},
  windowSize = function() return vec2(400, 300) end,
  availableSpaceX = function() return 300 end,
  getCursor = function() return vec2(0, 0) end,
  drawRectFilled = function(p1, p2, col)
    -- Record interior bar fill colors (height <= 10, not background rect).
    if col and (p2.y - p1.y) <= 10 and (p2.y - p1.y) >= 4 then
      barFillColors[#barFillColors + 1] = col
    end
  end,
  drawRect = function() end,
  drawLine = function() end,
  dummy = function() end,
  dwriteTextAligned = function(t) uiTexts[#uiTexts + 1] = tostring(t) end,
  text = function(t) uiTexts[#uiTexts + 1] = tostring(t) end,
  textColored = function(t, _) uiTexts[#uiTexts + 1] = tostring(t) end,
  checkbox = function(_, _) return false end,
  slider = function(_, v) return v, false end,
  button = function(_) return false end,
  sameLine = function() end,
  setClipboardText = function() end,
}}

-- Create expected KN5 path so loadKN5 check passes.
io.save({json.dumps(str(tmp_path / "content/cars/ig_toyota_gr86_premium/toyota_gr86_premium.kn5"))}, "kn5")
-- Actually create parent dirs via Python below; script runs now.
dofile({json.dumps(str(LUA_SCRIPT))})

-- 1. Settle writer lease (need >= 0.6s of script.update ticks).
for step = 1, 50 do
  sim.gameTime = sim.gameTime + 0.02
  sim.dt = 0.02
  script.update(0.02)
end

-- 2. Launch car and advance across Sector 0 -> Sector 1 -> Sector 2 (>2s apart).
-- Keep lapTimeMs < 2.5 * runtimeDurationS (4.5s) while losing ~0.35 s/s vs ghost.
car0.speedKmh = 95.0
for step = 1, 55 do
  sim.gameTime = sim.gameTime + 0.1
  sim.dt = 0.1
  car0.lapTimeMs = 1600 + step * 50
  car0.splinePosition = 0.05 + step * 0.008
  if step == 20 then car0.currentSector = 1 end
  if step == 45 then car0.currentSector = 2 end
  local theta = car0.splinePosition * 2 * math.pi
  car0.position = makeVec3(50 * math.cos(theta), 0, 50 * math.sin(theta))
  script.update(0.1)
end

-- 3. Render both UI windows (verifies translucent bg + delta-t formatting + red bar color when losing).
uiTexts = {{}}
barFillColors = {{}}
script.windowMain(0.016)
script.windowSettings(0.016)

local foundDelta = false
for _, line in ipairs(uiTexts) do
  if line:find('Delta vs Ghost:', 1, true) then
    foundDelta = true
  end
  assert(not line:find('ERROR:', 1, true), 'UI reported error: ' .. line)
end
assert(foundDelta, 'Delta vs Ghost row missing from UI output')
-- In step 2, player lapTimeMs grew faster than ghost spline (losing time -> bar must turn vivid red, r > 0.85, g < 0.30).
assert(#barFillColors >= 1, 'Expected Delta Bar fill rect to be drawn')
local lastBarCol = barFillColors[#barFillColors]
assert(lastBarCol.r > 0.85 and lastBarCol.g < 0.30,
  string.format('Expected red Delta Bar when losing time, got rgb=(%.2f,%.2f,%.2f)', lastBarCol.r, lastBarCol.g, lastBarCol.b))

script.shutdown()
print('HEADLESS_SMOKE_OK')
""",
        encoding="utf-8",
    )
    # Ensure nested mock directories and KN5 file exist before running Lua.
    kn5_dir = tmp_path / "content" / "cars" / "ig_toyota_gr86_premium"
    kn5_dir.mkdir(parents=True, exist_ok=True)
    (kn5_dir / "toyota_gr86_premium.kn5").write_bytes(b"kn5")
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

    proc = subprocess.run(
        [lua, str(harness)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"Headless Lua smoke failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    assert "HEADLESS_SMOKE_OK" in proc.stdout

    verify_log = tmp_path / "logs" / "ghostcar_hotlap_verify.log"
    assert verify_log.is_file(), "Expected verify log to be written"
    log_text = verify_log.read_text(encoding="utf-8")
    assert "ERROR " not in log_text, f"Unexpected ERROR in headless log:\n{log_text}"
    assert "INIT ok app=" in log_text
    assert "LAPSYNC sector playerSector=1 prevSector=0" in log_text
    assert "LAPSYNC sector playerSector=2 prevSector=1" in log_text


@pytest.mark.skipif(_find_lua() is None, reason="lua interpreter not installed")
def test_lua_idle_on_track_mismatch(tmp_path: Path):
    """Ensure ghostcar_hotlap stays completely idle and never loads KN5 on a different track."""
    lua = _find_lua()
    assert lua is not None

    frames = [
        {
            "timeS": round(i * 0.015, 6),
            "positionM": [float(i), 0.0, float(i)],
            "rotationRad": [0.0, 0.0, 0.0],
            "steerAngleDeg": 0.0,
            "velocityMS": [0.0, 0.0, 20.0],
        }
        for i in range(40)
    ]
    (tmp_path / "ghost-runtime-v1.json").write_text(
        json.dumps(
            {
                "schema": "ghost-runtime-v1",
                "carId": "ig_toyota_gr86_premium",
                "trackId": "thunderhill_raceway_park/threemilebypass",
                "frameIntervalS": 0.015,
                "lapTimeS": 0.6,
                "frames": frames,
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

    harness = tmp_path / "harness_mismatch.lua"
    harness.write_text(
        f"""
__dirname = {json.dumps(str(tmp_path))}
script = {{}}
math.saturate = math.saturate or function(x) return math.max(0, math.min(1, x)) end
math.atan2 = math.atan2 or math.atan
function vec2(x, y) return {{ x = x or 0, y = y or 0 }} end
function vec3(x, y, z) return {{ x = x or 0, y = y or 0, z = z or 0 }} end
function rgbm(r, g, b, m) return {{ r = r or 0, g = g or 0, b = b or 0, mult = m or 1 }} end
json = {{}}
function json.decode(text)
  local lua_src = text:gsub('%[', '{{'):gsub('%]', '}}'):gsub('"([^"]+)"%s*:', '["%1"]=')
  return assert(load('return ' .. lua_src))()
end
function io.fileExists(p) local f = io.open(p, 'r'); if f then f:close(); return true end; return false end
function io.load(p, fb) local f = io.open(p, 'r'); if not f then return fb end; local d = f:read('*a'); f:close(); return d end
function io.save(p, c) local f = io.open(p, 'w'); if not f then return false end; f:write(c); f:close(); return true end
function io.createDir(_) return true end

local store = {{}}
local sim = {{ gameTime = 0, dt = 0.02, isPaused = false, isInMainMenu = false, currentSessionIndex = 0 }}
local car0 = {{ position = vec3(0,0,0), speedKmh = 80, lapCount = 0, lapTimeMs = 5000, splinePosition = 0.95 }}
local kn5Loaded = false
ac = {{
  FolderID = {{ Root = 1, Logs = 2, ContentCars = 4 }},
  getFolder = function(id)
    if id == 2 then return {json.dumps(str(tmp_path / "logs"))} end
    return {json.dumps(str(tmp_path))}
  end,
  getSim = function() return sim end,
  getCar = function(i) return i == 0 and car0 or nil end,
  getTrackID = function() return 'monza' end,
  getTrackLayout = function() return '' end,
  findNodes = function(_)
    return {{
      createBoundingSphereNode = function() kn5Loaded = true; error('should not create ghost node on wrong track') end,
    }}
  end,
  store = function(k, v) store[k] = v end,
  load = function(k) return store[k] end,
  storage = function(d, _) return d end,
  warn = function(_) end,
  log = function(_) end,
}}
render = {{ on = function() return {{ dispose = function() end }} end }}
local uiTexts = {{}}
ui = {{
  windowSize = function() return vec2(320, 100) end,
  drawRectFilled = function() end,
  drawRect = function() end,
  text = function(t) uiTexts[#uiTexts + 1] = tostring(t) end,
}}

dofile({json.dumps(str(LUA_SCRIPT))})
for _ = 1, 45 do
  sim.gameTime = sim.gameTime + 0.02
  script.update(0.02)
end
script.windowMain(0.02)
assert(not kn5Loaded, 'Ghost node/KN5 must not be loaded on a non-matching track')
local sawIdle = false
for _, line in ipairs(uiTexts) do
  if line:find('IDLE (wrong track)', 1, true) then sawIdle = true end
end
assert(sawIdle, 'Expected IDLE (wrong track) in windowMain UI')
script.shutdown()
print('MISMATCH_IDLE_OK')
""",
        encoding="utf-8",
    )

    proc = subprocess.run([lua, str(harness)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"Track mismatch test failed:\n{proc.stdout}\n{proc.stderr}"
    assert "MISMATCH_IDLE_OK" in proc.stdout
    log_text = (tmp_path / "logs" / "ghostcar_hotlap_verify.log").read_text(encoding="utf-8")
    assert "IDLE track-mismatch" in log_text
    assert "ACTIVE mode=" not in log_text
    assert "ERROR " not in log_text


@pytest.mark.skipif(_find_lua() is None, reason="lua interpreter not installed")
def test_lua_multi_ghost_switch_and_hot_reload(tmp_path: Path):
    """Verify tracks/<track>/<layout>/*.json discovery, live Next> switching, and Reload."""
    lua = _find_lua()
    assert lua is not None

    track_dir = tmp_path / "tracks" / "synth_track" / "loop"
    track_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

    def _make_runtime(lap_time_s: float, car_id: str) -> str:
        frames = [
            {
                "timeS": round(i * 0.015, 6),
                "positionM": [float(i), 0.0, float(i)],
                "rotationRad": [0.0, 0.0, 0.0],
                "steerAngleDeg": 0.0,
                "velocityMS": [0.0, 0.0, 20.0],
            }
            for i in range(40)
        ]
        return json.dumps(
            {
                "schema": "ghost-runtime-v1",
                "carId": car_id,
                "trackId": "synth_track/loop",
                "frameIntervalS": 0.015,
                "lapTimeS": lap_time_s,
                "frames": frames,
            },
            separators=(",", ":"),
        )

    (track_dir / "alpha_fast.json").write_text(_make_runtime(95.123, "car_alpha"), encoding="utf-8")
    (track_dir / "beta_slow.json").write_text(_make_runtime(98.456, "car_beta"), encoding="utf-8")

    harness = tmp_path / "harness_multi.lua"
    harness.write_text(
        f"""
__dirname = {json.dumps(str(tmp_path))}
script = {{}}
math.saturate = math.saturate or function(x) return math.max(0, math.min(1, x)) end
math.atan2 = math.atan2 or math.atan
function vec2(x, y) return {{ x = x or 0, y = y or 0 }} end
function vec3(x, y, z) return {{ x = x or 0, y = y or 0, z = z or 0 }} end
function rgbm(r, g, b, m) return {{ r = r or 0, g = g or 0, b = b or 0, mult = m or 1 }} end
json = {{}}
function json.decode(text)
  local lua_src = text:gsub('%[', '{{'):gsub('%]', '}}'):gsub('"([^"]+)"%s*:', '["%1"]=')
  return assert(load('return ' .. lua_src))()
end
function io.fileExists(p) local f = io.open(p, 'r'); if f then f:close(); return true end; return false end
function io.load(p, fb) local f = io.open(p, 'r'); if not f then return fb end; local d = f:read('*a'); f:close(); return d end
function io.save(p, c) local f = io.open(p, 'w'); if not f then return false end; f:write(c); f:close(); return true end
function io.createDir(_) return true end
local dirFiles = {{ 'alpha_fast.json', 'beta_slow.json' }}
function io.scanDir(dir, mask, cb)
  if dir:gsub('\\\\', '/'):find('tracks/synth_track/loop', 1, true) then
    for _, name in ipairs(dirFiles) do cb(name) end
  end
end

local store = {{}}
local sim = {{ gameTime = 0, dt = 0.02, isPaused = false, isInMainMenu = false, currentSessionIndex = 0 }}
local car0 = {{ position = vec3(0,0,0), speedKmh = 80, lapCount = 0, lapTimeMs = 5000, splinePosition = 0.95 }}
local function makeNode()
  local n = {{}}
  function n:createBoundingSphereNode() return makeNode() end
  function n:loadKN5() return makeNode() end
  function n:findMeshes() return {{}} end
  function n:findNodes() return makeNode() end
  function n:setPosition() end
  function n:setOrientation() end
  function n:setVisible() end
  function n:dispose() end
  return n
end
ac = {{
  FolderID = {{ Root = 1, Logs = 2, ContentCars = 4 }},
  getFolder = function(id)
    if id == 2 then return {json.dumps(str(tmp_path / "logs"))} end
    return {json.dumps(str(tmp_path))}
  end,
  getSim = function() return sim end,
  getCar = function(i) return i == 0 and car0 or nil end,
  getTrackID = function() return 'synth_track' end,
  getTrackLayout = function() return 'loop' end,
  findNodes = function(_) return makeNode() end,
  store = function(k, v) store[k] = v end,
  load = function(k) return store[k] end,
  storage = function(d, _) return d end,
  warn = function(_) end,
  log = function(_) end,
}}
render = {{ on = function() return {{ dispose = function() end }} end }}
local clickTarget = nil
local uiTexts = {{}}
ui = {{
  windowSize = function() return vec2(410, 450) end,
  drawRectFilled = function() end,
  drawRect = function() end,
  sameLine = function() end,
  text = function(t) uiTexts[#uiTexts + 1] = tostring(t) end,
  button = function(label)
    if clickTarget == label then
      clickTarget = nil
      return true
    end
    return false
  end,
}}

dofile({json.dumps(str(LUA_SCRIPT))})
for _ = 1, 20 do
  sim.gameTime = sim.gameTime + 0.02
  script.update(0.02)
end
-- Initial ghost should be [1/2] alpha_fast.json
uiTexts = {{}}
script.windowSettings(0.02)
local sawAlpha = false
for _, line in ipairs(uiTexts) do
  if line:find('Ghost [1/2]: alpha_fast.json', 1, true) then sawAlpha = true end
end
assert(sawAlpha, 'Expected initial Ghost [1/2]: alpha_fast.json')

-- Click Next > to switch to beta_slow.json
clickTarget = 'Next >'
script.windowSettings(0.02)
for _ = 1, 5 do
  sim.gameTime = sim.gameTime + 0.02
  script.update(0.02)
end
uiTexts = {{}}
script.windowSettings(0.02)
local sawBeta = false
for _, line in ipairs(uiTexts) do
  if line:find('Ghost [2/2]: beta_slow.json', 1, true) then sawBeta = true end
end
assert(sawBeta, 'Expected switched Ghost [2/2]: beta_slow.json')

-- Click Reload ghosts
clickTarget = 'Reload ghosts'
script.windowSettings(0.02)
script.shutdown()
print('MULTI_GHOST_OK')
""",
        encoding="utf-8",
    )

    proc = subprocess.run([lua, str(harness)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"Multi-ghost test failed:\n{proc.stdout}\n{proc.stderr}"
    assert "MULTI_GHOST_OK" in proc.stdout
    log_text = (tmp_path / "logs" / "ghostcar_hotlap_verify.log").read_text(encoding="utf-8")
    assert "RELOAD reason=ui-next status=ok file=beta_slow.json carId=car_beta" in log_text
    assert "RELOAD reason=ui-reload status=ok file=beta_slow.json" in log_text
    assert "ERROR " not in log_text


@pytest.mark.skipif(_find_lua() is None, reason="lua interpreter not installed")
def test_lua_in_game_import_telemetry_and_combo(tmp_path: Path):
    """Verify clicking 'Import telemetry...' invokes os.openFileDialog + os.runConsoleProcess and hot-loads."""
    lua = _find_lua()
    assert lua is not None

    track_dir = tmp_path / "tracks" / "synth_track" / "loop"
    track_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

    frames = [
        {
            "timeS": round(i * 0.015, 6),
            "positionM": [float(i), 0.0, float(i)],
            "rotationRad": [0.0, 0.0, 0.0],
            "steerAngleDeg": 0.0,
            "velocityMS": [0.0, 0.0, 20.0],
        }
        for i in range(40)
    ]
    imported_json = track_dir / "new_lap.json"
    imported_json.write_text(
        json.dumps(
            {
                "schema": "ghost-runtime-v1",
                "carId": "ks_mazda_mx5_cup",
                "trackId": "synth_track/loop",
                "frameIntervalS": 0.015,
                "lapTimeS": 91.234,
                "frames": frames,
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    harness = tmp_path / "harness_import.lua"
    harness.write_text(
        f"""
__dirname = {json.dumps(str(tmp_path))}
script = {{}}
math.saturate = math.saturate or function(x) return math.max(0, math.min(1, x)) end
math.atan2 = math.atan2 or math.atan
function vec2(x, y) return {{ x = x or 0, y = y or 0 }} end
function vec3(x, y, z) return {{ x = x or 0, y = y or 0, z = z or 0 }} end
function rgbm(r, g, b, m) return {{ r = r or 0, g = g or 0, b = b or 0, mult = m or 1 }} end
json = {{}}
function json.decode(text)
  local lua_src = text:gsub('%[', '{{'):gsub('%]', '}}'):gsub('"([^"]+)"%s*:', '["%1"]=')
  return assert(load('return ' .. lua_src))()
end
function io.fileExists(p) local f = io.open(p, 'r'); if f then f:close(); return true end; return false end
function io.load(p, fb) local f = io.open(p, 'r'); if not f then return fb end; local d = f:read('*a'); f:close(); return d end
function io.save(p, c) local f = io.open(p, 'w'); if not f then return false end; f:write(c); f:close(); return true end
function io.createDir(_) return true end
function io.createFileDir(_) return true end

local scanned = {{}}
function io.scanDir(dir, mask, cb)
  if dir:gsub('\\\\', '/'):find('tracks/synth_track/loop', 1, true) then
    for _, name in ipairs(scanned) do cb(name) end
  end
end

local procCalls = {{}}
os.openFileDialog = function(params, cb)
  cb(nil, 'D:/telemetry/new_lap.ld')
end
os.runConsoleProcess = function(params, cb)
  procCalls[#procCalls + 1] = params
  scanned = {{ 'new_lap.json' }}
  cb(nil, {{
    exitCode = 0,
    stdout = 'Deployed to AC: ' .. {json.dumps(str(imported_json))} .. '\\n',
    stderr = '',
  }})
end

local store = {{}}
local sim = {{ gameTime = 0, dt = 0.02, isPaused = false, isInMainMenu = false, currentSessionIndex = 0 }}
local car0 = {{ position = vec3(0,0,0), speedKmh = 80, lapCount = 0, lapTimeMs = 5000, splinePosition = 0.95 }}
local function makeNode()
  local n = {{}}
  function n:createBoundingSphereNode() return makeNode() end
  function n:loadKN5() return makeNode() end
  function n:findMeshes() return {{}} end
  function n:findNodes() return makeNode() end
  function n:setPosition() end
  function n:setOrientation() end
  function n:setVisible() end
  function n:dispose() end
  return n
end
ac = {{
  FolderID = {{ Root = 1, Logs = 2, ContentCars = 4 }},
  getFolder = function(id)
    if id == 2 then return {json.dumps(str(tmp_path / "logs"))} end
    return {json.dumps(str(tmp_path))}
  end,
  getSim = function() return sim end,
  getCar = function(i) return i == 0 and car0 or nil end,
  getCarID = function(_) return 'ks_mazda_mx5_cup' end,
  getTrackID = function() return 'synth_track' end,
  getTrackLayout = function() return 'loop' end,
  findNodes = function(_) return makeNode() end,
  store = function(k, v) store[k] = v end,
  load = function(k) return store[k] end,
  storage = function(d, _) return d end,
  warn = function(_) end,
  log = function(_) end,
}}
render = {{ on = function() return {{ dispose = function() end }} end }}
local clickTarget = nil
local uiTexts = {{}}
ui = {{
  windowSize = function() return vec2(420, 480) end,
  drawRectFilled = function() end,
  drawRect = function() end,
  sameLine = function() end,
  combo = function(lbl, preview, flags, fn) if fn then fn() end end,
  selectable = function(lbl, sel) return false end,
  text = function(t) uiTexts[#uiTexts + 1] = tostring(t) end,
  button = function(label)
    if clickTarget == label then
      clickTarget = nil
      return true
    end
    return false
  end,
}}

dofile({json.dumps(str(LUA_SCRIPT))})
for _ = 1, 20 do
  sim.gameTime = sim.gameTime + 0.02
  script.update(0.02)
end

clickTarget = 'Import telemetry...'
script.windowSettings(0.02)
assert(#procCalls == 1, 'Expected 1 os.runConsoleProcess call')
local argsStr = table.concat(procCalls[1].arguments, ' ')
assert(argsStr:find('export-runtime', 1, true), 'Missing export-runtime arg')
assert(argsStr:find('--ac-track-dir', 1, true), 'Missing --ac-track-dir arg')
assert(argsStr:find('--ac-layout loop', 1, true), 'Missing --ac-layout loop arg')
assert(argsStr:find('--car-id ks_mazda_mx5_cup', 1, true), 'Missing --car-id arg')
assert(argsStr:find('--deploy-ac', 1, true), 'Missing --deploy-ac arg')

uiTexts = {{}}
script.windowSettings(0.02)
local sawImported = false
for _, line in ipairs(uiTexts) do
  if line:find('Ghost [1/1]: new_lap.json', 1, true) then sawImported = true end
end
assert(sawImported, 'Expected hot-loaded Ghost [1/1]: new_lap.json after import')
script.shutdown()
print('IMPORT_TELEMETRY_OK')
""",
        encoding="utf-8",
    )

    proc = subprocess.run([lua, str(harness)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"Import test failed:\n{proc.stdout}\n{proc.stderr}"
    assert "IMPORT_TELEMETRY_OK" in proc.stdout
    log_text = (tmp_path / "logs" / "ghostcar_hotlap_verify.log").read_text(encoding="utf-8")
    assert "IMPORT start file=new_lap.ld" in log_text
    assert "IMPORT ok file=new_lap.ld" in log_text
    assert "RELOAD reason=ui-import status=ok file=new_lap.json" in log_text
    assert "ERROR " not in log_text


@pytest.mark.skipif(_find_lua() is None, reason="lua interpreter not installed")
def test_lua_brake_lights_from_channel(tmp_path: Path):
    """Verify ghost brake lights toggle ksEmissive from frame brake channel."""
    lua = _find_lua()
    assert lua is not None

    frames = []
    for i in range(40):
        frames.append(
            {
                "timeS": round(i * 0.015, 6),
                "positionM": [float(i), 0.0, float(i)],
                "rotationRad": [0.0, 0.0, 0.0],
                "steerAngleDeg": 0.0,
                "brake": 0.85 if 10 <= i <= 25 else 0.0,
                "velocityMS": [0.0, 0.0, 20.0],
            }
        )
    (tmp_path / "ghost-runtime-v1.json").write_text(
        json.dumps(
            {
                "schema": "ghost-runtime-v1",
                "carId": "ig_toyota_gr86_premium",
                "trackId": "synth_track/loop",
                "frameIntervalS": 0.015,
                "lapTimeS": 0.6,
                "frames": frames,
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    car_dir = tmp_path / "ig_toyota_gr86_premium"
    car_dir.mkdir(parents=True, exist_ok=True)
    (car_dir / "toyota_gr86_premium.kn5").write_bytes(b"sc6969")

    harness = tmp_path / "harness_brake.lua"
    harness.write_text(
        f"""
__dirname = {json.dumps(str(tmp_path))}
script = {{}}
math.saturate = math.saturate or function(x) return math.max(0, math.min(1, x)) end
math.atan2 = math.atan2 or math.atan
function vec2(x, y) return {{ x = x or 0, y = y or 0 }} end
function vec3(x, y, z) return {{ x = x or 0, y = y or 0, z = z or 0 }} end
function rgbm(r, g, b, m) return {{ r = r or 0, g = g or 0, b = b or 0, mult = m or 1 }} end
json = {{}}
function json.decode(text)
  local lua_src = text:gsub('%[', '{{'):gsub('%]', '}}'):gsub('"([^"]+)"%s*:', '["%1"]=')
  return assert(load('return ' .. lua_src))()
end
function io.fileExists(p) local f = io.open(p, 'r'); if f then f:close(); return true end; return false end
function io.load(p, fb) local f = io.open(p, 'r'); if not f then return fb end; local d = f:read('*a'); f:close(); return d end
function io.save(p, c) local f = io.open(p, 'w'); if not f then return false end; f:write(c); f:close(); return true end
function io.createDir(_) return true end

local brakeEmissiveLog = {{}}
local function makeBrakeMesh()
  local m = {{}}
  function m:empty() return false end
  function m:setVisible() end
  function m:setMaterialProperty(k, v)
    if k == 'ksEmissive' then
      brakeEmissiveLog[#brakeEmissiveLog + 1] = v.x
    end
  end
  return m
end
local function makeNode()
  local n = {{}}
  function n:empty() return false end
  function n:createBoundingSphereNode() return makeNode() end
  function n:loadKN5() return makeNode() end
  function n:findMeshes(pat)
    if pat == 'RearLight_0' then return makeBrakeMesh() end
    return {{ empty = function() return true end }}
  end
  function n:findNodes() return makeNode() end
  function n:ensureUniqueMaterials() end
  function n:setTransparent() end
  function n:setBlendMode() end
  function n:setDepthMode() end
  function n:setShadows() end
  function n:excludeFromCubemap() end
  function n:excludeFromSecondary() end
  function n:setMaterialProperty() end
  function n:setMaterialTexture() end
  function n:setPosition() end
  function n:setOrientation() end
  function n:setRotation() end
  function n:setVirtualCarFlag() end
  function n:setVisible() end
  function n:dispose() end
  return n
end

local store = {{}}
local sim = {{ gameTime = 0, dt = 0.02, isPaused = false, isInMainMenu = false, currentSessionIndex = 0 }}
local car0 = {{ position = vec3(0,0,0), speedKmh = 80, lapCount = 0, lapTimeMs = 5000, splinePosition = 0.95, isInPit = false, isInPitlane = false, resetCounter = 0 }}
ac = {{
  FolderID = {{ Root = 1, Logs = 2, ContentCars = 4 }},
  getFolder = function(id)
    if id == 2 then return {json.dumps(str(tmp_path / "logs"))} end
    return {json.dumps(str(tmp_path))}
  end,
  INIConfig = {{
    carData = function(_, fname)
      if fname == 'lights.ini' then
        return {{
          get = function(_, sec, key, def)
            if sec == 'BRAKE_0' and key == 'NAME' then return 'RearLight_0' end
            if sec == 'BRAKE_0' and key == 'COLOR' then return '400,0,0' end
            return def
          end,
        }}
      end
      return nil
    end,
  }},
  getSim = function() return sim end,
  getCar = function(i) return i == 0 and car0 or nil end,
  getCarID = function(_) return 'ig_toyota_gr86_premium' end,
  getTrackID = function() return 'synth_track' end,
  getTrackLayout = function() return 'loop' end,
  findNodes = function(_) return makeNode() end,
  store = function(k, v) store[k] = v end,
  load = function(k) return store[k] end,
  storage = function(d, _) return d end,
  warn = function(_) end,
  log = function(_) end,
}}
render = {{ on = function() return {{ dispose = function() end }} end }}
ui = {{}}

dofile({json.dumps(str(LUA_SCRIPT))})
for i = 1, 80 do
  sim.gameTime = sim.gameTime + 0.02
  car0.lapTimeMs = car0.lapTimeMs + 20
  car0.position.z = car0.position.z + 0.5
  car0.splinePosition = (car0.splinePosition + 0.005) % 1.0
  script.update(0.02)
end
script.shutdown()

local sawOn, sawOffAfterOn = false, false
for _, r in ipairs(brakeEmissiveLog) do
  if r >= 300 then sawOn = true end
  if sawOn and r == 0 then sawOffAfterOn = true end
end
assert(sawOn, 'Expected brake light ksEmissive >= 300 during braking frames')
assert(sawOffAfterOn, 'Expected brake light ksEmissive to return to 0 after braking')
print('BRAKE_LIGHTS_OK')
""",
        encoding="utf-8",
    )

    proc = subprocess.run([lua, str(harness)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"Brake lights test failed:\n{proc.stdout}\n{proc.stderr}"
    assert "BRAKE_LIGHTS_OK" in proc.stdout
    log_text = (tmp_path / "logs" / "ghostcar_hotlap_verify.log").read_text(encoding="utf-8")
    assert "BRAKELIGHTS count=1 headlights=0 discs=0 source=carData" in log_text
    assert "ERROR " not in log_text
