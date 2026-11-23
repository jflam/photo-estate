// story-flow.js: telling a story about photos, typed or spoken, and reading the stories told (stories.py,
// multiplayer phase 7). One component, used by the inbox (answering a question) and by the photo app's viewer
// ("Tell a story about this photo"). Its machine is strict and its states own their data (fsm.js):
//
//   idle ──► asking ──► recording ──► recorded ──► sending ──► sent ──► idle
//     │        ├──► refused ──► asking | idle       │  ▲          └──► failed ──► sending | asking | idle
//     │        └──► idle (cancelled)                │  └ redo: recorded ──► asking
//     └──► sending (typed)            recording ──► idle (cancelled) | failed (the recorder broke)
//
//   idle       owns nothing: the text being typed is the page's field, not the machine's
//   asking     the microphone has been asked for (only ever when record is pressed, never on load)
//   refused    owns { error }: no microphone, or it was not allowed. The person can type instead
//   recording  owns { started, rec }: when it started (the clock), and the recorder (rec.stop, rec.cancel)
//   recorded   owns { blob, type, seconds, url }: the recording, to listen to (url) and redo before sending
//   sending    owns { what }: { text } or the recorded clip, sent once
//   sent       owns { story }: the server's story (as GET /api/photos/<sha>/stories lists one)
//   failed     owns { what, error }: what could not be sent (or recorded), to send again
//
//   teller({ microphone, recorder, send, ... })   -> { page, record(), stop(), cancel(), redo(), discard(), submit(text), retry(), again(), seconds() }
//   pickType(isTypeSupported)                     -> the recording type to ask MediaRecorder for, "" for its own
//   browserMicrophone(), browserRecorder()        the browser's (navigator.mediaDevices, MediaRecorder), injectable
//   refusedSays(error)                            why recording is not possible, in words
//   tellHtml(page, { seconds, dismiss, placeholder })   the component's inside, in its state (a string of HTML)
//   storyHtml(story), toldBy(story), transcriptSays(story), waiting(stories)    one story, and its words
//   follow(read, options)                         ask a story again until its transcript is no longer waiting
//   mountTeller(el, options)                      a teller drawn in an element and kept drawn (the pages use this)
//   photoStories({ read })                        the viewer's: the stories of the photo showing, and telling one
//   STORIES_STEPS
//   TELL_STEPS, PREFERRED, ACCEPTED, MAX_SECONDS
//
// The recording: the browser's own MediaRecorder, asked for AAC in MP4 first (Safari, and Chrome since it can:
// an .m4a the iPhone app plays too), then MP4 of any codec, then WebM or Ogg Opus (Firefox); every one a type the
// server takes (stories.AUDIO). It is sent as the whole body of the request, with its type (api.js posts a Blob
// as it is). The server keeps it and writes it out later on the mini (transcribe.py): the story then says
// "being written down" until it has been, and the page asks again now and then (follow) rather than waiting on
// an open connection.

import { flow, latest, poll, STALE } from "./fsm.js";
import { esc } from "./dom.js";
import { duration } from "./format.js";

/** @typedef {"idle" | "asking" | "refused" | "recording" | "recorded" | "sending" | "sent" | "failed"} TellState */
/**
 * @typedef {object} Story   a story as GET /api/photos/<sha>/stories lists one
 * @property {number} id
 * @property {{ id: number, name: string }} told_by
 * @property {string} told_at
 * @property {string | null} text
 * @property {{ id: number, text: string } | null} question
 * @property {string[]} photos
 * @property {{ url: string, type: string } | null} recording
 * @property {string | null} transcript
 * @property {"waiting" | "done" | "failed" | null} transcript_state
 * @property {string | null} [transcript_error]
 */
/** @typedef {{ blob: Blob, type: string, seconds: number, url: string }} Clip */
/** @typedef {{ text: string } | Clip} What   what is sent: typed text, or a recorded clip */
/** @typedef {{ type: string, stop: () => Promise<Blob>, cancel: () => void }} Rec   a recording under way */

export const TELL_STEPS = /** @type {Record<TellState, readonly TellState[]>} */ ({
  idle: ["asking", "sending"],
  asking: ["recording", "refused", "idle"],
  refused: ["asking", "idle"],
  recording: ["recorded", "failed", "idle"],
  recorded: ["asking", "sending", "idle"],
  sending: ["sent", "failed"],
  sent: ["idle"],
  failed: ["sending", "asking", "idle"],
});

