// viewer-flow.js: the photo viewer and its history entry (issue #81). The history here is a model of
// the browser's: back() is answered later, by the test, as a popstate.
import test from "node:test";
import assert from "node:assert/strict";

import { viewerFlow } from "../viewer-flow.js";
import { TransitionError } from "../fsm.js";
import { timers } from "./helpers.js";

/** A viewer on a model of the browser's history: a stack of entries, and backs still to be answered. */
function viewer() {
  const t = timers();
  /** @type {any[]} */ const stack = [{ tab: "search", d: 0 }];
  let backs = 0;
  /** @type {string[]} */ const log = [];
  const v = viewerFlow({ setTimer: t.setTimer, clearTimer: t.clearTimer, history: {
    push() { stack.push({ ...stack[stack.length - 1], v: 1 }); log.push("push v"); },
    back() { backs++; log.push("back"); },
    write(mode, entry, to) { if (mode === "push") stack.push(entry); else stack[stack.length - 1] = entry; log.push(`${mode} ${to}`); },
  } });
  /** The browser gets round to a back(): the entry goes, and it says so. Returns what the viewer made of it. */
  const pop = () => { assert.ok(backs > 0, "no back() is waiting"); backs--; stack.pop(); return v.popped(stack[stack.length - 1]); };
  /** The person presses Back. */
  const pressBack = () => { stack.pop(); return v.popped(stack[stack.length - 1]); };
  const list = { items: ["p0", "p1", "p2", "p3"] };
  return { v, t, stack, log, list, pop, pressBack, get backs() { return backs; } };
}

test("viewer: open, next and previous, close with the button", () => {
  const { v, t, stack, list, pop } = viewer();
  assert.equal(v.state, "closed");
  assert.equal(v.open, null);
  const o = v.openOn(list, 1);
  assert.equal(v.state, "open");
  assert.equal(v.open, o);
  assert.equal(o?.items, list.items);
  assert.deepEqual({ ...o, list: null, items: null }, { list: null, items: null, i: 1, pick: null, stack: null, sheetUp: false, share: null, note: "", settle: null });
  assert.equal(stack.length, 2, "an open photo is a history entry");
  assert.equal(stack[1].v, 1);

  // what belongs to one photo does not follow to the next
  Object.assign(o, { pick: "shot", stack: ["p1", "shot"], share: { sha: "p1" }, note: "Tap share again", settle: () => {}, sheetUp: true });
  assert.equal(v.neighbour(1), 2);
  assert.equal(v.show(2), o);
  assert.deepEqual([o.i, o.pick, o.stack, o.share, o.note, o.settle], [2, null, null, null, "", null]);
  assert.equal(o.sheetUp, true, "the details stay up from photo to photo");
  assert.equal(v.neighbour(-1), 1);
  v.show(1); v.show(0);
  assert.equal(v.neighbour(-1), null, "no photo before the first");
  const first = list.items;
  list.items = [];                                   // the grid is asked for another list: the open photo is still one of those it opened on
  assert.equal(v.neighbour(1), 1);
  assert.equal(o.items, first);
  list.items = first;
  v.show(3);
  assert.equal(v.neighbour(1), null, "nor after the last");

  assert.equal(v.close(), o, "closing hands back what the viewer owned, for the page to put the photo away");
  assert.equal(v.state, "leaving");
  assert.equal(v.open, null);
  assert.deepEqual(t.waits, [1000]);
  assert.equal(pop(), "left");
  assert.equal(v.state, "closed");
  assert.equal(stack.length, 1);
  assert.deepEqual(t.waits, [], "the fallback timer went with the state it belonged to");
});

test("viewer: no viewer data survives closed", () => {
  for (const how of /** @type {const} */ (["back", "gone", "taken"])) {
    const { v, list, pop } = viewer();
    const o = v.openOn(list, 2);
    assert.ok(o);
    Object.assign(o, { pick: "shot", stack: ["a", "b"], share: { sha: "x", file: {} }, note: "n", sheetUp: true, settle: () => {} });
    v.close(how);
    if (how === "back") {
      assert.deepEqual(Object.keys(v.flow.data).sort(), ["stop", "timer", "waiting"], "leaving owns only its own");
      pop();
    }
    assert.equal(v.state, "closed");
    assert.deepEqual(v.flow.data, {}, `closed owns nothing (${how})`);
    assert.equal(v.open, null);
    assert.equal(v.show(1), null, "there is no photo to move to");
    assert.equal(v.neighbour(1), null);
    assert.equal(v.close(), null, "closing a closed viewer does nothing");
    const again = v.openOn(list, 0);
    assert.notEqual(again, o, "the next opening starts from nothing");
    assert.deepEqual([again?.pick, again?.stack, again?.share, again?.note, again?.sheetUp], [null, null, null, "", false]);
  }
});

