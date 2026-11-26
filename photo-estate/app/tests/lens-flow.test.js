// lens-flow.js: the lenses on the pages. The two machines (every pair of states), the lenses loaded and changed
// one at a time (asked for again after a change; a refusal leaves them as they were), what each kind of lens
// says and offers, the words for why a photo is hidden, and the viewer's why line: asked about only once a
// photo has stayed, an answer for a photo left behind dropped, "show it anyway" and taking it back.
import test from "node:test";
import assert from "node:assert/strict";

import { flow, STALE } from "../fsm.js";
import { lensesFlow, whyFlow, offers, says, said, lensName, whySays, day, SUBSCRIBING, LENSES_STEPS, WHY_STEPS } from "../lens-flow.js";
import { deferred, everyPair, settled } from "./helpers.js";

/** @returns {any} */
const lens = (/** @type {number} */ id, extra = {}) => ({
  id, title: "Hidden photos", kind: "filter", set_id: null, published: false, estate_default: false, created_at: "",
  owner: { id: 2, name: "Ben" }, mine: false, subscription: null, proposed: false, on: false, photos: 3, ...extra,
});
const listing = (lenses = [lens(1, { mine: true, on: true })]) => ({ lenses, mine: 1, sysadmin: false, proposed: [] });
const TODAY = new Date(2026, 10, 22);
/** @returns {any} */
const why = (extra = {}) => ({
  sha: "a", hidden: true, shown_anyway: false, text: "", visible: false,
  reasons: [{ set: { id: 4, title: "School" }, hidden_at: "2026-11-23T10:00:00Z", lens: { id: 1, title: "Hidden photos", estate_default: false },
    owner: { id: 2, name: "Ben" }, mine: true }], ...extra,
});

test("the lenses and the why line machines: every legal step is taken, every other pair throws TransitionError", () => {
  const make = (/** @type {any} */ s, /** @type {any} */ from) => flow("t", s, from);
  assert.deepEqual(everyPair(LENSES_STEPS, make, assert.throws), { legal: 8, refused: 17 });
  assert.deepEqual(everyPair(WHY_STEPS, make, assert.throws), { legal: 14, refused: 11 });
});

test("load: ready owns the server's answer; a failure is failed, and asking again starts over", async () => {
  let fail = true;
  const f = lensesFlow({ list: async () => { if (fail) throw new Error("The server did not answer"); return listing(); } });
  assert.equal(await f.load(), "failed");
  assert.equal(f.listing, null);
  fail = false;
  assert.equal(await f.load(), "ready");
  assert.deepEqual(f.listing?.lenses.map(x => x.id), [1]);
});

test("change: saving owns what is changed, one change at a time, then the lenses as the server has them", async () => {
  const answer = deferred();
  let n = 0;
  const seen = /** @type {string[]} */ ([]);
  const f = lensesFlow({ list: async () => (++n === 1 ? listing() : listing([lens(1, { mine: true, published: true })])), onChange: to => seen.push(to) });
  await f.load();
  const saving = f.change("lens:1", () => answer.promise);
  assert.equal(f.page.state, "saving");
  assert.equal(f.page.data.key, "lens:1");
  assert.deepEqual(await f.change("lens:2", async () => 1), { ok: false, error: new Error("Wait for the last change to be saved.") });
  assert.equal(await f.load(), "busy");
  answer.resolve({ lens: 1, published: true, changed: true });
  const got = await saving;
  assert.deepEqual(got, { ok: true, answer: { lens: 1, published: true, changed: true } });
  assert.equal(f.listing?.lenses[0].published, true, "asked for again after the change");
  assert.deepEqual(seen, ["loading", "ready", "saving", "ready"]);
});

test("change: a refusal goes back to ready with the lenses as they were, and says why", async () => {
  const f = lensesFlow({ list: async () => listing() });
  await f.load();
  const before = f.page.data;
  const got = await f.change("lens:1", async () => { throw new Error("this lens is someone else's"); });
  assert.equal(got.ok, false);
  assert.equal(f.page.state, "ready");
  assert.equal(f.page.data, before);
});

