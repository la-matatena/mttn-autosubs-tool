-- 1 - AutoSubs ES - Exportar Audio.lua
-- One-shot script (it does not block the DaVinci Resolve 20 UI).
-- Works on both DaVinci Resolve 20 Free and DaVinci Resolve 20 Studio.
--
-- Purpose:
--   Bounces the active timeline's audio mix to a WAV file and writes timeline
--   metadata so MTTN AutoSubs Tool can transcribe it. This is the Free-edition
--   path; on Studio the server can do the same through the Python API
--   (POST /api/resolve/export-audio), so this script is optional there.
--
-- How it is launched:
--   install_imac.sh copies this file to
--   ~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/
--   Scripts/Utility/ as "1 - MTTN AutoSubs - Exportar Audio.lua", so it shows
--   up under Workspace > Scripts. It runs once and exits; it never loops.
--
-- Files written (bridge inbox,
-- ~/Library/Application Support/AutoSubsES/bridge/inbox/):
--   timeline_audio.wav  48 kHz / 16-bit LinearPCM mix of the whole timeline.
--   timeline_meta.json  Project/timeline names, fps, start/end frames,
--                       resolution, WAV path, and Media Pool Text+ template
--                       names. Read by resolve_bridge.read_timeline_meta().
--   Fusion.prefs        Same JSON stored under Global.AutoSubsES.Meta as a
--                       fallback for sandboxes where io.open is disabled.
--
-- HTTP endpoint called:
--   POST http://127.0.0.1:55120/api/resolve/notify-export (fire-and-forget,
--   1 s timeout) so a running server reloads the inbox WAV and waveform.
--
-- All user-facing messages go to Workspace > Console with an "[AutoSubs ES]"
-- prefix and stay in Spanish.

-- Resolve() is injected when run from the Scripts menu; bmd.scriptapp() is the
-- fallback for other hosts (e.g. the Fusion console).
local resolve = Resolve()
if not resolve and bmd and bmd.scriptapp then
  resolve = bmd.scriptapp("Resolve")
end

-- The Fusion object is only needed for MapPath (HOME fallback) and SetPrefs
-- (Fusion.prefs metadata fallback); try every known way to obtain it.
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

--- Escapes a value for embedding inside a JSON string literal.
-- Handles backslashes, double quotes, and newlines; carriage returns are
-- dropped. Other control characters are not escaped.
-- @param str any Value to escape; nil becomes "".
-- @return string Escaped text without surrounding quotes.
local function jsonEscape(str)
  str = tostring(str or "")
  str = str:gsub("\\", "\\\\")
  str = str:gsub('"', '\\"')
  str = str:gsub("\r", "")
  str = str:gsub("\n", "\\n")
  return str
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

local homeDir = getHomeDir()
if homeDir == "" then
  print("[AutoSubs ES] Error: No se pudo determinar el directorio HOME del usuario.")
  return
end
local bridgeBase = homeDir .. "/Library/Application Support/AutoSubsES/bridge"
local inboxDir = bridgeBase .. "/inbox"

-- Create the inbox up front so neither the render TargetDir nor the
-- timeline_meta.json write fails on a fresh install (installer not run yet).
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

-- Remember the current page: queueing/starting a render can switch Resolve to
-- the Deliver page, and the editor should land back where they were.
local prevPage = "edit"
if resolve.GetCurrentPage then
  local ok, p = pcall(function() return resolve:GetCurrentPage() end)
  if ok and p and p ~= "" then prevPage = p end
end

local projectName = project:GetName() or "Proyecto"
local timelineName = timeline:GetName() or "Timeline"
-- Fallbacks mirror Resolve defaults: 24 fps and a 01:00:00:00 start
-- (86400 frames at 24 fps). Frames are absolute timeline frame numbers.
local fps = tonumber(timeline:GetSetting("timelineFrameRate")) or 24.0
local startFrame = tonumber(timeline:GetStartFrame()) or 86400
local endFrame = tonumber(timeline:GetEndFrame()) or startFrame
local width = tonumber(timeline:GetSetting("timelineResolutionWidth")) or 1920
local height = tonumber(timeline:GetSetting("timelineResolutionHeight")) or 1080

