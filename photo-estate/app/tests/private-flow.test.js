// private-flow.js: the viewer's private line and the held view (multiplayer phase 5). Both machines (every pair of
// states); what the line says for a held photo, a withdrawn one, one with a release day, one another member
// shares too; the details' row for a photo the member shared; a change saved, refused, or answered after the
// viewer moved on; the held view's bar, honest about what a search shows, leading to the review; choosing photos in
// it and releasing exactly those, and a refusal. There is no "release all".
import test from "node:test";
import assert from "node:assert/strict";

import { flow } from "../fsm.js";
import { privateLine, heldView, lineSays, rowSays, barSays, PRIVATE_STEPS, HELD_STEPS, WHY_HELD } from "../private-flow.js";
import { deferred, everyPair, settled } from "./helpers.js";

const TODAY = new Date(2026, 10, 22);
const held = { sharer: true, mine: { why: /** @type {const} */ ("held"), since: "2026-11-20T20:00:00Z", until: null }, others_sharing: 0 };
const shared = { sharer: true, mine: null, others_sharing: 0 };

test("the private line and held view machines: every legal step is taken, every other pair throws", () => {
  const make = (/** @type {any} */ s, /** @type {any} */ from) => flow("t", s, from);
  assert.deepEqual(everyPair(PRIVATE_STEPS, make, assert.throws), { legal: 6, refused: 3 });
  assert.deepEqual(everyPair(HELD_STEPS, make, assert.throws), { legal: 11, refused: 5 });
  assert.equal(HELD_STEPS.viewing.includes("releasing"), false, "nothing is released from viewing: only what was chosen");
});

test("lineSays: what the line says of a photo private to the member, and what it offers", () => {
  const h = lineSays(held, TODAY);
  assert.equal(h?.html, `<i>Private to you</i> since Nov 20: held: ${WHY_HELD}`);
  assert.deepEqual(h?.offers.map(o => o.label), ["Release", "Release on a day…"]);
  assert.deepEqual(h?.offers[0].body, { private: false });
  assert.equal(h?.offers[1].body, "day", "the page asks which day");
  const w = lineSays({ sharer: true, mine: { why: "withdrawn", since: "2025-03-01T20:00:00Z", until: "2026-12-26" }, others_sharing: 2 }, TODAY);
  assert.equal(w?.html, "<i>Private to you</i> since Mar 1, 2025: you withdrew it. It is released by itself on Dec 26."
    + " 2 other members shared it too, so the family still sees it");
  assert.deepEqual(w?.offers.map(o => [o.label, o.body]), [["Release", { private: false }], ["No release day", { private: true, until: null }]]);
  assert.match(lineSays({ sharer: true, mine: { why: "new", since: null, until: null }, others_sharing: 0 }, TODAY)?.html ?? "", /being checked/);
  assert.equal(lineSays(shared), null, "a photo shared with the family: no line");
  assert.equal(lineSays(null), null);
});

test("rowSays: a photo the member shared, and nobody's private, offers to withdraw it", () => {
  assert.equal(rowSays(shared)?.label, "Withdraw from the family");
  assert.equal(rowSays(held), null, "private already: the line says so");
  assert.equal(rowSays({ sharer: false, mine: null, others_sharing: null }), null, "someone else's: nothing to withdraw");
});

test("the line: look, act saved (the server's answer is the line), refused (as it was), and the viewer closing", async () => {
  /** @type {any[]} */ const sent = [];
  let answer = deferred();
  const seen = /** @type {string[]} */ ([]);
  const L = privateLine({ send: (sha, body) => { sent.push([sha, body]); return answer.promise; }, onChange: to => seen.push(to) });
  assert.equal(L.says, null);
  L.look("a", shared);
  assert.equal(L.says, null);
  assert.equal(L.row?.busy, false);
  const going = L.act({ private: true });
  assert.equal(L.line.state, "saving");
  assert.equal(L.row?.busy, true);
  assert.equal(L.look("b", shared), false, "the why answer of another photo waits for the change");
  answer.resolve({ private: held });
  assert.deepEqual(await going, { ok: true });
  assert.deepEqual(sent, [["a", { private: true }]]);
  assert.equal(L.says?.html.startsWith("<i>Private to you</i>"), true);
  assert.equal(L.row, null);
  answer = deferred();
  const refused = L.act({ private: false });
  answer.reject(new Error("only someone who shared a photo can make it private or release it"));
  const got = await refused;
  assert.equal(got.ok, false);
  assert.equal(L.line.state, "known");
  assert.equal(L.says?.busy, false, "as it was: still private");
  assert.deepEqual(seen, ["known", "saving", "known", "saving", "known"]);
  answer = deferred();
  const late = L.act({ private: false });
  L.clear();
  answer.resolve({ private: shared });
  assert.deepEqual(await late, { ok: true });
  assert.equal(L.line.state, "idle", "an answer after the viewer closed changes nothing");
  assert.equal((await L.act({ private: false })).ok, false, "nothing to change with no photo");
});

