// grid-flow.js: one list of photos as the photo app asks for it (search.html's Grid): its first page,
// then more at either end as they come into sight. What it is asking for, and what it has, is the
// state of a strict machine (fsm.js's flow()), and everything that belongs to one list lives in that
// state: when another list is asked for, the old one's requests are aborted, its retry timers are
// cleared, and an answer of its that still arrives finds that it is no longer the list and is dropped.
//
//   empty ──► loading ──► ready ◄──┐          each end of a ready list (down, up):
//     ▲          │  └──► failed    │            idle ──► asking ──► idle
//     └──────────┴─────────┴───────┘ (and any ──► loading: another list)      └──► retrying ──► idle
//
//   empty     nothing asked for
//   loading   the first page is on its way.   owns { of, start, how, retry, ctl }
//   ready     showing; either end may grow.   owns { of, lo, next, page, down, up, ctl } and its timers
//   failed    the first page did not come.    owns { of, start, error, retry }
//
//   `of` is what the list is a list of (a month, a question, an album's id): fixed when the list is
//   asked for and handed to every fetch of it, so a page is never asked for with whatever the page
//   happens to be showing by the time it is wanted. `retry` is what "Try again" does.
//
//   gridFlow(name, options)   -> { phase, load, clear, more, less, poke, lo, next, page, of, end }
//   LIST_STEPS, END_STEPS
//
// Nothing here touches the page: the page is told what to draw through `view`.

import { flow, machine } from "./fsm.js";

/** @typedef {"empty" | "loading" | "ready" | "failed"} ListState */
/** @typedef {"idle" | "asking" | "retrying"} EndState */
/** @typedef {"down" | "up"} End */

export const LIST_STEPS = /** @type {Record<ListState, readonly ListState[]>} */ ({
  empty: ["loading", "empty"],
  loading: ["loading", "ready", "failed", "empty"],
  ready: ["loading", "empty"],
  failed: ["loading", "empty"],
});

export const END_STEPS = /** @type {Record<EndState, readonly EndState[]>} */ ({
  idle: ["asking"],
  asking: ["idle", "retrying"],
  retrying: ["idle"],
});

/**
 * A page of a list, as the server sends it.
 * @typedef {{ results: any[], next: number | null } & Record<string, any>} Page
 */

/**
 * @typedef {object} GridView
 * @property {(page: Page, list: { start: number, of: any, how: any }) => void} [first]  the first page came
 * @property {(results: any[]) => void} [below]             more photos, after those showing
 * @property {(results: any[], at: number) => void} [above] the photos just before those showing, from `at`
 */

/**
 * @typedef {object} GridOptions
 * @property {(at: number, n: number, signal: AbortSignal, of: any) => Promise<Page>} fetch  `n` photos from `at`
 * @property {number} [first]        photos in the first page (default 150)
 * @property {number} [step]         and in each one after (default 200)
 * @property {number} [retryAfter]   ms before an end whose page did not come may ask again (default 2000)
 * @property {() => boolean} [live]  whether the list is on screen at all (an end of a hidden list does not grow)
 * @property {(end: End, again?: boolean) => boolean} [near]  whether that end is in sight, or nearly
 *   (`again`: asked straight after a page came, to go on)
 * @property {GridView} [view]
 * @property {(fn: () => void, ms: number) => any} [setTimer]   (tests)
 * @property {(id: any) => void} [clearTimer]
 */

/**
 * @param {string} name   for the report of a refused step
 * @param {GridOptions} options
 */