/** The longest recording, in seconds: it stops by itself then (well inside the server's 8 MiB). */
export const MAX_SECONDS = 15 * 60;

/** What MediaRecorder is asked for, best first. */
export const PREFERRED = ["audio/mp4;codecs=mp4a.40.2", "audio/mp4", "audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus"];

/** The types the server takes (stories.AUDIO), without their parameters. */
export const ACCEPTED = ["audio/mp4", "audio/x-m4a", "audio/m4a", "audio/aac", "audio/mpeg", "audio/wav", "audio/x-wav",
  "audio/wave", "audio/webm", "audio/ogg"];

/** @param {string} t */
const base = t => (t || "").split(";")[0].trim().toLowerCase();

/**
 * The type to ask MediaRecorder for: the first of PREFERRED the browser can make, or "" (none of them: the browser
 * picks, and the server says whether it takes it).
 * @param {(type: string) => boolean} can   MediaRecorder.isTypeSupported
 * @returns {string}
 */
export function pickType(can) {
  return PREFERRED.find(t => { try { return can(t); } catch { return false; } }) || "";
}

/**
 * Why recording is not possible here, in words a person can act on.
 * @param {unknown} error
 * @returns {string}
 */
export function refusedSays(error) {
  const name = /** @type {{ name?: string }} */ (error || {}).name;
  if (name === "NotAllowedError" || name === "SecurityError") return "The microphone isn’t allowed for this page. Allow it in the browser’s settings, or type instead.";
  if (name === "NotFoundError" || name === "OverconstrainedError") return "No microphone was found. Type instead.";
  if (name === "NotReadableError") return "The microphone is in use by something else. Try again, or type instead.";
  return `This browser can’t record here${error instanceof Error && error.message ? ` (${error.message})` : ""}. Type instead.`;
}

/**
 * The browser's microphone: a function that asks for it (the browser shows its own prompt, the first time).
 * @param {MediaDevices | undefined} [md]
 * @returns {() => Promise<MediaStream>}
 */
export function browserMicrophone(md = globalThis.navigator?.mediaDevices) {
  return async () => {
    if (!md?.getUserMedia) throw Object.assign(new Error("no microphone here"), { name: "NotFoundError" });
    return md.getUserMedia({ audio: true });
  };
}

/**
 * The browser's recorder: start recording a stream. stop() resolves to the recording, one Blob of its type;
 * cancel() throws it away. Either lets the microphone go.
 * @param {any} [MR]  MediaRecorder
 * @returns {(stream: MediaStream) => Rec}
 */
export function browserRecorder(MR = globalThis.MediaRecorder) {
  return stream => {
    if (!MR) throw new Error("this browser has no MediaRecorder");
    const asked = pickType(t => MR.isTypeSupported(t));
    const r = new MR(stream, asked ? { mimeType: asked } : undefined);
    /** @type {Blob[]} */ const chunks = [];
    const release = () => { for (const t of stream.getTracks?.() || []) t.stop(); };
    r.ondataavailable = (/** @type {{ data: Blob }} */ e) => { if (e.data && e.data.size) chunks.push(e.data); };
    r.start();
    const type = () => base(r.mimeType || asked) || "audio/webm";
    return {
      get type() { return type(); },
      stop: () => new Promise((resolve, reject) => {
        r.onstop = () => { release(); resolve(new Blob(chunks, { type: type() })); };
        r.onerror = (/** @type {any} */ e) => { release(); reject(e?.error || new Error("the recording stopped")); };
        r.stop();
      }),
      cancel() { r.onstop = null; try { r.stop(); } catch { /* already stopped */ } release(); },
    };
  };
}

/**
 * @typedef {object} TellerOptions
 * @property {() => Promise<MediaStream>} microphone        browserMicrophone()
 * @property {(stream: MediaStream) => Rec} recorder          browserRecorder()
 * @property {(what: What) => Promise<Story>} send           POST the text (JSON) or the clip (its Blob)
 * @property {() => number} [now]                            ms (Date.now)
 * @property {(fn: () => void, ms: number) => any} [setTimer]
 * @property {(id: any) => void} [clearTimer]
 * @property {{ make: (b: Blob) => string, revoke: (url: string) => void }} [urls]   URL.createObjectURL and its revoke
 * @property {number} [maxSeconds]
 * @property {(to: TellState, from: TellState) => void} [onChange]
 */

