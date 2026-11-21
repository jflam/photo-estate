// favourite.js: the heart on a photo in the photo app (search.html). A favourite is said on the page
// at once and sent to the server behind; the person can press again before the answer comes. What
// must hold however the presses and the answers fall:
//
//   - the page shows what was last asked for, and when everything has been answered the server has it too;
//   - an answer never changes the heart: only a press does, or a save that failed;
//   - a save that fails puts the heart back to what the server has, and says so.
//
// So each photo with a save under way has one record { server, want }: what the server was last
// known to hold, and what the person last asked for. One request at a time per photo: when it is
// answered and `want` has moved on meanwhile, the next is sent; when it fails, `want` goes back to
// `server`. The record is gone when the two agree. The count of favourites that comes back with
// each answer is taken only from the answer to the request sent last, so two photos' answers
// arriving the other way round cannot leave an old count showing.
//
// Every photo of an album at once ("Make all favourites") is one request, and goes through here too, so
// that it and the hearts cannot cross: it waits for the saves under way, a heart pressed while it is
// being sent is sent after it, and two of them go one after the other. It is shown at once on the
// photos the page has; if it fails those are put back, except one the person has pressed since.
//
//   favourites({ save, show, saved, failed })   -> { set, toggle, all, saving, pending }

/**
 * @template T
 * @typedef {object} FavouriteOptions
 * @property {(item: T, fav: boolean) => Promise<any>} save      tell the server; its answer goes to `saved`
 * @property {(item: T, fav: boolean) => void} show              draw the photo as a favourite, or not
 * @property {(answer: any, item: T, fav: boolean) => void} [saved]   the newest answer (the favourites' count)
 * @property {(error: unknown, item: T, fav: boolean) => void} [failed]  a save failed and the heart was
 *   put back; `fav` is what could not be saved. Not called when the person had already asked for
 *   what the server has.
 */

/**
 * @template T
 * @param {FavouriteOptions<T>} options
 */
export function favourites({ save, show, saved, failed }) {
  /** @type {Map<string, { server: boolean, want: boolean, done: Promise<boolean> }>} */
  const open = new Map();
  let sent = 0, heard = 0;
  /** @type {Promise<any> | null} */ let bulk = null;   // the last "all of them" under way; it never rejects

  /**
   * @param {string} key @param {T} item @param {{ server: boolean, want: boolean }} rec
   * @returns {Promise<boolean>} whether everything asked for was saved
   */
  async function run(key, item, rec) {
    if (bulk) await bulk;                            // asked for after it, so sent after it
    while (rec.want !== rec.server) {
      const fav = rec.want, n = ++sent;
      try {
        const answer = await save(item, fav);
        rec.server = fav;
        if (n > heard) { heard = n; saved?.(answer, item, fav); }
      } catch (error) {
        const asked = rec.want;                      // (it may have moved on while this was being sent)
        rec.want = rec.server;
        open.delete(key);
        show(item, rec.server);
        if (asked !== rec.server) failed?.(error, item, fav);
        return false;
      }
    }
    open.delete(key);
    return true;
  }

  /**
   * The person asks for this photo to be a favourite, or not. `now` is what the page shows for it
   * (and, with no save under way, what the server has).
   * @param {string} key   the photo (its sha)
   * @param {boolean} want
   * @param {boolean} now
   * @param {T} item       handed to save and show
   * @returns {Promise<boolean>} done when the server has been told everything: true if it was all saved
   */
  function set(key, want, now, item) {
    const rec = open.get(key);
    if (rec) {
      rec.want = want;
      show(item, want);
      return rec.done;
    }
    if (want === now) return Promise.resolve(true);
    const fresh = { server: now, want, done: Promise.resolve(true) };
    open.set(key, fresh);
    show(item, want);
    return fresh.done = run(key, item, fresh);
  }

  /**
   * @param {() => [key: string, now: boolean, item: T][]} photos
   * @param {boolean} want
   * @param {(want: boolean) => Promise<any>} send
   * @param {Promise<any> | null} before
   * @returns {Promise<{ ok: true, answer: any } | { ok: false, error: unknown }>}
   */
  async function everyOne(photos, want, send, before) {
    if (before) await before;
    const first = photos();
    /** @type {[string, boolean, T][]} */ const changed = [];   // the ones to put back if it fails
    for (const [key, now, item] of first) {
      const rec = open.get(key);
      if (rec) rec.want = want;                      // its own save takes it there first, or says it could not
      else if (now !== want) changed.push([key, now, item]);
      show(item, want);
    }
    await Promise.all([...open.values()].map(rec => rec.done));   // (run() never rejects)
    const n = ++sent;
    try {
      const answer = await send(want);
      if (n > heard) heard = n;
      const seen = new Set();
      for (const [key, , item] of [...first, ...photos()]) {      // and the ones the page has got since
        if (seen.has(key)) continue;
        seen.add(key);
        const rec = open.get(key);
        if (rec) rec.server = want;                  // pressed since: that press is still to be sent, from here
        else show(item, want);
      }
      return { ok: true, answer };
    } catch (error) {
      for (const [key, now, item] of changed) {
        const rec = open.get(key);
        if (rec) rec.server = now;                   // pressed since: the press stands; the server has the old value
        else show(item, now);
      }
      return { ok: false, error };
    }
  }

  /**
   * Every photo of a set at once (an album's "Make all favourites"): one request, `send`, for photos
   * the page may not have. `photos()` says which of them the page has, as [key, what it shows, item];
   * it is asked when this starts and again when the answer comes, and says none if the page has left
   * the set meanwhile. The answer (how many changed) is the caller's to use; it never rejects.
   * @param {() => [key: string, now: boolean, item: T][]} photos
   * @param {boolean} want
   * @param {(want: boolean) => Promise<any>} send
   * @returns {Promise<{ ok: true, answer: any } | { ok: false, error: unknown }>}
   */
  function all(photos, want, send) {
    const mine = everyOne(photos, want, send, bulk);
    bulk = mine;
    mine.then(() => { if (bulk === mine) bulk = null; });
    return mine;
  }

  return {
    set,
    all,
    /** The other of what was last asked for. @param {string} key @param {boolean} now @param {T} item */
    toggle(key, now, item) { const rec = open.get(key); return set(key, !(rec ? rec.want : now), now, item); },
    /** Whether a save of this photo is under way. @param {string} key */
    saving: key => open.has(key),
    /** How many photos have a save under way. */
    get pending() { return open.size; },
  };
}
