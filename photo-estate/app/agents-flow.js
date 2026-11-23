// agents-flow.js: what agents.html (/agents, "Connect your agent") does over time: how to connect a member's own
// agent, and their agents as the server gave them (GET /api/agents: one per device it called /mcp from; members.py),
// each allowed or turned off one at a time. A strict machine whose states own their data (fsm.js). The page asks
// again now and then while it is looked at (a gentle refresh: no connection is held open), so an agent that has
// just connected shows up as it happens.
//
//   idle ──► loading ──► ready ──► deciding ──► ready
//               └──────► failed ──► loading
//   ready ──► loading (asked again)
//
//   ready     owns the server's answer: { bindings, url, here, me, demo, login }
//   deciding  owns the same, and { id }: the agent being allowed or turned off. The page shows it as busy and
//             takes no other until the server has answered; then ready again, with that agent as the server now
//             has it, or as it was if the server refused (the page says why: allowing it from its own device).
//   failed    owns { error }
//
//   agentsFlow({ list, decide })   -> { page, load(), decide(id, allow) }
//   says(binding, here, now?)      -> how one agent is shown: { state, line, offers, same }
//   command(listing) / config(listing)   what to run, or to put in another harness's configuration
//   REFRESH, AGENTS_STEPS

import { flow, latest, STALE } from "./fsm.js";

/** @typedef {"idle" | "loading" | "ready" | "deciding" | "failed"} AgentsState */
/**
 * @typedef {object} Binding   a member's own agent on one device, as GET /api/agents lists it
 * @property {number} id
 * @property {string} node                         what tells the device apart (the tailnet node, or its address)
 * @property {string} device                       its name, as the member is shown it
 * @property {"pending" | "approved" | "revoked"} status
 * @property {string} requested_at                 its first call
 * @property {string | null} decided_at
 * @property {string | null} seen_at               its last call, to the minute
 */
/**
 * @typedef {object} Listing   GET /api/agents
 * @property {Binding[]} bindings
 * @property {string} url                          /mcp, as this page reached the app
 * @property {{ node: string, device: string }} here   the device this page is open on
 * @property {string | null} me                    the member's name
 * @property {boolean} demo
 * @property {string | null} login                 (the demo only) the demo member's login, for its header
 */

export const AGENTS_STEPS = /** @type {Record<AgentsState, readonly AgentsState[]>} */ ({
  idle: ["loading"],
  loading: ["loading", "ready", "failed"],
  ready: ["loading", "deciding"],
  deciding: ["ready"],
  failed: ["loading"],
});

/** How often the page asks again while it is looked at (ms): often at first, then gently. */
export const REFRESH = { first: 4000, then: 20000, firstFor: 120000 };

