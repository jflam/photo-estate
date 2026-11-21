// compare-flow.js: a run of the model comparison goes through every search once, in order, sends each vote
// as the model it was for (not the side), and ends in the report; a late answer or a second press changes
// nothing; a vote that is not saved can be given again.
import test from "node:test";
import assert from "node:assert/strict";

import { compareRun, RUN_STEPS, winnerOf } from "../compare-flow.js";
import { flow } from "../fsm.js";
import { ApiError } from "../api.js";
import { deferred, settled, everyPair } from "./helpers.js";

/** A server and a page. `held`: paths whose answers wait to be given by hand. */
function setup({ terms = ["cats", "dogs", "boats"], answers = {}, sides = [0.9, 0.1, 0.9], held = /** @type {string[]} */ ([]), ready = { state: "done" } } = {}) {
  /** @type {any[]} */ const calls = [], seen = [];
  /** @type {Record<string, ReturnType<typeof deferred>[]>} */ const waiting = {};
  /** @type {Record<string, (body: any) => any>} */ const fail = {}, always = {};   // fail once; fail every time
  const server = { run: { id: "r1", k: 24, terms, answers: { ...answers } }, finished: /** @type {any} */ (null) };
  const answer = (/** @type {string} */ path, /** @type {any} */ body) => {
    const key = Object.keys({ ...fail, ...always }).find(k => path.startsWith(k));
    if (key && always[key]) throw always[key](body);
    if (key) { const f = fail[key]; delete fail[key]; throw f(body); }
    if (path === "/api/compare/run") return JSON.parse(JSON.stringify(server.run));
    if (path.startsWith("/api/compare?")) {
      const q = decodeURIComponent(path.split("q=")[1]);
      return { q, overlap: 7, a: { text_ms: 20, results: Array.from({ length: 24 }, (_, i) => ({ id: `a${i}` })) }, b: { text_ms: 60, results: [] } };
    }
    if (path === "/api/compare/vote") { server.run.answers[body.q] = body.winner; return { ok: true }; }
    if (path === "/api/compare/run/finish") { server.finished = body; return { id: body.run, verdict: "done", count: {} }; }
    if (path.startsWith("/api/compare/report")) return { id: path.split("run=")[1], verdict: "an older one" };
    throw new Error("no such path " + path);
  };
  const api = async (/** @type {string} */ path, /** @type {any} */ body) => {
    calls.push([path.split("?")[0] + (path.includes("q=") ? "?" + decodeURIComponent(path.split("q=")[1]) : ""), body]);
    const key = held.find(h => path.startsWith(h));
    if (key) { const d = deferred(); (waiting[key] ||= []).push(d); await d.promise; }
    return answer(path, body);
  };
  let s = 0;
  const run = compareRun({
    api, ready: async () => ready, random: () => sides[s++ % sides.length],
    view: {
      waiting: r => seen.push(["waiting", r.id]), asking: (t, i, n) => seen.push(["asking", t, i, n]),
      judge: q => seen.push(["judge", q.term, q.order.join("")]), note: t => seen.push(["note", t]),
      report: r => seen.push(["report", r.id]), failed: msg => seen.push(["failed", msg]),
    },
  });
  return { run, calls, seen, server, waiting, fail, always };
}
const votes = (/** @type {any[]} */ calls) => calls.filter(c => c[0] === "/api/compare/vote").map(c => [c[1].q, c[1].winner, c[1].left]);

test("the run's steps: every legal step is taken, every other pair throws", () => {
  const { legal, refused } = everyPair(RUN_STEPS, (steps, from) => flow("run", steps, from), assert.throws);
  assert.deepEqual([legal, legal + refused], [Object.values(RUN_STEPS).flat().length, 81]);
});

test("left and right are the models that were there, not the sides", () => {
  assert.deepEqual([winnerOf("left", ["b", "a"]), winnerOf("right", ["b", "a"]), winnerOf("same", ["b", "a"]), winnerOf("skip", ["a", "b"])],
    ["b", "a", "same", "skip"]);
});

test("a run asks every search once, in order, and ends in the report", async () => {
  const p = setup();
  await p.run.start();
  assert.deepEqual(p.seen, [["waiting", "r1"], ["asking", "cats", 0, 3], ["judge", "cats", "ba"]]);
  await p.run.vote("left");                       // the left was model b
  await p.run.vote("left");                       // the second search has a on the left
  await p.run.vote("same");
  assert.deepEqual(votes(p.calls), [["cats", "b", "b"], ["dogs", "a", "a"], ["boats", "same", "b"]]);
  assert.deepEqual(p.calls.at(-1), ["/api/compare/run/finish", { run: "r1", early: false }]);
  assert.equal(p.run.state, "report");
  assert.deepEqual(p.seen.at(-1), ["report", "r1"]);
  const sent = p.calls.find(c => c[0] === "/api/compare/vote")[1];
  assert.deepEqual([sent.run, sent.k, sent.overlap, sent.a_ms, sent.b_ms], ["r1", 24, 7, 20, 60], "what the report is made from goes with the vote");
});