/**
 * One story being told. Every method may be called in any state: one that does not apply there does nothing
 * and returns false (a second press, a late answer), so the page never has to ask first.
 * @param {TellerOptions} options
 */
export function teller({ microphone, recorder, send, now = Date.now, setTimer = setTimeout, clearTimer = clearTimeout,
  urls = { make: b => URL.createObjectURL(b), revoke: u => URL.revokeObjectURL(u) }, maxSeconds = MAX_SECONDS, onChange }) {
  /** @type {any} */ let limit = null;
  const page = flow("teller", TELL_STEPS, /** @type {TellState} */ ("idle"), (to, from, data, old) => {
    if (from === "recording") { clearTimer(limit); limit = null; }
    const gone = old.url || old.what?.url;                      // a clip let go: its address too
    if (gone && gone !== data.url && gone !== data.what?.url) urls.revoke(gone);
    onChange?.(to, from);
  });
  const t = {
    page,
    /** Seconds recorded so far (recording), or the clip's length (recorded, sending). */
    seconds() {
      if (page.is("recording")) return Math.max(0, (now() - page.data.started) / 1000);
      return page.data.seconds ?? page.data.what?.seconds ?? 0;
    },
    /** Ask for the microphone, and record. Resolves to how it ended up: "recording", "refused", or false. */
    async record() {
      if (!page.can("asking")) return false;
      const mine = page.to("asking");
      /** @type {MediaStream} */ let stream;
      try { stream = await microphone(); } catch (error) {
        if (page.data === mine) page.to("refused", { error });
        return page.data.error === error ? "refused" : false;
      }
      if (page.data !== mine) { for (const tr of stream.getTracks?.() || []) tr.stop(); return false; }   // cancelled meanwhile
      /** @type {Rec} */ let rec;
      try { rec = recorder(stream); } catch (error) { page.to("refused", { error }); return "refused"; }
      const started = now();
      const rd = page.to("recording", { started, rec });
      limit = setTimer(() => { if (page.data === rd) t.stop(); }, maxSeconds * 1000);
      return "recording";
    },
    /** Stop recording: the clip, to listen to. Resolves true once it is recorded. */
    async stop() {
      if (!page.is("recording")) return false;
      const rd = page.data, seconds = (now() - rd.started) / 1000;
      try {
        const blob = await rd.rec.stop();
        if (page.data !== rd) return false;
        page.to("recorded", { blob, type: base(blob.type) || rd.rec.type, seconds, url: urls.make(blob) });
        return true;
      } catch (error) {
        if (page.data === rd) page.to("failed", { what: null, error });
        return false;
      }
    },
    /** Stop asking or recording, and keep nothing. */
    cancel() {
      if (page.is("recording")) { page.data.rec.cancel(); page.to("idle"); return true; }
      if (page.is("asking", "refused")) { page.to("idle"); return true; }
      return false;
    },
    /** Throw the clip away and record again. */
    redo() { return page.is("recorded", "failed") ? t.record() : Promise.resolve(false); },
    /** Throw the clip (or the failure) away. */
    discard() { if (!page.is("recorded", "failed", "sent")) return false; page.to("idle"); return true; },
    /**
     * Send: the clip when there is one (recorded), else `text` (idle). Resolves to the story, or null (nothing
     * to send, or it failed: then failed owns it, to send again).
     * @param {string} [text]
     * @returns {Promise<Story | null>}
     */
    async submit(text = "") {
      /** @type {What | null} */
      const what = page.is("recorded") ? /** @type {Clip} */ (page.data) : page.is("idle") && text.trim() ? { text: text.trim() } : null;
      return what ? go(what) : null;
    },
    /**
     * Send again what failed: the same clip, or the text as it is in the field now (`text`, when given).
     * @param {string} [text]
     */
    retry(text) {
      if (!page.is("failed") || !page.data.what) return Promise.resolve(null);
      const was = page.data.what;
      return go("text" in was && text != null && text.trim() ? { text: text.trim() } : was);
    },
    /** After a story is told: ready for another. */
    again() { if (!page.is("sent")) return false; page.to("idle"); return true; },
  };
  /** @param {What} what */
  async function go(what) {
    const mine = page.to("sending", { what });
    try {
      const story = await send(what);
      if (page.data === mine) page.to("sent", { story });
      return story;
    } catch (error) {
      if (page.data === mine) page.to("failed", { what, error });
      return null;
    }
  }
  return t;
}

