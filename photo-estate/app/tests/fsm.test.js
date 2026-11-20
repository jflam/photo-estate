// fsm.js: machines, latest() and poll(). `npm test` in photo-estate/app. No timers longer than a few ms.
import test from "node:test";
import assert from "node:assert/strict";

import { machine, latest, poll, sleep, STALE, TransitionError, PollError } from "../fsm.js";
import { isAbort } from "../api.js";

const VIEWER = { closed: ["open"], open: ["leaving", "closed"], leaving: ["closed"] };
/** A promise and the functions that settle it. */
function deferred() {
  /** @type {(v?: any) => void} */ let resolve = () => {};
  /** @type {(e?: any) => void} */ let reject = () => {};
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}
/** Run `fn` with console.warn collected instead of printed. */
function warnings(/** @type {() => void} */ fn) {
  const real = console.warn, said = [];
  console.warn = (...a) => said.push(a.join(" "));
  try { fn(); } finally { console.warn = real; }
  return said;
}

/* ── machine ───────────────────────────────────────────────────────── */
test("machine: a step in the table is taken", () => {
  const m = machine("viewer", VIEWER, "closed");
  assert.equal(m.state, "closed");
  assert.equal(m.to("open"), true);
  assert.equal(m.state, "open");
  assert.equal(m.to("leaving"), true);
  assert.equal(m.to("closed"), true);
  assert.equal(m.state, "closed");
});

test("machine: a step that is not in the table is refused, reported, and changes nothing", () => {
  const m = machine("viewer", VIEWER, "closed");
  const said = warnings(() => {
    assert.equal(m.to("leaving"), false);
    assert.equal(m.to("closed"), false, "a state has no step to itself unless the table says so");
    assert.equal(m.to(/** @type {any} */ ("nowhere")), false);
  });
  assert.equal(m.state, "closed");
  assert.deepEqual(said, ["viewer: no step from closed to leaving", "viewer: no step from closed to closed",
    "viewer: no step from closed to nowhere"]);
});

test("machine: is() and can()", () => {
  const m = machine("viewer", VIEWER, "open");
  assert.equal(m.is("open"), true);
  assert.equal(m.is("closed", "open"), true);
  assert.equal(m.is("closed", "leaving"), false);
  assert.equal(m.can("leaving"), true);
  assert.equal(m.can("open"), false);
  assert.equal(m.state, "open", "can() takes no step");
});

test("machine: a step to the same state is taken when the table has it (the grid's loading -> loading)", () => {
  const m = machine("grid", { empty: ["loading", "empty"], loading: ["loading", "ready"], ready: ["empty"] }, "empty");
  assert.equal(m.to("empty"), true);
  assert.equal(m.to("loading"), true);
  assert.equal(m.to("loading"), true);
  assert.equal(m.to("ready"), true);
});

test("machine: state cannot be set from outside", () => {
  const m = machine("viewer", VIEWER, "closed");
  assert.throws(() => { "use strict"; /** @type {any} */ (m).state = "open"; }, TypeError);
  assert.equal(m.state, "closed");
});

test("machine: two machines do not share a state", () => {
  const a = machine("a", VIEWER, "closed"), b = machine("b", VIEWER, "closed");
  a.to("open");
  assert.equal(b.state, "closed");
});

test("machine: onRefuse and onChange are told", () => {
  /** @type {string[]} */ const log = [];
  const m = machine("viewer", VIEWER, "closed", {
    onRefuse: (from, to, name) => log.push(`refused ${name} ${from}>${to}`), onChange: (to, from) => log.push(`${from}>${to}`) });
  const said = warnings(() => { m.to("open"); m.to("open"); });
  assert.deepEqual(log, ["closed>open", "refused viewer open>open"]);
  assert.deepEqual(said, [], "with onRefuse the console is left alone");
});

test("machine, strict: a refused step throws and changes nothing", () => {
  const m = machine("viewer", VIEWER, "closed", { strict: true, onRefuse: () => {} });
  assert.equal(m.to("open"), true);
  assert.throws(() => m.to("open"), e => e instanceof TransitionError && e.machine === "viewer" && e.from === "open"
    && e.to === "open" && e.message === "viewer: no step from open to open");
  assert.equal(m.state, "open");
  assert.throws(() => machine("viewer", VIEWER, /** @type {any} */ ("ajar"), { strict: true }), TypeError);
});

