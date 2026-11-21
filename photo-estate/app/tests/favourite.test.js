// favourite.js: the heart on a photo (issue #81): whatever order the presses and the answers come in,
// the page ends on what was last asked for and so does the server; a failed save is put back.
import test from "node:test";
import assert from "node:assert/strict";

import { favourites } from "../favourite.js";
import { deferred, settled } from "./helpers.js";

/** A page and a server: `sent` are the saves asked for, each answered by hand; `heart` is what the page shows. */
function page() {
  /** @type {{ sha: string, fav: boolean, d: ReturnType<typeof deferred> }[]} */ const sent = [];
  /** @type {Record<string, boolean>} */ const heart = {}, server = {};
  /** @type {any[]} */ const counts = [], failures = [];
  const favs = favourites({
    save: (/** @type {{ sha: string }} */ r, fav) => { const d = deferred(); sent.push({ sha: r.sha, fav, d }); return d.promise; },
    show: (r, fav) => { heart[r.sha] = fav; },
    saved: answer => counts.push(answer.favourites),
    failed: (e, r, fav) => failures.push([r.sha, fav, /** @type {Error} */ (e).message]),
  });
  /** Answer the n-th save as the server would. */
  const answer = (/** @type {number} */ n, favourites = 0) => { server[sent[n].sha] = sent[n].fav; sent[n].d.resolve({ favourites }); };
  const press = (/** @type {string} */ sha) => favs.toggle(sha, !!heart[sha], { sha });
  return { favs, sent, heart, server, counts, failures, answer, press };
}

test("one press: shown at once, saved once", async () => {
  const p = page();
  const done = p.press("a");
  assert.equal(p.heart.a, true, "the heart is on before the server has answered");
  assert.equal(p.favs.saving("a"), true);
  assert.deepEqual(p.sent.map(s => [s.sha, s.fav]), [["a", true]]);
  p.answer(0, 4);
  assert.equal(await done, true);
  assert.deepEqual([p.heart.a, p.server.a, p.counts, p.favs.pending], [true, true, [4], 0]);
});

test("double favourite toggle: the page and the server end on what was asked last", async () => {
  const p = page();
  p.press("a"); const done = p.press("a");           // on, then off, before the first save is answered
  assert.equal(p.heart.a, false, "the page shows what was last asked for");
  assert.equal(p.sent.length, 1, "one save at a time for a photo");
  p.answer(0, 5);
  await settled();
  assert.equal(p.heart.a, false, "the answer to the first save did not put the heart back on");
  assert.deepEqual(p.sent.map(s => s.fav), [true, false], "the last request is the last thing asked for");
  p.answer(1, 4);
  assert.equal(await done, true);
  assert.deepEqual([p.heart.a, p.server.a, p.favs.pending], [false, false, 0]);
});

test("toggle during a save, three times: nothing more is sent when the server already has what is wanted", async () => {
  const p = page();
  p.press("a"); p.press("a"); const done = p.press("a");     // on, off, on
  assert.equal(p.heart.a, true);
  p.answer(0, 1);
  assert.equal(await done, true);
  assert.deepEqual(p.sent.map(s => s.fav), [true]);
  assert.deepEqual([p.heart.a, p.server.a], [true, true]);
  // and it can be pressed again afterwards
  const off = p.press("a");
  p.answer(1, 0);
  await off;
  assert.deepEqual([p.heart.a, p.server.a, p.sent.length], [false, false, 2]);
});

test("out-of-order responses: an older answer cannot flip a heart or put an old count back", async () => {
  const p = page();
  const a = p.press("a"), b = p.press("b");          // two photos, both saving
  p.answer(1, 2);                                    // b's answer (sent second) comes first: 2 favourites
  await settled();
  p.answer(0, 1);                                    // a's, sent first and counted before b was, comes last
  await Promise.all([a, b]);
  assert.deepEqual(p.counts, [2], "the count of the request sent last stands");
  assert.deepEqual([p.heart.a, p.heart.b], [true, true]);

  // one photo: pressed off while its "on" is being saved; the "on" answer arrives after the press
  const q = page();
  q.press("a"); q.press("a");
  q.answer(0, 1);
  await settled();
  assert.equal(q.heart.a, false);
  q.answer(1, 0);
  await settled();
  assert.deepEqual([q.heart.a, q.server.a, q.counts], [false, false, [1, 0]]);
});

