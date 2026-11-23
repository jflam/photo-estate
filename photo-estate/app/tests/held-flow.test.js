// held-flow.js: the review of held photos (/held). Its machine (every pair of states); what "Reviewed up to
// here" covers; why a photo is held, in words; the page's words; reading the list with the marks kept in this
// browser; a review saved (the photos it covered leave), refused (as it was), and a mark made while one is on
// its way; a photo held after the list was read is never sent.
import test from "node:test";
import assert from "node:assert/strict";

import { flow } from "../fsm.js";
import { heldReview, cover, whyHeld, heldSays, HELD_REVIEW_STEPS, SECTION_WORDS } from "../held-flow.js";
import { deferred, everyPair } from "./helpers.js";

const PROMPTS = ["a receipt", "a screenshot of a phone", "a document"];
/** @param {string} sha @param {string} [section] @returns {import("../held-flow.js").HeldItem} */
const it = (sha, section = "close") => ({ sha, section, why: ["a receipt"], scores: [0.1, 0.05, 0.04] });
const LIST = ["a", "b", "c", "d", "e"].map(s => it(s));

test("the held review machine: every legal step is taken, every other pair throws", () => {
  assert.deepEqual(everyPair(HELD_REVIEW_STEPS, (/** @type {any} */ s, /** @type {any} */ from) => flow("t", s, from), assert.throws),
    { legal: 8, refused: 8 });
});

test("cover: up to and including the photo, marked released, the rest kept; null: all; unknown: none", () => {
  const marks = new Set(["b", "d"]);
  assert.deepEqual(cover(LIST, marks, "c"), { release: ["b"], keep: ["a", "c"] });
  assert.deepEqual(cover(LIST, marks, "a"), { release: [], keep: ["a"] });
  assert.deepEqual(cover(LIST, marks, null), { release: ["b", "d"], keep: ["a", "c", "e"] });
  assert.deepEqual(cover(LIST, marks, "zz"), { release: [], keep: [] }, "a photo not listed covers nothing");
  assert.deepEqual(cover([], marks, null), { release: [], keep: [] });
});

test("whyHeld: the prompts it scored over the line, with the score; a screenshot's reasons; an older line", () => {
  assert.equal(whyHeld(it("a"), PROMPTS), "Looks like a receipt (0.100)");
  assert.equal(whyHeld({ sha: "a", section: "sure", why: ["a receipt", "a document", "is_screenshot"], scores: [0.13, 0.02, 0.141] }, PROMPTS),
    "Looks like a receipt (0.130), a document (0.141); a screenshot by its size, with no camera");
  assert.equal(whyHeld({ sha: "a", section: "sure", why: ["phone_screenshot"], scores: [0.01, 0.02, 0.03] }, PROMPTS),
    "The phone marked it a screenshot");
  assert.equal(whyHeld({ sha: "a", section: "close", why: [], scores: [0.081, 0.02, 0.03] }, PROMPTS),
    "Held under an older line: its best score is 0.081");
  assert.equal(whyHeld({ sha: "a", section: "documents", why: [], scores: null }, PROMPTS), "Held as private");
  assert.deepEqual(Object.keys(SECTION_WORDS), ["named", "close", "documents", "sure", "hidden"], "private.SECTIONS, in order");
});

test("heldSays: what the dock says U would do, the counts, more held since, and nothing left", () => {
  const d = { items: LIST, marks: new Set(["b", "d"]), waiting: 5 };
  const w = heldSays(d, "c");
  assert.equal(w.say, "Up to here: <b>release 1</b>, keep 2 private · 2 after it");
  assert.equal(w.sum, "5 to look over · <b>2</b> to release");
  assert.equal(w.fin, "");
  assert.equal(heldSays(d, "e").say, "Up to here: <b>release 2</b>, keep 3 private");
  assert.equal(heldSays(d, null).say, "<b>2 to release</b> · 3 stay private");
  assert.match(heldSays({ ...d, waiting: 7 }, "c").fin, /^<b>2 more photos held since this page was read\.<\/b>/);
  const none = heldSays({ items: [], marks: new Set(), waiting: 0 }, null);
  assert.equal(none.say, "Nothing waits to be looked over");
  assert.equal(none.fin, "", "the page says it, with where the rest is");
});

