/**
 * @fileoverview Unit tests for createJobPoller() in ui/app_helpers.js, the
 * per-job polling logic behind pollJob() in ui/app.js.
 *
 * The poller gets a fake server (every GET /api/jobs/<id> returns a promise
 * the test settles by hand) and fake timers, so the tests control exactly
 * when polls fire and when responses arrive. That makes "no overlapping
 * requests" and "onSuccess at most once" deterministic to check.
 *
 * Run with the built-in Node test runner (no npm packages):
 *   node --test tests/js/
 */
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { describe, test } from "node:test";

const require = createRequire(import.meta.url);
const { createJobPoller, isStaleJobResult } = require("../../ui/app_helpers.js");

/**
 * Lets pending promise continuations (microtasks) run.
 * @return {!Promise<void>}
 */
const flush = () => new Promise((resolve) => setImmediate(resolve));

/**
 * Builds a poller wired to a hand-driven fake server and fake timers.
 * @return {!Object} {poller, events, timers, pending, fireTimers, respond,
 *     fail, requests}. `events` records every hook call in order.
 */
function harness() {
  const timers = new Map();
  let timerSeq = 0;
  const pending = [];
  const requests = [];
  const events = [];
  const poller = createJobPoller({
    fetchJson: (url) =>
      new Promise((resolve, reject) => {
        const jobId = url.replace("/api/jobs/", "");
        requests.push(jobId);
        pending.push({ jobId, resolve, reject });
      }),
    setTimeout: (fn, ms) => {
      timerSeq += 1;
      timers.set(timerSeq, { fn, ms });
      return timerSeq;
    },
    clearTimeout: (id) => {
      timers.delete(id);
    },
    onShow: (jobId) => events.push(["show", jobId]),
    onRender: (job, others) => events.push(["render", job.stage ?? null, others]),
    onIdle: (succeeded) => events.push(["idle", succeeded]),
    onToast: (msg, ms) => events.push(["toast", msg, ms]),
  });

  /**
   * Fires every timer scheduled so far, then lets the polls start.
   * @return {!Promise<void>}
   */
  async function fireTimers() {
    const due = [...timers.values()];
    timers.clear();
    due.forEach((t) => t.fn());
    await flush();
  }

  /**
   * Settles the oldest in-flight request of a job.
   * @param {string} jobId Job id.
   * @param {function(!Object)} fn Receives the pending entry and settles it.
   * @return {!Promise<void>}
   */
  async function settle(jobId, fn) {
    const i = pending.findIndex((p) => p.jobId === jobId);
    assert.ok(i >= 0, `no request in flight for ${jobId}`);
    const [entry] = pending.splice(i, 1);
    fn(entry);
    await flush();
  }

  return {
    poller,
    events,
    timers,
    pending,
    requests,
    fireTimers,
    respond: (jobId, snap) => settle(jobId, (p) => p.resolve(snap)),
    fail: (jobId, err) => settle(jobId, (p) => p.reject(err)),
    /** @return {number} In-flight requests for one job. */
    inFlight: (jobId) => pending.filter((p) => p.jobId === jobId).length,
  };
}

