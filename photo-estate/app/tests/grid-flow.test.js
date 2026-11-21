// grid-flow.js: one list of photos as search.html asks for it (issue #81): pages that arrive after
// their list was replaced are dropped, their requests aborted, and a retry timer of a list that is
// gone does nothing.
import test from "node:test";
import assert from "node:assert/strict";

import { gridFlow } from "../grid-flow.js";
import { deferred, settled, timers } from "./helpers.js";

/** A server that answers when the test says so: every request is kept, with its signal. */
function server() {
  /** @type {{ at: number, n: number, of: any, signal: AbortSignal, answer: (results?: any[], next?: number | null) => void, fail: (e?: any) => void }[]} */
  const asked = [];
  return {
    asked,
    /** @param {number} at @param {number} n @param {AbortSignal} signal @param {any} of */
    fetch(at, n, signal, of) {
      const d = deferred();
      asked.push({ at, n, of, signal, fail: (e = new Error("no answer")) => d.reject(e),
        answer: (results = Array.from({ length: n }, (_, k) => `${of}:${at + k}`), next = at + n) => d.resolve({ results, next, of }) });
      return d.promise;
    },
  };
}

/** A grid with a page that only records what it was told to draw. */
function grid(/** @type {Partial<import("../grid-flow.js").GridOptions>} */ o = {}) {
  const s = server(), t = timers();
  /** @type {any[]} */ const drawn = [];
  const g = gridFlow("#grid", { fetch: s.fetch, first: 3, step: 2, setTimer: t.setTimer, clearTimer: t.clearTimer,
    view: { first: (page, list) => drawn.push(["first", list.of, page.results]), below: r => drawn.push(["below", r]), above: (r, at) => drawn.push(["above", at, r]) }, ...o });
  return { g, s, t, drawn };
}

test("a list loads its first page, then grows at the end that is asked", async () => {
  const { g, s, drawn } = grid();
  assert.equal(g.phase.state, "empty");
  const loading = g.load(4, { of: "a", how: "fresh" });
  assert.equal(g.phase.state, "loading");
  assert.equal(g.lo, 4, "while loading, the list starts where it was asked from (ghosts are laid out there)");
  assert.deepEqual([s.asked[0].at, s.asked[0].n, s.asked[0].of], [4, 3, "a"]);
  s.asked[0].answer();
  assert.equal((await loading)?.next, 7);
  assert.equal(g.phase.state, "ready");
  assert.deepEqual([g.lo, g.next, g.of, g.end("down"), g.end("up")], [4, 7, "a", "idle", "idle"]);

  g.more(); g.more();                                // a second ask of an end that is asking does nothing
  assert.equal(g.end("down"), "asking");
  assert.equal(s.asked.length, 2);
  g.less();
  assert.deepEqual(s.asked.slice(1).map(a => [a.at, a.n]), [[7, 2], [2, 2]]);
  s.asked[1].answer(undefined, null);
  s.asked[2].answer();
  await settled();
  assert.deepEqual([g.lo, g.next, g.end("down"), g.end("up")], [2, null, "idle", "idle"]);
  assert.deepEqual(drawn, [["first", "a", ["a:4", "a:5", "a:6"]], ["below", ["a:7", "a:8"]], ["above", 2, ["a:2", "a:3"]]]);
  g.more();                                          // the end of the list: nothing more is asked for
  assert.equal(s.asked.length, 3);
});

test("a query edited mid-fetch: the earlier search's pages are dropped and the grid shows only the new query", async () => {
  const { g, s, drawn } = grid();
  const first = g.load(0, { of: "kitten" });
  const second = g.load(0, { of: "kitten on a sofa" });       // the question changed before the answer came
  assert.equal(s.asked[0].signal.aborted, true, "the earlier search's request was aborted");
  assert.equal(s.asked[1].signal.aborted, false);
  s.asked[0].answer();                               // it answers all the same (a request that ignored its signal)
  assert.equal(await first, null);
  assert.deepEqual(drawn, []);
  assert.equal(g.phase.state, "loading");
  s.asked[1].answer();
  await second;
  assert.deepEqual(drawn, [["first", "kitten on a sofa", ["kitten on a sofa:0", "kitten on a sofa:1", "kitten on a sofa:2"]]]);

  // and the same for a page of more: asked for the old question, answered after the new one was asked
  g.more();
  const third = g.load(0, { of: "dog" });
  assert.equal(s.asked[2].of, "kitten on a sofa");
  assert.equal(s.asked[2].signal.aborted, true);
  s.asked[2].answer();
  s.asked[3].answer();
  await third; await settled();
  assert.deepEqual(drawn.map(d => d[0] + " " + d[1]), ["first kitten on a sofa", "first dog"]);
  assert.equal(g.of, "dog");
  assert.equal(g.next, 3, "the old list's page did not move the new list's cursor");
  assert.equal(g.end("down"), "idle");
});

