// toast.js with a stand-in for the page: an element that records what is done to it. `npm test` in photo-estate/app.
import test, { afterEach, beforeEach } from "node:test";
import assert from "node:assert/strict";

import { toast, TOAST_MS, TOAST_ACTION_MS } from "../toast.js";

/** As much of an element as toast() touches. */
function element() {
  const classes = new Set();
  /** @type {any[]} */ const children = [];
  let text = "";
  return {
    classList: { add: (/** @type {string} */ c) => classes.add(c), remove: (/** @type {string} */ c) => classes.delete(c) },
    get on() { return classes.has("on"); },
    get textContent() { return text; },
    set textContent(v) { text = String(v); children.length = 0; },     // as in the DOM: setting the text removes the children
    append: (/** @type {any} */ c) => children.push(c),
    children,
    /** @type {null | (() => void)} */ onclick: null,
  };
}

/** @type {ReturnType<typeof element>} */ let el;
beforeEach(t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  el = element();
  /** @type {any} */ (globalThis).document = { querySelector: (/** @type {string} */ s) => (s === "#toast" ? el : null), createElement: element };
});
afterEach(() => { delete /** @type {any} */ (globalThis).document; });

test("a toast shows its message as text and leaves by itself", t => {
  toast("Saved <b>3</b>");
  assert.equal(el.textContent, "Saved <b>3</b>", "text, never HTML");
  assert.equal(el.on, true);
  t.mock.timers.tick(TOAST_MS - 1);
  assert.equal(el.on, true);
  t.mock.timers.tick(1);
  assert.equal(el.on, false);
});

test("a new toast replaces the one showing and starts the time again", t => {
  toast("first");
  t.mock.timers.tick(TOAST_MS - 100);
  toast("second");
  t.mock.timers.tick(200);
  assert.equal(el.on, true, "the first toast's timer does not put the second away");
  assert.equal(el.textContent, "second");
  t.mock.timers.tick(TOAST_MS);
  assert.equal(el.on, false);
});

test("an action is a button: it stays longer, and pressing it puts the toast away and acts", t => {
  let undone = 0;
  toast("Saved 12", { action: { label: "Undo", fn: () => undone++ } });
  assert.equal(el.children.length, 1);
  const button = el.children[0];
  assert.equal(button.textContent, "Undo");
  t.mock.timers.tick(TOAST_MS);
  assert.equal(el.on, true, "longer than a plain toast");
  button.onclick();
  assert.equal(undone, 1);
  assert.equal(el.on, false);
  t.mock.timers.tick(TOAST_ACTION_MS);
  assert.equal(el.on, false);
});

test("a toast with an action leaves by itself after its own, longer time", t => {
  toast("Saved 12", { action: { label: "Undo", fn: () => {} } });
  t.mock.timers.tick(TOAST_ACTION_MS - 1);
  assert.equal(el.on, true);
  t.mock.timers.tick(1);
  assert.equal(el.on, false);
});

test("the next toast does not keep the last one's button", () => {
  toast("Saved 12", { action: { label: "Undo", fn: () => {} } });
  toast("Undone");
  assert.equal(el.children.length, 0);
});

test("ms sets how long; what toast returns puts it away now", t => {
  const hide = toast("Started", { ms: 50 });
  t.mock.timers.tick(49);
  assert.equal(el.on, true);
  hide();
  assert.equal(el.on, false);
  toast("Again", { ms: 50 });
  t.mock.timers.tick(50);
  assert.equal(el.on, false);
});

test("another element can be the toast; a page without one is left alone", () => {
  const other = element();
  toast("elsewhere", { el: /** @type {any} */ (other) });
  assert.equal(other.textContent, "elsewhere");
  assert.equal(el.on, false);
  /** @type {any} */ (globalThis).document.querySelector = () => null;
  assert.doesNotThrow(() => toast("nowhere to show this")());
});
