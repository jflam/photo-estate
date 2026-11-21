// library-flow.js: where the Library is and what is known of it (issue #81), and the photo app's
// views switched faster than the server answers.
import test from "node:test";
import assert from "node:assert/strict";

import { libraryFlow } from "../library-flow.js";
import { albumsFlow } from "../albums-flow.js";
import { gridFlow } from "../grid-flow.js";
import { STALE } from "../fsm.js";
import { deferred, settled } from "./helpers.js";

const periodsOf = (/** @type {string} */ tag, matching = 3) => ({ tag, matching, total: matching, years: [],
  months: [{ key: "2016-06", n: 2, at: 0, cover: tag + "1" }, { key: "2016-05", n: matching - 2, at: 2, cover: tag + "2" }] });

/** A library whose two requests are answered by hand. */
function library(/** @type {Partial<import("../library-flow.js").LibraryOptions>} */ o = {}) {
  /** @type {{ what: string, q: string, signal: AbortSignal, d: ReturnType<typeof deferred> }[]} */ const asked = [];
  const ask = (/** @type {string} */ what) => (/** @type {string} */ q, /** @type {AbortSignal} */ signal) => {
    const d = deferred(); asked.push({ what, q, signal, d }); return d.promise; };
  const lib = libraryFlow({ periods: ask("periods"), layout: ask("layout"), ...o });
  return { lib, asked };
}

test("the Library's place: a scale that owns its anchor and trail, replaced whole", () => {
  const { lib } = library({ scale: "years" });
  assert.deepEqual([lib.scale, lib.anchor, lib.trail], ["years", null, []]);
  lib.set({ scale: "months", anchor: "2016", trail: [{ key: "2016", back: "years" }] });
  assert.deepEqual([lib.scale, lib.anchor, lib.trail.length], ["months", "2016", 1]);
  lib.set({ anchor: "2016-06" });                    // the same scale, another card: nothing kept from before
  assert.deepEqual([lib.scale, lib.anchor, lib.trail], ["months", "2016-06", []]);
  lib.set({ scale: "month", anchor: "2016-06", trail: [{ key: "2016-06", back: "months" }] });
  lib.set({ scale: "all" });
  assert.deepEqual([lib.scale, lib.anchor, lib.trail], ["all", null, []]);
});

test("what is known of the library: asked, answered, asked again with the old facts still up", async () => {
  /** @type {any[]} */ const drawn = [];
  const { lib, asked } = library({ onKnown: (facts, ctx) => drawn.push([facts.P.tag, ctx]) });
  assert.equal(lib.known.state, "unknown");
  assert.equal(lib.facts, null);
  let waited = false;
  const waiting = lib.settled().then(() => { waited = true; assert.deepEqual(drawn, [["a", "keep"]], "the page drew before anyone waiting went on"); });
  const loading = lib.load("sort=new", { ctx: "keep" });
  assert.equal(lib.known.state, "asking");
  assert.deepEqual(asked.map(a => [a.what, a.q]), [["periods", "sort=new"], ["layout", "sort=new"]]);
  asked[0].d.resolve(periodsOf("a"));
  asked[1].d.resolve(new Uint8Array([1, 2, 3]).buffer);
  assert.equal(await loading, "known");
  await waiting;
  assert.equal(waited, true);
  assert.deepEqual([lib.facts?.q, lib.facts?.P.tag, [...(lib.facts?.months || [])], [...(lib.facts?.shapes || [])]],
    ["sort=new", "a", [["2016-06", 2], ["2016-05", 1]], [1, 2, 3]]);

  const again = lib.load("sort=old");
  assert.equal(lib.known.state, "asking");
  assert.equal(lib.facts?.P.tag, "a", "what was known stays up while it is asked again");
  asked[2].d.resolve(periodsOf("b", 4));
  asked[3].d.resolve(new Uint8Array([1, 2, 3]).buffer);            // shapes for another number of photos: not used
  assert.equal(await again, "known");
  assert.deepEqual([lib.facts?.P.tag, lib.facts?.shapes], ["b", null]);

  const lost = lib.load("sort=best-month");
  asked[4].d.reject(new Error("offline")); asked[5].d.reject(new Error("offline"));
  assert.equal(await lost, "kept");
  assert.deepEqual([lib.known.state, lib.facts?.P.tag, lib.facts?.q], ["known", "b", "sort=old"], "a failure with the months already known leaves them known");
});

