// layout.js: the justified grid's rows. `npm test` in photo-estate/app.
import test from "node:test";
import assert from "node:assert/strict";

import { MIN_SHAPE, MAX_SHAPE, clampShape, shapeFromByte, packRows } from "../layout.js";

const sum = (/** @type {number[]} */ a) => a.reduce((x, y) => x + y, 0);
/** A list of shapes that repeats: landscapes, portraits, a square, a panorama. */
const SHAPES = [1.5, 1.5, 0.67, 1.33, 1, 2.6, 0.75, 1.78, 1.5, 0.5];
const shape = (/** @type {number} */ i) => SHAPES[i % SHAPES.length];

test("clampShape keeps a shape drawable; an unknown one is 3:2", () => {
  assert.equal(clampShape(1.33), 1.33);
  assert.equal(clampShape(0.1), MIN_SHAPE);
  assert.equal(clampShape(12), MAX_SHAPE);
  for (const unknown of [0, null, undefined, NaN]) assert.equal(clampShape(unknown), 1.5);
});

test("shapeFromByte: the scale catalog.py writes, 0.5 to 2.6 in 255 steps", () => {
  assert.equal(shapeFromByte(0), 0.5);
  assert.ok(Math.abs(shapeFromByte(255) - 2.6) < 1e-12);
  for (const b of [0, 1, 17, 121, 200, 254, 255]) assert.equal(shapeFromByte(b), .5 + b * 2.1 / 255, `byte ${b}`);
  // a shape survives the round trip to within half a step (tests/test_layout.py checks the server's side)
  for (const ar of [0.5, 0.67, 1, 1.5, 1.78, 2.6]) {
    const b = Math.round((ar - 0.5) * 255 / 2.1);
    assert.ok(Math.abs(shapeFromByte(b) - ar) <= 2.1 / 255 / 2 + 1e-9, `shape ${ar}`);
  }
});

test("every photo is in exactly one row, in order", () => {
  const rows = packRows(57, shape, 0, 1200, 160, 3);
  let next = 0;
  for (const r of rows) {
    assert.equal(r.start, next);
    assert.ok(r.n >= 1);
    assert.equal(r.widths.length, r.n);
    next += r.n;
  }
  assert.equal(next, 57);
});

test("a full row is exactly as wide as the screen, and (but for a stretched last row) no taller than asked for", () => {
  for (const [width, height, gap] of [[1200, 160, 3], [375, 96, 2], [1917, 390, 3], [700, 64, 0]]) {
    const rows = packRows(200, shape, 0, width, height, gap);
    for (const r of rows.slice(0, -1)) assert.equal(r.full, true, "only the last row may be short");
    for (const r of rows.filter(r => r.full)) {
      assert.equal(sum(r.widths) + gap * (r.n - 1), width, `row at ${r.start} of ${width}px`);
      const most = r === rows.at(-1) ? Math.ceil(height / .9) : height;     // the last row may be stretched by a ninth
      assert.ok(r.height <= most, `row at ${r.start}: ${r.height}px is over ${most}px`);
      assert.ok(r.widths.every(w => w > 0));
    }
  }
});

test("photos keep their shapes: each width is its shape times the row's height, give or take the rounding", () => {
  for (const r of packRows(200, shape, 0, 1200, 160, 3)) {
    r.widths.forEach((w, k) => assert.ok(Math.abs(w - shape(r.start + k) * r.height) <= 1.5, `photo ${r.start + k}: ${w}px in a ${r.height}px row`));
  }
});

test("a short last row stays at the height asked for, ragged", () => {
  const rows = packRows(2, () => 1.5, 0, 1200, 160, 3);
  assert.deepEqual(rows, [{ start: 0, n: 2, full: false, height: 160, widths: [240, 240] }]);
});

test("a last row within a tenth of full is stretched to the edge", () => {
  // 4 photos of 1.5 at 160px: 960 of the 1011px there is room for, which is over nine tenths
  const [row] = packRows(4, () => 1.5, 0, 1020, 160, 3);
  assert.equal(row.full, true);
  assert.equal(sum(row.widths) + 3 * 3, 1020);
  assert.ok(row.height > 160 && row.height <= 170);
});

test("one photo wider than the screen has a row to itself, shrunk to fit", () => {
  const [row, ...rest] = packRows(1, () => 2.6, 0, 300, 160, 3);
  assert.equal(rest.length, 0);
  assert.deepEqual(row, { start: 0, n: 1, full: true, height: 115, widths: [300] });
});

test("rows are stable from the left: a longer list changes nothing before the unfinished last row", () => {
  const short = packRows(23, shape, 0, 1200, 160, 3), long = packRows(60, shape, 0, 1200, 160, 3);
  const done = short.filter(r => r.full);
  assert.ok(done.length >= 2);
  assert.deepEqual(long.slice(0, done.length), done);
});

test("packing from a row's start gives the rows from there on (how the grid grows)", () => {
  const all = packRows(60, shape, 0, 1200, 160, 3);
  const from = all[3].start;
  assert.deepEqual(packRows(60, shape, from, 1200, 160, 3), all.slice(3));
});

test("nothing to pack is no rows", () => {
  assert.deepEqual(packRows(0, shape, 0, 1200, 160, 3), []);
  assert.deepEqual(packRows(10, shape, 10, 1200, 160, 3), []);
});

test("a width that is not a whole number of px still ends (the spare px are rounded)", () => {
  const rows = packRows(40, shape, 0, 1187.5, 160, 3);
  assert.equal(sum(rows.map(r => r.n)), 40);
  for (const r of rows.filter(r => r.full)) assert.ok(Math.abs(sum(r.widths) + 3 * (r.n - 1) - 1187.5) <= 0.5);
});
