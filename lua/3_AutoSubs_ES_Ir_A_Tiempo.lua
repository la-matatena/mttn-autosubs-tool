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
if not fusion and bmd and bmd.scriptapp then
  fusion = bmd.scriptapp("Fusion")
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
  local lastErr = nil

  --- Runs a compiled chunk and accepts its result only if it is a table.
  -- pcall keeps a corrupt payload from aborting the script, so the next
  -- strategy still gets a chance.
  -- @param fn function Compiled chunk.
  -- @return table|nil The returned table, or nil (lastErr is updated).
  local function runChunk(fn)
    local ok, result = pcall(fn)
    if ok and type(result) == "table" then return result end
    lastErr = ok and "el archivo no devolvió una tabla" or tostring(result)
    return nil
  end

  if loadfile then
    local fn, err = loadfile(path)
    if fn then
      local tbl = runChunk(fn)
      if tbl then return tbl end
    else
      lastErr = err
    end
  end
  if io and io.open then
    local f = io.open(path, "r")
    if f then
      local src = f:read("*a")
      f:close()
      -- loadstring first: in plain Lua 5.1 `load` only accepts a reader
      -- function and throws on a string; Lua 5.2+ only has `load`.
      local loader = loadstring or load
      if loader and src then
        local okLoad, fn, err = pcall(loader, src)
        if not okLoad then fn, err = nil, fn end
        if fn then
          local tbl = runChunk(fn)
          if tbl then return tbl end
        else
          lastErr = err
        end
      end
    end
  end
  if bmd and bmd.readfile then
    local tblPath = path:gsub("%.lua$", ".table")
    local ok, tbl = pcall(function()
      return bmd.readfile(tblPath) or bmd.readfile(path)
    end)
    if ok and type(tbl) == "table" then
      return tbl
    end
  end
  local msg = "No se pudo cargar " .. path
  if lastErr then msg = msg .. " (" .. tostring(lastErr) .. ")" end
  return nil, msg
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

-- Exits with a clear message when Resolve, the project, or the timeline is
-- missing, instead of doing nothing.
if not resolve then
  print("[AutoSubs ES] Error: No se pudo conectar con DaVinci Resolve 20.")
  return
end
local projectManager = resolve:GetProjectManager()
local project = projectManager and projectManager:GetCurrentProject()
if not project then
  print("[AutoSubs ES] Error: Abre un proyecto en DaVinci Resolve 20 antes de mover el cabezal.")
  return
end
local timeline = project:GetCurrentTimeline()
if not timeline then
  print("[AutoSubs ES] Error: Abre un timeline activo en DaVinci Resolve 20 antes de mover el cabezal.")
  return
end

local timecode = tostring(data.timecode)
-- SetCurrentTimecode returns false (it does not throw) when the timecode is
-- outside the timeline or the current page has no playhead (e.g. Media or
-- Fusion), so the result must be checked before reporting success.
local ok, moved = pcall(function()
  return timeline:SetCurrentTimecode(timecode)
end)
if ok and moved then
  print("[AutoSubs ES] Cabezal movido a " .. timecode)
else
  print("[AutoSubs ES] Error: no se pudo mover el cabezal a " .. timecode .. ".")
  print("[AutoSubs ES] Comprueba que el tiempo esté dentro del timeline y que estés en la página Cut, Edit o Fairlight.")
end
