// story-flow.js: telling a story, typed or spoken. The machine (every pair of states), the microphone asked for
// only when Record is pressed, refused, recording and its clock, listening and redoing before sending, sending
// the clip or the text, a failure and sending again, the recording's type, the browser's recorder (a fake
// MediaRecorder), the words and HTML of a story and of the component, and asking a transcript again until it
// is written down.
import test from "node:test";
import assert from "node:assert/strict";

import { flow } from "../fsm.js";
import {
  teller, pickType, refusedSays, browserMicrophone, browserRecorder, tellHtml, storyHtml, toldBy, transcriptSays,
  waiting, follow, photoStories, TELL_STEPS, STORIES_STEPS, PREFERRED, ACCEPTED,
} from "../story-flow.js";
import { deferred, everyPair, naps, settled, timers } from "./helpers.js";

const stream = () => { const stopped = []; return { stopped, getTracks: () => [{ stop: () => stopped.push(1) }] }; };
const story = (/** @type {Partial<import("../story-flow.js").Story>} */ more = {}) => ({
  id: 7, told_by: { id: 2, name: "Ben" }, told_at: "2026-11-23T10:00:00Z", text: null, question: null, photos: ["a"],
  recording: null, transcript: null, transcript_state: null, ...more,
});

/** A teller with a microphone, a recorder and a server that answer when told to. */
function rig({ mic = async () => /** @type {any} */ (stream()), fail = false } = {}) {
  let clock = 1000;
  const sent = /** @type {any[]} */ ([]), made = /** @type {string[]} */ ([]), revoked = /** @type {string[]} */ ([]);
  const tm = timers();
  const recs = /** @type {any[]} */ ([]);
  const seen = /** @type {string[]} */ ([]);
  const t = teller({
    microphone: mic,
    recorder: s => {
      const r = { s, cancelled: false, type: "audio/mp4", stop: async () => new Blob([new Uint8Array(8)], { type: "audio/mp4;codecs=mp4a.40.2" }), cancel() { r.cancelled = true; } };
      recs.push(r);
      return r;
    },
    send: async what => { sent.push(what); if (fail) throw new Error("the server did not answer"); return /** @type {any} */ (story({ text: "text" in what ? what.text : null })); },
    now: () => clock, setTimer: tm.setTimer, clearTimer: tm.clearTimer,
    urls: { make: () => { made.push(`blob:${made.length}`); return made[made.length - 1]; }, revoke: u => { revoked.push(u); } },
    maxSeconds: 60, onChange: to => seen.push(to),
  });
  return { t, sent, made, revoked, tm, recs, seen, tick: (/** @type {number} */ ms) => { clock += ms; } };
}

test("the teller's machine: every legal step is taken, every other pair throws TransitionError", () => {
  assert.deepEqual(everyPair(TELL_STEPS, (/** @type {any} */ s, /** @type {any} */ from) => flow("t", s, from), assert.throws),
    { legal: 19, refused: 45 });
});

test("typed: idle to sending to sent, and nothing is sent for an empty field", async () => {
  const r = rig();
  assert.equal(await r.t.submit("   "), null);
  assert.equal(r.t.page.state, "idle");
  const got = await r.t.submit("  We were at the lake.  ");
  assert.equal(got?.text, "We were at the lake.");
  assert.deepEqual(r.sent, [{ text: "We were at the lake." }]);
  assert.equal(r.t.page.state, "sent");
  assert.deepEqual(r.seen, ["sending", "sent"]);
  assert.equal(r.t.again(), true);
  assert.equal(r.t.page.state, "idle");
});

