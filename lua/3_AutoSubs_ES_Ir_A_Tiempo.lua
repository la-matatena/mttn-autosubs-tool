-- 3 - AutoSubs ES - Ir a Tiempo.lua
-- Moves the DaVinci Resolve 20 playhead to the timecode selected in AutoSubs ES.
--
-- Purpose:
--   Free-edition counterpart of POST /api/resolve/jump. On Studio the server
--   moves the playhead directly via the Python API; on Free it only stages the
--   target timecode, and this script applies it.
--
-- How it is launched:
--   install_imac.sh copies this file to Fusion/Scripts/Utility/ as
--   "3 - MTTN AutoSubs - Ir a Tiempo.lua" so it appears under
--   Workspace > Scripts. It runs once and exits.
--
-- Files read (bridge outbox,
-- ~/Library/Application Support/AutoSubsES/bridge/outbox/), written by
-- resolve_bridge.ResolveBridge.jump_playhead():
--   jump_tc.lua    `return { timecode = "HH:MM:SS:FF", ms = <int> }`.
--   jump_tc.table  Same table without `return`, for bmd.readfile.
--   The timecode is already absolute NDF timeline TC (start frame applied).
-- Files written: none.
-- HTTP endpoints called: none.

-- Resolve() is injected when run from the Scripts menu; bmd.scriptapp() is the
-- fallback for other hosts.
local resolve = Resolve()
if not resolve and bmd and bmd.scriptapp then
  resolve = bmd.scriptapp("Resolve")
end

-- Fusion is only needed for the MapPath-based HOME fallback.
local fusion = nil
if resolve and resolve.Fusion then
  fusion = resolve:Fusion()
end
if not fusion and Fusion then
  fusion = Fusion()
end

--- Returns the current user's home directory.
-- Prefers $HOME; if Resolve's sandbox hides os.getenv, derives it from the
-- Fusion "UserData:" path map (macOS-only: matches /Users/<name>).
-- @return string Absolute home path, or "" when it cannot be determined.
local function getHomeDir()
  if os and os.getenv then
    local h = os.getenv("HOME")
    if h and h ~= "" then return h end
  end
  if fusion and fusion.MapPath then
    local userData = fusion:MapPath("UserData:")
    if userData and userData ~= "" then
      local home = userData:match("^(/Users/[^/]+)")
      if home then return home end
    end
  end
  return ""
end

--- Loads a Lua data file, surviving Resolve's partially sandboxed runtimes.
-- Same three-tier strategy as script 2 (keep both copies in sync):
--   1. loadfile(path) and run the `return { ... }` chunk.
--   2. io.open + load/loadstring on the file contents.
--   3. bmd.readfile on the ".table" companion (then on `path` itself).
--      bmd.readfile only parses a bare `{ ... }` table literal and returns
--      nil for a module that starts with `return`.
-- @param path string Absolute path to the ".lua" module.
-- @return table|nil The loaded table, or nil on failure.
-- @return string|nil Error message when every strategy fails.
local function loadLuaModule(path)
  if loadfile then
    local fn, err = loadfile(path)
    if fn then return fn() end
  end
  if io and io.open then
    local f = io.open(path, "r")
    if f then
      local src = f:read("*a")
      f:close()
      local loader = load or loadstring
      if loader then
        local fn, err = loader(src)
        if fn then return fn() end
      end
    end
  end
  if bmd and bmd.readfile then
    local tblPath = path:gsub("%.lua$", ".table")
    local tbl = bmd.readfile(tblPath) or bmd.readfile(path)
    if type(tbl) == "table" then
      return tbl
    end
  end
  return nil, "No se pudo cargar " .. path
end

local homeDir = getHomeDir()
if homeDir == "" then
  print("[AutoSubs ES] Error: No se pudo determinar el directorio HOME del usuario.")
  return
end
local jumpPath = homeDir .. "/Library/Application Support/AutoSubsES/bridge/outbox/jump_tc.lua"
local data, err = loadLuaModule(jumpPath)
if not data or not data.timecode then
  print("[AutoSubs ES] No hay ningún salto de tiempo pendiente: " .. tostring(err))
  return
end

-- Exits silently when Resolve is unreachable or no timeline is open.
if not resolve then return end
local project = resolve:GetProjectManager():GetCurrentProject()
local timeline = project and project:GetCurrentTimeline()
if timeline then
  timeline:SetCurrentTimecode(data.timecode)
  print("[AutoSubs ES] Cabezal movido a " .. tostring(data.timecode))
end
