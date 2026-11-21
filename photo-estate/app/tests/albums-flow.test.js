// albums-flow.js: the Albums tab, and an album made from a search (issue #81): cancelled by moving
// on, it makes no album, asks for no further page, and never reads the page for its name.
import test from "node:test";
import assert from "node:assert/strict";

import { albumsFlow, albumMaker } from "../albums-flow.js";
import { STALE } from "../fsm.js";
import { deferred, settled } from "./helpers.js";

/** A search of `pages` pages answered by hand, and the albums made. */
function maker() {
  /** @type {{ key: string, cursor: number, signal: AbortSignal, d: ReturnType<typeof deferred> }[]} */ const asked = [];
  /** @type {any[]} */ const made = [];
  const create = deferred();
  const m = albumMaker({
    page: (key, cursor, signal) => { const d = deferred(); asked.push({ key, cursor, signal, d }); return d.promise; },
    create: album => { made.push(album); return create.promise; },
  });
  const answer = (/** @type {number} */ n, /** @type {string[]} */ shas, /** @type {number | null} */ next) =>
    asked[n].d.resolve({ results: shas.map(sha => ({ sha, city: "x" })), next });
  return { m, asked, made, create, answer };
}

test("an album from a search: every page of the search, then one request that makes it", async () => {
  const { m, asked, made, create, answer } = maker();
  const making = m.make({ key: "q=kitten", title: "Kittens", prompt: "From the search “kitten”" });
  assert.equal(m.job.state, "collecting");
  assert.deepEqual([asked[0].key, asked[0].cursor], ["q=kitten", 0]);
  answer(0, ["a", "b"], 2);
  await settled();
  assert.equal(asked[1].cursor, 2);
  answer(1, ["c"], null);
  await settled();
  assert.equal(m.job.state, "creating");
  assert.deepEqual(made, [{ title: "Kittens", prompt: "From the search “kitten”", items: [{ sha: "a" }, { sha: "b" }, { sha: "c" }] }]);
  assert.equal(m.cancel(), false, "an album being created is not cancelled");
  create.resolve({ id: 7 });
  assert.deepEqual(await making, { id: 7 });
  assert.equal(m.job.state, "idle");
  assert.deepEqual(m.job.data, {});
});

for (const why of ["navigation", "a query edit"]) {
  test(`albumFromSearch aborted by ${why}: no album created, later pages not fetched, name taken from the start`, async () => {
    // What the page shows, which moves on: the maker was handed the question and the heading when it was asked.
    const showing = { key: "q=kitten", heading: "“kitten”" };
    const { m, asked, made, answer } = maker();
    const making = m.make({ key: showing.key, title: "Kittens", prompt: `From the search ${showing.heading}` });
    answer(0, ["a", "b"], 2);
    await settled();
    assert.equal(asked.length, 2);

    if (why === "navigation") Object.assign(showing, { key: "", heading: "Albums" });      // another tab
    else Object.assign(showing, { key: "q=dog", heading: "“dog”" });                       // another question
    assert.equal(m.cancel(), true);
    assert.equal(asked[1].signal.aborted, true, "the page being read was aborted");
    assert.equal(m.job.state, "idle");

    answer(1, ["c"], 4);                             // it answers anyway
    assert.equal(await making, STALE);
    await settled();
    assert.equal(asked.length, 2, "no later page was asked for");
    assert.deepEqual(made, [], "no album was created");

    // the same again, left to finish: the album is of the question and under the name given at the start
    const again = m.make({ key: "q=kitten", title: "Kittens", prompt: "From the search “kitten”" });
    Object.assign(showing, { key: "q=cat", heading: "“cat”" });
    asked[2].d.resolve({ results: [{ sha: "a" }], next: null });
    await settled();
    assert.deepEqual(made, [{ title: "Kittens", prompt: "From the search “kitten”", items: [{ sha: "a" }] }]);
    assert.deepEqual(asked.map(a => a.key), ["q=kitten", "q=kitten", "q=kitten"]);
    void again.catch(() => {});
  });
}

test("an album from a search that fails: the job is idle again and the failure is the caller's to show", async () => {
  const a = maker();
  const making = a.m.make({ key: "q=x", title: "X", prompt: "" });
  a.asked[0].d.reject(new Error("The catalog is being repaired"));
  await assert.rejects(making, /being repaired/);
  assert.equal(a.m.job.state, "idle");
  assert.deepEqual(a.made, []);

  const b = maker();
  const none = b.m.make({ key: "q=x", title: "X", prompt: "" });
  b.answer(0, [], null);
  await assert.rejects(none, /finds no photos now/);
  assert.equal(b.m.job.state, "idle");
  assert.deepEqual(b.made, []);

  const c = maker();
  const refused = c.m.make({ key: "q=x", title: "X", prompt: "" });
  c.answer(0, ["a"], null);
  await settled();
  await assert.rejects(c.m.make({ key: "q=y", title: "Y", prompt: "" }), /still being made/, "one album at a time once the request is sent");
  c.create.reject(new Error("not allowed"));
  await assert.rejects(refused, /not allowed/);
  assert.equal(c.m.job.state, "idle");
});

test("a second make() while the first is collecting replaces it: one album", async () => {
  const { m, asked, made, create } = maker();
  const first = m.make({ key: "q=a", title: "A", prompt: "" });
  const second = m.make({ key: "q=b", title: "B", prompt: "" });
  assert.equal(asked[0].signal.aborted, true);
  asked[0].d.resolve({ results: [{ sha: "a" }], next: null });
  asked[1].d.resolve({ results: [{ sha: "b" }], next: null });
  create.resolve({ id: 1 });
  assert.equal(await first, STALE);
  await second;
  assert.deepEqual(made.map(a => a.title), ["B"]);
});

test("the Albums tab: the list or an album; the list asked twice shows the last answer only", async () => {
  /** @type {{ signal: AbortSignal, d: ReturnType<typeof deferred> }[]} */ const asked = [];
  const a = albumsFlow({ list: signal => { const d = deferred(); asked.push({ signal, d }); return d.promise; } });
  assert.equal(a.id, null);
  a.go(3);
  assert.deepEqual([a.where.state, a.id], ["album", 3]);
  a.go(5);
  assert.equal(a.id, 5);
  a.go(null);
  assert.deepEqual([a.where.state, a.id, a.where.data], ["list", null, {}]);

  const first = a.load(), second = a.load();
  assert.equal(asked[0].signal.aborted, true);
  asked[0].d.resolve({ sets: [{ id: 1 }] });
  assert.equal(await first, STALE);
  assert.equal(a.listing.state, "loading");
  asked[1].d.resolve({ sets: [{ id: 2 }] });
  assert.equal(await second, "ready");
  assert.deepEqual(a.listing.data.sets, [{ id: 2 }]);

  const third = a.load();
  asked[2].d.reject(new Error("offline"));
  assert.equal(await third, "failed");
  assert.equal(a.listing.data.error.message, "offline");
});
