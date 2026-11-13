/**
 * @fileoverview Unit tests for the pure helpers in ui/app_helpers.js: text
 * folding and timecodes, WCAG contrast, waveform geometry (x <-> ms, hit
 * testing) and cue drag clamping.
 *
 * Run with the built-in Node test runner (no npm packages):
 *   node --test tests/js/
 */
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { describe, test } from "node:test";

const require = createRequire(import.meta.url);
const H = require("../../ui/app_helpers.js");

/**
 * Builds a cue with the fields the helpers read.
 * @param {number} start Start, ms.
 * @param {number} end End, ms.
 * @param {!Object=} extra Extra fields (text, lines, words...).
 * @return {!Object} Cue.
 */
function cue(start, end, extra = {}) {
  return { start_ms: start, end_ms: end, ...extra };
}

/**
 * Asserts two floats are equal up to rounding noise.
 * @param {number} actual Computed value.
 * @param {number} expected Expected value.
 * @param {number=} eps Allowed absolute difference.
 */
function assertClose(actual, expected, eps = 1e-9) {
  assert.ok(Math.abs(actual - expected) <= eps, `${actual} != ${expected}`);
}

describe("module shape", () => {
  test("exports every helper app.js destructures", () => {
    for (const name of [
      "computeCueDrag",
      "computeViewRange",
      "contrastLevel",
      "cpsClassName",
      "createJobPoller",
      "findPreviewCue",
      "formatMsReadable",
      "formatTick",
      "hexToRgba",
      "hitTestCueAt",
      "isStaleJobResult",
      "msToX",
      "rescaleWords",
      "syncCueWordsAfterTextEdit",
      "transformPreviewText",
      "worstCaseContrast",
      "xToMs",
    ]) {
      assert.equal(typeof H[name], "function", name);
    }
  });
});

describe("foldSpanishToAsciiPreview", () => {
  test("folds accents, ñ and drops inverted ¿ ¡", () => {
    assert.equal(
      H.foldSpanishToAsciiPreview("¿Qué tal, señor? ¡Año único!"),
      "Que tal, senor? Ano unico!"
    );
  });

  test("folds uppercase letters and diaeresis", () => {
    assert.equal(H.foldSpanishToAsciiPreview("ÑANDÚ PINGÜINO ÁÉÍÓÚÜ"), "NANDU PINGUINO AEIOUU");
  });

  test("straightens quotes, dashes and the ellipsis", () => {
    assert.equal(
      H.foldSpanishToAsciiPreview("«hola» “sí” ‘no’ — a – b…"),
      '"hola" "si" \'no\' - a - b...'
    );
  });

  test("decomposed input and other accents fold through NFKD", () => {
    assert.equal(H.foldSpanishToAsciiPreview("n\u0303o\u0301"), "no");
    assert.equal(H.foldSpanishToAsciiPreview("àçê"), "ace");
  });

  test("null, undefined and empty become an empty string", () => {
    assert.equal(H.foldSpanishToAsciiPreview(null), "");
    assert.equal(H.foldSpanishToAsciiPreview(undefined), "");
    assert.equal(H.foldSpanishToAsciiPreview(""), "");
  });
});

describe("transformPreviewText", () => {
  test("folds before uppercasing, like the exporters", () => {
    assert.equal(
      H.transformPreviewText("¿ñandú?", { ascii_fold: true, uppercase: true }),
      "NANDU?"
    );
  });

  test("uppercase alone keeps Spanish letters", () => {
    assert.equal(H.transformPreviewText("¿ñandú?", { uppercase: true }), "¿ÑANDÚ?");
  });

  test("no flags returns the text unchanged", () => {
    assert.equal(H.transformPreviewText("¿ñandú?", {}), "¿ñandú?");
  });
});

