/**
 * @fileoverview Pure, DOM-free helpers shared by the Studio UI (ui/app.js)
 * and the Node unit tests (tests/js/*.test.mjs).
 *
 * Why a separate file: ui/app.js is one IIFE that touches the DOM almost
 * everywhere, so its math could not be tested without a browser. Everything
 * here takes plain values and returns plain values (or, for
 * createJobPoller(), receives its side effects as injected callbacks), so
 * `node --test tests/js/` can exercise it with no dependencies.
 *
 * Loading: UMD-style, no build step. index.html loads this file with a plain
 * <script> before app.js, which attaches the API to `window.AutoSubsHelpers`;
 * under Node (CommonJS) the same object is `module.exports`.
 *
 * Units: times are ms on the media timeline, x/y are CSS px relative to the
 * waveform canvas, colors are "#rrggbb" strings or [r, g, b] 0..255 arrays.
 * User-facing strings are Spanish by design (AGENTS.md).
 */
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.AutoSubsHelpers = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  // ---------------------------------------------------------------------------
  // Text: ASCII folding, timecodes, tokens
  // ---------------------------------------------------------------------------

  /**
   * Spanish-specific characters mapped to ASCII for fonts without Spanish
   * glyphs (preview of «Adaptar glifos ASCII»). Mirrors
   * exporters.fold_spanish_to_ascii so the monitor matches the exports.
   */
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

  /**
   * Folds Spanish text to plain ASCII for the preview: the explicit map first
   * (ñ -> n, ¿/¡ removed, curly quotes -> straight), then NFKD plus removal of
   * combining marks for any other accented letter.
   * @param {*} text Text to fold; null/undefined become "".
   * @return {string} ASCII-only text.
   */
  function foldSpanishToAsciiPreview(text) {
    return String(text || "")
      .normalize("NFC")
      .replace(/[áéíóúüñÁÉÍÓÚÜÑ¿¡«»“”‘’—–…]/g, (ch) => ASCII_FOLD_MAP[ch] ?? ch)
      .normalize("NFKD")
      .replace(/[\u0300-\u036f]/g, "");
  }

  /**
   * Applies the preview text transforms in the same order as the exporters:
   * ASCII folding (if enabled), then uppercase (if enabled).
   * @param {string} text Token or whole cue text.
   * @param {{ascii_fold: (boolean|undefined),
   *     uppercase: (boolean|undefined)}} st Style from readStyleFromUI().
   * @return {string} Transformed text.
   */
  function transformPreviewText(text, st) {
    let out = st.ascii_fold ? foldSpanishToAsciiPreview(text) : text;
    if (st.uppercase) out = out.toUpperCase();
    return out;
  }

  /**
   * Formats a time as "MM:SS.cc" (centiseconds) for cue timecodes.
   * @param {number} ms Time in ms; negative or missing values clamp to 0.
   * @return {string} e.g. "01:04.25".
   */
  function formatMsReadable(ms) {
    const safe = Math.max(0, Math.floor(ms || 0));
    const min = Math.floor(safe / 60000);
    const sec = Math.floor((safe % 60000) / 1000);
    const cs = Math.floor((safe % 1000) / 10);
    return `${String(min).padStart(2, "0")}:${String(sec).padStart(2, "0")}.${String(cs).padStart(2, "0")}`;
  }

  /**
   * Formats a ruler label as "m:ss", or "m:ss.s" when ticks are < 1 s apart.
   * @param {number} ms Tick time.
   * @param {number} step Tick spacing in ms.
   * @return {string}
   */
  function formatTick(ms, step) {
    const totalS = ms / 1000;
    const m = Math.floor(totalS / 60);
    const sec = totalS - m * 60;
    const secStr = step < 1000 ? sec.toFixed(1).padStart(4, "0") : String(Math.floor(sec)).padStart(2, "0");
    return `${m}:${secStr}`;
  }

  /**
   * CSS class of a cue's reading-speed badge. Only speeds that need
   * attention are colored: > 17 CPS warn, > 20 CPS danger.
   * @param {number} cps Characters per second.
   * @return {string} Class name(s).
   */
  function cpsClassName(cps) {
    if (cps > 20) return "pill pill-danger";
    if (cps > 17) return "pill pill-warn";
    return "cue-cps";
  }

  /**
   * Splits cue lines into whitespace-separated word tokens, in reading order.
   * This is the token sequence that cue.words is aligned with.
   * @param {?Array<string>} lines Cue lines.
   * @return {!Array<string>} Non-empty tokens.
   */
  function tokenizeLines(lines) {
    return (lines || []).flatMap((ln) => ln.split(/\s+/).filter(Boolean));
  }

  /**
   * Keeps word timings when a cue's text is edited. The server
   * (align_words_to_lines) keeps Whisper timings for words that survive and
   * spreads time over new ones; locally the word texts are only synced when
   * the token count still matches.
   * @param {!Object} cue Cue whose `lines` were just updated (mutated).
   */
  function syncCueWordsAfterTextEdit(cue) {
    const tokens = tokenizeLines(cue.lines);
    if (cue.words && cue.words.length === tokens.length) {
      cue.words.forEach((w, i) => { w.text = tokens[i]; });
    }
  }

  /**
   * Linearly remaps word timings from an old cue span to a new one.
   * @param {?Array<!Object>} words Words with start_ms/end_ms.
   * @param {number} oldStart Old cue start, ms.
   * @param {number} oldEnd Old cue end, ms.
   * @param {number} newStart New cue start, ms.
   * @param {number} newEnd New cue end, ms.
   * @return {!Array<!Object>} New word objects (inputs not mutated); [] when
   *     there are no words. A zero or negative old span is treated as 1 ms
   *     so the scale never divides by zero.
   */
  function rescaleWords(words, oldStart, oldEnd, newStart, newEnd) {
    if (!words || !words.length) return [];
    const scale = (newEnd - newStart) / Math.max(1, oldEnd - oldStart);
    return words.map((w) => ({
      ...w,
      start_ms: Math.round(newStart + (w.start_ms - oldStart) * scale),
      end_ms: Math.round(newStart + (w.end_ms - oldStart) * scale),
    }));
  }

  /**
   * Picks the cue the preview shows at a given time: the first cue spanning
   * `ms` (bounds inclusive), else the active cue so the cue being edited
   * stays visible while paused elsewhere.
   * @param {!Array<!Object>} cues Cues with start_ms/end_ms.
   * @param {number} ms Time in ms.
   * @param {number} activeIdx state.activeCueIndex (-1 for none).
   * @return {?Object} The cue, or null.
   */
  function findPreviewCue(cues, ms, activeIdx) {
    return cues.find((c) => ms >= c.start_ms && ms <= c.end_ms) ||
      (activeIdx >= 0 ? cues[activeIdx] : null);
  }

  // ---------------------------------------------------------------------------
  // Colors and WCAG contrast
  // ---------------------------------------------------------------------------

  /**
   * Converts a hex color plus alpha into a CSS rgba() string.
   * @param {string} hex "#rgb" or "#rrggbb" ("#" optional).
   * @param {number} alpha Opacity 0..1.
   * @return {string} e.g. "rgba(255, 212, 59, 0.750)".
   */
  function hexToRgba(hex, alpha) {
    const h = String(hex || "#000000").replace("#", "");
    const full = h.length === 3 ? h.split("").map((c) => c + c).join("") : h.padEnd(6, "0");
    const r = parseInt(full.slice(0, 2), 16) || 0;
    const g = parseInt(full.slice(2, 4), 16) || 0;
    const b = parseInt(full.slice(4, 6), 16) || 0;
    return `rgba(${r}, ${g}, ${b}, ${Number(alpha).toFixed(3)})`;
  }

  /**
   * Parses "#rrggbb" into an RGB triple (short "#rgb" is not expanded).
   * @param {string} hex Color string.
   * @return {!Array<number>} [r, g, b], each 0..255.
   */
  function parseHex(hex) {
    const h = String(hex || "#000000").replace("#", "").padEnd(6, "0");
    return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16) || 0);
  }

  /**
   * Alpha-composites one RGB color over another ("over" operator).
   * @param {!Array<number>} top Foreground RGB.
   * @param {number} alpha Foreground opacity 0..1.
   * @param {!Array<number>} under Background RGB.
   * @return {!Array<number>} Composited RGB (not rounded).
   */
  function blend(top, alpha, under) {
    return top.map((c, i) => c * alpha + under[i] * (1 - alpha));
  }

  /**
   * WCAG 2.x relative luminance (sRGB linearization, Rec. 709 weights).
   * @param {!Array<number>} rgb RGB 0..255.
   * @return {number} Luminance 0..1.
   */
  function luminance(rgb) {
    const [r, g, b] = rgb.map((v) => {
      const c = v / 255;
      return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  }

  /**
   * WCAG contrast ratio between two colors.
   * @param {!Array<number>} a RGB 0..255.
   * @param {!Array<number>} b RGB 0..255.
   * @return {number} Ratio from 1 to 21.
   */
  function contrastRatio(a, b) {
    const la = luminance(a);
    const lb = luminance(b);
    return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
  }

  /**
   * Worst-case contrast of semi-transparent text over a semi-transparent
   * layer (box or outline). The video under the caption is unknown, so the
   * layer is composited over white and over black and the lower ratio wins.
   * @param {string} fillHex Text color "#rrggbb".
   * @param {number} fillAlpha Text opacity 0..1.
   * @param {string} layerHex Box/outline color "#rrggbb".
   * @param {number} layerAlpha Box/outline opacity 0..1.
   * @return {number} Ratio from 1 to 21.
   */
  function worstCaseContrast(fillHex, fillAlpha, layerHex, layerAlpha) {
    return Math.min(...[[255, 255, 255], [0, 0, 0]].map((backdrop) => {
      const bg = blend(parseHex(layerHex), layerAlpha, backdrop);
      const fg = blend(parseHex(fillHex), fillAlpha, bg);
      return contrastRatio(fg, bg);
    }));
  }

  /**
   * Maps a contrast ratio to the legibility badge level: < 3:1 "warn",
   * < 4.5:1 "info" (below WCAG AA for normal text), else "ok".
   * @param {number} ratio Contrast ratio.
   * @return {string} "warn", "info" or "ok".
   */
  function contrastLevel(ratio) {
    if (ratio < 3) return "warn";
    if (ratio < 4.5) return "info";
    return "ok";
  }

  // ---------------------------------------------------------------------------
  // Waveform geometry: viewport, x <-> ms, hit testing, cue drags
  // ---------------------------------------------------------------------------

  /**
   * Maps a time to an x coordinate in the visible window.
   * @param {number} ms Time in ms.
   * @param {number} w Canvas width in CSS px.
   * @param {{start: number, span: number}} vr Window from viewRange().
   * @return {number} x in CSS px (may fall outside 0..w).
   */
  const msToX = (ms, w, vr) => ((ms - vr.start) / vr.span) * w;

  /**
   * Maps an x coordinate in the visible window to a time.
   * @param {number} x x in CSS px.
   * @param {number} w Canvas width in CSS px.
   * @param {{start: number, span: number}} vr Window from viewRange().
   * @return {number} Time in ms.
   */
  const xToMs = (x, w, vr) => vr.start + (x / w) * vr.span;

  /**
   * Resolves a stored waveform view into a clamped visible window.
   * @param {number} dur Timeline duration in ms (> 0).
   * @param {{startMs: number, spanMs: number}} view state.view; spanMs 0
   *     means "fit the whole duration".
   * @return {{start: number, span: number, dur: number}} All in ms.
   */
  function computeViewRange(dur, view) {
    const span = view.spanMs > 0 ? Math.min(view.spanMs, dur) : dur;
    const start = Math.max(0, Math.min(view.startMs, dur - span));
    return { start, span, dur };
  }

  /**
   * Finds the cue block (and which part of it) under the pointer.
   * @param {!Array<!Object>} cues Cues with start_ms/end_ms, in time order.
   * @param {number} x Pointer x, CSS px relative to the canvas.
   * @param {number} y Pointer y, CSS px relative to the canvas.
   * @param {{w: number, h: number, vr: {start: number, span: number},
   *     laneH: number, edgePx: number}} geom Canvas size (CSS px), visible
   *     window, cue lane height and edge grab tolerance (CSS px).
   * @return {?{idx: number, mode: string}} mode is "start" or "end" (edge
   *     resize) or "move"; null when off the lane or far from any block.
   *     Gap hits also carry a `dist` field (px to the edge).
   */
  function hitTestCueAt(cues, x, y, geom) {
    const { w, h, vr, laneH, edgePx } = geom;
    // The lane accepts clicks slightly above its top edge.
    if (y < h - laneH - 8) return null;
    const spans = cues.map((cue) => [msToX(cue.start_ms, w, vr), msToX(cue.end_ms, w, vr)]);
    // Edge tolerance: up to edgePx but at most a third of a narrow block
    // (min 2 px), so the middle of short cues stays draggable.
    const tolFor = (x1, x2) => Math.min(edgePx, Math.max(2, (x2 - x1) / 3));
    // Inside a block only its own edges count (never the neighbor's).
    const insideIdx = spans.findIndex(([x1, x2]) => x >= x1 && x <= x2);
    if (insideIdx >= 0) {
      const [x1, x2] = spans[insideIdx];
      const tol = tolFor(x1, x2);
      if (x - x1 <= tol) return { idx: insideIdx, mode: "start" };
      if (x2 - x <= tol) return { idx: insideIdx, mode: "end" };
      return { idx: insideIdx, mode: "move" };
    }
    // In a gap: the nearest edge within tolerance.
    let edge = null;
    spans.forEach(([x1, x2], idx) => {
      const tol = tolFor(x1, x2);
      [["start", Math.abs(x - x1)], ["end", Math.abs(x - x2)]].forEach(([mode, dist]) => {
        if (dist <= tol && (!edge || dist < edge.dist)) edge = { idx, mode, dist };
      });
    });
    return edge;
  }

  /**
   * Computes the new times of a dragged cue from its original times: snaps
   * to 10 ms, keeps at least `minCueMs`, and never overlaps the neighbors
   * (previous cue end, next cue start, or 0 / `totalDur` at the ends). When
   * the neighbors leave less room than `minCueMs`, the lower bound wins so
   * the cue never crosses the previous cue.
   * @param {!Array<!Object>} cues Cues with start_ms/end_ms, in time order.
   * @param {{idx: number, mode: string, orig: {start: number, end: number}}}
   *     d state.drag of kind "cue"; mode "start" | "end" | "move".
   * @param {number} deltaMs Pointer offset from the grab point, ms.
   * @param {number} totalDur Timeline duration, ms (bound for the last cue).
   * @param {number} minCueMs Shortest cue a drag can produce, ms.
   * @return {{start: number, end: number}} New cue times, ms.
   */
  function computeCueDrag(cues, d, deltaMs, totalDur, minCueMs) {
    const prevEnd = d.idx > 0 ? cues[d.idx - 1].end_ms : 0;
    const nextStart = d.idx < cues.length - 1 ? cues[d.idx + 1].start_ms : totalDur;
    const { start, end } = d.orig;
    const snap = (v) => Math.round(v / 10) * 10;
    const clamp = (v, lo, hi) => Math.max(lo, Math.min(v, Math.max(lo, hi)));
    let ns = start;
    let ne = end;
    if (d.mode === "start") {
      ns = clamp(snap(start + deltaMs), prevEnd, end - minCueMs);
    } else if (d.mode === "end") {
      ne = clamp(snap(end + deltaMs), start + minCueMs, nextStart);
    } else {
      const len = end - start;
      ns = clamp(snap(start + deltaMs), prevEnd, nextStart - len);
      ne = ns + len;
    }
    return { start: ns, end: ne };
  }

  // ---------------------------------------------------------------------------
  // Background job polling
  // ---------------------------------------------------------------------------

  /**
   * Whether a finished transcription job's result must be discarded. The
   * server marks the result stale when the media changed while Whisper ran
   * (e.g. a new Resolve export); its cues belong to the old audio.
   * @param {?Object} job Finished job snapshot.
   * @return {boolean} True only for an explicit `result.stale === true`.
   */
  function isStaleJobResult(job) {
    return Boolean(job && job.result && job.result.stale === true);
  }

  /**
   * Creates the per-job poller used by the Studio UI. Each job gets its own
   * poller, so starting a second job never abandons the first. The next
   * request is scheduled only after the previous response arrives
   * (self-rescheduling timer, not setInterval), so slow responses never
   * overlap and `onSuccess` runs at most once. If the id is already polled
   * (the server can return the running job for a duplicate start), no second
   * poller is created; the job is just brought to the progress box.
   *
   * The progress box shows the most recently started active job with
   * showInBox (default). All DOM work is delegated to the hooks below so this
   * logic can run under Node with fake timers and a fake server.
   * @param {{fetchJson: function(string): !Promise<!Object>,
   *     setTimeout: (function(function(), number): *|undefined),
   *     clearTimeout: (function(*)|undefined),
   *     onShow: (function(string)|undefined),
   *     onRender: (function(!Object, number)|undefined),
   *     onIdle: (function(boolean)|undefined),
   *     onToast: (function(string, (number|undefined))|undefined)}} deps
   *     fetchJson GETs a JSON URL (rejects on network/HTTP errors);
   *     setTimeout/clearTimeout default to the globals; onShow(jobId) runs
   *     when a job becomes the shown one (unhide box and cancel button);
   *     onRender(job, others) paints the shown job's last snapshot ({} before
   *     the first response) plus the count of other boxed jobs; onIdle(
   *     succeeded) runs when the last boxed job ends; onToast(msg, ms) shows
   *     errors that have no onError handler.
   * @return {{pollJob: function(string, function(!Object), !Object=),
   *     size: number, activeJobId: ?string, has: function(string): boolean}}
   *     pollJob(jobId, onSuccess, opts) starts polling (see below); `size`
   *     and `activeJobId` are live getters.
   */
  function createJobPoller(deps) {
    const fetchJson = deps.fetchJson;
    const setTimer = deps.setTimeout || ((fn, ms) => setTimeout(fn, ms));
    const clearTimer = deps.clearTimeout || ((h) => clearTimeout(h));
    const noop = () => {};
    const onShow = deps.onShow || noop;
    const onRender = deps.onRender || noop;
    const onIdle = deps.onIdle || noop;
    const onToast = deps.onToast || noop;

    /**
     * Jobs currently being polled, by job id, in start order. Each entry:
     * {onSuccess, onError, onProgress, showInBox, intervalMs, timer, job}
     * where `timer` is the pending timer handle and `job` the last snapshot
     * from GET /api/jobs/<id> (null before the first response).
     * @type {!Map<string, !Object>}
     */
    const pollers = new Map();
    /** @type {?string} Job shown in the progress box (cancel target). */
    let activeJobId = null;

    /**
     * Polls a server job until it finishes and runs its callbacks.
     * @param {string} jobId Id returned by the job-starting endpoint.
     * @param {function(!Object)} onSuccess Receives the finished job (payload
     *     in job.result). May be async; a rejection is shown as a toast.
     * @param {{onError: (function(?Object, ?Error)|undefined),
     *     onProgress: (function(!Object)|undefined),
     *     showInBox: (boolean|undefined),
     *     intervalMs: (number|undefined)}=} opts onError receives the failed
     *     job, or null and the network/HTTP error; onProgress receives every
     *     snapshot; showInBox false keeps the job out of the box; intervalMs
     *     is the delay between polls (default 500).
     */
    function pollJob(jobId, onSuccess, opts = {}) {
      const existing = pollers.get(jobId);
      if (existing) {
        if (existing.showInBox) showJob(jobId);
        return;
      }
      const poller = {
        onSuccess,
        onError: opts.onError || null,
        onProgress: opts.onProgress || null,
        showInBox: opts.showInBox !== false,
        intervalMs: opts.intervalMs || 500,
        timer: null,
        job: null,
      };
      pollers.set(jobId, poller);
      if (poller.showInBox) showJob(jobId);
      poller.timer = setTimer(() => pollOnce(jobId), poller.intervalMs);
    }

    /**
     * One poll step: fetches the job, reports progress, then either finishes
     * the poller (done / error / cancelled / request failure) or schedules
     * the next step.
     * @async
     * @param {string} jobId Polled job id.
     * @return {!Promise<void>}
     */
    async function pollOnce(jobId) {
      const poller = pollers.get(jobId);
      if (!poller) return;
      let job;
      try {
        job = await fetchJson(`/api/jobs/${jobId}`);
      } catch (err) {
        if (pollers.get(jobId) !== poller) return;
        finish(jobId, false);
        if (poller.onError) poller.onError(null, err);
        else onToast(`Error consultando tarea: ${err.message}`);
        return;
      }
      // Guard against a poller removed while the request was in flight.
      if (pollers.get(jobId) !== poller) return;
      poller.job = job;
      if (poller.onProgress) poller.onProgress(job);
      if (activeJobId === jobId) render();

      if (job.status === "done") {
        finish(jobId, true);
        Promise.resolve()
          .then(() => poller.onSuccess(job))
          .catch((err) => onToast(err.message, 5000));
      } else if (job.status === "error" || job.status === "cancelled") {
        finish(jobId, false);
        if (poller.onError) poller.onError(job, null);
        else onToast(job.error || job.stage, 5000);
      } else {
        poller.timer = setTimer(() => pollOnce(jobId), poller.intervalMs);
      }
    }

    /**
     * Removes a finished job's poller and updates the progress box: if the
     * finished job was shown, the box switches to the newest remaining boxed
     * job; with none left onIdle(succeeded) runs.
     * @param {string} jobId Finished job id.
     * @param {boolean} succeeded Whether the job ended with status "done".
     */
    function finish(jobId, succeeded) {
      const poller = pollers.get(jobId);
      if (poller) clearTimer(poller.timer);
      pollers.delete(jobId);
      if (activeJobId !== jobId) {
        // Another job is shown: only its "+N en curso" count changed.
        if (activeJobId) render();
        return;
      }
      const next = newestBoxedJobId();
      if (next) {
        showJob(next);
        return;
      }
      activeJobId = null;
      onIdle(succeeded);
    }

    /**
     * Returns the most recently started job that is shown in the box.
     * @return {?string} Job id, or null when no boxed job is running.
     */
    function newestBoxedJobId() {
      let newest = null;
      pollers.forEach((p, id) => {
        if (p.showInBox) newest = id;
      });
      return newest;
    }

    /**
     * Makes a job the one shown in the box (and the cancel target).
     * @param {string} jobId Polled job id.
     */
    function showJob(jobId) {
      activeJobId = jobId;
      onShow(jobId);
      render();
    }

    /**
     * Hands the shown job's last snapshot and the number of other boxed jobs
     * to onRender(). No-op when no job is shown.
     */
    function render() {
      const poller = pollers.get(activeJobId);
      if (!poller) return;
      let others = -1;
      pollers.forEach((p) => {
        if (p.showInBox) others += 1;
      });
      onRender(poller.job || {}, others);
    }

    return {
      pollJob,
      has: (jobId) => pollers.has(jobId),
      get size() {
        return pollers.size;
      },
      get activeJobId() {
        return activeJobId;
      },
    };
  }

  return {
    ASCII_FOLD_MAP,
    blend,
    computeCueDrag,
    computeViewRange,
    contrastLevel,
    contrastRatio,
    cpsClassName,
    createJobPoller,
    findPreviewCue,
    foldSpanishToAsciiPreview,
    formatMsReadable,
    formatTick,
    hexToRgba,
    hitTestCueAt,
    isStaleJobResult,
    luminance,
    msToX,
    parseHex,
    rescaleWords,
    syncCueWordsAfterTextEdit,
    tokenizeLines,
    transformPreviewText,
    worstCaseContrast,
    xToMs,
  };
});
