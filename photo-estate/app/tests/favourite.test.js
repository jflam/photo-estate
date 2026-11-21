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
