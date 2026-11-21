// stats-watch.js: how stats.html keeps itself up to date. One loop (loop.js) asks for both things the
// page shows, one request at a time:
//
//   /api/ingest   the photos uploaded from the phone and the run that catalogues them: every 2 s while
//                 a run is going, every 15 s otherwise (not at all once the server says uploads are off)
//   /api/stats    the figures: every 1.5 s while the server is still measuring ("working"), then no more
//                 until measure() asks for them again
//
//   statsWatch(api, view)    -> { start(), stop(), pause(), resume(), measure(), retry(), now() }
//   problem(error)           -> { title, text }: what to tell a person about a failed /api/stats
//
// Every request goes through api.js, so an answer that is not 2xx is an ApiError, shown as an error and
// never drawn as if it were figures. `view` is the page:
//   view.stats(d)          the figures (d.state "working" means more are coming; d.disk may not be there yet)
//   view.statsError(e)     they could not be had: an ApiError, or an Error with the server's own words
//                          when it answered { state: "error" }. No more are asked for until retry().
//   view.uploads(u)        the answer of /api/ingest
//   view.uploadsError(e)   it could not be had (asked again in 15 s)

import { loop } from "./loop.js";
import { isAbort, ApiError } from "./api.js";

export const WORKING_MS = 1500, RUNNING_MS = 2000, IDLE_MS = 15000;

/**
 * @typedef {object} StatsView
 * @property {(d: any) => void} stats
 * @property {(e: unknown) => void} statsError
 * @property {(u: any) => void} uploads
 * @property {(e: unknown) => void} [uploadsError]
 */

/**
 * What to tell a person about figures that could not be had.
 * @param {unknown} e
 * @returns {{ title: string, text: string }}
 */
export function problem(e) {
  if (e instanceof ApiError && (e.kind === "network" || e.kind === "timeout")) {
    return { title: "Can’t reach the library", text: "The server didn’t answer." };
  }
  const text = e instanceof Error && e.message ? e.message : "The server gave no reason.";
  return { title: "Couldn’t take stock", text };
}

/**
 * @param {import("./api.js").Api} api
 * @param {StatsView} view
 * @param {import("./loop.js").LoopOptions} [options]   timers (tests)
 */
export function statsWatch(api, view, options = {}) {
  let wantStats = true, refresh = false, uploadsOff = false;
  const L = loop(async signal => {
    /** @type {any} */ let u = null;
    if (!uploadsOff) {
      try {
        u = await api("/api/ingest", undefined, { signal });
        uploadsOff = !u.enabled;
        view.uploads(u);
      } catch (e) {
        if (isAbort(e)) throw e;
        view.uploadsError?.(e);
      }
    }
    if (wantStats) {
      const path = "/api/stats" + (refresh ? "?refresh=1" : "");
      refresh = false;                               // asked for once: the asks that follow only read
      try {
        const d = await api(path, undefined, { signal });
        if (d.state === "error") throw new Error(d.error || "");
        view.stats(d);
        wantStats = d.state === "working";
      } catch (e) {
        if (isAbort(e)) throw e;
        wantStats = false;
        view.statsError(e);
      }
    }
    if (wantStats) return WORKING_MS;
    if (uploadsOff) return false;                    // nothing left to wait for
    return u?.running ? RUNNING_MS : IDLE_MS;
  }, { onError: () => IDLE_MS, ...options });
  return {
    start: L.start, stop: L.stop, pause: L.pause, resume: L.resume, now: L.now,
    /** Measure the disk again, and show the figures as they come. */
    measure() { wantStats = true; refresh = true; L.now(); },
    /** Ask for the figures again after statsError. */
    retry() { wantStats = true; L.now(); },
    get busy() { return L.busy; },
    get on() { return L.on; },
  };
}
