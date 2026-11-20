// layout.js: the arithmetic of the justified grid (book chapter 13): photos packed into rows as wide as the
// screen, every photo at its own shape. search.html's Grid draws what this works out.
//
//   MIN_SHAPE, MAX_SHAPE      the narrowest and widest a tile is drawn (width / height)
//   clampShape(ar)            a photo's shape, kept within those; 1.5 when it is not known
//   shapeFromByte(b)          a byte of /api/layout back to the shape it stands for
//   packRows(n, shape, from, width, rowHeight, gap) -> rows
//
// A shape here is width / height: 1.5 is a landscape 3:2, 0.67 a portrait.

export const MIN_SHAPE = 0.5, MAX_SHAPE = 2.6;

/**
 * A photo's shape as the grid draws it: no narrower than MIN_SHAPE, no wider than MAX_SHAPE (a panorama
 * would take a row to itself and a sliver would be too thin to see). Unknown (0, null, NaN) is 1.5.
 * @param {number | null | undefined} ar
 * @returns {number}
 */
export const clampShape = ar => Math.min(MAX_SHAPE, Math.max(MIN_SHAPE, ar || 1.5));

/**
 * /api/layout sends every photo's shape as one byte: MIN_SHAPE to MAX_SHAPE in 255 steps
 * (catalog.py writes it).
 * @param {number} b  0-255
 * @returns {number}
 */
export const shapeFromByte = b => MIN_SHAPE + b * (MAX_SHAPE - MIN_SHAPE) / 255;

/**
 * @typedef {object} Row
 * @property {number} start    the index of its first photo
 * @property {number} n        how many photos are in it
 * @property {boolean} full    whether it reaches the right edge: only a list's last row may not
 * @property {number} height   px
 * @property {number[]} widths px, one per photo; in a full row they and the gaps add up to `width` exactly
 */

/**
 * Pack photos `from`..`n - 1` into rows `width` px wide. Photos are taken in order at `rowHeight` until
 * they reach the edge; the row is then shrunk until it fits exactly, so a full row is never taller than
 * `rowHeight`. The last row, if its photos do not reach the edge, stays at `rowHeight` and ragged, unless
 * it is within a tenth of full, in which case it is stretched to the edge.
 *
 * Rows are stable from the left: packing a longer list gives the same rows up to the last full one, so
 * the grid only lays out again from an unfinished last row.
 *
 * @param {number} n                       how many photos there are
 * @param {(i: number) => number} shape    photo i's shape (already clamped)
 * @param {number} from                    the first photo to pack
 * @param {number} width                   px across
 * @param {number} rowHeight               px: the height rows aim for
 * @param {number} gap                     px between photos in a row
 * @returns {Row[]}
 */
export function packRows(n, shape, from, width, rowHeight, gap) {
  /** @type {Row[]} */
  const rows = [];
  for (let i = from; i < n;) {
    let sum = 0, j = i, full = false;
    while (j < n) { sum += shape(j++); if (sum * rowHeight + gap * (j - i - 1) >= width) { full = true; break; } }
    const count = j - i, room = width - gap * (count - 1);
    if (!full && sum * rowHeight >= room * .9) full = true;
    const height = Math.round(full ? room / sum : rowHeight);
    const widths = [];
    for (let k = i; k < j; k++) widths.push(Math.round(shape(k) * height));
    if (full) {                                    // rounding leaves a few px over or under: spread them from the left
      let d = Math.round(room - widths.reduce((a, b) => a + b, 0));
      for (let k = 0; d !== 0; k = (k + 1) % count) { widths[k] += Math.sign(d); d -= Math.sign(d); }
    }
    rows.push({ start: i, n: count, full, height, widths });
    i = j;
  }
  return rows;
}