-- Collect Text+ templates available in the Media Pool to report to the app.
local mediaPool = project:GetMediaPool()
local templates = {}

--- Recursively collects Media Pool clip names that look like Text+ templates.
-- A clip matches when its name contains "autosubs" or "text+"
-- (case-insensitive). Matches are appended to the upvalue `templates`.
-- @param folder Folder|nil Media Pool folder to scan; nil is a no-op.
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

-- Render settings snapshot --
-- The audio job below changes the project's current render settings (format,
-- codec, TargetDir, CustomName, ExportVideo, range...). The scripting API has
-- no GetRenderSettings(), so the snapshot is a temporary user render preset
-- (a preset captures the Deliver page settings; whether it also keeps the
-- output location may vary by Resolve build) plus
-- GetCurrentRenderFormatAndCodec() as a fallback if the preset path fails.
-- Every call is wrapped in pcall because older or Free builds may lack some
-- of these methods.

-- Fixed name so a stale preset left by a crashed run is cleaned up next time.
local RESTORE_PRESET_NAME = "AutoSubsES_Restaurar_Render_Temporal"

--- Calls a method on a Resolve API object, swallowing missing-method errors.
-- @param obj table|nil API object (e.g. the project).
-- @param method string Method name.
-- @param ... any Arguments forwarded to the method.
-- @return boolean True when the method exists and returned a truthy value.
-- @return any The method's first return value (nil on failure).
local function safeCall(obj, method, ...)
  if not obj then return false, nil end
  local hasFn, fn = pcall(function() return obj[method] end)
  if not hasFn or not fn then return false, nil end
  local ok, result = pcall(fn, obj, ...)
  if ok and result then return true, result end
  return false, nil
end

local prevFormat, prevCodec = nil, nil
do
  local ok, fc = safeCall(project, "GetCurrentRenderFormatAndCodec")
  if ok and type(fc) == "table" then
    prevFormat, prevCodec = fc.format, fc.codec
  end
end

safeCall(project, "DeleteRenderPreset", RESTORE_PRESET_NAME)
local presetSaved = safeCall(project, "SaveAsNewRenderPreset", RESTORE_PRESET_NAME)

--- Restores the render settings captured before the audio export.
-- Loads (then deletes) the temporary preset; if that is unavailable, at
-- least puts back the previous format/codec. Called once, after the render
-- attempt, whether it succeeded, failed, or threw.
local function restoreRenderSettings()
  local restored = false
  if presetSaved then
    restored = safeCall(project, "LoadRenderPreset", RESTORE_PRESET_NAME)
    safeCall(project, "DeleteRenderPreset", RESTORE_PRESET_NAME)
  end
  if not restored and prevFormat and prevCodec then
    restored = safeCall(project, "SetCurrentRenderFormatAndCodec", prevFormat, prevCodec)
  end
  if not restored then
    print("[AutoSubs ES] Aviso: no se pudieron restaurar los ajustes de render anteriores; revísalos en la página Deliver.")
  end
end

-- Render job --

-- Resolve appends the container extension, producing timeline_audio.wav.
local customName = "timeline_audio"
-- Wall-clock budget for the render and the polling interval, in seconds.
local RENDER_TIMEOUT_S = 180
local POLL_INTERVAL_S = 0.2

--- Sleeps between render polls without burning CPU.
-- bmd.wait is the native Fusion sleep; when a host lacks it, a short shell
-- `sleep` keeps the loop from spinning. If neither exists the call returns
-- immediately (the wall-clock timeout still bounds the loop).
-- @param seconds number Time to sleep, in seconds.
local function sleepBriefly(seconds)
  if bmd and bmd.wait then
    bmd.wait(seconds)
    return
  end
  if os and os.execute then
    pcall(function() os.execute("sleep " .. tostring(seconds)) end)
  end
end