test("viewer: close during opening (pressed again before the browser has taken the entry back) and reopen", () => {
  const { v, list, stack, pop } = viewer();
  v.openOn(list, 0);
  v.close();                                         // straight away: the photo is still flying in
  assert.equal(v.state, "leaving");
  assert.equal(v.openOn(list, 1), null, "not opened again while the last one is still leaving");
  assert.equal(v.close(), null);
  assert.equal(stack.length, 2, "nothing was pushed for the refused opening");
  assert.equal(pop(), "left");
  const o = v.openOn(list, 1);
  assert.equal(o?.i, 1);
  assert.equal(v.openOn(list, 2), null, "nor while one is open");
  assert.equal(v.open?.i, 1);
  assert.throws(() => v.flow.to("open"), TransitionError, "the machine itself refuses open -> open");
});

test("viewer: back-button close versus button close", () => {
  // The button: the page closes, then the browser reports the entry gone.
  const a = viewer();
  a.v.openOn(a.list, 0);
  a.v.close("back");
  assert.deepEqual(a.log, ["push v", "back"]);
  assert.equal(a.pop(), "left");
  assert.equal(a.backs, 0);

  // Back: the browser has taken the entry already; the page is told to close, and asks for no back().
  const b = viewer();
  const o = b.v.openOn(b.list, 0);
  assert.equal(b.pressBack(), "close");
  assert.equal(b.v.state, "open", "popped() only says so: the page closes it, with what it knows of the photo");
  assert.equal(b.v.close("gone"), o);
  assert.equal(b.v.state, "closed");
  assert.deepEqual(b.log, ["push v"], "no back() for an entry that has already gone");
  assert.equal(b.stack.length, 1);

  // Both end the same way: closed, one entry, and Back now goes to the page's own entry.
  for (const x of [a, b]) {
    x.stack.push({ tab: "library" });
    assert.equal(x.pressBack(), "restore");
  }

  // A search from the open photo takes its entry's place.
  const c = viewer();
  c.v.openOn(c.list, 0);
  c.v.close("taken");
  c.v.nav("replace", { tab: "search", s: "q=dog" }, "?q=dog");
  assert.deepEqual(c.stack, [{ tab: "search", d: 0 }, { tab: "search", s: "q=dog" }]);
});

test("viewer: steps asked for while leaving wait for the entry to go, then are written in order", () => {
  const { v, list, stack, log, pop } = viewer();
  v.nav("push", { tab: "search", s: "q=a" }, "?q=a");
  assert.deepEqual(log, ["push ?q=a"], "with no viewer leaving, a step is written at once");
  v.openOn(list, 0);
  v.close();
  v.nav("push", { tab: "albums" }, "?albums");       // a tab pressed while the photo is still going
  v.nav("replace", { tab: "albums", alb: 3 }, "?album=3");
  assert.equal(stack.length, 3, "nothing written yet: it would be the entry taken back");
  assert.equal(pop(), "left");
  assert.deepEqual(log.slice(-2), ["push ?albums", "replace ?album=3"]);
  assert.deepEqual(stack.map(e => e.tab + (e.alb || "")), ["search", "search", "albums3"]);
});

test("viewer: the popstate fallback timeout closes a viewer the browser never reported, once", () => {
  const { v, t, list, log } = viewer();
  v.openOn(list, 0);
  v.close();
  v.nav("push", { tab: "library" }, "/");
  assert.deepEqual(t.waits, [1000]);
  t.fire();                                          // a second went by and no popstate came
  assert.equal(v.state, "closed");
  assert.deepEqual(log.slice(-1), ["push /"], "the waiting step was written");
  assert.deepEqual(v.flow.data, {});

  // a timer that fires although it was cleared (popstate came first) does nothing, even with the viewer open again
  /** @type {(() => void)[]} */ const fired = [];
  const stack = [{}];
  const w = viewerFlow({ setTimer: fn => fired.push(fn), clearTimer: () => {}, history: { push() { stack.push({ v: 1 }); }, back() {}, write() {} } });
  w.openOn(list, 0);
  w.close();
  assert.equal(w.popped({}), "left");
  const o = w.openOn(list, 2);
  assert.doesNotThrow(() => fired[0]());
  assert.equal(w.state, "open");
  assert.equal(w.open, o);
});

test("viewer: forward into the entry of a photo that is no longer open steps back over it", () => {
  const { v, stack, log } = viewer();
  stack.push({ tab: "search", v: 1 });               // forward, to a viewer's old entry
  assert.equal(v.popped(stack[stack.length - 1]), "over");
  assert.deepEqual(log, ["back"]);
  assert.equal(v.state, "closed");
  assert.equal(v.popped(null), "restore", "an address with no entry behind it is the page's to show");
});