describe("formatMsReadable / formatTick", () => {
  test("formats MM:SS.cc", () => {
    assert.equal(H.formatMsReadable(64250), "01:04.25");
    assert.equal(H.formatMsReadable(0), "00:00.00");
    assert.equal(H.formatMsReadable(59999), "00:59.99");
  });

  test("negative, missing and fractional values clamp/floor", () => {
    assert.equal(H.formatMsReadable(-500), "00:00.00");
    assert.equal(H.formatMsReadable(undefined), "00:00.00");
    assert.equal(H.formatMsReadable(1009.9), "00:01.00");
  });

  test("ruler ticks gain a decimal below 1 s steps", () => {
    assert.equal(H.formatTick(65000, 5000), "1:05");
    assert.equal(H.formatTick(65500, 500), "1:05.5");
    assert.equal(H.formatTick(500, 250), "0:00.5");
  });
});

describe("cpsClassName", () => {
  test("thresholds are strictly above 17 and 20 CPS", () => {
    assert.equal(H.cpsClassName(17), "cue-cps");
    assert.equal(H.cpsClassName(17.1), "pill pill-warn");
    assert.equal(H.cpsClassName(20), "pill pill-warn");
    assert.equal(H.cpsClassName(20.1), "pill pill-danger");
    assert.equal(H.cpsClassName(undefined), "cue-cps");
  });
});

describe("tokens and word sync", () => {
  test("tokenizeLines splits on any whitespace and skips empties", () => {
    assert.deepEqual(H.tokenizeLines(["  ¿Qué  tal? ", "muy\tbien"]), [
      "¿Qué",
      "tal?",
      "muy",
      "bien",
    ]);
    assert.deepEqual(H.tokenizeLines(null), []);
  });

  test("word texts follow the edit when the token count matches", () => {
    const c = cue(0, 1000, {
      lines: ["Hola", "Señor"],
      words: [
        { text: "hola", start_ms: 0, end_ms: 400 },
        { text: "senor", start_ms: 500, end_ms: 900 },
      ],
    });
    H.syncCueWordsAfterTextEdit(c);
    assert.deepEqual(
      c.words.map((w) => w.text),
      ["Hola", "Señor"]
    );
    assert.equal(c.words[1].start_ms, 500);
  });

  test("words are left untouched when the token count changes", () => {
    const words = [{ text: "hola", start_ms: 0, end_ms: 400 }];
    const c = cue(0, 1000, { lines: ["Hola de nuevo"], words });
    H.syncCueWordsAfterTextEdit(c);
    assert.equal(c.words[0].text, "hola");
  });
});

describe("rescaleWords", () => {
  const words = [
    { text: "a", start_ms: 1000, end_ms: 1500, conf: 0.9 },
    { text: "b", start_ms: 1500, end_ms: 2000 },
  ];

  test("empty or missing words give []", () => {
    assert.deepEqual(H.rescaleWords([], 0, 1, 0, 1), []);
    assert.deepEqual(H.rescaleWords(null, 0, 1, 0, 1), []);
  });

  test("stretches and shifts linearly, keeping other fields", () => {
    const out = H.rescaleWords(words, 1000, 2000, 3000, 5000);
    assert.deepEqual(out, [
      { text: "a", start_ms: 3000, end_ms: 4000, conf: 0.9 },
      { text: "b", start_ms: 4000, end_ms: 5000 },
    ]);
  });

  test("does not mutate the input words", () => {
    H.rescaleWords(words, 1000, 2000, 0, 100);
    assert.equal(words[0].start_ms, 1000);
  });

  test("zero old duration divides by 1 ms instead of 0", () => {
    const out = H.rescaleWords([{ start_ms: 1000, end_ms: 1000 }], 1000, 1000, 2000, 2500);
    assert.deepEqual(out, [{ start_ms: 2000, end_ms: 2000 }]);
    for (const w of out) assert.ok(Number.isFinite(w.start_ms) && Number.isFinite(w.end_ms));
  });

  test("inverted (negative) old span is also floored to 1 ms", () => {
    const out = H.rescaleWords([{ start_ms: 1000, end_ms: 1001 }], 1000, 900, 0, 10);
    assert.deepEqual(out, [{ start_ms: 0, end_ms: 10 }]);
  });

  test("results are rounded to whole ms", () => {
    const out = H.rescaleWords([{ start_ms: 0, end_ms: 1 }], 0, 3, 0, 1);
    assert.deepEqual(out, [{ start_ms: 0, end_ms: 0 }]);
  });
});

