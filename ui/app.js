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
    hwProfile: null,
    editSeq: 0,
    view: { startMs: 0, spanMs: 0 },
    drag: null,
    playUntilMs: null,
    machine: null,
    prefsTimer: null,
    playUntilRaf: 0,
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
      fill_alpha: 1,
      highlight_alpha: 1,
      outline_alpha: 1,
      shadow_alpha: 0.65,
      box_alpha: 0.75,
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
      fill_alpha: 1,
      highlight_alpha: 1,
      outline_alpha: 1,
      shadow_alpha: 0.65,
      box_alpha: 0.75,
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
      fill_alpha: 1,
      highlight_alpha: 1,
      outline_alpha: 1,
      shadow_alpha: 0.8,
      box_alpha: 0.75,
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
      fill_alpha: 1,
      highlight_alpha: 1,
      outline_alpha: 1,
      shadow_alpha: 0.65,
      box_alpha: 0.8,
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
      fill_alpha: 1,
      highlight_alpha: 1,
      outline_alpha: 1,
      shadow_alpha: 0.65,
      box_alpha: 0.75,
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
    updateStyleTargetHint();
    renderCueList();
  }

  function alphaOf(id) {
    const el = $(id);
    const v = el ? Number(el.value) : 100;
    return Math.max(0, Math.min(1, (Number.isFinite(v) ? v : 100) / 100));
  }

  function hexToRgba(hex, alpha) {
    const h = String(hex || "#000000").replace("#", "");
    const full = h.length === 3 ? h.split("").map((c) => c + c).join("") : h.padEnd(6, "0");
    const r = parseInt(full.slice(0, 2), 16) || 0;
    const g = parseInt(full.slice(2, 4), 16) || 0;
    const b = parseInt(full.slice(4, 6), 16) || 0;
    return `rgba(${r}, ${g}, ${b}, ${Number(alpha).toFixed(3)})`;
  }

  // Valores visibles junto a cada slider y color de la muestra de opacidad.
  const ALPHA_ROWS = [
    ["st-fill-a", "st-fill"],
    ["st-highlight-a", "st-highlight"],
    ["st-outline-a", "st-outline"],
    ["st-shadow-a", null],
    ["st-box-a", "st-box-color"],
  ];

  function updateStyleReadouts() {
    $("st-size-val").textContent = `${(Number($("st-size").value) * 100).toFixed(1)} %`;
    $("st-posy-val").textContent = `${Math.round(Number($("st-posy").value) * 100)} %`;
    $("st-outline-w-val").textContent = Number($("st-outline-w").value).toFixed(2);
    $("st-size").setAttribute("aria-valuetext", `${$("st-size-val").textContent} de la altura del vídeo`);
    $("st-posy").setAttribute("aria-valuetext", `${$("st-posy-val").textContent} desde abajo`);
    ALPHA_ROWS.forEach(([alphaId, colorId]) => {
      const out = $(`${alphaId}-val`);
      if (!out) return;
      const pct = Math.round(alphaOf(alphaId) * 100);
      out.querySelector(".alpha-num").textContent = `${pct} %`;
      const color = colorId ? $(colorId).value : "#000000";
      out.querySelector(".alpha-chip i").style.background = hexToRgba(color, pct / 100);
      $(alphaId).setAttribute("aria-valuetext", `${pct} por ciento`);
    });
  }

  // Desactiva controles que no tienen efecto en el estado actual.
  function syncStyleControlStates() {
    const karaoke = $("st-karaoke").checked;
    $("st-highlight").disabled = !karaoke;
    $("st-highlight-a").disabled = !karaoke;
    $("st-shadow-a").disabled = !$("st-shadow").checked;
    const box = $("st-box").checked;
    $("st-box-color").disabled = !box;
    $("st-box-a").disabled = !box;
    document.querySelectorAll(".color-row").forEach((row) => {
      const range = row.querySelector(".alpha-range");
      row.classList.toggle("is-disabled", Boolean(range && range.disabled));
    });
  }

  // Estilos guardados por el usuario (localStorage).
  const CUSTOM_PRESETS_KEY = "autosubs-custom-presets";

  function loadCustomPresets() {
    try {
      const raw = JSON.parse(localStorage.getItem(CUSTOM_PRESETS_KEY) || "{}");
      return raw && typeof raw === "object" ? raw : {};
    } catch (_) {
      return {};
    }
  }

  function saveCustomPresets(map) {
    try {
      localStorage.setItem(CUSTOM_PRESETS_KEY, JSON.stringify(map));
      return true;
    } catch (_) {
      return false;
    }
  }

  function getPreset(key) {
    if (!key) return null;
    if (key.startsWith("custom:")) return loadCustomPresets()[key.slice(7)] || null;
    return STYLE_PRESETS[key] || null;
  }

  function renderCustomChips() {
    const box = $("custom-chips");
    const map = loadCustomPresets();
    const ids = Object.keys(map);
    box.textContent = "";
    box.classList.toggle("hidden", ids.length === 0);
    ids.forEach((id) => {
      const wrap = document.createElement("span");
      wrap.className = "custom-chip-wrap";
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "chip";
      chip.dataset.style = `custom:${id}`;
      chip.textContent = map[id].preset_name || id;
      const del = document.createElement("button");
      del.type = "button";
      del.className = "chip-delete";
      del.dataset.deletePreset = id;
      del.setAttribute("aria-label", `Borrar estilo ${map[id].preset_name || id}`);
      del.textContent = "×";
      wrap.append(chip, del);
      box.appendChild(wrap);
    });
    updatePresetStatus();
  }

  function saveCurrentAsPreset(name) {
    const clean = String(name || "").trim().slice(0, 28);
    if (!clean) return;
    const st = readStyleFromUI();
    const id = clean.toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "")
      .replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || String(Date.now());
    const map = loadCustomPresets();
    map[id] = {
      preset_name: clean,
      font: $("st-font").value,
      font_style: st.font_style,
      size: st.size,
      pos_y: st.pos_y,
      fill_hex: st.fill_hex,
      highlight_hex: st.highlight_hex,
      outline_hex: st.outline_hex,
      outline_thickness: st.outline_thickness,
      shadow_enabled: st.shadow_enabled,
      box_enabled: st.box_enabled,
      box_hex: st.box_hex,
      fill_alpha: st.fill_alpha,
      highlight_alpha: st.highlight_alpha,
      outline_alpha: st.outline_alpha,
      shadow_alpha: st.shadow_alpha,
      box_alpha: st.box_alpha,
      animation_mode: st.animation_mode,
      anim_hold_frames: st.anim_hold_frames,
      anim_jitter: st.anim_jitter,
      ascii_fold: st.ascii_fold,
      template_name: $("push-template").value,
    };
    if (!saveCustomPresets(map)) {
      showToast("No se pudo guardar el estilo (almacenamiento del navegador bloqueado).");
      return;
    }
    renderCustomChips();
    applyStylePreset(`custom:${id}`);
    showToast(`Estilo «${clean}» guardado.`);
  }

  // Contraste aproximado (WCAG) entre el texto y lo que tiene detrás, ya
  // mezclado con su opacidad. Se evalúa sobre fondo blanco y negro y se toma
  // el peor caso, porque no sabemos qué habrá en el vídeo.
  function parseHex(hex) {
    const h = String(hex || "#000000").replace("#", "").padEnd(6, "0");
    return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16) || 0);
  }

  function blend(top, alpha, under) {
    return top.map((c, i) => c * alpha + under[i] * (1 - alpha));
  }

  function luminance(rgb) {
    const [r, g, b] = rgb.map((v) => {
      const c = v / 255;
      return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  }

  function contrastRatio(a, b) {
    const la = luminance(a);
    const lb = luminance(b);
    return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
  }

  function updateContrastBadge() {
    const el = $("style-contrast");
    if (!el) return;
    const st = readStyleFromUI();
    const outlineVisible = st.outline_thickness > 0 && st.outline_alpha >= 0.15;
    let msg = "";
    let level = "ok";
    if (st.fill_alpha < 0.2) {
      msg = outlineVisible
        ? "Letras huecas: la legibilidad depende del contorno."
        : "Texto casi invisible: sube la opacidad del texto o añade contorno.";
      level = outlineVisible ? "info" : "warn";
    } else if (!st.box_enabled && !outlineVisible) {
      msg = st.shadow_enabled
        ? "Sin contorno ni caja: solo la sombra separa el texto del vídeo."
        : "Sin contorno, caja ni sombra: la legibilidad dependerá del vídeo.";
      level = "warn";
    } else {
      const layerHex = st.box_enabled ? st.box_hex : st.outline_hex;
      const layerA = st.box_enabled ? st.box_alpha : st.outline_alpha;
      const ratio = Math.min(...[[255, 255, 255], [0, 0, 0]].map((backdrop) => {
        const bg = blend(parseHex(layerHex), layerA, backdrop);
        const fg = blend(parseHex(st.fill_hex), st.fill_alpha, bg);
        return contrastRatio(fg, bg);
      }));
      const ref = st.box_enabled ? "la caja" : "el contorno";
      if (ratio < 3) {
        level = "warn";
        msg = `Legibilidad baja: contraste ${ratio.toFixed(1)}:1 con ${ref}.`;
      } else if (ratio < 4.5) {
        level = "info";
        msg = `Contraste justo: ${ratio.toFixed(1)}:1 con ${ref} (recomendado ≥ 4.5:1).`;
      } else {
        msg = `Contraste ${ratio.toFixed(1)}:1 con ${ref}.`;
      }
    }
    el.textContent = msg;
    el.dataset.level = level;
  }

  // Campos que definen un preset; sirven para detectar si el usuario lo modificó.
  const PRESET_KEYS = [
    "size", "pos_y", "fill_hex", "highlight_hex", "outline_hex", "outline_thickness",
    "shadow_enabled", "box_enabled", "box_hex", "fill_alpha", "highlight_alpha",
    "outline_alpha", "shadow_alpha", "box_alpha", "font_style", "animation_mode", "ascii_fold",
  ];

  function styleSignature() {
    const st = readStyleFromUI();
    return JSON.stringify(PRESET_KEYS.map((k) => {
      const v = st[k];
      return typeof v === "number" ? Number(v.toFixed(3)) : typeof v === "string" ? v.toLowerCase() : v;
    }).concat([$("st-font").value]));
  }

  function updatePresetStatus() {
    const key = state.activePreset;
    const preset = getPreset(key);
    const modified = Boolean(preset && state.presetSignature && styleSignature() !== state.presetSignature);
    $("preset-status-text").textContent = preset
      ? `Preset: ${preset.preset_name}${modified ? " · modificado" : ""}`
      : "Estilo personalizado";
    $("btn-preset-reset").classList.toggle("hidden", !modified);
    document.querySelectorAll(".chip").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.style === key);
      btn.classList.toggle("modified", btn.dataset.style === key && modified);
      btn.setAttribute("aria-pressed", String(btn.dataset.style === key));
    });
  }


  function updateStyleTargetHint() {
    const el = $("style-target-hint");
    if (!el) return;
    const mode = state.appMode === "standalone" ? "" : $("push-mode").value;
    const msg =
      mode === "subtitle_srt"
        ? "La pista nativa de subtítulos de Resolve no admite colores ni opacidad. Estos ajustes se aplican a Text+, FCPXML, .ASS y al MP4 subtitulado."
        : mode === "fcpxml"
        ? "En FCPXML solo viaja la opacidad del texto, y Resolve podría ignorarla al importar. Para transparencias completas usa Text+, .ASS o MP4."
        : "";
    el.textContent = msg;
    el.classList.toggle("hidden", !msg);
  }

  function onStyleControlChanged() {
    syncStyleControlStates();
    updateStyleReadouts();
    updatePresetStatus();
    updateContrastBadge();
    renderPreviewAtMs((activeMedia().currentTime || 0) * 1000);
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
      box_alpha: alphaOf("st-box-a"),
      fill_alpha: alphaOf("st-fill-a"),
      highlight_alpha: alphaOf("st-highlight-a"),
      outline_alpha: alphaOf("st-outline-a"),
      shadow_alpha: alphaOf("st-shadow-a"),
      uppercase: $("cfg-uppercase").checked,
      ascii_fold: $("st-ascii-fold").checked,
      animation_mode: $("st-anim-mode").value || "none",
      anim_hold_frames: Number($("st-anim-hold").value || 3),
      anim_jitter: Number($("st-anim-jitter").value || 0.15),
      font_variants: fontVariants,
      karaoke_enabled: $("st-karaoke").checked,
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
    const p = getPreset(key);
    if (!p) return;
    const isCustom = key.startsWith("custom:");

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
    } else if (state.packByOptionValue[p.font] ||
               Array.from($("st-font").options).some((o) => o.value === p.font)) {
      $("st-font").value = p.font;
    } else if (isCustom) {
      showToast(`La fuente «${p.font}» de este estilo no está disponible; se mantiene la actual.`);
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
    $("st-fill-a").value = Math.round((p.fill_alpha ?? 1) * 100);
    $("st-highlight-a").value = Math.round((p.highlight_alpha ?? 1) * 100);
    $("st-outline-a").value = Math.round((p.outline_alpha ?? 1) * 100);
    $("st-shadow-a").value = Math.round((p.shadow_alpha ?? 0.65) * 100);
    $("st-box-a").value = Math.round((p.box_alpha ?? 0.75) * 100);
    $("st-anim-mode").value = p.animation_mode || "none";
    $("st-anim-hold").value = p.anim_hold_frames || 3;
    $("st-anim-jitter").value = p.anim_jitter ?? 0.15;
    $("st-ascii-fold").checked = key === "anitype" ? !selectedSupportsSpanish : Boolean(p.ascii_fold);
    if (p.template_name) $("push-template").value = p.template_name;

    syncFontSelection(false, p.font_style);
    if ((p.animation_mode || "none") !== "none") $("sec-fonts").open = true;

    state.activePreset = key;
    syncStyleControlStates();
    updateStyleReadouts();
    state.presetSignature = styleSignature();
    updatePresetStatus();
    updateContrastBadge();
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
        ? "Exportar MP4 con subtítulos incrustados"
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

    state.view = { startMs: 0, spanMs: 0 };
    updateZoomReadout();

    const previewMs = state.cues.length > 0 ? state.cues[0].start_ms + 120 : 0;
    state.activeCueIndex = state.cues.length > 0 ? 0 : -1;
    renderCueList();
    drawWaveform(previewMs);
    renderPreviewAtMs(previewMs);
  }

  // Perfil de hardware detectado por el servidor (Intel iMac 2017 / Apple Silicon).
  function fillThreadOptions(sel, hw) {
    const maxT = Number(hw.threads_standalone || 4);
    const resolveT = Number(hw.threads_with_resolve || 3);
    const prev = sel.value;
    sel.innerHTML = "";
    for (let n = maxT; n >= 1; n--) {
      const opt = document.createElement("option");
      opt.value = String(n);
      const tag = n === maxT ? " (máximo)" : n === resolveT ? " (con Resolve)" : n === 2 ? " (silencioso)" : "";
      opt.textContent = `${n} hilo${n === 1 ? "" : "s"}${tag}`;
      sel.appendChild(opt);
    }
    if (prev) sel.value = prev;
  }

  function setGpuLabel(lbl, hw) {
    lbl.textContent = "";
    const code = document.createElement("code");
    code.textContent = "-ng";
    if (hw.profile_id === "apple_silicon") {
      lbl.append("Solo CPU, desactivar GPU Metal (", code, ")");
    } else {
      const vram = hw.gpu_vram_mb ? ` ${Math.round(hw.gpu_vram_mb / 1024)} GB` : "";
      lbl.append(`Proteger ${hw.gpu_name || "GPU"}${vram} (`, code, ", solo CPU)");
    }
  }

  // Perfil de hardware detectado por el servidor (Intel / Apple Silicon).
  function initHardwareControls(hw) {
    fillThreadOptions($("threads-select"), hw);
    fillThreadOptions($("mp-threads"), hw);
    setGpuLabel($("opt-no-gpu-label"), hw);
    setGpuLabel($("mp-no-gpu-label"), hw);
    $("hw-profile-note").textContent = hw.name || "";
  }

  // ---------------------------------------------------------------------------
  // Rendimiento de esta Mac: ajustes guardados en settings.json de cada equipo
  // ---------------------------------------------------------------------------
  const PERF_MODE_LABELS = { auto: "Automático", safe: "Seguro", max: "Máximo", custom: "Personalizado" };
  const PREF_FIELDS = [
    ["dialect", "dialect-select", "value"],
    ["domain", "domain-select", "value"],
    ["preset_id", "preset-select", "value"],
    ["max_cpl", "cfg-cpl", "number"],
    ["max_lines", "cfg-lines", "number"],
    ["pause_split_ms", "cfg-pause", "number"],
    ["fix_inverted_punctuation", "cfg-inverted", "checked"],
    ["remove_muletillas", "cfg-muletillas", "checked"],
    ["uppercase", "cfg-uppercase", "checked"],
    ["custom_glossary", "cfg-glossary", "value"],
  ];

  function fillModelOptions(sel, env, recommendedId) {
    const prev = sel.value;
    sel.innerHTML = "";
    ["turbo-q8", "large-v3-q5", "turbo-q5", "small-q8", "base-q8"].forEach((id) => {
      const m = env.models[id];
      if (!m) return;
      const opt = document.createElement("option");
      opt.value = id;
      opt.textContent = `${m.installed ? "✓ " : "↓ "}${m.label}${id === recommendedId ? " · recomendado" : ""}`;
      sel.appendChild(opt);
    });
    if (prev) sel.value = prev;
  }

  function renderMachineHardware(hw) {
    const dl = $("machine-hw");
    dl.textContent = "";
    const vram = hw.gpu_vram_mb ? ` · ${Math.round(hw.gpu_vram_mb / 1024)} GB VRAM` : "";
    [
      ["Equipo", hw.name],
      ["Procesador", hw.cpu_description],
      ["Memoria", hw.ram_gb ? `${hw.ram_gb} GB RAM` : "—"],
      ["GPU", `${hw.gpu_name || "—"}${vram}${hw.force_no_gpu ? " (Whisper usa solo CPU)" : " (Whisper usa Metal)"}`],
    ].forEach(([k, v]) => {
      const dt = document.createElement("dt");
      dt.textContent = k;
      const dd = document.createElement("dd");
      dd.textContent = v || "—";
      dl.append(dt, dd);
    });
  }

  function renderBenchHistory(benchmarks) {
    const ul = $("bench-history");
    ul.textContent = "";
    Object.values(benchmarks || {})
      .sort((a, b) => (b.at || 0) - (a.at || 0))
      .forEach((b) => {
        const m = state.env && state.env.models[b.model_id];
        const li = document.createElement("li");
        const when = b.at ? new Date(b.at * 1000).toLocaleDateString("es-MX") : "";
        li.textContent =
          `${m ? m.label : b.model_id}: ${b.realtime_factor}× tiempo real ` +
          `(${b.threads} hilos, ${b.force_no_gpu ? "CPU" : "GPU"}) ${when}`;
        ul.appendChild(li);
      });
  }

  // Refleja los ajustes efectivos del servidor en la tarjeta 2 y en el diálogo.
  function applyMachinePayload(m, { firstLoad = false } = {}) {
    if (!m) return;
    state.machine = m;
    const eff = m.effective;
    const mode = m.settings.performance.mode || "auto";
    if (state.env) {
      fillModelOptions($("mp-model"), state.env, m.recommended_model_id);
    }
    $("threads-select").value = String(eff.threads);
    $("mp-threads").value = String(eff.threads);
    $("beam-mode").value = eff.fast_beam ? "fast" : "quality";
    $("mp-beam").value = eff.fast_beam ? "fast" : "quality";
    $("opt-no-gpu").checked = eff.force_no_gpu;
    $("mp-no-gpu").checked = eff.force_no_gpu;
    $("opt-enhance-voice").checked = eff.enhance_voice;
    $("mp-enhance").checked = eff.enhance_voice;
    $("mp-model").value = eff.model_id;
    if (firstLoad && $("model-select").querySelector(`option[value="${eff.model_id}"]`)) {
      $("model-select").value = eff.model_id;
      updateModelHint();
    }
    const radio = document.querySelector(`input[name="perf-mode"][value="${mode}"]`);
    if (radio) radio.checked = true;
    $("perf-mode-pill").textContent = PERF_MODE_LABELS[mode] || mode;
    $("perf-mode-pill").className = "pill " + (mode === "auto" ? "pill-info" : "pill-neutral");
    renderMachineHardware(m.profile);
    renderBenchHistory(m.settings.benchmarks);
    $("machine-settings-path").textContent = m.settings_path || "";
    updateHardwareBadges();
  }

  async function saveMachineSettings(patch, toastMsg = "") {
    try {
      const m = await apiJson("/api/settings", { method: "PUT", body: JSON.stringify(patch) });
      applyMachinePayload(m);
      if (toastMsg) showToast(toastMsg);
    } catch (err) {
      showToast(`No se pudo guardar el ajuste: ${err.message}`);
    }
  }

  // Cambiar hilos, GPU o precisión a mano convierte el perfil en "Personalizado".
  function saveCustomPerformance(threadsSel, beamSel, gpuChk) {
    saveMachineSettings(
      {
        performance: {
          mode: "custom",
          threads: Number(threadsSel.value),
          fast_beam: beamSel.value === "fast",
          force_no_gpu: gpuChk.checked,
        },
      },
      "Perfil personalizado guardado para esta Mac."
    );
  }

  function readPreferencesFromUI() {
    const prefs = {};
    PREF_FIELDS.forEach(([key, id, kind]) => {
      const el = $(id);
      if (!el) return;
      prefs[key] = kind === "checked" ? el.checked : kind === "number" ? Number(el.value) : el.value;
    });
    return prefs;
  }

  function applyPreferencesToUI(prefs) {
    if (!prefs) return;
    PREF_FIELDS.forEach(([key, id, kind]) => {
      const el = $(id);
      if (!el || prefs[key] === undefined || prefs[key] === null) return;
      if (kind === "checked") el.checked = Boolean(prefs[key]);
      else el.value = String(prefs[key]);
    });
  }

  function schedulePreferencesSave() {
    clearTimeout(state.prefsTimer);
    state.prefsTimer = setTimeout(() => {
      apiJson("/api/settings", {
        method: "PUT",
        body: JSON.stringify({ preferences: readPreferencesFromUI() }),
      }).catch(() => {});
    }, 600);
  }

  async function exportPreferences() {
    try {
      const data = await apiJson("/api/settings/export");
      data.preferences = {
        ...data.preferences,
        ...readPreferencesFromUI(),
        karaoke_enabled: $("st-karaoke").checked,
        custom_style_presets: loadCustomPresets(),
      };
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "autosubs-preferencias.json";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(a.href);
    } catch (err) {
      showToast(`No se pudo exportar: ${err.message}`);
    }
  }

  async function importPreferences(file) {
    if (!file) return;
    try {
      const payload = JSON.parse(await file.text());
      const prefs = payload && payload.preferences;
      if (!prefs || typeof prefs !== "object") {
        throw new Error("el archivo no contiene preferencias de AutoSubs");
      }
      await apiJson("/api/settings/import", { method: "POST", body: JSON.stringify(payload) });
      applyPreferencesToUI(prefs);
      if (typeof prefs.karaoke_enabled === "boolean") {
        $("st-karaoke").checked = prefs.karaoke_enabled;
        $("st-karaoke").dispatchEvent(new Event("change"));
      }
      if (prefs.custom_style_presets && typeof prefs.custom_style_presets === "object") {
        saveCustomPresets({ ...loadCustomPresets(), ...prefs.custom_style_presets });
        renderCustomChips();
      }
      showToast("Preferencias importadas. Los ajustes de rendimiento de esta Mac no cambiaron.");
    } catch (err) {
      showToast(`No se pudo importar: ${err.message}`, 5000);
    } finally {
      $("prefs-import-input").value = "";
    }
  }

  async function runBenchmark() {
    const box = $("bench-result");
    const btn = $("btn-benchmark");
    box.classList.remove("hidden");
    $("btn-bench-apply").classList.add("hidden");
    box.textContent = "Iniciando prueba...";
    btn.disabled = true;
    try {
      const res = await apiJson("/api/benchmark", {
        method: "POST",
        body: JSON.stringify({ model_id: $("mp-model").value }),
      });
      const timer = setInterval(async () => {
        try {
          const job = await apiJson(`/api/jobs/${res.job_id}`);
          box.textContent = `${job.stage || "Procesando..."} (${job.progress || 0}%)`;
          if (job.status === "done" || job.status === "error" || job.status === "cancelled") {
            clearInterval(timer);
            btn.disabled = false;
            if (job.status !== "done") {
              box.textContent = job.error || job.stage;
              return;
            }
            const r = job.result;
            const rec = state.env && state.env.models[r.recommended_model_id];
            box.textContent =
              `${r.realtime_factor}× tiempo real: ${r.audio_s} s de audio en ${r.elapsed_s} s ` +
              `(${r.threads} hilos, ${r.force_no_gpu ? "solo CPU" : "GPU"}). ${r.note}` +
              (r.recommended_model_id !== r.model_id && rec ? ` Recomendado: ${rec.label}.` : "");
            if (r.recommended_model_id !== $("mp-model").value) {
              const apply = $("btn-bench-apply");
              apply.dataset.model = r.recommended_model_id;
              apply.classList.remove("hidden");
            }
            const m = await apiJson("/api/settings");
            applyMachinePayload(m);
          }
        } catch (err) {
          clearInterval(timer);
          btn.disabled = false;
          box.textContent = `Error consultando la prueba: ${err.message}`;
        }
      }, 700);
    } catch (err) {
      btn.disabled = false;
      box.textContent = err.message;
    }
  }

  function wireMachinePanel() {
    // Respaldo para WebKit sin Invoker Commands (command / commandfor).
    if (!("commandForElement" in HTMLButtonElement.prototype)) {
      document.addEventListener("click", (ev) => {
        const btn = ev.target.closest("button[commandfor]");
        const target = btn && $(btn.getAttribute("commandfor"));
        if (!target) return;
        const cmd = btn.getAttribute("command");
        if (cmd === "show-modal") target.showModal();
        else if (cmd === "close") target.close();
      });
    }
    const dialog = $("machine-dialog");
    // Respaldo de closedby="any" (cerrar al hacer clic fuera) para Safari/WebKit.
    if (!("closedBy" in HTMLDialogElement.prototype)) {
      const outside = (e) => {
        const r = dialog.getBoundingClientRect();
        return e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom;
      };
      let pressedOutside = false;
      dialog.addEventListener("pointerdown", (e) => { pressedOutside = outside(e); });
      dialog.addEventListener("pointerup", (e) => { if (pressedOutside && outside(e)) dialog.close(); });
    }

    document.querySelectorAll('input[name="perf-mode"]').forEach((radio) => {
      radio.addEventListener("change", () => {
        if (!radio.checked) return;
        saveMachineSettings(
          { performance: { mode: radio.value } },
          `Perfil «${PERF_MODE_LABELS[radio.value]}» guardado para esta Mac.`
        );
      });
    });
    ["mp-threads", "mp-beam", "mp-no-gpu"].forEach((id) =>
      $(id).addEventListener("change", () => saveCustomPerformance($("mp-threads"), $("mp-beam"), $("mp-no-gpu")))
    );
    ["threads-select", "beam-mode", "opt-no-gpu"].forEach((id) =>
      $(id).addEventListener("change", () => saveCustomPerformance($("threads-select"), $("beam-mode"), $("opt-no-gpu")))
    );
    $("mp-model").addEventListener("change", () => {
      $("model-select").value = $("mp-model").value;
      updateModelHint();
      saveMachineSettings({ performance: { model_id: $("mp-model").value } });
    });
    $("model-select").addEventListener("change", () => {
      saveMachineSettings({ performance: { model_id: $("model-select").value } });
    });
    $("mp-enhance").addEventListener("change", () =>
      saveMachineSettings({ performance: { enhance_voice: $("mp-enhance").checked } })
    );
    $("opt-enhance-voice").addEventListener("change", () =>
      saveMachineSettings({ performance: { enhance_voice: $("opt-enhance-voice").checked } })
    );
    $("btn-perf-reset").addEventListener("click", async () => {
      try {
        const m = await apiJson("/api/settings/reset", { method: "POST", body: "{}" });
        applyMachinePayload(m);
        showToast("Rendimiento restablecido a lo recomendado para esta Mac.");
      } catch (err) {
        showToast(err.message);
      }
    });
    $("btn-benchmark").addEventListener("click", runBenchmark);
    $("btn-bench-apply").addEventListener("click", () => {
      const id = $("btn-bench-apply").dataset.model;
      if (!id) return;
      $("mp-model").value = id;
      $("mp-model").dispatchEvent(new Event("change"));
      $("btn-bench-apply").classList.add("hidden");
    });
    $("btn-prefs-export").addEventListener("click", exportPreferences);
    $("btn-prefs-import").addEventListener("click", () => $("prefs-import-input").click());
    $("prefs-import-input").addEventListener("change", (ev) => importPreferences(ev.target.files[0]));

    PREF_FIELDS.forEach(([, id]) => {
      const el = $(id);
      if (el) el.addEventListener("change", schedulePreferencesSave);
    });
    $("cfg-glossary").addEventListener("input", schedulePreferencesSave);
  }

  function updateHardwareBadges() {
    const hw = state.hwProfile || {};
    const isApple = hw.profile_id === "apple_silicon";
    const threads = $("threads-select").value || hw.threads_standalone || 4;
    const cpuBadge = $("badge-cpu");
    cpuBadge.textContent = `${hw.cpu_short || "CPU"} · ${threads} hilos`;
    cpuBadge.title = `${hw.cpu_short || "CPU"}: ${threads} de ${hw.logical_threads || 4} hilos para Whisper`;

    const noGpu = $("opt-no-gpu").checked;
    const gpuBadge = $("badge-gpu");
    if (isApple) {
      gpuBadge.textContent = noGpu ? "Solo CPU" : "Metal";
      gpuBadge.title = noGpu ? "GPU Metal: sin usar (-ng)" : "GPU Metal: activa";
      gpuBadge.className = "pill pill-neutral";
    } else {
      const vram = hw.gpu_vram_mb ? ` ${Math.round(hw.gpu_vram_mb / 1024)} GB VRAM` : "";
      gpuBadge.textContent = noGpu ? "GPU protegida" : "GPU en uso";
      gpuBadge.title = noGpu
        ? `${hw.gpu_name || "GPU"}${vram}: 0 MB (-ng)`
        : `${hw.gpu_name || "GPU"}${vram}: en uso (puede competir con Resolve)`;
      gpuBadge.className = "pill " + (noGpu ? "pill-neutral" : "pill-warn");
    }
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
        const firstFonts = !state.fontCatalog;
        updateFontCatalog(data.fonts);
        // El preset inicial se aplica antes de conocer las fuentes, cuando aún
        // no existe el peso "Semibold"; se reaplica si el usuario no lo tocó.
        if (firstFonts && state.activePreset && styleSignature() === state.presetSignature) {
          applyStylePreset(state.activePreset);
        }
      }

      if (!state.userChoseMode) {
        const resolveReady =
          Boolean(data.environment.resolve_running) ||
          Boolean(data.bridge && data.bridge.inbox_audio_path);
        setAppMode(resolveReady ? "resolve" : "standalone", false);
      }

      const resolveBadge = $("badge-resolve");
      const bridgeMode = data.bridge.mode;
      resolveBadge.textContent =
        bridgeMode === "studio_direct"
          ? "Resolve Studio"
          : bridgeMode === "lua_bridge_ready"
          ? "Resolve listo"
          : "Resolve sin scripts";
      resolveBadge.title = data.bridge.mode_label;
      resolveBadge.className =
        "pill " + (bridgeMode === "studio_direct" || bridgeMode === "lua_bridge_ready" ? "pill-neutral" : "pill-warn");

      const hw = data.environment.hardware_profile || {};
      const firstLoad = !state.hwProfile;
      if (firstLoad) {
        state.hwProfile = hw;
        initHardwareControls(hw);
      }
      applyMachinePayload(data.machine, { firstLoad });
      if (firstLoad && data.machine) {
        // Preferencias guardadas en esta Mac (la sesión activa, si existe, manda después).
        applyPreferencesToUI(data.machine.settings.preferences);
      }

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
        // Al restaurar una sesión al abrir la app, los selectores muestran las
        // preferencias guardadas (lo que el usuario eligió por última vez), no
        // la configuración con la que se generó esa sesión.
        if (firstLoad && data.machine) applyPreferencesToUI(data.machine.settings.preferences);
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
      startIn.className = "cue-start";
      startIn.type = "number";
      startIn.step = "10";
      startIn.value = String(cue.start_ms);
      startIn.title = "Inicio en milisegundos";
      startIn.addEventListener("change", () => {
        const oldStart = cue.start_ms;
        cue.start_ms = Math.max(0, Number(startIn.value || 0));
        cue.words = rescaleWords(cue.words, oldStart, cue.end_ms, cue.start_ms, cue.end_ms);
        updateCueRowTimes(idx);
        drawWaveform(currentMs());
        scheduleSaveCues();
      });

      const endIn = document.createElement("input");
      endIn.className = "cue-end";
      endIn.type = "number";
      endIn.step = "10";
      endIn.value = String(cue.end_ms);
      endIn.title = "Fin en milisegundos";
      endIn.addEventListener("change", () => {
        const oldEnd = cue.end_ms;
        cue.end_ms = Math.max(cue.start_ms + 80, Number(endIn.value || cue.start_ms + 500));
        cue.words = rescaleWords(cue.words, cue.start_ms, oldEnd, cue.start_ms, cue.end_ms);
        updateCueRowTimes(idx);
        drawWaveform(currentMs());
        scheduleSaveCues();
      });

      const tcSpan = document.createElement("span");
      tcSpan.className = "cue-tc";
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

      const btnPlayCue = makeMiniBtn("▶", "Escuchar este subtítulo", () => seekAndPlay(cue.start_ms, cue.end_ms));
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
        ensureMsVisible(cue.start_ms);
        drawWaveform(currentMs());
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

  // Las palabras se conservan al editar: el servidor (align_words_to_lines)
  // mantiene los tiempos de Whisper de las palabras que siguen y reparte el
  // tiempo entre las nuevas. Aquí solo se sincroniza el texto si coincide el número.
  function syncCueWordsAfterTextEdit(cue) {
    const tokens = (cue.lines || []).flatMap((ln) => ln.split(/\s+/).filter(Boolean));
    if (cue.words && cue.words.length === tokens.length) {
      cue.words.forEach((w, i) => { w.text = tokens[i]; });
    }
  }

  function rescaleWords(words, oldStart, oldEnd, newStart, newEnd) {
    if (!words || !words.length) return [];
    const scale = (newEnd - newStart) / Math.max(1, oldEnd - oldStart);
    return words.map((w) => ({
      ...w,
      start_ms: Math.round(newStart + (w.start_ms - oldStart) * scale),
      end_ms: Math.round(newStart + (w.end_ms - oldStart) * scale),
    }));
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
    state.editSeq += 1;
    clearTimeout(state.saveTimer);
    state.saveTimer = setTimeout(saveCuesToServer, 350);
  }

  async function saveCuesToServer() {
    clearTimeout(state.saveTimer);
    const seq = state.editSeq;
    try {
      const data = await apiJson("/api/cues", {
        method: "PUT",
        body: JSON.stringify({ cues: state.cues }),
      });
      // Ignora respuestas viejas si hubo ediciones mientras viajaba la petición.
      if (data.session && seq === state.editSeq) {
        const serverCues = data.session.cues || [];
        if (serverCues.length === state.cues.length) {
          // Actualiza en sitio: los textareas abiertos siguen apuntando a estos objetos.
          serverCues.forEach((c, i) => Object.assign(state.cues[i], c));
        } else {
          state.cues = serverCues;
          renderCueList();
        }
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
    // Con tiempos por palabra, corta justo donde empieza la palabra central.
    const timed = (cue.words || []).length === words.length ? cue.words : null;
    const rawMid = timed ? timed[midWord].start_ms : (cue.start_ms + cue.end_ms) / 2;
    const midMs = Math.round(Math.min(Math.max(rawMid, cue.start_ms + 80), cue.end_ms - 80));
    const first = {
      ...cue,
      end_ms: midMs,
      lines: [words.slice(0, midWord).join(" ")],
      text: words.slice(0, midWord).join(" "),
      words: timed ? timed.slice(0, midWord) : [],
    };
    const second = {
      ...cue,
      start_ms: midMs,
      lines: [words.slice(midWord).join(" ")],
      text: words.slice(midWord).join(" "),
      words: timed ? timed.slice(midWord) : [],
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
    cap.style.color = hexToRgba(st.fill_hex, st.fill_alpha);
    cap.style.background = st.box_enabled ? hexToRgba(st.box_hex, st.box_alpha) : "transparent";

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

    // Contorno real (stroke) pintado bajo el relleno: con texto al 0 % quedan
    // letras huecas, igual que en .ASS y Text+.
    const ol = Math.max(0, Math.round(st.outline_thickness * 18));
    cap.style.webkitTextStroke = ol > 0 ? `${ol * 2}px ${hexToRgba(st.outline_hex, st.outline_alpha)}` : "0";
    cap.style.paintOrder = "stroke fill";
    cap.style.textShadow = st.shadow_enabled
      ? `0 3px 8px ${hexToRgba("#000000", st.shadow_alpha)}`
      : "none";

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

    if (
      st.karaoke_enabled &&
      active.words && active.words.length > 0 &&
      ms >= active.start_ms && ms <= active.end_ms
    ) {
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
            span.style.color = hexToRgba(st.highlight_hex, st.highlight_alpha);
          }
          cap.appendChild(span);
        });
      });
    } else {
      cap.textContent = transformToken(active.text || "");
    }
  }

  function getCurrentScheme() {
    const meta = document.querySelector('meta[name="color-scheme"]');
    const content = meta ? meta.content : "light dark";
    if (content === "light" || content === "dark") return content;
    const attr = document.documentElement.getAttribute("data-theme");
    if (attr === "light" || attr === "dark") return attr;
    if (window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches) {
      return "light";
    }
    return "dark";
  }

  function applyTheme(theme, persist = true) {
    const meta = document.querySelector('meta[name="color-scheme"]');
    const systemPref = (window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches) ? "light" : "dark";

    if (theme === "system" || !theme) {
      if (persist) {
        try { localStorage.removeItem("autosubs-theme"); } catch (_) {}
      }
      if (meta) meta.content = "light dark";
      document.documentElement.removeAttribute("data-theme");
    } else {
      if (persist) {
        try {
          if (theme === systemPref) {
            localStorage.removeItem("autosubs-theme");
          } else {
            localStorage.setItem("autosubs-theme", theme);
          }
        } catch (_) {}
      }
      if (meta) meta.content = theme;
      document.documentElement.setAttribute("data-theme", theme);
    }

    const btn = $("btn-theme-toggle");
    if (btn) {
      const active = getCurrentScheme();
      btn.setAttribute("aria-label", active === "dark" ? "Cambiar a tema claro" : "Cambiar a tema oscuro");
      btn.title = active === "dark" ? "Cambiar a tema claro" : "Cambiar a tema oscuro";
    }

    if (typeof drawWaveform === "function") {
      const mediaEl = activeMedia();
      drawWaveform(mediaEl ? (mediaEl.currentTime || 0) * 1000 : 0);
    }
  }

  function toggleTheme() {
    const current = getCurrentScheme();
    const target = current === "dark" ? "light" : "dark";
    applyTheme(target, true);
    showToast(`Tema ${target === "light" ? "claro" : "oscuro"} activado`);
  }

  // ---------------------------------------------------------------------------
  // Forma de onda: regla de tiempo, zoom/desplazamiento y arrastre de subtítulos
  // ---------------------------------------------------------------------------
  const WAVE_RULER_H = 14;
  const WAVE_LANE_H = 26;
  const WAVE_EDGE_PX = 8;
  const MIN_CUE_MS = 200;
  const MIN_VIEW_MS = 2000;

  function currentMs() {
    return (activeMedia().currentTime || 0) * 1000;
  }

  function totalDurMs() {
    const m = activeMedia();
    const lastEnd = state.cues.length ? state.cues[state.cues.length - 1].end_ms : 0;
    return Math.max(1000, state.durationMs || (m.duration ? m.duration * 1000 : 0) || lastEnd);
  }

  function viewRange() {
    const dur = totalDurMs();
    const span = state.view.spanMs > 0 ? Math.min(state.view.spanMs, dur) : dur;
    const start = Math.max(0, Math.min(state.view.startMs, dur - span));
    return { start, span, dur };
  }

  function setView(startMs, spanMs) {
    const dur = totalDurMs();
    const span = Math.max(MIN_VIEW_MS, Math.min(spanMs, dur));
    state.view.spanMs = span >= dur ? 0 : span;
    state.view.startMs = Math.max(0, Math.min(startMs, dur - span));
    updateZoomReadout();
    drawWaveform(currentMs());
  }

  function zoomAround(centerMs, factor) {
    const { start, span } = viewRange();
    const f = Math.max(0.25, Math.min(4, factor));
    const frac = (centerMs - start) / span;
    setView(centerMs - frac * span * f, span * f);
  }

  function updateZoomReadout() {
    const el = $("zoom-readout");
    if (!el) return;
    const { span, dur } = viewRange();
    el.textContent =
      span >= dur ? "Todo"
      : span >= 60000 ? `${(span / 60000).toFixed(1)} min`
      : `${(span / 1000).toFixed(span < 10000 ? 1 : 0)} s`;
  }

  function ensureMsVisible(ms, pad = 0.1) {
    if (state.view.spanMs <= 0) return;
    const { start, span } = viewRange();
    if (ms < start || ms > start + span) setView(ms - span * pad, span);
  }

  function waveSize() {
    const canvas = $("waveform-canvas");
    const rect = canvas.getBoundingClientRect();
    return { canvas, w: rect.width || 960, h: rect.height || 96 };
  }

  const msToX = (ms, w, vr) => ((ms - vr.start) / vr.span) * w;
  const xToMs = (x, w, vr) => vr.start + (x / w) * vr.span;

  function formatTick(ms, step) {
    const totalS = ms / 1000;
    const m = Math.floor(totalS / 60);
    const sec = totalS - m * 60;
    const secStr = step < 1000 ? sec.toFixed(1).padStart(4, "0") : String(Math.floor(sec)).padStart(2, "0");
    return `${m}:${secStr}`;
  }

  function drawWaveform(playheadMs) {
    const { canvas, w, h } = waveSize();
    const dpr = window.devicePixelRatio || 1;
    const pw = Math.round(w * dpr);
    const ph = Math.round(h * dpr);
    if (canvas.width !== pw || canvas.height !== ph) {
      canvas.width = pw;
      canvas.height = ph;
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    const isLight = getCurrentScheme() === "light";
    ctx.fillStyle = isLight ? "#f0f0f3" : "#141417";
    ctx.fillRect(0, 0, w, h);

    const vr = viewRange();
    const laneTop = h - WAVE_LANE_H;
    const waveTop = WAVE_RULER_H;
    const waveBottom = laneTop - 2;
    const waveMid = (waveTop + waveBottom) / 2;
    const waveHalf = (waveBottom - waveTop) / 2;
    const mutedText = isLight ? "#71717a" : "#92929c";

    // Regla de tiempo
    const steps = [100, 250, 500, 1000, 2000, 5000, 10000, 15000, 30000, 60000, 120000, 300000, 600000];
    const step = steps.find((st) => (st / vr.span) * w >= 70) || 600000;
    ctx.fillStyle = mutedText;
    ctx.font = "10px -apple-system, BlinkMacSystemFont, sans-serif";
    ctx.textBaseline = "top";
    for (let t = Math.ceil(vr.start / step) * step; t <= vr.start + vr.span; t += step) {
      const x = Math.round(msToX(t, w, vr));
      ctx.fillRect(x, 0, 1, 4);
      ctx.fillText(formatTick(t, step), x + 3, 2);
    }

    // Regiones de subtítulos sobre la onda
    state.cues.forEach((cue, idx) => {
      const x1 = msToX(cue.start_ms, w, vr);
      const x2 = msToX(cue.end_ms, w, vr);
      if (x2 < 0 || x1 > w) return;
      ctx.fillStyle = idx === state.activeCueIndex ? "rgba(131,103,199,0.16)" : "rgba(131,103,199,0.06)";
      ctx.fillRect(x1, waveTop, Math.max(1, x2 - x1), waveBottom - waveTop);
    });

    // Picos de audio (máximo por columna de píxel en la ventana visible)
    const peaks = state.peaks || [];
    if (peaks.length > 0 && state.durationMs > 0) {
      const n = peaks.length;
      const perMs = n / state.durationMs;
      ctx.fillStyle = isLight ? "#94949e" : "#555562";
      for (let x = 0; x < w; x++) {
        const i0 = Math.max(0, Math.floor(xToMs(x, w, vr) * perMs));
        const i1 = Math.min(n, Math.max(i0 + 1, Math.floor(xToMs(x + 1, w, vr) * perMs)));
        let pk = 0;
        for (let i = i0; i < i1; i++) if (peaks[i] > pk) pk = peaks[i];
        const amp = Math.max(0.5, pk * waveHalf);
        ctx.fillRect(x, waveMid - amp, 1, amp * 2);
      }
    }

    // Carril de bloques arrastrables
    ctx.textBaseline = "middle";
    state.cues.forEach((cue, idx) => {
      const x1 = msToX(cue.start_ms, w, vr);
      const x2 = msToX(cue.end_ms, w, vr);
      if (x2 < 0 || x1 > w) return;
      const active = idx === state.activeCueIndex || (state.drag && state.drag.idx === idx);
      const bw = Math.max(2, x2 - x1 - 1);
      ctx.fillStyle = active
        ? "rgba(131,103,199,0.92)"
        : isLight ? "rgba(131,103,199,0.35)" : "rgba(131,103,199,0.32)";
      ctx.beginPath();
      if (ctx.roundRect) ctx.roundRect(x1 + 0.5, laneTop + 2, bw, WAVE_LANE_H - 4, 4);
      else ctx.rect(x1 + 0.5, laneTop + 2, bw, WAVE_LANE_H - 4);
      ctx.fill();
      if (active && bw > 10) {
        ctx.fillStyle = "rgba(255,255,255,0.85)";
        ctx.fillRect(x1 + 2, laneTop + 8, 2, WAVE_LANE_H - 16);
        ctx.fillRect(x2 - 4, laneTop + 8, 2, WAVE_LANE_H - 16);
      }
      if (bw > 34) {
        ctx.save();
        ctx.beginPath();
        ctx.rect(x1 + 6, laneTop, bw - 12, WAVE_LANE_H);
        ctx.clip();
        ctx.fillStyle = active ? "#ffffff" : isLight ? "#3f3f46" : "#d4d4d8";
        ctx.fillText(`#${idx + 1} ${(cue.text || "").replace(/\n/g, " ")}`, x1 + 7, laneTop + WAVE_LANE_H / 2);
        ctx.restore();
      }
    });

    // Cabezal de reproducción
    const px = msToX(playheadMs, w, vr);
    if (px >= 0 && px <= w) {
      ctx.fillStyle = "#8367c7";
      ctx.fillRect(Math.round(px), 0, 2, h);
    }
  }

  // Devuelve { idx, mode: "start" | "end" | "move" } si el puntero está sobre un bloque.
  function hitTestCue(x, y) {
    const { w, h } = waveSize();
    // El carril acepta clics un poco por encima de su borde superior.
    if (y < h - WAVE_LANE_H - 8) return null;
    const vr = viewRange();
    const spans = state.cues.map((cue) => [msToX(cue.start_ms, w, vr), msToX(cue.end_ms, w, vr)]);
    const tolFor = (x1, x2) => Math.min(WAVE_EDGE_PX, Math.max(2, (x2 - x1) / 3));
    // Dentro de un bloque solo cuentan sus propios bordes (nunca los del vecino).
    const insideIdx = spans.findIndex(([x1, x2]) => x >= x1 && x <= x2);
    if (insideIdx >= 0) {
      const [x1, x2] = spans[insideIdx];
      const tol = tolFor(x1, x2);
      if (x - x1 <= tol) return { idx: insideIdx, mode: "start" };
      if (x2 - x <= tol) return { idx: insideIdx, mode: "end" };
      return { idx: insideIdx, mode: "move" };
    }
    // En un hueco: el borde más cercano dentro de la tolerancia.
    let edge = null;
    spans.forEach(([x1, x2], idx) => {
      const tol = tolFor(x1, x2);
      [["start", Math.abs(x - x1)], ["end", Math.abs(x - x2)]].forEach(([mode, dist]) => {
        if (dist <= tol && (!edge || dist < edge.dist)) edge = { idx, mode, dist };
      });
    });
    return edge;
  }

  function seekToMs(ms) {
    state.playUntilMs = null;
    const clamped = Math.max(0, Math.min(ms, totalDurMs()));
    const m = activeMedia();
    try {
      if (m.getAttribute("src")) m.currentTime = clamped / 1000;
    } catch (_) {}
    drawWaveform(clamped);
    renderPreviewAtMs(clamped);
  }

  function selectCue(idx) {
    state.activeCueIndex = idx;
    document.querySelectorAll(".cue-item").forEach((el, i) => el.classList.toggle("active", i === idx));
    scrollCueIntoList(idx);
  }

  // Desplaza solo la lista de subtítulos. scrollIntoView también movía la
  // página y la forma de onda quedaba en otro sitio bajo el puntero, así que el
  // siguiente clic en un borde caía en el lugar equivocado.
  function scrollCueIntoList(idx) {
    const list = $("cue-list");
    const el = list.querySelector(`.cue-item[data-index="${idx}"]`);
    if (!el) return;
    const lr = list.getBoundingClientRect();
    const er = el.getBoundingClientRect();
    if (er.top < lr.top) list.scrollTop += er.top - lr.top - 4;
    else if (er.bottom > lr.bottom) list.scrollTop += er.bottom - lr.bottom + 4;
  }

  function updateCueRowTimes(idx) {
    const cue = state.cues[idx];
    const el = document.querySelector(`.cue-item[data-index="${idx}"]`);
    if (!cue || !el) return;
    el.querySelector(".cue-start").value = String(cue.start_ms);
    el.querySelector(".cue-end").value = String(cue.end_ms);
    el.querySelector(".cue-tc").textContent =
      `${formatMsReadable(cue.start_ms)} → ${formatMsReadable(cue.end_ms)}`;
  }

  function applyCueDrag(d, deltaMs) {
    const cue = state.cues[d.idx];
    const prevEnd = d.idx > 0 ? state.cues[d.idx - 1].end_ms : 0;
    const nextStart = d.idx < state.cues.length - 1 ? state.cues[d.idx + 1].start_ms : totalDurMs();
    const { start, end } = d.orig;
    const snap = (v) => Math.round(v / 10) * 10;
    const clamp = (v, lo, hi) => Math.max(lo, Math.min(v, Math.max(lo, hi)));
    let ns = start;
    let ne = end;
    if (d.mode === "start") {
      ns = clamp(snap(start + deltaMs), prevEnd, end - MIN_CUE_MS);
    } else if (d.mode === "end") {
      ne = clamp(snap(end + deltaMs), start + MIN_CUE_MS, nextStart);
    } else {
      const len = end - start;
      ns = clamp(snap(start + deltaMs), prevEnd, nextStart - len);
      ne = ns + len;
    }
    cue.start_ms = ns;
    cue.end_ms = ne;
    cue.words = rescaleWords(d.orig.words, start, end, ns, ne);
    updateCueRowTimes(d.idx);
    renderPreviewAtMs(d.mode === "end" ? ne - 20 : ns + 20);
    drawWaveform(currentMs());
  }

  function cancelCueDrag() {
    const d = state.drag;
    if (!d || d.kind !== "cue") return;
    const cue = state.cues[d.idx];
    cue.start_ms = d.orig.start;
    cue.end_ms = d.orig.end;
    cue.words = d.orig.words;
    state.drag = null;
    updateCueRowTimes(d.idx);
    drawWaveform(currentMs());
    renderPreviewAtMs(currentMs());
  }

  function wireWaveform() {
    const canvas = $("waveform-canvas");
    const local = (ev) => {
      const r = canvas.getBoundingClientRect();
      return { x: ev.clientX - r.left, y: ev.clientY - r.top };
    };

    canvas.addEventListener("pointerdown", (ev) => {
      if (ev.button !== 0) return;
      const { x, y } = local(ev);
      const { w } = waveSize();
      const vr = viewRange();
      canvas.setPointerCapture(ev.pointerId);
      const hit = hitTestCue(x, y);
      if (hit) {
        const cue = state.cues[hit.idx];
        state.drag = {
          kind: "cue",
          idx: hit.idx,
          mode: hit.mode,
          grabMs: xToMs(x, w, vr),
          startX: x,
          moved: false,
          orig: { start: cue.start_ms, end: cue.end_ms, words: (cue.words || []).map((wd) => ({ ...wd })) },
        };
        selectCue(hit.idx);
        drawWaveform(currentMs());
      } else {
        state.drag = { kind: "scrub" };
        seekToMs(xToMs(x, w, vr));
      }
    });

    canvas.addEventListener("pointermove", (ev) => {
      const { x, y } = local(ev);
      const d = state.drag;
      if (!d) {
        const hit = hitTestCue(x, y);
        canvas.style.cursor = !hit ? "pointer" : hit.mode === "move" ? "grab" : "ew-resize";
        return;
      }
      const { w } = waveSize();
      const vr = viewRange();
      if (d.kind === "scrub") {
        seekToMs(xToMs(x, w, vr));
        return;
      }
      if (!d.moved && Math.abs(x - d.startX) < 3) return;
      d.moved = true;
      canvas.style.cursor = d.mode === "move" ? "grabbing" : "ew-resize";
      applyCueDrag(d, xToMs(x, w, vr) - d.grabMs);
    });

    const endDrag = (cancelled) => {
      const d = state.drag;
      if (!d) return;
      if (d.kind !== "cue") {
        state.drag = null;
        return;
      }
      if (cancelled) {
        cancelCueDrag();
        return;
      }
      state.drag = null;
      const cue = state.cues[d.idx];
      if (!d.moved) {
        seekToMs(cue.start_ms);
        return;
      }
      scheduleSaveCues();
      renderCueList();
      selectCue(d.idx);
      drawWaveform(currentMs());
    };
    canvas.addEventListener("pointerup", () => endDrag(false));
    canvas.addEventListener("pointercancel", () => endDrag(true));
    document.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape" && state.drag && state.drag.kind === "cue") cancelCueDrag();
    });

    canvas.addEventListener(
      "wheel",
      (ev) => {
        const { x } = local(ev);
        const { w } = waveSize();
        const vr = viewRange();
        if (ev.ctrlKey || ev.metaKey) {
          // Pellizco en trackpad (ctrlKey) o ⌘ + rueda: zoom alrededor del puntero.
          ev.preventDefault();
          zoomAround(xToMs(x, w, vr), Math.exp(ev.deltaY * 0.01));
        } else if (state.view.spanMs > 0) {
          ev.preventDefault();
          const delta = Math.abs(ev.deltaX) > Math.abs(ev.deltaY) ? ev.deltaX : ev.deltaY;
          setView(vr.start + (delta / w) * vr.span, vr.span);
        }
      },
      { passive: false }
    );

    const zoomCenter = () => {
      const { start, span } = viewRange();
      const ms = currentMs();
      return ms >= start && ms <= start + span ? ms : start + span / 2;
    };
    $("btn-zoom-in").addEventListener("click", () => zoomAround(zoomCenter(), 0.5));
    $("btn-zoom-out").addEventListener("click", () => zoomAround(zoomCenter(), 2));
    $("btn-zoom-fit").addEventListener("click", () => setView(0, Infinity));
    window.addEventListener("resize", () => drawWaveform(currentMs()));
  }

  // Reproduce desde `ms`; con `stopAtMs` se pausa sola al llegar ahí
  // (se revisa en cada frame, así no se pasa ~250 ms como con timeupdate).
  function seekAndPlay(ms, stopAtMs = null) {
    const mediaEl = activeMedia();
    state.playUntilMs = stopAtMs;
    mediaEl.currentTime = Math.max(0, ms / 1000);
    mediaEl.play().catch(() => {});
    if (stopAtMs == null) return;
    cancelAnimationFrame(state.playUntilRaf);
    const tick = () => {
      const m = activeMedia();
      if (state.playUntilMs == null || m.paused) {
        state.playUntilMs = null;
        return;
      }
      if (m.currentTime * 1000 >= state.playUntilMs) {
        m.pause();
        // Retrocede unos ms para que el monitor siga mostrando este subtítulo
        // y no el siguiente cuando ambos se tocan.
        m.currentTime = Math.max(0, (state.playUntilMs - 30) / 1000);
        state.playUntilMs = null;
        return;
      }
      state.playUntilRaf = requestAnimationFrame(tick);
    };
    state.playUntilRaf = requestAnimationFrame(tick);
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
    if ($("btn-theme-toggle")) {
      $("btn-theme-toggle").addEventListener("click", toggleTheme);
    }
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
      state.playUntilMs = null; // El botón principal reproduce sin límite.
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
      if (!m.paused && !state.drag) ensureMsVisible(ms);
      renderPreviewAtMs(ms);
      drawWaveform(ms);
    };
    [audio, video].forEach((mEl) => {
      mEl.addEventListener("play", onPlay);
      mEl.addEventListener("pause", onPause);
      mEl.addEventListener("timeupdate", onTimeUpdate);
      // Muestra la duración en cuanto se conoce, sin esperar a reproducir.
      mEl.addEventListener("loadedmetadata", onTimeUpdate);
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

    wireWaveform();

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

    document.querySelectorAll(".style-chips").forEach((box) => {
      box.addEventListener("click", (ev) => {
        const del = ev.target.closest("[data-delete-preset]");
        if (del) {
          const map = loadCustomPresets();
          const name = (map[del.dataset.deletePreset] || {}).preset_name || "";
          delete map[del.dataset.deletePreset];
          saveCustomPresets(map);
          if (state.activePreset === `custom:${del.dataset.deletePreset}`) state.activePreset = null;
          renderCustomChips();
          showToast(`Estilo «${name}» borrado.`);
          return;
        }
        const chip = ev.target.closest(".chip[data-style]");
        if (chip) applyStylePreset(chip.dataset.style);
      });
    });

    const saveForm = $("preset-save-form");
    const toggleSaveForm = (open) => {
      saveForm.classList.toggle("hidden", !open);
      $("btn-preset-save").classList.toggle("hidden", open);
      if (open) {
        $("preset-save-name").value = "";
        $("preset-save-name").focus();
      }
    };
    $("btn-preset-save").addEventListener("click", () => toggleSaveForm(true));
    $("btn-preset-save-cancel").addEventListener("click", () => toggleSaveForm(false));
    saveForm.addEventListener("submit", (ev) => {
      ev.preventDefault();
      saveCurrentAsPreset($("preset-save-name").value);
      toggleSaveForm(false);
    });
    saveForm.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape") toggleSaveForm(false);
    });
    renderCustomChips();

    // Karaoke global (apagado por defecto, se recuerda entre sesiones).
    const syncKaraoke = () => {
      const on = $("st-karaoke").checked;
      syncStyleControlStates();
      updatePresetStatus();
      try { localStorage.setItem("autosubs-karaoke", on ? "1" : "0"); } catch (_) {}
      renderPreviewAtMs(currentMs());
    };
    try { $("st-karaoke").checked = localStorage.getItem("autosubs-karaoke") === "1"; } catch (_) {}
    syncStyleControlStates();
    $("st-karaoke").addEventListener("change", syncKaraoke);

    $("btn-preset-reset").addEventListener("click", () => {
      if (state.activePreset) applyStylePreset(state.activePreset);
    });

    // Fondo de prueba del monitor (útil para evaluar transparencias).
    const STAGE_BGS = ["black", "gray", "light", "checker"];
    const setStageBg = (bg, persist = true) => {
      const value = STAGE_BGS.includes(bg) ? bg : "black";
      const stage = $("preview-stage");
      STAGE_BGS.forEach((b) => stage.classList.toggle(`stage-bg-${b}`, b === value));
      document.querySelectorAll("#stage-bg-toggle .bg-swatch").forEach((btn) => {
        btn.setAttribute("aria-pressed", String(btn.dataset.bg === value));
      });
      if (persist) {
        try { localStorage.setItem("autosubs-stage-bg", value); } catch (_) {}
      }
    };
    document.querySelectorAll("#stage-bg-toggle .bg-swatch").forEach((btn) => {
      btn.addEventListener("click", () => setStageBg(btn.dataset.bg));
    });
    let savedBg = "black";
    try { savedBg = localStorage.getItem("autosubs-stage-bg") || "black"; } catch (_) {}
    setStageBg(savedBg, false);

    wireMachinePanel();
    ["st-weight", "st-ascii-fold", "st-size", "st-posy", "st-fill", "st-highlight", "st-outline", "st-outline-w", "st-shadow", "st-box", "st-box-color", "cfg-uppercase",
     "st-fill-a", "st-highlight-a", "st-outline-a", "st-shadow-a", "st-box-a", "st-anim-mode"]
      .forEach((id) => {
        $(id).addEventListener("input", onStyleControlChanged);
        $(id).addEventListener("change", onStyleControlChanged);
      });
    $("st-font").addEventListener("change", updatePresetStatus);

    function syncPushModeVisibility() {
      $("template-field").classList.toggle("hidden", $("push-mode").value !== "textplus");
    }
    $("push-mode").addEventListener("change", syncPushModeVisibility);
    $("push-mode").addEventListener("change", updateStyleTargetHint);
    syncPushModeVisibility();
    updateStyleTargetHint();

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

    // Sincronización de tema entre pestañas y con el sistema
    window.addEventListener("storage", (e) => {
      if (e.key === "autosubs-theme") {
        const t = e.newValue;
        applyTheme(t === "light" || t === "dark" ? t : "system", false);
      }
    });

    if (window.matchMedia) {
      window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
        let saved = null;
        try { saved = localStorage.getItem("autosubs-theme"); } catch (_) {}
        if (!saved) {
          applyTheme("system", false);
        }
      });
    }
  }

  wireEvents();
  let initialTheme = null;
  try { initialTheme = localStorage.getItem("autosubs-theme"); } catch (_) {}
  applyTheme(initialTheme, false);
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
