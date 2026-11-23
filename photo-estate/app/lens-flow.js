// lens-flow.js: lenses on the pages (multiplayer phase 3; the words are docs/notes/MULTIPLAYER-PLAN.md's). A
// lens is a filter (always on: it leaves photos out of everything a member sees) or a series (an album that
// is opened). search.html's Albums tab lists them and changes them; its viewer says why a photo is hidden
// and offers to show it anyway; changes.html names the lens a hiding set hides for.
//
// The lenses, as GET /api/lenses gave them, and one change to them at a time:
//
//   idle ──► loading ──► ready ──► saving ──► ready
//               └──────► failed ──► loading
//   ready ──► loading (asked again)
//
//   ready    owns the server's answer: { lenses, mine, sysadmin, proposed }
//   saving   owns the same, and { key }: what is being changed ("lens:3", "album:5"). The page shows that
//            row as busy and takes no other change until the server has answered; then ready again, with
//            the lenses as the server has them now (they are asked for again: one change can move several,
//            a snapshot makes a new one), or as they were if the change was refused.
//   failed   owns { error }
//
// The viewer's "why" line, for the photo showing:
//
//   idle ──► asking ──► known ──► saving ──► known (or failed, if asking again fails)
//              │  └───► failed       └─────► asking (another photo, while it saves)
//   asking, known, saving, failed ──► idle (the viewer closed)
//
//   asking   owns { sha }: a short wait (a photo swiped past is never asked about), then GET /api/photos/<sha>/why
//   known    owns { sha, why }: the server's answer; the line shows if the photo is hidden or shown anyway
//   saving   owns { sha, why, on }: "show it anyway" (on) or taking that back, sent; then asked again
//   failed   owns { sha, error }: the line does not show
//
//   lensesFlow({ list, onChange })  -> { page, load(), change(key, send) }
//   whyFlow({ ask, show, wait })    -> { line, sha, look(sha), toggle(), clear() }
//   offers(lens, listing)           what a lens's row offers: [{ label, what, body?, primary? }]
//   says(lens)                      what a lens's row says of it: ["Hides 3 photos", "On for you", ...]
//   said(lens, offer, answer)       what the page says once an offer is done
//   lensName(lens), whySays(why), day(when)   the words for a lens, for why a photo is hidden, for a day
//   SUBSCRIBING, LENSES_STEPS, WHY_STEPS

import { flow, latest, sleep, STALE } from "./fsm.js";
import { esc } from "./dom.js";
import { plural } from "./format.js";

/** @typedef {"idle" | "loading" | "ready" | "saving" | "failed"} LensesState */
/** @typedef {"idle" | "asking" | "known" | "saving" | "failed"} WhyState */
/**
 * @typedef {object} Lens   a lens as GET /api/lenses lists it
 * @property {number} id
 * @property {string} title
 * @property {"filter" | "series"} kind
 * @property {number | null} set_id          a series: its album
 * @property {boolean} published
 * @property {boolean} estate_default
 * @property {{ id: number | null, name: string | null }} owner
 * @property {boolean} mine
 * @property {"live" | "snapshot" | "opted_out" | null} subscription
 * @property {boolean} proposed
 * @property {boolean} [on]                  a filter: whether it hides for this member now
 * @property {number} [photos]               a filter: how many photos it hides
 * @property {number[]} [sets]               a filter of the member's own: its hiding sets
 */
/**
 * @typedef {object} Listing  GET /api/lenses
 * @property {Lens[]} lenses
 * @property {number | null} mine            the member's own filter, where their hides go by default
 * @property {boolean} sysadmin
 * @property {number[]} proposed
 */
/**
 * @typedef {object} Reason   one of GET /api/photos/<sha>/why's reasons
 * @property {{ id: number, title: string }} set
 * @property {string | null} hidden_at
 * @property {{ id: number, title: string, estate_default: boolean } | null} lens
 * @property {{ id: number | null, name: string | null }} owner
 * @property {boolean} mine
 */
