-- 2 - AutoSubs ES - Insertar Subtitulos.lua
-- Inserts subtitles prepared by AutoSubs ES into DaVinci Resolve 20 (Free and
-- Studio).
--
-- Purpose:
--   Reads the payload staged by "Enviar a DaVinci Resolve 20" in MTTN AutoSubs
--   Tool and places the cues on the current timeline using one of three modes:
--     subtitle_srt  Import the SRT and append it to a new subtitle (ST) track.
--     fcpxml        Import the FCPXML as a new timeline (Basic Titles).
--     textplus      Append one Media Pool Text+ template clip per cue on a new
--                   video track and style each clip's Fusion comp. Falls back
--                   to the FCPXML import when no template is in the Media Pool.
--   On Studio the server usually inserts directly via the Python API; this
--   script is the Free-edition path.
--
-- How it is launched:
--   install_imac.sh copies this file to Fusion/Scripts/Utility/ as
--   "2 - MTTN AutoSubs - Insertar Subtitulos.lua" so it appears under
--   Workspace > Scripts. It runs once and exits.
--
-- Files read (bridge outbox,
-- ~/Library/Application Support/AutoSubsES/bridge/outbox/), all written by
-- resolve_bridge.py:
--   push_payload.lua    `return { ... }` module with mode, track/template
--                       names, file paths, style, and cues (see
--                       exporters.export_resolve_lua_payload).
--   push_payload.table  Same table without `return`, for bmd.readfile.
--   autosubs_es.srt / autosubs_es_resolve_tc.srt / autosubs_es.fcpxml
--                       Referenced by path from the payload.
-- Files written: none.
-- HTTP endpoints called: none.
--
-- All user-facing messages go to Workspace > Console with an "[AutoSubs ES]"
-- prefix and stay in Spanish.

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
-- Tries three strategies in order, because each is disabled on some
-- Resolve/Fusion builds:
--   1. loadfile(path) and run the `return { ... }` chunk.
--   2. io.open + load/loadstring on the file contents.
--   3. bmd.readfile on the ".table" companion (then on `path` itself).
--      bmd.readfile only parses a bare `{ ... }` table literal and returns
--      nil for a module that starts with `return`, which is why
--      resolve_bridge.py writes both .lua and .table versions.
-- Running a chunk is pcall-protected and must yield a table, so a corrupt or
-- half-written payload falls through to the next strategy instead of
-- aborting the script.
-- @param path string Absolute path to the ".lua" module.
-- @return table|nil The loaded table, or nil on failure.
-- @return string|nil Error message when every strategy fails.
local function loadLuaModule(path)
  local lastErr = nil

  --- Runs a compiled chunk and accepts its result only if it is a table.
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
local payloadPath = homeDir .. "/Library/Application Support/AutoSubsES/bridge/outbox/push_payload.lua"

local payload, loadErr = loadLuaModule(payloadPath)
if not payload then
  print("[AutoSubs ES] No se encontró push_payload.lua: " .. tostring(loadErr))
  print("[AutoSubs ES] Pulsa 'Enviar a DaVinci Resolve 20' en MTTN AutoSubs Tool primero.")
  return
end

if not resolve then
  print("[AutoSubs ES] Error: No se pudo conectar con DaVinci Resolve 20.")
  return
end

local project = resolve:GetProjectManager():GetCurrentProject()
local mediaPool = project and project:GetMediaPool()
local timeline = project and project:GetCurrentTimeline()

if not project or not mediaPool or not timeline then
  print("[AutoSubs ES] Error: Abre un proyecto y un timeline activo en DaVinci Resolve 20.")
  return
end

local mode = payload.mode or "subtitle_srt"
local trackName = payload.track_name or "Subtítulos ES"
local cues = payload.cues or {}

if #cues == 0 then
  print("[AutoSubs ES] No hay subtítulos en el paquete.")
  return
end

