(() => {
  "use strict";

  const state = {
    env: null,
    bridge: null,
    presets: {},
    domains: {},
    cues: [],
    peaks: [],
    durationMs: 0,
    activeCueIndex: -1,
    activeJobId: null,
    jobTimer: null,
    saveTimer: null,
    aspect: "16:9",
    appMode: "resolve",
    userChoseMode: false,
    isVideo: false,
    canBurninVideo: false,
    uploadSource: "resolve",
    mediaRev: null,
    threadsInitialized: false,
    fontCatalog: null,
    packByOptionValue: {},
    selectedPack: null,
    animClockMs: 0,
    animTimer: null,
  };

  function activeMedia() {
    return state.isVideo ? $("video-player") : $("audio-player");
  }

  const ASCII_FOLD_MAP = {
    á: "a",
    é: "e",
    í: "i",
    ó: "o",
    ú: "u",
    ü: "u",
    ñ: "n",
    Á: "A",
    É: "E",
    Í: "I",
    Ó: "O",
    Ú: "U",
    Ü: "U",
    Ñ: "N",
    "¿": "",
    "¡": "",
    "«": '"',
    "»": '"',
    "“": '"',
    "”": '"',
    "‘": "'",
    "’": "'",
    "—": "-",
    "–": "-",
    "…": "...",
  };

  function foldSpanishToAsciiPreview(text) {
    return String(text || "")
      .normalize("NFC")
      .replace(/[áéíóúüñÁÉÍÓÚÜÑ¿¡«»“”‘’—–…]/g, (ch) => ASCII_FOLD_MAP[ch] ?? ch)
      .normalize("NFKD")
      .replace(/[\u0300-\u036f]/g, "");
  }

  const STYLE_PRESETS = {
    netflix: {
      preset_name: "Cine Netflix",
      font: "Open Sans",
      font_style: "Semibold",
      size: 0.056,
      pos_y: 0.14,
      fill_hex: "#ffffff",
      highlight_hex: "#ffd43b",
      outline_hex: "#0b0d12",
      outline_thickness: 0.09,
      shadow_enabled: true,
      box_enabled: false,
      box_hex: "#0b0d12",
      animation_mode: "none",
      anim_hold_frames: 3,
      anim_jitter: 0.15,
      ascii_fold: false,
      template_name: "AutoSubs_ES_Cine_Netflix",
    },
    amarillo: {
      preset_name: "Amarillo Clásico",
      font: "Helvetica Neue",
      font_style: "Bold",
      size: 0.058,
      pos_y: 0.14,
      fill_hex: "#ffdc38",
      highlight_hex: "#ffffff",
      outline_hex: "#08080a",
      outline_thickness: 0.11,
      shadow_enabled: true,
      box_enabled: false,
      box_hex: "#0b0d12",
      animation_mode: "none",
      anim_hold_frames: 3,
      anim_jitter: 0.15,
      ascii_fold: false,
      template_name: "AutoSubs_ES_Amarillo_Clasico",
    },
    reels: {
      preset_name: "Reels Impacto",
      font: "Montserrat",
      font_style: "ExtraBold",
      size: 0.075,
      pos_y: 0.28,
      fill_hex: "#ffffff",
      highlight_hex: "#b197fc",
      outline_hex: "#08080a",
      outline_thickness: 0.16,
      shadow_enabled: true,
      box_enabled: false,
      box_hex: "#13101b",
      animation_mode: "none",
      anim_hold_frames: 3,
      anim_jitter: 0.15,
      ascii_fold: false,
      template_name: "AutoSubs_ES_Reels_Impacto",
    },
    caja: {
      preset_name: "Caja Editorial",
      font: "Montserrat",
      font_style: "Bold",
      size: 0.052,
      pos_y: 0.14,
      fill_hex: "#fafafa",
      highlight_hex: "#b197fc",
      outline_hex: "#0b0d12",
      outline_thickness: 0.0,
      shadow_enabled: false,
      box_enabled: true,
      box_hex: "#15121e",
      animation_mode: "none",
      anim_hold_frames: 3,
      anim_jitter: 0.15,
      ascii_fold: false,
      template_name: "AutoSubs_ES_Caja_Editorial",
    },
    anitype: {
      preset_name: "Anitype Animado",
      font: "Anitype Journal 1",
      font_style: "Regular",
      size: 0.066,
      pos_y: 0.16,
      fill_hex: "#ffffff",
      highlight_hex: "#ffd43b",
      outline_hex: "#0a0c10",
      outline_thickness: 0.09,
      shadow_enabled: true,
      box_enabled: false,
      box_hex: "#0d1017",
      animation_mode: "font_cycle_boil",
      anim_hold_frames: 3,
      anim_jitter: 0.15,
      ascii_fold: true,
      template_name: "AutoSubs_ES_Anitype_Animado",
    },
  };

  const $ = (id) => document.getElementById(id);

  function showToast(msg, ms = 3800) {
    const el = $("toast");
    el.textContent = msg;
    el.classList.remove("hidden");
    clearTimeout(el._hideTimer);
    el._hideTimer = setTimeout(() => el.classList.add("hidden"), ms);
  }

  function formatMsReadable(ms) {
    const safe = Math.max(0, Math.floor(ms || 0));
    const min = Math.floor(safe / 60000);
    const sec = Math.floor((safe % 60000) / 1000);
    const cs = Math.floor((safe % 1000) / 10);
    return `${String(min).padStart(2, "0")}:${String(sec).padStart(2, "0")}.${String(cs).padStart(2, "0")}`;
  }

  async function apiJson(url, opts = {}) {
    const resp = await fetch(url, {
      headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
      ...opts,
    });
    const data = await resp.json();
    if (!resp.ok) {
      throw new Error(data.error || `Error HTTP ${resp.status}`);
    }
    return data;
  }

  function readFormatConfigFromUI() {
    return {
      preset_id: $("preset-select").value,
      dialect: $("dialect-select").value,
      domain: $("domain-select").value,
      max_cpl: Number($("cfg-cpl").value || 42),
      max_lines: Number($("cfg-lines").value || 2),
      pause_split_ms: Number($("cfg-pause").value || 650),
      fix_inverted_punctuation: $("cfg-inverted").checked,
      remove_muletillas: $("cfg-muletillas").checked,
      uppercase: $("cfg-uppercase").checked,
      custom_glossary: $("cfg-glossary").value,
    };
  }

  function setAppMode(mode, userInitiated = false) {
    state.appMode = mode === "standalone" ? "standalone" : "resolve";
    if (userInitiated) state.userChoseMode = true;

    $("btn-mode-standalone").classList.toggle("active", state.appMode === "standalone");
    $("btn-mode-resolve").classList.toggle("active", state.appMode === "resolve");

    const targetTab = state.appMode === "standalone" ? "file" : "resolve";
    state.uploadSource = targetTab === "resolve" ? "inbox" : "uploaded";
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === targetTab));
    $("tab-resolve").classList.toggle("active", targetTab === "resolve");
    $("tab-file").classList.toggle("active", targetTab === "file");

    const resolveCard = $("card-resolve-push");
    if (resolveCard) resolveCard.classList.toggle("card-muted", state.appMode === "standalone");
    const exportCard = $("card-standalone-export");
    if (exportCard) exportCard.classList.toggle("card-highlight", state.appMode === "standalone");
    const rTitle = $("resolve-push-title");
    if (rTitle) {
      rTitle.textContent =
        state.appMode === "standalone" ? "5. DaVinci Resolve 20 (Opcional)" : "5. DaVinci Resolve 20";
    }
    const eTitle = $("export-card-title");
    if (eTitle) {
      eTitle.textContent =
        state.appMode === "standalone" ? "6. Exportar (Modo Independiente)" : "6. Exportar archivos";
    }
    renderCueList();
  }

  function readStyleFromUI() {
    const optVal = $("st-font").value;
    const pack = state.packByOptionValue[optVal] || state.selectedPack;
    const primaryFont =
      pack && pack.variants && pack.variants.length > 0
        ? pack.variants[0].font
        : optVal || "Open Sans";
    const fontVariants =
      pack && pack.is_animated_pack && pack.variants && pack.variants.length > 0
        ? pack.variants.slice(0, 5).map((v) => ({
            font: v.font,
            font_style: v.font_style || "Regular",
          }))
        : [{ font: primaryFont, font_style: $("st-weight").value || "Regular" }];

    return {
      font: primaryFont,
      font_style: $("st-weight").value || "Regular",
      size: Number($("st-size").value),
      pos_y: Number($("st-posy").value),
      fill_hex: $("st-fill").value,
      highlight_hex: $("st-highlight").value,
      outline_hex: $("st-outline").value,
      outline_thickness: Number($("st-outline-w").value),
      shadow_enabled: $("st-shadow").checked,
      box_enabled: $("st-box").checked,
      box_hex: $("st-box-color").value,
      box_alpha: 0.78,
      uppercase: $("cfg-uppercase").checked,
      ascii_fold: $("st-ascii-fold").checked,
      animation_mode: $("st-anim-mode").value || "none",
      anim_hold_frames: Number($("st-anim-hold").value || 3),
      anim_jitter: Number($("st-anim-jitter").value || 0.15),
      font_variants: fontVariants,
    };
  }

  function injectFontFaces(customFaces) {
    const styleEl = $("dynamic-fonts-style");
    if (!styleEl) return;
    styleEl.textContent = (customFaces || [])
      .map((f) => {
        const fam = String(f.css_family || "").replace(/"/g, "");
        return `@font-face { font-family: "${fam}"; src: url("${f.url}"); font-display: swap; }`;
      })
      .join("\n");
  }

  function populateWeightSelect(pack, preferredStyle) {
    const weightSel = $("st-weight");
    const prev = preferredStyle || weightSel.value || "Regular";
    const styles = [];
    if (pack && Array.isArray(pack.variants)) {
      pack.variants.forEach((v) => {
        const s = v.font_style || "Regular";
        if (!styles.includes(s)) styles.push(s);
      });
    }
    if (!styles.length) styles.push("Regular", "Bold");

    weightSel.innerHTML = "";
    styles.forEach((s) => {
      const opt = document.createElement("option");
      opt.value = s;
      opt.textContent = s;
      weightSel.appendChild(opt);
    });
    if (styles.includes(prev)) {
      weightSel.value = prev;
    } else {
      weightSel.value = styles[0];
    }
  }

  function syncAnimControlsVisibility() {
    const mode = $("st-anim-mode").value || "none";
    const active = mode !== "none";
    $("anim-params-row").classList.toggle("hidden", !active);
    $("st-anim-hold-val").textContent = String($("st-anim-hold").value || 3);
    $("st-anim-jitter-val").textContent = Number($("st-anim-jitter").value || 0.15).toFixed(2);

    const hintEl = $("anim-hint");
    const pack = state.selectedPack;
    if (active && (mode === "font_cycle" || mode === "font_cycle_boil")) {
      if (pack && pack.is_animated_pack) {
        hintEl.textContent =
          `Usando ${pack.variant_count} variantes en flipbook. Resolve usará TextPlus1..5 y 1 sola rama activa por frame.`;
        hintEl.classList.remove("hidden");
      } else {
        hintEl.textContent =
          "Sube un pack multi-fuente (p. ej. AnitypeJournal-1..5.otf o su .zip) arriba para animar variantes de glifos.";
        hintEl.classList.remove("hidden");
      }
    } else if (active && mode === "boil") {
      hintEl.textContent =
        "Boil escalonado en el nodo BoilTransform de AutoSubs_ES_Anitype_Animado.";
      hintEl.classList.remove("hidden");
    } else {
      hintEl.classList.add("hidden");
    }

    if (active) {
      if (!state.animTimer) {
        state.animTimer = setInterval(() => {
          state.animClockMs += 80;
          const mediaEl = activeMedia();
          renderPreviewAtMs((mediaEl.currentTime || 0) * 1000);
        }, 80);
      }
    } else if (state.animTimer) {
      clearInterval(state.animTimer);
      state.animTimer = null;
    }
  }

  function syncFontSelection(autoConfigure = false, preferredStyle = "") {
    const selVal = $("st-font").value;
    const pack = state.packByOptionValue[selVal] || null;
    state.selectedPack = pack;

    populateWeightSelect(pack, preferredStyle);

    const warnEl = $("font-glyph-warning");
    if (pack && pack.supports_spanish === false && (pack.missing_spanish_chars || []).length > 0) {
      const missingStr = pack.missing_spanish_chars.join(" ");
      warnEl.textContent =
        `Aviso en "${pack.family}": faltan glifos en español (${missingStr}). Activa «Adaptar glifos ASCII» para evitar huecos en Resolve.`;
      warnEl.classList.remove("hidden");
      if (autoConfigure) {
        $("st-ascii-fold").checked = true;
      }
    } else {
      warnEl.classList.add("hidden");
      if (autoConfigure) {
        $("st-ascii-fold").checked = false;
      }
    }

    if (autoConfigure) {
      if (pack && pack.is_animated_pack) {
        $("st-anim-mode").value = "font_cycle_boil";
        $("push-mode").value = "textplus";
        $("template-field").classList.remove("hidden");
        $("push-template").value = "AutoSubs_ES_Anitype_Animado";
      } else {
        if ($("st-anim-mode").value === "font_cycle_boil" || $("st-anim-mode").value === "font_cycle") {
          $("st-anim-mode").value = "none";
        }
      }
    }

    syncAnimControlsVisibility();
    renderPreviewAtMs((activeMedia().currentTime || 0) * 1000);
  }

  function updateFontCatalog(fontsData, preferredPackId = "") {
    if (!fontsData) return;
    state.fontCatalog = fontsData;
    injectFontFaces(fontsData.custom_faces || []);

    const sel = $("st-font");
    const prevVal = preferredPackId || sel.value || "Open Sans";
    sel.innerHTML = "";
    state.packByOptionValue = {};

    const packs = fontsData.packs || [];
    const animPacks = packs.filter((p) => p.is_animated_pack);
    const singlePacks = packs.filter((p) => !p.is_animated_pack);

    if (animPacks.length > 0) {
      const grp = document.createElement("optgroup");
      grp.label = "Packs animados (Flipbook)";
      animPacks.forEach((p) => {
        const opt = document.createElement("option");
        opt.value = p.pack_id;
        opt.textContent = p.label;
        grp.appendChild(opt);
        state.packByOptionValue[p.pack_id] = p;
      });
      sel.appendChild(grp);
    }

    if (singlePacks.length > 0) {
      const grp = document.createElement("optgroup");
      grp.label = "Fuentes personalizadas y macOS";
      singlePacks.forEach((p) => {
        const opt = document.createElement("option");
        opt.value = p.pack_id;
        opt.textContent = p.label;
        grp.appendChild(opt);
        state.packByOptionValue[p.pack_id] = p;
      });
      sel.appendChild(grp);
    }

    const builtinGrp = document.createElement("optgroup");
    builtinGrp.label = "Fuentes integradas";
    (fontsData.builtin_fonts || []).forEach((b) => {
      const opt = document.createElement("option");
      opt.value = b.font;
      opt.textContent = b.font;
      builtinGrp.appendChild(opt);
      state.packByOptionValue[b.font] = {
        pack_id: b.font,
        label: b.font,
        family: b.font,
        is_animated_pack: false,
        variant_count: 1,
        supports_spanish: true,
        missing_spanish_chars: [],
        source: "builtin",
        variants: (b.styles || ["Regular", "Bold"]).map((s) => ({
          font: b.font,
          font_style: s,
        })),
      };
    });
    sel.appendChild(builtinGrp);

    if (state.packByOptionValue[prevVal]) {
      sel.value = prevVal;
    } else if (preferredPackId) {
      const match = packs.find(
        (p) => p.pack_id === preferredPackId || p.family.toLowerCase() === preferredPackId.toLowerCase()
      );
      if (match) sel.value = match.pack_id;
    }
    syncFontSelection(Boolean(preferredPackId));
  }

  function applyStylePreset(key) {
    const p = STYLE_PRESETS[key];
    if (!p) return;

    let selectedSupportsSpanish = true;
    if (key === "anitype") {
      const packs = (state.fontCatalog && state.fontCatalog.packs) || [];
      const animPack =
        packs.find((pk) => pk.is_animated_pack && pk.family.toLowerCase().includes("anitype")) ||
        packs.find((pk) => pk.is_animated_pack) ||
        null;
      if (animPack && state.packByOptionValue[animPack.pack_id]) {
        $("st-font").value = animPack.pack_id;
        selectedSupportsSpanish = animPack.supports_spanish !== false;
      }
      $("push-mode").value = "textplus";
      $("template-field").classList.remove("hidden");
    } else if (state.packByOptionValue[p.font]) {
      $("st-font").value = p.font;
    }

    $("st-size").value = p.size;
    $("st-posy").value = p.pos_y;
    $("st-fill").value = p.fill_hex;
    $("st-highlight").value = p.highlight_hex;
    $("st-outline").value = p.outline_hex;
    $("st-outline-w").value = p.outline_thickness;
    $("st-shadow").checked = p.shadow_enabled;
    $("st-box").checked = p.box_enabled;
    $("st-box-color").value = p.box_hex;
    $("st-anim-mode").value = p.animation_mode || "none";
    $("st-anim-hold").value = p.anim_hold_frames || 3;
    $("st-anim-jitter").value = p.anim_jitter ?? 0.15;
    $("st-ascii-fold").checked = key === "anitype" ? !selectedSupportsSpanish : Boolean(p.ascii_fold);
    $("push-template").value = p.template_name;

    syncFontSelection(false, p.font_style);

    document.querySelectorAll(".chip").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.style === key);
    });
    renderPreviewAtMs((activeMedia().currentTime || 0) * 1000);
  }

  function updateModelSelect(env) {
    const sel = $("model-select");
    const prev = sel.value || env.default_model_id;
    sel.innerHTML = "";
    const order = ["turbo-q8", "large-v3-q5", "turbo-q5", "small-q8", "base-q8"];
    order.forEach((id) => {
      const m = env.models[id];
      if (!m) return;
      const opt = document.createElement("option");
      opt.value = id;
      opt.textContent = `${m.installed ? "✓ " : "↓ "}${m.label} (${m.size_mb} MB)`;
      sel.appendChild(opt);
    });
    if (prev && env.models[prev]) sel.value = prev;
    updateModelHint();
  }

  function updateModelHint() {
    if (!state.env) return;
    const m = state.env.models[$("model-select").value];
    if (!m) return;
    $("model-note").textContent = `${m.spanish_wer} · ${m.ram_mb} MB RAM · ${m.speed_note}`;
    $("btn-download-model").textContent = m.installed ? "Instalado" : `Descargar (${m.size_mb} MB)`;
    $("btn-download-model").disabled = m.installed;
  }

  function applySession(session) {
    state.cues = session.cues || [];
    state.peaks = session.peaks || [];
    state.durationMs = session.duration_ms || 0;
    state.isVideo = Boolean(session.is_video);
    state.canBurninVideo = Boolean(session.can_burnin_video);
    $("media-name-badge").textContent = session.media_name || "Sin audio";

    const audio = $("audio-player");
    const video = $("video-player");
    video.classList.toggle("hidden", !state.isVideo);
    if ($("btn-burn-video")) {
      $("btn-burn-video").title = state.canBurninVideo
        ? "Exportar MP4 con subtítulos incrustados (CPU libx264)"
        : "Sube primero un archivo de vídeo (.mp4 / .mov / .mkv) para renderizar MP4";
    }

    if (session.media_url) {
      const targetUrl = `${session.media_url}?v=${encodeURIComponent(session.media_rev || Date.now())}`;
      const revChanged = Boolean(session.media_rev && session.media_rev !== state.mediaRev);
      state.mediaRev = session.media_rev || null;
      if (state.isVideo) {
        audio.pause();
        audio.removeAttribute("src");
        if (revChanged || !video.getAttribute("src") || session.demo_mode) {
          video.src = targetUrl;
        }
      } else {
        video.pause();
        video.removeAttribute("src");
        if (revChanged || !audio.getAttribute("src") || session.demo_mode) {
          audio.src = targetUrl;
        }
      }
    } else {
      state.mediaRev = null;
      audio.pause();
      audio.removeAttribute("src");
      video.pause();
      video.removeAttribute("src");
    }

    if (session.format_config) {
      const fc = session.format_config;
      $("preset-select").value = fc.preset_id || "youtube_16_9";
      if ($("dialect-select") && ["neutro", "es_es", "es_mx", "es_ar"].includes(fc.dialect)) {
        $("dialect-select").value = fc.dialect;
      }
      if ($("domain-select") && fc.domain) {
        $("domain-select").value = fc.domain;
      }
      $("cfg-cpl").value = fc.max_cpl || 42;
      $("cfg-lines").value = fc.max_lines || 2;
      $("cfg-pause").value = fc.pause_split_ms || 650;
      $("cfg-inverted").checked = fc.fix_inverted_punctuation !== false;
      $("cfg-muletillas").checked = Boolean(fc.remove_muletillas);
      $("cfg-uppercase").checked = Boolean(fc.uppercase);
    }

    const previewMs = state.cues.length > 0 ? state.cues[0].start_ms + 120 : 0;
    state.activeCueIndex = state.cues.length > 0 ? 0 : -1;
    renderCueList();
    drawWaveform(previewMs);
    renderPreviewAtMs(previewMs);
  }

  async function refreshStatus() {
    try {
      const data = await apiJson("/api/status");
      state.env = data.environment;
      state.bridge = data.bridge;
      state.presets = data.presets || {};
      state.domains = data.domains || {};

      updateModelSelect(data.environment);
      if (data.fonts) {
        updateFontCatalog(data.fonts);
      }

      if (!state.userChoseMode) {
        const resolveReady =
          Boolean(data.environment.resolve_running) ||
          Boolean(data.bridge && data.bridge.inbox_audio_path);
        setAppMode(resolveReady ? "resolve" : "standalone", false);
      }

      const resolveBadge = $("badge-resolve");
      resolveBadge.textContent = data.bridge.mode_label;
      resolveBadge.className = "pill " + (
        data.bridge.mode === "studio_direct"
          ? "pill-ok"
          : data.bridge.mode === "lua_bridge_ready"
          ? "pill-info"
          : "pill-warn"
      );

      const recThreads = String(data.environment.recommended_threads || 3);
      if (!state.threadsInitialized) {
        $("threads-select").value = recThreads;
        state.threadsInitialized = true;
      }
      $("badge-cpu").textContent = `CPU i5 AVX2 (${$("threads-select").value || recThreads}/4 hilos)`;

      const meta = data.bridge.timeline_meta || {};
      const metaBox = $("resolve-meta-box");
      metaBox.textContent = "";
      if (meta.timeline_name) {
        const lines = [
          ["Timeline: ", meta.timeline_name],
          [
            "Proyecto: ",
            `${meta.project_name || "-"} · ${Number(meta.fps || 24).toFixed(2)} fps (${meta.width || 1920}×${meta.height || 1080})`,
          ],
          [
            "Audio buzón: ",
            data.bridge.inbox_audio_path ? "✓ Listo para transcribir" : "Pendiente",
          ],
        ];
        lines.forEach(([lbl, val], i) => {
          if (i > 0) metaBox.appendChild(document.createElement("br"));
          const strong = document.createElement("strong");
          strong.textContent = lbl;
          metaBox.appendChild(strong);
          metaBox.appendChild(document.createTextNode(String(val)));
        });
      } else {
        metaBox.textContent =
          "Ejecuta en Resolve: Workspace > Scripts > 1 - MTTN AutoSubs - Exportar Audio.";
      }

      if (
        data.session &&
        ((data.session.media_rev && data.session.media_rev !== state.mediaRev) ||
          (state.cues.length === 0 && data.session.cue_count > 0))
      ) {
        applySession(data.session);
      }
    } catch (err) {
      showToast(`Error leyendo estado: ${err.message}`);
    }
  }

  function renderCueList() {
    const list = $("cue-list");
    const cues = state.cues;

    $("stat-cues").textContent = `${cues.length} subtítulos`;
    if (cues.length === 0) {
      $("stat-cps").textContent = "0.0 CPS medio";
      $("stat-warns").textContent = "0 avisos";
      list.innerHTML =
        '<div class="empty-state">Todavía no hay subtítulos. Exporta audio desde DaVinci Resolve 20, sube un archivo o prueba el Modo Demo ES.</div>';
      return;
    }

    const avgCps = cues.reduce((acc, c) => acc + (c.cps || 0), 0) / cues.length;
    const warnsCount = cues.filter((c) => (c.warnings || []).length > 0).length;
    $("stat-cps").textContent = `${avgCps.toFixed(1)} CPS medio`;
    $("stat-warns").textContent = `${warnsCount} avisos`;
    $("stat-warns").className = "pill " + (warnsCount > 0 ? "pill-warn" : "pill-ok");

    list.innerHTML = "";
    cues.forEach((cue, idx) => {
      const card = document.createElement("div");
      card.className = "cue-item" + (idx === state.activeCueIndex ? " active" : "");
      card.dataset.index = String(idx);

      const cpsClass =
        cue.cps > 20 ? "pill-danger" : cue.cps > 17 ? "pill-warn" : "pill-ok";

      const head = document.createElement("div");
      head.className = "cue-head";

      const times = document.createElement("div");
      times.className = "cue-times";
      times.innerHTML = `<strong>#${cue.index}</strong>`;

      const startIn = document.createElement("input");
      startIn.type = "number";
      startIn.step = "50";
      startIn.value = String(cue.start_ms);
      startIn.title = "Inicio en milisegundos";
      startIn.addEventListener("change", () => {
        cue.start_ms = Math.max(0, Number(startIn.value || 0));
        scheduleSaveCues();
      });

      const endIn = document.createElement("input");
      endIn.type = "number";
      endIn.step = "50";
      endIn.value = String(cue.end_ms);
      endIn.title = "Fin en milisegundos";
      endIn.addEventListener("change", () => {
        cue.end_ms = Math.max(cue.start_ms + 80, Number(endIn.value || cue.start_ms + 500));
        scheduleSaveCues();
      });

      const tcSpan = document.createElement("span");
      tcSpan.textContent = `${formatMsReadable(cue.start_ms)} → ${formatMsReadable(cue.end_ms)}`;

      times.appendChild(startIn);
      times.appendChild(endIn);
      times.appendChild(tcSpan);

      const actions = document.createElement("div");
      actions.className = "cue-actions";

      const cpsBadge = document.createElement("span");
      cpsBadge.className = `pill ${cpsClass}`;
      cpsBadge.textContent = `${Number(cue.cps || 0).toFixed(1)} CPS`;
      actions.appendChild(cpsBadge);

      const btnPlayCue = makeMiniBtn("▶", "Escuchar este subtítulo", () => seekAndPlay(cue.start_ms));
      const btnSplit = makeMiniBtn("÷", "Dividir en dos subtítulos", () => splitCue(idx));
      const btnMerge = makeMiniBtn("+Unir", "Unir con el siguiente", () => mergeCue(idx));
      const btnDel = makeMiniBtn("×", "Eliminar subtítulo", () => deleteCue(idx));

      if (state.appMode === "resolve") {
        const btnJump = makeMiniBtn("Resolve", "Mover cabezal en DaVinci Resolve", () => jumpResolve(cue.start_ms));
        actions.append(btnPlayCue, btnJump, btnSplit, btnMerge, btnDel);
      } else {
        actions.append(btnPlayCue, btnSplit, btnMerge, btnDel);
      }
      head.append(times, actions);

      const textarea = document.createElement("textarea");
      textarea.className = "cue-text";
      textarea.rows = Math.max(1, (cue.lines || []).length);
      textarea.value = cue.text || "";
      textarea.addEventListener("focus", () => {
        state.activeCueIndex = idx;
        renderPreviewAtMs(cue.start_ms + 10);
      });
      textarea.addEventListener("input", () => {
        cue.text = textarea.value;
        cue.lines = textarea.value.split("\n").map((s) => s.trim()).filter(Boolean);
        syncCueWordsAfterTextEdit(cue);
        renderPreviewAtMs(cue.start_ms + 10);
        scheduleSaveCues();
      });

      card.append(head, textarea);

      if (cue.warnings && cue.warnings.length > 0) {
        const warns = document.createElement("div");
        warns.className = "cue-warns";
        cue.warnings.forEach((w) => {
          const span = document.createElement("span");
          span.className = "pill pill-warn";
          span.textContent = w;
          warns.appendChild(span);
        });
        card.appendChild(warns);
      }

      list.appendChild(card);
    });
  }

  function syncCueWordsAfterTextEdit(cue) {
    const tokCount = (cue.lines || []).reduce(
      (n, ln) => n + ln.split(/\s+/).filter(Boolean).length,
      0
    );
    if (cue.words && cue.words.length !== tokCount) cue.words = [];
  }

  function makeMiniBtn(label, title, onClick) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "btn btn-ghost btn-sm";
    b.textContent = label;
    b.title = title;
    b.addEventListener("click", onClick);
    return b;
  }

  function scheduleSaveCues() {
    clearTimeout(state.saveTimer);
    state.saveTimer = setTimeout(saveCuesToServer, 350);
  }

  async function saveCuesToServer() {
    try {
      const data = await apiJson("/api/cues", {
        method: "PUT",
        body: JSON.stringify({ cues: state.cues }),
      });
      if (data.session) {
        state.cues = data.session.cues;
        const avgCps = state.cues.length
          ? state.cues.reduce((acc, c) => acc + (c.cps || 0), 0) / state.cues.length
          : 0;
        const warnsCount = state.cues.filter((c) => (c.warnings || []).length > 0).length;
        $("stat-cues").textContent = `${state.cues.length} subtítulos`;
        $("stat-cps").textContent = `${avgCps.toFixed(1)} CPS medio`;
        $("stat-warns").textContent = `${warnsCount} avisos`;
        $("stat-warns").className = "pill " + (warnsCount > 0 ? "pill-warn" : "pill-ok");
        drawWaveform((activeMedia().currentTime || 0) * 1000);
      }
    } catch (err) {
      showToast(`No se pudo guardar: ${err.message}`);
    }
  }

  async function splitCue(idx) {
    const cue = state.cues[idx];
    if (!cue) return;
    const words = (cue.text || "").trim().split(/\s+/);
    if (words.length < 2) return;
    const midWord = Math.floor(words.length / 2);
    const midMs = Math.floor((cue.start_ms + cue.end_ms) / 2);
    const first = {
      ...cue,
      end_ms: midMs,
      lines: [words.slice(0, midWord).join(" ")],
      text: words.slice(0, midWord).join(" "),
      words: [],
    };
    const second = {
      ...cue,
      start_ms: midMs,
      lines: [words.slice(midWord).join(" ")],
      text: words.slice(midWord).join(" "),
      words: [],
    };
    state.cues.splice(idx, 1, first, second);
    await saveCuesToServer();
    renderCueList();
  }

  async function mergeCue(idx) {
    if (idx >= state.cues.length - 1) return;
    const cur = state.cues[idx];
    const nxt = state.cues[idx + 1];
    cur.end_ms = nxt.end_ms;
    cur.lines = [cur.text.replace(/\n/g, " "), nxt.text.replace(/\n/g, " ")];
    cur.text = cur.lines.join("\n");
    cur.words = [...(cur.words || []), ...(nxt.words || [])];
    state.cues.splice(idx + 1, 1);
    await saveCuesToServer();
    renderCueList();
  }

  async function deleteCue(idx) {
    state.cues.splice(idx, 1);
    await saveCuesToServer();
    renderCueList();
  }

  function renderPreviewAtMs(ms) {
    const cap = $("preview-caption");
    const st = readStyleFromUI();
    const pack = state.selectedPack;
    const audio = $("audio-player");

    const active = state.cues.find((c) => ms >= c.start_ms && ms <= c.end_ms) ||
      (state.activeCueIndex >= 0 ? state.cues[state.activeCueIndex] : null);

    const holdFrames = Math.max(1, Number(st.anim_hold_frames || 3));
    const clockMs = !activeMedia().paused ? ms : state.animClockMs;
    const step = Math.floor(clockMs / (holdFrames * (1000 / 24)));

    const variants = (pack && pack.variants) || [];
    let resolvedFamily = `"${st.font}", sans-serif`;
    if (
      (st.animation_mode === "font_cycle" || st.animation_mode === "font_cycle_boil") &&
      variants.length > 0
    ) {
      const v = variants[step % variants.length];
      if (v.css_family) {
        resolvedFamily = `"${v.css_family}", "${v.font}", sans-serif`;
      }
    } else if (variants.length > 0 && variants[0].css_family) {
      resolvedFamily = `"${variants[0].css_family}", "${st.font}", sans-serif`;
    }

    cap.style.fontFamily = resolvedFamily;
    cap.style.fontWeight = st.font_style.toLowerCase().includes("extra") ? "800" : st.font_style.toLowerCase().includes("bold") ? "700" : "500";
    const stageH = $("preview-stage").clientHeight || 260;
    cap.style.fontSize = `${Math.max(12, Math.round(st.size * stageH * 1.15))}px`;
    cap.style.bottom = `${Math.round(st.pos_y * 100)}%`;
    cap.style.color = st.fill_hex;
    cap.style.background = st.box_enabled ? st.box_hex : "transparent";

    if (
      (st.animation_mode === "boil" || st.animation_mode === "font_cycle_boil") &&
      st.anim_jitter > 0
    ) {
      const j = Number(st.anim_jitter || 0);
      const dx = (Math.sin(step * 2.13) * 1.8 * j).toFixed(2);
      const dy = (Math.cos(step * 1.67) * 2.4 * j).toFixed(2);
      const rot = (Math.sin(step * 1.49) * 0.75 * j).toFixed(2);
      const sc = (1 + Math.sin(step * 2.71) * 0.012 * j).toFixed(4);
      cap.style.transform = `translate(${dx}px, ${dy}px) rotate(${rot}deg) scale(${sc})`;
    } else {
      cap.style.transform = "none";
    }

    const ol = Math.max(0, Math.round(st.outline_thickness * 18));
    if (ol > 0) {
      cap.style.textShadow =
        `${ol}px 0 0 ${st.outline_hex}, -${ol}px 0 0 ${st.outline_hex}, ` +
        `0 ${ol}px 0 ${st.outline_hex}, 0 -${ol}px 0 ${st.outline_hex}` +
        (st.shadow_enabled ? ", 0 3px 8px rgba(0,0,0,0.75)" : "");
    } else {
      cap.style.textShadow = st.shadow_enabled ? "0 3px 8px rgba(0,0,0,0.8)" : "none";
    }

    const transformToken = (txt) => {
      let out = st.ascii_fold ? foldSpanishToAsciiPreview(txt) : txt;
      if (st.uppercase) out = out.toUpperCase();
      return out;
    };

    if (!active) {
      const emptyMsg = state.cues.length
        ? ""
        : "Carga un audio o pulsa «Probar Demo ES» para comenzar.";
      cap.textContent = emptyMsg ? transformToken(emptyMsg) : "";
      return;
    }

    if (active.words && active.words.length > 0 && ms >= active.start_ms && ms <= active.end_ms) {
      cap.innerHTML = "";
      let wIdx = 0;
      (active.lines || [active.text]).forEach((line, lIdx) => {
        if (lIdx > 0) cap.appendChild(document.createElement("br"));
        line.split(/\s+/).forEach((tok, tIdx) => {
          if (tIdx > 0) cap.appendChild(document.createTextNode(" "));
          const span = document.createElement("span");
          const wObj = active.words[wIdx];
          wIdx += 1;
          span.textContent = transformToken(tok);
          if (wObj && ms >= wObj.start_ms && ms <= wObj.end_ms) {
            span.style.color = st.highlight_hex;
          }
          cap.appendChild(span);
        });
      });
    } else {
      cap.textContent = transformToken(active.text || "");
    }
  }

  function drawWaveform(playheadMs) {
    const canvas = $("waveform-canvas");
    const ctx = canvas.getContext("2d");
    const w = canvas.width;
    const h = canvas.height;
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = "#141417";
    ctx.fillRect(0, 0, w, h);

    const dur = Math.max(1, state.durationMs || (state.cues.length ? state.cues[state.cues.length - 1].end_ms : 1000));

    // Dibujar bloques de subtítulos en la parte inferior
    state.cues.forEach((cue, idx) => {
      const x1 = (cue.start_ms / dur) * w;
      const x2 = Math.max(x1 + 2, (cue.end_ms / dur) * w);
      ctx.fillStyle = idx === state.activeCueIndex ? "rgba(131,103,199,0.75)" : "rgba(131,103,199,0.22)";
      ctx.fillRect(x1, h - 16, x2 - x1, 14);
    });

    // Dibujar picos de audio
    const peaks = state.peaks;
    if (peaks && peaks.length > 0) {
      const step = w / peaks.length;
      ctx.fillStyle = "#555562";
      peaks.forEach((p, i) => {
        const amp = Math.max(2, p * (h - 22));
        const y = (h - 18 - amp) / 2;
        ctx.fillRect(i * step, y, Math.max(1, step - 0.5), amp);
      });
    }

    // Cabezal de reproducción
    const px = Math.max(0, Math.min(w - 1, (playheadMs / dur) * w));
    ctx.fillStyle = "#8367c7";
    ctx.fillRect(px, 0, 2, h);
  }

  function seekAndPlay(ms) {
    const mediaEl = activeMedia();
    mediaEl.currentTime = Math.max(0, ms / 1000);
    mediaEl.play().catch(() => {});
  }

  async function jumpResolve(ms) {
    try {
      const res = await apiJson("/api/resolve/jump", {
        method: "POST",
        body: JSON.stringify({ ms }),
      });
      if (res.jumped_directly) {
        showToast(`Resolve movido a ${res.timecode}`);
      } else {
        showToast(res.message);
      }
    } catch (err) {
      showToast(err.message);
    }
  }

  function pollJob(jobId, onSuccess) {
    state.activeJobId = jobId;
    $("job-box").classList.remove("hidden");
    $("btn-cancel-job").classList.remove("hidden");

    clearInterval(state.jobTimer);
    state.jobTimer = setInterval(async () => {
      try {
        const job = await apiJson(`/api/jobs/${jobId}`);
        $("job-stage").textContent = job.stage || "Procesando...";
        $("job-pct").textContent = `${job.progress || 0}%`;
        $("job-bar").style.width = `${job.progress || 0}%`;

        if (job.status === "done") {
          clearInterval(state.jobTimer);
          $("btn-cancel-job").classList.add("hidden");
          setTimeout(() => $("job-box").classList.add("hidden"), 2000);
          onSuccess(job);
        } else if (job.status === "error" || job.status === "cancelled") {
          clearInterval(state.jobTimer);
          $("btn-cancel-job").classList.add("hidden");
          showToast(job.error || job.stage, 5000);
        }
      } catch (err) {
        clearInterval(state.jobTimer);
        $("btn-cancel-job").classList.add("hidden");
        showToast(`Error consultando tarea: ${err.message}`);
      }
    }, 500);
  }

  async function uploadFile(file) {
    if (!file) return;
    $("upload-status").textContent = `Subiendo ${file.name}...`;
    try {
      const resp = await fetch("/api/upload", {
        method: "POST",
        headers: { "X-Filename": encodeURIComponent(file.name) },
        body: file,
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || "Fallo al subir archivo");
      state.uploadSource = "uploaded";
      $("upload-status").textContent = `✓ Cargado: ${data.filename}`;
      $("media-name-badge").textContent = data.filename;
      if (data.session) applySession(data.session);
      showToast(`Archivo ${data.filename} listo para transcribir.`);
    } catch (err) {
      $("upload-status").textContent = `Error: ${err.message}`;
      showToast(err.message);
    }
  }

  function wireEvents() {
    $("btn-mode-standalone").addEventListener("click", () => setAppMode("standalone", true));
    $("btn-mode-resolve").addEventListener("click", () => setAppMode("resolve", true));

    if ($("btn-fill-bgg-glossary")) {
      $("btn-fill-bgg-glossary").addEventListener("click", () => {
        const bggPreset = [
          "BoardGameGeek",
          "Meeple",
          "Worker Placement",
          "Deckbuilding",
          "Eurogame",
          "Kickstarter",
          "Gamefound",
          "Devir",
          "Zacatrus",
          "Maldito Games",
        ];
        const existing = ($("cfg-glossary").value || "")
          .split(/[,;\n]+/)
          .map((s) => s.trim())
          .filter(Boolean);
        const lowerSet = new Set(existing.map((s) => s.toLowerCase()));
        bggPreset.forEach((term) => {
          if (!lowerSet.has(term.toLowerCase())) {
            existing.push(term);
            lowerSet.add(term.toLowerCase());
          }
        });
        $("cfg-glossary").value = existing.join(", ");
        showToast("Glosario BGG / juegos de mesa añadido.");
      });
    }

    document.querySelectorAll(".tab-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const tab = btn.dataset.tab;
        state.uploadSource = tab === "resolve" ? "inbox" : "uploaded";
        document.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b === btn));
        $("tab-resolve").classList.toggle("active", tab === "resolve");
        $("tab-file").classList.toggle("active", tab === "file");
      });
    });

    $("model-select").addEventListener("change", updateModelHint);
    $("threads-select").addEventListener("change", () => {
      $("badge-cpu").textContent = `CPU i5 AVX2 (${$("threads-select").value}/4 hilos)`;
    });
    $("btn-refresh").addEventListener("click", refreshStatus);
    $("btn-check-inbox").addEventListener("click", async () => {
      try {
        const notifyRes = await apiJson("/api/resolve/notify-export", { method: "POST", body: "{}" });
        await refreshStatus();
        if (notifyRes && notifyRes.session) applySession(notifyRes.session);
        showToast("Buzón de DaVinci Resolve 20 actualizado.");
      } catch (err) {
        showToast(err.message);
      }
    });

    $("btn-resolve-export").addEventListener("click", async () => {
      try {
        const res = await apiJson("/api/resolve/export-audio", { method: "POST", body: "{}" });
        showToast(res.message || "Exportación de audio iniciada en Resolve Studio.", 5500);
      } catch (err) {
        showToast(err.message);
      }
    });

    $("btn-demo").addEventListener("click", async () => {
      try {
        const data = await apiJson("/api/demo", {
          method: "POST",
          body: JSON.stringify({ format_config: readFormatConfigFromUI() }),
        });
        applySession(data.session);
        showToast("Demo en español cargada. Prueba editar, cambiar a 9:16 o enviar a Resolve.");
      } catch (err) {
        showToast(err.message);
      }
    });

    $("btn-download-model").addEventListener("click", async () => {
      try {
        const modelId = $("model-select").value;
        const res = await apiJson("/api/models/download", {
          method: "POST",
          body: JSON.stringify({ model_id: modelId }),
        });
        pollJob(res.job_id, () => {
          refreshStatus();
          showToast("Modelo Whisper instalado correctamente.");
        });
      } catch (err) {
        showToast(err.message);
      }
    });

    $("btn-transcribe").addEventListener("click", async () => {
      try {
        const activeTab = document.querySelector(".tab-btn.active")?.dataset.tab || "resolve";
        const source = activeTab === "resolve" ? "inbox" : "uploaded";
        const payload = {
          source,
          model_id: $("model-select").value,
          threads: Number($("threads-select").value || 3),
          fast_beam: $("beam-mode").value === "fast",
          force_no_gpu: $("opt-no-gpu").checked,
          enhance_voice: $("opt-enhance-voice").checked,
          format_config: readFormatConfigFromUI(),
        };
        const res = await apiJson("/api/transcribe", {
          method: "POST",
          body: JSON.stringify(payload),
        });
        pollJob(res.job_id, (job) => {
          if (job.result) applySession(job.result);
          showToast(job.stage || "Transcripción finalizada.");
        });
      } catch (err) {
        showToast(err.message, 5000);
      }
    });

    $("btn-cancel-job").addEventListener("click", async () => {
      if (!state.activeJobId) return;
      await apiJson(`/api/jobs/${state.activeJobId}/cancel`, { method: "POST", body: "{}" });
    });

    $("btn-resegment").addEventListener("click", async () => {
      try {
        const data = await apiJson("/api/resegment", {
          method: "POST",
          body: JSON.stringify({ format_config: readFormatConfigFromUI() }),
        });
        applySession(data.session);
        showToast("Subtítulos reformateados con reglas RAE.");
      } catch (err) {
        showToast(err.message);
      }
    });

    $("preset-select").addEventListener("change", () => {
      const p = state.presets[$("preset-select").value];
      if (!p) return;
      $("cfg-cpl").value = p.max_cpl;
      $("cfg-lines").value = p.max_lines;
      $("cfg-pause").value = p.pause_split_ms;
      if ($("preset-select").value === "reels_9_16" || $("preset-select").value === "karaoke_impacto") {
        setAspect("9:16");
      } else {
        setAspect("16:9");
      }
    });

    // Subida de archivos y drag & drop
    const dz = $("dropzone");
    $("file-input").addEventListener("change", (ev) => uploadFile(ev.target.files[0]));
    dz.addEventListener("dragover", (ev) => {
      ev.preventDefault();
      dz.classList.add("dragover");
    });
    dz.addEventListener("dragleave", () => dz.classList.remove("dragover"));
    dz.addEventListener("drop", (ev) => {
      ev.preventDefault();
      dz.classList.remove("dragover");
      if (ev.dataTransfer.files[0]) uploadFile(ev.dataTransfer.files[0]);
    });

    // Monitor y audio/vídeo
    const audio = $("audio-player");
    const video = $("video-player");
    $("btn-play").addEventListener("click", () => {
      const m = activeMedia();
      if (m.paused) m.play().catch(() => {});
      else m.pause();
    });
    const onPlay = () => ($("btn-play").textContent = "II Pausar");
    const onPause = () => ($("btn-play").textContent = "▶ Reproducir");
    const onTimeUpdate = () => {
      const m = activeMedia();
      const ms = (m.currentTime || 0) * 1000;
      const tot = state.durationMs || (m.duration ? m.duration * 1000 : 0);
      $("time-readout").textContent = `${formatMsReadable(ms)} / ${formatMsReadable(tot)}`;
      const idx = state.cues.findIndex((c) => ms >= c.start_ms && ms <= c.end_ms);
      if (idx !== state.activeCueIndex) {
        state.activeCueIndex = idx;
        document.querySelectorAll(".cue-item").forEach((el, i) => {
          el.classList.toggle("active", i === idx);
        });
      }
      renderPreviewAtMs(ms);
      drawWaveform(ms);
    };
    [audio, video].forEach((mEl) => {
      mEl.addEventListener("play", onPlay);
      mEl.addEventListener("pause", onPause);
      mEl.addEventListener("timeupdate", onTimeUpdate);
    });
    $("btn-back").addEventListener("click", () => {
      const m = activeMedia();
      m.currentTime = Math.max(0, m.currentTime - 1);
    });
    $("btn-fwd").addEventListener("click", () => {
      const m = activeMedia();
      m.currentTime = m.currentTime + 1;
    });
    $("playback-rate").addEventListener("change", () => {
      const rate = Number($("playback-rate").value);
      audio.playbackRate = rate;
      video.playbackRate = rate;
    });

    $("waveform-canvas").addEventListener("click", (ev) => {
      const rect = ev.target.getBoundingClientRect();
      const frac = Math.max(0, Math.min(1, (ev.clientX - rect.left) / rect.width));
      const m = activeMedia();
      const dur = state.durationMs || (m.duration ? m.duration * 1000 : 0);
      if (dur > 0) {
        m.currentTime = (frac * dur) / 1000;
        drawWaveform(frac * dur);
        renderPreviewAtMs(frac * dur);
      }
    });

    // Aspecto 16:9 / 9:16
    function setAspect(mode) {
      state.aspect = mode;
      const stage = $("preview-stage");
      stage.classList.toggle("aspect-16-9", mode === "16:9");
      stage.classList.toggle("aspect-9-16", mode === "9:16");
      $("btn-aspect-16-9").classList.toggle("active", mode === "16:9");
      $("btn-aspect-9-16").classList.toggle("active", mode === "9:16");
      renderPreviewAtMs((activeMedia().currentTime || 0) * 1000);
    }
    $("btn-aspect-16-9").addEventListener("click", () => setAspect("16:9"));
    $("btn-aspect-9-16").addEventListener("click", () => setAspect("9:16"));

    // Buscar y reemplazar
    $("btn-replace-all").addEventListener("click", async () => {
      const find = $("search-input").value;
      const rep = $("replace-input").value;
      if (!find) return;
      state.cues.forEach((cue) => {
        cue.text = cue.text.split(find).join(rep);
        cue.lines = cue.text.split("\n").map((s) => s.trim()).filter(Boolean);
        syncCueWordsAfterTextEdit(cue);
      });
      await saveCuesToServer();
      renderCueList();
      showToast(`Reemplazado "${find}" por "${rep}".`);
    });

    // Chips, fuentes personalizadas y controles de estilo
    async function uploadFontFiles(fileList) {
      const files = Array.from(fileList || []).filter(Boolean);
      if (!files.length) return;
      const installMacos = $("opt-install-macos-font").checked ? "1" : "0";
      $("font-upload-status").textContent = `Instalando ${files.length} archivo(s) de fuente...`;
      let lastRes = null;
      let totalFaces = 0;
      try {
        for (const file of files) {
          const resp = await fetch("/api/fonts/upload", {
            method: "POST",
            headers: {
              "X-Filename": encodeURIComponent(file.name),
              "X-Install-Macos": installMacos,
            },
            body: file,
          });
          const data = await resp.json();
          if (!resp.ok) throw new Error(data.error || `Error subiendo ${file.name}`);
          lastRes = data;
          totalFaces += Number(data.installed_count || 0);
        }
        if (lastRes) {
          updateFontCatalog(lastRes, lastRes.selected_pack_id);
          const restartNote = lastRes.installed_to_macos && lastRes.resolve_running
            ? " Reinicia DaVinci Resolve 20 para que detecte la nueva tipografía en ~/Library/Fonts."
            : "";
          $("font-upload-status").textContent = `✓ ${totalFaces} variante(s) listas.${restartNote}`;
          showToast(
            `Tipografía instalada (${totalFaces}).${restartNote}`,
            lastRes.resolve_running ? 6500 : 4000
          );
        }
      } catch (err) {
        $("font-upload-status").textContent = `Error: ${err.message}`;
        showToast(err.message, 5000);
      } finally {
        $("font-input").value = "";
      }
    }

    $("font-input").addEventListener("change", (ev) => uploadFontFiles(ev.target.files));
    $("st-font").addEventListener("change", () => syncFontSelection(true));
    $("st-anim-mode").addEventListener("change", () => {
      if ($("st-anim-mode").value !== "none") {
        $("push-mode").value = "textplus";
        $("template-field").classList.remove("hidden");
        $("push-template").value = "AutoSubs_ES_Anitype_Animado";
      }
      syncAnimControlsVisibility();
      renderPreviewAtMs((activeMedia().currentTime || 0) * 1000);
    });
    ["st-anim-hold", "st-anim-jitter"].forEach((id) => {
      $(id).addEventListener("input", () => {
        syncAnimControlsVisibility();
        renderPreviewAtMs((activeMedia().currentTime || 0) * 1000);
      });
    });

    document.querySelectorAll(".chip").forEach((chip) => {
      chip.addEventListener("click", () => applyStylePreset(chip.dataset.style));
    });
    ["st-weight", "st-ascii-fold", "st-size", "st-posy", "st-fill", "st-highlight", "st-outline", "st-outline-w", "st-shadow", "st-box", "st-box-color", "cfg-uppercase"]
      .forEach((id) => $(id).addEventListener("input", () => renderPreviewAtMs((activeMedia().currentTime || 0) * 1000)));

    function syncPushModeVisibility() {
      $("template-field").classList.toggle("hidden", $("push-mode").value !== "textplus");
    }
    $("push-mode").addEventListener("change", syncPushModeVisibility);
    syncPushModeVisibility();

    // Push a DaVinci Resolve 20
    $("btn-push-resolve").addEventListener("click", async () => {
      try {
        const res = await apiJson("/api/resolve/push", {
          method: "POST",
          body: JSON.stringify({
            mode: $("push-mode").value,
            track_name: $("push-track").value || "Subtítulos ES",
            template_name: $("push-template").value,
            style: readStyleFromUI(),
          }),
        });
        const box = $("push-result");
        box.classList.remove("hidden");
        box.textContent = "";
        const title = document.createElement("strong");
        title.textContent = res.pushed_directly
          ? "✓ Insertado en Resolve Studio"
          : "✓ Listo para insertar en Resolve 20";
        const msgNode = document.createTextNode(res.message || "");
        const srtHint = document.createElement("span");
        srtHint.className = "hint";
        srtHint.textContent = `SRT: ${res.files.srt}`;
        box.append(title, document.createElement("br"), msgNode, document.createElement("br"), srtHint);
        showToast(res.message, 5000);
      } catch (err) {
        showToast(err.message);
      }
    });

    // Exportación de archivos y modo independiente
    document.querySelectorAll(".export-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        try {
          const data = await apiJson("/api/export", {
            method: "POST",
            body: JSON.stringify({
              format: btn.dataset.fmt,
              style: readStyleFromUI(),
            }),
          });
          const blob = new Blob([data.content], { type: data.mime_type });
          const a = document.createElement("a");
          a.href = URL.createObjectURL(blob);
          a.download = data.filename;
          document.body.appendChild(a);
          a.click();
          a.remove();
          URL.revokeObjectURL(a.href);
        } catch (err) {
          showToast(err.message);
        }
      });
    });

    if ($("btn-copy-txt")) {
      $("btn-copy-txt").addEventListener("click", async () => {
        try {
          const data = await apiJson("/api/export", {
            method: "POST",
            body: JSON.stringify({ format: "txt", style: readStyleFromUI() }),
          });
          if (!data.content) {
            showToast("No hay texto transcrito todavía.");
            return;
          }
          if (navigator.clipboard && navigator.clipboard.writeText) {
            await navigator.clipboard.writeText(data.content);
          }
          showToast("Guion copiado al portapapeles.");
        } catch (err) {
          showToast(err.message);
        }
      });
    }

    if ($("btn-burn-video")) {
      $("btn-burn-video").addEventListener("click", async () => {
        try {
          const res = await apiJson("/api/export-video", {
            method: "POST",
            body: JSON.stringify({
              style: readStyleFromUI(),
              threads: Number($("threads-select").value || 3),
            }),
          });
          pollJob(res.job_id, (job) => {
            if (job.result && job.result.download_url) {
              const a = document.createElement("a");
              a.href = job.result.download_url;
              a.download = job.result.filename || "subtitulado.mp4";
              document.body.appendChild(a);
              a.click();
              a.remove();
            }
            showToast(job.stage || "MP4 subtitulado listo.");
          });
        } catch (err) {
          showToast(err.message, 5000);
        }
      });
    }

    // Atajo espacio para reproducir/pausar fuera de inputs
    document.addEventListener("keydown", (ev) => {
      if (ev.code === "Space" && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) {
        ev.preventDefault();
        $("btn-play").click();
      }
    });
  }

  wireEvents();
  if (typeof CSS !== "undefined" && !CSS.supports("accent-color: #8367c7")) {
    document.querySelectorAll('input[type="range"]').forEach((slider) => {
      const updateProgress = () => {
        const min = Number(slider.min) || 0;
        const max = Number(slider.max) || 100;
        const pct = ((Number(slider.value) - min) / (max - min)) * 100;
        slider.style.setProperty("--progress", `${pct}%`);
      };
      slider.addEventListener("input", updateProgress);
      updateProgress();
    });
  }
  applyStylePreset("netflix");
  refreshStatus();
})();
