// fsm.js: the two things a page needs to stay in step with itself: states that say what they are, and
// work that takes time without its answer landing in the wrong place.
//
//   machine(name, steps, state, options)   a state machine with a table of the steps it may take
//   flow(name, steps, state, onChange)     a strict machine whose states own their data
//   latest()                               one piece of async work at a time: a new run cancels the one before,
//                                          and an answer nobody is waiting for any more is dropped (STALE)
//   poll(step, options)                    ask again and again until an answer is the last one; bounded, cancellable
//   sleep(ms, signal)                      a pause that a cancellation ends at once
//   STALE, TransitionError, PollError
//
// Nothing here touches the page, so all of it runs under `node --test` (app/tests/fsm.test.js).
//
// The rule about steps (issue #81). A step the code asks for that the table does not have is a bug in
// the code, never something to carry on from. So the machines of the photo app (search.html) and of
// the setup page are strict: flow(), or machine() with `strict: true`. A refused step throws a
// TransitionError before anything has changed: the machine is where it was, and so is the data its
// state owns, because that data is handed over in the same call (flow's `to(next, data)`) and not
// written beside it afterwards. Code that takes a step is therefore in one of two positions:
//
//   - the step is legal wherever the code can be reached (a tap in the field starts typing): it just
//     takes it;
//   - the event can arrive in more than one state (a second finger, Back, an answer from the server):
//     it asks first, with is() or can(), and does nothing when the answer is no. A late answer is not
//     such a case: it is dropped before it reaches a machine at all, by the signal of the work it
//     belonged to (latest()) or because the state object it was asked for is no longer the one the
//     machine holds (`flow.data !== mine`).
//
// A TransitionError that does get thrown surfaces where any bug does (an uncaught error or an
// unhandled rejection, in the console), with the machine and the two states in its message, and the
// page carries on from the state it was in. machine() without `strict` still only reports a refused
// step and returns false: the secondary pages' save queue uses that, and checks the answer.

/* ── machines. Whatever in the page is in one of several states (a list loading, the photo viewer, a finger on
      the screen) says so in one place: its name, the state it is in, and every step it may take. A step that
      is not in its table is refused: reported, and in a strict machine thrown, so a mistake shows as a line
      in the console naming the two states, not as a page that is subtly wrong some time later. ── */

/** A step a strict machine was asked to take that is not in its table. */
export class TransitionError extends Error {
  /**
   * @param {string} name   the machine's name
   * @param {string} from
   * @param {string} to
   */
  constructor(name, from, to) {
    super(`${name}: no step from ${from} to ${to}`);
    this.name = "TransitionError";
    this.machine = name;
    this.from = from;
    this.to = to;
  }
}

/**
 * @template {string} S
 * @typedef {object} MachineOptions
 * @property {boolean} [strict]  a refused step throws a TransitionError instead of returning false.
 *   Off by default: a refused step is reported (onRefuse) and the machine stays where it is.
 * @property {(from: S, to: S, name: string) => void} [onRefuse]  told of every refused step, before a
 *   strict machine throws. By default a console.warn naming the machine and the two states.
 * @property {(to: S, from: S) => void} [onChange]  told after every step taken
 */

/**
 * @template {string} S
 * @typedef {object} Machine
 * @property {S} state                         the state it is in (read only)
 * @property {(...names: S[]) => boolean} is   whether it is in one of these states
 * @property {(next: S) => boolean} can        whether the table has a step from here to `next`
 * @property {(next: S) => boolean} to         take the step: true if taken. Refused: false (or, strict, a throw)
 */

/**
 * A state machine: `steps` lists, for every state, the states it may go to next.
 *
 *     const VW = machine("viewer", { closed: ["open"], open: ["leaving", "closed"], leaving: ["closed"] }, "closed");
 *     VW.to("open");      // true
 *     VW.to("leaving");   // true
 *     VW.to("open");      // false, and a console.warn: "viewer: no step from leaving to open"
 *
 * @template {string} S
 * @param {string} name                      for the report of a refused step
 * @param {Record<S, readonly S[]>} steps    state -> the states it may step to
 * @param {S} state                          where it starts
 * @param {MachineOptions<S>} [options]
 * @returns {Machine<S>}
 */
