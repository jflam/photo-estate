// ask-flow.js: an inbox item's questions (/inbox?ask=<group>). Its machine (every pair of states), the item
// found by its group, a question answered moving under its answer (and staying there when asked again), a
// transcript written down, "I don't know" one at a time and a refusal, and the words: who asks, and the title.
import test from "node:test";
import assert from "node:assert/strict";

import { flow } from "../fsm.js";
import { askFlow, askedBy, groupOf, heading, ASK_STEPS } from "../ask-flow.js";
import { deferred, everyPair } from "./helpers.js";

const q = (/** @type {number} */ id, more = {}) => ({ id, text: `question ${id}`, why: null, photos: [{ sha: `s${id}` }], asked_at: "2026-11-23T10:00:00Z",
  asked_by: { id: 4, name: "The estate's agent", kind: "agent" }, for: null, to: /** @type {"you"} */ ("you"), ...more });
const listing = () => ({ items: [{ item: 9, group_key: "Thanksgiving 1998", questions: [q(3), q(4)] }, { item: 8, group_key: null, questions: [q(2)] }] });
const story = (/** @type {number} */ id, state = "waiting") => /** @type {any} */ ({ id, transcript_state: state, recording: { url: "", type: "" } });

test("the ask machine: every legal step is taken, every other pair throws TransitionError", () => {
  assert.deepEqual(everyPair(ASK_STEPS, (/** @type {any} */ s, /** @type {any} */ from) => flow("t", s, from), assert.throws),
    { legal: 9, refused: 16 });
});

test("an item is found by its group: its key, or q<id> for a question on its own", async () => {
  assert.equal(groupOf(listing().items[0]), "Thanksgiving 1998");
  assert.equal(groupOf(listing().items[1]), "q2");
  const f = askFlow({ list: async () => listing(), dismiss: async () => ({}) });
  assert.equal(await f.load("q2"), "ready");
  assert.deepEqual(f.page.data.questions.map((/** @type {any} */ x) => x.id), [2]);
  assert.equal(f.page.data.item.item, 8);
  await f.load("not any more");
  assert.equal(f.page.data.item, null, "answered elsewhere, or not dealt: nothing here");
  assert.deepEqual(f.page.data.questions, []);
});

test("answered: the question moves under its answer, stays there when asked again, and its transcript comes", async () => {
  let items = listing();
  const f = askFlow({ list: async () => items, dismiss: async () => ({}) });
  await f.load("Thanksgiving 1998");
  assert.equal(f.answered(3, story(70)), true);
  assert.equal(f.answered(3, story(70)), false, "once");
  assert.deepEqual(f.page.data.questions.map((/** @type {any} */ x) => x.id), [4]);
  assert.deepEqual(f.page.data.answered.map((/** @type {any} */ a) => [a.question.id, a.story.id]), [[3, 70]]);
  items = { items: [{ item: 9, group_key: "Thanksgiving 1998", questions: [q(4)] }] };   // the server no longer lists it
  await f.load("Thanksgiving 1998");
  assert.deepEqual(f.page.data.answered.map((/** @type {any} */ a) => a.story.id), [70], "kept when asked again");
  assert.equal(f.told(story(70, "done")), true);
  assert.equal(f.page.data.answered[0].story.transcript_state, "done");
  assert.equal(f.told(story(71, "done")), false, "not one answered here");
});

test("I don't know: one at a time, gone once the server has it; a refusal leaves it as it was", async () => {
  const gate = deferred();
  const sent = /** @type {number[]} */ ([]);
  const f = askFlow({ list: async () => listing(), dismiss: id => { sent.push(id); return gate.promise; } });
  await f.load("Thanksgiving 1998");
  const going = f.dismiss(3);
  assert.equal(f.page.state, "dismissing");
  assert.equal(f.page.data.id, 3);
  assert.equal((await f.dismiss(4)).ok, false);
  assert.equal(await f.load("Thanksgiving 1998"), "busy");
  assert.equal(f.answered(4, story(1)), false, "not while one is being put away");
  gate.resolve({ dismissed: true });
  assert.equal((await going).ok, true);
  assert.deepEqual(f.page.data.questions.map((/** @type {any} */ x) => x.id), [4]);
  assert.deepEqual(sent, [3]);
  const g = askFlow({ list: async () => listing(), dismiss: async () => { throw new Error("no such question"); } });
  await g.load("q2");
  const before = g.page.data;
  const got = await g.dismiss(2);
  assert.equal(got.ok, false);
  assert.equal(g.page.data, before);
});

test("a list that fails is failed, and asking again starts over", async () => {
  let fail = true;
  const f = askFlow({ list: async () => { if (fail) throw new Error("down"); return listing(); }, dismiss: async () => ({}) });
  assert.equal(await f.load("q2"), "failed");
  assert.equal(f.page.data.error.message, "down");
  fail = false;
  assert.equal(await f.load("q2"), "ready");
});

test("who asks, and the title", () => {
  assert.equal(askedBy(q(1)), "The estate’s agent asks you, for the family");
  assert.equal(askedBy(q(1, { for: "Ben" })), "The estate’s agent asks you, for Ben");
  assert.equal(askedBy(q(1, { asked_by: { id: 1, name: "Ada", kind: "person" }, to: "the family" })), "Ada asks the family");
  assert.equal(heading(listing().items[1]), "A question for you");
  assert.equal(heading(listing().items[0]), "2 questions about Thanksgiving 1998");
  assert.equal(heading(listing().items[0], 1), "2 questions about Thanksgiving 1998: 1 left");
  assert.equal(heading({ group_key: null, questions: [q(1), q(2)] }), "2 questions");
  assert.equal(heading(null), "Nothing to answer here");
});