test("spoken: the microphone is asked for only on record; stop, listen, redo, send the clip", async () => {
  let asked = 0;
  const r = rig({ mic: async () => { asked++; return /** @type {any} */ (stream()); } });
  assert.equal(asked, 0, "never on load");
  assert.equal(await r.t.record(), "recording");
  assert.equal(asked, 1);
  assert.equal(r.t.page.state, "recording");
  assert.deepEqual(r.tm.waits, [60000], "it stops by itself at the longest a recording may be");
  r.tick(12400);
  assert.equal(Math.round(r.t.seconds()), 12);
  assert.equal(await r.t.stop(), true);
  assert.equal(r.t.page.state, "recorded");
  assert.deepEqual(r.tm.waits, [], "the limit is let go");
  assert.equal(r.t.page.data.type, "audio/mp4", "the type without its codecs");
  assert.equal(r.t.page.data.url, "blob:0", "something to listen to");
  assert.equal(Math.round(r.t.seconds()), 12);
  assert.equal(await r.t.redo(), "recording", "redo: thrown away, and recorded again");
  assert.deepEqual(r.revoked, ["blob:0"]);
  r.tick(3000);
  await r.t.stop();
  const s = await r.t.submit("typed text is not sent with a recording");
  assert.ok(s);
  assert.equal(r.sent.length, 1);
  assert.ok(r.sent[0].blob instanceof Blob && r.sent[0].seconds === 3 && !("text" in r.sent[0]), "the clip, alone");
  assert.equal(r.t.page.state, "sent");
  assert.deepEqual(r.revoked, ["blob:0", "blob:1"], "the clip's address goes once it is sent");
});

test("refused: no microphone, or not allowed; the person can type instead or try again", async () => {
  const no = Object.assign(new Error("denied"), { name: "NotAllowedError" });
  const r = rig({ mic: async () => { throw no; } });
  assert.equal(await r.t.record(), "refused");
  assert.equal(r.t.page.state, "refused");
  assert.equal(r.t.page.data.error, no);
  assert.match(refusedSays(no), /isn’t allowed/);
  assert.match(refusedSays({ name: "NotFoundError" }), /No microphone/);
  assert.match(refusedSays(new Error("odd")), /\(odd\)/);
  assert.equal(r.t.cancel(), true, "type instead");
  assert.equal(r.t.page.state, "idle");
  assert.equal(await r.t.record(), "refused", "tried again");
});

test("cancelled while asking: the microphone it then gets is let go, and nothing records", async () => {
  const got = deferred();
  const r = rig({ mic: () => got.promise });
  const going = r.t.record();
  assert.equal(r.t.page.state, "asking");
  assert.equal(await r.t.submit("no"), null, "nothing is sent while asking");
  r.t.cancel();
  const s = stream();
  got.resolve(s);
  assert.equal(await going, false);
  assert.equal(r.t.page.state, "idle");
  assert.equal(s.stopped.length, 1);
  assert.equal(r.recs.length, 0);
});

test("cancelled while recording: the recorder is told, nothing is kept", async () => {
  const r = rig();
  await r.t.record();
  assert.equal(r.t.cancel(), true);
  assert.equal(r.recs[0].cancelled, true);
  assert.equal(r.t.page.state, "idle");
  assert.deepEqual(r.tm.waits, []);
});

test("the recording stops by itself at the limit", async () => {
  const r = rig();
  await r.t.record();
  r.tick(60000);
  r.tm.fire();
  await settled();
  assert.equal(r.t.page.state, "recorded");
  assert.equal(r.t.seconds(), 60);
});

test("a send that fails keeps what it was, to send again; typed text as the field has it then", async () => {
  const r = rig({ fail: true });
  assert.equal(await r.t.submit("first try"), null);
  assert.equal(r.t.page.state, "failed");
  assert.deepEqual(r.t.page.data.what, { text: "first try" });
  assert.match(tellHtml(r.t.page), /Couldn’t send it: the server did not answer/);
  assert.match(tellHtml(r.t.page), />first try<\/textarea>/, "the field keeps what was typed");
  await r.t.retry("second try");
  assert.deepEqual(r.sent.map(x => x.text), ["first try", "second try"]);
  const r2 = rig({ fail: true });
  await r2.t.record(); await r2.t.stop(); await r2.t.submit();
  assert.equal(r2.t.page.state, "failed");
  await r2.t.retry("ignored for a clip");
  assert.ok(r2.sent[1].blob, "the same clip again");
  assert.equal(r2.t.discard(), true);
  assert.deepEqual(r2.revoked, ["blob:0"]);
});

test("a step that does not apply does nothing", async () => {
  const r = rig();
  assert.equal(await r.t.stop(), false);
  assert.equal(r.t.discard(), false);
  assert.equal(r.t.again(), false);
  assert.equal(r.t.cancel(), false);
  assert.equal(await r.t.retry(), null);
  assert.equal(r.t.page.state, "idle");
});