/** The time of day, as the page says it: "3:42 PM". @param {string | null | undefined} at */
const clock = at => (at ? new Date(at).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" }) : "");
/** The day, as the page says it: "Nov 23". @param {string | null | undefined} at */
const day = at => (at ? new Date(at).toLocaleDateString("en-US", { month: "short", day: "numeric" }) : "");

/**
 * How one agent is shown: its state in words, a line about its last call, what the member can do, and whether
 * this page is open on the agent's own device (it cannot be allowed from there: anything there could be the agent).
 * @param {Binding} b
 * @param {{ node: string }} here
 * @param {Date} [now]
 * @returns {{ state: string, line: string, offers: { label: string, allow: boolean, primary?: boolean, disabled?: boolean }[], same: boolean }}
 */
export function says(b, here, now = new Date()) {
  const same = b.node === here.node;
  const seen = b.seen_at || b.requested_at;
  const when = seen && new Date(seen).toDateString() === now.toDateString() ? `at ${clock(seen)}` : `on ${day(seen)}`;
  if (b.status === "pending") {
    return { state: "Waiting for you", same,
      line: `Connected as you ${when}. Until you allow it, it can look but not change anything.`,
      offers: [{ label: "Allow it", allow: true, primary: true, disabled: same }, { label: "Turn it off", allow: false }] };
  }
  if (b.status === "approved") {
    return { state: "Allowed", same, line: `Last connected ${when}. It works as you: what it makes is yours.`,
      offers: [{ label: "Turn it off", allow: false }] };
  }
  return { state: "Turned off", same, line: `Turned off ${b.decided_at ? `on ${day(b.decided_at)}` : ""}. It is refused, whatever it asks.`.replace("  ", " "),
    offers: [{ label: "Allow it again", allow: true, disabled: same }] };
}

/** Quote a word for a shell, if it needs it. @param {string} s */
const sh = s => (/^[\w@%+=:,./-]+$/.test(s) ? s : `'${s.replace(/'/g, `'\\''`)}'`);

/**
 * The headers the demo needs to say who is calling, from which device (only with --demo: the real app reads the
 * tailnet's own).
 * @param {Pick<Listing, "demo" | "login" | "me">} d
 * @returns {Record<string, string>}
 */
function demoHeaders(d) {
  if (!d.demo) return {};
  const who = d.login || (d.me || "").toLowerCase();
  return { "X-Demo-Member": who, "X-Demo-Device": `${who}-laptop` };
}

/**
 * What to run to add the estate to Claude Code.
 * @param {Pick<Listing, "url" | "demo" | "login" | "me">} d
 * @returns {string}
 */
export function command(d) {
  const h = Object.entries(demoHeaders(d)).map(([k, v]) => ` --header ${sh(`${k}: ${v}`)}`).join("");
  return `claude mcp add --transport http${h} photo-estate ${sh(d.url)}`;
}

/**
 * The same, as the configuration another harness reads (an mcpServers entry, as .mcp.json has it).
 * @param {Pick<Listing, "url" | "demo" | "login" | "me">} d
 * @returns {string}
 */
export function config(d) {
  const headers = demoHeaders(d);
  return JSON.stringify({ mcpServers: { "photo-estate": { type: "http", url: d.url, ...(d.demo ? { headers } : {}) } } }, null, 2);
}

/**
 * @param {{ list: (signal: AbortSignal) => Promise<Listing>, decide: (id: number, allow: boolean) => Promise<{ binding: Binding }>,
 *   onChange?: (to: AgentsState, from: AgentsState) => void }} options
 *   `list`: GET /api/agents; `decide`: POST /api/agents/<id> {allow}; `onChange`: told after every step (the page draws)
 */
export function agentsFlow({ list, decide, onChange }) {
  const page = flow("agents", AGENTS_STEPS, /** @type {AgentsState} */ ("idle"), (to, from) => onChange?.(to, from));
  const asking = latest();
  return {
    page,
    /**
     * Ask for the agents. Never rejects: "ready", "failed", STALE when asked again before the answer came, or
     * "busy" while one is being decided (not asked then). A refresh that fails after a good answer keeps it.
     * @returns {Promise<"ready" | "failed" | "busy" | typeof STALE>}
     */
    async load() {
      if (page.is("deciding")) return "busy";
      const had = page.is("ready") ? page.data : null;
      page.to("loading", had ? { ...had } : {});
      try {
        const d = await asking.run(signal => list(signal));
        if (d === STALE) return STALE;
        page.to("ready", d);
        return "ready";
      } catch (error) {
        if (!page.is("loading")) return "failed";
        if (had) page.to("ready", had);
        else page.to("failed", { error });
        return "failed";
      }
    },
    /**
     * Allow one agent, or turn it off. Only from ready, one at a time. Never rejects: { ok: true } with the agent
     * as the server has it now, or { ok: false, error } with the agents as they were.
     * @param {number} id
     * @param {boolean} allow
     * @returns {Promise<{ ok: true } | { ok: false, error: unknown }>}
     */
    async decide(id, allow) {
      if (!page.is("ready")) return { ok: false, error: new Error("Wait for the last one to be done.") };
      const d = /** @type {Listing} */ (page.data);
      page.to("deciding", { ...d, id });
      try {
        const got = await decide(id, allow);
        page.to("ready", { ...d, bindings: d.bindings.map(b => (b.id === id ? got.binding : b)) });
        return { ok: true };
      } catch (error) {
        page.to("ready", d);
        return { ok: false, error };
      }
    },
  };
}