describe("findPreviewCue", () => {
  const cues = [
    cue(0, 1000, { text: "a" }),
    cue(900, 2000, { text: "b" }),
    cue(3000, 4000, { text: "c" }),
  ];

  test("bounds are inclusive", () => {
    assert.equal(H.findPreviewCue(cues, 0, -1).text, "a");
    assert.equal(H.findPreviewCue(cues, 2000, -1).text, "b");
  });

  test("overlapping cues resolve to the first one", () => {
    assert.equal(H.findPreviewCue(cues, 950, -1).text, "a");
  });

  test("gaps fall back to the active cue, or null", () => {
    assert.equal(H.findPreviewCue(cues, 2500, 2).text, "c");
    assert.equal(H.findPreviewCue(cues, 2500, -1), null);
    assert.equal(H.findPreviewCue([], 0, -1), null);
  });
});

describe("colors and WCAG contrast", () => {
  const WHITE = [255, 255, 255];
  const BLACK = [0, 0, 0];

  test("parseHex reads #rrggbb, pads short input and defaults to black", () => {
    assert.deepEqual(H.parseHex("#ffd43b"), [255, 212, 59]);
    assert.deepEqual(H.parseHex("ff"), [255, 0, 0]);
    assert.deepEqual(H.parseHex(null), [0, 0, 0]);
    assert.deepEqual(H.parseHex("#zzzzzz"), [0, 0, 0]);
  });

  test("hexToRgba expands #rgb and formats alpha with 3 decimals", () => {
    assert.equal(H.hexToRgba("#fff", 0.5), "rgba(255, 255, 255, 0.500)");
    assert.equal(H.hexToRgba("ffd43b", 0.75), "rgba(255, 212, 59, 0.750)");
    assert.equal(H.hexToRgba(undefined, 1), "rgba(0, 0, 0, 1.000)");
  });

  test("blend composites with the over operator", () => {
    assert.deepEqual(H.blend(WHITE, 0.5, BLACK), [127.5, 127.5, 127.5]);
    assert.deepEqual(H.blend(WHITE, 0, BLACK), BLACK);
    assert.deepEqual(H.blend(WHITE, 1, BLACK), WHITE);
  });

  test("luminance and contrastRatio match WCAG endpoints", () => {
    assertClose(H.luminance(WHITE), 1);
    assert.equal(H.luminance(BLACK), 0);
    assertClose(H.contrastRatio(WHITE, BLACK), 21);
    assert.equal(H.contrastRatio(BLACK, WHITE), H.contrastRatio(WHITE, BLACK));
    assert.equal(H.contrastRatio([90, 90, 90], [90, 90, 90]), 1);
  });

  test("known WCAG pairs land on the expected side of 3:1 and 4.5:1", () => {
    // #777777 on white is the classic "just below AA" grey (~4.48:1).
    const grey = H.contrastRatio(H.parseHex("#777777"), WHITE);
    assert.ok(grey > 4.4 && grey < 4.5, String(grey));
    assert.equal(H.contrastLevel(grey), "info");
    // #767676 on white is the darkest grey that passes AA (~4.54:1).
    assert.equal(H.contrastLevel(H.contrastRatio(H.parseHex("#767676"), WHITE)), "ok");
  });

  test("contrastLevel thresholds: < 3 warn, < 4.5 info, else ok", () => {
    assert.equal(H.contrastLevel(1), "warn");
    assert.equal(H.contrastLevel(2.999), "warn");
    assert.equal(H.contrastLevel(3), "info");
    assert.equal(H.contrastLevel(4.499), "info");
    assert.equal(H.contrastLevel(4.5), "ok");
    assert.equal(H.contrastLevel(21), "ok");
  });

  test("worstCaseContrast: opaque box gives the plain ratio", () => {
    assertClose(H.worstCaseContrast("#ffffff", 1, "#000000", 1), 21);
  });

  test("worstCaseContrast: a translucent box is judged on the worse backdrop", () => {
    const ratio = H.worstCaseContrast("#ffffff", 1, "#000000", 0.5);
    // Over white the 50 % black box is mid grey, which caps the contrast.
    const overWhite = H.contrastRatio(WHITE, H.blend(BLACK, 0.5, WHITE));
    assert.equal(ratio, overWhite);
    assert.ok(ratio < 21);
  });

  test("worstCaseContrast: invisible box and text collapse to 1:1", () => {
    assert.equal(H.worstCaseContrast("#ffffff", 0, "#000000", 0), 1);
  });
});