--- Finds the Text+ template clip to use for every cue.
-- Walks the whole Media Pool tree depth-first (a folder's clips, then its
-- subfolders) in a single pass and ranks candidates so that a better match
-- anywhere in the tree beats a weaker one found earlier:
--   1. Exact name match with `targetName` (returned immediately).
--   2. First case-insensitive substring match of `targetName`.
--   3. First clip whose name contains "autosubs" or "text+".
-- @param folder Folder|nil Media Pool root folder to search; nil returns nil.
-- @param targetName string Preferred template name ("" accepts any match).
-- @return MediaPoolItem|nil Matching template clip, or nil if none exists.
local function findTemplateClip(folder, targetName)
  targetName = targetName or ""
  local lowerTarget = string.lower(targetName)
  local substringMatch = nil
  local fallback = nil

  --- Recursively scans one folder; returns an exact match or nil.
  -- @param f Folder|nil Folder to scan.
  -- @return MediaPoolItem|nil Exact-name match, if any.
  local function visit(f)
    if not f then return nil end
    local clips = f:GetClipList() or {}
    for _, clip in ipairs(clips) do
      local name = clip:GetClipProperty("Clip Name") or clip:GetName() or ""
      if targetName ~= "" and name == targetName then
        return clip
      end
      local lname = string.lower(name)
      if not substringMatch and lowerTarget ~= "" and string.find(lname, lowerTarget, 1, true) then
        substringMatch = clip
      end
      if not fallback and (string.find(lname, "autosubs", 1, true) or string.find(lname, "text+", 1, true)) then
        fallback = clip
      end
    end
    local subs = f:GetSubFolderList() or {}
    for _, sub in ipairs(subs) do
      local found = visit(sub)
      if found then return found end
    end
    return nil
  end

  return visit(folder) or substringMatch or fallback
end

--- Adds a new track and gives it a name.
-- Verifies that the track count actually grew so a refused AddTrack never
-- makes the script write into (or later delete) one of the user's tracks.
-- @param trackType string "video", "audio", or "subtitle".
-- @param name string Track name to set (ignored on builds without
--   SetTrackName).
-- @return number|nil Index of the new track, or nil if it was not added.
local function addNamedTrack(trackType, name)
  local before = timeline:GetTrackCount(trackType) or 0
  local okAdd = pcall(function() timeline:AddTrack(trackType) end)
  local after = timeline:GetTrackCount(trackType) or 0
  if not okAdd or after ~= before + 1 then
    return nil
  end
  -- SetTrackName is missing on some builds; the track keeps its default name.
  pcall(function()
    timeline:SetTrackName(trackType, after, name)
  end)
  return after
end

--- Deletes a track this script just added, so a failed insert leaves the
-- timeline as it was. Only removes it while it is still empty.
-- @param trackType string "video", "audio", or "subtitle".
-- @param index number Track index returned by addNamedTrack().
local function removeAddedTrack(trackType, index)
  if not index then return end
  local items = timeline:GetItemListInTrack(trackType, index) or {}
  if #items > 0 then return end
  local ok, deleted = pcall(function()
    return timeline:DeleteTrack(trackType, index)
  end)
  if not ok or not deleted then
    print(string.format("[AutoSubs ES] Aviso: no se pudo eliminar la pista vacía %s %d; bórrala manualmente.", trackType, index))
  end
end

