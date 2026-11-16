/**
 * @fileoverview Front-end controller for MTTN AutoSubs Tool (ui/index.html).
 *
 * Structure: the whole file is one strict-mode IIFE, so nothing leaks into
 * the global scope. index.html loads it with a plain <script> at the end of
 * <body>, so the DOM already exists when it runs. No framework, no build
 * step, no dependencies. Function declarations are hoisted inside the IIFE,
 * so helpers are often called above the line where they are defined.
 *
 * Pure, DOM-free helpers (text folding, contrast math, waveform geometry,
 * cue drag clamping and the job poller factory) live in ui/app_helpers.js,
 * which index.html loads first as window.AutoSubsHelpers. They are unit
 * tested with `node --test "tests/js/*.test.mjs"`.
 *
 * Server: everything talks to the local Python server (autosubs_server.py,
 * http://127.0.0.1:55120 by default) through same-origin relative URLs.
 * JSON endpoints go through apiJson(); binary uploads (media, fonts) use
 * fetch() with the raw file as body and its name in the X-Filename header.
 *   GET  /api/status         Environment, Resolve bridge, fonts, machine
 *                            settings and a summary of the active session.
 *   PUT  /api/cues           Persist edited cues (debounced, see editSeq).
 *   POST /api/transcribe     Start Whisper; returns a job_id to poll.
 *   GET  /api/jobs/<id>      Progress of transcribe, model download,
 *                            benchmark and MP4 burn-in jobs.
 *   POST /api/export         Subtitle file content (SRT, ASS, FCPXML...).
 *   POST /api/export-video   Burned-in MP4 render job.
 *   POST /api/resolve/push   Send cues to DaVinci Resolve (or prepare files
 *                            for the Lua bridge scripts in Resolve Free).
 *   GET/PUT /api/settings    Per-machine performance settings + preferences.
 *
 * Main flows:
 *   1. Init (bottom of file): wireEvents() -> applyTheme() ->
 *      applyStylePreset("netflix") -> refreshStatus(), which fills selects,
 *      picks Resolve vs standalone mode and restores an existing session.
 *   2. Media in: Resolve inbox (Lua script exports the timeline audio) or a
 *      file upload -> applySession() loads cues, waveform peaks and media.
 *   3. Transcribe: POST /api/transcribe -> pollJob() -> applySession().
 *   4. Edit: cue text areas, time inputs and waveform drags mutate
 *      state.cues in place, then scheduleSaveCues() debounces a PUT.
 *   5. Style: style card controls -> readStyleFromUI() -> live preview; the
 *      same style object is sent with every export and Resolve push.
 *   6. Output: subtitle files, burned-in MP4, or DaVinci Resolve.
 *
 * Sections follow source order, which is only loosely grouped by topic.
 * User-facing strings are Spanish by design (AGENTS.md); comments and
 * identifiers are English.
 */
