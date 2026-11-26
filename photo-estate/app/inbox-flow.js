// inbox-flow.js: what inbox.html (/inbox) does over time: the member's open items as the server gave them
// (GET /api/inbox, member_inbox.py), and one item put away at a time. A strict machine whose states own their
// data (fsm.js). Each item opens the screen that already decides it; deciding there closes it on the server.
//
//   idle ──► loading ──► ready ──► dismissing ──► ready
//               └──────► failed ──► loading
//   ready ──► loading (asked again)
//
//   ready       owns the server's answer: { items, open }
//   dismissing  owns the same, and { id }: the item being put away. The page shows it as busy and takes no
//               other until the server has answered; then ready again, without it, or as it was if the server
//               refused (the page says why).
//   failed      owns { error }
//
//   inboxFlow({ list, dismiss })   -> { page, load(), dismiss(id) }
//   act(item)                      -> what an item's button says, and whether opening it puts it away (a notice)
//   badge(n)                       -> the count beside the Library menu's Inbox: "", "3", "9+"
//   KIND, INBOX_STEPS

import { flow, latest, STALE } from "./fsm.js";

/** @typedef {"idle" | "loading" | "ready" | "dismissing" | "failed"} InboxState */
/**
 * @typedef {object} Item   an item as GET /api/inbox lists it
 * @property {number} id
 * @property {string} kind              member_request | review | lens_proposed | lens_changed | held | question | series_update | binding
 * @property {string} ref               what it is about: member:<id>, album:<id>, lens:<id>, held:<batch>, ask:<group>
 * @property {string} title
 * @property {string} created_at
 * @property {string} updated_at        when it was made, or last brought up to date
 * @property {string | null} opens      the address that decides it
 */
/** @typedef {{ items: Item[], open: number }} Listing   GET /api/inbox */

export const INBOX_STEPS = /** @type {Record<InboxState, readonly InboxState[]>} */ ({
  idle: ["loading"],
  loading: ["loading", "ready", "failed"],
  ready: ["loading", "dismissing"],
  dismissing: ["ready"],
  failed: ["loading"],
});

/** Each kind, as the page names it (the pill on its card). */
export const KIND = /** @type {Record<string, string>} */ ({
  member_request: "Asking to be let in",
  review: "To look over",
  lens_proposed: "Proposed estate default",
  lens_changed: "A lens you follow",
  held: "Kept private to you",
  question: "A question for you",
  series_update: "New for a series",
  binding: "Your agent",
});

/**
 * What an item's button says, and whether opening it puts it away: a notice (lens_changed) waits on nobody,
 * so seeing it is all it asks; every other kind stays until it is decided where it opens.
 * @param {Pick<Item, "kind" | "ref">} item
 * @returns {{ label: string, notice: boolean }}
 */
export function act(item) {
  if (item.kind === "member_request") return { label: "Open Members", notice: false };
  if (item.kind === "review") return { label: "Look it over", notice: false };
  if (item.kind === "lens_proposed") return { label: "Decide in Albums", notice: false };
  if (item.kind === "lens_changed") return { label: item.ref.startsWith("album:") ? "Open the series" : "See it in Albums", notice: true };
  if (item.kind === "held") return { label: "Look them over", notice: false };   // (/held) done once none waits to be looked over
  if (item.kind === "question") return { label: "Answer", notice: false };       // done once each is answered or put away
  if (item.kind === "series_update") return { label: "Look at them", notice: false };   // done once they are added, or none
  if (item.kind === "binding") return { label: "Allow or turn off", notice: false };   // done once the member decides (/agents)
  return { label: "Open", notice: false };
}

/**
 * The count beside Inbox in the Library's menu: nothing for none, at most "9+".
 * @param {number} n
 * @returns {string}
 */
export const badge = n => (n > 9 ? "9+" : n > 0 ? String(n) : "");

/**
 * @param {{ list: (signal: AbortSignal) => Promise<Listing>, dismiss: (id: number) => Promise<{ open: number }>,
 *   onChange?: (to: InboxState, from: InboxState) => void }} options
 *   `list`: GET /api/inbox; `dismiss`: POST /api/inbox/<id>/dismiss; `onChange`: told after every step (the page draws)
 */
export function inboxFlow({ list, dismiss, onChange }) {
  const page = flow("inbox", INBOX_STEPS, /** @type {InboxState} */ ("idle"), (to, from) => onChange?.(to, from));
  const asking = latest();
  return {
    page,
    /**
     * Ask for the items. Never rejects. Resolves to how it ended: "ready", "failed", STALE when asked again
     * before the answer came, or "busy" while an item is being put away (it is not asked then).
     * @returns {Promise<"ready" | "failed" | "busy" | typeof STALE>}
     */
    async load() {
      if (page.is("dismissing")) return "busy";
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
     * Put one item away. Only from ready: one at a time. Never rejects: { ok: true } with the item gone from
     * the list, or { ok: false, error } (refused, or not ready) with the items as they were.
     * @param {number} id
     * @returns {Promise<{ ok: true } | { ok: false, error: unknown }>}
     */
    async dismiss(id) {
      if (!page.is("ready")) return { ok: false, error: new Error("Wait for the last one to be put away.") };
      const d = /** @type {Listing} */ (page.data);
      page.to("dismissing", { ...d, id });
      try {
        const got = await dismiss(id);
        const items = d.items.filter(x => x.id !== id);
        page.to("ready", { items, open: typeof got?.open === "number" ? got.open : items.length });
        return { ok: true };
      } catch (error) {
        page.to("ready", d);
        return { ok: false, error };
      }
    },
  };
}
