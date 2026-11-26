// private-flow.js: photos private to their sharer, on search.html (multiplayer phase 5: private.py; the words are
// docs/notes/MULTIPLAYER-PLAN.md's). A photo a member shared can be withdrawn from the family (private to them
// alone) and released again, now or by itself on a day they choose; one held at ingestion (it looked like a
// receipt, a screenshot or a document) is the same. The viewer says so in a line beside the "why" line, and its
// details offer to withdraw a photo the member shared; the held view (a search for `is:held`) releases those chosen
// in a batch. It is a search, so the view options (screenshots, stacks) and the member's hides leave photos out of it:
// its bar says how many it shows of how many are held, and leads to the review of every one (/held: held-flow.js).
// Nothing here releases a photo that was not tapped.
//
// The viewer's private line, for the photo showing (fed from the why answer: GET /api/photos/<sha>/why's `private`):
//
//   idle ──► known ──► saving ──► known (the server's answer, or as it was if the change was refused)
//   known ──► known (another photo)     known, saving ──► idle (the viewer closed)
//
//   known    owns { sha, p }: p is the why answer's `private` ({ sharer, mine: { why, since, until } | null,
//            others_sharing }); the line shows when mine is set, the details' row when sharer is
//   saving   owns { sha, p, body }: the change sent (POST /api/private {shas: [sha], private, until?})
//
// The held view (`is:held` in search), and choosing photos in it:
//
//   off ──► viewing ──► choosing ──► releasing ──► viewing (released), or choosing as it was (refused)
//            └──► off   choosing ──► viewing (cancelled)
//
//   viewing    owns { n, held?, waiting? }: how many photos the view lists; how many are held, and how many of
//              those wait to be looked over (GET /api/held?count=1), once known
//   choosing   owns { n, held?, waiting?, chosen }: a Set of the photos chosen (a tile tapped is chosen, not opened)
//   releasing  owns the same: what is being released; nothing else is done until the server answers
//
//   privateLine({ send, onChange })   -> { line, sha, look(sha, p), get says, get row, act(body), clear() }
//   heldView({ release, onChange })   -> { view, open(n), counted({ held, waiting }), close(), choose(), cancel(), toggle(sha),
//                                        chosen(sha), releaseChosen() }
//   lineSays(p, today?)               what the line says, and what it offers: { html, offers: [{ label, body | "day" }] } | null
//   rowSays(p)                        the details' row for a photo the member shared: { title, detail, label } | null
//   barSays(view)                     the held view's bar: { text, offers: [{ label, what, primary?, href? }] } | null
//   WHY_HELD, PRIVATE_STEPS, HELD_STEPS

import { flow } from "./fsm.js";
import { esc } from "./dom.js";
import { fmt, plural } from "./format.js";
import { day } from "./lens-flow.js";

/** @typedef {"idle" | "known" | "saving"} PrivateState */
/** @typedef {"off" | "viewing" | "choosing" | "releasing"} HeldState */
/**
 * @typedef {object} Private   the why answer's `private`
 * @property {boolean} sharer                  the member shared this photo (and so may withdraw it)
 * @property {{ why: "new" | "held" | "withdrawn", since: string | null, until: string | null } | null} mine
 * @property {number | null} others_sharing    sharers who do not hold it private (while any do, the family sees it)
 */
/** @typedef {{ private: boolean, until?: string | null }} Change   what POST /api/private is sent, but the photo */

export const PRIVATE_STEPS = /** @type {Record<PrivateState, readonly PrivateState[]>} */ ({
  idle: ["known"],
  known: ["known", "saving", "idle"],
  saving: ["known", "idle"],
});

export const HELD_STEPS = /** @type {Record<HeldState, readonly HeldState[]>} */ ({
  off: ["viewing"],
  viewing: ["viewing", "choosing", "off"],
  choosing: ["choosing", "viewing", "releasing", "off"],
  releasing: ["viewing", "choosing", "off"],
});

/** Why a photo is held, in the words the line and the inbox use. */
export const WHY_HELD = "it looks like a receipt, a screenshot or a document";

/**
 * What the viewer's private line says of a photo private to the member, and what it offers: release it now,
 * release it on a day (the page asks which: `body` "day"), or, with a day set, keep it private with no day.
 * Null when the photo is not private to them.
 * @param {Private | null | undefined} p
 * @param {Date} [today]
 * @returns {{ html: string, offers: { label: string, body: Change | "day" }[] } | null}
 */
