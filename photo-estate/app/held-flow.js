// held-flow.js: the review of held photos (/held: changes.html in its held mode; private.py's review_list and
// review; multiplayer phase 5). The change-set review with the roles swapped: on a change set every photo is
// hidden unless it is marked to keep; here every photo stays private to its sharer unless it is marked to release.
// The list is every held photo waiting to be looked over, in the server's order (likeliest mistakes first, in
// sections). "Reviewed up to here" releases the marked photos up to the one the member is on and keeps the rest
// up to there private: both leave the review, and what comes after waits. Nothing is released that was not
// marked, and nothing is sent that the page did not list: the request names every photo it covers.
//
//   loading ──► reviewing ──► sending ──► reviewing (the photos it covered gone, or as it was if refused)
//      │  ▲         │ ▲  └──► loading (read again)        sending ──► sending (a mark made meanwhile)
//      ▼  │         └─┘ (a mark)
//    failed
//
//   loading    owns {}: GET /api/held is on its way
//   reviewing  owns { items, marks, waiting, prompts, member, sections }: items in the server's order; marks, a Set of
//              the shas marked to release (kept in this browser by `remember`, so a reload does not lose them)
//   sending    owns the same and { sent: { release, keep } }: POST /api/held/review is on its way
//   failed     owns { error }
//
//   heldReview({ load, send, remember, onChange })   -> { review, open(), isMarked(sha), mark(shas, on), upTo(sha | null),
//                                                        items, prompts, marked (how many), busy }
//   cover(items, marks, through)   what "Reviewed up to here" at `through` sends: { release, keep } (null: all)
//   whyHeld(item, prompts)         why a photo is held, briefly ("Looks like a receipt (0.112)")
//   heldSays(data, through)        the page's words: { sum, say, hint, fin }
//   SECTION_WORDS, HELD_REVIEW_STEPS

import { flow } from "./fsm.js";
import { fmt, plural } from "./format.js";

/** @typedef {"loading" | "reviewing" | "sending" | "failed"} ReviewState */
/**
 * @typedef {object} HeldItem   a row of GET /api/held
 * @property {string} sha
 * @property {string} section             named | close | documents | sure | hidden
 * @property {string[]} why               why it is held now: prompts at or over the line, is_screenshot, phone_screenshot
 * @property {number[] | null} scores     its score for each prompt (null: no vector)
 * @property {string} [when]
 * @property {number} [ar]
 * @property {string | null} [place]
 * @property {string[]} [people]
 * @property {boolean} [hidden]           the member hides it from their own library
 * @property {boolean} [missing]          not in the library being served
 */
/** @typedef {{ release: string[], keep: string[] }} Covered */

export const HELD_REVIEW_STEPS = /** @type {Record<ReviewState, readonly ReviewState[]>} */ ({
  loading: ["reviewing", "failed"],
  reviewing: ["reviewing", "sending", "loading"],
  sending: ["sending", "reviewing"],
  failed: ["loading"],
});

/** Each section's heading and what it says, in the server's order (private.SECTIONS). */
export const SECTION_WORDS = /** @type {Record<string, { title: string, note: string }>} */ ({
  named: { title: "Someone is named in them", note: "The likeliest mistakes: a photo with a person in it is seldom a receipt." },
  close: { title: "Just over the line", note: "They only just scored as a receipt, a screenshot or a document." },
  documents: { title: "Receipts and documents", note: "They scored clearly as a receipt, a screenshot or a document." },
  sure: { title: "Screenshots, and the surest", note: "Screenshots, and the photos that scored highest: very likely right." },
  hidden: { title: "Already hidden from you", note: "You hide these from your own library." },
});

const PROMPT_WORDS = /** @type {Record<string, string>} */ ({
  "a receipt": "a receipt", "a screenshot of a phone": "a phone screenshot", "a document": "a document",
});

/**
 * What "Reviewed up to here" sends: of the photos up to and including `through` (in the list's order), those
 * marked are released and the rest kept private. `through` null: the whole list ("Done"); a photo not listed:
 * nothing (never more than the member meant).
 * @param {HeldItem[]} items @param {Set<string>} marks @param {string | null} through
 * @returns {Covered}
 */
export function cover(items, marks, through) {
  const upto = through == null ? items : items.slice(0, items.findIndex(it => it.sha === through) + 1);
  /** @type {Covered} */ const out = { release: [], keep: [] };
  for (const it of upto) (marks.has(it.sha) ? out.release : out.keep).push(it.sha);
  return out;
}

/**
 * Why a photo is held, in a few words: each prompt it scored at or over the line, with its score, and a
 * screenshot's own reasons. A photo held under an older, lower line says its best score.
 * @param {HeldItem} it @param {string[]} prompts  the prompts, in the order of `scores`
 */
export function whyHeld(it, prompts) {
  const sc = it.scores || [];
  const parts = it.why.filter(w => w in PROMPT_WORDS)
    .map(w => { const s = sc[prompts.indexOf(w)]; return PROMPT_WORDS[w] + (s != null ? ` (${s.toFixed(3)})` : ""); });
  const shot = it.why.includes("phone_screenshot") ? "the phone marked it a screenshot"
    : it.why.includes("is_screenshot") ? "a screenshot by its size, with no camera" : "";
  const say = [parts.length ? "looks like " + parts.join(", ") : "", shot].filter(Boolean).join("; ");
  if (say) return say[0].toUpperCase() + say.slice(1);
  if (sc.length) return `Held under an older line: its best score is ${Math.max(...sc).toFixed(3)}`;
  return "Held as private";
}

