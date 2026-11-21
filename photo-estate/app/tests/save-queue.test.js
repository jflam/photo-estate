// save-queue.js: review.html's saves (issue #80). `npm test` in photo-estate/app. Timers are fired by hand.
import test from "node:test";
import assert from "node:assert/strict";

import { saveQueue, retryAfter, SAVE_STEPS } from "../save-queue.js";
import { deferred, settled, timers } from "./helpers.js";

/** A queue whose saves are settled by the test: `sent` is every body, in order. */
function rig() {
  const t = timers(), sent = [], states = [];
  /** @type {ReturnType<typeof deferred>[]} */ const calls = [];
  let flying = 0, most = 0;
  const q = saveQueue(async body => {
    sent.push(body);
    most = Math.max(most, ++flying);
    const d = deferred();
    calls.push(d);
    try { await d.promise; } finally { flying--; }
  }, { setTimer: t.setTimer, clearTimer: t.clearTimer, onState: s => states.push(s) });
  return { q, t, sent, calls, states, get most() { return most; } };
}

test("a newer review edit survives a failed save and is what the retry sends", async () => {
  const { q, t, sent, calls } = rig();
  q.put("g1", { keep: ["a"] });
  q.put("g2", { keep: ["x"] });
  t.fire();                                               // the 400 ms are up: the save goes
  assert.deepEqual(sent, [{ g1: { keep: ["a"] }, g2: { keep: ["x"] } }]);
  q.put("g1", { keep: ["b"] });                           // changed again while that save is on its way
  calls[0].reject(new Error("503"));
  await settled();
  assert.equal(q.state, "failed");
  assert.equal(q.pending, 2, "what failed is waiting again");
  assert.deepEqual(t.waits, [3000], "and a retry is due");
  t.fire();
  assert.deepEqual(sent[1], { g1: { keep: ["b"] }, g2: { keep: ["x"] } }, "the newer edit, not the one that failed");
  calls[1].resolve();
  await settled();
  assert.equal(q.state, "saved");
  assert.equal(q.pending, 0);
  assert.deepEqual(t.waits, []);
});

test("saves never overlap or go out of order", async () => {
  const r = rig(), { q, t, sent, calls } = r;
  q.put("g1", 1);
  t.fire();
  q.put("g2", 2);                                         // while the first is on its way
  q.put("g1", 3);
  const asked = q.flush();                                // and someone asks for a save now
  q.flush();
  assert.equal(sent.length, 1, "still one request: the rest wait for it");
  assert.deepEqual(t.waits, [], "no timer is left to start a second one");
  calls[0].resolve();
  await settled();
  assert.deepEqual(sent, [{ g1: 1 }, { g2: 2, g1: 3 }], "what gathered goes in one request, after the first");
  q.put("g3", 4);
  calls[1].resolve();
  await settled();
  assert.deepEqual(sent[2], { g3: 4 });
  calls[2].resolve();
  assert.equal(await asked, true);
  assert.equal(r.most, 1, "never two in flight");
  assert.equal(q.state, "saved");
});

test("a run of edits is one request, and the last value of a key is the one sent", async () => {
  const { q, t, sent, calls } = rig();
  q.put("g1", 1); q.put("g1", 2); q.put("g2", 3);
  assert.deepEqual(t.waits, [400], "each edit moves the one timer");
  assert.equal(q.state, "dirty");
  t.fire();
  assert.deepEqual(sent, [{ g1: 2, g2: 3 }]);
  calls[0].resolve();
  await settled();
  assert.equal(q.state, "saved");
});

test("a failed save is retried later each time, and an edit or flush() retries sooner", async () => {
  const { q, t, sent, calls, states } = rig();
  q.put("g1", 1);
  t.fire();
  for (const wait of [3000, 6000, 12000]) {
    calls.at(-1)?.reject(new Error("down"));
    await settled();
    assert.deepEqual(t.waits, [wait]);
    assert.equal(q.failures, sent.length);
    t.fire();
  }
  calls.at(-1)?.reject(new Error("down"));
  await settled();
  q.put("g2", 2);                                         // an edit while failed: try again in 400 ms
  assert.deepEqual(t.waits, [400]);
  const done = q.flush();                                 // or now
  assert.deepEqual(t.waits, []);
  assert.deepEqual(sent.at(-1), { g1: 1, g2: 2 });
  calls.at(-1)?.resolve();
  assert.equal(await done, true);
  assert.equal(q.failures, 0);
  assert.deepEqual(states.slice(0, 4), ["dirty", "saving", "failed", "saving"]);
  assert.equal(states.at(-1), "saved");
  assert.deepEqual([1, 2, 3, 4, 5, 9].map(retryAfter), [3000, 6000, 12000, 24000, 30000, 30000]);
});

test("flush() with nothing waiting sends nothing; a failed flush resolves false, it does not reject", async () => {
  const { q, t, sent, calls } = rig();
  assert.equal(await q.flush(), true);
  assert.equal(sent.length, 0);
  q.put("g1", 1);
  const done = q.flush();
  calls[0].reject(new Error("no"));
  assert.equal(await done, false);
  assert.equal(q.state, "failed");
  assert.deepEqual(t.waits, [3000]);
});

test("the queue's table has every state, and no step out of saved but an edit", () => {
  assert.deepEqual(Object.keys(SAVE_STEPS).sort(), ["dirty", "failed", "saved", "saving"]);
  assert.deepEqual(SAVE_STEPS.saved, ["dirty"]);
  assert.deepEqual(SAVE_STEPS.failed, ["saving"], "a failed save can only be sent again");
});
