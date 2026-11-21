// save-queue.js: choices saved as they are made (review.html: keep or drop, group by group). Each choice
// is put in the queue under its key; the queue sends what has gathered a moment later, in one request.
//
//   saveQueue(send, { delay, retry, onState })   -> { put(key, value), flush(), state, pending, failures }
//
// What it keeps true:
//   - one request at a time. Choices made while one is on its way wait for it and go in the next, so two
//     saves can not overlap, or arrive in the wrong order;
//   - nothing is lost when a save fails: what it carried goes back in the queue UNDER whatever was chosen
//     since ({ ...failed, ...pending }), so a choice made while the request was failing is the one the
//     retry sends, never the older one it replaced;
//   - a failed save is tried again by itself, a little later each time (`retry`), and at once by flush().
//
// Its state is a machine (fsm.js), told to onState at every step:
//   saved     nothing waiting
//   dirty     choices waiting to be sent (in `delay` ms, so a run of clicks is one request)
//   saving    a request is on its way
//   failed    the last request failed; what it carried is waiting again, and a retry is due

import { machine } from "./fsm.js";

/** @typedef {"saved" | "dirty" | "saving" | "failed"} SaveState */

/** Every step the queue may take. */
export const SAVE_STEPS = /** @type {Record<SaveState, readonly SaveState[]>} */ ({
  saved: ["dirty"],
  dirty: ["saving"],
  saving: ["saved", "dirty", "failed"],
  failed: ["saving"],
});

/** How long after the n-th failure in a row to try again: 3 s, 6 s, 12 s, 24 s, then every 30 s. @param {number} n */
export const retryAfter = n => Math.min(3000 * 2 ** (n - 1), 30000);

/**
 * @template V
 * @typedef {object} SaveQueueOptions
 * @property {number} [delay]   ms to gather choices before sending (default 400)
 * @property {(failures: number) => number} [retry]   ms to wait after that many failures in a row
 * @property {(state: SaveState, queue: { pending: number, failures: number, error: unknown }) => void} [onState]
 * @property {(fn: () => void, ms: number) => any} [setTimer]   setTimeout (tests)
 * @property {(timer: any) => void} [clearTimer]                clearTimeout (tests)
 */

/**
 * @template V
 * @typedef {object} SaveQueue
 * @property {(key: string, value: V) => void} put   this is now the value to save under `key`
 * @property {() => Promise<boolean>} flush   send what is waiting now (or after the request under way).
 *   Resolves true once nothing is waiting, false when the save failed. Never rejects.
 * @property {SaveState} state
 * @property {number} pending    how many keys are waiting to be sent
 * @property {number} failures   how many saves in a row have failed
 */

/**
 * @template V
 * @param {(body: Record<string, V>) => Promise<unknown>} send   one save: rejects when it failed
 * @param {SaveQueueOptions<V>} [options]
 * @returns {SaveQueue<V>}
 */
export function saveQueue(send, options = {}) {
  const { delay = 400, retry = retryAfter, onState, setTimer = setTimeout, clearTimer = clearTimeout } = options;
  /** @type {Record<string, V>} */ let pending = {};
  let failures = 0;
  /** @type {unknown} */ let error = null;
  /** @type {any} */ let timer = null;
  /** @type {Promise<boolean> | null} */ let flying = null;
  const count = () => Object.keys(pending).length;
  const m = machine("save", SAVE_STEPS, /** @type {SaveState} */ ("saved"),
    { strict: true, onChange: state => onState?.(state, { pending: count(), failures, error }) });
  const clear = () => { if (timer !== null) { clearTimer(timer); timer = null; } };
  /** @param {number} ms */
  const later = ms => { clear(); timer = setTimer(() => { timer = null; flush(); }, ms); };

  async function sendAll() {
    for (;;) {
      const body = pending;
      pending = {};
      m.to("saving");
      try {
        await send(body);
      } catch (e) {
        pending = { ...body, ...pending };        // what was chosen since wins over what failed
        failures++; error = e;
        m.to("failed");
        later(retry(failures));
        return false;
      }
      failures = 0; error = null;
      if (!count()) { m.to("saved"); return true; }
      m.to("dirty");                              // chosen while it was on its way: send that now
    }
  }

  function flush() {
    if (flying) return flying;                    // one at a time: what waits goes when this one ends
    clear();
    if (!count()) return Promise.resolve(m.is("saved"));
    flying = sendAll().finally(() => { flying = null; });
    return flying;
  }

  return {
    put(key, value) {
      pending[key] = value;
      if (m.is("saved")) m.to("dirty");
      if (!m.is("saving")) later(delay);          // while saving, the request's end sends it
    },
    flush,
    get state() { return m.state; },
    get pending() { return count(); },
    get failures() { return failures; },
  };
}
