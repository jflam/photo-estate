// What the tests of the flow modules share: a promise settled from outside, and timers fired by hand.

/** A promise and the functions that settle it. */
export function deferred() {
  /** @type {(v?: any) => void} */ let resolve = () => {};
  /** @type {(e?: any) => void} */ let reject = () => {};
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

/** Let everything already settled run its `then`s. */
export const settled = async (turns = 12) => { for (let i = 0; i < turns; i++) await Promise.resolve(); };

/** setTimeout and clearTimeout that wait to be fired: `waits` lists the ms of each timer still set. */
export function timers() {
  /** @type {{ id: number, fn: () => void, ms: number }[]} */ let set = [];
  let n = 0;
  return {
    /** @param {() => void} fn @param {number} ms */
    setTimer(fn, ms) { set.push({ id: ++n, fn, ms }); return n; },
    /** @param {number} id */
    clearTimer(id) { set = set.filter(t => t.id !== id); },
    get waits() { return set.map(t => t.ms); },
    /** Fire the timer set first. */
    fire() { const t = set.shift(); if (!t) throw new Error("no timer is set"); t.fn(); },
  };
}

/** A sleep for poll() that does not wait, records what it was asked, and still obeys the signal. */
export function naps() {
  /** @type {number[]} */ const asked = [];
  return {
    asked,
    /** @param {number} ms @param {AbortSignal | null} [signal] */
    async sleep(ms, signal) {
      asked.push(ms);
      await Promise.resolve();
      if (signal?.aborted) throw new DOMException("The wait was cancelled", "AbortError");
    },
  };
}

/**
 * Every pair of states of a transition table, tried on a machine put in the first: the steps in the
 * table are taken, every other pair throws a TransitionError and leaves the machine where it was.
 * `make(steps, from)` makes the machine (fsm.js's flow, or machine with strict). Returns how many
 * pairs were legal and how many refused.
 * @param {Record<string, readonly string[]>} steps
 * @param {(steps: any, from: any) => { state: string, to: (next: any) => unknown }} make
 * @param {(fn: () => unknown, check: (e: any) => boolean, message: string) => void} throws  assert.throws
 */
export function everyPair(steps, make, throws) {
  const states = Object.keys(steps);
  let legal = 0, refused = 0;
  for (const tos of Object.values(steps)) for (const to of tos) if (!states.includes(to)) throw new Error(`${to} is not a state`);
  for (const from of states) for (const to of states) {
    const m = make(steps, from);
    if (steps[from].includes(to)) {
      m.to(to);
      if (m.state !== to) throw new Error(`${from} -> ${to} was not taken`);
      legal++;
    } else {
      throws(() => m.to(to), e => e.name === "TransitionError" && e.from === from && e.to === to, `${from} -> ${to}`);
      if (m.state !== from) throw new Error(`${from} -> ${to} was refused, and the machine moved`);
      refused++;
    }
  }
  return { legal, refused };
}