/**
 * @typedef {object} Why      GET /api/photos/<sha>/why
 * @property {string} sha
 * @property {boolean} hidden
 * @property {boolean} shown_anyway
 * @property {Reason[]} reasons
 * @property {string | null} text
 * @property {boolean} [visible]
 */
/** @typedef {{ label: string, what: "subscribe" | "unsubscribe" | "publish" | "propose" | "decide", body?: Record<string, unknown>, primary?: boolean }} Offer */

export const LENSES_STEPS = /** @type {Record<LensesState, readonly LensesState[]>} */ ({
  idle: ["loading"],
  loading: ["loading", "ready", "failed"],
  ready: ["loading", "saving"],
  saving: ["ready"],
  failed: ["loading"],
});

export const WHY_STEPS = /** @type {Record<WhyState, readonly WhyState[]>} */ ({
  idle: ["asking"],
  asking: ["asking", "known", "failed", "idle"],
  known: ["asking", "saving", "idle"],
  saving: ["known", "asking", "failed", "idle"],
  failed: ["asking", "idle"],
});

/** What subscribing does, in the plan's words: said on the page beside each Subscribe. */
export const SUBSCRIBING = { filter: "Subscribing changes what you see everywhere.", series: "Subscribing adds it to your Albums." };

/**
 * @param {{ list: (signal: AbortSignal) => Promise<Listing>, onChange?: (to: LensesState, from: LensesState) => void }} options
 *   `list`: GET /api/lenses; `onChange`: told after every step (the page draws)
 */
export function lensesFlow({ list, onChange }) {
  const page = flow("lenses", LENSES_STEPS, /** @type {LensesState} */ ("idle"), (to, from) => onChange?.(to, from));
  const asking = latest();
  return {
    page,
    /** The lenses, when ready (or being changed): the server's answer. @returns {Listing | null} */
    get listing() { return page.is("ready", "saving") ? /** @type {Listing} */ (page.data) : null; },
    /**
     * Ask for the lenses. Never rejects. Resolves to how it ended: "ready", "failed", STALE when asked again
     * before the answer came, or "busy" while a change is being saved (it is not asked then).
     * @returns {Promise<"ready" | "failed" | "busy" | typeof STALE>}
     */
    async load() {
      if (page.is("saving")) return "busy";
      page.to("loading");
      try {
        const d = await asking.run(signal => list(signal));
        if (d === STALE) return STALE;
        page.to("ready", d);
        return "ready";
      } catch (error) {
        if (page.is("loading")) page.to("failed", { error });
        return "failed";
      }
    },
    /**
     * One change (`send`: the POST), from ready only: one at a time. Then the lenses are asked for again.
     * Never rejects: { ok: true, answer } (the POST's answer), or { ok: false, error } (refused, or not
     * ready) with the lenses as they were.
     * @param {string} key   what is changed, which the page shows as busy meanwhile
     * @param {() => Promise<any>} send
     * @returns {Promise<{ ok: true, answer: any } | { ok: false, error: unknown }>}
     */
    async change(key, send) {
      if (!page.is("ready")) return { ok: false, error: new Error("Wait for the last change to be saved.") };
      const d = /** @type {Listing} */ (page.data);
      page.to("saving", { ...d, key });
      let answer;
      try { answer = await send(); }
      catch (error) { page.to("ready", d); return { ok: false, error }; }
      let fresh = d;
      try { fresh = await asking.run(signal => list(signal)).then(x => (x === STALE ? d : x)); } catch { /* the change stands; the list is as it was */ }
      page.to("ready", fresh);
      return { ok: true, answer };
    },
  };
}

/**
 * The words for a lens, as HTML: "your lens <i>Hidden photos</i>", "Ada's lens <i>Estate defaults</i>".
 * @param {{ title: string, mine: boolean, owner: { name: string | null } }} lens
 * @returns {string}
 */