export function machine(name, steps, state, options = {}) {
  const { strict = false, onChange } = options;
  const onRefuse = options.onRefuse || ((from, to) => console.warn(`${name}: no step from ${from} to ${to}`));
  if (strict && !(state in steps)) throw new TypeError(`${name}: ${state} is not one of its states`);
  /** @param {S} next */
  const can = next => (steps[state] || []).includes(next);
  return {
    get state() { return state; },
    is: (...names) => names.includes(state),
    can,
    to(next) {
      if (!can(next)) {
        onRefuse(state, next, name);
        if (strict) throw new TransitionError(name, state, next);
        return false;
      }
      const from = state;
      state = next;
      onChange?.(next, from);
      return true;
    },
  };
}

/**
 * @template {string} S
 * @typedef {object} Flow
 * @property {S} state
 * @property {Record<string, any>} data            what the state it is in owns
 * @property {(...names: S[]) => boolean} is
 * @property {(next: S) => boolean} can
 * @property {(next: S, data?: Record<string, any>) => Record<string, any>} to  take the step, with the
 *   data the new state owns (nothing by default: the old state's data is dropped). Returns that data.
 *   A step that is not in the table throws a TransitionError and changes nothing.
 */

/**
 * A strict machine whose states own their data: what a state needs to know is handed over at the step
 * into it and gone at the step out of it, so nothing is left behind by a state that has ended. The
 * data object is also the state's identity: work started for a state keeps the object, and when it
 * ends compares it with `flow.data` to learn whether its answer is still wanted.
 *
 *     const VW = flow("viewer", { closed: ["open"], open: ["closed"] }, "closed");
 *     const mine = VW.to("open", { photo: 3 });
 *     VW.to("closed");                  // VW.data is {} again: there is no photo of a closed viewer
 *     VW.data === mine;                 // false: whatever was started for that opening is stale
 *
 * `onChange(to, from, data, old)` is told after every step, with the data of the state entered and of
 * the state left (to stop what that one had under way: a timer, a request).
 *
 * @template {string} S
 * @param {string} name
 * @param {Record<S, readonly S[]>} steps
 * @param {S} state
 * @param {(to: S, from: S, data: Record<string, any>, old: Record<string, any>) => void} [onChange]
 * @returns {Flow<S>}
 */
export function flow(name, steps, state, onChange) {
  const m = machine(name, steps, state, { strict: true, onRefuse: () => {} });
  /** @type {Record<string, any>} */ let data = {};
  return {
    get state() { return m.state; },
    get data() { return data; },
    is: m.is,
    can: m.can,
    to(next, d = {}) {
      const from = m.state, old = data;
      m.to(next);
      data = d;
      onChange?.(next, from, d, old);
      return d;
    },
  };
}

/* ── work that takes time. A page asks for something, and before the answer comes the person has moved on:
      asked for something else, closed the view, left. The answer to the old question must not be shown, and
      the request for it is best stopped. ── */

/** What latest().run() resolves to when its work was replaced or cancelled before it finished. */
export const STALE = Symbol("stale");

/**
 * @typedef {object} Latest
 * @property {<T>(work: (signal: AbortSignal) => Promise<T> | T) => Promise<T | typeof STALE>} run
 *   Start `work`, cancelling whatever run() started before it. Resolves to the work's value, or STALE if a
 *   later run() or cancel() overtook it (whether the old work then finished, failed, or was aborted: its
 *   outcome is dropped). Rejects only with the failure of work that is still the latest.
 * @property {() => void} cancel   stop the work under way, if any: its run() resolves STALE
 * @property {boolean} busy        whether the latest run is still under way
 * @property {number} generation   how many runs and cancels there have been
 * @property {AbortSignal | null} signal   the signal of the work under way, or null
 */

/**
 * One piece of async work at a time (a list being loaded, a page polling for progress). The controller
 * owns the AbortController: each run() gets a fresh signal to hand to api() and poll(), and the run
 * before it is aborted. A generation count guards the result, so an answer that arrives after its
 * question was replaced is never returned, even from work that ignores its signal.
 *
 *     const loading = latest();
 *     async function show(id) {
 *       const d = await loading.run(signal => api(`/api/thing/${id}`, undefined, { signal }));
 *       if (d === STALE) return;          // show(other) was called meanwhile: that one draws
 *       draw(d);
 *     }
 *
 * @returns {Latest}
 */
export function latest() {
  let generation = 0;
  /** @type {AbortController | null} */
  let ctl = null;
  const cancel = () => { generation++; ctl?.abort(); ctl = null; };
  return {
    async run(work) {
      cancel();
      const mine = generation, own = ctl = new AbortController();
      try {
        const value = await work(own.signal);
        return mine === generation ? value : STALE;
      } catch (e) {
        if (mine !== generation) return STALE;
        throw e;
      } finally {
        if (ctl === own) ctl = null;
      }
    },
    cancel,
    get busy() { return ctl !== null; },
    get generation() { return generation; },
    get signal() { return ctl ? ctl.signal : null; },
  };
}