describe("createJobPoller: lifecycle", () => {
  test("polls until done and calls onSuccess exactly once", async () => {
    const h = harness();
    const done = [];
    h.poller.pollJob("A", (job) => done.push(job));
    assert.equal(h.poller.size, 1);
    assert.equal(h.poller.activeJobId, "A");
    assert.deepEqual(h.events, [
      ["show", "A"],
      ["render", null, 0],
    ]);
    assert.equal([...h.timers.values()][0].ms, 500);

    await h.fireTimers();
    await h.respond("A", { status: "running", progress: 40, stage: "Transcribiendo" });
    assert.equal(h.timers.size, 1);
    await h.fireTimers();
    await h.respond("A", { status: "done", progress: 100, stage: "Listo", result: { cues: [] } });

    assert.equal(done.length, 1);
    assert.equal(done[0].stage, "Listo");
    assert.equal(h.poller.size, 0);
    assert.equal(h.poller.activeJobId, null);
    assert.equal(h.timers.size, 0);
    assert.deepEqual(h.events.slice(-3), [
      ["render", "Transcribiendo", 0],
      ["render", "Listo", 0],
      ["idle", true],
    ]);

    // Nothing left to fire: no extra request and no second onSuccess.
    await h.fireTimers();
    assert.deepEqual(h.requests, ["A", "A"]);
    assert.equal(done.length, 1);
  });

  test("never overlaps requests: the next poll waits for the response", async () => {
    const h = harness();
    h.poller.pollJob("A", () => {}, { intervalMs: 50 });
    await h.fireTimers();
    assert.equal(h.inFlight("A"), 1);
    // A slow response: no timer is pending while the request is in flight.
    assert.equal(h.timers.size, 0);
    await h.fireTimers();
    await h.fireTimers();
    assert.equal(h.inFlight("A"), 1);
    assert.equal(h.requests.length, 1);
    await h.respond("A", { status: "running" });
    assert.equal(h.timers.size, 1);
    assert.equal([...h.timers.values()][0].ms, 50);
    assert.equal(h.inFlight("A"), 0);
  });

  test("onProgress receives every snapshot, including the final one", async () => {
    const h = harness();
    const seen = [];
    h.poller.pollJob("A", () => {}, { onProgress: (job) => seen.push(job.progress) });
    await h.fireTimers();
    await h.respond("A", { status: "running", progress: 10 });
    await h.fireTimers();
    await h.respond("A", { status: "done", progress: 100 });
    assert.deepEqual(seen, [10, 100]);
  });

  test("duplicate job id keeps a single poller and the first callback", async () => {
    const h = harness();
    const calls = [];
    h.poller.pollJob("A", () => calls.push("first"));
    h.poller.pollJob("A", () => calls.push("dup"), { intervalMs: 5 });
    assert.equal(h.poller.size, 1);
    assert.equal(h.timers.size, 1);
    // The duplicate start brings the job back to the box.
    assert.deepEqual(
      h.events.filter((e) => e[0] === "show"),
      [
        ["show", "A"],
        ["show", "A"],
      ]
    );

    await h.fireTimers();
    assert.equal(h.requests.length, 1);
    await h.respond("A", { status: "done" });
    assert.deepEqual(calls, ["first"]);
  });

  test("duplicate of a background job (showInBox false) is not shown", () => {
    const h = harness();
    h.poller.pollJob("bench", () => {}, { showInBox: false });
    h.poller.pollJob("bench", () => {});
    assert.equal(h.poller.size, 1);
    assert.equal(h.poller.activeJobId, null);
    assert.deepEqual(h.events, []);
  });
});

describe("createJobPoller: failures", () => {
  test("cancelled job: toast with the stage, no onSuccess, box stays", async () => {
    const h = harness();
    let succeeded = false;
    h.poller.pollJob("A", () => {
      succeeded = true;
    });
    await h.fireTimers();
    await h.respond("A", { status: "cancelled", stage: "Cancelado por el usuario" });
    assert.equal(succeeded, false);
    assert.equal(h.poller.size, 0);
    assert.deepEqual(h.events.slice(-2), [
      ["idle", false],
      ["toast", "Cancelado por el usuario", 5000],
    ]);
  });

  test("cancelled job with onError: handler instead of toast", async () => {
    const h = harness();
    const errors = [];
    h.poller.pollJob("A", () => {}, { onError: (job, err) => errors.push([job.status, err]) });
    await h.fireTimers();
    await h.respond("A", { status: "cancelled" });
    assert.deepEqual(errors, [["cancelled", null]]);
    assert.equal(
      h.events.some((e) => e[0] === "toast"),
      false
    );
  });

  test("error job: toast prefers job.error over the stage", async () => {
    const h = harness();
    h.poller.pollJob("A", () => {});
    await h.fireTimers();
    await h.respond("A", { status: "error", error: "whisper-cli falló", stage: "Error" });
    assert.deepEqual(h.events.at(-1), ["toast", "whisper-cli falló", 5000]);
    assert.equal(h.timers.size, 0);
  });

  test("request failure stops polling and reports the error", async () => {
    const h = harness();
    h.poller.pollJob("A", () => {});
    await h.fireTimers();
    await h.fail("A", new Error("HTTP 500"));
    assert.equal(h.poller.size, 0);
    assert.equal(h.timers.size, 0);
    assert.deepEqual(h.events.slice(-2), [
      ["idle", false],
      ["toast", "Error consultando tarea: HTTP 500", undefined],
    ]);
  });

  test("request failure with onError: receives (null, err)", async () => {
    const h = harness();
    const got = [];
    h.poller.pollJob("A", () => {}, { onError: (job, err) => got.push([job, err.message]) });
    await h.fireTimers();
    await h.fail("A", new Error("offline"));
    assert.deepEqual(got, [[null, "offline"]]);
  });

  test("a rejecting onSuccess is shown as a toast, still only once", async () => {
    const h = harness();
    let calls = 0;
    h.poller.pollJob("A", async () => {
      calls += 1;
      throw new Error("No se pudo aplicar");
    });
    await h.fireTimers();
    await h.respond("A", { status: "done" });
    await flush();
    assert.equal(calls, 1);
    assert.deepEqual(h.events.at(-1), ["toast", "No se pudo aplicar", 5000]);
  });
});