export const lensName = lens => `${lens.mine ? "your" : `${esc(lens.owner.name || "someone")}’s`} lens <i>${esc(lens.title)}</i>`;

/**
 * A day as the pages say it: "Nov 3", with the year when it is not this one ("Nov 3, 2025"); "" for none. A
 * moment the server wrote ("2026-11-23T03:00:00Z", UTC) is the day it was where the page is looked at (or in
 * `timeZone`); a bare day ("2026-11-23") is that day.
 * @param {string | null | undefined} when   "YYYY-MM-DD", or "YYYY-MM-DDTHH:MM…Z"
 * @param {Date} [today]
 * @param {string} [timeZone]   (for the tests) an IANA zone in place of the browser's own
 * @returns {string}
 */
export function day(when, today = new Date(), timeZone = undefined) {
  const m = /^(\d{4})-(\d\d)-(\d\d)(T.+)?/.exec(when || "");
  if (!m) return "";
  const at = m[4] ? new Date(/** @type {string} */ (when)) : null;
  if (at && !Number.isNaN(at.getTime())) {
    const zone = timeZone ? { timeZone } : {};
    const year = (/** @type {Date} */ d) => d.toLocaleDateString("en-US", { year: "numeric", ...zone });
    return at.toLocaleDateString("en-US", { month: "short", day: "numeric", ...zone, ...(year(at) === year(today) ? {} : { year: "numeric" }) });
  }
  const d = new Date(+m[1], +m[2] - 1, +m[3]);
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", ...(+m[1] === today.getFullYear() ? {} : { year: "numeric" }) });
}

/**
 * Why a photo is hidden, as HTML, from GET /api/photos/<sha>/why: "Hidden by your lens <i>Hidden photos</i> on
 * Nov 23", "Shown anyway. Hidden by Ada's lens <i>Estate defaults</i> on Nov 3", "…, and by 1 more". "" when
 * nothing that is on for the member hides it. Built from `reasons`, not the server's `text` (which says the
 * day in UTC): the day is the viewer's own (day()).
 * @param {Why} why
 * @param {Date} [today]
 * @param {string} [timeZone]   (for the tests) as day()'s
 * @returns {string}
 */
export function whySays(why, today = new Date(), timeZone = undefined) {
  const r = why.reasons[0];
  if (!r) return "";
  const by = r.lens ? lensName({ title: r.lens.title, mine: r.mine, owner: r.owner })
    : `${r.mine ? "your" : `${esc(r.owner.name || "someone")}’s`} change set <i>${esc(r.set.title)}</i>`;
  const on = day(r.hidden_at, today, timeZone), more = why.reasons.length - 1;
  return `${why.shown_anyway ? "Shown anyway. Hidden" : "Hidden"} by ${by}${on ? ` on ${on}` : ""}${more ? `, and by ${more} more` : ""}`;
}

/**
 * What a lens's row says of it, a part each: what it hides or is, whether it is on for this member, and
 * what subscribing would do.
 * @param {Lens} lens
 * @param {Pick<Listing, "sysadmin"> | null} [listing]   to a sysadmin, a proposed filter says it waits for them
 * @returns {string[]}
 */
export function says(lens, listing = null) {
  if (lens.kind === "series") {
    if (lens.mine) return [lens.published ? "Your series: published" : "Your album"];
    return [`${lens.owner.name || "Someone"}’s series`, lens.subscription === "live" ? "In your Albums"
      : lens.subscription === "snapshot" ? "You keep a copy of it" : SUBSCRIBING.series];
  }
  const hides = `Hides ${plural(lens.photos || 0, "photo")}`;
  const whose = lens.mine ? "Yours" : `${lens.owner.name || "Someone"}’s`;
  if (lens.estate_default) {
    return [whose, hides, lens.on ? "On for everyone, you too" : lens.subscription === "snapshot" ? "Off: you keep a copy of it" : "You opted out"];
  }
  if (lens.mine) return [hides, "Yours: always on", lens.proposed ? "Proposed as an estate default" : lens.published ? "Published" : "Only you see it"];
  if (lens.proposed && listing?.sysadmin) return [whose, hides, "Proposed as an estate default: every member would see through it"];
  return [whose, hides, lens.on ? "On for you" : lens.subscription === "snapshot" ? "Off: you keep a copy of it" : SUBSCRIBING.filter];
}

