// people-flow.js: what people.html waits for.
//
//   unknownFaces(ask, options)    ask for the groups of unnamed faces until the server has them, or says
//                                 it failed, or it has taken too long
//   UNKNOWN                       how often it asks and for how long
//
// faces.py groups the faces nobody has named in a thread of its own when it starts, and
// /api/people/unknown says how far that is: "waiting" or "working" (ask again), "ready" (the groups), or
// "failed" with `error` (it will not be ready: the grouping raised, and only a restart runs it again).
// The page asks again while it is not ready, each time a little later (1.5 s, growing by half, to 10 s),
// and gives up after ten minutes with a way to ask again. Leaving the view cancels it (the signal).

import { until } from "./loop.js";

/** Ten minutes, as a time and as the asks that fit in it (whichever is reached first). */
export const UNKNOWN = { first: 1500, grow: 1.5, most: 10000, timeout: 600000, tries: 64, retries: 2 };

/**
 * @typedef {{ state: string, groups?: any[], error?: string }} UnknownAnswer
 * @typedef {{ state: "ready", groups: any[] } | { state: "failed", error: string } | { state: "timeout" }
 *   | { state: "error", error: unknown }} UnknownEnd
 */

/**
 * @param {(signal: AbortSignal | null) => Promise<UnknownAnswer>} ask   /api/people/unknown, once
 * @param {import("./fsm.js").PollOptions<UnknownAnswer> & { onWaiting?: (tries: number) => void }} [options]
 * @returns {Promise<UnknownEnd>}  rejects with an AbortError only when cancelled
 */
export async function unknownFaces(ask, options = {}) {
  const { onWaiting, ...rest } = options;
  const end = await until(ask, {
    until: (u, tries) => {
      const last = u.state === "ready" || u.state === "failed";
      if (!last) onWaiting?.(tries);
      return last;
    },
    every: tries => Math.min(UNKNOWN.first * UNKNOWN.grow ** (tries - 1), UNKNOWN.most),
    timeout: UNKNOWN.timeout,
    tries: UNKNOWN.tries,
    retries: UNKNOWN.retries,
    ...rest,
  });
  if (end.state === "timeout") return { state: "timeout" };
  if (end.state === "error") return end;
  const u = end.value;
  return u.state === "failed" ? { state: "failed", error: u.error || "unknown error" } : { state: "ready", groups: u.groups || [] };
}