/**
 * Wait `ms`. Rejects at once with an AbortError when `signal` is, or becomes, aborted.
 * @param {number} ms
 * @param {AbortSignal | null} [signal]
 * @returns {Promise<void>}
 */
export function sleep(ms, signal) {
  return new Promise((resolve, reject) => {
    const stopped = () => new DOMException("The wait was cancelled", "AbortError");
    if (signal?.aborted) return reject(stopped());
    const done = () => { signal?.removeEventListener("abort", abort); resolve(); };
    const abort = () => { clearTimeout(timer); reject(stopped()); };
    const timer = setTimeout(done, ms);
    signal?.addEventListener("abort", abort, { once: true });
  });
}

/** poll() gave up: it reached its bound (`tries` or `timeout`) before an answer was the last one. */
export class PollError extends Error {
  /**
   * @param {"tries" | "timeout"} bound   which bound was reached
   * @param {number} tries                how many times it asked
   * @param {unknown} last                the last answer (or the last failure, if it ended on one)
   */
  constructor(bound, tries, last) {
    super(bound === "tries" ? `Gave up after asking ${tries} times` : `Gave up after ${tries} tries: it took too long`);
    this.name = "PollError";
    this.bound = bound;
    this.tries = tries;
    this.last = last;
  }
}

/**
 * @template T
 * @typedef {object} PollOptions
 * @property {(value: T, tries: number) => boolean} [until]  whether this answer is the last one. By
 *   default any answer but undefined is (so `step` returns nothing to be asked again).
 * @property {number | ((tries: number, value: T | undefined) => number)} [every]  ms between asks
 *   (default 1000), or a function of how many asks there have been and of the last answer (undefined
 *   after a failure), for a wait that grows
 * @property {number} [tries]     ask at most this many times (default: no limit)
 * @property {number} [timeout]   give up this many ms after starting (default: no limit)
 * @property {number} [retries]   how many failures in a row to ask again after (default 0: the first
 *   failure ends the poll). An answer resets the count.
 * @property {(error: unknown, failures: number) => boolean | void} [onError]  told of each failure that
 *   will be retried; returning false ends the poll with that failure instead
 * @property {AbortSignal | null} [signal]  cancels the poll, during a step or a wait: an AbortError
 * @property {(ms: number, signal?: AbortSignal | null) => Promise<void>} [sleep]  the wait (tests)
 */

/**
 * Ask until done: call `step` again and again, `every` ms apart, until an answer is the last one
 * (`until`), and resolve to it. Never runs for ever unless asked to: `tries` and `timeout` bound it (a
 * PollError when reached), `signal` cancels it (an AbortError), and a failing `step` ends it with that
 * failure unless `retries` says how many in a row to ride out.
 *
 *     const done = await poll(signal => api(`/api/changes/${id}/check`, undefined, { signal }),
 *                             { until: c => !c.busy, every: 700, timeout: 120000, retries: 3, signal });
 *
 * @template T
 * @param {(signal: AbortSignal | null, tries: number) => Promise<T> | T} step  `tries` counts from 1
 * @param {PollOptions<T>} [options]
 * @returns {Promise<T>}
 */
export async function poll(step, options = {}) {
  const { until = v => v !== undefined, every = 1000, tries = Infinity, timeout = Infinity, retries = 0, onError,
    signal = null, sleep: wait = sleep } = options;
  const aborted = () => new DOMException("The poll was cancelled", "AbortError");
  const started = Date.now();
  /** @type {T | undefined} */
  let value;
  /** @type {unknown} */
  let last;
  let failures = 0;
  for (let n = 1; ; n++) {
    if (signal?.aborted) throw aborted();
    try {
      value = await step(signal, n);
      failures = 0;
      last = value;
    } catch (e) {
      if (signal?.aborted) throw aborted();
      if (++failures > retries || onError?.(e, failures) === false) throw e;
      value = undefined;
      last = e;
    }
    if (signal?.aborted) throw aborted();
    if (failures === 0 && until(/** @type {T} */ (value), n)) return /** @type {T} */ (value);
    if (n >= tries) throw new PollError("tries", n, last);
    const pause = typeof every === "function" ? every(n, value) : every;
    if (Date.now() - started + pause > timeout) throw new PollError("timeout", n, last);
    await wait(pause, signal);
  }
}