(() => {
  "use strict";

  // Pure helpers from ui/app_helpers.js (see its JSDoc for each function).
  // Destructured once here, before any code below runs, so call sites keep
  // their short names.
  const {
    buildCuesPutBody,
    computeCueDrag,
    computeViewRange,
    contrastLevel,
    cpsClassName,
    createJobPoller,
    cuesRevOf,
    findPreviewCue,
    formatMsReadable,
    formatTick,
    hexToRgba,
    hitTestCueAt,
    isStaleJobResult,
    msToX,
    rescaleWords,
    syncCueWordsAfterTextEdit,
    transformPreviewText,
    worstCaseContrast,
    xToMs,
  } = window.AutoSubsHelpers;

  // ---------------------------------------------------------------------------
  // Application state
  // ---------------------------------------------------------------------------

  /**
   * Single mutable store for the whole UI. There is no reactivity: code that
   * changes a field must re-render whatever depends on it. Two more fields are
   * added lazily by applyStylePreset(): `activePreset` (chip key of the last
   * applied preset) and `presetSignature` (styleSignature() right after it).
   */
  const state = {
    // Last `environment` block from /api/status (models, Resolve,
    // hardware profile).
    env: null,
    // Resolve bridge status from /api/status (mode, inbox audio,
    // timeline_meta).
    bridge: null,
    // Server subtitle presets by id (max_cpl, max_lines,
    // pause_split_ms).
    presets: {},
    // Glossary domains reported by the server (e.g. juegos_mesa).
    domains: {},
    // Session cues: {index, start_ms, end_ms, text, lines[], words[],
    // cps, warnings[]}. Edited in place: cue list handlers close over
    // these objects.
    cues: [],
    // Waveform peak amplitudes (0..1), evenly spaced over durationMs.
    peaks: [],
    // Media duration in ms as reported by the server (0 = unknown).
    durationMs: 0,
    // Index into `cues` of the highlighted cue, or -1.
    activeCueIndex: -1,
    // Job shown in the progress box (target of the cancel button), or
    // null. Every polled job lives in jobPollers (see pollJob()).
    activeJobId: null,
    // setTimeout handle that hides the job box 2 s after success.
    jobHideTimer: null,
    // setTimeout handle of the debounced cue save (350 ms).
    saveTimer: null,
    // Monitor aspect ratio: "16:9" or "9:16".
    aspect: "16:9",
    // "resolve" (DaVinci Resolve workflow) or "standalone" (file in,
    // MP4 out). See setAppMode().
    appMode: "resolve",
    // True once the user clicked a mode button; stops
    // refreshStatus() from auto-selecting the mode.
    userChoseMode: false,
    // True when the session media is video (<video> player, burn-in).
    isVideo: false,
    // Server says the media can be rendered as a subtitled MP4.
    canBurninVideo: false,
    // Server media revision (name:mtime:size); a change forces the
    // player to reload its src.
    mediaRev: null,
    // Server cue revision (integer) the local cues are based on, sent
    // with PUT /api/cues; null when the server does not send one.
    cuesRev: null,
    // Resolves when the PUT /api/cues in flight (and its 409 recovery)
    // finishes; null when idle. Saves never overlap: each one must carry
    // the cues_rev of the previous reply (see saveCuesToServer).
    cuesSaveInFlight: null,
    // Last fonts payload: {packs, builtin_fonts, custom_faces}.
    fontCatalog: null,
    // #st-font <option> value -> font pack. Built-in fonts are
    // wrapped as single-family packs so every option resolves.
    packByOptionValue: {},
    // Pack currently selected in #st-font (null if none).
    selectedPack: null,
    // Synthetic clock (ms) driving preview animation while paused.
    animClockMs: 0,
    // setInterval handle advancing animClockMs every 80 ms while an
    // animation mode is active.
    animTimer: null,
    // Hardware profile from the first /api/status (threads, GPU).
    hwProfile: null,
    // Monotonic counter bumped on every local cue edit and every save.
    // A PUT /api/cues response is applied only if it is still the
    // latest (see saveCuesToServer).
    editSeq: 0,
    // Waveform viewport in ms; spanMs 0 means "fit the whole duration".
    view: { startMs: 0, spanMs: 0 },
    // Active waveform gesture: null, {kind: "scrub"} or {kind: "cue",
    // idx, mode: "start"|"end"|"move", grabMs, startX, moved, orig}.
    drag: null,
    // Auto-pause target (ms) for seekAndPlay(), or null.
    playUntilMs: null,
    // Last /api/settings payload (settings, effective, profile).
    machine: null,
    // setTimeout handle of the debounced preferences save (600 ms).
    prefsTimer: null,
    // requestAnimationFrame id of the seekAndPlay() stop watcher.
    playUntilRaf: 0,
  };

  /**
   * Returns the media element that plays the current session.
   * @return {!HTMLMediaElement} #video-player for video, else #audio-player.
   */
  function activeMedia() {
    return state.isVideo ? $("video-player") : $("audio-player");
  }

  // ---------------------------------------------------------------------------
  // Static data: built-in style presets
  // ---------------------------------------------------------------------------

  /**
   * Built-in style presets keyed by chip id (data-style). `size` and `pos_y`
   * are fractions of the video height (pos_y measured from the bottom);
   * outline_thickness is a unitless value shared with the exporters; alphas
   * are 0..1; anim_hold_frames counts 24 fps frames; template_name is the
   * Resolve Text+ template (.setting) used by the "textplus" push mode.
   */
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

  // ---------------------------------------------------------------------------
  // DOM helpers and small utilities
  // ---------------------------------------------------------------------------

  /**
   * Shorthand for document.getElementById.
   * @param {string} id Element id.
   * @return {?HTMLElement} The element, or null.
   */
  const $ = (id) => document.getElementById(id);

  /**
   * Shows the bottom-right toast, replacing any visible message. The hide
   * timer lives on the element so consecutive toasts restart it.
   * @param {string} msg Message (Spanish UI text).
   * @param {number=} ms How long to keep it visible, in ms.
   */
  function showToast(msg, ms = 3800) {
    const el = $("toast");
    el.textContent = msg;
    el.classList.remove("hidden");
    clearTimeout(el._hideTimer);
    el._hideTimer = setTimeout(() => el.classList.add("hidden"), ms);
  }

  // ---------------------------------------------------------------------------
  // API client
  // ---------------------------------------------------------------------------

  /**
   * Reads a fetch() response as JSON without trusting the body to be JSON.
   * Error pages from the server or a proxy can be plain text or HTML, and
   * resp.json() would turn them into an opaque SyntaxError; reading the text
   * first keeps the real message.
   * @async
   * @param {!Response} resp Response from fetch().
   * @param {string=} fallbackMsg Message for an HTTP error with empty body.
   * @return {!Promise<!Object>} Parsed JSON body of a 2xx response.
   * @throws {Error} With the server's `error` field, the first 200 chars of a
   *     non-JSON error body, or a generic message for empty/invalid JSON.
   *     HTTP errors also carry `status` (number) and `data` (parsed body or
   *     null) so callers can react to specific codes such as 409.
   */
  async function readJsonOrText(resp, fallbackMsg = "") {
    const raw = await resp.text();
    let data = null;
    if (raw) {
      try {
        data = JSON.parse(raw);
      } catch (_) {
        data = null;
      }
    }
    if (!resp.ok) {
      const msg =
        (data && data.error) ||
        raw.trim().slice(0, 200) ||
        fallbackMsg ||
        `Error HTTP ${resp.status}`;
      const err = new Error(msg);
      err.status = resp.status;
      err.data = data;
      throw err;
    }
    if (data === null) {
      throw new Error(`Respuesta no válida del servidor (HTTP ${resp.status})`);
    }
    return data;
  }

  /**
   * Calls a JSON endpoint of the local server and returns the parsed body.
   * `opts` is spread before `headers` so a caller passing opts.headers adds
   * to the JSON Content-Type instead of replacing the merged headers.
   * @async
   * @param {string} url Same-origin path, e.g. "/api/status".
   * @param {!RequestInit=} opts fetch() options (method, body, headers...).
   * @return {!Promise<!Object>} Parsed JSON body.
   * @throws {Error} See readJsonOrText().
   */
  async function apiJson(url, opts = {}) {
    const resp = await fetch(url, {
      ...opts,
      headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    });
    return readJsonOrText(resp);
  }

  /**
   * Starts a browser download of a URL through a temporary <a download>.
   * @param {string} href Same-origin or blob: URL.
   * @param {string} filename Suggested file name.
   */
  function triggerDownload(href, filename) {
    const a = document.createElement("a");
    a.href = href;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  /**
   * Downloads in-memory content as a file via a temporary Blob URL. The URL
   * is revoked 1 s later rather than synchronously because WebKit can cancel
   * a download whose blob URL is revoked inside the click handler.
   * @param {!Blob} blob File content.
   * @param {string} filename Suggested file name.
   */
  function downloadBlob(blob, filename) {
    const url = URL.createObjectURL(blob);
    triggerDownload(url, filename);
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  /**
   * Reads the segmentation options sent as `format_config` with transcribe,
   * demo and resegment requests.
   * @return {!Object} preset_id, dialect, domain, max_cpl (characters per
   *     line), max_lines, pause_split_ms, boolean fixes and custom_glossary.
   */
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

  // ---------------------------------------------------------------------------
  // Mode switching (DaVinci Resolve vs standalone)
  // ---------------------------------------------------------------------------

  /**
   * Switches between the Resolve workflow and the standalone (file -> MP4)
   * workflow: segmented control, source tab, which card is highlighted or
   * collapsed (.card-muted), which export button is primary, and the per-cue
   * Resolve jump button (hidden by .mode-standalone in CSS).
   * @param {string} mode "standalone"; any other value means "resolve".
   * @param {boolean=} userInitiated True for clicks; disables auto-detection
   *     in refreshStatus() from then on.
   */
  function setAppMode(mode, userInitiated = false) {
    state.appMode = mode === "standalone" ? "standalone" : "resolve";
    if (userInitiated) state.userChoseMode = true;

    $("btn-mode-standalone").classList.toggle("active", state.appMode === "standalone");
    $("btn-mode-resolve").classList.toggle("active", state.appMode === "resolve");

    const targetTab = state.appMode === "standalone" ? "file" : "resolve";
    document
      .querySelectorAll(".tab-btn")
      .forEach((b) => b.classList.toggle("active", b.dataset.tab === targetTab));
    $("tab-resolve").classList.toggle("active", targetTab === "resolve");
    $("tab-file").classList.toggle("active", targetTab === "file");

    const resolveCard = $("card-resolve-push");
    if (resolveCard) resolveCard.classList.toggle("card-muted", state.appMode === "standalone");
    const exportCard = $("card-standalone-export");
    if (exportCard) exportCard.classList.toggle("card-highlight", state.appMode === "standalone");
    // One primary action per area: in standalone mode it is the MP4 render.
    const burnBtn = $("btn-burn-video");
    if (burnBtn) {
      burnBtn.classList.toggle("btn-primary", state.appMode === "standalone");
      burnBtn.classList.toggle("btn-secondary", state.appMode !== "standalone");
    }
    const rTitle = $("resolve-push-title");
    if (rTitle) {
      rTitle.textContent =
        state.appMode === "standalone"
          ? "5. DaVinci Resolve 20 (Opcional)"
          : "5. DaVinci Resolve 20";
    }
    const cueList = $("cue-list");
    if (cueList) {
      cueList.classList.toggle("mode-resolve", state.appMode === "resolve");
      cueList.classList.toggle("mode-standalone", state.appMode === "standalone");
    }
    updateStyleTargetHint();
  }

  // ---------------------------------------------------------------------------
  // Style controls: readouts, user presets, contrast check
  // ---------------------------------------------------------------------------

  /**
   * Reads an opacity slider (0..100 %) as alpha.
   * @param {string} id Range input id.
   * @return {number} Alpha 0..1; 1 if the element is missing or not numeric.
   */
  function alphaOf(id) {
    const el = $(id);
    const v = el ? Number(el.value) : 100;
    return Math.max(0, Math.min(1, (Number.isFinite(v) ? v : 100) / 100));
  }

  // [opacity slider id, color input id]: drives the % readout next to each
  // slider and the swatch color (null = shadow, always black).
  const ALPHA_ROWS = [
    ["st-fill-a", "st-fill"],
    ["st-highlight-a", "st-highlight"],
    ["st-outline-a", "st-outline"],
    ["st-shadow-a", null],
    ["st-box-a", "st-box-color"],
  ];

  /**
   * Refreshes the readouts next to the style sliders (size, position, outline
   * width, opacities), their aria-valuetext, and each opacity swatch color.
   */
  function updateStyleReadouts() {
    $("st-size-val").textContent = `${(Number($("st-size").value) * 100).toFixed(1)} %`;
    $("st-posy-val").textContent = `${Math.round(Number($("st-posy").value) * 100)} %`;
    $("st-outline-w-val").textContent = Number($("st-outline-w").value).toFixed(2);
    $("st-size").setAttribute(
      "aria-valuetext",
      `${$("st-size-val").textContent} de la altura del vídeo`
    );
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

  /**
   * Disables controls that have no effect right now (highlight without
   * karaoke, shadow opacity without shadow, box color/opacity without box)
   * and dims their rows with .is-disabled.
   */
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

  // User-saved styles, stored in localStorage as {id: preset} with the same
  // shape as STYLE_PRESETS entries. Chip keys use the "custom:<id>" prefix.
  const CUSTOM_PRESETS_KEY = "autosubs-custom-presets";

  /**
   * Loads the user presets from localStorage.
   * @return {!Object<string, !Object>} id -> preset; {} when missing, corrupt
   *     or when storage is blocked.
   */
  function loadCustomPresets() {
    try {
      const raw = JSON.parse(localStorage.getItem(CUSTOM_PRESETS_KEY) || "{}");
      return raw && typeof raw === "object" ? raw : {};
    } catch (_) {
      return {};
    }
  }

  /**
   * Persists the user presets to localStorage.
   * @param {!Object<string, !Object>} map id -> preset.
   * @return {boolean} False if storage is unavailable or full.
   */
  function saveCustomPresets(map) {
    try {
      localStorage.setItem(CUSTOM_PRESETS_KEY, JSON.stringify(map));
      return true;
    } catch (_) {
      return false;
    }
  }

  /**
   * Resolves a chip key to its preset.
   * @param {?string} key Built-in id (e.g. "netflix") or "custom:<id>".
   * @return {?Object} The preset, or null if unknown.
   */
  function getPreset(key) {
    if (!key) return null;
    if (key.startsWith("custom:")) return loadCustomPresets()[key.slice(7)] || null;
    return STYLE_PRESETS[key] || null;
  }

  /**
   * Rebuilds the user preset chips, each with a "×" delete button (handled by
   * the delegated .style-chips listener in wireEvents), and refreshes the
   * preset status line.
   */
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

  /**
   * Saves the current style controls as a user preset and applies it. The id
   * is an accent-free slug of the name, so reusing a name overwrites it.
   * @param {string} name Display name; trimmed and capped at 28 characters.
   */
  function saveCurrentAsPreset(name) {
    const clean = String(name || "")
      .trim()
      .slice(0, 28);
    if (!clean) return;
    const st = readStyleFromUI();
    const id =
      clean
        .toLowerCase()
        .normalize("NFD")
        .replace(/[\u0300-\u036f]/g, "")
        .replace(/[^a-z0-9]+/g, "-")
        .replace(/^-|-$/g, "") || String(Date.now());
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

  // Approximate WCAG contrast between the text and whatever is behind it,
  // already blended with its opacity. It is evaluated over white and black
  // backdrops and the worst case wins, because the video content is unknown.
  // The math (parseHex, blend, luminance, contrastRatio, worstCaseContrast,
  // contrastLevel) lives in ui/app_helpers.js.

  /**
   * Updates the legibility badge of the style card. Checks, in order: almost
   * transparent fill (< 20 %), no outline and no box, then the worst-case
   * contrast of the fill against the box (or outline) over black and white.
   * Levels: < 3:1 warn, < 4.5:1 info (WCAG AA for normal text), else ok
   * (hidden by CSS). An outline counts if thickness > 0 and opacity >= 15 %.
   */
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
      const ratio = worstCaseContrast(st.fill_hex, st.fill_alpha, layerHex, layerA);
      const ref = st.box_enabled ? "la caja" : "el contorno";
      level = contrastLevel(ratio);
      if (level === "warn") {
        msg = `Legibilidad baja: contraste ${ratio.toFixed(1)}:1 con ${ref}.`;
      } else if (level === "info") {
        msg = `Contraste justo: ${ratio.toFixed(1)}:1 con ${ref} (recomendado ≥ 4.5:1).`;
      } else {
        msg = `Contraste ${ratio.toFixed(1)}:1 con ${ref}.`;
      }
    }
    el.textContent = msg;
    el.dataset.level = level;
  }

  // Fields that define a preset; used to detect whether the user modified it.
  const PRESET_KEYS = [
    "size",
    "pos_y",
    "fill_hex",
    "highlight_hex",
    "outline_hex",
    "outline_thickness",
    "shadow_enabled",
    "box_enabled",
    "box_hex",
    "fill_alpha",
    "highlight_alpha",
    "outline_alpha",
    "shadow_alpha",
    "box_alpha",
    "font_style",
    "animation_mode",
    "ascii_fold",
  ];

  /**
   * Fingerprint of the current style controls (numbers rounded to 3 decimals,
   * strings lower-cased, plus the selected font). Compared against
   * state.presetSignature to flag a preset as «modificado».
   * @return {string} JSON string.
   */
  function styleSignature() {
    const st = readStyleFromUI();
    return JSON.stringify(
      PRESET_KEYS.map((k) => {
        const v = st[k];
        return typeof v === "number"
          ? Number(v.toFixed(3))
          : typeof v === "string"
            ? v.toLowerCase()
            : v;
      }).concat([$("st-font").value])
    );
  }

  /**
   * Shows the active preset and whether it was modified; toggles the reset
   * link and every chip's active / modified / aria-pressed state.
   */
  function updatePresetStatus() {
    const key = state.activePreset;
    const preset = getPreset(key);
    const modified = Boolean(
      preset && state.presetSignature && styleSignature() !== state.presetSignature
    );
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

  /**
   * Warns when the selected Resolve push mode cannot carry the style: native
   * SRT subtitle tracks ignore colors and opacity, FCPXML only carries text
   * opacity. Hidden in standalone mode.
   */
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

  // ---------------------------------------------------------------------------
  // Style / preview caches
  // ---------------------------------------------------------------------------
  // renderPreviewAtMs() runs on every timeupdate and animation tick, so it must
  // not read ~25 controls or force layout every time.
  //   cachedStyle   Last readStyleFromUI() result. Cleared by
  //                 invalidateStyleCache() whenever a style control, preset,
  //                 font, animation setting, karaoke toggle, preference import
  //                 or session changes.
  //   cachedStageH  #preview-stage height in CSS px (caption font size is
  //                 relative to it). Cleared on window resize, by a
  //                 ResizeObserver on the stage, on aspect change and in
  //                 applySession().
  // Code that writes style inputs programmatically must call
  // invalidateStyleCache(), or the preview keeps showing the old style.

  /** @type {?Object} Memoized readStyleFromUI(); null means stale. */
  let cachedStyle = null;
  /** @type {number} Memoized stage height in CSS px; 0 means stale. */
  let cachedStageH = 0;

  /** Marks cachedStyle stale; the next getCachedStyle() re-reads the DOM. */
  function invalidateStyleCache() {
    cachedStyle = null;
  }

  /** Marks cachedStageH stale; call after any layout change of the stage. */
  function invalidateStageSizeCache() {
    cachedStageH = 0;
  }

  /**
   * Returns the memoized style, reading the controls only when stale.
   * @return {!Object} Same shape as readStyleFromUI().
   */
  function getCachedStyle() {
    if (!cachedStyle) cachedStyle = readStyleFromUI();
    return cachedStyle;
  }

  /**
   * Returns the preview stage height, measuring it only when stale.
   * @return {number} Height in CSS px. Falls back to 260 before layout; that
   *     fallback stays cached until the next invalidation.
   */
  function getCachedStageH() {
    if (!cachedStageH) cachedStageH = $("preview-stage").clientHeight || 260;
    return cachedStageH;
  }

  /**
   * Shared input/change handler of the style controls: invalidates the style
   * cache, refreshes dependent UI and re-renders the preview at the playhead.
   */
  function onStyleControlChanged() {
    invalidateStyleCache();
    syncStyleControlStates();
    updateStyleReadouts();
    updatePresetStatus();
    updateContrastBadge();
    renderPreviewAtMs((activeMedia().currentTime || 0) * 1000);
  }

  /**
   * Reads the full style from the style card. This exact object is sent as
   * `style` to /api/export, /api/export-video and /api/resolve/push, and it
   * feeds the live preview (through getCachedStyle()). Animated packs list
   * up to 5 font_variants (the Resolve template has TextPlus1..5); any other
   * font yields a single variant.
   * @return {!Object} font, font_style, size and pos_y (fractions of video
   *     height), *_hex colors, *_alpha (0..1), outline_thickness, shadow/box
   *     flags, uppercase, ascii_fold, animation_mode, anim_hold_frames (24 fps
   *     frames), anim_jitter, font_variants and karaoke_enabled.
   */
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

  // ---------------------------------------------------------------------------
  // Fonts: catalog, weights and animation controls
  // ---------------------------------------------------------------------------

  /**
   * Replaces <style id="dynamic-fonts-style"> with @font-face rules for the
   * user-uploaded fonts (served by the local server) so the preview can use
   * them.
   * @param {!Array<!Object>} customFaces Items with css_family and url.
   */
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

  /**
   * Fills #st-weight with the distinct styles of a pack's variants.
   * @param {?Object} pack Font pack; null falls back to Regular/Bold.
   * @param {string=} preferredStyle Style to keep if available (defaults to
   *     the current selection, else the first style).
   */
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

  /**
   * Shows/hides the animation parameters and contextual hint for the chosen
   * mode, and starts/stops the 80 ms timer (state.animTimer) that advances
   * state.animClockMs so boil/flipbook effects keep moving while paused.
   */
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
        hintEl.textContent = `Usando ${pack.variant_count} variantes en flipbook. Resolve usará TextPlus1..5 y 1 sola rama activa por frame.`;
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

  /**
   * Applies the font selected in #st-font: state.selectedPack, weight options,
   * missing-glyph warning, animation controls and preview.
   * @param {boolean=} autoConfigure True when the user picked the font or a
   *     pack was just uploaded: also toggles ASCII folding for fonts without
   *     Spanish glyphs, and switches animated packs to the animated Text+
   *     template (or turns flipbook modes off for regular fonts).
   * @param {string=} preferredStyle Weight to select if the pack has it.
   */
  function syncFontSelection(autoConfigure = false, preferredStyle = "") {
    invalidateStyleCache();
    const selVal = $("st-font").value;
    const pack = state.packByOptionValue[selVal] || null;
    state.selectedPack = pack;

    populateWeightSelect(pack, preferredStyle);

    const warnEl = $("font-glyph-warning");
    if (pack && pack.supports_spanish === false && (pack.missing_spanish_chars || []).length > 0) {
      const missingStr = pack.missing_spanish_chars.join(" ");
      warnEl.textContent = `Aviso en "${pack.family}": faltan glifos en español (${missingStr}). Activa «Adaptar glifos ASCII» para evitar huecos en Resolve.`;
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
        if (
          $("st-anim-mode").value === "font_cycle_boil" ||
          $("st-anim-mode").value === "font_cycle"
        ) {
          $("st-anim-mode").value = "none";
        }
      }
    }

    syncAnimControlsVisibility();
    renderPreviewAtMs((activeMedia().currentTime || 0) * 1000);
  }

  /**
   * Rebuilds the #st-font options from a fonts payload (from /api/status or a
   * font upload): animated packs, custom/macOS fonts and built-in fonts, each
   * in its own optgroup, and refreshes state.packByOptionValue.
   * @param {?Object} fontsData {packs, builtin_fonts, custom_faces}.
   * @param {string=} preferredPackId Pack id or family name to select (e.g.
   *     the pack just uploaded); when given, autoConfigure is enabled.
   */
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
        (p) =>
          p.pack_id === preferredPackId || p.family.toLowerCase() === preferredPackId.toLowerCase()
      );
      if (match) sel.value = match.pack_id;
    }
    syncFontSelection(Boolean(preferredPackId));
  }

  /**
   * Writes a preset into every style control and records it as the active
   * preset (state.activePreset / state.presetSignature) so later edits show
   * as «modificado». "anitype" also selects the first animated pack, forces
   * the Text+ push mode and enables ASCII folding only if that pack lacks
   * Spanish glyphs.
   * @param {string} key Built-in preset id or "custom:<id>".
   */
  function applyStylePreset(key) {
    invalidateStyleCache();
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
    } else if (
      state.packByOptionValue[p.font] ||
      Array.from($("st-font").options).some((o) => o.value === p.font)
    ) {
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
    $("st-ascii-fold").checked =
      key === "anitype" ? !selectedSupportsSpanish : Boolean(p.ascii_fold);
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

  // ---------------------------------------------------------------------------
  // Whisper model selection and session loading
  // ---------------------------------------------------------------------------

  /**
   * Fills #model-select with the known Whisper models in recommended order
   * (✓ installed, ↓ needs download), keeping the previous choice.
   * @param {!Object} env `environment` from /api/status.
   */
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

  /**
   * Shows accuracy/RAM/speed notes for the selected model and updates the
   * download button (disabled when already installed).
   */
  function updateModelHint() {
    if (!state.env) return;
    const m = state.env.models[$("model-select").value];
    if (!m) return;
    $("model-note").textContent = `${m.spanish_wer} · ${m.ram_mb} MB RAM · ${m.speed_note}`;
    $("btn-download-model").textContent = m.installed ? "Instalado" : `Descargar (${m.size_mb} MB)`;
    $("btn-download-model").disabled = m.installed;
  }

  /**
   * Loads a server session into the UI: cues, waveform peaks, duration, media
   * source and the format settings it was generated with. Resets the waveform
   * zoom and both preview caches, then re-renders the cue list, waveform and
   * preview at the first cue. The media src is reassigned only when media_rev
   * changes (or in demo mode) to avoid re-buffering the same file on every
   * refresh; `?v=<rev>` busts the browser cache when the file does change.
   * Also adopts the session's cues_rev as the base of the next cue save.
   * @param {!Object} session cues, cues_rev, peaks, duration_ms, is_video,
   *     can_burnin_video, media_url, media_rev, media_name, format_config and
   *     demo_mode.
   */
  function applySession(session) {
    state.cues = session.cues || [];
    state.cuesRev = cuesRevOf(session);
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
    invalidateStyleCache();
    invalidateStageSizeCache();
    renderCueList();
    drawWaveform(previewMs);
    renderPreviewAtMs(previewMs);
  }

  // ---------------------------------------------------------------------------
  // Hardware profile (server-detected: Intel iMac 2017 / Apple Silicon)
  // ---------------------------------------------------------------------------

  /**
   * Fills a thread-count select from threads_standalone down to 1, tagging
   * the maximum, the safe count while Resolve is open and the quiet
   * 2-thread option.
   * @param {!HTMLSelectElement} sel Select to fill (previous value kept).
   * @param {!Object} hw Hardware profile.
   */
  function fillThreadOptions(sel, hw) {
    const maxT = Number(hw.threads_standalone || 4);
    const resolveT = Number(hw.threads_with_resolve || 3);
    const prev = sel.value;
    sel.innerHTML = "";
    for (let n = maxT; n >= 1; n--) {
      const opt = document.createElement("option");
      opt.value = String(n);
      const tag =
        n === maxT
          ? " (máximo)"
          : n === resolveT
            ? " (con Resolve)"
            : n === 2
              ? " (silencioso)"
              : "";
      opt.textContent = `${n} hilo${n === 1 ? "" : "s"}${tag}`;
      sel.appendChild(opt);
    }
    if (prev) sel.value = prev;
  }

  /**
   * Writes the label of a "CPU only" checkbox, which maps to whisper.cpp's
   * `-ng` flag. On Intel iMacs it protects the 2 GB AMD VRAM that Resolve
   * needs; on Apple Silicon it disables Metal.
   * @param {!HTMLElement} lbl Label element (content replaced).
   * @param {!Object} hw Hardware profile.
   */
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

  /**
   * One-time setup of the hardware-dependent controls (card 2 and the
   * «Esta Mac» dialog) from the server-detected profile.
   * @param {!Object} hw environment.hardware_profile from /api/status.
   */
  function initHardwareControls(hw) {
    fillThreadOptions($("threads-select"), hw);
    fillThreadOptions($("mp-threads"), hw);
    setGpuLabel($("opt-no-gpu-label"), hw);
    setGpuLabel($("mp-no-gpu-label"), hw);
    $("hw-profile-note").textContent = hw.name || "";
  }

  // ---------------------------------------------------------------------------
  // Machine performance («Esta Mac»): settings stored per computer in the
  // server's settings.json, plus portable user preferences.
  // ---------------------------------------------------------------------------
  // Display labels of performance.mode values.
  const PERF_MODE_LABELS = {
    auto: "Automático",
    safe: "Seguro",
    max: "Máximo",
    custom: "Personalizado",
  };
  // Portable preferences mirrored to settings.json:
  // [preferences key, element id, how to read/write it: "value"|"number"|"checked"].
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

  /**
   * Fills the «Esta Mac» model select, marking the model the server
   * recommends for this machine.
   * @param {!HTMLSelectElement} sel Select to fill (previous value kept).
   * @param {!Object} env `environment` from /api/status.
   * @param {string} recommendedId Recommended model id.
   */
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

  /**
   * Renders the hardware summary (<dl>) of the «Esta Mac» dialog.
   * @param {!Object} hw Profile: name, cpu_description, ram_gb, gpu_name,
   *     gpu_vram_mb (MB) and force_no_gpu.
   */
  function renderMachineHardware(hw) {
    const dl = $("machine-hw");
    dl.textContent = "";
    const vram = hw.gpu_vram_mb ? ` · ${Math.round(hw.gpu_vram_mb / 1024)} GB VRAM` : "";
    [
      ["Equipo", hw.name],
      ["Procesador", hw.cpu_description],
      ["Memoria", hw.ram_gb ? `${hw.ram_gb} GB RAM` : "—"],
      [
        "GPU",
        `${hw.gpu_name || "—"}${vram}${hw.force_no_gpu ? " (Whisper usa solo CPU)" : " (Whisper usa Metal)"}`,
      ],
    ].forEach(([k, v]) => {
      const dt = document.createElement("dt");
      dt.textContent = k;
      const dd = document.createElement("dd");
      dd.textContent = v || "—";
      dl.append(dt, dd);
    });
  }

  /**
   * Lists previous benchmark results, newest first.
   * @param {?Object<string, !Object>} benchmarks Results keyed by model id;
   *     `at` is a Unix timestamp in seconds.
   */
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

  /**
   * Mirrors the server's effective performance settings into card 2 and the
   * «Esta Mac» dialog (both control sets stay in sync), and updates the mode
   * pill, summary line, hardware info and topbar badges.
   * @param {?Object} m /api/settings payload: settings, effective, profile,
   *     recommended_model_id and settings_path.
   * @param {{firstLoad: (boolean|undefined)}=} options firstLoad also copies
   *     the effective model into the main model select (startup only, so
   *     later refreshes do not override the user's current choice).
   */
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
    // Summary of what will be applied; the controls live in the «Esta Mac» dialog.
    $("hw-profile-note").textContent = [
      `${eff.threads} hilos`,
      eff.force_no_gpu ? "solo CPU" : "GPU",
      eff.fast_beam ? "rápido" : "precisión",
    ].join(" · ");
    renderMachineHardware(m.profile);
    renderBenchHistory(m.settings.benchmarks);
    $("machine-settings-path").textContent = m.settings_path || "";
    updateHardwareBadges();
  }

  /**
   * PUTs a partial settings patch and applies the merged result. Errors are
   * shown as a toast, never thrown.
   * @async
   * @param {!Object} patch e.g. {performance: {mode: "safe"}}.
   * @param {string=} toastMsg Confirmation toast on success ("" = silent).
   * @return {!Promise<void>}
   */
  async function saveMachineSettings(patch, toastMsg = "") {
    try {
      const m = await apiJson("/api/settings", { method: "PUT", body: JSON.stringify(patch) });
      applyMachinePayload(m);
      if (toastMsg) showToast(toastMsg);
    } catch (err) {
      showToast(`No se pudo guardar el ajuste: ${err.message}`);
    }
  }

  /**
   * Saves manual thread/beam/GPU choices. Any manual change turns the
   * performance profile into "custom" (Personalizado). Called with either
   * the card-2 controls or the dialog controls.
   * @param {!HTMLSelectElement} threadsSel Threads select.
   * @param {!HTMLSelectElement} beamSel Beam select ("fast" | "quality").
   * @param {!HTMLInputElement} gpuChk "CPU only" checkbox.
   */
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

  /**
   * Collects the portable preferences listed in PREF_FIELDS.
   * @return {!Object} preferences key -> value.
   */
  function readPreferencesFromUI() {
    const prefs = {};
    PREF_FIELDS.forEach(([key, id, kind]) => {
      const el = $(id);
      if (!el) return;
      prefs[key] =
        kind === "checked" ? el.checked : kind === "number" ? Number(el.value) : el.value;
    });
    return prefs;
  }

  /**
   * Writes saved preferences into the controls (null/undefined keys are
   * skipped). No change events are dispatched, so this does not trigger
   * schedulePreferencesSave().
   * @param {?Object} prefs Preferences object from settings.json.
   */
  function applyPreferencesToUI(prefs) {
    if (!prefs) return;
    PREF_FIELDS.forEach(([key, id, kind]) => {
      const el = $(id);
      if (!el || prefs[key] === undefined || prefs[key] === null) return;
      if (kind === "checked") el.checked = Boolean(prefs[key]);
      else el.value = String(prefs[key]);
    });
    invalidateStyleCache();
  }

  /**
   * Debounces (600 ms) a PUT of the current preferences. Failures are ignored:
   * preferences are a convenience, not session data.
   */
  function schedulePreferencesSave() {
    clearTimeout(state.prefsTimer);
    state.prefsTimer = setTimeout(() => {
      apiJson("/api/settings", {
        method: "PUT",
        body: JSON.stringify({ preferences: readPreferencesFromUI() }),
      }).catch(() => {});
    }, 600);
  }

  /**
   * Downloads autosubs-preferencias.json: the server export merged with the
   * current UI preferences and UI-only data (karaoke toggle, user presets
   * from localStorage), via a temporary Blob URL.
   * @async
   * @return {!Promise<void>}
   */
  async function exportPreferences() {
    try {
      const data = await apiJson("/api/settings/export");
      data.preferences = {
        ...data.preferences,
        ...readPreferencesFromUI(),
        karaoke_enabled: $("st-karaoke").checked,
        custom_style_presets: loadCustomPresets(),
      };
      downloadBlob(
        new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }),
        "autosubs-preferencias.json"
      );
    } catch (err) {
      showToast(`No se pudo exportar: ${err.message}`);
    }
  }

  /**
   * Imports a file produced by exportPreferences(): sends it to the server,
   * applies it to the UI, restores karaoke and merges user presets (same ids
   * are overwritten). Per-machine performance settings are not changed. The
   * file input is always cleared so the same file can be chosen again.
   * @async
   * @param {?File} file Selected JSON file.
   * @return {!Promise<void>}
   */
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

  /**
   * Runs the Whisper speed benchmark with the dialog's model: starts a job,
   * polls it every 700 ms through pollJob() without taking over the main job
   * box (progress goes to #bench-result), shows the realtime factor and
   * offers to apply the model the server recommends for this machine.
   * @async
   * @return {!Promise<void>}
   */
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
      pollJob(
        res.job_id,
        async (job) => {
          btn.disabled = false;
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
          try {
            const m = await apiJson("/api/settings");
            applyMachinePayload(m);
          } catch (err) {
            box.textContent = `Error consultando la prueba: ${err.message}`;
          }
        },
        {
          showInBox: false,
          intervalMs: 700,
          onProgress: (job) => {
            box.textContent = `${job.stage || "Procesando..."} (${job.progress || 0}%)`;
          },
          onError: (job, err) => {
            btn.disabled = false;
            box.textContent = err
              ? `Error consultando la prueba: ${err.message}`
              : job.error || job.stage;
          },
        }
      );
    } catch (err) {
      btn.disabled = false;
      box.textContent = err.message;
    }
  }

  /**
   * Wires the «Esta Mac» dialog and the card-2 performance controls: mode
   * radios, manual overrides, model and voice-enhance toggles, reset,
   * benchmark, preferences import/export and debounced preference saving.
   * Adds fallbacks for WebKit builds without Invoker Commands or closedby.
   */
  function wireMachinePanel() {
    // Fallback for WebKit without Invoker Commands (command / commandfor).
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
    // Fallback for closedby="any" (close on outside click) in Safari/WebKit.
    if (!("closedBy" in HTMLDialogElement.prototype)) {
      // True when the pointer is outside the dialog box (i.e. on the backdrop).
      const outside = (e) => {
        const r = dialog.getBoundingClientRect();
        return (
          e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom
        );
      };
      // Both press and release must be outside, so a text selection that ends on
      // the backdrop does not close the dialog.
      let pressedOutside = false;
      dialog.addEventListener("pointerdown", (e) => {
        pressedOutside = outside(e);
      });
      dialog.addEventListener("pointerup", (e) => {
        if (pressedOutside && outside(e)) dialog.close();
      });
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
      $(id).addEventListener("change", () =>
        saveCustomPerformance($("mp-threads"), $("mp-beam"), $("mp-no-gpu"))
      )
    );
    ["threads-select", "beam-mode", "opt-no-gpu"].forEach((id) =>
      $(id).addEventListener("change", () =>
        saveCustomPerformance($("threads-select"), $("beam-mode"), $("opt-no-gpu"))
      )
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
    $("prefs-import-input").addEventListener("change", (ev) =>
      importPreferences(ev.target.files[0])
    );

    PREF_FIELDS.forEach(([, id]) => {
      const el = $(id);
      if (el) el.addEventListener("change", schedulePreferencesSave);
    });
    $("cfg-glossary").addEventListener("input", schedulePreferencesSave);
  }

  /**
   * Updates the topbar CPU/GPU status from the hardware profile and the
   * card-2 thread / "CPU only" choices. On Intel an active GPU is flagged
   * (pill-warn) because it competes with Resolve for VRAM.
   */
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

  // ---------------------------------------------------------------------------
  // Server status
  // ---------------------------------------------------------------------------

  /**
   * Fetches /api/status and syncs the UI with it: models, fonts, app mode
   * (auto-selected until the user picks one), Resolve badge, hardware controls
   * (first call only), machine settings and saved preferences, Resolve
   * timeline info, and the session when it changed server-side (new media_rev,
   * or server cues while none are loaded). Runs at startup, from the
   * refresh / inbox buttons and model download, after a cue-save conflict,
   * and when the window regains focus (refreshOnReturn, throttled); there is
   * no periodic polling.
   * @async
   * @return {!Promise<void>} Errors are shown as a toast.
   */
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
        // The initial preset is applied before fonts are known, when the
        // "Semibold" weight does not exist yet; re-apply it if the user has
        // not touched the style since.
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
        "pill " +
        (bridgeMode === "studio_direct" || bridgeMode === "lua_bridge_ready"
          ? "pill-neutral"
          : "pill-warn");

      const hw = data.environment.hardware_profile || {};
      const firstLoad = !state.hwProfile;
      if (firstLoad) {
        state.hwProfile = hw;
        initHardwareControls(hw);
      }
      applyMachinePayload(data.machine, { firstLoad });
      if (firstLoad && data.machine) {
        // Preferences saved on this Mac (an active session is applied afterwards).
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
        // When a session is restored at startup, the selects show the saved
        // preferences (what the user picked last), not the configuration that
        // session was generated with.
        if (firstLoad && data.machine) applyPreferencesToUI(data.machine.settings.preferences);
      }
    } catch (err) {
      showToast(`Error leyendo estado: ${err.message}`);
    }
  }

  // Minimum gap (ms) between focus-triggered refreshes: /api/status probes
  // tools and processes server-side, and focus + visibilitychange usually
  // fire together when the user comes back to the window.
  const FOCUS_REFRESH_MIN_MS = 5000;
  // Date.now() of the last focus-triggered refresh. Starts at load time
  // because startup already calls refreshStatus() (and a focus event can
  // fire right after load).
  let lastFocusRefreshMs = Date.now();

  /**
   * Re-syncs the session when the user returns to the window, so changes
   * made meanwhile from DaVinci Resolve scripts (new inbox audio, timeline
   * info) show up without pressing «refresh». /api/status is the only read
   * endpoint that returns the session. Skipped while the page is hidden,
   * while any job is polled (its onSuccess will refresh what it needs), and
   * when the last such refresh was under FOCUS_REFRESH_MIN_MS ago.
   */
  function refreshOnReturn() {
    if (document.visibilityState === "hidden") return;
    if (jobPollers.size > 0) return;
    const now = Date.now();
    if (now - lastFocusRefreshMs < FOCUS_REFRESH_MIN_MS) return;
    lastFocusRefreshMs = now;
    refreshStatus();
  }

  // ---------------------------------------------------------------------------
  // Cue editor: list, edits and save sequencing
  // ---------------------------------------------------------------------------

  /**
   * Updates the editor stats (cue count, mean CPS, warnings) as plain text;
   * the warnings count is colored only when non-zero.
   */
  function updateEditorStats() {
    const cues = state.cues;
    const avgCps = cues.length ? cues.reduce((acc, c) => acc + (c.cps || 0), 0) / cues.length : 0;
    const warnsCount = cues.filter((c) => (c.warnings || []).length > 0).length;
    $("stat-cues").textContent = `${cues.length} subtítulos`;
    $("stat-cps").textContent = `${avgCps.toFixed(1)} CPS medio`;
    $("stat-warns").textContent = warnsCount === 1 ? "1 aviso" : `${warnsCount} avisos`;
    $("stat-warns").className = "stat" + (warnsCount > 0 ? " stat-warn" : "");
  }

  /**
   * Rebuilds the whole cue list from state.cues. Used after structural
   * changes (load, split, merge, delete, drag end), not on every keystroke.
   * Each row has start/end ms inputs, a readable timecode, a CPS badge, cue
   * tools and a text area. Handlers close over the cue object, which is why
   * server responses update cues in place (see saveCuesToServer).
   */
  function renderCueList() {
    const list = $("cue-list");
    const cues = state.cues;

    updateEditorStats();
    if (cues.length === 0) {
      list.innerHTML =
        '<div class="empty-state">Todavía no hay subtítulos. Exporta audio desde DaVinci Resolve 20, sube un archivo o prueba el Modo Demo ES.</div>';
      return;
    }

    list.innerHTML = "";
    cues.forEach((cue, idx) => {
      const card = document.createElement("div");
      card.className = "cue-item" + (idx === state.activeCueIndex ? " active" : "");
      card.dataset.index = String(idx);

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
      // Time edits rescale word timings so karaoke stays in sync.
      startIn.addEventListener("change", () => {
        const oldStart = cue.start_ms;
        cue.start_ms = Math.max(0, Number(startIn.value || 0));
        cue.words = rescaleWords(cue.words, oldStart, cue.end_ms, cue.start_ms, cue.end_ms);
        updateCueRowTimes(idx);
        invalidateWaveCache();
        drawWaveform(currentMs());
        scheduleSaveCues();
      });

      const endIn = document.createElement("input");
      endIn.className = "cue-end";
      endIn.type = "number";
      endIn.step = "10";
      endIn.value = String(cue.end_ms);
      endIn.title = "Fin en milisegundos";
      // The end is kept at least 80 ms after the start.
      endIn.addEventListener("change", () => {
        const oldEnd = cue.end_ms;
        cue.end_ms = Math.max(cue.start_ms + 80, Number(endIn.value || cue.start_ms + 500));
        cue.words = rescaleWords(cue.words, cue.start_ms, oldEnd, cue.start_ms, cue.end_ms);
        updateCueRowTimes(idx);
        invalidateWaveCache();
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
      cpsBadge.className = cpsClassName(cue.cps);
      cpsBadge.textContent = `${Number(cue.cps || 0).toFixed(1)} CPS`;
      actions.appendChild(cpsBadge);

      const btnPlayCue = makeMiniBtn("▶", "Escuchar este subtítulo", () =>
        seekAndPlay(cue.start_ms, cue.end_ms)
      );
      const btnSplit = makeMiniBtn("÷", "Dividir en dos subtítulos", () => splitCue(idx));
      const btnMerge = makeMiniBtn("+Unir", "Unir con el siguiente", () => mergeCue(idx));
      const btnDel = makeMiniBtn("×", "Eliminar subtítulo", () => deleteCue(idx));

      // Tools dimmed until the row is hovered or focused (still in the tab order).
      const tools = document.createElement("span");
      tools.className = "cue-tools";
      const btnJump = makeMiniBtn("Resolve", "Mover cabezal en DaVinci Resolve", () =>
        jumpResolve(cue.start_ms)
      );
      btnJump.classList.add("cue-btn-resolve");
      tools.append(btnPlayCue, btnJump, btnSplit, btnMerge, btnDel);
      actions.appendChild(tools);
      head.append(times, actions);

      const textarea = document.createElement("textarea");
      textarea.className = "cue-text";
      textarea.rows = Math.max(1, (cue.lines || []).length);
      textarea.value = cue.text || "";
      textarea.addEventListener("focus", () => {
        setActiveCueIndex(idx);
        ensureMsVisible(cue.start_ms);
        drawWaveform(currentMs());
        renderPreviewAtMs(cue.start_ms + 10);
      });
      textarea.addEventListener("input", () => {
        cue.text = textarea.value;
        cue.lines = textarea.value
          .split("\n")
          .map((s) => s.trim())
          .filter(Boolean);
        syncCueWordsAfterTextEdit(cue);
        invalidateWaveCache();
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

  /**
   * Creates a small ghost button for cue rows.
   * @param {string} label Visible text.
   * @param {string} title Tooltip and aria-label.
   * @param {function(!Event)} onClick Click handler.
   * @return {!HTMLButtonElement}
   */
  function makeMiniBtn(label, title, onClick) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "btn btn-ghost btn-sm";
    b.textContent = label;
    b.title = title;
    b.setAttribute("aria-label", title);
    b.addEventListener("click", onClick);
    return b;
  }

  // Save sequencing: every local edit calls scheduleSaveCues(), which bumps
  // state.editSeq and debounces a PUT by 350 ms. saveCuesToServer() bumps it
  // again and remembers its own value; the response is applied only if
  // editSeq is unchanged, i.e. no edit happened while the request was in
  // flight. Otherwise the stale response is dropped and the pending debounced
  // save sends the newer cues.
  //
  // Saves are serialized: the server bumps cues_rev on every successful PUT
  // and rejects a PUT carrying an older one (409), so two overlapping PUTs
  // would make the second fail. A save requested while another is in flight
  // waits for it, then sends the latest cues with the cues_rev of that reply;
  // of several waiting saves only the newest one is sent.

  /** Marks cues dirty and (re)starts the 350 ms save debounce. */
  function scheduleSaveCues() {
    state.editSeq += 1;
    clearTimeout(state.saveTimer);
    state.saveTimer = setTimeout(saveCuesToServer, 350);
  }

  /**
   * PUTs state.cues to /api/cues now (cancelling a pending debounce), after
   * any save already in flight. The server recomputes CPS, warnings and word
   * alignment; its version is merged back only if no newer edit exists. With
   * the same cue count, objects are updated in place so open text areas keep
   * their references and caret; otherwise the array is replaced and the list
   * re-rendered.
   *
   * The body carries the media_rev and cues_rev the cues were edited
   * against. If the Resolve script replaced the inbox audio, or the cues
   * were replaced server-side (another tab, a transcription...), the server
   * answers 409: the save is dropped (never retried) and the current session
   * is loaded instead (see handleCuesConflict()).
   * @async
   * @return {!Promise<boolean>} True only when the server accepted these
   *     cues and its reply was merged back. False when the save was
   *     superseded (a newer save, edit or session took over; while waiting
   *     no request is sent), on a 409 conflict, or on an error (shown as a
   *     toast), so callers can skip their own success toast.
   */
  async function saveCuesToServer() {
    clearTimeout(state.saveTimer);
    state.saveTimer = null;
    state.editSeq += 1;
    const seq = state.editSeq;
    while (state.cuesSaveInFlight) {
      await state.cuesSaveInFlight;
    }
    // A newer save or edit (which schedules its own save), or a conflict
    // reload, happened while waiting: nothing left for this call to send.
    if (seq !== state.editSeq) return false;

    let applied = false;
    const baseRev = state.cuesRev;
    // Released in `finally`, i.e. after a 409 has bumped editSeq, so waiting
    // saves wake up superseded and send nothing.
    let release = () => {};
    state.cuesSaveInFlight = new Promise((resolve) => (release = resolve));
    try {
      const data = await apiJson("/api/cues", {
        method: "PUT",
        body: JSON.stringify(buildCuesPutBody(state.cues, state.mediaRev, baseRev)),
      });
      // A session applied while the request was in flight (demo, resegment,
      // transcription...) carries newer cues and cues_rev than this reply.
      const superseded = state.cuesRev !== baseRev;
      // The server accepted these cues, so its new cues_rev is the base of
      // the next save even when the reply is stale and dropped below.
      if (data.session && !superseded) state.cuesRev = cuesRevOf(data.session);
      // Ignore stale responses if there were edits while the request was in flight.
      if (data.session && !superseded && seq === state.editSeq) {
        const serverCues = data.session.cues || [];
        if (serverCues.length === state.cues.length) {
          // Update in place: open textareas still point at these objects.
          serverCues.forEach((c, i) => Object.assign(state.cues[i], c));
          invalidateWaveCache();
        } else {
          state.cues = serverCues;
          renderCueList();
        }
        updateEditorStats();
        drawWaveform((activeMedia().currentTime || 0) * 1000);
        applied = true;
      }
    } catch (err) {
      if (err.status === 409) {
        await handleCuesConflict(err.data);
        return false;
      }
      showToast(`No se pudo guardar: ${err.message}`);
    } finally {
      state.cuesSaveInFlight = null;
      release();
    }
    return applied;
  }

  /**
   * Recovers from a 409 on PUT /api/cues: the media was replaced server-side
   * (e.g. by the Resolve export script; media_rev mismatch) or the cues were
   * replaced (cues_rev mismatch). Drops any pending debounced save of the
   * stale cues, loads the server session (which also adopts its cues_rev)
   * and tells the user why their view changed. The 409 session has no peaks
   * (applySession() would blank the waveform): with the same media_rev the
   * current peaks still match and are kept; otherwise refreshStatus()
   * fetches the full session, which it applies because media_rev changed.
   * @async
   * @param {?Object} data Parsed 409 body; may hold the current `session`.
   * @return {!Promise<void>}
   */
  async function handleCuesConflict(data) {
    clearTimeout(state.saveTimer);
    state.saveTimer = null;
    state.editSeq += 1;
    const session = data && data.session;
    if (session && Array.isArray(session.peaks)) {
      applySession(session);
    } else if (session && session.media_rev === state.mediaRev) {
      applySession({ ...session, peaks: state.peaks });
    } else {
      await refreshStatus();
    }
    showToast(
      "Los subtítulos o el audio cambiaron en el servidor (p. ej., desde DaVinci Resolve " +
        "u otra pestaña). Se cargó la sesión actual; tus últimos cambios no se guardaron.",
      6500
    );
  }

  /**
   * Splits a cue at its middle word: at that word's start when per-word
   * timings exist, else at the time midpoint; the cut stays >= 80 ms from both
   * edges. Saves, then re-renders the list.
   * @async
   * @param {number} idx Cue index; cues with fewer than 2 words are ignored.
   * @return {!Promise<void>}
   */
  async function splitCue(idx) {
    const cue = state.cues[idx];
    if (!cue) return;
    const words = (cue.text || "").trim().split(/\s+/);
    if (words.length < 2) return;
    const midWord = Math.floor(words.length / 2);
    // With per-word timings, cut exactly where the middle word starts.
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

  /**
   * Merges a cue with the next one (one line each, word timings concatenated).
   * @async
   * @param {number} idx Cue index; the last cue is ignored.
   * @return {!Promise<void>}
   */
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

  /**
   * Deletes a cue, saves and re-renders the list.
   * @async
   * @param {number} idx Cue index.
   * @return {!Promise<void>}
   */
  async function deleteCue(idx) {
    state.cues.splice(idx, 1);
    await saveCuesToServer();
    renderCueList();
  }

  // ---------------------------------------------------------------------------
  // Live preview and theme
  // ---------------------------------------------------------------------------

  /**
   * Renders the caption overlay of the monitor at a given time. Hot path: runs
   * on every timeupdate, animation tick (80 ms) and style change, so it reads
   * the cached style and stage height. Approximates the exporters: font size
   * is a fraction of the stage height, the outline is a real stroke under the
   * fill, karaoke highlights the word being spoken, and flipbook/boil steps
   * every anim_hold_frames frames at 24 fps.
   * @param {number} ms Time in ms. If no cue spans it, the active cue is shown
   *     so the cue being edited stays visible while paused elsewhere.
   */
  function renderPreviewAtMs(ms) {
    const cap = $("preview-caption");
    const st = getCachedStyle();
    const pack = state.selectedPack;

    const active = findPreviewCue(state.cues, ms, state.activeCueIndex);

    // Animation step: playing media drives the clock; while paused the synthetic
    // animClockMs keeps boil/flipbook moving. 1000 / 24 ms = one 24 fps frame.
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

    // Weight is guessed from the style name (Extra* 800, *Bold 700, else 500).
    // 1.15 is an empirical size factor; 12 px keeps tiny stages legible.
    cap.style.fontFamily = resolvedFamily;
    cap.style.fontWeight = st.font_style.toLowerCase().includes("extra")
      ? "800"
      : st.font_style.toLowerCase().includes("bold")
        ? "700"
        : "500";
    const stageH = getCachedStageH();
    cap.style.fontSize = `${Math.max(12, Math.round(st.size * stageH * 1.15))}px`;
    cap.style.bottom = `${Math.round(st.pos_y * 100)}%`;
    cap.style.color = hexToRgba(st.fill_hex, st.fill_alpha);
    cap.style.background = st.box_enabled ? hexToRgba(st.box_hex, st.box_alpha) : "transparent";

    if (
      (st.animation_mode === "boil" || st.animation_mode === "font_cycle_boil") &&
      st.anim_jitter > 0
    ) {
      // Deterministic wobble per step (unrelated sine frequencies) scaled by
      // jitter: offsets in px, rotation in degrees, and a tiny scale change.
      const j = Number(st.anim_jitter || 0);
      const dx = (Math.sin(step * 2.13) * 1.8 * j).toFixed(2);
      const dy = (Math.cos(step * 1.67) * 2.4 * j).toFixed(2);
      const rot = (Math.sin(step * 1.49) * 0.75 * j).toFixed(2);
      const sc = (1 + Math.sin(step * 2.71) * 0.012 * j).toFixed(4);
      cap.style.transform = `translate(${dx}px, ${dy}px) rotate(${rot}deg) scale(${sc})`;
    } else {
      cap.style.transform = "none";
    }

    // Real outline (stroke) painted under the fill: with text at 0 % opacity
    // the letters are hollow, as in .ASS and Text+. The width is doubled
    // because the fill covers the inner half of the stroke.
    const ol = Math.max(0, Math.round(st.outline_thickness * 18));
    cap.style.webkitTextStroke =
      ol > 0 ? `${ol * 2}px ${hexToRgba(st.outline_hex, st.outline_alpha)}` : "0";
    cap.style.paintOrder = "stroke fill";
    cap.style.textShadow = st.shadow_enabled
      ? `0 3px 8px ${hexToRgba("#000000", st.shadow_alpha)}`
      : "none";

    // Applies ASCII folding and uppercase the same way the exporters do.
    const transformToken = (txt) => transformPreviewText(txt, st);

    if (!active) {
      const emptyMsg = state.cues.length
        ? ""
        : "Carga un audio o pulsa «Probar Demo ES» para comenzar.";
      cap.textContent = emptyMsg ? transformToken(emptyMsg) : "";
      return;
    }

    if (
      st.karaoke_enabled &&
      active.words &&
      active.words.length > 0 &&
      ms >= active.start_ms &&
      ms <= active.end_ms
    ) {
      cap.innerHTML = "";
      let wIdx = 0;
      (active.lines || [active.text]).forEach((line, lIdx) => {
        if (lIdx > 0) cap.appendChild(document.createElement("br"));
        line
          .trim()
          .split(/\s+/)
          .filter(Boolean)
          .forEach((tok, tIdx) => {
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

  /**
   * Resolves the effective color scheme: explicit meta color-scheme value,
   * then the data-theme attribute, then the OS preference.
   * @return {string} "light" or "dark".
   */
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

  /**
   * Applies a theme through <meta name="color-scheme"> and data-theme (the CSS
   * keys off both). Only an explicit choice that differs from the OS setting
   * is stored ("autosubs-theme" in localStorage), so the app otherwise keeps
   * following the system. Redraws the waveform because its canvas colors
   * depend on the theme (part of the waveCache key).
   * @param {?string} theme "light", "dark", or "system"/null for automatic.
   * @param {boolean=} persist Whether to update localStorage (false for the
   *     initial load and for changes coming from another tab or the OS).
   */
  function applyTheme(theme, persist = true) {
    const meta = document.querySelector('meta[name="color-scheme"]');
    const systemPref =
      window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches
        ? "light"
        : "dark";

    if (theme === "system" || !theme) {
      if (persist) {
        try {
          localStorage.removeItem("autosubs-theme");
        } catch (_) {}
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
      btn.setAttribute(
        "aria-label",
        active === "dark" ? "Cambiar a tema claro" : "Cambiar a tema oscuro"
      );
      btn.title = active === "dark" ? "Cambiar a tema claro" : "Cambiar a tema oscuro";
    }

    if (typeof drawWaveform === "function") {
      // Token values changed with the theme; re-read them on the next draw.
      invalidateWaveColors();
      const mediaEl = activeMedia();
      drawWaveform(mediaEl ? (mediaEl.currentTime || 0) * 1000 : 0);
    }
  }

  /** Flips between light and dark (persisted) and confirms with a toast. */
  function toggleTheme() {
    const current = getCurrentScheme();
    const target = current === "dark" ? "light" : "dark";
    applyTheme(target, true);
    showToast(`Tema ${target === "light" ? "claro" : "oscuro"} activado`);
  }

  // ---------------------------------------------------------------------------
  // Waveform: time ruler, zoom/scroll and cue dragging
  // ---------------------------------------------------------------------------
  // Coordinates: times are ms on the media timeline; x/y are CSS px relative
  // to the canvas box. The visible window comes from viewRange() and
  // msToX()/xToMs() convert between both. Layout from the top: time ruler
  // (WAVE_RULER_H), audio peaks, then the draggable cue lane (WAVE_LANE_H).
  // Layout in CSS px: ruler height, cue lane height, edge grab tolerance.
  const WAVE_RULER_H = 14;
  const WAVE_LANE_H = 26;
  const WAVE_EDGE_PX = 8;
  // Shortest cue a drag can produce, and the narrowest zoom window (ms).
  const MIN_CUE_MS = 200;
  const MIN_VIEW_MS = 2000;

  /**
   * Playhead of the active media element.
   * @return {number} Time in ms.
   */
  function currentMs() {
    return (activeMedia().currentTime || 0) * 1000;
  }

  /**
   * Timeline length for the waveform: server duration, else media metadata,
   * else the end of the last cue; never below 1 s.
   * @return {number} Duration in ms.
   */
  function totalDurMs() {
    const m = activeMedia();
    const lastEnd = state.cues.length ? state.cues[state.cues.length - 1].end_ms : 0;
    return Math.max(1000, state.durationMs || (m.duration ? m.duration * 1000 : 0) || lastEnd);
  }

  /**
   * Resolves state.view into a clamped visible window.
   * @return {{start: number, span: number, dur: number}} All in ms.
   */
  function viewRange() {
    return computeViewRange(totalDurMs(), state.view);
  }

  /**
   * Sets the waveform window, clamped to [MIN_VIEW_MS, duration]; a span
   * covering the whole duration is stored as 0 ("fit"). Redraws.
   * @param {number} startMs Window start, ms.
   * @param {number} spanMs Window length, ms (Infinity = fit all).
   */
  function setView(startMs, spanMs) {
    const dur = totalDurMs();
    const span = Math.max(MIN_VIEW_MS, Math.min(spanMs, dur));
    state.view.spanMs = span >= dur ? 0 : span;
    state.view.startMs = Math.max(0, Math.min(startMs, dur - span));
    updateZoomReadout();
    drawWaveform(currentMs());
  }

  /**
   * Zooms while keeping `centerMs` at the same x position.
   * @param {number} centerMs Anchor time, ms.
   * @param {number} factor Span multiplier (< 1 zooms in), clamped to 0.25..4.
   */
  function zoomAround(centerMs, factor) {
    const { start, span } = viewRange();
    const f = Math.max(0.25, Math.min(4, factor));
    const frac = (centerMs - start) / span;
    setView(centerMs - frac * span * f, span * f);
  }

  /** Shows the visible span ("Todo", "1.5 min", "8.0 s") next to the zoom buttons. */
  function updateZoomReadout() {
    const el = $("zoom-readout");
    if (!el) return;
    const { span, dur } = viewRange();
    el.textContent =
      span >= dur
        ? "Todo"
        : span >= 60000
          ? `${(span / 60000).toFixed(1)} min`
          : `${(span / 1000).toFixed(span < 10000 ? 1 : 0)} s`;
  }

  /**
   * Scrolls the zoomed waveform so `ms` is visible (no-op when fitting all).
   * @param {number} ms Time to reveal.
   * @param {number=} pad Fraction of the span left before `ms` after a scroll.
   */
  function ensureMsVisible(ms, pad = 0.1) {
    if (state.view.spanMs <= 0) return;
    const { start, span } = viewRange();
    if (ms < start || ms > start + span) setView(ms - span * pad, span);
  }

  /**
   * Measures the waveform canvas box (reads layout).
   * @return {{canvas: !HTMLCanvasElement, w: number, h: number}} Size in CSS
   *     px, with 960 x 96 fallbacks before layout.
   */
  function waveSize() {
    const canvas = $("waveform-canvas");
    const rect = canvas.getBoundingClientRect();
    return { canvas, w: rect.width || 960, h: rect.height || 96 };
  }

  // msToX(ms, w, vr), xToMs(x, w, vr) and formatTick(ms, step) come from
  // ui/app_helpers.js; vr is the window returned by viewRange().

  // Offscreen render cache of the waveform. Everything except the playhead
  // (ruler, cue regions, peaks, cue lane) is drawn into waveCache.canvas only
  // when one of the recorded keys changes; otherwise drawWaveform() just blits
  // it and paints the playhead, which keeps per-timeupdate redraws cheap.
  // Keys: device-px size, devicePixelRatio, theme and its --waveform-* colors,
  // visible window, peaks and cues array identity, durationMs, active cue,
  // dragged cue and cuesVersion.
  // In-place cue edits keep the array identity, so they must call
  // invalidateWaveCache() to bump cuesVersion.
  const waveCache = {
    // Sentinel initial values never match real state, so the first draw renders.
    canvas: null,
    pw: 0,
    ph: 0,
    dpr: 0,
    isLight: null,
    // Theme colors the layer was rendered with (see getWaveColors()).
    bg: null,
    peak: null,
    start: -1,
    end: -1,
    peaksRef: null,
    durationMs: -1,
    cuesRef: null,
    cuesVersion: 0,
    renderedVersion: -1,
    activeCueIndex: -2,
    dragIdx: -2,
  };

  /**
   * Memoized --waveform-bg / --waveform-peak values for one color scheme.
   * Reading computed styles forces a style recalc, so it is done once per
   * scheme instead of on every timeupdate redraw. Null means "re-read".
   * @type {?{scheme: string, bg: string, peak: string}}
   */
  let waveColors = null;

  /** Drops the memoized waveform colors; call after any theme change. */
  function invalidateWaveColors() {
    waveColors = null;
  }

  /**
   * Returns the waveform background and peak colors from the CSS theme
   * tokens (canvas cannot use var() directly). Falls back to the previous
   * hard-coded values if a token is missing, so the look never breaks.
   * @param {boolean} isLight Whether the light scheme is active (also the
   *     cache key, so a scheme change re-reads the tokens by itself).
   * @return {{bg: string, peak: string}} CSS color strings.
   */
  function getWaveColors(isLight) {
    const scheme = isLight ? "light" : "dark";
    if (!waveColors || waveColors.scheme !== scheme) {
      const cs = getComputedStyle(document.documentElement);
      waveColors = {
        scheme,
        bg: cs.getPropertyValue("--waveform-bg").trim() || (isLight ? "#f0f0f3" : "#141417"),
        peak: cs.getPropertyValue("--waveform-peak").trim() || (isLight ? "#94949e" : "#555562"),
      };
    }
    return waveColors;
  }

  /**
   * Forces the next drawWaveform() to re-render the offscreen layer (needed
   * after in-place cue edits).
   */
  function invalidateWaveCache() {
    waveCache.cuesVersion += 1;
  }

  /**
   * Draws the static waveform layer into the offscreen canvas: background,
   * time ruler (tick step chosen so labels are >= 70 px apart), translucent
   * cue regions, audio peaks (max of the peaks under each px column) and the
   * cue lane with grab handles and clipped labels. Background and peak
   * colors come from the --waveform-* theme tokens (see getWaveColors());
   * the remaining colors are still hard-coded per theme.
   * @param {!CanvasRenderingContext2D} offCtx Offscreen context.
   * @param {number} w Width in CSS px.
   * @param {number} h Height in CSS px.
   * @param {number} dpr devicePixelRatio (drawing is scaled to device px).
   * @param {{start: number, span: number}} vr Visible window, ms.
   * @param {boolean} isLight Use light-theme colors.
   * @param {{bg: string, peak: string}} colors Theme token colors.
   */
  function renderWaveformBackground(offCtx, w, h, dpr, vr, isLight, colors) {
    offCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
    offCtx.fillStyle = colors.bg;
    offCtx.fillRect(0, 0, w, h);

    const laneTop = h - WAVE_LANE_H;
    const waveTop = WAVE_RULER_H;
    const waveBottom = laneTop - 2;
    const waveMid = (waveTop + waveBottom) / 2;
    const waveHalf = (waveBottom - waveTop) / 2;
    const mutedText = isLight ? "#71717a" : "#92929c";

    // Time ruler
    const steps = [
      100, 250, 500, 1000, 2000, 5000, 10000, 15000, 30000, 60000, 120000, 300000, 600000,
    ];
    const step = steps.find((st) => (st / vr.span) * w >= 70) || 600000;
    offCtx.fillStyle = mutedText;
    offCtx.font = "10px -apple-system, BlinkMacSystemFont, sans-serif";
    offCtx.textBaseline = "top";
    for (let t = Math.ceil(vr.start / step) * step; t <= vr.start + vr.span; t += step) {
      const x = Math.round(msToX(t, w, vr));
      offCtx.fillRect(x, 0, 1, 4);
      offCtx.fillText(formatTick(t, step), x + 3, 2);
    }

    // Cue regions over the waveform
    state.cues.forEach((cue, idx) => {
      const x1 = msToX(cue.start_ms, w, vr);
      const x2 = msToX(cue.end_ms, w, vr);
      if (x2 < 0 || x1 > w) return;
      offCtx.fillStyle =
        idx === state.activeCueIndex ? "rgba(131,103,199,0.16)" : "rgba(131,103,199,0.06)";
      offCtx.fillRect(x1, waveTop, Math.max(1, x2 - x1), waveBottom - waveTop);
    });

    // Audio peaks (max per pixel column within the visible window)
    const peaks = state.peaks || [];
    if (peaks.length > 0 && state.durationMs > 0) {
      const n = peaks.length;
      const perMs = n / state.durationMs;
      offCtx.fillStyle = colors.peak;
      for (let x = 0; x < w; x++) {
        const i0 = Math.max(0, Math.floor(xToMs(x, w, vr) * perMs));
        const i1 = Math.min(n, Math.max(i0 + 1, Math.floor(xToMs(x + 1, w, vr) * perMs)));
        let pk = 0;
        for (let i = i0; i < i1; i++) if (peaks[i] > pk) pk = peaks[i];
        const amp = Math.max(0.5, pk * waveHalf);
        offCtx.fillRect(x, waveMid - amp, 1, amp * 2);
      }
    }

    // Lane of draggable cue blocks
    offCtx.textBaseline = "middle";
    state.cues.forEach((cue, idx) => {
      const x1 = msToX(cue.start_ms, w, vr);
      const x2 = msToX(cue.end_ms, w, vr);
      if (x2 < 0 || x1 > w) return;
      const active = idx === state.activeCueIndex || (state.drag && state.drag.idx === idx);
      const bw = Math.max(2, x2 - x1 - 1);
      offCtx.fillStyle = active
        ? "rgba(131,103,199,0.92)"
        : isLight
          ? "rgba(131,103,199,0.35)"
          : "rgba(131,103,199,0.32)";
      offCtx.beginPath();
      if (offCtx.roundRect) offCtx.roundRect(x1 + 0.5, laneTop + 2, bw, WAVE_LANE_H - 4, 4);
      else offCtx.rect(x1 + 0.5, laneTop + 2, bw, WAVE_LANE_H - 4);
      offCtx.fill();
      if (active && bw > 10) {
        offCtx.fillStyle = "rgba(255,255,255,0.85)";
        offCtx.fillRect(x1 + 2, laneTop + 8, 2, WAVE_LANE_H - 16);
        offCtx.fillRect(x2 - 4, laneTop + 8, 2, WAVE_LANE_H - 16);
      }
      if (bw > 34) {
        offCtx.save();
        offCtx.beginPath();
        offCtx.rect(x1 + 6, laneTop, bw - 12, WAVE_LANE_H);
        offCtx.clip();
        offCtx.fillStyle = active ? "#ffffff" : isLight ? "#3f3f46" : "#d4d4d8";
        offCtx.fillText(
          `#${idx + 1} ${(cue.text || "").replace(/\n/g, " ")}`,
          x1 + 7,
          laneTop + WAVE_LANE_H / 2
        );
        offCtx.restore();
      }
    });
  }

  /**
   * Draws the waveform canvas: re-renders the offscreen layer only when the
   * cache is dirty, then blits it and paints the playhead. The backing store
   * is sized to CSS px x devicePixelRatio for crisp lines on Retina screens.
   * @param {number} playheadMs Playhead time in ms.
   */
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

    const isLight = getCurrentScheme() === "light";
    const colors = getWaveColors(isLight);
    const vr = viewRange();
    const viewEnd = vr.start + vr.span;
    const peaks = state.peaks || [];
    const dragIdx = state.drag && state.drag.kind === "cue" ? state.drag.idx : -1;

    const dirty =
      !waveCache.canvas ||
      waveCache.pw !== pw ||
      waveCache.ph !== ph ||
      waveCache.dpr !== dpr ||
      waveCache.isLight !== isLight ||
      waveCache.bg !== colors.bg ||
      waveCache.peak !== colors.peak ||
      waveCache.start !== vr.start ||
      waveCache.end !== viewEnd ||
      waveCache.peaksRef !== peaks ||
      waveCache.durationMs !== state.durationMs ||
      waveCache.cuesRef !== state.cues ||
      waveCache.renderedVersion !== waveCache.cuesVersion ||
      waveCache.activeCueIndex !== state.activeCueIndex ||
      waveCache.dragIdx !== dragIdx;

    if (dirty) {
      if (!waveCache.canvas) waveCache.canvas = document.createElement("canvas");
      if (waveCache.canvas.width !== pw || waveCache.canvas.height !== ph) {
        waveCache.canvas.width = pw;
        waveCache.canvas.height = ph;
      }
      const offCtx = waveCache.canvas.getContext("2d");
      renderWaveformBackground(offCtx, w, h, dpr, vr, isLight, colors);
      waveCache.pw = pw;
      waveCache.ph = ph;
      waveCache.dpr = dpr;
      waveCache.isLight = isLight;
      waveCache.bg = colors.bg;
      waveCache.peak = colors.peak;
      waveCache.start = vr.start;
      waveCache.end = viewEnd;
      waveCache.peaksRef = peaks;
      waveCache.durationMs = state.durationMs;
      waveCache.cuesRef = state.cues;
      waveCache.renderedVersion = waveCache.cuesVersion;
      waveCache.activeCueIndex = state.activeCueIndex;
      waveCache.dragIdx = dragIdx;
    }

    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.drawImage(waveCache.canvas, 0, 0);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    // Playhead
    const px = msToX(playheadMs, w, vr);
    if (px >= 0 && px <= w) {
      ctx.fillStyle = "#8367c7";
      ctx.fillRect(Math.round(px), 0, 2, h);
    }
  }

  /**
   * Finds the cue block (and which part of it) under the pointer.
   * @param {number} x Pointer x, CSS px relative to the canvas.
   * @param {number} y Pointer y, CSS px relative to the canvas.
   * @return {?{idx: number, mode: string}} mode is "start" or "end" (edge
   *     resize) or "move"; null when off the lane or far from any block.
   *     Gap hits also carry a `dist` field (px to the edge).
   */
  function hitTestCue(x, y) {
    const { w, h } = waveSize();
    return hitTestCueAt(state.cues, x, y, {
      w,
      h,
      vr: viewRange(),
      laneH: WAVE_LANE_H,
      edgePx: WAVE_EDGE_PX,
    });
  }

  /**
   * Seeks the active media (if it has a source), cancels any auto-stop and
   * redraws the waveform and preview.
   * @param {number} ms Target time, clamped to [0, totalDurMs()].
   */
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

  /**
   * Changes the highlighted cue by toggling only the two affected rows (no
   * list re-render). Does not redraw the waveform.
   * @param {number} idx Cue index or -1.
   */
  function setActiveCueIndex(idx) {
    const prevIdx = state.activeCueIndex;
    if (idx === prevIdx) return;
    const list = $("cue-list");
    if (list) {
      if (prevIdx >= 0) {
        const prevEl = list.querySelector(`.cue-item[data-index="${prevIdx}"]`);
        if (prevEl) prevEl.classList.remove("active");
      }
      if (idx >= 0) {
        const nextEl = list.querySelector(`.cue-item[data-index="${idx}"]`);
        if (nextEl) nextEl.classList.add("active");
      }
    }
    state.activeCueIndex = idx;
  }

  /**
   * Highlights a cue and scrolls the list to it.
   * @param {number} idx Cue index.
   */
  function selectCue(idx) {
    setActiveCueIndex(idx);
    scrollCueIntoList(idx);
  }

  /**
   * Scrolls only the cue list so a row is visible. scrollIntoView() also
   * moved the page, leaving the waveform somewhere else under the pointer,
   * so the next click on an edge landed in the wrong place.
   * @param {number} idx Cue index.
   */
  function scrollCueIntoList(idx) {
    const list = $("cue-list");
    const el = list.querySelector(`.cue-item[data-index="${idx}"]`);
    if (!el) return;
    const lr = list.getBoundingClientRect();
    const er = el.getBoundingClientRect();
    if (er.top < lr.top) list.scrollTop += er.top - lr.top - 4;
    else if (er.bottom > lr.bottom) list.scrollTop += er.bottom - lr.bottom + 4;
  }

  /**
   * Refreshes a row's time inputs and timecode without re-rendering the list
   * (used during drags and time edits).
   * @param {number} idx Cue index.
   */
  function updateCueRowTimes(idx) {
    const cue = state.cues[idx];
    const el = document.querySelector(`.cue-item[data-index="${idx}"]`);
    if (!cue || !el) return;
    el.querySelector(".cue-start").value = String(cue.start_ms);
    el.querySelector(".cue-end").value = String(cue.end_ms);
    el.querySelector(".cue-tc").textContent =
      `${formatMsReadable(cue.start_ms)} → ${formatMsReadable(cue.end_ms)}`;
  }

  /**
   * Applies a drag offset to the dragged cue, relative to its original times:
   * snaps to 10 ms, keeps at least MIN_CUE_MS, never overlaps the neighbors
   * and rescales word timings. Updates row, waveform and preview live; the
   * save happens on pointerup.
   * @param {!Object} d state.drag of kind "cue".
   * @param {number} deltaMs Pointer offset from the grab point, ms.
   */
  function applyCueDrag(d, deltaMs) {
    const cue = state.cues[d.idx];
    const { start, end } = d.orig;
    const { start: ns, end: ne } = computeCueDrag(state.cues, d, deltaMs, totalDurMs(), MIN_CUE_MS);
    cue.start_ms = ns;
    cue.end_ms = ne;
    cue.words = rescaleWords(d.orig.words, start, end, ns, ne);
    updateCueRowTimes(d.idx);
    invalidateWaveCache();
    renderPreviewAtMs(d.mode === "end" ? ne - 20 : ns + 20);
    drawWaveform(currentMs());
  }

  /**
   * Aborts a cue drag (Escape or pointercancel), restoring the original times
   * and words.
   */
  function cancelCueDrag() {
    const d = state.drag;
    if (!d || d.kind !== "cue") return;
    const cue = state.cues[d.idx];
    cue.start_ms = d.orig.start;
    cue.end_ms = d.orig.end;
    cue.words = d.orig.words;
    state.drag = null;
    updateCueRowTimes(d.idx);
    invalidateWaveCache();
    drawWaveform(currentMs());
    renderPreviewAtMs(currentMs());
  }

  /**
   * Wires waveform interaction: press on a cue block to move/resize it (a
   * click without movement seeks to its start), press elsewhere to scrub,
   * Escape cancels a drag, ⌘/Ctrl + wheel or pinch zooms around the pointer,
   * plain wheel scrolls while zoomed. Also wires the zoom buttons and resize
   * handling (which invalidates the cached stage height).
   */
  function wireWaveform() {
    const canvas = $("waveform-canvas");
    // Pointer position in canvas-local CSS px.
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
          orig: {
            start: cue.start_ms,
            end: cue.end_ms,
            words: (cue.words || []).map((wd) => ({ ...wd })),
          },
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
      // 3 px dead zone so a plain click is not treated as a drag.
      if (!d.moved && Math.abs(x - d.startX) < 3) return;
      d.moved = true;
      canvas.style.cursor = d.mode === "move" ? "grabbing" : "ew-resize";
      applyCueDrag(d, xToMs(x, w, vr) - d.grabMs);
    });

    // Ends the current gesture. A cancelled cue drag restores the original
    // times; a click without movement seeks; a real drag saves and re-renders.
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
          // Trackpad pinch (ctrlKey) or ⌘ + wheel: zoom around the pointer.
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

    // Zoom buttons anchor on the playhead when visible, else the window center.
    const zoomCenter = () => {
      const { start, span } = viewRange();
      const ms = currentMs();
      return ms >= start && ms <= start + span ? ms : start + span / 2;
    };
    $("btn-zoom-in").addEventListener("click", () => zoomAround(zoomCenter(), 0.5));
    $("btn-zoom-out").addEventListener("click", () => zoomAround(zoomCenter(), 2));
    $("btn-zoom-fit").addEventListener("click", () => setView(0, Infinity));
    window.addEventListener("resize", () => {
      invalidateStageSizeCache();
      drawWaveform(currentMs());
    });
    if (typeof ResizeObserver !== "undefined" && $("preview-stage")) {
      new ResizeObserver(invalidateStageSizeCache).observe($("preview-stage"));
    }
  }

  /**
   * Plays from `ms`; with `stopAtMs` it pauses itself on arrival. The check
   * runs every animation frame, so it does not overshoot by ~250 ms the way
   * a timeupdate listener would.
   * @param {number} ms Start time, ms.
   * @param {?number=} stopAtMs Auto-pause time in ms, or null to play freely.
   */
  function seekAndPlay(ms, stopAtMs = null) {
    const mediaEl = activeMedia();
    state.playUntilMs = stopAtMs;
    mediaEl.currentTime = Math.max(0, ms / 1000);
    mediaEl.play().catch(() => {});
    if (stopAtMs == null) return;
    cancelAnimationFrame(state.playUntilRaf);
    // rAF loop; stops once paused, when the limit is cleared, or on arrival.
    const tick = () => {
      const m = activeMedia();
      if (state.playUntilMs == null || m.paused) {
        state.playUntilMs = null;
        return;
      }
      if (m.currentTime * 1000 >= state.playUntilMs) {
        m.pause();
        // Step back a few ms so the monitor keeps showing this cue and not
        // the next one when both touch.
        m.currentTime = Math.max(0, (state.playUntilMs - 30) / 1000);
        state.playUntilMs = null;
        return;
      }
      state.playUntilRaf = requestAnimationFrame(tick);
    };
    state.playUntilRaf = requestAnimationFrame(tick);
  }

  // ---------------------------------------------------------------------------
  // Resolve bridge
  // ---------------------------------------------------------------------------

  /**
   * Moves DaVinci Resolve's playhead to a time. Resolve Studio is driven
   * directly; otherwise the server stores the timecode and the toast tells the
   * user to run the «3 - MTTN AutoSubs - Ir a Tiempo» Lua script.
   * @async
   * @param {number} ms Time on the session timeline, ms.
   * @return {!Promise<void>}
   */
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

  // ---------------------------------------------------------------------------
  // Background jobs and uploads
  // ---------------------------------------------------------------------------

  /**
   * Per-job poller (see createJobPoller() in ui/app_helpers.js). It owns the
   * map of polled jobs and which one #job-box shows; the hooks below do the
   * DOM work. Several jobs (e.g. a model download and a transcription) can
   * run at once; each keeps its own callbacks. `jobPollers.size` is the
   * number of jobs being polled. state.activeJobId mirrors the shown job so
   * the cancel button knows its target.
   */
  const jobPollers = createJobPoller({
    fetchJson: (url) => apiJson(url),
    setTimeout: (fn, ms) => setTimeout(fn, ms),
    clearTimeout: (handle) => clearTimeout(handle),
    // A job became the shown one: cancel a pending hide and reveal the box.
    onShow: (jobId) => {
      state.activeJobId = jobId;
      clearTimeout(state.jobHideTimer);
      state.jobHideTimer = null;
      $("job-box").classList.remove("hidden");
      $("btn-cancel-job").classList.remove("hidden");
    },
    onRender: renderJobBox,
    // The last boxed job ended: hide the cancel button and, after a success,
    // hide the box 2 s later (after a failure the final stage stays visible).
    onIdle: (succeeded) => {
      state.activeJobId = null;
      $("btn-cancel-job").classList.add("hidden");
      if (succeeded) {
        clearTimeout(state.jobHideTimer);
        state.jobHideTimer = setTimeout(() => $("job-box").classList.add("hidden"), 2000);
      }
    },
    onToast: showToast,
  });

  /**
   * Polls a server job until it finishes and runs its callbacks. Each job
   * gets its own poller, so starting a second job never abandons the first.
   * The next request is scheduled only after the previous response arrives
   * (self-rescheduling setTimeout, not setInterval), so slow responses never
   * overlap and `onSuccess` runs at most once. If the id is already polled
   * (the server can return the running job for a duplicate start), no second
   * poller is created; the job is just brought to the progress box.
   *
   * Jobs with showInBox (default) drive #job-box: it shows the most recently
   * started active job, plus a count of the others, and the cancel button
   * targets the job shown. On success the box hides 2 s after the last boxed
   * job ends; errors and cancellations are toasts unless `onError` is given.
   * @param {string} jobId Id returned by the job-starting endpoint.
   * @param {function(!Object)} onSuccess Receives the finished job (payload in
   *     job.result). May be async; a rejection is shown as a toast.
   * @param {{onError: (function(?Object, ?Error)|undefined),
   *     onProgress: (function(!Object)|undefined),
   *     showInBox: (boolean|undefined),
   *     intervalMs: (number|undefined)}=} opts onError receives the failed
   *     job, or null and the network/HTTP error; onProgress receives every
   *     snapshot; showInBox false keeps the job out of #job-box; intervalMs
   *     is the delay between polls (default 500).
   */
  function pollJob(jobId, onSuccess, opts = {}) {
    jobPollers.pollJob(jobId, onSuccess, opts);
  }

  /**
   * Paints the shown job's last snapshot into #job-box. Before the first
   * response it shows a neutral 0% instead of the previous job's text. When
   * other boxed jobs are running, their count is appended to the stage.
   * @param {!Object} job Last snapshot of the shown job ({} before the first
   *     response).
   * @param {number} others Number of other boxed jobs still running.
   */
  function renderJobBox(job, others) {
    const progress = job.progress || 0;
    const extra = others > 0 ? ` (+${others} ${others === 1 ? "tarea" : "tareas"} en curso)` : "";
    $("job-stage").textContent = `${job.stage || "Procesando..."}${extra}`;
    $("job-pct").textContent = `${progress}%`;
    $("job-bar").style.width = `${progress}%`;
  }

  /**
   * Uploads a media file as the raw request body (URI-encoded name in
   * X-Filename, so non-ASCII names survive) and loads the returned session.
   * @async
   * @param {?File} file Audio or video file.
   * @return {!Promise<void>}
   */
  async function uploadFile(file) {
    if (!file) return;
    $("upload-status").textContent = `Subiendo ${file.name}...`;
    try {
      const resp = await fetch("/api/upload", {
        method: "POST",
        headers: { "X-Filename": encodeURIComponent(file.name) },
        body: file,
      });
      const data = await readJsonOrText(resp, "Fallo al subir archivo");
      $("upload-status").textContent = `✓ Cargado: ${data.filename}`;
      $("media-name-badge").textContent = data.filename;
      if (data.session) applySession(data.session);
      showToast(`Archivo ${data.filename} listo para transcribir.`);
    } catch (err) {
      $("upload-status").textContent = `Error: ${err.message}`;
      showToast(err.message);
    }
  }

  // ---------------------------------------------------------------------------
  // Event wiring
  // ---------------------------------------------------------------------------
  // wireEvents() runs once at startup and calls one wire* function per UI
  // area. Call order is the listener registration order; it matters where two
  // areas listen to the same element (e.g. #st-font "change" runs
  // syncFontSelection before updatePresetStatus, and wireMachinePanel() adds
  // its own #model-select / #preset-select / #cfg-uppercase listeners after
  // the ones registered before it).

  /**
   * Switches the monitor aspect ratio and re-renders the preview.
   * @param {string} mode "16:9" or "9:16".
   */
  function setAspect(mode) {
    state.aspect = mode;
    const stage = $("preview-stage");
    stage.classList.toggle("aspect-16-9", mode === "16:9");
    stage.classList.toggle("aspect-9-16", mode === "9:16");
    $("btn-aspect-16-9").classList.toggle("active", mode === "16:9");
    $("btn-aspect-9-16").classList.toggle("active", mode === "9:16");
    invalidateStageSizeCache();
    renderPreviewAtMs((activeMedia().currentTime || 0) * 1000);
  }

  /**
   * Uploads font files (.ttf/.otf or a .zip pack) one at a time, optionally
   * installing them into ~/Library/Fonts so Resolve can use them, then
   * refreshes the catalog selecting the last uploaded pack.
   * @async
   * @param {?FileList} fileList Files from the input.
   * @return {!Promise<void>}
   */
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
        const data = await readJsonOrText(resp, `Error subiendo ${file.name}`);
        lastRes = data;
        totalFaces += Number(data.installed_count || 0);
      }
      if (lastRes) {
        updateFontCatalog(lastRes, lastRes.selected_pack_id);
        const restartNote =
          lastRes.installed_to_macos && lastRes.resolve_running
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

  // Monitor test backgrounds (useful to judge transparency); each maps to a
  // .stage-bg-<name> class in styles.css.
  const STAGE_BGS = ["black", "gray", "light", "checker"];

  /**
   * Applies a stage-bg-* class to the monitor and syncs the swatch buttons'
   * aria-pressed.
   * @param {string} bg One of STAGE_BGS; anything else falls back to "black".
   * @param {boolean=} persist Whether to remember it in localStorage.
   */
  function setStageBg(bg, persist = true) {
    const value = STAGE_BGS.includes(bg) ? bg : "black";
    const stage = $("preview-stage");
    STAGE_BGS.forEach((b) => stage.classList.toggle(`stage-bg-${b}`, b === value));
    document.querySelectorAll("#stage-bg-toggle .bg-swatch").forEach((btn) => {
      btn.setAttribute("aria-pressed", String(btn.dataset.bg === value));
    });
    if (persist) {
      try {
        localStorage.setItem("autosubs-stage-bg", value);
      } catch (_) {}
    }
  }

  /** Shows the Text+ template field only for the "textplus" push mode. */
  function syncPushModeVisibility() {
    $("template-field").classList.toggle("hidden", $("push-mode").value !== "textplus");
  }

  /** Workflow mode buttons (Resolve / standalone) and the source tabs. */
  function wireModeAndSourceTabs() {
    $("btn-mode-standalone").addEventListener("click", () => setAppMode("standalone", true));
    $("btn-mode-resolve").addEventListener("click", () => setAppMode("resolve", true));
    $("btn-switch-to-resolve").addEventListener("click", () => setAppMode("resolve", true));

    document.querySelectorAll(".tab-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const tab = btn.dataset.tab;
        document
          .querySelectorAll(".tab-btn")
          .forEach((b) => b.classList.toggle("active", b === btn));
        $("tab-resolve").classList.toggle("active", tab === "resolve");
        $("tab-file").classList.toggle("active", tab === "file");
      });
    });
  }

  /** Status refresh, Resolve inbox and audio export buttons, and the demo. */
  function wireResolveInbox() {
    $("btn-refresh").addEventListener("click", refreshStatus);
    $("btn-check-inbox").addEventListener("click", async () => {
      try {
        const notifyRes = await apiJson("/api/resolve/notify-export", {
          method: "POST",
          body: "{}",
        });
        if (notifyRes && notifyRes.session) {
          applySession(notifyRes.session);
        } else {
          await refreshStatus();
        }
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
  }

  /** Whisper model select and download, transcription start and job cancel. */
  function wireTranscription() {
    $("model-select").addEventListener("change", updateModelHint);

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
        // The source follows the visible tab (Resolve inbox vs uploaded file).
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
          // The server marks the result stale when the media changed while
          // Whisper ran (e.g. a new Resolve export); its cues belong to the
          // old audio, so load the current session instead of this result.
          if (isStaleJobResult(job)) {
            showToast(
              "Se descartó la transcripción porque el audio cambió mientras se procesaba. " +
                "Vuelve a transcribir.",
              6500
            );
            refreshStatus();
            return;
          }
          if (job.result) applySession(job.result);
          showToast(job.stage || "Transcripción finalizada.");
        });
      } catch (err) {
        showToast(err.message, 5000);
      }
    });

    $("btn-cancel-job").addEventListener("click", async () => {
      if (!state.activeJobId) return;
      try {
        await apiJson(`/api/jobs/${state.activeJobId}/cancel`, { method: "POST", body: "{}" });
      } catch (err) {
        showToast(err.message);
      }
    });
  }

  /** Subtitle format card: RAE resegmentation, format presets, BGG glossary. */
  function wireFormatSettings() {
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

    // Server presets fill CPL/lines/pause; vertical presets switch to 9:16.
    $("preset-select").addEventListener("change", () => {
      const p = state.presets[$("preset-select").value];
      if (!p) return;
      $("cfg-cpl").value = p.max_cpl;
      $("cfg-lines").value = p.max_lines;
      $("cfg-pause").value = p.pause_split_ms;
      if (
        $("preset-select").value === "reels_9_16" ||
        $("preset-select").value === "karaoke_impacto"
      ) {
        setAspect("9:16");
      } else {
        setAspect("16:9");
      }
    });

    if ($("btn-fill-bgg-glossary")) {
      $("btn-fill-bgg-glossary").addEventListener("click", () => {
        // Board-game (BGG) terms appended to the custom glossary, skipping duplicates.
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
  }

  /** Media file input and the drag & drop zone. */
  function wireUploads() {
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
  }

  /** Monitor transport: play/pause, ±1 s, playback rate, media events. */
  function wireTransport() {
    const audio = $("audio-player");
    const video = $("video-player");
    $("btn-play").addEventListener("click", () => {
      state.playUntilMs = null; // The main button plays without a stop limit.
      const m = activeMedia();
      if (m.paused) m.play().catch(() => {});
      else m.pause();
    });
    const onPlay = () => ($("btn-play").textContent = "II Pausar");
    const onPause = () => ($("btn-play").textContent = "▶ Reproducir");
    // Hot path (~4 Hz while playing): time readout, active cue, auto-scroll of a
    // zoomed waveform (not while dragging), preview and waveform.
    const onTimeUpdate = () => {
      const m = activeMedia();
      const ms = (m.currentTime || 0) * 1000;
      const tot = state.durationMs || (m.duration ? m.duration * 1000 : 0);
      $("time-readout").textContent = `${formatMsReadable(ms)} / ${formatMsReadable(tot)}`;
      const idx = state.cues.findIndex((c) => ms >= c.start_ms && ms <= c.end_ms);
      if (idx !== state.activeCueIndex) {
        setActiveCueIndex(idx);
      }
      if (!m.paused && !state.drag) ensureMsVisible(ms);
      renderPreviewAtMs(ms);
      drawWaveform(ms);
    };
    [audio, video].forEach((mEl) => {
      mEl.addEventListener("play", onPlay);
      mEl.addEventListener("pause", onPause);
      mEl.addEventListener("timeupdate", onTimeUpdate);
      // Show the duration as soon as it is known, without waiting for playback.
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
  }

  /** Aspect ratio buttons (16:9 / 9:16). */
  function wireAspectButtons() {
    $("btn-aspect-16-9").addEventListener("click", () => setAspect("16:9"));
    $("btn-aspect-9-16").addEventListener("click", () => setAspect("9:16"));
  }

  /** Find and replace (literal, case-sensitive, across all cues). */
  function wireFindReplace() {
    $("btn-replace-all").addEventListener("click", async () => {
      const find = $("search-input").value;
      const rep = $("replace-input").value;
      if (!find) return;
      state.cues.forEach((cue) => {
        cue.text = cue.text.split(find).join(rep);
        cue.lines = cue.text
          .split("\n")
          .map((s) => s.trim())
          .filter(Boolean);
        syncCueWordsAfterTextEdit(cue);
      });
      const saved = await saveCuesToServer();
      renderCueList();
      // A conflict or save error already showed its own toast; keep it visible.
      if (saved) showToast(`Reemplazado "${find}" por "${rep}".`);
    });
  }

  /** Custom font upload, font select and the animation controls. */
  function wireFontsAndAnimation() {
    $("font-input").addEventListener("change", (ev) => uploadFontFiles(ev.target.files));
    $("st-font").addEventListener("change", () => syncFontSelection(true));
    $("st-anim-mode").addEventListener("change", () => {
      invalidateStyleCache();
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
        invalidateStyleCache();
        syncAnimControlsVisibility();
        renderPreviewAtMs((activeMedia().currentTime || 0) * 1000);
      });
    });
  }

  /**
   * Style preset chips, the «save style» form, the karaoke toggle and the
   * preset reset button. Also renders the saved user chips and restores the
   * karaoke toggle from localStorage.
   */
  function wireStylePresets() {
    // Delegated clicks on chip containers: apply a preset or delete a user preset.
    document.querySelectorAll(".style-chips").forEach((box) => {
      box.addEventListener("click", (ev) => {
        const del = ev.target.closest("[data-delete-preset]");
        if (del) {
          const map = loadCustomPresets();
          const name = (map[del.dataset.deletePreset] || {}).preset_name || "";
          delete map[del.dataset.deletePreset];
          saveCustomPresets(map);
          if (state.activePreset === `custom:${del.dataset.deletePreset}`) {
            state.activePreset = null;
          }
          renderCustomChips();
          showToast(`Estilo «${name}» borrado.`);
          return;
        }
        const chip = ev.target.closest(".chip[data-style]");
        if (chip) applyStylePreset(chip.dataset.style);
      });
    });

    const saveForm = $("preset-save-form");
    // Shows the inline «save style» form and hides its trigger (or the reverse).
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

    // Global karaoke toggle (off by default, remembered across sessions).
    const syncKaraoke = () => {
      invalidateStyleCache();
      const on = $("st-karaoke").checked;
      syncStyleControlStates();
      updatePresetStatus();
      try {
        localStorage.setItem("autosubs-karaoke", on ? "1" : "0");
      } catch (_) {}
      renderPreviewAtMs(currentMs());
    };
    try {
      $("st-karaoke").checked = localStorage.getItem("autosubs-karaoke") === "1";
    } catch (_) {}
    syncStyleControlStates();
    $("st-karaoke").addEventListener("change", syncKaraoke);

    $("btn-preset-reset").addEventListener("click", () => {
      if (state.activePreset) applyStylePreset(state.activePreset);
    });
  }

  /** Monitor background swatches; restores the saved background. */
  function wireStageBackground() {
    document.querySelectorAll("#stage-bg-toggle .bg-swatch").forEach((btn) => {
      btn.addEventListener("click", () => setStageBg(btn.dataset.bg));
    });
    let savedBg = "black";
    try {
      savedBg = localStorage.getItem("autosubs-stage-bg") || "black";
    } catch (_) {}
    setStageBg(savedBg, false);
  }

  /**
   * Every style control re-renders through onStyleControlChanged: "input" for
   * live slider feedback, "change" for selects, checkboxes and color pickers.
   */
  function wireStyleControls() {
    [
      "st-weight",
      "st-ascii-fold",
      "st-size",
      "st-posy",
      "st-fill",
      "st-highlight",
      "st-outline",
      "st-outline-w",
      "st-shadow",
      "st-box",
      "st-box-color",
      "cfg-uppercase",
      "st-fill-a",
      "st-highlight-a",
      "st-outline-a",
      "st-shadow-a",
      "st-box-a",
      "st-anim-mode",
    ].forEach((id) => {
      $(id).addEventListener("input", onStyleControlChanged);
      $(id).addEventListener("change", onStyleControlChanged);
    });
    $("st-font").addEventListener("change", updatePresetStatus);
  }

  /** Push to DaVinci Resolve 20: push mode select and the push button. */
  function wireResolvePush() {
    $("push-mode").addEventListener("change", syncPushModeVisibility);
    $("push-mode").addEventListener("change", updateStyleTargetHint);
    syncPushModeVisibility();
    updateStyleTargetHint();

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
        box.append(
          title,
          document.createElement("br"),
          msgNode,
          document.createElement("br"),
          srtHint
        );
        showToast(res.message, 5000);
      } catch (err) {
        showToast(err.message);
      }
    });
  }

  /**
   * File exports, copy-script and burned-in MP4 (standalone mode). The server
   * returns the file content, downloaded through a temporary Blob URL.
   */
  function wireExports() {
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
          downloadBlob(new Blob([data.content], { type: data.mime_type }), data.filename);
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
              triggerDownload(job.result.download_url, job.result.filename || "subtitulado.mp4");
            }
            showToast(job.stage || "MP4 subtitulado listo.");
          });
        } catch (err) {
          showToast(err.message, 5000);
        }
      });
    }
  }

  /** Space play/pause shortcut and the throttled refresh on window return. */
  function wireKeyboardAndFocus() {
    // Space toggles play/pause unless the focus is in a form field
    document.addEventListener("keydown", (ev) => {
      if (
        ev.code === "Space" &&
        !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)
      ) {
        ev.preventDefault();
        $("btn-play").click();
      }
    });

    // Pick up changes made from DaVinci Resolve scripts while away (throttled).
    document.addEventListener("visibilitychange", refreshOnReturn);
    window.addEventListener("focus", refreshOnReturn);
  }

  /** Theme toggle button, plus theme sync across tabs and with the OS. */
  function wireTheme() {
    if ($("btn-theme-toggle")) {
      $("btn-theme-toggle").addEventListener("click", toggleTheme);
    }

    window.addEventListener("storage", (e) => {
      if (e.key === "autosubs-theme") {
        const t = e.newValue;
        applyTheme(t === "light" || t === "dark" ? t : "system", false);
      }
    });

    if (window.matchMedia) {
      window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
        let saved = null;
        try {
          saved = localStorage.getItem("autosubs-theme");
        } catch (_) {}
        if (!saved) {
          applyTheme("system", false);
        }
      });
    }
  }

  /**
   * Attaches every UI event handler; called once at startup. Also restores
   * UI-only localStorage state (karaoke, stage background) and syncs the
   * push-mode dependent fields. See the section comment for why the call
   * order must be kept.
   */
  function wireEvents() {
    wireModeAndSourceTabs();
    wireResolveInbox();
    wireTranscription();
    wireFormatSettings();
    wireUploads();
    wireTransport();
    wireWaveform();
    wireAspectButtons();
    wireFindReplace();
    wireFontsAndAnimation();
    wireStylePresets();
    wireStageBackground();
    wireMachinePanel();
    wireStyleControls();
    wireResolvePush();
    wireExports();
    wireKeyboardAndFocus();
    wireTheme();
  }

  // ---------------------------------------------------------------------------
  // Startup
  // ---------------------------------------------------------------------------
  // Order matters: handlers first, then the theme, then a default preset so the
  // preview has a style before /api/status (and fonts) arrive. refreshStatus()
  // re-applies that preset once fonts are known, unless the user changed it.
  wireEvents();
  let initialTheme = null;
  try {
    initialTheme = localStorage.getItem("autosubs-theme");
  } catch (_) {}
  applyTheme(initialTheme, false);
  applyStylePreset("netflix");
  refreshStatus();
})();