test("a failed save rolls back to what the server has, and says so", async () => {
  const p = page();
  const done = p.press("a");
  p.sent[0].d.reject(new Error("not allowed"));
  assert.equal(await done, false);
  assert.equal(p.heart.a, false, "the heart is put back");
  assert.deepEqual(p.failures, [["a", true, "not allowed"]]);
  assert.equal(p.favs.pending, 0, "and it can be tried again");
  const again = p.press("a");
  p.answer(1, 1);
  assert.equal(await again, true);
  assert.deepEqual([p.heart.a, p.server.a], [true, true]);
});

test("a failed save after a second press: back to the server's truth, which is what was asked for, so nothing is said", async () => {
  const p = page();
  p.press("a"); const done = p.press("a");           // on, off; the "on" then fails
  p.sent[0].d.reject(new Error("offline"));
  assert.equal(await done, false);
  assert.deepEqual([p.heart.a, p.sent.length, p.failures], [false, 1, []]);

  // on (saved), then off and on again while "off" is being sent, and "off" fails: the server still has on
  const q = page();
  const first = q.press("a"); q.answer(0, 1); await first;
  q.press("a"); const last = q.press("a");
  q.sent[1].d.reject(new Error("offline"));
  await last;
  assert.deepEqual([q.heart.a, q.server.a, q.failures], [true, true, []]);
});

test("asking for what is already so sends nothing", async () => {
  const p = page();
  assert.equal(await p.favs.set("a", false, false, { sha: "a" }), true);
  assert.equal(p.sent.length, 0);
});

// ── every photo of an album at once ("Make all favourites"): one request, in step with the hearts ──

/** An album on the page: `bulk` are the album requests asked for; `make(want)` asks for one. */
function albumOn(/** @type {ReturnType<typeof page>} */ p, /** @type {string[]} */ shas) {
  /** @type {{ want: boolean, d: ReturnType<typeof deferred> }[]} */ const bulk = [];
  const view = { shas };                               // what the page has of the album; [] once it shows another
  const photos = () => view.shas.map(sha => /** @type {[string, boolean, { sha: string }]} */ ([sha, !!p.heart[sha], { sha }]));
  const make = (/** @type {boolean} */ want) => p.favs.all(photos, want, w => { const d = deferred(); bulk.push({ want: w, d }); return d.promise; });
  return { bulk, view, make };
}

test("all of an album: shown at once on the photos the page has, one request, and its answer is the caller's", async () => {
  const p = page(), a = albumOn(p, ["a", "b", "c"]);
  p.heart.b = true;
  const done = a.make(true);
  await settled();
  assert.deepEqual([p.heart.a, p.heart.b, p.heart.c], [true, true, true], "before the server has answered");
  assert.deepEqual([a.bulk.map(b => b.want), p.sent.length], [[true], 0], "one request for the album, none for a photo");
  a.bulk[0].d.resolve({ fav: true, changed: 2 });
  assert.deepEqual(await done, { ok: true, answer: { fav: true, changed: 2 } });
  assert.deepEqual(p.counts, [], "the count is not taken from it as from a heart's answer");
});

test("all of an album fails: only the photos it changed are put back", async () => {
  const p = page(), a = albumOn(p, ["a", "b"]);
  p.heart.b = true;
  const done = a.make(true);
  await settled();
  a.bulk[0].d.reject(new Error("not allowed"));
  const got = await done;
  assert.equal(got.ok, false);
  assert.equal(/** @type {any} */ (got).error.message, "not allowed");
  assert.deepEqual([p.heart.a, p.heart.b], [false, true], "a is put back; b was a favourite before and still is");
});

