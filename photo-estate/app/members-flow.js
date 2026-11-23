// members-flow.js: what members.html (/members, the sysadmin's page) does over time: the members as the
// server gave them, and one change to a member at a time. A strict machine whose states own their data
// (fsm.js).
//
//   idle ──► loading ──► ready ──► saving ──► ready
//               └──────► failed ──► loading
//   ready ──► loading (asked again)
//
//   ready    owns the server's answer: { members, persons, me, demo, roles, policies } (GET /api/members)
//   saving   owns the same, and { id }: the member being changed. The page shows that member as busy and
//            takes no other change until the server has answered; then ready again, with that member as
//            the server now has it, or as it was if the server refused (the page says why).
//   failed   owns { error }
//
//   membersFlow({ list, save })   -> { page, load(), change(id, fields) }
//   actions(member)               -> what a member's row offers: [{ label, fields, primary? }]
//   STATUS, MEMBERS_STEPS

import { flow, latest, STALE } from "./fsm.js";

/** @typedef {"idle" | "loading" | "ready" | "saving" | "failed"} MembersState */
/**
 * @typedef {object} Member   a member as GET /api/members lists it
 * @property {number} id
 * @property {string} name
 * @property {string} role               sysadmin | member
 * @property {string} status             pending | active | revoked
 * @property {string} upload_policy      everything | review | review_flagged
 * @property {number | null} person_id
 * @property {string | null} person      that person's name
 * @property {string | null} joined_at
 * @property {{ login: string, added_at: string }[]} logins
 */
/**
 * @typedef {object} Listing  GET /api/members
 * @property {Member[]} members
 * @property {{ id: number, name: string }[]} persons
 * @property {number | null} me
 * @property {boolean} demo
 * @property {string[]} roles
 * @property {string[]} policies
 */
/** @typedef {{ name?: string, role?: string, status?: string, upload_policy?: string, person_id?: number | null }} Fields */
/** @typedef {{ ok: true, member: Member } | { ok: false, error: unknown }} Saved */

export const MEMBERS_STEPS = /** @type {Record<MembersState, readonly MembersState[]>} */ ({
  idle: ["loading"],
  loading: ["loading", "ready", "failed"],
  ready: ["loading", "saving"],
  saving: ["ready"],
  failed: ["loading"],
});

/** How each status is said on the page. */
export const STATUS = /** @type {Record<string, string>} */ ({
  pending: "Asking to be let in",
  active: "In",
  revoked: "Turned off",
});

/**
 * What a member's row offers, by its status: letting in, turning away, turning off.
 * @param {Pick<Member, "status">} m
 * @returns {{ label: string, fields: Fields, primary?: boolean }[]}
 */
export function actions(m) {
  if (m.status === "pending") return [{ label: "Let in", fields: { status: "active" }, primary: true }, { label: "Turn away", fields: { status: "revoked" } }];
  if (m.status === "active") return [{ label: "Turn off", fields: { status: "revoked" } }];
  return [{ label: "Let in again", fields: { status: "active" } }];
}

/**
 * @param {{ list: (signal: AbortSignal) => Promise<Listing>, save: (id: number, fields: Fields) => Promise<{ member: Member }>,
 *   onChange?: (to: MembersState, from: MembersState) => void }} options
 *   `list`: GET /api/members; `save`: POST /api/members/<id>; `onChange`: told after every step (the page draws)
 */
export function membersFlow({ list, save, onChange }) {
  const page = flow("members", MEMBERS_STEPS, /** @type {MembersState} */ ("idle"), (to, from) => onChange?.(to, from));
  const asking = latest();
  return {
    page,
    /**
     * Ask for the members. Never rejects. Resolves to how it ended: "ready", "failed", STALE when asked
     * again before the answer came, or "busy" while a change is being saved (it is not asked then).
     * @returns {Promise<"ready" | "failed" | "busy" | typeof STALE>}
     */
    async load() {
      if (page.is("saving")) return "busy";
      page.to("loading");
      try {
        const d = await asking.run(signal => list(signal));
        if (d === STALE) return STALE;
        page.to("ready", d);
        return "ready";
      } catch (error) {
        if (page.is("loading")) page.to("failed", { error });
        return "failed";
      }
    },
    /**
     * Change one member (approve, revoke, a role, a policy, a person, a name). Only from ready: one change
     * at a time. Never rejects: resolves { ok: true, member } with the member as the server now has it, or
     * { ok: false, error } (refused, or not ready), with the members as they were.
     * @param {number} id
     * @param {Fields} fields
     * @returns {Promise<Saved>}
     */
    async change(id, fields) {
      if (!page.is("ready")) return { ok: false, error: new Error("Wait for the last change to be saved.") };
      const d = /** @type {Listing} */ (page.data);
      page.to("saving", { ...d, id });
      try {
        const { member } = await save(id, fields);
        page.to("ready", { ...d, members: d.members.map(m => (m.id === member.id ? member : m)) });
        return { ok: true, member };
      } catch (error) {
        page.to("ready", d);
        return { ok: false, error };
      }
    },
  };
}
