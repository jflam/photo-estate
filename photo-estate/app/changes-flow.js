// changes-flow.js: what changes.html does over time with one change set.
//
//   settle(check, options)        ask the set's check until the app has taken a change in; bounded
//   edits(set, reload, shown)     -> { make(change, send), waiting }: marks shown at once and sent behind,
//                                 put right from the server when one fails
//   SETTLE                        how long settle() waits, and how often it asks
//
// settle(): after a set is applied or reverted the app re-reads the library, and its check says `busy`
// until it has. The page asks until it is not busy, but not for ever: after SETTLE.timeout it gives up and
// says so, and the page offers to check again. A new settle, or leaving the page, cancels the one before
// (the page runs it under fsm.js's latest()).
//
// edits(): a mark (keep this photo), an apply, a revert are shown before the server has answered. They
// are sent one at a time, in order (work.js's serial), so an apply never overtakes the mark made just
// before it. If one fails, the page may show what the server does not have: once nothing more is on its
// way the set is read again from the server and takes its place, and `shown` is told so the page redraws
// and says what failed. If the server can not be read either, the page is left as it is and `shown` is
// told that too (reconciled: false), so it can say that what it shows may be wrong.

import { until } from "./loop.js";
import { serial } from "./work.js";

/** Ask every 700 ms, for at most two minutes (as a time, and as that many asks: whichever is reached
    first); ride out three failed asks in a row. */
export const SETTLE = { every: 700, timeout: 120000, tries: 172, retries: 3 };

/**
 * @template {{ busy?: boolean }} C
 * @param {(signal: AbortSignal | null) => Promise<C>} check   the set's check, once
 * @param {import("./fsm.js").PollOptions<C>} [options]        signal, and other bounds than SETTLE's
 * @returns {Promise<import("./loop.js").Ended<C>>}  done (value: the check, no longer busy), timeout or
 *   error; rejects with an AbortError only when cancelled
 */
export function settle(check, options = {}) {
  return until(check, { until: c => !c.busy, ...SETTLE, ...options });
}

/**
 * @template {object} S
 * @param {S} set                          the set as the page shows it; replaced in place when re-read
 * @param {() => Promise<S>} reload        the set, from the server
 * @param {(what: { errors: unknown[], reconciled: boolean }) => void} shown  told after a failure, once
 *   the set has been put right (or could not be)
 * @returns {{ make: <T>(change: ((set: S) => void) | null, send: () => Promise<T>) => Promise<import("./work.js").Outcome<T>>, waiting: number }}
 */
export function edits(set, reload, shown) {
  const queue = serial({
    async settle(errors) {
      /** @type {S | null} */ let fresh = null;
      try { fresh = await reload(); } catch { /* the server can not be read: say so below */ }
      if (fresh) Object.assign(set, fresh);
      shown({ errors, reconciled: !!fresh });
    },
  });
  return {
    /** Show `change` now (it alters the set in place), then send it when its turn comes. */
    make(change, send) {
      change?.(set);
      return queue.run(send);
    },
    get waiting() { return queue.waiting; },
  };
}
