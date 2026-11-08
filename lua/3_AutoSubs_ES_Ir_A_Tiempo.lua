-- 3 - AutoSubs ES - Ir a Tiempo.lua
-- Mueve el cabezal de DaVinci Resolve 20 al timecode seleccionado en AutoSubs ES.

local resolve = Resolve()
if not resolve and bmd and bmd.scriptapp then
  resolve = bmd.scriptapp("Resolve")
end

local fusion = nil
if resolve and resolve.Fusion then
  fusion = resolve:Fusion()
end
if not fusion and Fusion then
  fusion = Fusion()
end

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
  return "~"
end

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

local jumpPath = getHomeDir() .. "/Library/Application Support/AutoSubsES/bridge/outbox/jump_tc.lua"
local data, err = loadLuaModule(jumpPath)
if not data or not data.timecode then
  print("[AutoSubs ES] No hay ningún salto de tiempo pendiente: " .. tostring(err))
  return
end

if not resolve then return end
local project = resolve:GetProjectManager():GetCurrentProject()
local timeline = project and project:GetCurrentTimeline()
if timeline then
  timeline:SetCurrentTimecode(data.timecode)
  print("[AutoSubs ES] Cabezal movido a " .. tostring(data.timecode))
end