describe("waveform geometry", () => {
  const vr = { start: 1000, span: 4000 };

  test("msToX / xToMs are inverse maps of the visible window", () => {
    assert.equal(H.msToX(1000, 800, vr), 0);
    assert.equal(H.msToX(5000, 800, vr), 800);
    assert.equal(H.msToX(0, 800, vr), -200);
    assert.equal(H.xToMs(400, 800, vr), 3000);
    for (const ms of [0, 1234, 4999, 9000]) {
      assert.ok(Math.abs(H.xToMs(H.msToX(ms, 800, vr), 800, vr) - ms) < 1e-9);
    }
  });

  test("computeViewRange: spanMs 0 fits the whole duration", () => {
    assert.deepEqual(H.computeViewRange(10000, { startMs: 3000, spanMs: 0 }), {
      start: 0,
      span: 10000,
      dur: 10000,
    });
  });

  test("computeViewRange clamps the span to the duration and the start inside it", () => {
    assert.deepEqual(H.computeViewRange(10000, { startMs: 9000, spanMs: 4000 }), {
      start: 6000,
      span: 4000,
      dur: 10000,
    });
    assert.deepEqual(H.computeViewRange(10000, { startMs: -50, spanMs: 4000 }), {
      start: 0,
      span: 4000,
      dur: 10000,
    });
    assert.deepEqual(H.computeViewRange(3000, { startMs: 500, spanMs: 9000 }), {
      start: 0,
      span: 3000,
      dur: 3000,
    });
  });
});

describe("hitTestCueAt", () => {
  // 1000 px for 10 s: 1 px = 10 ms. Lane is the bottom 26 px of a 100 px canvas.
  const geom = { w: 1000, h: 100, vr: { start: 0, span: 10000 }, laneH: 26, edgePx: 8 };
  const cues = [cue(1000, 2000), cue(2000, 3000), cue(5000, 5030)];

  test("pointer above the lane (minus 8 px slack) misses", () => {
    assert.equal(H.hitTestCueAt(cues, 150, 65, geom), null);
    assert.deepEqual(H.hitTestCueAt(cues, 150, 66, geom), { idx: 0, mode: "move" });
  });

  test("inside a block: edges resize, the middle moves", () => {
    assert.deepEqual(H.hitTestCueAt(cues, 103, 90, geom), { idx: 0, mode: "start" });
    assert.deepEqual(H.hitTestCueAt(cues, 150, 90, geom), { idx: 0, mode: "move" });
    assert.deepEqual(H.hitTestCueAt(cues, 195, 90, geom), { idx: 0, mode: "end" });
  });

  test("touching cues: the shared edge belongs to the first block found", () => {
    assert.deepEqual(H.hitTestCueAt(cues, 200, 90, geom), { idx: 0, mode: "end" });
    assert.deepEqual(H.hitTestCueAt(cues, 202, 90, geom), { idx: 1, mode: "start" });
  });

  test("narrow blocks shrink the edge tolerance to a third (min 2 px)", () => {
    // Cue 2 spans x 500..503 (3 px): tolerance is 2 px, nothing is "move".
    assert.deepEqual(H.hitTestCueAt(cues, 501, 90, geom), { idx: 2, mode: "start" });
    assert.deepEqual(H.hitTestCueAt(cues, 503, 90, geom), { idx: 2, mode: "end" });
  });

  test("in a gap the nearest edge within tolerance wins and reports dist", () => {
    assert.deepEqual(H.hitTestCueAt(cues, 95, 90, geom), { idx: 0, mode: "start", dist: 5 });
    assert.deepEqual(H.hitTestCueAt(cues, 306, 90, geom), { idx: 1, mode: "end", dist: 6 });
  });

  test("far from every block misses; no cues misses", () => {
    assert.equal(H.hitTestCueAt(cues, 400, 90, geom), null);
    assert.equal(H.hitTestCueAt([], 400, 90, geom), null);
  });
});