describe("createJobPoller: stale results", () => {
  test("a stale transcription reaches onSuccess with the flag intact", async () => {
    const h = harness();
    const results = [];
    h.poller.pollJob("T", (job) => results.push(isStaleJobResult(job)));
    await h.fireTimers();
    await h.respond("T", { status: "done", result: { stale: true } });
    h.poller.pollJob("U", (job) => results.push(isStaleJobResult(job)));
    await h.fireTimers();
    await h.respond("U", { status: "done", result: { cues: [] } });
    assert.deepEqual(results, [true, false]);
  });

  test("isStaleJobResult only accepts an explicit boolean true", () => {
    assert.equal(isStaleJobResult({ result: { stale: true } }), true);
    assert.equal(isStaleJobResult({ result: { stale: "true" } }), false);
    assert.equal(isStaleJobResult({ result: { stale: 1 } }), false);
    assert.equal(isStaleJobResult({ result: null }), false);
    assert.equal(isStaleJobResult({}), false);
    assert.equal(isStaleJobResult(null), false);
  });
});

describe("createJobPoller: which job the box shows", () => {
  test("newest boxed job is shown; background jobs never are", async () => {
    const h = harness();
    h.poller.pollJob("A", () => {});
    h.poller.pollJob("B", () => {});
    h.poller.pollJob("bench", () => {}, { showInBox: false });
    assert.equal(h.poller.activeJobId, "B");
    assert.equal(h.poller.size, 3);
    // B is rendered with one other boxed job (A) running.
    assert.deepEqual(h.events.at(-1), ["render", null, 1]);
    assert.equal(
      h.events.some((e) => e[1] === "bench"),
      false
    );
  });

  test("when the shown job ends, the newest remaining boxed job takes over", async () => {
    const h = harness();
    h.poller.pollJob("A", () => {});
    h.poller.pollJob("B", () => {});
    h.poller.pollJob("bench", () => {}, { showInBox: false });
    await h.fireTimers();
    await h.respond("B", { status: "done", stage: "B ok" });
    assert.equal(h.poller.activeJobId, "A");
    assert.deepEqual(h.events.slice(-2), [
      ["show", "A"],
      ["render", null, 0],
    ]);
    assert.equal(
      h.events.some((e) => e[0] === "idle"),
      false
    );

    // The background job finishing does not touch the shown job.
    await h.respond("bench", { status: "done" });
    assert.equal(h.poller.activeJobId, "A");

    await h.respond("A", { status: "done", stage: "A ok" });
    assert.equal(h.poller.activeJobId, null);
    assert.deepEqual(h.events.at(-1), ["idle", true]);
  });

  test("an older job ending only refreshes the shown job's count", async () => {
    const h = harness();
    h.poller.pollJob("A", () => {});
    h.poller.pollJob("B", () => {});
    await h.fireTimers();
    await h.respond("B", { status: "running", stage: "B1" });
    await h.respond("A", { status: "error", stage: "A falló" });
    assert.equal(h.poller.activeJobId, "B");
    assert.deepEqual(h.events.slice(-2), [
      ["render", "B1", 0],
      ["toast", "A falló", 5000],
    ]);
  });

  test("progress of a hidden job does not repaint the box", async () => {
    const h = harness();
    h.poller.pollJob("A", () => {});
    h.poller.pollJob("B", () => {});
    await h.fireTimers();
    const before = h.events.length;
    await h.respond("A", { status: "running", stage: "A1" });
    assert.equal(h.events.length, before);
  });

  test("default timers are the globals when none are injected", async () => {
    let calls = 0;
    const poller = createJobPoller({
      fetchJson: async () => {
        calls += 1;
        return { status: "done" };
      },
    });
    const done = new Promise((resolve) => poller.pollJob("A", resolve, { intervalMs: 1 }));
    const job = await done;
    assert.equal(job.status, "done");
    assert.equal(calls, 1);
    assert.equal(poller.size, 0);
  });
});
