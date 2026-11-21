// setup-flow.js: the setup page's workflow, said out loud. setup.html is two things at once, each a
// strict state machine (fsm.js's flow(): a step that is not in the table throws), and what each state
// needs to know lives with the state: it is handed over at the step into it and gone at the step out.
//
// The page: where the server is in bringing photos in.
//
//   loading ──► place ──► idle ◄──► watching ◄──► answering          opening ──► app
//      └──────────┴────────┴───────────┴──────────────┴─────────────►   ▲ │
//                                                                  stuck ◄┘
//   loading    nothing heard from the server yet
//   place      no library yet: choosing where it lives (step 1)
//   idle       a library, and nothing coming in: choosing a source; the server is asked every 8 s
//   watching   a job is queued or running: asked every 1.2 s
//   answering  a job is waiting for the person (a sign-in, a code): asked every 1.2 s
//   opening    the first photos are in and the server is becoming the photo app (or stopped answering):
//              /api/info is asked until it answers, then the page goes to the library. If the setup
//              server answers again instead, the page goes back to where that says it is.
//   stuck      opening took too long: says so, with a button back to opening
//   app        the photo app answered: the page is leaving for it (nothing follows)
//   The states place..answering come from the server's answer (pageStateOf); the loop that asks belongs
//   to them and is stopped at the step out of them.
//
// The panel: the source being added (step 2).
//
//   closed ──► open ──► checking ──► checked ──► starting ──► closed
//   open       a source is chosen; data { id, config }: what has been typed into its fields
//   checking   its preview is being asked for; data { id, config }
//   checked    the preview said yes; data { id, config, preview }
//   starting   the job is being started; data { id, config, preview }
//   Typing in a field, or the preview saying no, goes back to open (the preview is no longer true of
//   what is typed); choosing another source goes to open with that source; Not now goes to closed.
//
//   setupFlow({ onPage, onPanel })   -> { page, panel, askedWhere }
//   pageStateOf(answer)              the page state an answer of /api/intake means
//   askEvery(state)                  ms between asks in that state
//   PAGE_STEPS, PANEL_STEPS, ASKING

import { flow } from "./fsm.js";

/** @typedef {"loading" | "place" | "idle" | "watching" | "answering" | "opening" | "stuck" | "app"} PageState */
/** @typedef {"closed" | "open" | "checking" | "checked" | "starting"} PanelState */

export const PAGE_STEPS = /** @type {Record<PageState, readonly PageState[]>} */ ({
  loading: ["place", "idle", "watching", "answering", "opening"],
  place: ["idle", "watching", "answering", "opening"],
  idle: ["watching", "answering", "opening"],
  watching: ["idle", "answering", "opening"],
  answering: ["idle", "watching", "opening"],
  opening: ["app", "stuck", "place", "idle", "watching", "answering"],
  stuck: ["opening"],
  app: [],
});

export const PANEL_STEPS = /** @type {Record<PanelState, readonly PanelState[]>} */ ({
  closed: ["open"],
  open: ["open", "checking", "closed"],
  checking: ["checked", "open", "closed"],
  checked: ["starting", "open", "closed"],
  starting: ["closed", "checked", "open"],
});

/** The states in which the page asks the setup server how things stand. */
export const ASKING = /** @type {readonly PageState[]} */ (["loading", "place", "idle", "watching", "answering"]);

const LIVE = ["queued", "running", "waiting"];

/** Whether a job is still going (queued, running, or waiting for the person). @param {{ state: string }} job */
export const live = job => LIVE.includes(job.state);

/**
 * The page state an answer of /api/intake means.
 * @param {{ mode: string, library: { chosen: boolean }, jobs: { state: string }[] }} answer
 * @returns {PageState}
 */
export function pageStateOf(answer) {
  if (answer.mode === "starting") return "opening";
  if (!answer.library.chosen) return "place";
  if (answer.jobs.some(j => j.state === "waiting")) return "answering";
  return answer.jobs.some(live) ? "watching" : "idle";
}

/** ms between asks: often while something is coming in, seldom otherwise. @param {PageState} state */
export const askEvery = state => state === "watching" || state === "answering" ? 1200 : 8000;

/**
 * The setup page's two machines.
 * @param {{ onPage?: (to: PageState, from: PageState) => void, onPanel?: (to: PanelState, from: PanelState) => void }} [options]
 */
export function setupFlow({ onPage, onPanel } = {}) {
  let askedWhere = false;
  const page = flow("setup", PAGE_STEPS, /** @type {PageState} */ ("loading"), (to, from) => {
    if (to === "place") askedWhere = true;
    onPage?.(to, from);
  });
  const panel = flow("source", PANEL_STEPS, /** @type {PanelState} */ ("closed"), (to, from) => onPanel?.(to, from));
  return {
    page, panel,
    /** Whether the page asked where the library should live (so the steps are numbered 1 and 2). */
    get askedWhere() { return askedWhere; },
    /**
     * Take the page to the state an answer of /api/intake means, if it is not there already.
     * @param {Parameters<typeof pageStateOf>[0]} answer
     * @returns {PageState}
     */
    heard(answer) {
      const next = pageStateOf(answer);
      if (next !== page.state) page.to(next);
      return next;
    },
  };
}