test("a library that fails the first time owns the error and what Try again does", async () => {
  const { lib, asked } = library();
  const again = () => {};
  const loading = lib.load("", { retry: again });
  let settledNow = false;
  lib.settled().then(() => { settledNow = true; });
  asked[0].d.reject(new Error("The catalog is being repaired")); asked[1].d.resolve(null);
  assert.equal(await loading, "failed");
  await settled();
  assert.equal(settledNow, true);
  assert.deepEqual([lib.known.state, lib.known.data.error.message, lib.known.data.retry, lib.facts], ["failed", "The catalog is being repaired", again, null]);
  assert.equal(await lib.covers(), false, "nothing known: no covers to ask for");
});

test("the library asked twice (two view options in a row): the first answer is dropped and its requests aborted", async () => {
  /** @type {string[]} */ const drawn = [];
  const { lib, asked } = library({ onKnown: facts => drawn.push(facts.P.tag) });
  const first = lib.load("dedupe=1"), second = lib.load("dedupe=0");
  assert.deepEqual(asked.slice(0, 2).map(a => a.signal.aborted), [true, true]);
  asked[2].d.resolve(periodsOf("second")); asked[3].d.resolve(null);
  assert.equal(await second, "known");
  asked[0].d.resolve(periodsOf("first")); asked[1].d.resolve(null);      // the first answers last
  assert.equal(await first, STALE);
  assert.deepEqual(drawn, ["second"]);
  assert.equal(lib.facts?.P.tag, "second");
});

test("covers after a favourite: taken for the same library only", async () => {
  const { lib, asked } = library();
  const loading = lib.load("q1");
  asked[0].d.resolve(periodsOf("a")); asked[1].d.resolve(null);
  await loading;
  const c1 = lib.covers();
  assert.deepEqual([asked[2].what, asked[2].q], ["periods", "q1"]);
  asked[2].d.resolve(periodsOf("a2"));
  assert.equal(await c1, true);
  assert.equal(lib.facts?.P.tag, "a2");

  const c2 = lib.covers();                           // and the library is asked again before it answers
  const reload = lib.load("q2");
  asked[3].d.resolve(periodsOf("late"));
  asked[4].d.resolve(periodsOf("b")); asked[5].d.resolve(null);
  assert.equal(await c2, false);
  await reload;
  assert.equal(lib.facts?.P.tag, "b", "the covers of the library before did not replace the new one");

  const c3 = lib.covers();
  asked[6].d.resolve(periodsOf("other", 9));         // another number of photos: not the same months
  assert.equal(await c3, false);
  assert.equal(lib.facts?.P.tag, "b");
});

