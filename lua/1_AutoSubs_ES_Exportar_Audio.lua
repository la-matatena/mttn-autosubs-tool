-- 1 - AutoSubs ES - Exportar Audio.lua
-- Script de una sola ejecución (no bloquea la interfaz de DaVinci Resolve 20).
-- Funciona tanto en DaVinci Resolve 20 Free como en DaVinci Resolve 20 Studio.

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
if not fusion and bmd and bmd.scriptapp then
  fusion = bmd.scriptapp("Fusion")
end

local function jsonEscape(str)
  str = tostring(str or "")
  str = str:gsub("\\", "\\\\")
  str = str:gsub('"', '\\"')
  str = str:gsub("\r", "")
  str = str:gsub("\n", "\\n")
  return str
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

local homeDir = getHomeDir()
local bridgeBase = homeDir .. "/Library/Application Support/AutoSubsES/bridge"
local inboxDir = bridgeBase .. "/inbox"

if os and os.execute then
  pcall(function()
    os.execute('mkdir -p "' .. inboxDir .. '"')
  end)
end

if not resolve then
  print("[AutoSubs ES] Error: No se pudo conectar con DaVinci Resolve 20.")
  return
end

local projectManager = resolve:GetProjectManager()
local project = projectManager and projectManager:GetCurrentProject()
if not project then
  print("[AutoSubs ES] Abre un proyecto antes de exportar audio.")
  return
end

local timeline = project:GetCurrentTimeline()
if not timeline then
  print("[AutoSubs ES] Abre un timeline activo en DaVinci Resolve 20.")
  return
end

local prevPage = "edit"
if resolve.GetCurrentPage then
  local ok, p = pcall(function() return resolve:GetCurrentPage() end)
  if ok and p and p ~= "" then prevPage = p end
end

local projectName = project:GetName() or "Proyecto"
local timelineName = timeline:GetName() or "Timeline"
local fps = tonumber(timeline:GetSetting("timelineFrameRate")) or 24.0
local startFrame = tonumber(timeline:GetStartFrame()) or 86400
local endFrame = tonumber(timeline:GetEndFrame()) or startFrame
local width = tonumber(timeline:GetSetting("timelineResolutionWidth")) or 1920
local height = tonumber(timeline:GetSetting("timelineResolutionHeight")) or 1080

-- Buscar plantillas Text+ disponibles en el Media Pool para informar a la app.
local mediaPool = project:GetMediaPool()
local templates = {}
local function scanFolder(folder)
  if not folder then return end
  local clips = folder:GetClipList() or {}
  for _, clip in ipairs(clips) do
    local name = clip:GetClipProperty("Clip Name") or clip:GetName() or ""
    local lname = string.lower(name)
    if string.find(lname, "autosubs", 1, true) or string.find(lname, "text+", 1, true) then
      table.insert(templates, name)
    end
  end
  local subs = folder:GetSubFolderList() or {}
  for _, sub in ipairs(subs) do
    scanFolder(sub)
  end
end
if mediaPool then
  scanFolder(mediaPool:GetRootFolder())
end

print("[AutoSubs ES] Exportando mezcla de audio del timeline: " .. timelineName)

local formatOk = project:SetCurrentRenderFormatAndCodec("wav", "LinearPCM")
if not formatOk then
  print("[AutoSubs ES] Aviso: usando formato WAV por defecto.")
end

local customName = "timeline_audio"
project:SetRenderSettings({
  TargetDir = inboxDir,
  CustomName = customName,
  ExportVideo = false,
  ExportAudio = true,
  AudioCodec = "LinearPCM",
  AudioSampleRate = 48000,
  AudioBitDepth = 16,
  SelectAllFrames = true,
})

local jobId = project:AddRenderJob()
if not jobId then
  print("[AutoSubs ES] Error: no se pudo añadir el trabajo de render de audio.")
  return
end

local started = project:StartRendering(jobId)
if not started then
  print("[AutoSubs ES] Error: Resolve no pudo iniciar el render de audio.")
  return
end

local elapsed = 0.0
while project:IsRenderingInProgress() and elapsed < 180.0 do
  if bmd and bmd.wait then
    bmd.wait(0.2)
  end
  elapsed = elapsed + 0.2
end

pcall(function()
  project:DeleteRenderJob(jobId)
end)

if prevPage and resolve.OpenPage then
  pcall(function() resolve:OpenPage(prevPage) end)
end

local audioPath = inboxDir .. "/" .. customName .. ".wav"
local tmplJsonParts = {}
for _, tname in ipairs(templates) do
  table.insert(tmplJsonParts, '"' .. jsonEscape(tname) .. '"')
end

local jsonStr = string.format(
  '{\n' ..
  '  "project_name": "%s",\n' ..
  '  "timeline_name": "%s",\n' ..
  '  "fps": %.4f,\n' ..
  '  "start_frame": %d,\n' ..
  '  "end_frame": %d,\n' ..
  '  "width": %d,\n' ..
  '  "height": %d,\n' ..
  '  "audio_path": "%s",\n' ..
  '  "media_pool_templates": [%s]\n' ..
  '}\n',
  jsonEscape(projectName),
  jsonEscape(timelineName),
  fps,
  math.floor(startFrame),
  math.floor(endFrame),
  math.floor(width),
  math.floor(height),
  jsonEscape(audioPath),
  table.concat(tmplJsonParts, ", ")
)

local wroteFile = false
if io and io.open then
  local f = io.open(inboxDir .. "/timeline_meta.json", "w")
  if f then
    f:write(jsonStr)
    f:close()
    wroteFile = true
  end
end

if fusion and fusion.SetPrefs then
  pcall(function()
    fusion:SetPrefs("Global.AutoSubsES.Meta", jsonStr)
    fusion:SavePrefs()
  end)
end

if os and os.execute then
  pcall(function()
    os.execute('curl -s -m 1 -X POST http://127.0.0.1:55120/api/resolve/notify-export >/dev/null 2>&1 &')
  end)
end

print(string.format(
  "[AutoSubs ES] Audio listo en %s (%.1fs, meta=%s). Abre AutoSubs ES en tu navegador.",
  audioPath,
  elapsed,
  tostring(wroteFile)
))