/**
 * What the page says once a lens's offer is done (a toast), from the lens as it was and the server's answer.
 * @param {Lens} lens
 * @param {Offer} offer
 * @param {Record<string, any>} [answer]
 * @returns {string}
 */
export function said(lens, offer, answer = {}) {
  const t = `“${lens.title}”`, n = plural(lens.photos || 0, "photo");
  if (offer.what === "subscribe" && offer.body?.mode === "snapshot") {
    return lens.kind === "series" ? `Kept a copy of ${t}: an album of your own, in your Albums`
      : `Kept a copy of ${t}: your own filter now, hiding ${plural(answer.photos ?? lens.photos ?? 0, "photo")}`;
  }
  if (offer.what === "subscribe") {
    if (lens.kind === "series") return `Subscribed: ${t} is in your Albums`;
    return lens.estate_default ? `Back in: ${t} hides ${n} from you again` : `Subscribed: ${t} hides ${n} from you everywhere`;
  }
  if (offer.what === "unsubscribe") return lens.estate_default ? `Opted out of ${t}: you see what it hides` : `Unsubscribed from ${t}`;
  if (offer.what === "publish") return offer.body?.on ? `Published: the family can subscribe to ${t}` : `${t} is only yours again`;
  if (offer.what === "propose") return `Proposed: a sysadmin decides whether ${t} becomes an estate default`;
  return offer.body?.accept ? `${t} is an estate default: everyone sees through it unless they opt out`
    : `Declined: ${t} stays ${lens.mine ? "yours" : `${lens.owner.name || "its owner"}’s`}`;
}

/**
 * What a lens's row offers, by what it is and how the member stands with it. A filter of someone else's:
 * subscribe (live) or keep a copy (a snapshot), or unsubscribe, or keep a copy instead of following it; an
 * estate default: opt out or back in, or keep a copy instead; a filter of the member's own: publish or take it back, and propose it as an estate default.
 * A proposed filter, to a sysadmin: accept or decline. A series of someone else's: subscribe, keep a copy,
 * unsubscribe; a series of the member's own: stop publishing.
 * @param {Lens} lens
 * @param {Pick<Listing, "sysadmin">} listing
 * @returns {Offer[]}
 */
export function offers(lens, listing) {
  const copy = /** @type {Offer} */ ({ label: "Keep a copy", what: "subscribe", body: { mode: "snapshot" } });
  const instead = /** @type {Offer} */ ({ ...copy, label: "Keep a copy instead" });   // (a copy ends following it live)
  if (lens.proposed && listing.sysadmin) {
    return [{ label: "Make it an estate default", what: "decide", body: { accept: true }, primary: true }, { label: "Decline", what: "decide", body: { accept: false } }];
  }
  if (lens.kind === "series") {
    if (lens.mine) return lens.published ? [{ label: "Stop publishing", what: "publish", body: { on: false } }] : [];
    return lens.subscription === "live" ? [{ label: "Unsubscribe", what: "unsubscribe" }]
      : [{ label: "Subscribe", what: "subscribe", body: { mode: "live" }, primary: true }, copy];
  }
  if (lens.estate_default) {
    return lens.on ? [{ label: "Opt out", what: "unsubscribe" }, instead]
      : [{ label: "Opt back in", what: "subscribe", body: { mode: "live" }, primary: true }];
  }
  if (lens.mine) {
    return [lens.published ? { label: "Stop publishing", what: "publish", body: { on: false } } : { label: "Publish", what: "publish", body: { on: true } },
      ...(lens.proposed ? [] : [/** @type {Offer} */ ({ label: "Propose as an estate default", what: "propose" })])];
  }
  return lens.subscription === "live" ? [{ label: "Unsubscribe", what: "unsubscribe" }, instead]
    : [{ label: "Subscribe", what: "subscribe", body: { mode: "live" }, primary: true }, copy];
}

