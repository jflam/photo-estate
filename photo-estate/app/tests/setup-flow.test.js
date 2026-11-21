// setup-flow.js: the setup page's two machines (issue #80: setup.html).
import test from "node:test";
import assert from "node:assert/strict";

import { setupFlow, pageStateOf, askEvery, live, PAGE_STEPS, PANEL_STEPS, ASKING } from "../setup-flow.js";
import { flow, TransitionError } from "../fsm.js";

const answer = (/** @type {Partial<{ mode: string, chosen: boolean, jobs: string[] }>} */ o = {}) =>
  ({ mode: o.mode || "setup", library: { chosen: o.chosen ?? true }, jobs: (o.jobs || []).map(state => ({ state })) });

test("the setup machine accepts every legal step of the workflow and rejects illegal ones", () => {
  // The whole of a first setup, in order: choose a place, choose a source, check it, start it, answer
  // its prompt, watch it run, and leave for the photo app when the first photos are in.
  const f = setupFlow();
  assert.equal(f.page.state, "loading");
  assert.equal(f.panel.state, "closed");
  assert.equal(f.heard(answer({ chosen: false })), "place");
  assert.equal(f.heard(answer()), "idle");
  f.panel.to("open", { id: "takeout", config: {} });
  f.panel.to("checking", f.panel.data);
  f.panel.to("checked", { ...f.panel.data, preview: { ok: true, media: 12 } });
  f.panel.to("starting", f.panel.data);
  f.panel.to("closed");
  assert.equal(f.heard(answer({ jobs: ["running"] })), "watching");
  assert.equal(f.heard(answer({ jobs: ["waiting"] })), "answering");
  assert.equal(f.heard(answer({ jobs: ["running"] })), "watching");
  assert.equal(f.heard(answer({ mode: "starting", jobs: ["running"] })), "opening");
  f.page.to("stuck");
  f.page.to("opening");
  f.page.to("app");
  assert.equal(f.page.state, "app");

  // every step in the two tables is taken by a machine put in that state
  const reach = (/** @type {any} */ steps, /** @type {string} */ start) => {
    for (const [from, tos] of Object.entries(steps)) for (const to of /** @type {string[]} */ (tos)) {
      const m = flow("t", steps, from);
      assert.doesNotThrow(() => m.to(to), `${from} -> ${to}`);
      assert.equal(m.state, to);
    }
    assert.ok(start in steps);
  };
  reach(PAGE_STEPS, "loading");
  reach(PANEL_STEPS, "closed");

  // and every step that is not in them throws, and changes nothing
  const refuse = (/** @type {any} */ steps) => {
    let refused = 0;
    for (const from of Object.keys(steps)) for (const to of Object.keys(steps)) {
      if (steps[from].includes(to)) continue;
      const m = flow("t", steps, from);
      assert.throws(() => m.to(to, { lost: true }), TransitionError, `${from} -> ${to}`);
      assert.equal(m.state, from);
      assert.deepEqual(m.data, {}, "the refused step's data is not kept");
      refused++;
    }
    return refused;
  };
  assert.equal(refuse(PAGE_STEPS), 64 - Object.values(PAGE_STEPS).flat().length);
  assert.equal(refuse(PANEL_STEPS), 25 - Object.values(PANEL_STEPS).flat().length);

  // the ones that matter, by name
  const g = setupFlow();
  assert.throws(() => g.panel.to("starting"), TransitionError, "a job is not started from a closed panel");
  assert.throws(() => g.panel.to("checked"), TransitionError, "nor counted as checked without a check");
  g.panel.to("open", { id: "folder", config: {} });
  assert.throws(() => g.panel.to("starting"), TransitionError, "nor started before its preview said yes");
  assert.throws(() => g.page.to("stuck"), TransitionError, "only opening gets stuck");
  g.heard(answer());
  assert.throws(() => g.heard(answer({ chosen: false })), TransitionError, "a library, once chosen, is not unchosen");
  g.page.to("opening");
  g.page.to("app");
  for (const next of /** @type {const} */ (["loading", "idle", "opening", "stuck"])) assert.throws(() => g.page.to(next), TransitionError, "app is the end");
});

test("flow: a state's data comes with the step into it and goes with the step out", () => {
  const seen = [];
  const p = flow("source", PANEL_STEPS, "closed", (to, from, data) => seen.push([from, to, data]));
  assert.deepEqual(p.data, {});
  const d = p.to("open", { id: "flickr", config: { user: "ada" } });
  assert.equal(p.data, d);
  p.to("checking", p.data);
  p.to("checked", { ...p.data, preview: { ok: true } });
  assert.deepEqual(p.data.preview, { ok: true });
  p.to("open", { id: p.data.id, config: p.data.config });      // a field was edited: the preview is gone
  assert.equal(p.data.preview, undefined);
  p.to("closed");
  assert.deepEqual(p.data, {}, "closed owns nothing");
  assert.equal(p.can("checking"), false);
  assert.equal(p.is("closed", "open"), true);
  assert.deepEqual(seen.map(s => `${s[0]}>${s[1]}`), ["closed>open", "open>checking", "checking>checked", "checked>open", "open>closed"]);
});

test("pageStateOf: the state an answer of /api/intake means", () => {
  assert.equal(pageStateOf(answer({ chosen: false })), "place");
  assert.equal(pageStateOf(answer()), "idle");
  assert.equal(pageStateOf(answer({ jobs: ["done", "failed", "cancelled"] })), "idle");
  assert.equal(pageStateOf(answer({ jobs: ["done", "queued"] })), "watching");
  assert.equal(pageStateOf(answer({ jobs: ["running", "waiting"] })), "answering", "a job that needs you comes first");
  assert.equal(pageStateOf(answer({ mode: "starting", chosen: false })), "opening", "whatever else it says");
  assert.equal(pageStateOf(answer({ mode: "app", jobs: ["running"] })), "watching");
  assert.deepEqual(["queued", "running", "waiting", "done", "failed", "cancelled"].map(state => live({ state })), [true, true, true, false, false, false]);
});

test("the page asks the server only in the states that own the asking, often while something is coming in", () => {
  assert.deepEqual(ASKING, ["loading", "place", "idle", "watching", "answering"]);
  assert.ok(!ASKING.includes("opening") && !ASKING.includes("stuck") && !ASKING.includes("app"));
  assert.deepEqual(ASKING.map(askEvery), [8000, 8000, 8000, 1200, 1200]);
});

test("setupFlow: says whether the page asked where the library lives, and tells of each step", () => {
  const steps = [];
  const f = setupFlow({ onPage: (to, from) => steps.push(`${from}>${to}`), onPanel: (to, from) => steps.push(`panel ${from}>${to}`) });
  assert.equal(f.askedWhere, false);
  f.heard(answer({ chosen: false }));
  f.heard(answer({ chosen: false }));                      // the same state: no step
  assert.equal(f.askedWhere, true);
  f.heard(answer());
  f.panel.to("open", { id: "folder", config: {} });
  assert.deepEqual(steps, ["loading>place", "place>idle", "panel closed>open"]);
  assert.equal(f.askedWhere, true, "remembered after the place is chosen");
  assert.equal(setupFlow().heard(answer()), "idle");
});
