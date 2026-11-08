-- 2 - AutoSubs ES - Insertar Subtitulos.lua
-- Inserta subtítulos preparados por AutoSubs ES en DaVinci Resolve 20 (Free y Studio).

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

local homeDir = getHomeDir()
local payloadPath = homeDir .. "/Library/Application Support/AutoSubsES/bridge/outbox/push_payload.lua"

local payload, loadErr = loadLuaModule(payloadPath)
if not payload then
  print("[AutoSubs ES] No se encontró push_payload.lua: " .. tostring(loadErr))
  print("[AutoSubs ES] Pulsa 'Enviar a DaVinci Resolve 20' en la app AutoSubs ES primero.")
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

local function findTemplateClip(folder, targetName)
  if not folder then return nil end
  local fallback = nil
  local clips = folder:GetClipList() or {}
  for _, clip in ipairs(clips) do
    local name = clip:GetClipProperty("Clip Name") or clip:GetName() or ""
    local lname = string.lower(name)
    if name == targetName or (targetName ~= "" and string.find(lname, string.lower(targetName), 1, true)) then
      return clip
    end
    if not fallback and (string.find(lname, "autosubs", 1, true) or string.find(lname, "text+", 1, true)) then
      fallback = clip
    end
  end
  local subs = folder:GetSubFolderList() or {}
  for _, sub in ipairs(subs) do
    local found = findTemplateClip(sub, targetName)
    if found then return found end
  end
  return fallback
end

if mode == "subtitle_srt" then
  timeline:AddTrack("subtitle")
  local subCount = timeline:GetTrackCount("subtitle") or 1
  pcall(function()
    timeline:SetTrackName("subtitle", subCount, trackName)
  end)

  local srtPath = payload.srt_path
  local imported = mediaPool:ImportMedia({ srtPath })
  if imported and #imported > 0 then
    local startFrame = timeline:GetStartFrame() or payload.timeline_start_frame or 86400
    local appended = mediaPool:AppendToTimeline({
      {
        mediaPoolItem = imported[1],
        startFrame = 0,
        recordFrame = startFrame,
        trackIndex = subCount,
      }
    })
    if appended and #appended > 0 then
      local trackItems = timeline:GetItemListInTrack("subtitle", subCount) or {}
      if #trackItems > 0 then
        print(string.format("[AutoSubs ES] Pista SRT '%s' (ST%d) insertada con %d subtítulos.", trackName, subCount, #cues))
        return
      end
    end
  end
  print("[AutoSubs ES] Si Resolve ignoró AppendToTimeline en SRT, importa manualmente este archivo desde Archivo > Importar > Subtítulo:")
  print("  " .. tostring(payload.srt_timeline_tc_path or payload.srt_path))
  return
end

if mode == "fcpxml" then
  local ok = mediaPool:ImportTimelineFromFile(payload.fcpxml_path)
  if ok then
    print("[AutoSubs ES] Timeline FCPXML importado con éxito.")
  else
    print("[AutoSubs ES] Importa manualmente Archivo > Importar > Línea de tiempo...: " .. tostring(payload.fcpxml_path))
  end
  return
end

-- Modo textplus: GPU-light single-node Text+ (ideal para Radeon Polaris 2 GB)
local templateClip = findTemplateClip(mediaPool:GetRootFolder(), payload.template_name or "")
if not templateClip then
  print("[AutoSubs ES] No hay ninguna plantilla Text+ en tu Media Pool.")
  print("[AutoSubs ES] Consejo: Arrastra 'Effects > Titles > AutoSubs ES > AutoSubs_ES_Cine_Netflix' (o Anitype_Animado) al Media Pool una vez (o Power Bins).")
  print("[AutoSubs ES] Respaldo automático: importando FCPXML con Basic Titles...")
  local ok = mediaPool:ImportTimelineFromFile(payload.fcpxml_path)
  if ok then
    print("[AutoSubs ES] Subtítulos importados vía FCPXML correctamente.")
  else
    print("[AutoSubs ES] Importa manualmente Archivo > Importar > Línea de tiempo...: " .. tostring(payload.fcpxml_path))
  end
  return
end

timeline:AddTrack("video")
local trackIdx = timeline:GetTrackCount("video") or 1
pcall(function()
  timeline:SetTrackName("video", trackIdx, trackName)
end)

local tlStart = timeline:GetStartFrame() or payload.timeline_start_frame or 86400
local clipInfos = {}
for _, cue in ipairs(cues) do
  local dur = math.max(1, (cue.duration_frames or 24) - 1)
  table.insert(clipInfos, {
    mediaPoolItem = templateClip,
    startFrame = 0,
    endFrame = dur,
    recordFrame = tlStart + (cue.start_frame_offset or 0),
    trackIndex = trackIdx,
  })
end

local appended = mediaPool:AppendToTimeline(clipInfos)
if not appended or #appended == 0 then
  print("[AutoSubs ES] Error: AppendToTimeline no pudo añadir los clips Text+.")
  return
end

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
for i, item in ipairs(appended) do
  local cue = cues[i]
  if cue and item and item.GetFusionCompCount and item:GetFusionCompCount() > 0 then
    local comp = item:GetFusionCompByIndex(1)
    if comp then
      local tp = comp:FindTool("TextPlus1")
      if not tp then
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
        if st.size then tp:SetInput("Size", st.size) end
        if st.pos_y then tp:SetInput("Center", { 0.5, st.pos_y }) end
        if st.fill_r then
          tp:SetInput("Red1", st.fill_r)
          tp:SetInput("Green1", st.fill_g)
          tp:SetInput("Blue1", st.fill_b)
        end
        if st.outline_thickness ~= nil then
          tp:SetInput("Enabled2", st.outline_thickness > 0 and 1 or 0)
          tp:SetInput("Thickness2", st.outline_thickness)
          tp:SetInput("Red2", st.outline_r or 0)
          tp:SetInput("Green2", st.outline_g or 0)
          tp:SetInput("Blue2", st.outline_b or 0)
        end
        tp:SetInput("Enabled3", st.shadow_enabled and 1 or 0)
        tp:SetInput("Enabled4", st.box_enabled and 1 or 0)
        if st.box_enabled then
          tp:SetInput("Red4", st.box_r or 0)
          tp:SetInput("Green4", st.box_g or 0)
          tp:SetInput("Blue4", st.box_b or 0)
          tp:SetInput("Alpha4", st.box_alpha or 0.72)
        end

        -- Configurar nodos adicionales si se usa la plantilla animada multi-fuente.
        for k = 2, 5 do
          local tpK = comp:FindTool("TextPlus" .. tostring(k))
          if tpK and #variants > 0 then
            local vIdx = cycleActive and (((k - 1) % #variants) + 1) or 1
            local v = variants[vIdx]
            if v and v.font then tpK:SetInput("Font", v.font) end
            if v and v.font_style then tpK:SetInput("Style", v.font_style) end
          end
        end

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

print(string.format("[AutoSubs ES] ¡Listo! %d clips Text+ insertados en pista V%d (%s).", updated, trackIdx, trackName))
