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
//   favourites({ save, show, saved, failed })   -> { set, toggle, saving, pending }

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

  /**
   * @param {string} key @param {T} item @param {{ server: boolean, want: boolean }} rec
   * @returns {Promise<boolean>} whether everything asked for was saved
   */
  async function run(key, item, rec) {
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

  return {
    set,
    /** The other of what was last asked for. @param {string} key @param {boolean} now @param {T} item */
    toggle(key, now, item) { const rec = open.get(key); return set(key, !(rec ? rec.want : now), now, item); },
    /** Whether a save of this photo is under way. @param {string} key */
    saving: key => open.has(key),
    /** How many photos have a save under way. */
    get pending() { return open.size; },
  };
}
