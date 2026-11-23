// private-flow.js: the viewer's private line and the held view (multiplayer phase 5). Both machines (every pair of
// states); what the line says for a held photo, a withdrawn one, one with a release day, one another member
// shares too; the details' row for a photo the member shared; a change saved, refused, or answered after the
// viewer moved on; choosing photos in the held view and releasing them, all or those chosen, and a refusal.
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
  assert.deepEqual(everyPair(HELD_STEPS, make, assert.throws), { legal: 12, refused: 4 });
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

test("barSays: the held view's bar, as it is viewed, chosen in and released", () => {
  assert.equal(barSays({ state: "off", data: null }), null);
  assert.deepEqual(barSays({ state: "viewing", data: { n: 0 } })?.offers, []);
  const v = barSays({ state: "viewing", data: { n: 3 } });
  assert.match(v?.text ?? "", /^3 photos kept private to you/);
  assert.deepEqual(v?.offers.map(o => [o.label, o.what]), [["Release all 3", "all"], ["Choose…", "choose"]]);
  assert.deepEqual(barSays({ state: "viewing", data: { n: 1 } })?.offers[0].label, "Release it");
  assert.equal(barSays({ state: "choosing", data: { n: 3, chosen: new Set() } })?.text, "Tap the photos to release");
  assert.deepEqual(barSays({ state: "choosing", data: { n: 3, chosen: new Set(["a", "b"]) } })?.offers.map(o => o.label), ["Release 2", "Cancel"]);
  assert.equal(barSays({ state: "releasing", data: { n: 3, chosen: new Set(["a"]), all: false } })?.text, "Releasing 1 photo…");
});

test("the held view: choose, toggle, release those chosen; release all; a refusal leaves it as it was", async () => {
  /** @type {any[]} */ const asked = [];
  let answer = deferred();
  const H = heldView({ release: what => { asked.push(what); return answer.promise; } });
  H.open(3);
  assert.equal(H.view.state, "viewing");
  assert.equal(H.choosing, false);
  assert.equal(H.toggle("a"), false, "not choosing: a tap opens the photo");
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
  assert.deepEqual([H.view.state, H.view.data.n], ["viewing", 2]);
  answer = deferred();
  H.choose();
  H.toggle("c");
  going = H.releaseChosen();
  answer.reject(new Error("no such photo"));
  assert.equal((await going).ok, false);
  assert.deepEqual([H.view.state, [...H.view.data.chosen]], ["choosing", ["c"]], "refused: as it was, still chosen");
  H.cancel();
  answer = deferred();
  going = H.releaseAll();
  answer.resolve({ released: 2 });
  await going;
  assert.deepEqual(asked.at(-1), { which: "held" });
  assert.deepEqual([H.view.state, H.view.data.n], ["viewing", 0]);
  assert.equal((await H.releaseAll()).ok, false, "nothing left to release");
  H.close();
  assert.equal(H.view.state, "off");
  await settled();
});
