// loop.js: a page that keeps itself up to date asks the server again and again for as long as it is open
// (a job's progress, the uploads waiting, the tasks an agent is working on). loop() is the one way to do
// that: one step at a time, the next one only after the last has ended, stopped or paused from outside.
//
//   loop(step, { every, onError })    -> { start(), stop(), now(), pause(), resume(), on, busy }
//   until(step, options)              poll() (fsm.js) that answers instead of throwing: how it ended
//
// Where fsm.js's poll() asks until an answer is the last one, a loop has no last answer: it runs until it
// is stopped, or until its step says there is nothing more to wait for. What it guarantees:
//   - never two steps at once. A step is started only when the one before has ended, whatever start(),
//     now() and resume() are called meanwhile (a timer and a click can not both start one);
//   - the step says how long to wait: it returns the ms until the next one (so a page asks often while
//     something is running and seldom when nothing is), nothing for `every`, or false to stop;
//   - a failing step does not end the loop: onError is told and says how long to wait (or false to stop);
//   - stop() aborts the step under way (its signal) and nothing it returns is acted on.
//
// Nothing here touches the page: the page says when to pause (document.hidden) and when to stop (pagehide).

import { poll, PollError } from "./fsm.js";
import { isAbort } from "./api.js";

/**
 * @typedef {object} LoopOptions
 * @property {number} [every]  ms between steps when the step does not say (default 1000)
 * @property {(error: unknown) => number | false | void} [onError]  told when a step fails; returns the ms
 *   to wait before the next one, nothing for `every`, or false to stop the loop
 * @property {(fn: () => void, ms: number) => any} [setTimer]   setTimeout (tests)
 * @property {(timer: any) => void} [clearTimer]                clearTimeout (tests)
 */

/**
 * @typedef {object} Loop
 * @property {() => void} start    begin: a step now, unless one is under way
 * @property {() => void} stop     end: the step under way is aborted, no other follows
 * @property {() => void} now      a step as soon as possible: now, or right after the one under way. Starts a stopped loop.
 * @property {() => void} pause    no further steps until resume(); the one under way finishes
 * @property {() => void} resume   after pause(): a step now
 * @property {boolean} on          started and not stopped (paused or not)
 * @property {boolean} busy        a step is under way
 */

/**
 * @param {(signal: AbortSignal) => Promise<number | false | null | void> | number | false | null | void} step
 * @param {LoopOptions} [options]
 * @returns {Loop}
 */
export function loop(step, options = {}) {
  const { every = 1000, onError, setTimer = setTimeout, clearTimer = clearTimeout } = options;
  let on = false, paused = false, busy = false, again = false;
  /** @type {any} */ let timer = null;
  /** @type {AbortController | null} */ let ctl = null;
  const clear = () => { if (timer !== null) { clearTimer(timer); timer = null; } };

  async function tick() {
    if (!on || paused) return;
    if (busy) { again = true; return; }
    clear();
    busy = true; again = false;
    const own = ctl = new AbortController();
    /** @type {number | false | null | void} */ let next;
    try {
      next = await step(own.signal);
    } catch (e) {
      next = own.signal.aborted || !onError ? undefined : onError(e);
    } finally {
      busy = false;
      if (ctl === own) ctl = null;
    }
    if (own.signal.aborted) {                 // stopped while it ran: what it said does not count
      if (on && again) tick();                // ...but it was started again meanwhile
      return;
    }
    if (!on) return;
    if (next === false || next === null) { on = false; return; }
    if (again) { tick(); return; }
    if (!paused) timer = setTimer(() => { timer = null; tick(); }, typeof next === "number" ? next : every);
  }

  return {
    start() { on = true; tick(); },
    stop() { on = false; again = false; clear(); ctl?.abort(); ctl = null; },
    now() { on = true; tick(); },
    pause() { paused = true; clear(); },
    resume() { if (!paused) return; paused = false; tick(); },
    get on() { return on; },
    get busy() { return busy; },
  };
}

/**
 * @template T
 * @typedef {{ state: "done", value: T } | { state: "timeout", last: unknown } | { state: "error", error: unknown }} Ended
 */

/**
 * poll(), for a page that shows how the wait ended instead of catching it: resolves to
 *   { state: "done", value }      an answer was the last one
 *   { state: "timeout", last }    a bound (tries or timeout) was reached first; `last` is the last answer
 *   { state: "error", error }     the step failed (more often than `retries` allows)
 * A cancelled wait still rejects with its AbortError: nobody is waiting, so there is nothing to show.
 *
 * @template T
 * @param {(signal: AbortSignal | null, tries: number) => Promise<T> | T} step
 * @param {import("./fsm.js").PollOptions<T>} [options]
 * @returns {Promise<Ended<T>>}
 */
export async function until(step, options = {}) {
  try {
    return { state: "done", value: await poll(step, options) };
  } catch (e) {
    if (isAbort(e)) throw e;
    if (e instanceof PollError) return { state: "timeout", last: e.last };
    return { state: "error", error: e };
  }
}
