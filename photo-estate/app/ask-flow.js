// ask-flow.js: one item of the member's inbox that asks them something (a `question` item, opened as
// /inbox?ask=<group>): its questions as the server dealt them (GET /api/questions, stories.py), each answered
// (a story: story-flow.js tells it) or put away ("I don't know": POST /api/questions/<id>/dismiss), one at a
// time. A strict machine whose states own their data (fsm.js).
//
//   idle ──► loading ──► ready ──► dismissing ──► ready
//               └──────► failed ──► loading
//   ready ──► ready (a question answered, a transcript written down) | loading (asked again)
//
//   ready       owns { group, item, questions, answered }: the item's questions still waiting, and those answered
//               here, each { question, story } (the story as the server has it now, its transcript too).
//               item is null when the group is not (or no longer) in the member's inbox.
//   dismissing  owns the same, and { id }: the question being put away
//   failed      owns { error }
//
//   askFlow({ list, dismiss })   -> { page, load(), answered(qid, story), dismiss(qid), told(story) }
//   groupOf(item)                -> the group an item of GET /api/questions is (as its inbox item's ref says)
//   askedBy(question)            -> who asks, and whom: "Ada asks you", "The estate’s agent asks the family, for Ben"
//   heading(item)                -> the page's title for the item
//   ASK_STEPS

import { flow, latest, STALE } from "./fsm.js";

/** @typedef {"idle" | "loading" | "ready" | "dismissing" | "failed"} AskState */
/**
 * @typedef {object} Question   a question as GET /api/questions lists it
 * @property {number} id
 * @property {string} text
 * @property {string | null} why
 * @property {Record<string, any>[]} photos   rows as /api/search gives them
 * @property {string} asked_at
 * @property {{ id: number | null, name: string, kind: string }} asked_by
 * @property {string | null} for            the member an agent's run asked for
 * @property {"you" | "the family"} to
 */
/** @typedef {{ item: number, group_key: string | null, questions: Question[] }} Item */
/** @typedef {import("./story-flow.js").Story} Story */

export const ASK_STEPS = /** @type {Record<AskState, readonly AskState[]>} */ ({
  idle: ["loading"],
  loading: ["loading", "ready", "failed"],
  ready: ["ready", "loading", "dismissing"],
  dismissing: ["ready"],
  failed: ["loading"],
});

/**
 * The group an item is: its group_key, or for a question asked on its own "q<its id>" (stories.py's _group), as
 * the inbox item's ref (ask:<group>) and the address /inbox?ask=<group> name it.
 * @param {Pick<Item, "group_key" | "questions">} item
 * @returns {string}
 */
export const groupOf = item => item.group_key || `q${Math.min(...item.questions.map(q => q.id))}`;

/**
 * Who asks, and whom.
 * @param {Pick<Question, "asked_by" | "for" | "to">} q
 * @returns {string}
 */
export function askedBy(q) {
  const agent = q.asked_by?.kind === "agent";
  const who = agent ? "The estate’s agent" : q.asked_by?.name || "Someone";
  return `${who} asks ${q.to === "you" ? "you" : "the family"}${agent ? q.for ? `, for ${q.for}` : ", for the family" : ""}`;
}

/**
 * The title of the page for an item (null: it is not in the inbox).
 * @param {Pick<Item, "group_key" | "questions"> | null} item
 * @param {number} [left]   how many of its questions are still waiting
 */
export function heading(item, left = item?.questions.length || 0) {
  if (!item) return "Nothing to answer here";
  const n = item.questions.length;
  if (n === 1) return "A question for you";
  return `${n} questions${item.group_key ? ` about ${item.group_key}` : ""}${left < n ? `: ${left} left` : ""}`;
}

/**
 * @param {{ list: (signal: AbortSignal) => Promise<{ items: Item[] }>, dismiss: (qid: number) => Promise<unknown>,
 *   onChange?: (to: AskState, from: AskState) => void }} options
 */
export function askFlow({ list, dismiss, onChange }) {
  const page = flow("ask", ASK_STEPS, /** @type {AskState} */ ("idle"), (to, from) => onChange?.(to, from));
  const asking = latest();
  /** @typedef {{ group: string, item: Item | null, questions: Question[], answered: { question: Question, story: Story }[] }} Ready */
  return {
    page,
    /**
     * Ask for the questions of `group`. Never rejects: "ready", "failed", "busy" (one is being put away) or STALE.
     * The questions answered here stay, under their answers.
     * @param {string} group
     * @returns {Promise<"ready" | "failed" | "busy" | typeof STALE>}
     */
    async load(group) {
      if (page.is("dismissing")) return "busy";
      const before = page.is("ready") && page.data.group === group ? page.data.answered : [];
      page.to("loading");
      try {
        const d = await asking.run(signal => list(signal));
        if (d === STALE) return STALE;
        const item = d.items.find(x => groupOf(x) === group) || null;
        const done = new Set(before.map((/** @type {any} */ a) => a.question.id));
        page.to("ready", { group, item, questions: item ? item.questions.filter(q => !done.has(q.id)) : [], answered: before });
        return "ready";
      } catch (error) {
        if (page.is("loading")) page.to("failed", { error });
        return "failed";
      }
    },
    /**
     * A question was answered here: it moves under its answer.
     * @param {number} qid
     * @param {Story} story
     */
    answered(qid, story) {
      if (!page.is("ready")) return false;
      const d = /** @type {Ready} */ (page.data), q = d.questions.find(x => x.id === qid);
      if (!q) return false;
      page.to("ready", { ...d, questions: d.questions.filter(x => x !== q), answered: [...d.answered, { question: q, story }] });
      return true;
    },
    /**
     * A story answered here, as the server has it now (its transcript written down).
     * @param {Story} story
     */
    told(story) {
      if (!page.is("ready")) return false;
      const d = /** @type {Ready} */ (page.data);
      if (!d.answered.some(a => a.story.id === story.id)) return false;
      page.to("ready", { ...d, answered: d.answered.map(a => a.story.id === story.id ? { ...a, story } : a) });
      return true;
    },
    /**
     * "I don't know": put the question away. Only from ready, one at a time. Never rejects.
     * @param {number} qid
     * @returns {Promise<{ ok: true } | { ok: false, error: unknown }>}
     */
    async dismiss(qid) {
      if (!page.is("ready")) return { ok: false, error: new Error("Wait for the last one.") };
      const d = /** @type {Ready} */ (page.data);
      page.to("dismissing", { ...d, id: qid });
      try {
        await dismiss(qid);
        page.to("ready", { ...d, questions: d.questions.filter(q => q.id !== qid) });
        return { ok: true };
      } catch (error) {
        page.to("ready", d);
        return { ok: false, error };
      }
    },
  };
}