test("barSays: the held view's bar says what the search shows of what is held, and leads to the review", () => {
  assert.equal(barSays({ state: "off", data: null }), null);
  assert.deepEqual(barSays({ state: "viewing", data: { n: 0 } })?.offers, []);
  assert.match(barSays({ state: "viewing", data: { n: 0 } })?.text ?? "", /^Nothing is held/);
  const hid = barSays({ state: "viewing", data: { n: 0, held: 2, waiting: 2 } });
  assert.match(hid?.text ?? "", /^None of the 2 photos held for you is in this search/);
  assert.deepEqual(hid?.offers.map(o => [o.label, o.href]), [["Look over all 2", "/held"]]);
  const v = barSays({ state: "viewing", data: { n: 4439, held: 7992, waiting: 7992 } });
  assert.match(v?.text ?? "", /^This search shows 4,439 of the 7,992 photos held for you: your view options \(screenshots, stacks\) and what you hide leave the rest out\. 7,992 wait for you to look them over\./);
  assert.deepEqual(v?.offers.map(o => [o.label, o.what, !!o.primary]), [["Look over all 7,992", "review", true], ["Choose…", "choose", false]]);
  assert.ok(!v?.offers.some(o => /release all/i.test(o.label)), "no release-all");
  const done = barSays({ state: "viewing", data: { n: 3, held: 3, waiting: 0 } });
  assert.match(done?.text ?? "", /^3 photos held for you\. They look like/);
  assert.deepEqual(done?.offers.map(o => [o.label, !!o.primary]), [["Review held photos", false], ["Choose…", true]]);
  assert.match(barSays({ state: "viewing", data: { n: 3 } })?.text ?? "", /^3 photos held for you\./, "before the counts come");
  assert.equal(barSays({ state: "choosing", data: { n: 3, chosen: new Set() } })?.text, "Tap the photos to release");
  assert.deepEqual(barSays({ state: "choosing", data: { n: 3, chosen: new Set(["a", "b"]) } })?.offers.map(o => o.label), ["Release 2", "Cancel"]);
  assert.equal(barSays({ state: "releasing", data: { n: 3, chosen: new Set(["a"]) } })?.text, "Releasing 1 photo…");
});

test("the held view: choose, toggle, release exactly those chosen; a refusal leaves it as it was; the counts", async () => {
  /** @type {any[]} */ const asked = [];
  let answer = deferred();
  const H = heldView({ release: what => { asked.push(what); return answer.promise; } });
  H.open(3);
  assert.equal(H.view.state, "viewing");
  H.counted({ held: 5, waiting: 4 });
  assert.deepEqual([H.view.data.n, H.view.data.held, H.view.data.waiting], [3, 5, 4]);
  H.open(3);
  assert.equal(H.view.data.held, 5, "the counts stay when the list is asked again");
  assert.equal(H.choosing, false);
  assert.equal(H.toggle("a"), false, "not choosing: a tap opens the photo");
  assert.equal("releaseAll" in H, false, "nothing releases every photo");
  H.choose();
  assert.equal(H.choosing, true);
  assert.equal(H.toggle("a"), true);
  assert.equal(H.toggle("b"), true);
  assert.equal(H.toggle("a"), false, "tapped again: not chosen");
  H.open(3);                                        // the list asked again while choosing: still choosing, still chosen
  assert.deepEqual([...H.view.data.chosen], ["b"]);
  let going = H.releaseChosen();
  assert.equal(H.view.state, "releasing");
  answer.resolve({ released: 1 });
  assert.deepEqual(await going, { ok: true, released: 1 });
  assert.deepEqual(asked, [{ shas: ["b"] }]);
  assert.deepEqual([H.view.state, H.view.data.n, H.view.data.held], ["viewing", 2, 4]);
  answer = deferred();
  H.choose();
  H.toggle("c");
  going = H.releaseChosen();
  answer.reject(new Error("no such photo"));
  assert.equal((await going).ok, false);
  assert.deepEqual([H.view.state, [...H.view.data.chosen]], ["choosing", ["c"]], "refused: as it was, still chosen");
  H.cancel();
  assert.equal((await H.releaseChosen()).ok, false, "nothing chosen: nothing released");
  assert.deepEqual(asked, [{ shas: ["b"] }, { shas: ["c"] }]);
  H.close();
  assert.equal(H.view.state, "off");
  await settled();
});