test("the review: read with the marks this browser kept (those still listed), marked, reviewed up to a photo", async () => {
  /** @type {Record<string, string[]>} */ const kept = { 7: ["b", "gone"] };
  /** @type {any[]} */ const sent = [];
  let answer = deferred();
  const R = heldReview({
    load: async () => ({ items: LIST.slice(), waiting: 5, prompts: PROMPTS, member: 7, sections: [{ key: "close", n: 5 }] }),
    send: c => { sent.push(c); return answer.promise; },
    remember: { get: m => kept[m] || [], set: (m, shas) => { kept[m] = shas; } },
  });
  assert.equal(R.review.state, "loading");
  assert.deepEqual(R.items, []);
  assert.deepEqual(await R.open(), { ok: true });
  assert.deepEqual([R.isMarked("b"), R.marked], [true, 1]);
  assert.deepEqual(kept[7], ["b"], "a mark of a photo no longer listed is let go");
  R.mark(["d"], true);
  assert.deepEqual(kept[7], ["b", "d"]);
  const going = R.upTo("c");
  assert.equal(R.review.state, "sending");
  assert.equal(R.busy, true);
  assert.equal((await R.upTo("e")).ok, false, "one review at a time");
  assert.equal((await R.open()).ok, false, "not read again while one is on its way");
  R.mark(["e"], true);                                   // a mark made while the review is on its way stays
  answer.resolve({ released: 1, kept: 2, skipped: 0, waiting: 2 });
  const got = await going;
  assert.equal(got.ok, true);
  assert.deepEqual(sent, [{ release: ["b"], keep: ["a", "c"] }]);
  assert.deepEqual(R.items.map(x => x.sha), ["d", "e"], "what it covered left the review");
  assert.deepEqual([...R.review.data.marks].sort(), ["d", "e"]);
  assert.deepEqual(kept[7].sort(), ["d", "e"]);
  assert.equal(R.review.data.waiting, 2);
  // refused: as it was
  answer = deferred();
  const refused = R.upTo(null);
  answer.reject(new Error("HTTP 500"));
  assert.equal((await refused).ok, false);
  assert.deepEqual([R.review.state, R.items.map(x => x.sha), [...R.review.data.marks].sort()], ["reviewing", ["d", "e"], ["d", "e"]]);
  // the whole list ("Done")
  answer = deferred();
  const done = R.upTo(null);
  answer.resolve({ released: 2, kept: 0, skipped: 0, waiting: 0 });
  await done;
  assert.deepEqual(sent.at(-1), { release: ["d", "e"], keep: [] });
  assert.deepEqual([R.items, R.marked], [[], 0]);
  assert.equal((await R.upTo(null)).ok, false, "nothing left to look over");
});

test("the review: a photo held after the list was read is in no request; the list read again shows it", async () => {
  const server = { items: LIST.slice(0, 2), waiting: 2 };
  /** @type {any[]} */ const sent = [];
  const R = heldReview({
    load: async () => ({ ...server, items: server.items.slice(), prompts: PROMPTS, member: 1 }),
    send: async c => { sent.push(c); server.items = server.items.filter(x => !c.keep.includes(x.sha) && !c.release.includes(x.sha)); return { released: 0, kept: c.keep.length, skipped: 0, waiting: server.items.length }; },
  });
  await R.open();
  server.items = [...server.items, it("new")];         // held meanwhile (an upload checked)
  server.waiting = 3;
  const got = await R.upTo(null);
  assert.deepEqual(sent, [{ release: [], keep: ["a", "b"] }], "only what the page listed");
  assert.equal(got.ok && got.value.waiting, 1);
  assert.deepEqual(R.items, []);
  assert.match(heldSays(R.review.data, null).fin, /^<b>1 more photo held since/);
  await R.open();
  assert.deepEqual(R.items.map(x => x.sha), ["new"]);
});

test("the review: a list that cannot be read fails, and can be read again", async () => {
  let fail = true;
  const R = heldReview({ load: async () => { if (fail) throw new Error("offline"); return { items: [], waiting: 0, member: 1 }; }, send: async () => ({ released: 0, kept: 0, skipped: 0, waiting: 0 }) });
  const r = await R.open();
  assert.equal(r.ok, false);
  assert.equal(R.review.state, "failed");
  fail = false;
  assert.deepEqual(await R.open(), { ok: true });
  assert.equal(R.review.state, "reviewing");
  assert.equal(R.mark(["x"], true), true);
});
