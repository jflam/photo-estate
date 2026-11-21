// viewer-flow.js: the photo viewer of the photo app (search.html) as a strict machine whose states own
// their data (fsm.js's flow()), and the one thing it has to stay in step with: the browser's history.
//
// An open photo is a history entry, so Back closes it. That makes the viewer's state and the state of
// its entry the same thing, and this is the one table for both:
//
//   closed ──► open ──► leaving ──► closed
//                └──────────────────►
//
//   closed    no viewer, and no entry of its own in history. Owns nothing: whatever the open viewer
//             knew (the photo, its stack, the file fetched to share) is gone with the step.
//   open      a photo is showing; its entry has been pushed. Owns
//             { list, items, i, pick, stack, sheetUp, share, note, settle }:
//               list     the grid it was opened from
//               items    that grid's photos as they were when it opened: the photos to step through.
//                        (The grid adds to them as it grows; a grid that is asked for another list
//                        starts a new array, and the open photo goes on being one of these.)
//               i        the photo showing, as a position in `items`
//               stack    the shots of the stack that photo is on top of (once fetched), and
//               pick     the one of them being looked at in its place
//               sheetUp  whether the details are up, on a phone (never to begin with)
//               share    the file fetched for this photo, waiting for a second tap
//               note     a line said under the caption ("Couldn't save the favourite")
//               settle   finishes a swipe that is still sliding into place
//             show(i) moves to another photo and drops what belonged to the one before
//             (pick, stack, share, note, settle).
//   leaving   put away on screen by the page (the close button, Escape, a swipe down); history.back()
//             has been asked for and the browser has not yet said the entry is gone (popstate). Owns
//             { waiting, timer }: the history steps the page asked for meanwhile, written when the
//             entry has gone (written now, one of them would be the entry taken back instead); and
//             the timer that stops waiting after `backWithin` ms, for a browser that never says so.
//
//   How the viewer closes, and what becomes of its entry:
//     close("back")    the page closed it: open ──► leaving, history.back(); then popped() or the
//                      timer: leaving ──► closed, and the waiting steps are written
//     close("gone")    Back was pressed (popped() said "close"): the entry already has gone
//     close("taken")   a search is about to take the entry's place (history.replaceState)
//
//   viewerFlow({ history, backWithin, setTimer, clearTimer })
//     -> { flow, open, state, is, openOn, show, neighbour, close, popped, nav }
//   VIEWER_STEPS, GESTURE_STEPS

import { flow } from "./fsm.js";

/** @typedef {"closed" | "open" | "leaving"} ViewerState */

export const VIEWER_STEPS = /** @type {Record<ViewerState, readonly ViewerState[]>} */ ({
  closed: ["open"],
  open: ["leaving", "closed"],
  leaving: ["closed"],
});

/** @typedef {"none" | "press" | "pan" | "swipe" | "down" | "up" | "pinch"} GestureState */

// A touch on the open photo. none; press (one finger down, not yet moved: a tap if it lifts); then what the
// movement turned out to be: pan (of a zoomed photo), swipe (to a neighbour), down (to close), up (for the
// details); or pinch (two fingers), which becomes a pan when one of them lifts. Each state owns that
// gesture's numbers (where it started, the zoom it started from): search.html's GS.
export const GESTURE_STEPS = /** @type {Record<GestureState, readonly GestureState[]>} */ ({
  none: ["press"],
  press: ["pan", "swipe", "down", "up", "pinch", "none"],
  pan: ["pinch", "none"],
  swipe: ["pinch", "none"],
  down: ["pinch", "none"],
  up: ["pinch", "none"],
  pinch: ["pan", "none"],
});

/**
 * What the open viewer owns.
 * @typedef {object} Open
 * @property {{ items: any[] } & Record<string, any>} list
 * @property {any[]} items
 * @property {number} i
 * @property {any} pick
 * @property {any[] | null} stack
 * @property {boolean} sheetUp
 * @property {any} share
 * @property {string} note
 * @property {(() => void) | null} settle
 */

/**
 * The page's history, as the viewer uses it.
 * @typedef {object} ViewerHistory
 * @property {() => void} push    add the open photo's entry (history.pushState)
 * @property {() => void} back    take one entry back (history.back): the browser answers later, with popstate
 * @property {(mode: string, entry: any, to: string) => void} write  a step of the page's own: push or replace
 */