test("the type asked of MediaRecorder: AAC in MP4 first, then what the browser makes; every one the server takes", () => {
  assert.equal(pickType(() => true), "audio/mp4;codecs=mp4a.40.2", "Safari, and Chrome now");
  assert.equal(pickType(t => t.startsWith("audio/webm")), "audio/webm;codecs=opus", "Firefox, an older Chrome");
  assert.equal(pickType(t => t === "audio/ogg;codecs=opus"), "audio/ogg;codecs=opus");
  assert.equal(pickType(() => false), "", "none: the browser's own");
  assert.equal(pickType(() => { throw new Error("no"); }), "");
  for (const t of PREFERRED) assert.ok(ACCEPTED.includes(t.split(";")[0]), t);
});

test("the browser's recorder: chunks into one Blob of its type, the microphone let go; and the microphone itself", async () => {
  /** @type {any} */ let made = null;
  class FakeRecorder {
    /** @param {any} s @param {any} o */
    constructor(s, o) { this.mimeType = o?.mimeType || ""; this.started = false; made = this; /** @type {any} */ this.ondataavailable = null; /** @type {any} */ this.onstop = null; }
    static isTypeSupported(/** @type {string} */ t) { return t === "audio/webm;codecs=opus"; }
    start() { this.started = true; }
    stop() { this.ondataavailable?.({ data: new Blob(["ab"]) }); this.ondataavailable?.({ data: new Blob([]) }); this.ondataavailable?.({ data: new Blob(["cd"]) }); setTimeout(() => this.onstop?.(), 0); }
  }
  const s = stream();
  const rec = browserRecorder(FakeRecorder)(/** @type {any} */ (s));
  assert.equal(made.mimeType, "audio/webm;codecs=opus");
  assert.equal(made.started, true);
  assert.equal(rec.type, "audio/webm");
  const blob = await rec.stop();
  assert.equal(blob.type, "audio/webm");
  assert.equal(await blob.text(), "abcd");
  assert.equal(s.stopped.length, 1);
  assert.throws(() => browserRecorder(undefined)(/** @type {any} */ (s)), /no MediaRecorder/);
  await assert.rejects(browserMicrophone(undefined)(), (/** @type {any} */ e) => e.name === "NotFoundError");
  /** @type {any[]} */ const asked = [];
  await browserMicrophone(/** @type {any} */ ({ getUserMedia: async (/** @type {any} */ c) => { asked.push(c); return s; } }))();
  assert.deepEqual(asked, [{ audio: true }]);
});

test("a story's words: who and when, the transcript or where it is", () => {
  assert.equal(toldBy(story()), "Ben · Nov 23, 2026");
  assert.equal(transcriptSays(story({ text: "typed" })), null);
  const rec = { url: "/api/stories/7/recording", type: "audio/mp4" };
  assert.deepEqual(transcriptSays(story({ recording: rec, transcript_state: "waiting" })), { text: "Being written down…", final: false });
  assert.deepEqual(transcriptSays(story({ recording: rec, transcript_state: "done", transcript: "At the lake." })), { text: "At the lake.", final: true });
  assert.match(transcriptSays(story({ recording: rec, transcript_state: "failed" }))?.text || "", /tried again/);
  assert.equal(waiting([story(), story({ recording: rec, transcript_state: "waiting" })]), true);
  assert.equal(waiting([story({ recording: rec, transcript_state: "done" })]), false);
  const h = storyHtml(story({ text: "<b>Lake</b>", question: { id: 1, text: "Where?" } }));
  assert.match(h, /Ben · Nov 23, 2026/);
  assert.match(h, /&lt;b&gt;Lake&lt;\/b&gt;/, "text is text");
  assert.match(h, /Answering “Where\?”/);
  assert.doesNotMatch(storyHtml(story({ question: { id: 1, text: "Where?" } }), { question: false }), /Answering/);
  const s = storyHtml(story({ recording: rec, transcript_state: "waiting" }));
  assert.match(s, /<audio controls preload="none" src="\/api\/stories\/7\/recording"/);
  assert.match(s, /class="said pending">Being written down…/);
});