test("a heart pressed while the album is being saved is sent after it, and is what stands", async () => {
  const p = page(), a = albumOn(p, ["a", "b"]);
  const all = a.make(true);
  await settled();
  const press = p.press("a");                          // off again, while "all on" is on its way
  await settled();
  assert.deepEqual([p.heart.a, p.sent.length], [false, 0], "shown at once, and not sent past the album's request");
  a.bulk[0].d.resolve({ fav: true, changed: 2 });
  await all; await settled();
  assert.deepEqual([p.heart.a, p.heart.b], [false, true], "the album's answer did not put the heart back on");
  assert.deepEqual(p.sent.map(s => [s.sha, s.fav]), [["a", false]], "then the press is sent");
  p.answer(0, 1);
  assert.equal(await press, true);
});

test("a heart pressed while the album is being saved, and the album fails: the press stands against what the server has", async () => {
  const p = page(), a = albumOn(p, ["a", "b"]);
  const all = a.make(true);
  await settled();
  const off = p.press("a");                            // back to what the server has always had
  p.press("b"); const on = p.press("b");               // off, and on again: wants what the album wanted
  a.bulk[0].d.reject(new Error("offline"));
  await all;
  assert.equal(await off, true);
  await settled();
  assert.deepEqual(p.sent.map(s => [s.sha, s.fav]), [["b", true]], "a needs no request; b's wish is sent by itself");
  p.answer(0, 1);
  assert.equal(await on, true);
  assert.deepEqual([p.heart.a, p.heart.b, p.failures], [false, true, []]);
});

test("the album waits for a heart's save under way, so the two cannot cross", async () => {
  const p = page(), a = albumOn(p, ["a", "b"]);
  p.press("a");                                        // on, being saved
  const all = a.make(false);                           // then: take all out
  await settled();
  assert.deepEqual([p.heart.a, a.bulk.length], [false, 0], "shown at once; not sent while a's save is out");
  p.answer(0, 1);
  await settled();
  assert.deepEqual(p.sent.map(s => s.fav), [true, false], "a's own save takes it to what was last asked for");
  p.answer(1, 0);
  await settled();
  assert.deepEqual(a.bulk.map(b => b.want), [false], "and then the album's request goes");
  a.bulk[0].d.resolve({ fav: false, changed: 0 });
  assert.equal((await all).ok, true);
  assert.deepEqual([p.heart.a, p.server.a, p.favs.pending], [false, false, 0]);
});

test("make all, then take all out, at once: sent one after the other, and the last is what shows", async () => {
  const p = page(), a = albumOn(p, ["a", "b"]);
  const on = a.make(true), off = a.make(false);
  await settled();
  assert.deepEqual(a.bulk.map(b => b.want), [true], "the second waits for the first");
  a.bulk[0].d.resolve({ fav: true, changed: 2 });
  await on; await settled();
  assert.deepEqual([a.bulk.map(b => b.want), p.heart.a, p.heart.b], [[true, false], false, false]);
  a.bulk[1].d.resolve({ fav: false, changed: 2 });
  assert.equal((await off).ok, true);
  assert.deepEqual([p.heart.a, p.heart.b], [false, false]);
});

test("the page left the album before the answer: no photo of another album is touched; ones loaded since are shown", async () => {
  const p = page(), a = albumOn(p, ["a"]);
  const one = a.make(true);
  await settled();
  a.view.shas = ["a", "b"];                            // another page of the album arrived
  a.bulk[0].d.resolve({ fav: true, changed: 2 });
  await one;
  assert.deepEqual([p.heart.a, p.heart.b], [true, true]);

  const q = page(), other = albumOn(q, ["a"]);
  const two = other.make(true);
  await settled();
  other.view.shas = [];                                // the page shows another album now
  q.heart.z = false;
  other.bulk[0].d.resolve({ fav: true, changed: 1 });
  await two;
  assert.deepEqual([q.heart.a, q.heart.z], [true, false], "what it showed at the start is kept right; nothing else is drawn");
});
