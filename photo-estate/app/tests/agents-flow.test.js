// agents-flow.js: the member's /agents page ("Connect your agent"). Its machine (every pair of states), the agents
// loaded, a refresh that fails keeping what was there, one agent allowed or turned off at a time, a refusal leaving
// them as they were, how each state is said (and that an agent cannot be allowed from its own device), and the
// command and configuration, with the demo's headers only on the demo.
import test from "node:test";
import assert from "node:assert/strict";

import { flow } from "../fsm.js";
import { agentsFlow, says, command, config, AGENTS_STEPS } from "../agents-flow.js";
import { deferred, everyPair } from "./helpers.js";

const NOW = new Date("2026-11-23T15:50:00");
const at = (/** @type {string} */ hm) => new Date(`2026-11-23T${hm}:00`).toISOString();
const binding = (/** @type {number} */ id, status = "pending", node = `node:${id}`) => /** @type {any} */ ({
  id, node, device: `laptop-${id}`, status, requested_at: at("15:42"), decided_at: status === "pending" ? null : at("15:45"), seen_at: at("15:42"),
});
const listing = () => ({ bindings: [binding(2), binding(1, "approved")], url: "https://mini.example.ts.net:8766/mcp",
  here: { node: "node:phone", device: "phone" }, me: "Ben", demo: false, login: null });

test("the agents machine: every legal step is taken, every other pair throws TransitionError", () => {
  assert.deepEqual(everyPair(AGENTS_STEPS, (/** @type {any} */ s, /** @type {any} */ from) => flow("t", s, from), assert.throws),
    { legal: 8, refused: 17 });
});

test("load: ready owns the answer; a first failure is failed; a refresh that fails keeps what was shown", async () => {
  let fail = true;
  const f = agentsFlow({ list: async () => { if (fail) throw new Error("no answer"); return listing(); }, decide: async () => ({ binding: binding(2) }) });
  assert.equal(await f.load(), "failed");
  assert.equal(f.page.state, "failed");
  fail = false;
  assert.equal(await f.load(), "ready");
  const had = f.page.data;
  fail = true;
  assert.equal(await f.load(), "failed");
  assert.equal(f.page.state, "ready", "a gentle refresh that failed is not a failed page");
  assert.equal(f.page.data, had);
});

test("decide: deciding owns the agent being decided; then ready with it as the server has it; a refusal keeps them", async () => {
  const answer = deferred();
  /** @type {[number, boolean][]} */ const sent = [];
  const f = agentsFlow({ list: async () => listing(), decide: (id, allow) => { sent.push([id, allow]); return answer.promise; } });
  await f.load();
  const going = f.decide(2, true);
  assert.equal(f.page.state, "deciding");
  assert.equal(f.page.data.id, 2);
  assert.equal((await f.decide(1, false)).ok, false, "one at a time");
  assert.equal(await f.load(), "busy");
  answer.resolve({ binding: binding(2, "approved") });
  assert.equal((await going).ok, true);
  assert.deepEqual(f.page.data.bindings.map((/** @type {any} */ b) => b.status), ["approved", "approved"]);
  assert.deepEqual(sent, [[2, true]]);

  const g = agentsFlow({ list: async () => listing(), decide: async () => { throw new Error("allow it from another device"); } });
  await g.load();
  const before = g.page.data;
  const got = await g.decide(2, true);
  assert.equal(got.ok, false);
  assert.equal(/** @type {any} */ (got).error.message, "allow it from another device");
  assert.equal(g.page.state, "ready");
  assert.equal(g.page.data, before);
});

test("says: each state in words, with what can be done; never allowed from the agent's own device", () => {
  const here = { node: "node:phone" };
  const p = says(binding(2), here, NOW);
  assert.equal(p.state, "Waiting for you");
  assert.match(p.line, /^Connected as you at 3:42\sPM\. Until you allow it, it can look but not change anything\.$/);
  assert.deepEqual(p.offers.map(o => [o.label, o.allow, !!o.disabled]), [["Allow it", true, false], ["Turn it off", false, false]]);
  const mine = says(binding(2, "pending", "node:phone"), here, NOW);
  assert.equal(mine.same, true);
  assert.equal(mine.offers[0].disabled, true, "allowing it from its own device is not offered");
  assert.equal(mine.offers[1].disabled, undefined, "turning it off is, from anywhere");
  assert.deepEqual(says(binding(1, "approved"), here, NOW).offers.map(o => o.label), ["Turn it off"]);
  const off = says(binding(3, "revoked"), here, NOW);
  assert.equal(off.state, "Turned off");
  assert.deepEqual(off.offers.map(o => [o.label, o.allow]), [["Allow it again", true]]);
  assert.match(says(binding(1, "approved"), here, new Date("2026-11-25T10:00:00")).line, /on Nov 23/, "another day: the day");
});

test("command and config: the address as given; the demo's headers on the demo only", () => {
  const d = listing();
  assert.equal(command(d), "claude mcp add --transport http photo-estate https://mini.example.ts.net:8766/mcp");
  assert.deepEqual(JSON.parse(config(d)), { mcpServers: { "photo-estate": { type: "http", url: d.url } } });
  const demo = { url: "http://127.0.0.1:8770/mcp", demo: true, login: "ben", me: "Ben" };
  assert.equal(command(demo), "claude mcp add --transport http --header 'X-Demo-Member: ben' --header 'X-Demo-Device: ben-laptop' photo-estate http://127.0.0.1:8770/mcp");
  assert.deepEqual(JSON.parse(config(demo)).mcpServers["photo-estate"].headers, { "X-Demo-Member": "ben", "X-Demo-Device": "ben-laptop" });
  assert.match(command({ ...demo, login: null, me: "Ada" }), /X-Demo-Member: ada/, "the demo's sysadmin, by name");
});