test("the component in each state", async () => {
  const r = rig();
  const idle = tellHtml(r.t.page, { dismiss: "I don’t know", text: "half typed" });
  assert.match(idle, /<textarea data-tell-text[^>]*>half typed<\/textarea>/);
  assert.match(idle, /data-tell="record"/);
  assert.match(idle, /data-tell="dismiss">I don’t know</);
  assert.match(idle, /data-tell="send">Send</);
  await r.t.record();
  assert.match(tellHtml(r.t.page, { seconds: 7 }), /Recording <span class="clock num">0:07<\/span>/);
  assert.match(tellHtml(r.t.page), /data-tell="stop">Stop</);
  await r.t.stop();
  const rec = tellHtml(r.t.page, { send: "Send answer" });
  assert.match(rec, /<audio controls src="blob:0"/);
  assert.match(rec, /data-tell="redo">Redo</);
  assert.match(rec, /data-tell="send">Send answer</);
  const gate = deferred();
  const r2 = teller({ microphone: async () => /** @type {any} */ (stream()), recorder: () => /** @type {any} */ ({}), send: () => gate.promise });
  r2.submit("x");
  assert.match(tellHtml(r2.page), /Sending…/);
  assert.match(tellHtml(r2.page), /disabled/);
});

test("follow: asks again, less and less often, until the transcript is written down", async () => {
  const n = naps();
  const answers = [story({ recording: { url: "", type: "" }, transcript_state: "waiting" }), story({ recording: { url: "", type: "" }, transcript_state: "waiting" }),
    story({ recording: { url: "", type: "" }, transcript_state: "done", transcript: "Hello." })];
  const got = await follow(async () => answers.shift() || null, { sleep: n.sleep });
  assert.equal(got?.transcript, "Hello.");
  assert.deepEqual(n.asked, [3000, 6000]);
  assert.equal(await follow(async () => null, { sleep: n.sleep }), null, "gone: nothing to wait for");
  const ctl = new AbortController();
  const going = follow(async () => { ctl.abort(); return story({ recording: { url: "", type: "" }, transcript_state: "waiting" }); }, { sleep: n.sleep, signal: ctl.signal });
  await assert.rejects(going, (/** @type {any} */ e) => e.name === "AbortError");
});

test("the viewer's stories machine: every legal step is taken, every other pair throws TransitionError", () => {
  assert.deepEqual(everyPair(STORIES_STEPS, (/** @type {any} */ s, /** @type {any} */ from) => flow("t", s, from), assert.throws),
    { legal: 10, refused: 6 });
});

test("the viewer's stories: asked once a photo, an answer for another photo dropped, telling one, a transcript", async () => {
  /** @type {Record<string, ReturnType<typeof deferred>>} */ const asked = {};
  const v = photoStories({ read: sha => { asked[sha] = deferred(); return asked[sha].promise; } });
  const a = v.look("a");
  assert.equal(await v.look("a"), "same", "on its way already");
  const b = v.look("b");
  asked.a.resolve({ stories: [story()] });
  asked.b.resolve({ stories: [] });
  assert.equal(String(await a), "Symbol(stale)");
  assert.equal(await b, "known");
  assert.equal(v.sha, "b");
  assert.deepEqual(v.page.data.stories, []);
  assert.equal(v.tell(true), true);
  assert.equal(v.page.data.telling, true);
  assert.equal(v.told(/** @type {any} */ (story({ photos: ["c"] }))), false, "a story about another photo");
  const mine = /** @type {any} */ (story({ id: 9, photos: ["b"], recording: { url: "", type: "" }, transcript_state: "waiting" }));
  assert.equal(v.told(mine), true);
  assert.equal(v.page.data.telling, false, "told: the teller closes");
  assert.equal(v.told({ ...mine, transcript_state: "done", transcript: "Hi." }), true);
  assert.equal(v.page.data.stories.length, 1, "the same story, as it is now");
  assert.equal(v.page.data.stories[0].transcript, "Hi.");
  v.clear();
  assert.equal(v.page.state, "idle");
  assert.equal(v.sha, null);
  const f = photoStories({ read: async () => { throw new Error("no such photo"); } });
  assert.equal(await f.look("x"), "failed");
  assert.equal(f.page.data.error.message, "no such photo");
  assert.equal(f.tell(true), false);
});