// ── words, and the HTML the pages draw ─────────────────────────────────────────────────────────────

const day = (/** @type {string} */ when) => {
  const d = new Date(when);
  return isNaN(+d) ? "" : d.toLocaleDateString("en-US", { day: "numeric", month: "short", year: "numeric" });
};

/**
 * Who told it, and when: "Ben · Nov 23, 2026".
 * @param {Pick<Story, "told_by" | "told_at">} s
 */
export const toldBy = s => [s.told_by?.name || "Someone", day(s.told_at)].filter(Boolean).join(" · ");

/**
 * What a spoken story's transcript is now, in words: the transcript itself, or where it is. null for a typed one.
 * @param {Pick<Story, "recording" | "transcript" | "transcript_state">} s
 * @returns {{ text: string, final: boolean } | null}
 */
export function transcriptSays(s) {
  if (!s.recording) return null;
  if (s.transcript_state === "done") return { text: s.transcript || "(nothing could be heard in it)", final: true };
  if (s.transcript_state === "failed") return { text: "Not written down yet: it will be tried again.", final: false };
  return { text: "Being written down…", final: false };
}

/**
 * Whether any of these stories has a recording still waiting to be written down.
 * @param {Pick<Story, "recording" | "transcript_state">[]} stories
 */
export const waiting = stories => stories.some(s => !!s.recording && s.transcript_state === "waiting");

/**
 * One story, as both pages show it: who and when, the question it answers, the words (typed, or the transcript
 * of the recording, or where that is), and the recording to play.
 * @param {Story} s
 * @param {{ question?: boolean }} [options]   question: say which question it answers (not under the question itself)
 * @returns {string}
 */
export function storyHtml(s, { question = true } = {}) {
  const tr = transcriptSays(s);
  return `<div class="story" data-story="${s.id}"><p class="told">${esc(toldBy(s))}${s.recording ? " · spoken" : ""}</p>`
    + (question && s.question ? `<p class="asked">Answering “${esc(s.question.text)}”</p>` : "")
    + (s.text ? `<p class="said">${esc(s.text)}</p>` : "")
    + (tr ? `<p class="said${tr.final ? "" : " pending"}"${s.transcript_error ? ` title="${esc(s.transcript_error)}"` : ""}>${esc(tr.text)}</p>` : "")
    + (s.recording ? `<audio controls preload="none" src="${esc(s.recording.url)}" aria-label="The recording"></audio>` : "")
    + `</div>`;
}

/**
 * The teller's inside, in the state it is in. The page puts it in an element (class "tell") and draws it again
 * on every step; the text field is left where it is while idle, so what is typed is never lost.
 * @param {{ state: TellState, data: Record<string, any> }} page
 * @param {{ seconds?: number, dismiss?: string, placeholder?: string, send?: string, text?: string }} [options]
 *   seconds: the clock; dismiss: a button that puts the question away ("I don't know"); send: the send button's
 *   word; text: what was typed so far (the field is drawn with it while idle)
 * @returns {string}
 */
