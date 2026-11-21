// The photo app's machines (issue #81: search.html): every table, every pair of states. A step in the
// table is taken; any other throws a TransitionError and leaves the machine, and its data, as it was.
import test from "node:test";
import assert from "node:assert/strict";

import { flow, machine, TransitionError } from "../fsm.js";
import { everyPair } from "./helpers.js";
import { VIEWER_STEPS, GESTURE_STEPS } from "../viewer-flow.js";
import { LIST_STEPS, END_STEPS } from "../grid-flow.js";
import { PLACE_STEPS, KNOWN_STEPS, SCALES } from "../library-flow.js";
import { ALBUMS_STEPS, LISTING_STEPS, MAKING_STEPS } from "../albums-flow.js";
import { FIELD_STEPS } from "../query.js";
import { VIDEO_STEPS } from "../video-flow.js";

const asFlow = (/** @type {any} */ steps, /** @type {any} */ from) => flow("t", steps, from);
const asMachine = (/** @type {any} */ steps, /** @type {any} */ from) => machine("t", steps, from, { strict: true, onRefuse: () => {} });

// name, table, how search.html makes it, legal steps, refused pairs
/** @type {[string, Record<string, readonly string[]>, typeof asFlow, number, number][]} */
const TABLES = [
  ["viewer (and its history entry)", VIEWER_STEPS, asFlow, 4, 5],
  ["gesture", GESTURE_STEPS, asFlow, 17, 32],
  ["field", FIELD_STEPS, asMachine, 2, 2],
  ["list of photos", LIST_STEPS, asFlow, 10, 6],
  ["end of a list", END_STEPS, asMachine, 4, 5],
  ["library place", PLACE_STEPS, asFlow, 16, 0],
  ["library known", KNOWN_STEPS, asFlow, 6, 10],
  ["albums", ALBUMS_STEPS, asFlow, 4, 0],
  ["album list", LISTING_STEPS, asFlow, 6, 10],
  ["album from search", MAKING_STEPS, asFlow, 5, 4],
  ["video in the viewer", VIDEO_STEPS, asFlow, 6, 10],
];

for (const [name, steps, make, legal, refused] of TABLES) {
  test(`${name} machine: every legal step is taken, every other pair throws TransitionError`, () => {
    assert.deepEqual(everyPair(steps, make, assert.throws), { legal, refused });
  });
}

test("a refused step of a flow leaves its state and the data the state owns untouched", () => {
  const f = flow("viewer", VIEWER_STEPS, "closed");
  const open = f.to("open", { i: 3 });
  f.to("leaving", { waiting: [] });
  const mine = f.data;
  assert.throws(() => f.to("open", { i: 9 }), TransitionError);
  assert.equal(f.state, "leaving");
  assert.equal(f.data, mine, "the data handed to the refused step was not taken");
  assert.notEqual(f.data, open);
});

test("a flow tells onChange the data of the state left, and a closed state owns nothing", () => {
  /** @type {any[]} */ const seen = [];
  const f = flow("viewer", VIEWER_STEPS, "closed", (to, from, data, old) => seen.push([from, to, data, old]));
  const open = f.to("open", { i: 1 });
  f.to("closed");
  assert.deepEqual(seen, [["closed", "open", open, {}], ["open", "closed", {}, open]]);
  assert.deepEqual(f.data, {});
});

test("the Library can go from any scale to any other", () => {
  assert.deepEqual([...SCALES].sort(), ["all", "month", "months", "years"]);
  for (const s of SCALES) assert.deepEqual(PLACE_STEPS[s], SCALES);
});