test("what a filter of each kind says and offers", () => {
  const ask = { sysadmin: false }, admin = { sysadmin: true };
  const labels = (/** @type {any} */ x, l = ask) => offers(x, l).map(o => o.label);
  // someone else's, published
  const theirs = lens(5, { published: true });
  assert.deepEqual(says(theirs), ["Ben’s", "Hides 3 photos", SUBSCRIBING.filter]);
  assert.deepEqual(labels(theirs), ["Subscribe", "Keep a copy"]);
  assert.deepEqual(offers(theirs, ask).map(o => o.body), [{ mode: "live" }, { mode: "snapshot" }]);
  const on = lens(5, { published: true, subscription: "live", on: true });
  assert.deepEqual([says(on), labels(on)], [["Ben’s", "Hides 3 photos", "On for you"], ["Unsubscribe", "Keep a copy instead"]]);
  const kept = lens(5, { published: true, subscription: "snapshot" });
  assert.deepEqual([says(kept)[2], labels(kept)], ["Off: you keep a copy of it", ["Subscribe", "Keep a copy"]]);
  // an estate default: opt out, or back in
  const dflt = lens(6, { title: "Estate defaults", published: true, estate_default: true, on: true, owner: { id: 1, name: "Ada" } });
  assert.deepEqual([says(dflt), labels(dflt)], [["Ada’s", "Hides 3 photos", "On for everyone, you too"], ["Opt out", "Keep a copy instead"]]);
  assert.equal(says({ ...dflt, mine: true })[0], "Yours");
  const out = { ...dflt, on: false, subscription: "opted_out" };
  assert.deepEqual([says(out), labels(out)], [["Ada’s", "Hides 3 photos", "You opted out"], ["Opt back in"]]);
  assert.deepEqual(says({ ...out, subscription: "snapshot" })[2], "Off: you keep a copy of it");
  // the member's own
  const mine = lens(1, { mine: true, on: true });
  assert.deepEqual([says(mine), labels(mine)], [["Hides 3 photos", "Yours: always on", "Only you see it"], ["Publish", "Propose as an estate default"]]);
  const pub = { ...mine, published: true };
  assert.deepEqual([says(pub)[2], labels(pub)], ["Published", ["Stop publishing", "Propose as an estate default"]]);
  const proposed = { ...pub, proposed: true };
  assert.deepEqual([says(proposed)[2], labels(proposed)], ["Proposed as an estate default", ["Stop publishing"]]);
  // a proposal, to a sysadmin
  const bens = lens(2, { proposed: true, published: true });
  assert.deepEqual(says(bens, admin), ["Ben’s", "Hides 3 photos", "Proposed as an estate default: every member would see through it"]);
  assert.deepEqual(says(bens, ask)[2], SUBSCRIBING.filter, "to anyone else, it is a filter they may subscribe to");
  assert.deepEqual(offers(proposed, admin).map(o => [o.label, o.what, o.body]),
    [["Make it an estate default", "decide", { accept: true }], ["Decline", "decide", { accept: false }]]);
});

test("what a series says and offers", () => {
  const theirs = lens(7, { kind: "series", set_id: 9, published: true, owner: { id: 1, name: "Ada" }, title: "Ada's trip" });
  assert.deepEqual(says(theirs), ["Ada’s series", SUBSCRIBING.series]);
  assert.deepEqual(offers(theirs, { sysadmin: false }).map(o => o.label), ["Subscribe", "Keep a copy"]);
  const sub = { ...theirs, subscription: "live" };
  assert.deepEqual([says(sub)[1], offers(sub, { sysadmin: false }).map(o => o.label)], ["In your Albums", ["Unsubscribe"]]);
  const mine = { ...theirs, mine: true };
  assert.deepEqual([says(mine), offers(mine, { sysadmin: false }).map(o => [o.label, o.body])], [["Your series: published"], [["Stop publishing", { on: false }]]]);
});

test("the words: a lens, a day, why a photo is hidden", () => {
  assert.equal(lensName({ title: "Hidden photos", mine: true, owner: { name: "Ben" } }), "your lens <i>Hidden photos</i>");
  assert.equal(lensName({ title: "<No> screenshots", mine: false, owner: { name: "Ada" } }), "Ada’s lens <i>&lt;No&gt; screenshots</i>");
  assert.equal(day("2026-11-03T10:00:00Z", TODAY), "Nov 3");
  assert.equal(day("2025-01-30", TODAY), "Jan 30, 2025");
  assert.equal(day(null, TODAY), "");
  assert.equal(whySays(why(), TODAY), "Hidden by your lens <i>Hidden photos</i> on Nov 23");
  const ada = why({ shown_anyway: true, hidden: false });
  ada.reasons = [{ ...ada.reasons[0], lens: { id: 6, title: "Estate defaults", estate_default: true }, owner: { id: 1, name: "Ada" }, mine: false, hidden_at: "2026-11-03" },
    ada.reasons[0]];
  assert.equal(whySays(ada, TODAY), "Shown anyway. Hidden by Ada’s lens <i>Estate defaults</i> on Nov 3, and by 1 more");
  const old = why();
  old.reasons[0].lens = null;
  assert.equal(whySays(old, TODAY), "Hidden by your change set <i>School</i> on Nov 23");
  assert.equal(whySays(why({ reasons: [] }), TODAY), "");
});

test("a day the server wrote in UTC is the viewer's own day: the why line says it in local time", () => {
  const late = "2026-11-23T03:00:00Z";                     // 7 in the evening of Nov 22 in California
  assert.equal(day(late, TODAY, "UTC"), "Nov 23");
  assert.equal(day(late, TODAY, "America/Los_Angeles"), "Nov 22");
  assert.equal(day("2026-01-01T05:00:00Z", new Date("2026-06-01T12:00:00Z"), "America/Los_Angeles"), "Dec 31, 2025",
    "the year is the viewer's too");
  assert.equal(day("2026-11-23", TODAY, "America/Los_Angeles"), "Nov 23", "a bare day is that day anywhere");
  const w = why();
  w.reasons[0].hidden_at = late;
  assert.equal(whySays(w, TODAY, "America/Los_Angeles"), "Hidden by your lens <i>Hidden photos</i> on Nov 22");
  assert.equal(whySays(w, TODAY, "Asia/Tokyo"), "Hidden by your lens <i>Hidden photos</i> on Nov 23");
});