export function lineSays(p, today = new Date()) {
  const m = p?.mine;
  if (!m) return null;
  const since = day(m.since || "", today);
  const why = m.why === "held" ? `held: ${WHY_HELD}` : m.why === "withdrawn" ? "you withdrew it" : "it is being checked";
  const until = m.until ? `. It is released by itself on ${esc(day(m.until, today))}` : "";
  const also = p?.others_sharing ? `. ${plural(p.others_sharing, "other member")} shared it too, so the family still sees it` : "";
  /** @type {{ label: string, body: Change | "day" }[]} */
  const offers = [{ label: "Release", body: { private: false } }];
  offers.push(m.until ? { label: "No release day", body: { private: true, until: null } } : { label: "Release on a day…", body: "day" });
  return { html: `<i>Private to you</i>${since ? ` since ${esc(since)}` : ""}: ${why}${until}${also}`, offers };
}

/**
 * The details' row for a photo the member shared and has not made private: withdraw it from the family.
 * @param {Private | null | undefined} p
 * @returns {{ title: string, detail: string, label: string } | null}
 */
export function rowSays(p) {
  if (!p?.sharer || p.mine) return null;
  return { title: "Shared by you", detail: "Withdraw it, and only you will see it. You can release it again at any time.",
    label: "Withdraw from the family" };
}

/**
 * The held view's bar: what it shows, honestly (a search: the view options and the member's hides leave photos out
 * of it), and what can be done: go to the review of every held photo, or choose photos here to release.
 * @param {{ state: HeldState, data: any }} view
 * @returns {{ text: string, offers: { label: string, what: "review" | "choose" | "chosen" | "cancel", primary?: boolean, href?: string }[] } | null}
 */
export function barSays(view) {
  if (view.state === "off") return null;
  const { n, held, waiting, chosen } = view.data;
  const toReview = /** @type {{ label: string, what: "review", href: string, primary?: boolean }} */ (
    { label: waiting ? `Look over ${waiting === 1 ? "it" : `all ${fmt(waiting)}`}` : "Review held photos", what: "review", href: "/held" });
  if (!n) {
    return { text: held ? `None of the ${plural(held, "photo")} held for you is in this search: your view options or what you hide leave ${held === 1 ? "it" : "them"} out.`
      : "Nothing is held: everything you shared, the family sees.", offers: held ? [{ ...toReview, primary: true }] : [] };
  }
  if (view.state === "viewing") {
    const shows = held != null && held > n ? `This search shows ${fmt(n)} of the ${plural(held, "photo")} held for you` : `${plural(n, "photo")} held for you`;
    const why = held != null && held > n ? ": your view options (screenshots, stacks) and what you hide leave the rest out" : "";
    const wait = waiting ? `. ${fmt(waiting)} ${waiting === 1 ? "waits" : "wait"} for you to look ${waiting === 1 ? "it" : "them"} over` : "";
    return { text: `${shows}${why}${wait}. They look like a receipt, a screenshot or a document, and only you see them until you release them.`,
      offers: [{ ...toReview, primary: !!waiting }, { label: "Choose…", what: "choose", primary: !waiting }] };
  }
  const k = chosen.size;
  if (view.state === "choosing") {
    return { text: k ? `${plural(k, "photo")} chosen` : "Tap the photos to release",
      offers: [{ label: k ? `Release ${k}` : "Release", what: "chosen", primary: true }, { label: "Cancel", what: "cancel" }] };
  }
  return { text: `Releasing ${plural(k, "photo")}…`, offers: [] };
}

/**
 * The viewer's private line.
 * @param {{ send: (sha: string, body: Change) => Promise<{ private?: Private }>, onChange?: (to: PrivateState, from: PrivateState) => void }} options
 *   `send`: POST /api/private {shas: [sha], ...body}, which answers with the photo's `private` as it is now
 */
