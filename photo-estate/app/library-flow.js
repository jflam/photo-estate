// library-flow.js: the Library tab of the photo app (search.html), apart from its lists of photos
// (grid-flow.js): where in it the person is, and what the page knows of the library as a whole.
// Both are strict machines whose states own their data (fsm.js's flow()).
//
// Where: the scale showing. Any scale can follow any other (the bar, a card, Back).
//
//   years | months | all | month      each owns { anchor, trail }: the card it was last entered or
//                                     left by (in `month`, the month showing), and the cards opened
//                                     to get here. A history entry holds exactly this.
//
// What is known: the months and years, how many photos in each, and every photo's shape, for one
// order and one set of view options (`q`, the query string they were asked with).
//
//   unknown ──► asking ──► known ──► asking ...
//                  └─────► failed ──► asking
//
//   unknown   never asked
//   asking    owns { q, had, ctl }: `had` is what was known before (it stays up meanwhile, and if
//             this asking fails it is what is known again), `ctl` aborts the two requests
//   known     owns { q, P, months, shapes }
//   failed    owns { error, retry }: asked, and nothing to show for it; `retry` is what "Try again" does
//
//   libraryFlow(options)   -> { place, known, set, load, covers, settled, facts, scale, anchor, trail }
//   PLACE_STEPS, KNOWN_STEPS, SCALES

import { flow, latest, STALE } from "./fsm.js";

/** @typedef {"years" | "months" | "all" | "month"} Scale */
/** @typedef {"unknown" | "asking" | "known" | "failed"} KnownState */
/** @typedef {{ key: string, n: number, at: number } & Record<string, any>} Month */
/** @typedef {{ months: Month[], years: any[], matching: number, total: number } & Record<string, any>} Periods */
/** @typedef {{ q: string, P: Periods, months: Map<string, number>, shapes: Uint8Array | null }} Facts */
/** @typedef {{ scale?: Scale, anchor?: string | null, trail?: any[] }} Place */

export const SCALES = /** @type {readonly Scale[]} */ (["years", "months", "all", "month"]);

export const PLACE_STEPS = /** @type {Record<Scale, readonly Scale[]>} */ ({
  years: SCALES, months: SCALES, all: SCALES, month: SCALES,
});

export const KNOWN_STEPS = /** @type {Record<KnownState, readonly KnownState[]>} */ ({
  unknown: ["asking"],
  asking: ["asking", "known", "failed"],
  known: ["asking"],
  failed: ["asking"],
});

/**
 * @typedef {object} LibraryOptions
 * @property {(q: string, signal: AbortSignal) => Promise<Periods>} periods   /api/periods
 * @property {(q: string, signal: AbortSignal) => Promise<ArrayBuffer | null>} layout  /api/layout: a byte a photo
 * @property {Scale} [scale]   the scale to start at (default all)
 * @property {(facts: Facts, ctx: any) => void} [onKnown]  told as soon as an asking is answered, before
 *   anyone waiting on settled(): the page draws what is now known. `ctx` is what load() was given.
 */

/** @param {LibraryOptions} options */
export function libraryFlow({ periods, layout, scale = "all", onKnown }) {
  const place = flow("place", PLACE_STEPS, scale);
  place.to(scale, { anchor: null, trail: [] });
  const known = flow("library", KNOWN_STEPS, /** @type {KnownState} */ ("unknown"), (_to, _from, _data, old) => old.stop?.());
  const covering = latest();
  /** @type {(() => void)[]} */ let waiting = [];
  const settle = () => { for (const done of waiting.splice(0)) done(); };

  /** What is known of the library: the answer showing, or the one before while asking again. @returns {Facts | null} */
  const facts = () => known.is("known") ? /** @type {Facts} */ (known.data) : known.is("asking") ? known.data.had : null;

  /**
   * Go to a place in the Library: a scale (the one showing if none is given), an anchor and a trail.
   * @param {Place} to
   */
  function set(to) {
    place.to(to.scale || place.state, { anchor: to.anchor ?? null, trail: to.trail || [] });
  }

  /**
   * Ask for the library again (another order, a view option, the first time). The asking before it,
   * if one is still under way, is aborted and its answer dropped. Never rejects. Resolves to how it
   * ended: "known"; "kept" (it failed, and what was known before still is); "failed"; or STALE.
   * @param {string} q
   * @param {{ retry?: () => unknown, ctx?: any }} [options]
   * @returns {Promise<"known" | "kept" | "failed" | typeof STALE>}
   */
  async function load(q, { retry, ctx } = {}) {
    covering.cancel();
    const ctl = new AbortController();
    const mine = known.to("asking", { q, had: facts(), ctl, stop: () => ctl.abort() });
    /** @type {unknown} */ let error = null;
    const [P, shapes] = await Promise.all([
      periods(q, ctl.signal).catch(e => { error = e; return null; }),
      layout(q, ctl.signal).catch(() => null)]);
    if (known.data !== mine) return STALE;           // asked again since: that answer is the one to show
    if (!P) {
      if (mine.had) { known.to("known", mine.had); settle(); return "kept"; }
      known.to("failed", { error, retry });
      settle();
      return "failed";
    }
    /** @type {Facts} */
    const now = { q, P, months: new Map(P.months.map(m => [m.key, m.n])),
      shapes: shapes && shapes.byteLength === P.matching ? new Uint8Array(shapes) : null };   // every photo's shape, in the library's order
    known.to("known", now);
    onKnown?.(now, ctx);
    settle();
    return "known";
  }

  /**
   * A favourite changed: the same months and years, perhaps with other covers. Taken only if the
   * library is still the one it was asked about, and has as many photos. Resolves to whether it was.
   * @returns {Promise<boolean>}
   */
  async function covers() {
    if (!known.is("known")) return false;
    const was = /** @type {Facts} */ (known.data);
    const P = await covering.run(signal => periods(was.q, signal)).catch(() => null);
    if (!P || P === STALE || known.data !== was || P.matching !== was.P.matching) return false;
    was.P = P;
    return true;
  }

  /** Done when the library is not being asked for: it is known, or it failed. @returns {Promise<void>} */
  const settled = () => known.is("known", "failed") ? Promise.resolve() : new Promise(done => { waiting.push(done); });

  return {
    place, known, set, load, covers, settled,
    get facts() { return facts(); },
    get scale() { return place.state; },
    /** @returns {string | null} */
    get anchor() { return place.data.anchor; },
    /** @returns {any[]} */
    get trail() { return place.data.trail; },
  };
}
