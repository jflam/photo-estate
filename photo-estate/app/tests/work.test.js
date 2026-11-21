// work.js: single(), batch() and serial() (issue #80: people.html's saves, changes.html's marks).
import test from "node:test";
import assert from "node:assert/strict";

import { single, batch, serial, SKIPPED } from "../work.js";
import { deferred, settled } from "./helpers.js";

test("a busy flag is cleared after a failed save, and the next save works", async () => {
  const saving = single();
  let calls = 0;
  const save = () => saving.run(async () => { if (++calls === 1) throw new Error("HTTP 500"); return "saved"; });
  await assert.rejects(save(), /HTTP 500/);
  assert.equal(saving.busy, false, "not wedged by the failure");
  assert.equal(await save(), "saved");
  assert.equal(calls, 2);
});

test("single: a second run while the first is under way does nothing", async () => {
  const saving = single(), d = deferred();
  let calls = 0;
  const first = saving.run(() => { calls++; return d.promise; });
  assert.equal(saving.busy, true);
  assert.equal(await saving.run(() => { calls++; }), SKIPPED);
  assert.equal(calls, 1);
  d.resolve("done");
  assert.equal(await first, "done");
  assert.equal(saving.busy, false);
});

test("single: work that throws before its first await still leaves it free", async () => {
  const saving = single();
  await assert.rejects(saving.run(() => { throw new TypeError("bad plan"); }), TypeError);
  assert.equal(saving.busy, false);
});

test("a batch that fails midway reports which items were applied and leaves the control usable", async () => {
  const told = [], control = { disabled: false }, saving = single();
  let down = true;
  const send = async (/** @type {number} */ id) => { if (id === 3 && down) throw new Error("HTTP 503"); told.push(id); };
  const press = (/** @type {number[]} */ ids) => saving.run(async () => {
    control.disabled = true;
    try { return await batch(ids, send); } finally { control.disabled = false; }
  });
  const r = /** @type {import("../work.js").BatchResult<number>} */ (await press([1, 2, 3, 4, 5]));
  assert.equal(r.ok, false);
  assert.deepEqual(r.done, [1, 2], "what the server has been told");
  assert.equal(r.failed, 3);
  assert.match(String(r.error), /503/);
  assert.deepEqual(r.rest, [4, 5], "not tried");
  assert.deepEqual(told, [1, 2], "nothing after the failure was sent");
  assert.equal(control.disabled, false, "the button works again");
  assert.equal(saving.busy, false);
  down = false;
  const again = /** @type {import("../work.js").BatchResult<number>} */ (await press([/** @type {number} */ (r.failed), ...r.rest]));
  assert.equal(again.ok, true);
  assert.deepEqual(again.done, [3, 4, 5]);
  assert.deepEqual(told, [1, 2, 3, 4, 5], "each item told once");
});

test("batch: all done, and nothing to do", async () => {
  const seen = [];
  const r = await batch(["a", "b"], (item, i) => { seen.push([item, i]); });
  assert.deepEqual(r, { ok: true, done: ["a", "b"], failed: null, error: undefined, rest: [] });
  assert.deepEqual(seen, [["a", 0], ["b", 1]]);
  assert.equal((await batch([], () => { throw new Error("never"); })).ok, true);
});

test("serial: one at a time, in the order asked", async () => {
  const q = serial(), order = [], a = deferred(), b = deferred();
  let flying = 0, most = 0;
  const work = (/** @type {string} */ name, /** @type {Promise<any>} */ p) => async () => {
    most = Math.max(most, ++flying); order.push(name + " sent");
    try { return await p; } finally { flying--; order.push(name + " ended"); }
  };
  const first = q.run(work("a", a.promise)), second = q.run(work("b", b.promise));
  assert.equal(q.waiting, 2);
  await settled();
  assert.deepEqual(order, ["a sent"], "b waits for a");
  b.resolve(2);                                           // b's answer is ready first; it still goes second
  a.resolve(1);
  assert.deepEqual(await first, { ok: true, value: 1 });
  assert.deepEqual(await second, { ok: true, value: 2 });
  assert.deepEqual(order, ["a sent", "a ended", "b sent", "b ended"]);
  assert.equal(most, 1);
  assert.equal(q.waiting, 0);
});

test("serial: after a failure, settle runs once, when the last one asked for has ended", async () => {
  const settles = [], order = [];
  const q = serial({ settle: errors => { settles.push(errors.map(String)); order.push("settle"); } });
  const a = deferred(), b = deferred(), c = deferred();
  const first = q.run(() => a.promise), second = q.run(() => b.promise.then(v => { order.push("b ended"); return v; }));
  a.reject(new Error("first failed"));
  await settled();
  assert.deepEqual(settles, [], "not while b is still on its way");
  b.resolve("b");
  assert.deepEqual(await first, { ok: false, error: new Error("first failed") });
  assert.deepEqual(await second, { ok: true, value: "b" });
  assert.deepEqual(order, ["b ended", "settle"]);
  assert.deepEqual(settles, [["Error: first failed"]]);
  q.run(() => c.promise);                                 // a later success settles nothing
  c.resolve();
  await settled();
  assert.equal(settles.length, 1);
});

test("serial: a settle that throws does not break the queue", async () => {
  const q = serial({ settle: () => { throw new Error("could not re-read"); } });
  assert.equal((await q.run(() => { throw new Error("x"); })).ok, false);
  assert.deepEqual(await q.run(() => 7), { ok: true, value: 7 });
});