test("a first page that fails: the list is failed and owns the error and what Try again does; an old failure is dropped", async () => {
  const { g, s } = grid();
  const again = () => "again";
  const loading = g.load(0, { of: "a", retry: again });
  s.asked[0].fail(new Error("The catalog is being repaired"));
  await assert.rejects(loading, /being repaired/);
  assert.equal(g.phase.state, "failed");
  assert.equal(g.phase.data.error.message, "The catalog is being repaired");
  assert.equal(g.phase.data.retry, again);
  assert.equal(g.lo, 0);

  const old = g.load(0, { of: "b" }), now = g.load(0, { of: "c" });
  s.asked[1].fail();                                 // the list before this one fails late: nobody is told
  assert.equal(await old, null);
  assert.equal(g.phase.state, "loading");
  s.asked[2].answer();
  await now;
  assert.equal(g.of, "c");
});

test("Grid.retry(): an end whose page did not come asks again after 2 s, if it is still in sight", async () => {
  const { g, s, t } = grid({ near: end => end === "down" });
  const loading = g.load(0, { of: "a" });
  s.asked[0].answer();
  await loading;
  g.more();
  s.asked[1].fail();
  await settled();
  assert.equal(g.end("down"), "retrying");
  assert.deepEqual(t.waits, [2000]);
  g.more();                                          // not while it waits
  assert.equal(s.asked.length, 2);
  t.fire();
  assert.equal(g.end("down"), "asking", "the timer put the end back to idle and it asked again");
  assert.deepEqual([s.asked[2].at, s.asked[2].n], [3, 2]);
});

test("Grid.retry(): the timer of a list that was replaced or cleared is cleared, and firing it does nothing", async () => {
  for (const change of ["load", "clear"]) {
    /** @type {(() => void)[]} */ const fired = [];
    /** @type {any[]} */ const cleared = [];
    // timers that can still be fired after they were cleared: a callback already on its way
    const { g, s } = grid({ near: () => true, setTimer: fn => fired.push(fn), clearTimer: id => cleared.push(id) });
    const loading = g.load(0, { of: "a" });
    s.asked[0].answer();
    await loading;
    g.more();
    s.asked[1].fail();
    await settled();
    assert.equal(g.end("down"), "retrying");
    assert.equal(fired.length, 1);

    const next = change === "load" ? g.load(0, { of: "b" }) : (g.clear(), null);
    assert.ok(cleared.includes(1), "the old list's timer was cleared when the list was left");
    const asked = s.asked.length;
    assert.doesNotThrow(() => fired[0]());           // it fires all the same
    assert.equal(s.asked.length, asked, "nothing was asked for");
    assert.equal(g.phase.state, change === "load" ? "loading" : "empty");
    if (next) { s.asked[2].answer(); await next; assert.equal(g.end("down"), "idle"); assert.equal(g.of, "b"); }
  }
});

test("a month response arriving after the month changed is dropped", async () => {
  // The month list as search.html asks for it: `of` is the month ({ key, at, n }), fixed at the start.
  const s = server();
  /** @type {any[]} */ const drawn = [];
  const g = gridFlow("#monGrid", { first: 150, step: 200, view: { first: (page, list) => drawn.push([list.of.key, page.results.length]) },
    fetch: async (c, n, signal, m) => {
      const take = Math.min(n, m.n - c);
      const d = await s.fetch(m.at + c, take, signal, m.key);
      return { ...d, next: c + take < m.n ? c + take : null };
    } });
  const june = { key: "2016-06", at: 40, n: 12 }, may = { key: "2016-05", at: 52, n: 7 };
  const first = g.load(0, { of: june });
  const second = g.load(0, { of: may });             // the month before, pressed before June answered
  assert.deepEqual(s.asked.map(a => [a.of, a.at, a.n]), [["2016-06", 40, 12], ["2016-05", 52, 7]],
    "each was asked for with its own month's place in the library");
  assert.equal(s.asked[0].signal.aborted, true);
  s.asked[1].answer();
  await second;
  s.asked[0].answer();                               // June answers last
  assert.equal(await first, null);
  assert.deepEqual(drawn, [["2016-05", 7]]);
  assert.equal(g.of, may);
  assert.equal(g.next, null, "the month ends where it ends, whatever comes after it in the library");
});

test("a short page above is not drawn (it would leave a gap), and a hidden list does not grow", async () => {
  let live = true;
  const { g, s, drawn } = grid({ live: () => live });
  const loading = g.load(5, { of: "a" });
  s.asked[0].answer();
  await loading;
  g.less();
  s.asked[1].answer(["a:3"]);
  await settled();
  assert.equal(g.lo, 5);
  assert.equal(g.end("up"), "idle");
  assert.equal(drawn.length, 1);
  live = false;
  g.more(); g.less();
  assert.equal(s.asked.length, 2);
});
