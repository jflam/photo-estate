// video-flow.js: a video, or a Live Photo's clip, in the viewer; and its stream let go whenever it is left.
// The element is a model of a <video>: what it was told, in order.
import test from "node:test";
import assert from "node:assert/strict";

import { videoFlow, videoSrc, release, VIDEO_STEPS } from "../video-flow.js";
import { TransitionError } from "../fsm.js";
import { settled } from "./helpers.js";

/** A model of a <video>: its attributes, whether it is paused or muted, and a log of what it was told. */
function player({ refuse = "" } = {}) {
  /** @type {Record<string, string>} */ const attrs = {};
  /** @type {string[]} */ const log = [];
  const el = {
    paused: true, muted: false, log, attrs,
    play() {
      log.push(`play${el.muted ? " muted" : ""}`);
      if (refuse && !el.muted) return Promise.reject(Object.assign(new Error("no"), { name: refuse }));
      el.paused = false; return Promise.resolve();
    },
    pause() { log.push("pause"); el.paused = true; },
    load() { log.push("load"); },
    /** @param {string} k @param {string} v */ setAttribute(k, v) { attrs[k] = v; log.push(`${k}=${v}`); },
    /** @param {string} k */ getAttribute(k) { return k in attrs ? attrs[k] : null; },
    /** @param {string} k */ removeAttribute(k) { delete attrs[k]; log.push(`no ${k}`); },
  };
  return el;
}
const VIDEO = { sha: "v1", kind: "video" }, FOUND = { sha: "v2", kind: "video", at: 10 };

test("video: where its stream is, and the moment a search found it at", () => {
  assert.equal(videoSrc(VIDEO), "/video/v1");
  assert.equal(videoSrc(FOUND), "/video/v2#t=10");
  assert.equal(videoSrc({ sha: "v3", at: 4.123456 }), "/video/v3#t=4.12");
  assert.equal(videoSrc({ sha: "v4", at: 0 }), "/video/v4", "from the start is no fragment");
  assert.equal(videoSrc({ sha: "v5", at: null }), "/video/v5");
});

test("video: release pauses, takes the src away and loads, so the connection closes", () => {
  const el = player();
  el.setAttribute("src", "/video/v1"); el.log.length = 0;
  release(el);
  assert.deepEqual(el.log, ["pause", "no src", "load"]);
  assert.equal(el.getAttribute("src"), null);
});

test("video: shown, it plays from where it was found; left, its stream is let go", async () => {
  const v = videoFlow(), el = player();
  assert.equal(v.state, "still");
  assert.equal(v.sha, null);
  v.play(el, FOUND);
  assert.deepEqual([v.state, v.sha, el.getAttribute("src")], ["video", "v2", "/video/v2#t=10"]);
  await settled();
  assert.equal(el.paused, false, "it starts playing (it was opened by a tap or a key)");
  v.still();
  assert.deepEqual([v.state, v.sha], ["still", null]);
  assert.deepEqual(el.log.slice(-3), ["pause", "no src", "load"]);
  v.still();                                          // already still: nothing to do, and no throw
  assert.equal(v.state, "still");
});

test("video: the next video stops the one before first; never two streams at once", async () => {
  const v = videoFlow(), a = player(), b = player();
  v.play(a, VIDEO);
  v.play(b, FOUND);
  assert.deepEqual([v.state, v.sha], ["video", "v2"]);
  assert.equal(a.getAttribute("src"), null, "the first let its stream go");
  assert.deepEqual(a.log.slice(-3), ["pause", "no src", "load"]);
  v.play(b, VIDEO);                                   // the same element, another video
  assert.equal(b.getAttribute("src"), "/video/v1");
  await settled();
});

test("video: a browser that will not play it with sound plays it muted", async () => {
  const v = videoFlow(), el = player({ refuse: "NotAllowedError" });
  v.play(el, VIDEO);
  await settled();
  assert.deepEqual(el.log.filter(x => x.startsWith("play")), ["play", "play muted"]);
  assert.equal(el.paused, false);
  const other = player({ refuse: "NotSupportedError" });
  v.play(other, VIDEO);
  await settled();
  assert.deepEqual(other.log.filter(x => x.startsWith("play")), ["play"], "any other refusal is for the error event to say");
});

test("video: one it cannot play is failed, its stream let go, until the photo is left", () => {
  const v = videoFlow(), el = player();
  v.play(el, VIDEO);
  assert.equal(v.failed(el), true);
  assert.deepEqual([v.state, v.sha], ["failed", "v1"]);
  assert.equal(el.getAttribute("src"), null);
  assert.equal(v.failed(el), false, "an error from an element already let go is not news");
  assert.equal(v.toggle(), false, "nothing to play or pause");
  v.still();
  assert.equal(v.state, "still");
  assert.equal(v.failed(el), false);
  assert.equal(v.state, "still");
});

test("video: an error from an element that is not the one playing is not this video's", () => {
  const v = videoFlow(), el = player(), stray = player();
  v.play(el, VIDEO);
  assert.equal(v.failed(stray), false);
  assert.equal(v.state, "video");
});

test("video: space plays and pauses the video showing, and nothing else", async () => {
  const v = videoFlow(), el = player();
  assert.equal(v.toggle(), false);
  v.play(el, VIDEO);
  await settled();
  assert.equal(v.toggle(), true);
  assert.equal(el.paused, true);
  assert.equal(v.toggle(), true);
  await settled();
  assert.equal(el.paused, false);
});

test("live: held, the clip plays muted over the still; let go or ended, it is the still again", async () => {
  const v = videoFlow(), el = player();
  assert.equal(el.getAttribute("src"), null, "nothing is loaded before the first press");
  assert.equal(v.live(el, "c1"), true);
  assert.deepEqual([v.state, v.sha, el.muted, el.getAttribute("src")], ["live", "c1", true, "/video/c1"]);
  assert.equal(v.live(el, "c1"), false, "a second press while it plays does nothing");
  await settled();
  assert.deepEqual(el.log.filter(x => x.startsWith("play")), ["play muted"]);
  assert.equal(v.toggle(), false, "space is not for a Live Photo");
  v.still();
  assert.deepEqual([v.state, el.getAttribute("src")], ["still", null]);

  v.live(el, "c1");
  v.ended(el);
  assert.equal(v.state, "still", "the clip came to its end");
  v.live(el, "c1");
  assert.equal(v.failed(el), false, "a clip that cannot play is the still again, with nothing to say");
  assert.equal(v.state, "still");
});

test("live: no clip over a video, and a video's end leaves it where it ended", () => {
  const v = videoFlow(), el = player(), clip = player();
  v.play(el, VIDEO);
  assert.equal(v.live(clip, "c1"), false);
  assert.equal(clip.getAttribute("src"), null);
  v.ended(el);
  assert.equal(v.state, "video");
});

test("video: a step not in the table throws, and changes nothing", () => {
  const v = videoFlow();
  assert.throws(() => v.flow.to("failed"), TransitionError);
  assert.equal(v.state, "still");
  assert.deepEqual(VIDEO_STEPS.still, ["video", "live"]);
});
