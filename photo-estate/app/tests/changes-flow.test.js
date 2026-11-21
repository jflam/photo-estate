// changes-flow.js: settle() and edits() (issue #80: changes.html).
import test from "node:test";
import assert from "node:assert/strict";

import { settle, edits, SETTLE } from "../changes-flow.js";
import { isAbort } from "../api.js";
import { latest, STALE } from "../fsm.js";
import { deferred, settled, naps } from "./helpers.js";

test("settle stops at its bound and on abort, and a failed mutation reconciles to server state", async () => {
  // its bound: a server that never says it is done
  const n = naps();
  let asks = 0;
  const never = await settle(async () => { asks++; return { busy: true }; }, { sleep: n.sleep });
  assert.deepEqual(never, { state: "timeout", last: { busy: true } });
  assert.equal(asks, SETTLE.tries, "asked for two minutes' worth, then stopped");
  assert.ok(SETTLE.tries * SETTLE.every <= SETTLE.timeout + SETTLE.every);
  assert.ok(n.asked.every(ms => ms === SETTLE.every));

  // abort: leaving the page
  const ctl = new AbortController(), m = naps();
  let asked = 0;
  const left = settle(async () => { if (++asked === 3) ctl.abort(); return { busy: true }; }, { signal: ctl.signal, sleep: m.sleep });
  await assert.rejects(left, isAbort);
  assert.equal(asked, 3, "no ask after the abort");

  // a failed mutation: the page shows the mark, the server refuses it, the set is read again
  const server = { state: "applied", items: [{ sha: "a", decision: "accept" }, { sha: "b", decision: "accept" }] };
  const set = structuredClone(server), told = [];
  const e = edits(set, async () => structuredClone(server), what => told.push(what));
  const r = await e.make(s => { s.items[0].decision = "reject"; s.state = "review"; }, async () => { throw new Error("HTTP 500"); });
  assert.equal(r.ok, false);
  assert.deepEqual(set, server, "the page is back to what the server has");
  assert.equal(told.length, 1);
  assert.equal(told[0].reconciled, true);
  assert.match(String(told[0].errors[0]), /500/);
});

test("settle: resolves with the check once the app is no longer busy", async () => {
  const answers = [{ busy: true }, { busy: true }, { busy: false, hidden: 3 }], n = naps();
  assert.deepEqual(await settle(async () => answers.shift() || { busy: false }, { sleep: n.sleep }),
    { state: "done", value: { busy: false, hidden: 3 } });
  assert.deepEqual(n.asked, [700, 700]);
});

test("settle: rides out a few failed asks, and says so when they do not stop", async () => {
  const n = naps();
  let asks = 0;
  const flaky = await settle(async () => { if (++asks <= 3) throw new Error("blip"); return { busy: false }; }, { sleep: n.sleep });
  assert.equal(flaky.state, "done");
  const down = await settle(async () => { throw new Error("The server did not answer"); }, { sleep: n.sleep });
  assert.equal(down.state, "error");
  assert.match(String(/** @type {any} */ (down).error), /did not answer/);
});

test("settle: a newer settle cancels the one before (latest)", async () => {
  const settling = latest(), n = naps(), gate = deferred();
  const old = settling.run(signal => settle(() => gate.promise, { signal, sleep: n.sleep }));
  const fresh = settling.run(signal => settle(async () => ({ busy: false }), { signal, sleep: n.sleep }));
  gate.resolve({ busy: true });
  assert.equal(await old, STALE);
  assert.deepEqual(await fresh, { state: "done", value: { busy: false } });
});

test("edits: a mark that is saved changes the page and asks the server nothing more", async () => {
  const set = { state: "review", items: [{ sha: "a", decision: "accept" }] };
  let reloads = 0;
  const e = edits(set, async () => { reloads++; return set; }, () => assert.fail("nothing failed"));
  const r = await e.make(s => { s.items[0].decision = "reject"; }, async () => ({ ok: true }));
  assert.deepEqual(r, { ok: true, value: { ok: true } });
  assert.equal(set.items[0].decision, "reject");
  assert.equal(reloads, 0);
});

test("edits: the set is read again only after the last mark on its way has ended", async () => {
  // Two marks in quick succession; the first is refused, the second is saved. Reading the set between
  // them would show the second as not made, though the server is about to have it.
  const server = { items: [{ sha: "a", decision: "accept" }, { sha: "b", decision: "accept" }] };
  const set = structuredClone(server), first = deferred(), second = deferred(), order = [];
  const e = edits(set, async () => { order.push("reload"); return structuredClone(server); }, w => order.push(`shown ${w.reconciled}`));
  const a = e.make(s => { s.items[0].decision = "reject"; }, () => first.promise);
  const b = e.make(s => { s.items[1].decision = "reject"; }, () => second.promise.then(() => { server.items[1].decision = "reject"; order.push("b saved"); }));
  assert.equal(e.waiting, 2);
  first.reject(new Error("HTTP 500"));
  await settled();
  assert.deepEqual(order, [], "b is still on its way");
  second.resolve();
  assert.equal((await a).ok, false);
  assert.equal((await b).ok, true);
  assert.deepEqual(order, ["b saved", "reload", "shown true"]);
  assert.deepEqual(set.items.map(i => i.decision), ["accept", "reject"], "a put back, b kept: what the server has");
});

test("edits: when the server can not be read either, the page is told, and left as it is", async () => {
  const set = { items: [{ sha: "a", decision: "accept" }] }, told = [];
  const e = edits(set, async () => { throw new Error("offline"); }, w => told.push(w));
  await e.make(s => { s.items[0].decision = "reject"; }, async () => { throw new Error("offline"); });
  assert.equal(told.length, 1);
  assert.equal(told[0].reconciled, false);
  assert.equal(set.items[0].decision, "reject");
});