/* ── latest ────────────────────────────────────────────────────────── */
test("latest: the work's value, and busy while it runs", async () => {
  const job = latest(), d = deferred();
  assert.equal(job.busy, false);
  const p = job.run(() => d.promise);
  assert.equal(job.busy, true);
  assert.ok(job.signal && !job.signal.aborted);
  d.resolve("photos");
  assert.equal(await p, "photos");
  assert.equal(job.busy, false);
  assert.equal(job.signal, null);
});

test("latest: a newer run cancels the older one, whose late answer is dropped", async () => {
  const job = latest(), first = deferred(), second = deferred();
  /** @type {AbortSignal[]} */ const signals = [];
  const a = job.run(s => { signals.push(s); return first.promise; });
  const b = job.run(s => { signals.push(s); return second.promise; });
  assert.equal(signals[0].aborted, true, "the first run was told to stop");
  assert.equal(signals[1].aborted, false);
  second.resolve("kittens");
  assert.equal(await b, "kittens");
  first.resolve("puppies");                 // the old answer arrives last: nobody is waiting for it
  assert.equal(await a, STALE);
});

test("latest: stale results are dropped even from work that ignores its signal, in any order", async () => {
  const job = latest(), ds = [deferred(), deferred(), deferred()];
  const runs = ds.map(d => job.run(() => d.promise));
  ds[2].resolve(3); ds[0].resolve(1); ds[1].reject(new Error("the old request failed"));
  assert.deepEqual(await Promise.all(runs), [STALE, STALE, 3]);
});

test("latest: cancel() stops the work and its run resolves STALE, whatever it does afterwards", async () => {
  const job = latest();
  /** @type {AbortSignal | undefined} */ let signal;
  const p = job.run(s => { signal = s; return sleep(10_000, s); });   // rejects with an AbortError when cancelled
  job.cancel();
  assert.equal(signal?.aborted, true);
  assert.equal(job.busy, false);
  assert.equal(await p, STALE);
});

test("latest: the failure of work that is still the latest is thrown; the controller works again after it", async () => {
  const job = latest();
  await assert.rejects(job.run(async () => { throw new Error("503"); }), /503/);
  assert.equal(job.busy, false);
  await assert.rejects(job.run(() => { throw new Error("thrown before any await"); }), /before any await/);
  assert.equal(await job.run(() => "recovered"), "recovered");
});

test("latest: an old run finishing does not mark the new one as done", async () => {
  const job = latest(), first = deferred(), second = deferred();
  const a = job.run(() => first.promise);
  const b = job.run(() => second.promise);
  first.resolve(1);
  await a;
  assert.equal(job.busy, true, "the second run is still under way");
  second.resolve(2);
  await b;
  assert.equal(job.busy, false);
});

test("latest: generation counts runs and cancels", () => {
  const job = latest(), g = job.generation;
  job.run(() => 1);
  job.cancel();
  assert.equal(job.generation, g + 2);
});

/* ── sleep ─────────────────────────────────────────────────────────── */
test("sleep waits, and a cancellation ends it at once", async () => {
  const t0 = Date.now();
  await sleep(15);
  assert.ok(Date.now() - t0 >= 10);
  const ctl = new AbortController();
  const p = sleep(60_000, ctl.signal);
  ctl.abort();
  assert.ok(isAbort(await p.catch(e => e)));
  assert.ok(isAbort(await sleep(60_000, ctl.signal).catch(e => e)), "already cancelled: no wait at all");
});

/* ── poll ──────────────────────────────────────────────────────────── */
test("poll: asks until the answer is the last one", async () => {
  const states = ["working", "working", "ready"];
  /** @type {number[]} */ const asked = [];
  const got = await poll((_, n) => { asked.push(n); return { state: states[n - 1] }; }, { until: s => s.state === "ready", every: 1 });
  assert.deepEqual(got, { state: "ready" });
  assert.deepEqual(asked, [1, 2, 3]);
});

test("poll: by default any answer but undefined is the last", async () => {
  let n = 0;
  assert.equal(await poll(() => (++n < 3 ? undefined : null), { every: 1 }), null);
  assert.equal(n, 3);
});

