// video-flow.js: what plays in the photo viewer of the photo app (search.html): a video, or a Live
// Photo's clip over its still. A strict machine whose states own their data (fsm.js's flow()).
//
//   still ──► video ──► failed ──► still
//     │         └────────────────► still
//     └──► live ─────────────────► still
//
//   still    a photo, or nothing, is showing: nothing plays and no stream is held. Owns nothing.
//   video    a video is showing in its <video>, which has its stream. Owns { sha, el }.
//   failed   the browser could not play that video (its `error` event): the page shows its poster, a
//            line saying so and a way to download it. Owns { sha }. Its stream has been let go.
//   live     a Live Photo's clip is playing, muted, over its still, for as long as its LIVE control is
//            held. Owns { sha, el }.
//
// Every step out of video or live lets the stream go: the element is paused, its src taken away and
// load() called, which is what makes a browser close the connection (pausing alone keeps it). So a
// photo left (the next one, the one before, the viewer closed) never goes on playing or downloading,
// whichever way it was left: the page only has to say still().
//
// Nothing here touches the page: the element is handed in, and only what a Player has is used, so
// all of it runs under `node --test` (app/tests/video-flow.test.js).
//
//   videoFlow() -> { flow, state, is, sha, play(el, row), live(el, sha), failed(el), ended(el), still(), toggle() }
//   VIDEO_STEPS, release(el), videoSrc(row)

import { flow } from "./fsm.js";

/** @typedef {"still" | "video" | "failed" | "live"} VideoState */

export const VIDEO_STEPS = /** @type {Record<VideoState, readonly VideoState[]>} */ ({
  still: ["video", "live"],
  video: ["failed", "still"],
  failed: ["still"],
  live: ["still"],
});

/**
 * What of a <video> element is used here (an HTMLVideoElement has all of it).
 * @typedef {object} Player
 * @property {boolean} paused
 * @property {boolean} muted
 * @property {() => Promise<void> | void} play
 * @property {() => void} pause
 * @property {() => void} load
 * @property {(name: string, value: string) => void} setAttribute
 * @property {(name: string) => string | null} getAttribute
 * @property {(name: string) => void} removeAttribute
 */

/**
 * A photo's row, as far as this needs it.
 * @typedef {object} Row
 * @property {string} sha
 * @property {string} [kind]     "video" on a video
 * @property {number | null} [at]  seconds: where in the video a typed search matched it best
 */

/**
 * Where a video's stream is: /video/<sha>, and, when a search found it at a moment in it, that moment as a
 * media fragment (#t=10), so the browser starts there without the page having to wait for it to load.
 * @param {Row} row
 * @returns {string}
 */
export function videoSrc(row) {
  const at = row.at != null && Number.isFinite(row.at) && row.at > 0 ? row.at : 0;
  return `/video/${row.sha}` + (at ? `#t=${+at.toFixed(2)}` : "");
}

/**
 * Let an element's stream go: paused, no src, and load(), so the browser closes the connection.
 * @param {Player} el
 */
export function release(el) {
  el.pause();
  el.removeAttribute("src");
  el.load();
}

/** Start it; a browser that will not play it with sound (no gesture it counts) plays it muted. @param {Player} el */
function start(el) {
  Promise.resolve().then(() => el.play()).catch(e => {
    if (e?.name !== "NotAllowedError" || el.muted || !el.getAttribute("src")) return;   // (an error event says the rest)
    el.muted = true;
    Promise.resolve().then(() => el.play()).catch(() => {});
  });
}

export function videoFlow() {
  const m = flow("video", VIDEO_STEPS, /** @type {VideoState} */ ("still"), (_to, _from, _data, old) => { if (old.el) release(old.el); });
  /** Whatever plays now stops, and lets its stream go. */
  const still = () => { if (!m.is("still")) m.to("still"); };
  /** Whether `el` is the element the state it is in plays in. @param {Player} el */
  const mine = el => m.is("video", "live") && m.data.el === el;

  return {
    flow: m,
    get state() { return m.state; },
    is: m.is,
    /** The video or clip the state is about, or null when still. @returns {string | null} */
    get sha() { return m.data.sha ?? null; },

    /**
     * Show the video `row` in `el`, playing, from where its search found it (`at`). Whatever played
     * before stops first, so two videos never hold a stream at once.
     * @param {Player} el @param {Row} row
     */
    play(el, row) {
      still();
      el.muted = false;
      el.setAttribute("src", videoSrc(row));
      m.to("video", { sha: row.sha, el });
      start(el);
    },

    /**
     * The LIVE control is pressed: the Live Photo's clip `sha` plays in `el`, muted, inline. Only from
     * still: a press on a video, or a second press while one plays, does nothing. Says whether it started.
     * @param {Player} el @param {string} sha
     * @returns {boolean}
     */
    live(el, sha) {
      if (!m.is("still")) return false;
      el.muted = true;
      el.setAttribute("src", `/video/${sha}`);
      m.to("live", { sha, el });
      start(el);
      return true;
    },

    /**
     * The element said it cannot play what it was given (its `error` event). A video that was showing
     * in it is failed, and its stream let go; a Live Photo's clip goes back to its still. An error from
     * an element that is not playing anything here (one already let go) is not news. Says whether the
     * page now has a failure to show.
     * @param {Player} el
     * @returns {boolean}
     */
    failed(el) {
      if (!mine(el)) return false;
      if (m.is("live")) { m.to("still"); return false; }
      m.to("failed", { sha: m.data.sha });
      return true;
    },

    /** The element came to its end. A Live Photo's clip goes back to its still; a video stays where it ended. @param {Player} el */
    ended(el) { if (mine(el) && m.is("live")) m.to("still"); },

    /** Nothing plays: the photo was left, or the LIVE control let go. */
    still,

    /** Space: a video showing plays if it was paused, and pauses if it was playing. Says whether there was one. */
    toggle() {
      if (!m.is("video")) return false;
      const el = m.data.el;
      if (el.paused) start(el); else el.pause();
      return true;
    },
  };
}