test("a run that was left is carried on at its first unanswered search", async () => {
  const p = setup({ answers: { cats: "a", dogs: "skip" } });
  await p.run.start();
  assert.deepEqual(p.seen.filter(s => s[0] === "asking"), [["asking", "boats", 2, 3]]);
  await p.run.vote("skip");
  assert.equal(p.run.state, "report");
  const done = setup({ answers: { cats: "a", dogs: "b", boats: "same" } });   // all answered, never finished: straight to the report
  await done.run.start();
  assert.deepEqual([done.run.state, votes(done.calls).length], ["report", 0]);
});

test("a second press while the vote is being sent is not a second vote", async () => {
  const p = setup({ held: ["/api/compare/vote"] });
  await p.run.start();
  const first = p.run.vote("left");
  p.run.vote("right"); p.run.vote("same");
  assert.equal(p.run.state, "saving");
  p.waiting["/api/compare/vote"][0].resolve();
  await first;
  assert.deepEqual(votes(p.calls), [["cats", "b", "b"]]);
  assert.deepEqual(p.seen.at(-1), ["judge", "dogs", "ab"]);
});

test("a vote that could not be saved is said, and can be given again", async () => {
  const p = setup();
  await p.run.start();
  p.fail["/api/compare/vote"] = () => new ApiError("database is locked", { kind: "http", status: 500 });
  await p.run.vote("right");
  assert.equal(p.run.state, "judging");
  assert.match(p.seen.at(-1)[1], /not saved \(database is locked\)/);
  assert.equal(p.run.data.term, "cats", "still the same search, with the same sides");
  await p.run.vote("right");
  assert.deepEqual(votes(p.calls).map(v => v[0]), ["cats", "cats"]);
  assert.deepEqual(p.seen.at(-1), ["judge", "dogs", "ab"]);
});

test("the model was let go while the run sat: a 503 loads it again, and the search is asked again", async () => {
  const p = setup({ answers: { cats: "a" } });
  p.fail["/api/compare?"] = () => new ApiError("the second model is loading", { kind: "http", status: 503 });
  await p.run.start();
  assert.deepEqual(p.calls.map(c => c[0]), ["/api/compare/run", "/api/compare?dogs", "/api/compare/run", "/api/compare?dogs"]);
  assert.equal(p.run.state, "judging");
  const stuck = setup();                           // and one that never loads is a failure to retry, not a loop
  stuck.always["/api/compare?"] = () => new ApiError("the second model is loading", { kind: "http", status: 503 });
  await stuck.run.start();
  assert.deepEqual([stuck.run.state, stuck.calls.filter(c => c[0] === "/api/compare?cats").length], ["failed", 3]);
});

test("the model does not load: a failure that start() tries again", async () => {
  const p = setup({ ready: /** @type {any} */ ({ state: "timeout" }) });
  await p.run.start();
  assert.deepEqual([p.run.state, p.seen.at(-1)[0]], ["failed", "failed"]);
  assert.equal(p.calls.filter(c => c[0].startsWith("/api/compare?")).length, 0, "no search is asked of a model that is not there");
  const q = setup();
  q.fail["/api/compare/run"] = () => new ApiError("not allowed", { kind: "http", status: 403 });
  await q.run.start();
  assert.deepEqual(q.seen, [["failed", "not allowed"]]);
  await q.run.start();
  assert.equal(q.run.state, "judging");
});

test("finishing early sends early, and a fresh run says so", async () => {
  const p = setup();
  await p.run.start();
  await p.run.vote("same");
  await p.run.finish(true);
  assert.deepEqual(p.calls.at(-1), ["/api/compare/run/finish", { run: "r1", early: true }]);
  assert.equal(p.run.state, "report");
  p.server.run = { id: "r2", k: 24, terms: ["cats"], answers: {} };
  await p.run.start(true);
  assert.deepEqual(p.calls.find(c => c[0] === "/api/compare/run" && c[1].fresh), ["/api/compare/run", { fresh: true }]);
  assert.equal(p.run.data.run.id, "r2");
});

test("an older report is shown from idle or from another report, never over a run under way", async () => {
  const p = setup();
  await p.run.show("20261101-101010");
  assert.deepEqual([p.run.state, p.seen.at(-1)], ["report", ["report", "20261101-101010"]]);
  await p.run.start();
  await p.run.show("20261101-101010");
  assert.equal(p.run.state, "judging", "a run under way is not replaced by a report");
});

test("an answer that comes after the run has moved on is dropped", async () => {
  const p = setup({ held: ["/api/compare?"] });
  const going = p.run.start();
  await settled();
  assert.equal(p.run.state, "asking");
  const early = p.run.finish(true);                 // not from asking: nothing happens
  await early;
  assert.equal(p.run.state, "asking");
  p.waiting["/api/compare?"][0].resolve();
  await going;
  assert.equal(p.run.state, "judging");
});
