// albums-flow.js: the Albums tab of the photo app (search.html), apart from the list of an album's
// photos (grid-flow.js): which of the two it shows, the list of albums as the server gave it, and the
// making of an album from a search. Each is a strict machine whose states own their data (fsm.js).
//
// Where: the list of albums, or one of them.
//
//   list ◄──► album          album owns { id }. (What the server said of that album, its title and
//                            sections, is owned by the list of its photos: the grid's `ready` state.)
//
// The list of albums:
//
//   idle ──► loading ──► ready ──► loading ...
//               └──────► failed ──► loading
//   ready owns { sets, ... } (the server's answer), failed owns { error }. Asking again aborts the
//   asking before it.
//
// An album from a search: every photo the search finds, fixed as they are now.
//
//   idle ──► collecting ──► creating ──► idle
//               └──► idle (cancelled, or a page did not come)
//   collecting  the search's pages are being read, one after another. owns { key, title, prompt }:
//               the question, the album's title and its note, all as they were when it was asked for.
//               cancel() ends it: the request under way is aborted, no further page is asked for,
//               and no album is made.
//   creating    the one request that makes the album has been sent. It is not cancelled: an album
//               half asked for is worse than one the person has moved on from. make() resolves to
//               it, and the page says so wherever the person now is.
//
// The new photos waiting for a series of the member's (multiplayer phase 7: listeners.py offers them, a
// series_update inbox item opens the series): added, those that belong, or none.
//
//   idle ──► loading ──► ready ──► saving ──► idle
//               └──────► idle (nothing waiting) | failed ──► loading
//   ready  owns { album, results, chosen }: the photos, and the shas chosen to add (all of them at first).
//          ready ──► ready: one more chosen, or one fewer
//   saving owns the same: the one request that adds them (an empty add says "none of these")
//
//   albumsFlow({ list })           -> { where, listing, id, go, load }
//   albumMaker({ page, create })   -> { job, make, cancel }
//   candidatesFlow({ list, add })  -> { page, load(album), toggle(sha), add(), none() }
//   ALBUMS_STEPS, LISTING_STEPS, MAKING_STEPS, CANDIDATES_STEPS

import { flow, latest, STALE } from "./fsm.js";

/** @typedef {"list" | "album"} AlbumsState */
/** @typedef {"idle" | "loading" | "ready" | "failed"} ListingState */
/** @typedef {"idle" | "collecting" | "creating"} MakingState */

export const ALBUMS_STEPS = /** @type {Record<AlbumsState, readonly AlbumsState[]>} */ ({
  list: ["list", "album"],
  album: ["album", "list"],
});

export const LISTING_STEPS = /** @type {Record<ListingState, readonly ListingState[]>} */ ({
  idle: ["loading"],
  loading: ["loading", "ready", "failed"],
  ready: ["loading"],
  failed: ["loading"],
});

export const MAKING_STEPS = /** @type {Record<MakingState, readonly MakingState[]>} */ ({
  idle: ["collecting"],
  collecting: ["collecting", "creating", "idle"],
  creating: ["idle"],
});

/** @param {{ list: (signal: AbortSignal) => Promise<Record<string, any>> }} options  `list`: /api/albums */
export function albumsFlow({ list }) {
  const where = flow("albums", ALBUMS_STEPS, /** @type {AlbumsState} */ ("list"));
  const listing = flow("album list", LISTING_STEPS, /** @type {ListingState} */ ("idle"));
  const asking = latest();
  return {
    where, listing,
    /** The album showing, or null for the list of them. @returns {number | null} */
    get id() { return where.is("album") ? where.data.id : null; },
    /** Show an album, or (null) the list. @param {number | null} id */
    go(id) { if (id == null) where.to("list"); else where.to("album", { id }); },
    /**
     * Ask for the list of albums. Never rejects. Resolves to how it ended: "ready", "failed", or
     * STALE when it was asked again before this answer came.
     * @returns {Promise<"ready" | "failed" | typeof STALE>}
     */
    async load() {
      listing.to("loading");
      try {
        const d = await asking.run(signal => list(signal));
        if (d === STALE) return STALE;
        listing.to("ready", d);
        return "ready";
      } catch (error) {
        listing.to("failed", { error });
        return "failed";
      }
    },
  };
}

/**
 * @typedef {object} MakerOptions
 * @property {(key: string, cursor: number, signal: AbortSignal) => Promise<{ results: { sha: string }[], next: number | null }>} page
 *   a page of the search `key` from `cursor`
 * @property {(album: { title: string, prompt: string, items: { sha: string }[] }) => Promise<any>} create  POST /api/albums
 */