-- Mode subtitle_srt: native subtitle (ST) track. ST tracks cannot host Fusion
-- effects, so styling is limited to Resolve's track-level caption settings.
if mode == "subtitle_srt" then
  -- srt_path holds zero-based times, so the clip is placed at the timeline
  -- start (recordFrame, absolute frames) rather than at 00:00:00:00.
  -- The track is added only after the import succeeds, and removed again if
  -- the append fails, so a failed run never leaves an empty ST track behind.
  local srtPath = payload.srt_path
  local imported = mediaPool:ImportMedia({ srtPath })
  local subCount = nil
  if imported and #imported > 0 then
    subCount = addNamedTrack("subtitle", trackName)
    if not subCount then
      print("[AutoSubs ES] Error: Resolve no permitió crear una pista de subtítulos nueva.")
    end
  end
  if subCount then
    local startFrame = timeline:GetStartFrame() or payload.timeline_start_frame or 86400
    local appended = mediaPool:AppendToTimeline({
      {
        mediaPoolItem = imported[1],
        startFrame = 0,
        recordFrame = startFrame,
        trackIndex = subCount,
      }
    })
    -- Resolve quirk: AppendToTimeline may return an empty table ({}) instead
    -- of nil when it silently refuses the clip, so test `#appended > 0` and
    -- then confirm the new ST track really has items before reporting success.
    if appended and #appended > 0 then
      local trackItems = timeline:GetItemListInTrack("subtitle", subCount) or {}
      if #trackItems > 0 then
        print(string.format("[AutoSubs ES] Pista SRT '%s' (ST%d) insertada con %d subtítulos.", trackName, subCount, #cues))
        return
      end
    end
    removeAddedTrack("subtitle", subCount)
  end
  -- Manual fallback: the *_resolve_tc.srt variant uses absolute timeline
  -- timecodes, which is what File > Import > Subtitle expects.
  print("[AutoSubs ES] Si Resolve ignoró AppendToTimeline en SRT, importa manualmente este archivo desde Archivo > Importar > Subtítulo:")
  print("  " .. tostring(payload.srt_timeline_tc_path or payload.srt_path))
  return
end

-- Mode fcpxml: import as a separate timeline of Basic Titles (also carries
-- the pre-sliced flipbook animation from exporters.export_fcpxml).
if mode == "fcpxml" then
  local ok = mediaPool:ImportTimelineFromFile(payload.fcpxml_path)
  if ok then
    print("[AutoSubs ES] Timeline FCPXML importado con éxito.")
  else
    print("[AutoSubs ES] Importa manualmente Archivo > Importar > Línea de tiempo...: " .. tostring(payload.fcpxml_path))
  end
  return
end

-- Mode textplus: GPU-light single-node Text+ (ideal for a 2 GB Radeon Polaris).
-- The template must already be in the Media Pool: AppendToTimeline only
-- accepts MediaPoolItems, not entries from the Effects > Titles library.
local templateClip = findTemplateClip(mediaPool:GetRootFolder(), payload.template_name or "")
if not templateClip then
  print("[AutoSubs ES] No hay ninguna plantilla Text+ en tu Media Pool.")
  print("[AutoSubs ES] Consejo: Arrastra 'Effects > Titles > MTTN AutoSubs > AutoSubs_ES_Cine_Netflix' (o Anitype_Animado) al Media Pool una vez (o Power Bins).")
  print("[AutoSubs ES] Respaldo automático: importando FCPXML con Basic Titles...")
  local ok = mediaPool:ImportTimelineFromFile(payload.fcpxml_path)
  if ok then
    print("[AutoSubs ES] Subtítulos importados vía FCPXML correctamente.")
  else
    print("[AutoSubs ES] Importa manualmente Archivo > Importar > Línea de tiempo...: " .. tostring(payload.fcpxml_path))
  end
  return
end

local trackIdx = addNamedTrack("video", trackName)
if not trackIdx then
  print("[AutoSubs ES] Error: Resolve no permitió crear una pista de vídeo nueva para los clips Text+.")
  return
end

local tlStart = timeline:GetStartFrame() or payload.timeline_start_frame or 86400
-- One clipInfo per cue. Frames are timeline frames: recordFrame is absolute
-- (timeline start + cue offset); startFrame/endFrame trim the template and
-- endFrame is inclusive, hence duration_frames - 1.
-- cuesByFrame maps each expected record frame to the cue indices placed
-- there, so appended items can be paired with their cue by position.
local clipInfos = {}
local cuesByFrame = {}
for i, cue in ipairs(cues) do
  local dur = math.max(1, (cue.duration_frames or 24) - 1)
  local recordFrame = math.floor(tlStart + (cue.start_frame_offset or 0) + 0.5)
  table.insert(clipInfos, {
    mediaPoolItem = templateClip,
    startFrame = 0,
    endFrame = dur,
    recordFrame = recordFrame,
    trackIndex = trackIdx,
  })
  local bucket = cuesByFrame[recordFrame]
  if not bucket then
    bucket = {}
    cuesByFrame[recordFrame] = bucket
  end
  table.insert(bucket, i)
end

local appended = mediaPool:AppendToTimeline(clipInfos)
-- Same quirk as above: a refused append can come back as {} rather than nil.
if not appended or #appended == 0 then
  removeAddedTrack("video", trackIdx)
  print("[AutoSubs ES] Error: AppendToTimeline no pudo añadir los clips Text+.")
  return
end

-- Max distance, in timeline frames, between an item's start and its cue's
-- recordFrame. Covers rounding by Resolve; real cues on one track never start
-- this close to each other, and the exact frame is always tried first.
local MATCH_TOLERANCE_FRAMES = 1
local cueClaimed = {}

--- Finds the cue an appended Text+ item belongs to, by timeline position.
-- Resolve can skip clips it refuses (e.g. overlaps) or return items in a
-- different order, so pairing appended[i] with cues[i] could put one cue's
-- text on another cue's clip. Each cue can be claimed only once.
-- @param item TimelineItem Appended item.
-- @return number|nil Index into `cues`, or nil when no cue starts within
--   MATCH_TOLERANCE_FRAMES of the item's start frame.
local function matchCueForItem(item)
  if not item or not item.GetStart then return nil end
  local ok, start = pcall(function() return item:GetStart() end)
  start = ok and tonumber(start) or nil
  if not start then return nil end
  start = math.floor(start + 0.5)
  for delta = 0, MATCH_TOLERANCE_FRAMES do
    local frames = (delta == 0) and { start } or { start - delta, start + delta }
    for _, frame in ipairs(frames) do
      local bucket = cuesByFrame[frame]
      if bucket then
        for _, idx in ipairs(bucket) do
          if not cueClaimed[idx] then
            cueClaimed[idx] = true
            return idx
          end
        end
      end
    end
  end
  return nil
end

-- Flipbook animation settings. Older payloads may lack font_variants, so a
-- single variant is synthesized from font/font_style.
local st = payload.style or {}
local variants = st.font_variants or {}
if #variants == 0 and st.font then
  variants = { { font = st.font, font_style = st.font_style or "Regular" } }
end
local animMode = st.animation_mode or "none"
local cycleActive = (animMode == "font_cycle" or animMode == "font_cycle_boil") and (#variants > 1)
local holdFrames = math.max(1, math.floor(st.anim_hold_frames or 3))
local boilJitter = 0
if animMode == "boil" or animMode == "font_cycle_boil" then
  boilJitter = tonumber(st.anim_jitter) or 0.15
end

local updated = 0
local unmatchedItems = 0
-- Items are paired with cues by timeline start frame (matchCueForItem), not
-- by index; an item without a matching cue is left unstyled rather than
-- risking another cue's text. Each Text+ title clip owns one Fusion comp.
for _, item in ipairs(appended) do
  local cueIdx = matchCueForItem(item)
  local cue = cueIdx and cues[cueIdx] or nil
  if not cue then
    unmatchedItems = unmatchedItems + 1
  end
  if cue and item and item.GetFusionCompCount and item:GetFusionCompCount() > 0 then
    local comp = item:GetFusionCompByIndex(1)
    if comp then
      local tp = comp:FindTool("TextPlus1")
      if not tp then
        -- Custom templates may rename the node. GetToolList returns a Lua
        -- table that can be 0- or 1-indexed depending on the build.
        local tools = comp:GetToolList(false, "TextPlus")
        tp = tools and (tools[1] or tools[0])
      end
      if tp then
        local primaryVariant = variants[1]
        tp:SetInput("StyledText", cue.text or "")
        if primaryVariant and primaryVariant.font then
          tp:SetInput("Font", primaryVariant.font)
        elseif st.font then
          tp:SetInput("Font", st.font)
        end
        if primaryVariant and primaryVariant.font_style then
          tp:SetInput("Style", primaryVariant.font_style)
        elseif st.font_style then
          tp:SetInput("Style", st.font_style)
        end
        -- Center is in normalized frame space (0..1, origin bottom-left), so
        -- pos_y is the fraction of frame height above the bottom edge.
        if st.size then tp:SetInput("Size", st.size) end
        if st.pos_y then tp:SetInput("Center", { 0.5, st.pos_y }) end
        -- Text+ shading elements: 1 = fill, 2 = outline, 3 = shadow,
        -- 4 = background box. Colors are normalized 0..1 floats.
        if st.fill_r then
          tp:SetInput("Red1", st.fill_r)
          tp:SetInput("Green1", st.fill_g)
          tp:SetInput("Blue1", st.fill_b)
          tp:SetInput("Alpha1", st.fill_a or 1.0)
        end
        if st.outline_thickness ~= nil then
          tp:SetInput("Enabled2", st.outline_thickness > 0 and 1 or 0)
          tp:SetInput("Thickness2", st.outline_thickness)
          tp:SetInput("Red2", st.outline_r or 0)
          tp:SetInput("Green2", st.outline_g or 0)
          tp:SetInput("Blue2", st.outline_b or 0)
          tp:SetInput("Alpha2", st.outline_a or 1.0)
        end
        tp:SetInput("Enabled3", st.shadow_enabled and 1 or 0)
        -- Shadow transparency is driven by Opacity3, not the shadow color's
        -- alpha channel (kept in sync with resolve_bridge.py; see tests).
        if st.shadow_a ~= nil then tp:SetInput("Opacity3", st.shadow_a) end
        tp:SetInput("Enabled4", st.box_enabled and 1 or 0)
        if st.box_enabled then
          tp:SetInput("Red4", st.box_r or 0)
          tp:SetInput("Green4", st.box_g or 0)
          tp:SetInput("Blue4", st.box_b or 0)
          tp:SetInput("Alpha4", st.box_alpha or 0.72)
        end

        -- Configure the extra nodes when the multi-font animated template is
        -- used (TextPlus2..TextPlus5). Without font cycling every node gets
        -- variant 1; with it, node k shows variant ((k - 1) % n) + 1.
        for k = 2, 5 do
          local tpK = comp:FindTool("TextPlus" .. tostring(k))
          if tpK and #variants > 0 then
            local vIdx = cycleActive and (((k - 1) % #variants) + 1) or 1
            local v = variants[vIdx]
            if v and v.font then tpK:SetInput("Font", v.font) end
            if v and v.font_style then tpK:SetInput("Style", v.font_style) end
          end
        end

        -- Published controls of the AutoSubs_ES_Anitype_Animado macro, read by
        -- its VariantSel/BoilTransform expressions: HoldFrames = frames per
        -- variant, VariantCount = 1..5 faces to cycle, BoilJitter = wobble
        -- amount (0 disables boil).
        local macroOp = comp:FindTool("AutoSubs_ES_Anitype_Animado")
        if macroOp then
          local effectiveCount = cycleActive and math.min(5, #variants) or 1
          macroOp:SetInput("HoldFrames", holdFrames)
          macroOp:SetInput("VariantCount", effectiveCount)
          macroOp:SetInput("BoilJitter", boilJitter)
        end

        updated = updated + 1
      end
    end
  end
end

local unmatchedCues = 0
for i = 1, #cues do
  if not cueClaimed[i] then unmatchedCues = unmatchedCues + 1 end
end
if unmatchedItems > 0 then
  print(string.format("[AutoSubs ES] Aviso: %d clips Text+ no coinciden con ningún subtítulo por posición y se dejaron sin texto; revísalos en la pista V%d.", unmatchedItems, trackIdx))
end
if unmatchedCues > 0 then
  print(string.format("[AutoSubs ES] Aviso: %d subtítulos no se insertaron (Resolve no colocó su clip en el frame esperado).", unmatchedCues))
end

print(string.format("[AutoSubs ES] ¡Listo! %d clips Text+ insertados en pista V%d (%s).", updated, trackIdx, trackName))
