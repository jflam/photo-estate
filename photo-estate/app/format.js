// format.js: numbers and dates as the pages show them.
//
//   fmt(n)                           1234567 -> "1,234,567", in the browser's language
//   plural(n, one, many = one + "s") "1 photo", "1,204 photos"
//   dateParts(when)                  "2016-07-04T09:30…" -> a Date at that wall-clock time, or null

/**
 * A number with its thousands grouped, as the browser's language writes it.
 * @param {number | string} n
 * @returns {string}
 */
export const fmt = n => Number(n).toLocaleString();

/**
 * A count with its noun: "1 photo", "1,204 photos".
 * @param {number} n
 * @param {string} one
 * @param {string} [many]
 * @returns {string}
 */
export const plural = (n, one, many = one + "s") => `${fmt(n)} ${n === 1 ? one : many}`;

/**
 * The date a catalog timestamp names, read as wall-clock time where the photo was taken: a zone it may
 * carry is ignored, so a photo taken at 9 in the morning says 9 wherever it is looked at. A bare year
 * ("2016") is not a date here; "2016-07" is the first of that month.
 * @param {string | null | undefined} when  "YYYY-MM", "YYYY-MM-DD" or "YYYY-MM-DDTHH:MM…"
 * @returns {Date | null}
 */
export function dateParts(when) {
  const m = /^(\d{4})-(\d\d)(?:-(\d\d))?(?:T(\d\d):(\d\d))?/.exec(when || "");
  return m && new Date(+m[1], +m[2] - 1, +(m[3] || 1), +(m[4] || 0), +(m[5] || 0));
}
