// inbox-flow.js: the member's /inbox page. Its machine (every pair of states), the list loaded or failed, one
// item put away at a time, a refusal leaving the items as they were, what each kind's button says, and the
// count beside Inbox in the Library's menu.
import test from "node:test";
import assert from "node:assert/strict";

import { flow } from "../fsm.js";
import { inboxFlow, act, badge, INBOX_STEPS, KIND } from "../inbox-flow.js";
import { deferred, everyPair, settled } from "./helpers.js";

const item = (/** @type {number} */ id, kind = "member_request", ref = `member:${id}`) => ({
  id, kind, ref, title: `item ${id}`, created_at: "2026-11-22T10:00:00Z", updated_at: "2026-11-22T10:00:00Z", opens: "/members",
});
const listing = () => ({ items: [item(3), item(2, "lens_changed", "lens:5")], open: 2 });

test("the inbox machine: every legal step is taken, every other pair throws TransitionError", () => {
  assert.deepEqual(everyPair(INBOX_STEPS, (/** @type {any} */ s, /** @type {any} */ from) => flow("t", s, from), assert.throws),
    { legal: 8, refused: 17 });
});

test("load: ready owns the server's answer; a failure is failed, and asking again starts over", async () => {
  let fail = true;
  const f = inboxFlow({ list: async () => { if (fail) throw new Error("The server did not answer"); return listing(); }, dismiss: async () => ({ open: 1 }) });
  assert.equal(await f.load(), "failed");
  assert.equal(f.page.state, "failed");
  assert.equal(/** @type {Error} */ (f.page.data.error).message, "The server did not answer");
  fail = false;
  assert.equal(await f.load(), "ready");
  assert.deepEqual(f.page.data.items.map((/** @type {any} */ x) => x.id), [3, 2]);
});

test("dismiss: dismissing owns the item being put away, then ready without it and the server's count", async () => {
  /** @type {number[]} */ const sent = [];
  const answer = deferred();
  const seen = /** @type {string[]} */ ([]);
  const f = inboxFlow({ list: async () => listing(), dismiss: id => { sent.push(id); return answer.promise; }, onChange: to => seen.push(to) });
  await f.load();
  const kept = f.page.data.items[0];
  const going = f.dismiss(2);
  assert.equal(f.page.state, "dismissing");
  assert.equal(f.page.data.id, 2);
  assert.equal((await f.dismiss(3)).ok, false, "one at a time");
  assert.equal(await f.load(), "busy", "not asked again while one is being put away");
  assert.deepEqual(sent, [2]);
  answer.resolve({ id: 2, state: "dismissed", open: 1 });
  assert.equal((await going).ok, true);
  assert.equal(f.page.state, "ready");
  assert.deepEqual(f.page.data, { items: [kept], open: 1 });
  assert.equal(f.page.data.items[0], kept, "the others are the same objects");
  assert.deepEqual(seen, ["loading", "ready", "dismissing", "ready"]);
});

test("dismiss: a refusal goes back to ready with the items as they were, and says why", async () => {
  const f = inboxFlow({ list: async () => listing(), dismiss: async () => { throw new Error("no such item"); } });
  await f.load();
  const before = f.page.data;
  const got = await f.dismiss(3);
  assert.equal(got.ok, false);
  assert.match(String(/** @type {any} */ (got).error.message), /no such item/);
  assert.equal(f.page.data, before);
  assert.equal((await inboxFlow({ list: async () => listing(), dismiss: async () => ({ open: 0 }) }).dismiss(1)).ok, false, "not ready yet");
});

test("an answer to a list asked for again is dropped", async () => {
  const first = deferred(), second = deferred();
  const answers = [first, second];
  const f = inboxFlow({ list: () => /** @type {any} */ (answers.shift()).promise, dismiss: async () => ({ open: 0 }) });
  const a = f.load(), b = f.load();
  second.resolve(listing());
  first.resolve({ items: [], open: 0 });
  assert.equal(await b, "ready");
  await settled();
  assert.equal(String(await a), "Symbol(stale)");
  assert.equal(f.page.data.items.length, 2);
});

test("what each kind's button says, and that only a notice is put away by opening it", () => {
  assert.deepEqual(act({ kind: "member_request", ref: "member:4" }), { label: "Open Members", notice: false });
  assert.deepEqual(act({ kind: "review", ref: "album:9" }), { label: "Look it over", notice: false });
  assert.deepEqual(act({ kind: "lens_proposed", ref: "lens:3" }), { label: "Decide in Albums", notice: false });
  assert.deepEqual(act({ kind: "lens_changed", ref: "lens:3" }), { label: "See it in Albums", notice: true });
  assert.deepEqual(act({ kind: "lens_changed", ref: "album:9" }), { label: "Open the series", notice: true });
  assert.deepEqual(act({ kind: "binding", ref: "binding:1" }), { label: "Allow or turn off", notice: false }, "done once the member decides");
  assert.deepEqual(act({ kind: "photo_frame", ref: "device:1" }), { label: "Open", notice: false }, "a kind this page does not know yet");
  assert.deepEqual(act({ kind: "question", ref: "ask:q1" }), { label: "Answer", notice: false }, "done once each is answered or put away");
  assert.deepEqual(act({ kind: "series_update", ref: "album:7" }), { label: "Look at them", notice: false });
  assert.deepEqual(act({ kind: "held", ref: "held:12" }), { label: "Look them over", notice: false }, "done once nothing is still held");
  assert.deepEqual(Object.keys(KIND), ["member_request", "review", "lens_proposed", "lens_changed", "held", "question", "series_update", "binding"]);
});

test("the count beside Inbox", () => {
  assert.equal(badge(0), "");
  assert.equal(badge(1), "1");
  assert.equal(badge(9), "9");
  assert.equal(badge(12), "9+");
});
