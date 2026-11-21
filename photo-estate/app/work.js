// work.js: saving, as a page does it: one at a time, and with an honest account of what happened.
//
//   single()                 -> { run(work), busy }: work that must not run twice at once (a Save button)
//   batch(items, step)       several requests in a row, and which of them were done when one failed
//   serial({ settle })       requests one after another in the order asked; when one has failed and the
//                            last has ended, `settle` is called once to put the page right
//   SKIPPED                  what single().run() resolves to when it was already busy
//
// The point of each is what happens when a request fails: the flag that says "busy" is put down again
// (a `finally`, so no path forgets it), and the page learns what the server has and has not been told.

/** What single().run() resolves to when work was already under way: this call did nothing. */
export const SKIPPED = Symbol("skipped");

/**
 * @typedef {object} Single
 * @property {<T>(work: () => Promise<T> | T) => Promise<T | typeof SKIPPED>} run  do `work`, unless work
 *   is under way (then SKIPPED). Resolves or rejects as the work does; either way it is no longer busy.
 * @property {boolean} busy
 */

/**
 * A guard for work that must not overlap itself: a second press of Save while the first is being sent
 * does nothing. Unlike a flag set before an `await` and cleared after it, this one is cleared when the
 * work fails too, so a failed save can be tried again.
 * @returns {Single}
 */
export function single() {
  let busy = false;
  return {
    async run(work) {
      if (busy) return SKIPPED;
      busy = true;
      try { return await work(); } finally { busy = false; }
    },
    get busy() { return busy; },
  };
}

/**
 * @template T
 * @typedef {object} BatchResult
 * @property {boolean} ok        every item was done
 * @property {T[]} done          the items whose step succeeded, in order
 * @property {T | null} failed   the item whose step failed, if one did
 * @property {unknown} error     what it failed with
 * @property {T[]} rest          the items after it, not tried
 */

/**
 * Do `step` for each item, one after another, stopping at the first that fails. Never rejects: the
 * answer says which items were done, which one failed and with what, and which were not tried, so the
 * page can show what the server now has and offer the rest again.
 * @template T
 * @param {readonly T[]} items
 * @param {(item: T, index: number) => Promise<unknown> | unknown} step
 * @returns {Promise<BatchResult<T>>}
 */
export async function batch(items, step) {
  /** @type {T[]} */ const done = [];
  for (let i = 0; i < items.length; i++) {
    try {
      await step(items[i], i);
    } catch (error) {
      return { ok: false, done, failed: items[i], error, rest: items.slice(i + 1) };
    }
    done.push(items[i]);
  }
  return { ok: true, done, failed: null, error: undefined, rest: [] };
}

/**
 * @template T
 * @typedef {{ ok: true, value: T } | { ok: false, error: unknown }} Outcome
 */

/**
 * @typedef {object} Serial
 * @property {<T>(send: () => Promise<T> | T) => Promise<Outcome<T>>} run  send after everything asked
 *   before it has ended. Never rejects. Resolves after `settle`, when this was the last of a run in
 *   which something failed.
 * @property {number} waiting   how many are under way or waiting their turn
 */

/**
 * Changes a page shows at once and sends behind (a mark on a photo): sent one at a time, in the order
 * made, so the server sees them as the person made them. When one fails, what the page shows may no
 * longer be what the server has. Putting that right is left until the last change asked for has ended
 * (reading the server's state while a later change is still on its way would undo that change on the
 * page, wrongly); then `settle` is called once, with the failures since the last time, to re-read the
 * server and redraw. `settle` may be async; if it fails, that is its own to show.
 *
 * @param {{ settle?: (errors: unknown[]) => Promise<unknown> | unknown }} [options]
 * @returns {Serial}
 */
export function serial({ settle } = {}) {
  /** @type {Promise<unknown>} */ let tail = Promise.resolve();
  let waiting = 0;
  /** @type {unknown[]} */ let errors = [];
  return {
    run(send) {
      waiting++;
      const mine = tail.then(async () => {
        /** @type {Outcome<any>} */ let out;
        try { out = { ok: true, value: await send() }; } catch (error) { out = { ok: false, error }; errors.push(error); }
        waiting--;
        if (waiting === 0 && errors.length) {
          const failed = errors;
          errors = [];
          try { await settle?.(failed); } catch { /* settle shows its own failures */ }
        }
        return out;
      });
      tail = mine;
      return mine;
    },
    get waiting() { return waiting; },
  };
}
