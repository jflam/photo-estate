// stats-watch.js: stats.html's one loop (issue #80). Requests are settled by the test, timers fired by hand.
import test from "node:test";
import assert from "node:assert/strict";

import { statsWatch, problem, WORKING_MS, RUNNING_MS, IDLE_MS } from "../stats-watch.js";
import { ApiError, createApi } from "../api.js";
import { deferred, settled, timers } from "./helpers.js";

const UP = { enabled: true, running: false, waiting: { files: 0, bytes: 0 }, log: [] };
const FIGURES = { state: "ready", disk: {}, library: {} };

/** A watch whose requests the test answers: `asked` is every path, `answer(v)` settles the oldest. */
function rig() {
  const t = timers(), asked = [], shown = [];
  /** @type {ReturnType<typeof deferred>[]} */ const open = [];
  let flying = 0, most = 0;
  /** @type {import("../api.js").Api} */
  const api = async path => {
    asked.push(path);
    most = Math.max(most, ++flying);
    const d = deferred();
    open.push(d);
    try { return await d.promise; } finally { flying--; }
  };
  const w = statsWatch(api, {
    stats: d => shown.push(["stats", d.state]),
    statsError: e => shown.push(["statsError", String(/** @type {any} */ (e).message)]),
    uploads: u => shown.push(["uploads", u.running]),
    uploadsError: e => shown.push(["uploadsError", String(/** @type {any} */ (e).message)]),
  }, { setTimer: t.setTimer, clearTimer: t.clearTimer });
  const answer = async (/** @type {any} */ v) => { open.shift()?.resolve(v); await settled(); };
  const fail = async (/** @type {any} */ e) => { open.shift()?.reject(e); await settled(); };
  return { w, t, asked, shown, answer, fail, get most() { return most; }, get flying() { return flying; } };
}

test("the stats poll loop never has two requests in flight and shows an error on a non-OK response", async () => {
  const r = rig(), { w, t, asked, shown } = r;
  w.start();
  w.measure(); w.now(); w.retry();                         // clicks while the first request is on its way
  assert.deepEqual(asked, ["/api/ingest"]);
  await r.answer({ ...UP, running: true });
  assert.deepEqual(asked, ["/api/ingest", "/api/stats?refresh=1"]);
  await r.answer({ state: "working" });
  assert.equal(r.flying, 1, "the clicks made one more round, after the first, not beside it");
  await r.answer(UP);
  await r.answer({ state: "working", disk: {}, library: {} });
  assert.deepEqual(t.waits, [WORKING_MS], "one timer");
  t.fire();
  w.now();                                                 // the timer and a click together
  await r.answer(UP);
  await r.fail(new ApiError("HTTP 500", { kind: "http", status: 500 }));
  assert.deepEqual(shown.at(-1), ["statsError", "HTTP 500"], "a 500 is shown as an error");
  assert.ok(!shown.slice(-2).some(s => s[0] === "stats"), "and never drawn as figures");
  assert.equal(r.most, 1, "never two requests in flight");
  await r.answer(UP);                                      // the round the click asked for
  assert.deepEqual(asked.slice(-2), ["/api/stats", "/api/ingest"], "after an error the figures are not asked for again");
  assert.deepEqual(t.waits, [IDLE_MS], "and still exactly one timer");
  w.stop();
  assert.deepEqual(t.waits, []);
});

test("statsWatch: figures until they are ready, then only the uploads, at their own pace", async () => {
  const r = rig(), { w, t, asked, shown } = r;
  w.start();
  await r.answer({ ...UP, running: true });
  await r.answer({ state: "working" });
  assert.deepEqual(t.waits, [WORKING_MS]);
  t.fire();
  await r.answer({ ...UP, running: true });
  await r.answer(FIGURES);
  assert.deepEqual(t.waits, [RUNNING_MS], "a run is going: every 2 s");
  t.fire();
  await r.answer(UP);
  assert.deepEqual(t.waits, [IDLE_MS], "nothing running: every 15 s");
  assert.deepEqual(asked, ["/api/ingest", "/api/stats", "/api/ingest", "/api/stats", "/api/ingest"]);
  assert.deepEqual(shown.filter(s => s[0] === "stats"), [["stats", "working"], ["stats", "ready"]]);
  w.measure();                                             // "Measure again"
  await r.answer(UP);
  assert.equal(asked.at(-1), "/api/stats?refresh=1");
  await r.answer({ state: "working" });
  t.fire();
  await r.answer(UP);
  assert.equal(asked.at(-1), "/api/stats", "refresh is asked for once");
  w.stop();
});

test("statsWatch: with uploads off and the figures in, the loop ends; retry() and measure() start it again", async () => {
  const r = rig(), { w, t, asked } = r;
  w.start();
  await r.answer({ enabled: false });
  await r.answer(FIGURES);
  assert.equal(w.on, false);
  assert.deepEqual(t.waits, []);
  w.measure();
  assert.deepEqual(asked.slice(2), ["/api/stats?refresh=1"], "uploads are off: not asked about again");
  await r.answer(FIGURES);
  assert.equal(w.on, false);
});

test("statsWatch: the server's own { state: error } and a failed /api/ingest are both shown", async () => {
  const r = rig(), { w, t, shown } = r;
  w.start();
  await r.fail(new ApiError("The server did not answer", { kind: "network" }));
  await r.answer({ state: "error", error: "The disk could not be read" });
  assert.deepEqual(shown, [["uploadsError", "The server did not answer"], ["statsError", "The disk could not be read"]]);
  assert.deepEqual(t.waits, [IDLE_MS], "the uploads are asked about again later");
  w.retry();
  await r.answer(UP);
  await r.answer(FIGURES);
  assert.deepEqual(shown.at(-1), ["stats", "ready"]);
  w.stop();
});

test("statsWatch: through api.js, a 500 from the server is an error with the server's words", async () => {
  const shown = [];
  const api = createApi({ fetch: /** @type {any} */ (async (/** @type {string} */ url) => url === "/api/ingest"
    ? new Response(JSON.stringify({ enabled: false }), { status: 200 })
    : new Response(JSON.stringify({ error: "The catalog is locked" }), { status: 500, headers: { "Content-Type": "application/json" } })) });
  const w = statsWatch(api, { stats: () => shown.push("stats"), statsError: e => shown.push(problem(e)), uploads: () => {} });
  w.start();
  for (let i = 0; i < 10 && !shown.length; i++) await new Promise(r => setTimeout(r, 1));
  assert.deepEqual(shown, [{ title: "Couldn’t take stock", text: "The catalog is locked" }]);
  assert.equal(w.on, false);
});

test("problem: what a person is told", () => {
  assert.deepEqual(problem(new ApiError("The server did not answer", { kind: "network" })), { title: "Can’t reach the library", text: "The server didn’t answer." });
  assert.equal(problem(new ApiError("The server took too long to answer", { kind: "timeout" })).title, "Can’t reach the library");
  assert.deepEqual(problem(new ApiError("HTTP 500", { kind: "http", status: 500 })), { title: "Couldn’t take stock", text: "HTTP 500" });
  assert.equal(problem(new Error("")).text, "The server gave no reason.");
});