test("rapid view switching (library, month, album, search and back, faster than the fetches answer): only the last view's data is shown, earlier fetches were aborted, no machine throws", async () => {
  // The page's machines, wired as search.html wires them, on a server that answers only when told.
  /** @type {{ url: string, signal: AbortSignal, d: ReturnType<typeof deferred> }[]} */ const asked = [];
  const get = (/** @type {string} */ url, /** @type {AbortSignal} */ signal) => { const d = deferred(); asked.push({ url, signal, d }); return d.promise; };
  /** @type {Record<string, any>} */ const shown = {};
  const page = (/** @type {string} */ what) => ({ first: (/** @type {any} */ p, /** @type {any} */ list) => { shown[what] = [list.of.key ?? list.of, p.results]; } });
  const lib = libraryFlow({ periods: (q, s) => get("periods?" + q, s), layout: (q, s) => get("layout?" + q, s), onKnown: f => { shown.library = f.P.tag; } });
  const albums = albumsFlow({ list: s => get("albums", s) });
  const lgrid = gridFlow("#libGrid", { fetch: (c, n, s, q) => get(`search?${q}&cursor=${c}`, s), view: page("all") });
  const mgrid = gridFlow("#monGrid", { fetch: (c, n, s, m) => get(`search?${m.q}&cursor=${m.at + c}&month=${m.key}`, s), view: page("month") });
  const agrid = gridFlow("#albGrid", { fetch: (c, n, s, id) => get(`albums/${id}/photos?cursor=${c}`, s), view: page("album") });
  const sgrid = gridFlow("#sGrid", { fetch: (c, n, s, q) => get(`search?${q.key}&cursor=${c}`, s), view: page("search") });
  const pending = /** @type {Promise<unknown>[]} */ ([]);
  const run = (/** @type {Promise<unknown>} */ p) => { pending.push(p); };

  // Library (asked, then asked again with another order before it answers) ...
  run(lib.load("sort=new")); run(lgrid.load(0, { of: "sort=new" }));
  run(lib.load("sort=old")); run(lgrid.load(0, { of: "sort=old" }));
  // ... a month, the month before it, then the month after again ...
  lib.set({ scale: "months" });
  for (const key of ["2016-06", "2016-05", "2016-06"]) {
    lib.set({ scale: "month", anchor: key, trail: [{ key, back: "months" }] });
    run(mgrid.load(0, { of: { key, at: key === "2016-06" ? 0 : 2, n: 2, q: "sort=old" } }));
  }
  // ... Albums: the list, an album, another album ...
  run(albums.load());
  albums.go(1); run(agrid.load(0, { of: 1 }));
  albums.go(2); run(agrid.load(0, { of: 2 }));
  // ... Search: a question, refined, and cleared; another question ...
  run(sgrid.load(0, { of: { key: "q=kit" } }));
  run(sgrid.load(0, { of: { key: "q=kitten" } }));
  sgrid.clear();
  run(sgrid.load(0, { of: { key: "q=dog" } }));
  // ... and back: the list of albums again, and the Library at All.
  albums.go(null); run(albums.load());
  lib.set({ scale: "all" });

  // Nothing has answered yet. What was superseded was aborted; the last of each is still wanted.
  const live = asked.filter(a => !a.signal.aborted).map(a => a.url).sort();
  assert.deepEqual(live, ["albums", "albums/2/photos?cursor=0", "layout?sort=old", "periods?sort=old", "search?q=dog&cursor=0",
    "search?sort=old&cursor=0", "search?sort=old&cursor=0&month=2016-06"]);
  assert.equal(asked.filter(a => a.signal.aborted).length, asked.length - live.length);
  assert.deepEqual(shown, {}, "nothing drawn before an answer");

  // Now everything answers, the oldest requests last.
  const answer = (/** @type {typeof asked[0]} */ a) => a.url.startsWith("periods") ? periodsOf(a.url) : a.url === "albums" ? { sets: [a.signal.aborted ? "old" : "new"] }
    : a.url.startsWith("layout") ? null : { results: [a.url], next: null };
  for (const a of [...asked].reverse()) { a.d.resolve(answer(a)); await settled(2); }
  const ends = await Promise.all(pending);           // none rejected: no machine threw
  // superseded: the library's first asking, All's first list, two months, the first list of albums, an album, two questions
  assert.equal(ends.filter(e => e === null || e === STALE).length, 8, "every superseded load ended as stale");

  assert.deepEqual(shown, {
    library: "periods?sort=old",
    all: ["sort=old", ["search?sort=old&cursor=0"]],
    month: ["2016-06", ["search?sort=old&cursor=0&month=2016-06"]],
    album: [2, ["albums/2/photos?cursor=0"]],
    search: ["q=dog", ["search?q=dog&cursor=0"]],
  });
  assert.deepEqual(albums.listing.data.sets, ["new"]);
  assert.deepEqual([lib.known.state, lib.scale, albums.where.state, lgrid.phase.state, mgrid.phase.state, agrid.phase.state, sgrid.phase.state],
    ["known", "all", "list", "ready", "ready", "ready", "ready"]);
});