/**
 * @typedef {object} ViewerOptions
 * @property {ViewerHistory} history
 * @property {number} [backWithin]   ms to wait for the browser to say the entry has gone (default 1000)
 * @property {(fn: () => void, ms: number) => any} [setTimer]   (tests)
 * @property {(id: any) => void} [clearTimer]
 */

/** @param {ViewerOptions} options */
export function viewerFlow({ history, backWithin = 1000, setTimer = (fn, ms) => setTimeout(fn, ms), clearTimer = id => clearTimeout(id) }) {
  const vw = flow("viewer", VIEWER_STEPS, /** @type {ViewerState} */ ("closed"), (_to, _from, _data, old) => old.stop?.());
  /** @returns {Open | null} */
  const open = () => vw.is("open") ? /** @type {Open} */ (vw.data) : null;

  /** The entry has gone: the viewer is closed, and the steps that waited for that are written. */
  function land() {
    const { waiting } = vw.data;
    vw.to("closed");
    for (const [mode, entry, to] of waiting) history.write(mode, entry, to);
  }

  return {
    flow: vw,
    get state() { return vw.state; },
    is: vw.is,
    /** What the open viewer owns, or null when it is not open. */
    get open() { return open(); },

    /**
     * Open on photo `i` of `list`. Null, and nothing done, unless the viewer is closed (it is open
     * already, or the last one is still leaving).
     * @param {Open["list"]} list @param {number} i
     * @returns {Open | null}
     */
    openOn(list, i) {
      if (!vw.is("closed")) return null;
      const o = /** @type {Open} */ (vw.to("open", { list, items: list.items, i, pick: null, stack: null, sheetUp: false, share: null, note: "", settle: null }));
      history.push();
      return o;
    },

    /**
     * Another photo of the list: what belonged to the one before is dropped. Null when not open.
     * @param {number} i
     * @returns {Open | null}
     */
    show(i) {
      const o = open();
      if (!o) return null;
      Object.assign(o, { i, pick: null, stack: null, share: null, note: "", settle: null });
      return o;
    },

    /** The position `d` photos on from the one showing, if the list has a photo there; else null. @param {number} d */
    neighbour(d) {
      const o = open();
      return o && o.items[o.i + d] ? o.i + d : null;
    },

    /**
     * Close the open viewer. Returns what it owned (the page puts the photo back in its tile from
     * that), or null when it was not open: then nothing is done.
     * @param {"back" | "gone" | "taken"} [entry]  what becomes of the viewer's history entry: see above
     * @returns {Open | null}
     */
    close(entry = "back") {
      const o = open();
      if (!o) return null;
      if (entry !== "back") { vw.to("closed"); return o; }
      const mine = vw.to("leaving", { waiting: [], timer: null, stop() { clearTimer(mine.timer); } });
      mine.timer = setTimer(() => { if (vw.data === mine) land(); }, backWithin);   // (a browser that never says so)
      history.back();
      return o;
    },

    /**
     * The browser says history moved (popstate), to an entry whose state is `state`. Says what that
     * means for the page:
     *   "left"     the viewer's own entry has been taken back: it is closed now, nothing more to do
     *   "close"    Back was pressed on an open photo: the page closes it, with close("gone")
     *   "over"     this is the entry left by a photo that is no longer open: it is being stepped
     *              over (history.back() has been asked for again)
     *   "restore"  an entry of the page's own: show what it holds
     * @param {any} state
     * @returns {"left" | "close" | "over" | "restore"}
     */
    popped(state) {
      if (vw.is("leaving")) { land(); return "left"; }
      if (vw.is("open")) return "close";
      if (state?.v) { history.back(); return "over"; }
      return "restore";
    },

    /**
     * A step in the page's history. While the viewer is leaving it waits for the viewer's entry to go.
     * @param {string} mode  "push" or "replace"
     * @param {any} entry @param {string} to
     */
    nav(mode, entry, to) {
      if (vw.is("leaving")) vw.data.waiting.push([mode, entry, to]); else history.write(mode, entry, to);
    },
  };
}