/**
 * The page's words for the review as it stands: `sum` (the top bar), `say` (the dock: what U would do, at
 * `through`), `hint` (over the photos), `fin` (a note above them, or ""). HTML: the counts are numbers.
 * @param {{ items: HeldItem[], marks: Set<string>, waiting: number }} d @param {string | null} through
 */
export function heldSays(d, through) {
  const n = d.items.length, m = d.items.filter(it => d.marks.has(it.sha)).length, more = d.waiting - n;
  const since = more > 0 ? `<b>${plural(more, "more photo")} held since this page was read.</b> Read the list again to see ${more === 1 ? "it" : "them"}.` : "";
  if (!n) {
    return { sum: "", say: since ? "Nothing more of what was read" : "Nothing waits to be looked over", hint: "", fin: since };
  }
  const c = cover(d.items, d.marks, through), after = n - c.release.length - c.keep.length;
  const say = through == null ? `<b>${fmt(m)} to release</b> · ${fmt(n - m)} stay private`
    : `Up to here: <b>release ${fmt(c.release.length)}</b>, keep ${fmt(c.keep.length)} private` + (after ? ` · ${fmt(after)} after it` : "");
  return {
    sum: `${fmt(n)} to look over · <b>${fmt(m)}</b> to release`,
    say,
    hint: "Everything here stays private to you, except what you mark to release.",
    fin: since,
  };
}

/**
 * The review.
 * @param {{ load: () => Promise<any>, send: (c: Covered) => Promise<{ released: number, kept: number, skipped: number, waiting: number }>,
 *   remember?: { get: (member: any) => string[], set: (member: any, shas: string[]) => void },
 *   onChange?: (to: ReviewState, from: ReviewState) => void }} options
 *   `load`: GET /api/held; `send`: POST /api/held/review; `remember`: the marks, kept in this browser per member
 */
export function heldReview({ load, send, remember, onChange }) {
  const review = flow("held review", HELD_REVIEW_STEPS, /** @type {ReviewState} */ ("loading"), (to, from) => onChange?.(to, from));
  const keepMarks = (/** @type {any} */ d) => remember?.set(d.member, [...d.marks]);
  let loads = 0;
  /** @param {any} d @param {Set<string>} marks */
  const withMarks = (d, marks) => ({ ...d, marks });
  return {
    review,
    /** Read the list (again): the marks made are kept for the photos still in it. Resolves when read. */
    async open() {
      if (review.is("sending")) return { ok: false, error: new Error("Wait for the last review to be saved.") };
      const was = review.is("reviewing") ? review.data.marks : null;
      if (!review.is("loading")) review.to("loading");
      const mine = ++loads;
      try {
        const got = await load();
        if (mine !== loads) return { ok: false, error: new Error("read again since") };
        const listed = new Set(got.items.map((/** @type {HeldItem} */ it) => it.sha));
        const kept = [...(was || remember?.get(got.member) || [])].filter(s => listed.has(s));
        const d = review.to("reviewing", { items: got.items, marks: new Set(kept), waiting: got.waiting, prompts: got.prompts || [],
          member: got.member, sections: got.sections || [] });
        keepMarks(d);
        return { ok: true };
      } catch (error) {
        if (mine === loads) review.to("failed", { error });
        return { ok: false, error };
      }
    },
    get items() { return review.is("reviewing", "sending") ? /** @type {HeldItem[]} */ (review.data.items) : []; },
    get prompts() { return review.is("reviewing", "sending") ? /** @type {string[]} */ (review.data.prompts) : []; },
    /** How many listed photos are marked to release. */
    get marked() {
      if (!review.is("reviewing", "sending")) return 0;
      const { items, marks } = review.data;
      return /** @type {HeldItem[]} */ (items).filter(it => marks.has(it.sha)).length;
    },
    get busy() { return review.is("sending"); },
    /** Whether this photo is marked to release. @param {string} sha */
    isMarked(sha) { return review.is("reviewing", "sending") && review.data.marks.has(sha); },
    /** Mark these photos to release (on), or take the mark off. Kept in this browser. @param {string[]} shas @param {boolean} on */
    mark(shas, on) {
      if (!review.is("reviewing", "sending")) return false;
      const marks = new Set(review.data.marks);
      for (const s of shas) on ? marks.add(s) : marks.delete(s);
      keepMarks(review.to(review.state, withMarks(review.data, marks)));
      return true;
    },
    /**
     * "Reviewed up to here": release what is marked up to `through`, keep the rest up to it private. Never
     * rejects: { ok: true, value } (the server's answer; the photos it covered are gone from the list) or
     * { ok: false, error } (as it was).
     * @param {string | null} through  a listed photo, or null for the whole list ("Done")
     */
    async upTo(through) {
      if (!review.is("reviewing")) return { ok: false, error: new Error(review.is("sending") ? "Wait for the last review to be saved." : "Nothing is listed.") };
      const before = review.data, sent = cover(before.items, before.marks, through);
      if (!sent.release.length && !sent.keep.length) return { ok: false, error: new Error("Nothing to look over.") };
      const mine = review.to("sending", { ...before, sent });
      try {
        const value = await send(sent);
        if (review.data !== mine && !review.is("sending")) return { ok: true, value };
        const gone = new Set([...sent.release, ...sent.keep]), now = review.data;
        const marks = new Set([...now.marks].filter(s => !gone.has(s)));
        keepMarks(review.to("reviewing", { ...now, sent: undefined, items: now.items.filter((/** @type {HeldItem} */ it) => !gone.has(it.sha)),
          marks, waiting: value?.waiting ?? now.waiting - gone.size }));
        return { ok: true, value };
      } catch (error) {
        if (review.is("sending")) review.to("reviewing", { ...review.data, sent: undefined });   // as it was, with any mark made meanwhile
        return { ok: false, error };
      }
    },
  };
}
