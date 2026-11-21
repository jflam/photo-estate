// people-flow.js: the wait for the groups of unnamed faces (issue #80: people.html).
import test from "node:test";
import assert from "node:assert/strict";

import { unknownFaces, UNKNOWN } from "../people-flow.js";
import { isAbort } from "../api.js";
import { naps } from "./helpers.js";

test('the unknown-faces poll stops on "failed", stops at its bound, and stops when cancelled', async () => {
  // "failed": the grouping raised. One ask after it would only hear the same.
  let asks = 0;
  const n = naps();
  const failed = await unknownFaces(async () => (++asks < 3 ? { state: "working" } : { state: "failed", error: "MemoryError: 12 GiB" }), { sleep: n.sleep });
  assert.deepEqual(failed, { state: "failed", error: "MemoryError: 12 GiB" });
  assert.equal(asks, 3);

  // its bound: a server that says "working" for ever
  let forever = 0;
  const m = naps();
  const gaveUp = await unknownFaces(async () => { forever++; return { state: "working" }; }, { sleep: m.sleep });
  assert.deepEqual(gaveUp, { state: "timeout" });
  assert.equal(forever, UNKNOWN.tries, "asked ten minutes' worth, then stopped");
  assert.ok(Math.abs(m.asked.reduce((a, b) => a + b, 0) - UNKNOWN.timeout) < 15000, "its waits add up to about ten minutes");

  // cancelled: the person went to another view
  const ctl = new AbortController(), k = naps();
  let asked = 0;
  const left = unknownFaces(async () => { if (++asked === 2) ctl.abort(); return { state: "waiting" }; }, { signal: ctl.signal, sleep: k.sleep });
  await assert.rejects(left, isAbort);
  assert.equal(asked, 2, "no ask after it was cancelled");
});

test("unknownFaces: the groups, once ready; told while waiting", async () => {
  const answers = [{ state: "waiting" }, { state: "working" }, { state: "ready", groups: [{ cluster: 4 }] }], waited = [], n = naps();
  const got = await unknownFaces(async () => /** @type {any} */ (answers.shift()), { sleep: n.sleep, onWaiting: t => waited.push(t) });
  assert.deepEqual(got, { state: "ready", groups: [{ cluster: 4 }] });
  assert.deepEqual(waited, [1, 2]);
});

test("unknownFaces: each ask waits longer than the last, up to ten seconds", async () => {
  const n = naps();
  await unknownFaces(async () => ({ state: "working" }), { sleep: n.sleep, tries: 8 });
  assert.deepEqual(n.asked, [1500, 2250, 3375, 5062.5, 7593.75, 10000, 10000]);
});

test("unknownFaces: a server that stops answering is an error, after a few tries", async () => {
  const n = naps();
  let asks = 0;
  const got = await unknownFaces(async () => { asks++; throw new Error("The server did not answer"); }, { sleep: n.sleep });
  assert.equal(got.state, "error");
  assert.equal(asks, UNKNOWN.retries + 1);
});
