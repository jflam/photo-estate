// query.js: the question the Search tab asks, as a value, and as the query string that is both the page's
// address (?q=kitten&person=Ada) and what /api/search is sent. search.html holds the question being
// asked (S); everything here is a pure function of one.
//
//   KEYS                 the facets a question can be narrowed by, in the order they are written
//   blank()              the empty question
//   copy(s)              a question that can be changed without changing `s`
//   toParams(s)          -> URLSearchParams
//   fromParams(p)        <- URLSearchParams (what toParams wrote, or an address typed by hand)
//   filtered(s)          whether it is narrowed by anything but its words
//   isEmpty(s)           whether it asks nothing at all
//   FIELD_STEPS          the field the words are typed in: resting or typing

/** @typedef {"country" | "region" | "city" | "person" | "album" | "camera" | "lens" | "trip"} Facet */

/**
 * @typedef {object} Query
 * @property {string} q        the words: a description of the photo
 * @property {boolean} plain   names in the words are words, not people (names=0)
 * @property {string} sha      photos like this one (/api/similar)
 * @property {string} event    the photos of one event
 * @property {string} stack    the shots of one stack
 * @property {boolean} fav     favourites only
 * @property {string} from     the first month, "YYYY-MM"
 * @property {string} to       the last month
 * @property {string} sort     "" (the server's choice), or an order by name
 * @property {Record<Facet, string[]>} f   the values chosen for each facet
 */

/** @type {readonly Facet[]} */
export const KEYS = ["country", "region", "city", "person", "album", "camera", "lens", "trip"];

/** The parts of a question that are one string in the address when set. */
const TEXT = /** @type {const} */ (["event", "stack", "from", "to", "sort"]);

/**
 * @param {(k: Facet) => string[]} of
 * @returns {Record<Facet, string[]>}
 */
function facets(of) {
  const f = /** @type {Record<Facet, string[]>} */ ({});
  for (const k of KEYS) f[k] = of(k);
  return f;
}

/** @returns {Query} */
export const blank = () => ({ q: "", plain: false, sha: "", event: "", stack: "", fav: false, from: "", to: "", sort: "", f: facets(() => []) });

/**
 * @param {Query} s
 * @returns {Query}
 */
export const copy = s => ({ ...s, f: facets(k => [...s.f[k]]) });

/**
 * @param {Query} s
 * @returns {URLSearchParams}
 */
export function toParams(s) {
  const p = new URLSearchParams();
  if (s.q) p.set("q", s.q);
  if (s.q && s.plain) p.set("names", "0");        // names typed in the search are words, not people
  if (s.sha) p.set("sha", s.sha);
  for (const k of KEYS) for (const v of s.f[k]) p.append(k, v);
  for (const k of TEXT) if (s[k]) p.set(k, s[k]);
  if (s.fav) p.set("fav", "1");
  return p;
}

/**
 * @param {URLSearchParams} p
 * @returns {Query}
 */
export function fromParams(p) {
  const s = blank();
  s.q = p.get("q") || "";
  s.sha = p.get("sha") || "";
  for (const k of TEXT) s[k] = p.get(k) || "";
  for (const k of KEYS) s.f[k] = p.getAll(k);
  s.fav = p.get("fav") === "1";
  s.plain = p.get("names") === "0";
  return s;
}

/**
 * Whether the question is narrowed by anything but its words: a facet, an event, a stack, favourites, dates.
 * @param {Query} s
 * @returns {boolean}
 */
export const filtered = s => KEYS.some(k => s.f[k].length > 0) || !!(s.event || s.stack || s.fav || s.from || s.to);

/**
 * Whether the question asks nothing: no words, no photo to be like, no filter.
 * @param {Query} s
 * @returns {boolean}
 */
export const isEmpty = s => !s.q && !s.sha && !filtered(s);

/** @typedef {"resting" | "typing"} FieldState */

// The field the question's words are typed in is resting or being typed in. Typing starts with a tap in it
// and ends however it ends (a search, Cancel, a tap elsewhere): search.html's FIELD, a strict machine.
export const FIELD_STEPS = /** @type {Record<FieldState, readonly FieldState[]>} */ ({
  resting: ["typing"],
  typing: ["resting"],
});
