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