/**
 * The viewer's "why" line.
 * @param {{ ask: (sha: string, signal: AbortSignal) => Promise<Why>, show: (sha: string, on: boolean) => Promise<unknown>,
 *   wait?: number, onChange?: (to: WhyState, from: WhyState) => void }} options
 *   `ask`: GET /api/photos/<sha>/why; `show`: POST /api/photos/<sha>/show {on}; `wait`: ms a photo is on
 *   screen before it is asked about (default 250); `onChange`: told after every step (the viewer draws)
 */
export function whyFlow({ ask, show, wait = 250, onChange }) {
  const line = flow("why", WHY_STEPS, /** @type {WhyState} */ ("idle"), (to, from) => onChange?.(to, from));
  const asking = latest();
  /** @param {string} sha @param {number} ms @returns {Promise<"known" | "failed" | typeof STALE>} */
  async function find(sha, ms) {
    const mine = line.data;
    try {
      const why = await asking.run(async signal => { if (ms) await sleep(ms, signal); return ask(sha, signal); });
      if (why === STALE || line.data !== mine) return STALE;
      line.to("known", { sha, why });
      return "known";
    } catch (error) {
      if (line.data !== mine) return STALE;
      line.to("failed", { sha, error });
      return "failed";
    }
  }
  return {
    line,
    /** The photo the line is about, or null. @returns {string | null} */
    get sha() { return line.is("idle") ? null : line.data.sha; },
    /**
     * What the line says now, or null when it says nothing (not asked yet, not hidden, failed).
     * @returns {{ html: string, action: string, busy: boolean } | null}
     */
    get says() {
      if (!line.is("known", "saving")) return null;
      const why = /** @type {Why} */ (line.data.why), html = whySays(why);
      if (!html) return null;
      return { html, action: why.shown_anyway ? "Hide it again" : "Show it anyway", busy: line.is("saving") };
    },
    /**
     * The photo showing is now `sha`: ask why (after `wait`). The same photo again changes nothing. Resolves
     * to how the asking ended ("known", "failed", or STALE once another photo or clear() came first).
     * @param {string} sha
     */
    look(sha) {
      if (!line.is("idle", "failed") && line.data.sha === sha) return Promise.resolve(/** @type {const} */ ("known"));
      line.to("asking", { sha });
      return find(sha, wait);
    },
    /**
     * "Show it anyway", or take that back, for the photo the line is about (from known only), then ask again.
     * Never rejects: { ok: true, why } with the new answer, or { ok: false, error } with the line as it was.
     * @returns {Promise<{ ok: true, why: Why | null } | { ok: false, error: unknown }>}
     */
    async toggle() {
      if (!line.is("known")) return { ok: false, error: new Error("Nothing to show or hide yet.") };
      const { sha, why } = line.data, on = !why.shown_anyway;
      const mine = line.to("saving", { sha, why, on });
      try { await show(sha, on); }
      catch (error) { if (line.data === mine) line.to("known", { sha, why }); return { ok: false, error }; }
      if (line.data !== mine) return { ok: true, why: null };       // another photo since: the change stands, said nowhere
      const got = await find(sha, 0);
      return { ok: true, why: got === "known" ? line.data.why : null };
    },
    /** The viewer closed: nothing is asked or shown. */
    clear() {
      asking.cancel();
      if (!line.is("idle")) line.to("idle");
    },
  };
}
