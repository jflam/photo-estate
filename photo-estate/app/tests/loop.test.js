// loop.js: loop() and until() (issue #80). Timers are fired by hand.
import test from "node:test";
import assert from "node:assert/strict";

import { loop, until } from "../loop.js";
import { isAbort } from "../api.js";
import { deferred, settled, timers, naps } from "./helpers.js";

/** A loop whose steps are settled by the test. */
function rig(/** @type {import("../loop.js").LoopOptions} */ options = {}) {
  const t = timers();
  /** @type {(ReturnType<typeof deferred> & { signal: AbortSignal })[]} */ const steps = [];
  let flying = 0, most = 0;
  const L = loop(async signal => {
    most = Math.max(most, ++flying);
    const d = { ...deferred(), signal };
    steps.push(d);
    try { return await d.promise; } finally { flying--; }
  }, { setTimer: t.setTimer, clearTimer: t.clearTimer, ...options });
  return { L, t, steps, get most() { return most; } };
}

test("loop: a step, then the next one after the wait the step asked for", async () => {
  const { L, t, steps } = rig({ every: 1000 });
  L.start();
  assert.equal(steps.length, 1);
  assert.equal(L.busy, true);
  assert.deepEqual(t.waits, [], "no timer while a step is under way");
  steps[0].resolve(2000);
  await settled();
  assert.deepEqual(t.waits, [2000]);
  t.fire();
  steps[1].resolve(undefined);
  await settled();
  assert.deepEqual(t.waits, [1000], "nothing said: `every`");
});

test("loop: never two steps at once, whatever is called meanwhile", async () => {
  const r = rig(), { L, t, steps } = r;
  L.start(); L.start(); L.now(); L.resume(); L.now();
  assert.equal(steps.length, 1);
  steps[0].resolve(5000);
  await settled();
  assert.equal(steps.length, 2, "now() during a step: one more, right after it, not a timer");
  assert.deepEqual(t.waits, []);
  steps[1].resolve(5000);
  await settled();
  assert.deepEqual(t.waits, [5000]);
  L.now();                                                // a click while the timer is waiting
  assert.equal(steps.length, 3);
  assert.deepEqual(t.waits, [], "the timer is taken down, so it can not start a second step");
  steps[2].resolve(5000);
  await settled();
  assert.equal(r.most, 1);
});

test("loop: stop() aborts the step under way, and nothing it answers is acted on", async () => {
  const { L, t, steps } = rig();
  L.start();
  L.stop();
  assert.equal(steps[0].signal.aborted, true);
  assert.equal(L.on, false);
  steps[0].resolve(10);
  await settled();
  assert.deepEqual(t.waits, [], "no next step");
  assert.equal(steps.length, 1);
});

test("loop: stopped and started again while a step ignores its signal: still one at a time", async () => {
  const r = rig(), { L, steps } = r;
  L.start();
  L.stop();
  L.start();
  assert.equal(steps.length, 1, "the old step has not ended yet");
  steps[0].resolve(10);
  await settled();
  assert.equal(steps.length, 2, "now the new one runs");
  assert.equal(r.most, 1);
  L.stop();
});

test("loop: a step that returns false stops the loop; now() starts it again", async () => {
  const { L, t, steps } = rig();
  L.start();
  steps[0].resolve(false);
  await settled();
  assert.equal(L.on, false);
  assert.deepEqual(t.waits, []);
  L.now();
  assert.equal(steps.length, 2);
  L.stop();
});

test("loop: a failing step does not end it: onError says how long to wait", async () => {
  const errors = [];
  const { L, t, steps } = rig({ onError: e => { errors.push(String(e)); return 15000; } });
  L.start();
  steps[0].reject(new Error("HTTP 502"));
  await settled();
  assert.deepEqual(errors, ["Error: HTTP 502"]);
  assert.deepEqual(t.waits, [15000]);
  t.fire();
  assert.equal(steps.length, 2);
  L.stop();
});

test("loop: paused, the step under way finishes and no other starts until resume()", async () => {
  const { L, t, steps } = rig();
  L.start();
  L.pause();
  steps[0].resolve(100);
  await settled();
  assert.deepEqual(t.waits, [], "paused: no timer");
  L.now();
  assert.equal(steps.length, 1, "now() waits for resume too");
  L.resume();
  assert.equal(steps.length, 2);
  steps[1].resolve(100);
  await settled();
  L.pause();
  assert.deepEqual(t.waits, [], "pausing takes the timer down");
  L.stop();
});

test("until: how a poll ended, as an answer", async () => {
  const n = naps();
  let asks = 0;
  assert.deepEqual(await until(() => ++asks, { until: v => v === 3, sleep: n.sleep }), { state: "done", value: 3 });
  assert.deepEqual(await until(() => "busy", { until: () => false, tries: 4, sleep: n.sleep }), { state: "timeout", last: "busy" });
  const failed = await until(() => { throw new Error("HTTP 500"); }, { sleep: n.sleep });
  assert.equal(failed.state, "error");
  assert.match(String(/** @type {any} */ (failed).error), /500/);
});

test("until: a cancelled wait rejects with its AbortError", async () => {
  const ctl = new AbortController(), n = naps();
  const p = until(() => { ctl.abort(); return "busy"; }, { until: () => false, signal: ctl.signal, sleep: n.sleep });
  await assert.rejects(p, isAbort);
});
