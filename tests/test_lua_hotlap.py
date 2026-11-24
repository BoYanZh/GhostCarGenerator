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
ui = {{
  windowSize = function() return vec2(400, 300) end,
  drawRectFilled = function() end,
  drawRect = function() end,
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
car0.speedKmh = 95.0
for step = 1, 70 do
  sim.gameTime = sim.gameTime + 0.1
  sim.dt = 0.1
  car0.lapTimeMs = 1600 + step * 100
  car0.splinePosition = 0.05 + step * 0.012
  if step == 20 then car0.currentSector = 1 end
  if step == 50 then car0.currentSector = 2 end
  local theta = car0.splinePosition * 2 * math.pi
  car0.position = makeVec3(50 * math.cos(theta), 0, 50 * math.sin(theta))
  script.update(0.1)
end

-- 3. Render both UI windows (verifies translucent bg + delta-t formatting).
uiTexts = {{}}
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