test("the why line: asked about once the photo has stayed; one left behind is never answered", async () => {
  /** @type {string[]} */ const asked = [];
  const answers = new Map([["a", deferred()], ["b", deferred()]]);
  const f = whyFlow({ wait: 0, ask: sha => { asked.push(sha); return /** @type {any} */ (answers.get(sha)).promise; }, show: async () => ({}) });
  const a = f.look("a");
  assert.equal(f.line.state, "asking");
  assert.equal(await f.look("a"), "known", "the same photo again asks nothing more");
  const b = f.look("b");
  await settled();
  /** @type {any} */ (answers.get("a")).resolve(why());
  /** @type {any} */ (answers.get("b")).resolve(why({ sha: "b", hidden: false, reasons: [] }));
  assert.equal(await a, STALE);
  assert.equal(await b, "known");
  assert.equal(f.sha, "b");
  assert.equal(f.says, null, "a photo nothing hides: the line says nothing");
  f.clear();
  assert.equal(f.line.state, "idle");
  assert.equal(f.sha, null);
});

test("the why line: show it anyway, then take it back; a refused save leaves it as it was", async () => {
  let anyway = false, refuse = false;
  /** @type {[string, boolean][]} */ const sent = [];
  const f = whyFlow({ wait: 0, ask: async () => why({ shown_anyway: anyway, hidden: !anyway }),
    show: async (sha, on) => { if (refuse) throw new Error("no such photo"); sent.push([sha, on]); anyway = on; return {}; } });
  await f.look("a");
  assert.deepEqual(f.says, { html: whySays(why()), action: "Show it anyway", busy: false });
  const saving = f.toggle();
  assert.equal(f.line.state, "saving");
  assert.equal(f.says?.busy, true);
  assert.equal((await saving).ok, true);
  assert.equal(f.says?.action, "Hide it again");
  assert.match(String(f.says?.html), /^Shown anyway\. Hidden by your lens/);
  await f.toggle();
  assert.equal(f.says?.action, "Show it anyway");
  assert.deepEqual(sent, [["a", true], ["a", false]]);
  refuse = true;
  const before = f.line.data;
  const got = await f.toggle();
  assert.equal(got.ok, false);
  assert.equal(f.line.state, "known");
  assert.equal(f.line.data.why, before.why);
});

test("the why line: a failed ask says nothing, and the photo can be asked about again", async () => {
  let fail = true;
  const f = whyFlow({ wait: 0, ask: async () => { if (fail) throw new Error("down"); return why(); }, show: async () => ({}) });
  assert.equal(await f.look("a"), "failed");
  assert.equal(f.says, null);
  assert.equal((await f.toggle()).ok, false);
  fail = false;
  assert.equal(await f.look("a"), "known");
  assert.equal(f.says?.action, "Show it anyway");
});

test("what the page says once an offer is done", () => {
  const theirs = lens(5, { published: true, title: "No screenshots" });
  const [sub, copy] = offers(theirs, { sysadmin: false });
  assert.equal(said(theirs, sub), "Subscribed: “No screenshots” hides 3 photos from you everywhere");
  assert.equal(said(theirs, copy, { photos: 4 }), "Kept a copy of “No screenshots”: your own filter now, hiding 4 photos");
  assert.equal(said({ ...theirs, subscription: "live" }, offers({ ...theirs, subscription: "live" }, { sysadmin: false })[0]), "Unsubscribed from “No screenshots”");
  const dflt = lens(6, { title: "Estate defaults", published: true, estate_default: true, on: true });
  assert.equal(said(dflt, offers(dflt, { sysadmin: false })[0]), "Opted out of “Estate defaults”: you see what it hides");
  assert.equal(said({ ...dflt, on: false }, offers({ ...dflt, on: false }, { sysadmin: false })[0]), "Back in: “Estate defaults” hides 3 photos from you again");
  const mine = lens(1, { mine: true });
  const [pub, prop] = offers(mine, { sysadmin: false });
  assert.equal(said(mine, pub), "Published: the family can subscribe to “Hidden photos”");
  assert.equal(said(mine, prop), "Proposed: a sysadmin decides whether “Hidden photos” becomes an estate default");
  const asked = lens(2, { proposed: true, published: true });
  const [yes, no] = offers(asked, { sysadmin: true });
  assert.equal(said(asked, yes), "“Hidden photos” is an estate default: everyone sees through it unless they opt out");
  assert.equal(said(asked, no), "Declined: “Hidden photos” stays Ben’s");
  const series = lens(7, { kind: "series", published: true, title: "Trips" });
  const [s1, s2] = offers(series, { sysadmin: false });
  assert.equal(said(series, s1), "Subscribed: “Trips” is in your Albums");
  assert.equal(said(series, s2), "Kept a copy of “Trips”: an album of your own, in your Albums");
});