export function tellHtml(page, { seconds = 0, dismiss = "", placeholder = "Type it, or record it", send = "Send", text: draft = "" } = {}) {
  const b = (/** @type {string} */ act, /** @type {string} */ label, pri = false, more = "") =>
    `<button type="button" class="btn${pri ? " pri" : ""}" data-tell="${act}"${more}>${esc(label)}</button>`;
  const no = dismiss ? b("dismiss", dismiss) : "";
  const clock = `<span class="clock num">${duration(seconds) || "0:00"}</span>`;
  const s = page.state, d = page.data;
  if (s === "idle" || (s === "failed" && d.what && "text" in d.what) || (s === "sending" && "text" in d.what)) {
    const text = s === "idle" ? draft : d.what.text;
    return `<textarea data-tell-text rows="3" placeholder="${esc(placeholder)}" aria-label="${esc(placeholder)}"${s === "sending" ? " disabled" : ""}>${esc(text)}</textarea>`
      + (s === "failed" ? `<p class="err" role="alert">Couldn’t send it: ${esc(d.error?.message || "")}</p>` : "")
      + `<div class="row">${b("record", "Record", false, s === "sending" ? " disabled" : "")}<span class="sp"></span>${no}`
      + (s === "failed" ? b("retry", "Try again", true) : b("send", s === "sending" ? "Sending…" : send, true, s === "sending" ? " disabled" : ""))
      + `</div>`;
  }
  if (s === "asking") return `<p class="state">Waiting for the microphone…</p><div class="row"><span class="sp"></span>${b("cancel", "Cancel")}</div>`;
  if (s === "refused") return `<p class="err" role="alert">${esc(refusedSays(d.error))}</p><div class="row">${b("record", "Try again")}<span class="sp"></span>${b("cancel", "Type instead", true)}</div>`;
  if (s === "recording") return `<p class="state rec"><i class="dot" aria-hidden="true"></i>Recording ${clock}</p><div class="row">${b("cancel", "Cancel")}<span class="sp"></span>${b("stop", "Stop", true)}</div>`;
  const clip = s === "recorded" ? d : d.what;
  if (clip && "blob" in clip) {
    const player = `<div class="clip"><audio controls src="${esc(clip.url)}" aria-label="Your recording"></audio><span class="clock num">${duration(clip.seconds) || "0:00"}</span></div>`;
    if (s === "recorded") return `${player}<div class="row">${b("redo", "Redo")}${b("discard", "Discard")}<span class="sp"></span>${no}${b("send", send, true)}</div>`;
    if (s === "sending") return `${player}<div class="row"><span class="sp"></span>${b("send", "Sending…", true, " disabled")}</div>`;
    if (s === "failed") return `${player}<p class="err" role="alert">Couldn’t send it: ${esc(d.error?.message || "")}</p><div class="row">${b("discard", "Discard")}<span class="sp"></span>${b("retry", "Try again", true)}</div>`;
  }
  if (s === "failed") return `<p class="err" role="alert">The recording stopped: ${esc(d.error?.message || "")}</p><div class="row">${b("discard", "Type instead")}<span class="sp"></span>${b("record", "Record again", true)}</div>`;
  return `<p class="state">Told. Thank you.</p>`;
}

/**
 * Ask for a story again, gently, until its transcript is written down (or failed): every 3 s at first, then less
 * often, for at most 10 minutes. Resolves to the story as it is then (null: it is not there any more). Cancel it
 * with `signal` (leaving the page, another photo).
 * @param {(signal: AbortSignal | null) => Promise<Story | null>} read
 * @param {{ signal?: AbortSignal | null, sleep?: (ms: number, signal?: AbortSignal | null) => Promise<void>, timeout?: number }} [options]
 * @returns {Promise<Story | null>}
 */
export function follow(read, { signal = null, sleep, timeout = 10 * 60 * 1000 } = {}) {
  return poll(read, { until: s => !s || s.transcript_state !== "waiting", every: n => Math.min(3000 * n, 20000),
    timeout, retries: 3, signal, ...(sleep ? { sleep } : {}) });
}

/**
 * @typedef {object} MountOptions
 * @property {(what: What) => Promise<Story>} send   POST the answer or the story (api.js: a Blob is sent as it is)
 * @property {(story: Story) => void} [onSent]
 * @property {() => void} [onDismiss]                the dismiss button ("I don't know") was pressed
 * @property {string} [dismiss]
 * @property {string} [placeholder]
 * @property {string} [sendLabel]
 * @property {Partial<TellerOptions>} [use]          another microphone or recorder (tests)
 */

/**
 * A teller in `el`, kept drawn: its buttons, the text field (what is typed is kept while recording, and after a
 * redraw), and the clock while recording. Nothing asks for the microphone until Record is pressed.
 * @param {HTMLElement} el
 * @param {MountOptions} o
 * @returns {{ teller: ReturnType<typeof teller>, unmount: () => void }}
 */
