// format.js: numbers and dates as the pages show them.
//
//   fmt(n)                           1234567 -> "1,234,567", in the browser's language
//   plural(n, one, many = one + "s") "1 photo", "1,204 photos"
//   dateParts(when)                  "2016-07-04T09:30…" -> a Date at that wall-clock time, or null
//   duration(seconds)                12 -> "0:12", 754.4 -> "12:34", 3723 -> "1:02:03"; unknown -> ""

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

/**
 * A video's length as a clock writes it: "0:12", "12:34", "1:02:03". Rounded to the second, and never
 * "0:00" for a video that has any length. Unknown (null, not a number, below zero) is "".
 * @param {number | null | undefined} seconds
 * @returns {string}
 */
export function duration(seconds) {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return "";
  const t = seconds > 0 ? Math.max(1, Math.round(seconds)) : 0, two = (/** @type {number} */ n) => String(n).padStart(2, "0");
  const h = Math.floor(t / 3600), m = Math.floor(t % 3600 / 60), s = t % 60;
  return h ? `${h}:${two(m)}:${two(s)}` : `${m}:${two(s)}`;
}