export function gridFlow(name, options) {
  const { fetch, first = 150, step = 200, retryAfter = 2000, live = () => true, near = () => false, view = {},
    setTimer = (fn, ms) => setTimeout(fn, ms), clearTimer = id => clearTimeout(id) } = options;
  // Whatever a state had under way ends with the state.
  const phase = flow(name, LIST_STEPS, /** @type {ListState} */ ("empty"), (_to, _from, _data, old) => old.stop?.());

  /** A state's own AbortController, and the `stop` that ends what the state had under way. */
  const own = (/** @type {Record<string, any>} */ data) => {
    const ctl = new AbortController();
    return Object.assign(data, { ctl, stop() { ctl.abort(); clearTimer(data.timers?.down); clearTimer(data.timers?.up); } });
  };
  const ready = () => phase.is("ready") ? phase.data : null;

  /**
   * The list from `start`. Resolves to its first page; to null if another list was asked for (or this
   * one cleared) before it came; rejects, the list `failed`, if the page did not come.
   * @param {number} [start]
   * @param {{ of?: any, retry?: (() => unknown) | null, how?: any }} [what]
   * @returns {Promise<Page | null>}
   */
  async function load(start = 0, { of = null, retry = null, how = null } = {}) {
    const mine = phase.to("loading", own({ of, start, how, retry }));
    /** @type {Page} */ let page;
    try {
      page = await fetch(start, first, mine.ctl.signal, of);
    } catch (error) {
      if (phase.data !== mine) return null;
      phase.to("failed", { of, start, error, retry });
      throw error;
    }
    if (phase.data !== mine) return null;
    const strict = { strict: true, onRefuse: () => {} };
    phase.to("ready", own({ of, lo: start, next: page.next, page, timers: { down: null, up: null },
      down: machine(`${name} down`, END_STEPS, /** @type {EndState} */ ("idle"), strict),
      up: machine(`${name} up`, END_STEPS, /** @type {EndState} */ ("idle"), strict) }));
    view.first?.(page, { start, of, how });
    return page;
  }

  /** Nothing showing, and nothing more to come from the old list. */
  function clear() { phase.to("empty"); }

  /** That end's page did not come: it may ask again shortly, if this is still the list. @param {Record<string, any>} list @param {End} end */
  function retry(list, end) {
    list[end].to("retrying");
    list.timers[end] = setTimer(() => {
      list.timers[end] = null;
      if (phase.data !== list) return;               // another list since: nothing of this one is wanted
      list[end].to("idle");
      poke();
    }, retryAfter);
  }

  /** More photos after those showing, if there are any and that end is not already asking. */
  async function more() {
    const list = ready();
    if (!list || !list.down.is("idle") || list.next == null || !live()) return;
    list.down.to("asking");
    /** @type {Page} */ let page;
    try {
      page = await fetch(list.next, step, list.ctl.signal, list.of);
    } catch {
      if (phase.data === list) retry(list, "down");
      return;
    }
    if (phase.data !== list) return;
    list.next = page.next;
    list.down.to("idle");
    view.below?.(page.results);
    if (phase.data === list && list.next != null && near("down", true)) more();
  }

  /** The photos before those showing (a list that started part way down). */
  async function less() {
    const list = ready();
    if (!list || !list.up.is("idle") || list.lo <= 0 || !live()) return;
    list.up.to("asking");
    const n = Math.min(step, list.lo), at = list.lo - n;
    /** @type {Page} */ let page;
    try {
      page = await fetch(at, n, list.ctl.signal, list.of);
    } catch {
      if (phase.data === list) retry(list, "up");
      return;
    }
    if (phase.data !== list) return;
    const ok = page.results.length === n;            // a short page would leave a gap
    list.up.to("idle");
    if (!ok) return;
    list.lo = at;
    view.above?.(page.results, at);
    if (phase.data === list && list.lo > 0 && near("up", true)) less();
  }

  /** Fill in either end that is in sight. */
  function poke() {
    if (near("up")) less();
    if (near("down")) more();
  }

  return {
    phase, load, clear, more, less, poke, first, step,
    /** The position in the whole list of the first photo it has (or will have, while loading). */
    get lo() { return phase.is("ready") ? phase.data.lo : phase.is("loading") ? phase.data.start : 0; },
    /** Where the next page down starts; null at the end of the list, or with no list. */
    get next() { return phase.is("ready") ? phase.data.next : null; },
    /** The first page of the list showing (what the server said of the whole list), or null. */
    get page() { return phase.is("ready") ? /** @type {Page} */ (phase.data.page) : null; },
    /** What the list asked for or showing is a list of, or null. */
    get of() { return phase.is("empty") ? null : phase.data.of; },
    /** The state of one end of a ready list (idle with no list). @param {End} end @returns {EndState} */
    end(end) { return phase.is("ready") ? phase.data[end].state : "idle"; },
  };
}