export function privateLine({ send, onChange }) {
  const line = flow("private", PRIVATE_STEPS, /** @type {PrivateState} */ ("idle"), (to, from) => onChange?.(to, from));
  return {
    line,
    /** The photo the line is about, or null. @returns {string | null} */
    get sha() { return line.is("idle") ? null : line.data.sha; },
    /** What the line says (lineSays), with busy while a change is on its way; null for nothing. */
    get says() {
      if (line.is("idle")) return null;
      const s = lineSays(line.data.p);
      return s && { ...s, busy: line.is("saving") };
    },
    /** The details' row (rowSays), with busy; null for none. */
    get row() {
      if (line.is("idle")) return null;
      const r = rowSays(line.data.p);
      return r && { ...r, busy: line.is("saving") };
    },
    /**
     * What the why answer said of the photo showing. While a change is on its way it waits for that (null: ignored).
     * @param {string} sha
     * @param {Private | null | undefined} p
     */
    look(sha, p) {
      if (line.is("saving")) return false;
      line.to("known", { sha, p: p || null });
      return true;
    },
    /**
     * Withdraw, release, or set the release day of the photo the line is about (from known only). Never
     * rejects: { ok: true } with the line as the server has it now, or { ok: false, error } as it was.
     * @param {Change} body
     * @returns {Promise<{ ok: true } | { ok: false, error: unknown }>}
     */
    async act(body) {
      if (!line.is("known")) return { ok: false, error: new Error("Wait for the last change to be saved.") };
      const { sha, p } = line.data;
      const mine = line.to("saving", { sha, p, body });
      try {
        const got = await send(sha, body);
        if (line.data === mine) line.to("known", { sha, p: got?.private ?? p });
        return { ok: true };
      } catch (error) {
        if (line.data === mine) line.to("known", { sha, p });
        return { ok: false, error };
      }
    },
    /** The viewer closed. */
    clear() { if (!line.is("idle")) line.to("idle"); },
  };
}

/**
 * The held view: its bar, and choosing photos in it to release.
 * @param {{ release: (what: { shas: string[] }) => Promise<{ released: number }>,
 *   onChange?: (to: HeldState, from: HeldState) => void }} options
 *   `release`: POST /api/private {private: false, shas} for those chosen
 */
export function heldView({ release, onChange }) {
  const view = flow("held", HELD_STEPS, /** @type {HeldState} */ ("off"), (to, from) => onChange?.(to, from));
  const counts = () => ({ held: view.data.held, waiting: view.data.waiting });
  return {
    view,
    /** The held view shows `n` photos (asked again: the count changes). @param {number} n */
    open(n) {
      if (view.is("releasing")) return;
      const was = view.is("off") ? {} : counts();
      view.to(view.is("choosing") ? "choosing" : "viewing", { ...(view.is("choosing") ? view.data : {}), ...was, n });
    },
    /** How many are held for the member, and how many wait to be looked over (GET /api/held?count=1). @param {{ held: number, waiting: number }} c */
    counted(c) {
      if (view.is("viewing", "choosing", "releasing")) view.to(view.state, { ...view.data, held: c.held, waiting: c.waiting });
    },
    /** It no longer shows (another search). */
    close() { if (!view.is("off")) view.to("off"); },
    /** Start choosing: a tile tapped is chosen, not opened. */
    choose() { if (view.is("viewing")) view.to("choosing", { ...view.data, chosen: new Set() }); },
    /** Stop choosing; nothing is released. */
    cancel() { if (view.is("choosing")) view.to("viewing", { n: view.data.n, ...counts() }); },
    /** Whether a tile tap chooses (true) or opens. */
    get choosing() { return view.is("choosing"); },
    /** Whether this photo is chosen (while choosing, or being released). @param {string} sha */
    chosen(sha) { return view.is("choosing", "releasing") && !!view.data.chosen?.has(sha); },
    /** Choose a photo, or take it back. @param {string} sha @returns {boolean} whether it is chosen now */
    toggle(sha) {
      if (!view.is("choosing")) return false;
      const chosen = new Set(view.data.chosen);
      const on = !chosen.delete(sha);
      if (on) chosen.add(sha);
      view.to("choosing", { ...view.data, chosen });
      return on;
    },
    /** Release the photos chosen: exactly those, never any other. */
    async releaseChosen() {
      if (!view.is("choosing") || !view.data.chosen.size) return { ok: false, error: new Error("Choose the photos to release first.") };
      const before = view.data;
      const mine = view.to("releasing", { ...before });
      try {
        const got = await release({ shas: [...before.chosen] });
        const released = got?.released ?? 0;
        if (view.data === mine) view.to("viewing", { n: Math.max(0, before.n - released), held: before.held == null ? before.held : Math.max(0, before.held - released), waiting: before.waiting });
        return { ok: true, released };
      } catch (error) {
        if (view.data === mine) view.to("choosing", before);   // as it was
        return { ok: false, error };
      }
    },
  };
}