export function mountTeller(el, o) {
  /** @type {any} */ let tick = null;
  let draft = "";
  const field = () => /** @type {HTMLTextAreaElement | null} */ (el.querySelector("[data-tell-text]"));
  const draw = () => {
    el.innerHTML = tellHtml(t.page, { seconds: t.seconds(), dismiss: o.dismiss, placeholder: o.placeholder, send: o.sendLabel, text: draft });
  };
  const t = teller({
    microphone: browserMicrophone(), recorder: browserRecorder(), send: o.send, ...o.use,
    onChange(to) {
      clearInterval(tick); tick = null;
      if (to === "recording") {
        tick = setInterval(() => { const c = el.querySelector(".clock"); if (c) c.textContent = duration(t.seconds()) || "0:00"; }, 250);
      }
      if (to === "sent") draft = "";
      draw();
      if (to === "sent") o.onSent?.(t.page.data.story);
    },
  });
  el.classList.add("tell");
  draw();
  const typed = (/** @type {Event} */ e) => { if (/** @type {HTMLElement} */ (e.target).matches("[data-tell-text]")) draft = field()?.value || ""; };
  const pressed = (/** @type {MouseEvent} */ e) => {
    const b = /** @type {HTMLElement | null} */ (/** @type {HTMLElement} */ (e.target).closest("[data-tell]"));
    if (!b) return;
    const act = b.dataset.tell;
    if (act === "record") t.page.is("recorded", "failed") ? t.redo() : t.record();
    else if (act === "redo") t.redo();
    else if (act === "stop") t.stop();
    else if (act === "cancel") t.cancel();
    else if (act === "discard") t.discard();
    else if (act === "send") { if (!t.page.is("idle") || draft.trim()) t.submit(draft); else field()?.focus(); }
    else if (act === "retry") t.retry(field()?.value);
    else if (act === "dismiss") o.onDismiss?.();
  };
  const keys = (/** @type {KeyboardEvent} */ e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && /** @type {HTMLElement} */ (e.target).matches("[data-tell-text]")) {
      e.preventDefault();
      t.page.is("failed") ? t.retry(field()?.value) : t.submit(draft);
    }
  };
  el.addEventListener("input", typed);
  el.addEventListener("click", pressed);
  el.addEventListener("keydown", keys);
  return {
    teller: t,
    unmount() {
      clearInterval(tick);
      t.cancel();
      el.removeEventListener("input", typed); el.removeEventListener("click", pressed); el.removeEventListener("keydown", keys);
    },
  };
}

// ── the viewer's: the stories of the photo showing ─────────────────────────────────────────────────
//
//   idle ──► loading ──► known ──► known (a story told, the teller opened or closed, a transcript come)
//               └──────► failed      └──► loading (another photo) | idle (the viewer closed)
//   loading owns { sha }; known owns { sha, stories, telling }; failed owns { sha, error }

/** @typedef {"idle" | "loading" | "known" | "failed"} StoriesState */

export const STORIES_STEPS = /** @type {Record<StoriesState, readonly StoriesState[]>} */ ({
  idle: ["loading"],
  loading: ["loading", "known", "failed", "idle"],
  known: ["known", "loading", "idle"],
  failed: ["loading", "idle"],
});

/**
 * @param {{ read: (sha: string, signal: AbortSignal) => Promise<{ stories: Story[] }>,
 *   onChange?: (to: StoriesState, from: StoriesState) => void }} options   read: GET /api/photos/<sha>/stories
 */
export function photoStories({ read, onChange }) {
  const page = flow("stories", STORIES_STEPS, /** @type {StoriesState} */ ("idle"), (to, from) => onChange?.(to, from));
  const asking = latest();
  return {
    page,
    /** The photo whose stories these are, or null. @returns {string | null} */
    get sha() { return page.is("idle") ? null : page.data.sha; },
    /**
     * The stories of photo `sha` (nothing is asked again for the photo already known or on its way).
     * @param {string} sha
     * @returns {Promise<"known" | "failed" | "same" | typeof STALE>}
     */
    async look(sha) {
      if (!page.is("idle", "failed") && page.data.sha === sha) return "same";
      page.to("loading", { sha });
      try {
        const d = await asking.run(signal => read(sha, signal));
        if (d === STALE) return STALE;
        page.to("known", { sha, stories: d.stories, telling: false });
        return "known";
      } catch (error) {
        if (page.is("loading") && page.data.sha === sha) page.to("failed", { sha, error });
        return "failed";
      }
    },
    /** Open the teller, or close it. @param {boolean} on */
    tell(on) { if (!page.is("known") || page.data.telling === on) return false; page.to("known", { ...page.data, telling: on }); return true; },
    /**
     * A story told here (the teller closes), or a story as it is now (its transcript written down).
     * @param {Story} story
     */
    told(story) {
      if (!page.is("known") || !story.photos.includes(page.data.sha)) return false;
      const had = page.data.stories.some((/** @type {Story} */ x) => x.id === story.id);
      page.to("known", { ...page.data, telling: had ? page.data.telling : false,
        stories: had ? page.data.stories.map((/** @type {Story} */ x) => x.id === story.id ? story : x) : [...page.data.stories, story] });
      return true;
    },
    /** The viewer closed. */
    clear() { asking.cancel(); if (!page.is("idle")) page.to("idle"); },
  };
}
