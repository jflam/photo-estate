// members-flow.js: the sysadmin's /members page. Its machine (every pair of states), the list loaded or
// failed, one change at a time, a refused change leaving the members as they were, and what each row offers.
import test from "node:test";
import assert from "node:assert/strict";

import { flow } from "../fsm.js";
import { membersFlow, actions, MEMBERS_STEPS, STATUS } from "../members-flow.js";
import { deferred, everyPair, settled } from "./helpers.js";

const member = (/** @type {number} */ id, /** @type {string} */ status, extra = {}) => ({
  id, name: `M${id}`, role: "member", status, upload_policy: "review", person_id: null, person: null, joined_at: null,
  logins: [{ login: `m${id}`, added_at: "2026-11-22T00:00:00Z" }], ...extra,
});
const listing = () => ({ members: [member(1, "active", { role: "sysadmin" }), member(2, "pending")], persons: [], me: 1,
  demo: true, roles: ["sysadmin", "member"], policies: ["everything", "review", "review_flagged"] });

test("the members machine: every legal step is taken, every other pair throws TransitionError", () => {
  assert.deepEqual(everyPair(MEMBERS_STEPS, (/** @type {any} */ s, /** @type {any} */ from) => flow("t", s, from), assert.throws),
    { legal: 8, refused: 17 });
});

test("load: ready owns the server's answer; a failure is failed, and asking again starts over", async () => {
  let fail = true;
  const f = membersFlow({ list: async () => { if (fail) throw new Error("The server did not answer"); return listing(); }, save: async () => ({ member: member(2, "active") }) });
  assert.equal(await f.load(), "failed");
  assert.equal(f.page.state, "failed");
  assert.equal(/** @type {Error} */ (f.page.data.error).message, "The server did not answer");
  fail = false;
  assert.equal(await f.load(), "ready");
  assert.deepEqual(f.page.data.members.map((/** @type {any} */ m) => m.id), [1, 2]);
});

test("change: saving owns the member being changed, then ready with the server's member in its place", async () => {
  /** @type {any[]} */ const sent = [];
  const answer = deferred();
  const seen = /** @type {string[]} */ ([]);
  const f = membersFlow({ list: async () => listing(), save: (id, fields) => { sent.push([id, fields]); return answer.promise; },
    onChange: to => seen.push(to) });
  await f.load();
  const before = f.page.data;
  const saving = f.change(2, { status: "active" });
  assert.equal(f.page.state, "saving");
  assert.equal(f.page.data.id, 2);
  assert.deepEqual(sent, [[2, { status: "active" }]]);
  const second = await f.change(1, { role: "member" });          // one change at a time
  assert.equal(second.ok, false);
  assert.equal(await f.load(), "busy", "not asked again while a change is being saved");
  assert.equal(sent.length, 1);
  answer.resolve({ member: member(2, "active", { joined_at: "2026-11-22T01:00:00Z" }) });
  const got = await saving;
  assert.equal(got.ok, true);
  assert.equal(f.page.state, "ready");
  assert.deepEqual(f.page.data.members.map((/** @type {any} */ m) => m.status), ["active", "active"]);
  assert.equal(f.page.data.members[0], before.members[0], "the other members are the same objects");
  assert.deepEqual(seen, ["loading", "ready", "saving", "ready"]);
});

test("change: a refusal goes back to ready with the members as they were, and says why", async () => {
  const f = membersFlow({ list: async () => listing(), save: async () => { throw new Error("the estate must keep an active sysadmin"); } });
  await f.load();
  const before = f.page.data;
  const got = await f.change(1, { status: "revoked" });
  assert.equal(got.ok, false);
  assert.match(String(/** @type {any} */ (got).error.message), /active sysadmin/);
  assert.equal(f.page.state, "ready");
  assert.equal(f.page.data, before);
});

test("an answer to a list asked for again is dropped", async () => {
  const first = deferred(), second = deferred();
  const answers = [first, second];
  const f = membersFlow({ list: () => /** @type {any} */ (answers.shift()).promise, save: async () => ({ member: member(1, "active") }) });
  const a = f.load(), b = f.load();
  second.resolve(listing());
  first.resolve({ ...listing(), members: [] });
  assert.equal(await b, "ready");
  await settled();
  assert.equal(String(await a), "Symbol(stale)");
  assert.equal(f.page.data.members.length, 2);
});

test("what each member's row offers, by status", () => {
  assert.deepEqual(actions({ status: "pending" }).map(a => [a.label, a.fields.status]), [["Let in", "active"], ["Turn away", "revoked"]]);
  assert.deepEqual(actions({ status: "active" }).map(a => [a.label, a.fields.status]), [["Turn off", "revoked"]]);
  assert.deepEqual(actions({ status: "revoked" }).map(a => [a.label, a.fields.status]), [["Let in again", "active"]]);
  assert.deepEqual(Object.keys(STATUS), ["pending", "active", "revoked"]);
});