/** @param {MakerOptions} options */
export function albumMaker({ page, create }) {
  const job = flow("album from search", MAKING_STEPS, /** @type {MakingState} */ ("idle"));
  const collecting = latest();
  return {
    job,
    /**
     * Make an album of everything the search `key` finds. `title` and `prompt` are the album's, said
     * now: nothing is read from the page when the photos have been collected. Resolves to the album
     * the server made, or to STALE if cancel() (or another make()) came while the photos were still
     * being collected: then no album was made. Rejects if a page did not come, if the search finds
     * nothing, or if the server would not make the album.
     * @param {{ key: string, title: string, prompt: string }} what
     * @returns {Promise<any | typeof STALE>}
     */
    async make({ key, title, prompt }) {
      if (job.is("creating")) throw new Error("The last album is still being made.");
      const mine = job.to("collecting", { key, title, prompt });
      /** @type {{ sha: string }[]} */ const items = [];
      try {
        const got = await collecting.run(async signal => {
          /** @type {number | null} */ let c = 0;
          while (c != null) {                        // the whole answer, a page at a time, as the grid asks for it
            const d = await page(key, c, signal);
            if (signal.aborted) throw new DOMException("The album was cancelled", "AbortError");
            for (const r of d.results) items.push({ sha: r.sha });
            c = d.next;
          }
          return true;
        });
        if (got === STALE) return STALE;
        if (!items.length) throw new Error("This search finds no photos now.");
      } catch (e) {
        if (job.data === mine) job.to("idle");
        throw e;
      }
      job.to("creating", mine);
      try { return await create({ title, prompt, items }); } finally { job.to("idle"); }
    },
    /**
     * The person has moved on (another question, another place, the menu put away). Stops an album
     * whose photos are still being collected; one already being created is left to finish.
     * @returns {boolean} whether there was something to stop
     */
    cancel() {
      if (!job.is("collecting")) return false;
      collecting.cancel();
      job.to("idle");
      return true;
    },
  };
}

/** @typedef {"idle" | "loading" | "ready" | "saving" | "failed"} CandidatesState */

export const CANDIDATES_STEPS = /** @type {Record<CandidatesState, readonly CandidatesState[]>} */ ({
  idle: ["loading"],
  loading: ["loading", "ready", "idle", "failed"],
  ready: ["ready", "saving", "loading", "idle"],
  saving: ["idle", "ready"],
  failed: ["loading", "idle"],
});

/**
 * @param {{ list: (album: number, signal: AbortSignal) => Promise<{ results: { sha: string }[] }>,
 *   add: (album: number, shas: string[]) => Promise<{ added: number }>,
 *   onChange?: (to: CandidatesState, from: CandidatesState) => void }} options
 *   list: GET /api/albums/<id>/candidates; add: POST it {add}
 */
export function candidatesFlow({ list, add, onChange }) {
  const page = flow("candidates", CANDIDATES_STEPS, /** @type {CandidatesState} */ ("idle"), (to, from) => onChange?.(to, from));
  const asking = latest();
  /** @param {string[]} shas */
  async function save(shas) {
    if (!page.is("ready")) return { ok: false, error: new Error("Nothing is waiting.") };
    const d = page.data;
    page.to("saving", d);
    try {
      const got = await add(d.album, shas);
      page.to("idle");
      return { ok: true, added: got?.added ?? 0 };
    } catch (error) {
      page.to("ready", d);
      return { ok: false, error };
    }
  }
  return {
    page,
    /** The album the flow is about, or null. @returns {number | null} */
    get album() { return page.is("ready", "saving", "loading") ? page.data.album ?? null : null; },
    /**
     * What waits for series `album`. Never rejects: "ready" (something waits), "none", "failed", or STALE.
     * @param {number} album
     */
    async load(album) {
      if (page.is("saving")) return "busy";
      page.to("loading", { album });
      try {
        const d = await asking.run(signal => list(album, signal));
        if (d === STALE) return STALE;
        if (!d.results.length) { page.to("idle"); return "none"; }
        page.to("ready", { album, results: d.results, chosen: new Set(d.results.map(r => r.sha)) });
        return "ready";
      } catch (error) {
        if (page.is("loading")) page.to("failed", { error, album });
        return "failed";
      }
    },
    /** Choose a photo to add, or not. @param {string} sha @returns {boolean} whether it is chosen now */
    toggle(sha) {
      if (!page.is("ready")) return false;
      const chosen = new Set(page.data.chosen);
      if (chosen.has(sha)) chosen.delete(sha); else chosen.add(sha);
      page.to("ready", { ...page.data, chosen });
      return chosen.has(sha);
    },
    /** Add the chosen ones (in the order they are shown). Never rejects. */
    add() { return page.is("ready") ? save(page.data.results.map((/** @type {{ sha: string }} */ r) => r.sha).filter((/** @type {string} */ s) => page.data.chosen.has(s))) : save([]); },
    /** None of these belong. Never rejects. */
    none() { return save([]); },
    /** Another album, or the list: what was shown is let go. */
    clear() { asking.cancel(); if (!page.is("idle", "saving")) page.to("idle"); },
  };
}