describe("computeCueDrag", () => {
  const MIN = 200;
  const cues = [cue(0, 1000), cue(2000, 3000), cue(4000, 5000)];
  const drag = (idx, mode) => ({
    idx,
    mode,
    orig: { start: cues[idx].start_ms, end: cues[idx].end_ms },
  });

  test("snaps to 10 ms", () => {
    assert.deepEqual(H.computeCueDrag(cues, drag(1, "move"), 123, 10000, MIN), {
      start: 2120,
      end: 3120,
    });
    assert.deepEqual(H.computeCueDrag(cues, drag(1, "start"), -14, 10000, MIN), {
      start: 1990,
      end: 3000,
    });
  });

  test("start edge stops at the previous cue end and at the minimum length", () => {
    assert.deepEqual(H.computeCueDrag(cues, drag(1, "start"), -5000, 10000, MIN), {
      start: 1000,
      end: 3000,
    });
    assert.deepEqual(H.computeCueDrag(cues, drag(1, "start"), 5000, 10000, MIN), {
      start: 2800,
      end: 3000,
    });
  });

  test("end edge stops at the next cue start and at the minimum length", () => {
    assert.deepEqual(H.computeCueDrag(cues, drag(1, "end"), 5000, 10000, MIN), {
      start: 2000,
      end: 4000,
    });
    assert.deepEqual(H.computeCueDrag(cues, drag(1, "end"), -5000, 10000, MIN), {
      start: 2000,
      end: 2200,
    });
  });

  test("move keeps the length and stays between both neighbours", () => {
    assert.deepEqual(H.computeCueDrag(cues, drag(1, "move"), -5000, 10000, MIN), {
      start: 1000,
      end: 2000,
    });
    assert.deepEqual(H.computeCueDrag(cues, drag(1, "move"), 5000, 10000, MIN), {
      start: 3000,
      end: 4000,
    });
  });

  test("first cue is bounded by 0, last cue by the total duration", () => {
    assert.deepEqual(H.computeCueDrag(cues, drag(0, "move"), -700, 10000, MIN), {
      start: 0,
      end: 1000,
    });
    assert.deepEqual(H.computeCueDrag(cues, drag(2, "end"), 99999, 6000, MIN), {
      start: 4000,
      end: 6000,
    });
    assert.deepEqual(H.computeCueDrag(cues, drag(2, "move"), 99999, 6000, MIN), {
      start: 5000,
      end: 6000,
    });
  });

  test("no room between overlapping neighbours: never crosses the previous cue", () => {
    const tight = [cue(0, 1500), cue(1000, 1800), cue(1600, 2500)];
    const d = { idx: 1, mode: "move", orig: { start: 1000, end: 1800 } };
    // prevEnd 1500 > nextStart - len 800: the lower bound wins.
    assert.deepEqual(H.computeCueDrag(tight, d, 0, 10000, MIN), { start: 1500, end: 2300 });
    const e = { idx: 1, mode: "end", orig: { start: 1000, end: 1800 } };
    // The end edge still stops at the next cue start.
    assert.deepEqual(H.computeCueDrag(tight, e, 900, 10000, MIN), { start: 1000, end: 1600 });
  });

  test("negative deltas past zero clamp to zero for the first cue start", () => {
    const d = { idx: 0, mode: "start", orig: { start: 300, end: 1000 } };
    assert.deepEqual(H.computeCueDrag(cues, d, -1000, 10000, MIN), { start: 0, end: 1000 });
  });
});