--- Configures, queues, and waits for the audio-only render job.
-- Mutates the project's render settings; the caller restores them with
-- restoreRenderSettings() whatever this returns or throws.
-- @return string Outcome: "ok", "no_job", "not_started", or "timeout".
-- @return number Wall-clock seconds spent waiting for the render.
local function runAudioRender()
  local formatOk = project:SetCurrentRenderFormatAndCodec("wav", "LinearPCM")
  if not formatOk then
    print("[AutoSubs ES] Aviso: usando formato WAV por defecto.")
  end

  project:SetRenderSettings({
    TargetDir = inboxDir,
    CustomName = customName,
    ExportVideo = false,
    ExportAudio = true,
    AudioCodec = "LinearPCM",
    AudioSampleRate = 48000,
    AudioBitDepth = 16,
    SelectAllFrames = true, -- Whole timeline, ignoring any In/Out marks.
  })

  local jobId = project:AddRenderJob()
  if not jobId then
    return "no_job", 0
  end

  local started = project:StartRendering(jobId)
  if not started then
    pcall(function() project:DeleteRenderJob(jobId) end)
    return "not_started", 0
  end

  -- StartRendering returns immediately, so poll IsRenderingInProgress for at
  -- most RENDER_TIMEOUT_S of wall time. os.time() is used rather than
  -- os.clock(): os.clock() measures this process's CPU time, which barely
  -- advances while bmd.wait sleeps (and can run faster than real time while
  -- Resolve's render threads are busy), so it cannot bound a wait. os.time()
  -- has 1 s resolution, which is plenty for a 180 s budget. Without os.time
  -- the loop falls back to counting poll intervals.
  local hasClock = os and os.time and os.difftime
  local t0 = hasClock and os.time() or 0
  local elapsed = 0
  while project:IsRenderingInProgress() and elapsed < RENDER_TIMEOUT_S do
    sleepBriefly(POLL_INTERVAL_S)
    if hasClock then
      elapsed = os.difftime(os.time(), t0)
    else
      elapsed = elapsed + POLL_INTERVAL_S
    end
  end
  local timedOut = project:IsRenderingInProgress()

  -- Drop the job so repeated runs do not pile up entries in the Render Queue.
  pcall(function()
    project:DeleteRenderJob(jobId)
  end)

  return timedOut and "timeout" or "ok", elapsed
end

local renderOk, outcome, elapsed = pcall(runAudioRender)
restoreRenderSettings()

if prevPage and resolve.OpenPage then
  pcall(function() resolve:OpenPage(prevPage) end)
end

if not renderOk then
  print("[AutoSubs ES] Error inesperado durante el render de audio: " .. tostring(outcome))
  return
end
elapsed = tonumber(elapsed) or 0
if outcome == "no_job" then
  print("[AutoSubs ES] Error: no se pudo añadir el trabajo de render de audio.")
  return
end
if outcome == "not_started" then
  print("[AutoSubs ES] Error: Resolve no pudo iniciar el render de audio.")
  return
end
if outcome == "timeout" then
  print(string.format("[AutoSubs ES] Error: el render de audio superó el límite de %.0fs; no se notificó al servidor.", elapsed))
  return
end

local audioPath = inboxDir .. "/" .. customName .. ".wav"
local tmplJsonParts = {}
for _, tname in ipairs(templates) do
  table.insert(tmplJsonParts, '"' .. jsonEscape(tname) .. '"')
end

-- Hand-built JSON (Resolve's Lua has no JSON library). Keys must match what
-- resolve_bridge.read_timeline_meta() and its callers expect.
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

-- Primary channel: timeline_meta.json in the inbox. Some Resolve builds
-- sandbox io.open, so this may silently be skipped (wroteFile stays false).
local wroteFile = false
if io and io.open then
  local f = io.open(inboxDir .. "/timeline_meta.json", "w")
  if f then
    f:write(jsonStr)
    f:close()
    wroteFile = true
  end
end

-- Fallback channel: persist the same JSON in Fusion.prefs. The server parses
-- the AutoSubsES = { Meta = "..." } entry when the JSON file is missing.
if fusion and fusion.SetPrefs then
  pcall(function()
    fusion:SetPrefs("Global.AutoSubsES.Meta", jsonStr)
    fusion:SavePrefs()
  end)
end

-- Wake up the local server so it reloads the WAV and waveform. Backgrounded
-- with "&" and capped at 1 s so Resolve never blocks if the app is closed.
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
