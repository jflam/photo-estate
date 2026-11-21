// compare-flow.js: one run of the model comparison, as compare.html goes through it (compare.py is the
// server's side). A run asks a fixed list of searches; for each, the two models' photos are shown in two
// columns in random order, with no names, and the person says which is better. At the end the server
// writes the report and lets the second model go.
//
//   compareRun({ api, view, ready, random })  -> { start(fresh), vote(which), finish(early), show(id), state, data }
//   RUN_STEPS                                 the steps of a run
//   winnerOf(which, order)                    "left" | "right" | "same" | "skip" -> "a" | "b" | "same" | "skip"
//
// The states, and what each holds:
//   idle        nothing
//   starting    the run is being asked for (POST /api/compare/run: the one under way, or a new one)
//   waiting     { run }: the second model is loading (ready() resolves when it is, or says it is not)
//   asking      { run, term, i }: this search's two rankings are being fetched
//   judging     { run, term, i, body, order }: on the page; order is which model is on the left, e.g. ["b", "a"]
//   saving      the same, while the vote is sent; a vote that could not be saved goes back to judging
//   finishing   { run }: the report is being made
//   report      { report }: a run's report (this one's, or an older one's through show())
//   failed      { message }: start() tries again
//
// Every wait is followed by a check that the state entered before it is still the one held, so a second
// press, or an answer that comes late, changes nothing.

import { flow } from "./fsm.js";
import { ApiError } from "./api.js";

/** @typedef {"idle"|"starting"|"waiting"|"asking"|"judging"|"saving"|"finishing"|"report"|"failed"} RunState */

/** @type {Record<RunState, readonly RunState[]>} */
export const RUN_STEPS = {
  idle: ["starting", "report", "failed"],
  starting: ["waiting", "failed"],
  waiting: ["asking", "finishing", "failed"],
  asking: ["judging", "starting", "failed"],
  judging: ["saving", "finishing"],
  saving: ["asking", "finishing", "judging"],
  finishing: ["report", "failed"],
  report: ["starting", "report", "failed"],
  failed: ["starting", "report", "failed"],
};

/**
 * @param {"left"|"right"|"same"|"skip"} which  what the person pressed
 * @param {readonly string[]} order             the model on the left, then the one on the right
 * @returns {"a"|"b"|"same"|"skip"}
 */
export function winnerOf(which, order) {
  return /** @type {any} */ (which === "left" ? order[0] : which === "right" ? order[1] : which);
}

/**
 * @typedef {object} RunView
 * @property {(run: any) => void} waiting                 the run is known; its model is loading
 * @property {(term: string, i: number, n: number) => void} asking
 * @property {(q: { term: string, i: number, n: number, body: any, order: string[] }) => void} judge
 * @property {(text: string) => void} note                beside what is shown: a vote that was not saved
 * @property {(report: any) => void} report
 * @property {(message: string) => void} failed
 */

/**
 * @param {object} o
 * @param {(path: string, body?: any) => Promise<any>} o.api
 * @param {RunView} o.view
 * @param {() => Promise<{ state: string, error?: any }>} o.ready   resolves { state: "done" } once the second model is loaded
 * @param {() => number} [o.random]
 */
export function compareRun({ api, view, ready, random = Math.random }) {
  const m = flow("compare run", RUN_STEPS, /** @type {RunState} */ ("idle"));
  const said = (/** @type {any} */ e) => (e && e.message) || "Something went wrong";
  const fail = (/** @type {any} */ e) => { m.to("failed", { message: said(e) }); view.failed(said(e)); };

  /** Ask for the run (or a new one with `fresh`), wait for its model, and go to its first unanswered search. */
  async function start(fresh = false, again = 0) {
    if (!m.can("starting")) return;
    const mine = m.to("starting", { again });
    let run;
    try { run = await api("/api/compare/run", fresh ? { fresh: true } : {}); }
    catch (e) { if (m.data === mine) fail(e); return; }
    if (m.data !== mine) return;
    const held = m.to("waiting", { run, again });
    view.waiting(run);
    const end = await ready();
    if (m.data !== held) return;
    if (end.state !== "done") return fail(end.error || new Error("The second model has not loaded. Try again."));
    await next(run, again);
  }

  /** @param {any} run @param {number} [again] */
  async function next(run, again = 0) {
    const i = run.terms.findIndex((/** @type {string} */ t) => !(t in run.answers));
    if (i < 0) return finish();
    const term = run.terms[i], n = run.terms.length;
    const mine = m.to("asking", { run, term, i });
    view.asking(term, i, n);
    let body;
    try { body = await api(`/api/compare?k=${run.k}&q=${encodeURIComponent(term)}`); }
    catch (e) {
      if (m.data !== mine) return;
      // 503: the model was let go while the run sat (the server keeps it only while it is used): load it again, once
      if (e instanceof ApiError && e.status === 503 && again < 2) return start(false, again + 1);
      return fail(e);
    }
    if (m.data !== mine) return;
    const order = random() < 0.5 ? ["a", "b"] : ["b", "a"];
    m.to("judging", { run, term, i, body, order });
    view.judge({ term, i, n, body, order });
  }

  /** @param {"left"|"right"|"same"|"skip"} which */
  async function vote(which) {
    if (!m.is("judging")) return;                      // a second press, or a press with nothing to judge
    const q = m.data, { run, term, body, order } = q;
    const winner = winnerOf(which, order);
    const mine = m.to("saving", q);
    try {
      await api("/api/compare/vote", { run: run.id, q: term, winner, left: order[0], k: body.a.results.length,
                                       overlap: body.overlap, a_ms: body.a.text_ms, b_ms: body.b.text_ms });
    } catch (e) {
      if (m.data !== mine) return;
      m.to("judging", q);
      view.note(`That was not saved (${said(e)}). Press again.`);
      return;
    }
    if (m.data !== mine) return;
    run.answers[term] = winner;
    await next(run);
  }

  /** End the run and show its report. `early`: with searches still unanswered. */
  async function finish(early = false) {
    if (!m.can("finishing")) return;
    const run = m.data.run;
    const mine = m.to("finishing", { run });
    let rep;
    try { rep = await api("/api/compare/run/finish", { run: run.id, early }); }
    catch (e) { if (m.data === mine) fail(e); return; }
    if (m.data !== mine) return;
    m.to("report", { report: rep });
    view.report(rep);
  }

  /** An older run's report. @param {string} id */
  async function show(id) {
    if (!m.can("report")) return;
    try {
      const rep = await api(`/api/compare/report?run=${encodeURIComponent(id)}`);
      if (!m.can("report")) return;
      m.to("report", { report: rep });
      view.report(rep);
    } catch (e) { if (m.can("failed")) fail(e); }
  }

  return { start: (fresh = false) => start(fresh), vote, finish, show, get state() { return m.state; }, get data() { return m.data; } };
}