test("poll: bounded by tries", async () => {
  let n = 0;
  const e = await poll(() => { n++; return "busy"; }, { until: () => false, every: 1, tries: 4 }).catch(e => e);
  assert.ok(e instanceof PollError);
  assert.equal(e.bound, "tries");
  assert.equal(e.tries, 4);
  assert.equal(e.last, "busy");
  assert.equal(n, 4, "not asked a fifth time");
});

test("poll: bounded by time, without waiting past the bound", async () => {
  const t0 = Date.now();
  let n = 0;
  const e = await poll(() => { n++; return false; }, { until: v => v, every: 20, timeout: 50 }).catch(e => e);
  assert.ok(e instanceof PollError);
  assert.equal(e.bound, "timeout");
  assert.ok(n >= 2 && n <= 3, `asked ${n} times`);
  assert.ok(Date.now() - t0 < 500, "gave up when the next wait would pass the bound, not some time later");
});

test("poll: `every` can be a function: a wait that grows", async () => {
  /** @type {number[]} */ const waits = [];
  await poll((_, n) => n, { until: n => n === 4, every: (n, v) => { assert.equal(v, n); return 100 * 2 ** n; },
    sleep: async ms => { waits.push(ms); } });
  assert.deepEqual(waits, [200, 400, 800]);
});

test("poll: cancelled during a wait", async () => {
  const ctl = new AbortController();
  let n = 0;
  const p = poll(() => { n++; }, { every: 60_000, signal: ctl.signal });
  await sleep(5);
  ctl.abort();
  assert.ok(isAbort(await p.catch(e => e)));
  assert.equal(n, 1);
});

test("poll: cancelled during a step, even one that answers afterwards", async () => {
  const ctl = new AbortController(), d = deferred();
  const p = poll(() => d.promise, { signal: ctl.signal, every: 1 });
  ctl.abort();
  d.resolve("too late");
  assert.ok(isAbort(await p.catch(e => e)));
  assert.ok(isAbort(await poll(() => 1, { signal: ctl.signal }).catch(e => e)), "already cancelled: the step never runs");
});

test("poll: the step is given the signal", async () => {
  const ctl = new AbortController();
  /** @type {(AbortSignal | null)[]} */ const seen = [];
  await poll(s => { seen.push(s); return 1; }, { signal: ctl.signal });
  await poll(s => { seen.push(s); return 1; });
  assert.deepEqual(seen, [ctl.signal, null]);
});

test("poll: a failure ends it, unless retries rides it out", async () => {
  await assert.rejects(poll(() => { throw new Error("down"); }, { every: 1 }), /down/);

  const answers = [new Error("502"), new Error("502"), "busy", new Error("502"), "ready"];
  /** @type {[string, number][]} */ const told = [];
  const got = await poll((_, n) => { const a = answers[n - 1]; if (a instanceof Error) throw a; return a; },
    { until: v => v === "ready", every: 1, retries: 2, onError: (e, k) => { told.push([/** @type {Error} */ (e).message, k]); } });
  assert.equal(got, "ready");
  assert.deepEqual(told, [["502", 1], ["502", 2], ["502", 1]], "an answer resets the count of failures in a row");
});

test("poll: more failures in a row than retries ends it with the last failure", async () => {
  let n = 0;
  await assert.rejects(poll(() => { throw new Error(`failure ${++n}`); }, { every: 1, retries: 2 }), /failure 3/);
  assert.equal(n, 3);
});

test("poll: onError returning false ends it at once (a 404 is not worth asking again)", async () => {
  let n = 0;
  await assert.rejects(poll(() => { n++; throw new Error("404"); }, { every: 1, retries: 5, onError: () => false }), /404/);
  assert.equal(n, 1);
});

test("poll: a failure on the last try is what PollError remembers", async () => {
  const e = await poll(() => { throw new Error("still down"); }, { every: 1, retries: 9, tries: 2 }).catch(e => e);
  assert.ok(e instanceof PollError);
  assert.equal(/** @type {Error} */ (e.last).message, "still down");
});

test("latest + poll: a newer run stops the older poll", async () => {
  const job = latest();
  let asked = 0;
  const old = job.run(signal => poll(() => { asked++; }, { every: 2, signal }));
  await sleep(10);
  const fresh = await job.run(() => "the new view");
  const then = asked;
  assert.equal(await old, STALE);
  await sleep(10);
  assert.equal(asked, then, "the old poll asks no more");
  assert.equal(fresh, "the new view");
});
