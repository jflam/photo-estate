"""listeners.py — what the estate does by itself when something happens (multiplayer phase 7; standard library
only). Each listener reads the journal (journal.since) from its cursor (listener_cursors), and acts; a rule decides
first, cheaply, from the tables, and wakes the model (a run of the estate's agent, tasks.py) only when it says yes.
Delivery is at least once: a cursor moves past an entry only after its handler has done its work, so after a crash
an entry may be handled again, and every handler checks first for what it would make (a run started for that entry
and that series or story, a request already journaled) and makes nothing twice. One thread of the app runs them
(PhotoApp.listen, every LISTEN_EVERY seconds and when woken by a reload or a story).

  Env                     what a listener is given (PhotoApp.listen_env makes it): find, loaded, in_catalog,
                          refused, submit
  WAIT                    a handler's answer for "not yet": the cursor stays, and the entry is tried next time
  series(con, entry, env) the first listener: keeps series up to date
  stories(con, entry, env) an answer to a question the agent asked, and wanted the answer to, starts a new run
  LISTENERS               (name, handler), in the order they run
  begin(con)              a listener with no cursor yet starts at the journal's end (the app's first start with
                          this code: nothing from before is acted on)
  run(con, env, limit=)   one pass of every listener -> {name: entries handled}

**What a series is, in the data** (changes.py): a list album (op view, kind list) with a search it grows from,
`change_sets.grows_from`. It holds chosen photos, is added to and never worked out again, so what its owner took
out stays out. A live album is not one: it *is* its search, and new photos are in it as soon as they are in the
catalog, with nothing for a listener to add. A **family series** is one whose series lens is maintained by the
estate's agent (`lenses.maintained_by`, set when the family archivist makes the album); any other is its owner's
own series.

**The series listener**, on `ingest.finished` (estate.py, whose data now carries `assets`: the rowids the run
added). It waits (WAIT) until the catalog being served was read after the entry (env.loaded), since a new photo is
searchable only then; the new photos are those rows that are in it (a photo with no vector is in no view). For each
series, it runs the series' search as its owner, over the whole library as the app ranks it (a search's floor is
relative to its best match, so it cannot be run over the new photos alone and give the same answer), and keeps the
new photos it finds that the album does not hold: one search per series per ingest, no model unless one matches.
  - a family series: a run of the estate's agent acting for no one (started_by journal:<entry>/album:<id>),
    which looks at them and adds those that belong (amend_album), directly;
  - a member's own: series.update_requested {ingest, photos, more?} (no actor, acting for the owner; at most
    MAX_NAMED photo ids) and a series_update inbox item for the owner, who adds them, or none, from the inbox
    (GET/POST /api/albums/<id>/candidates). Their own agent picking the entry up is phase 8.

**The stories listener**, on `story.told` (when it has text) and `story.transcribed`: if the story answers a
question that the estate's agent asked with follow_up, and the member that run acted for (or, for no one, the
archivist) may read the story, a new run acting for the same member is started with the question, the answer and
the photos (started_by journal:<entry>/story:<id>; once per story).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Callable

import journal
import member_inbox
import stories as stories_

MAX_NAMED = 200          # photo ids one series.update_requested entry, or one run's request, names at most
WAIT = object()


@dataclass(frozen=True)
class Env:
    """What the listeners are given. find(search, member) -> ids the search finds as that member sees the library,
    best first; loaded() -> the newest journal id the catalog being served was read after; in_catalog(sha);
    refused(sha, member) (stories.Refused); submit(prompt, acting_for, started_by) -> task id or None (no agent here)."""
    find: Callable[[dict, int | None], list[str]]
    loaded: Callable[[], int]
    in_catalog: Callable[[str], bool]
    refused: Callable[[str, int | None], bool]
    submit: Callable[[str, int | None, str], int | None]


def _started(con, by: str) -> bool:
    return con.execute("SELECT 1 FROM agent_tasks WHERE started_by=? LIMIT 1", (by,)).fetchone() is not None


def _owner(con, owner: int | None) -> int | None:
    if owner is not None:
        return owner
    row = con.execute("SELECT id FROM members WHERE role='sysadmin' AND status='active' ORDER BY id LIMIT 1").fetchone()
    return row[0] if row else None


def series(con, e: dict, env: Env):
    """The series listener (the module docstring)."""
    span = (e.get("data") or {}).get("assets") if e["kind"] == "ingest.finished" else None
    if not span:
        return None
    if e["id"] > env.loaded():
        return WAIT
    new = [s for s, in con.execute("SELECT sha256 FROM assets WHERE rowid > ? AND rowid <= ? ORDER BY rowid", span)
           if env.in_catalog(s)]
    if not new:
        return None
    fresh = set(new)
    for sid, title, grows, owner, keeper in con.execute(
            "SELECT s.id, s.title, s.grows_from, s.owner, l.maintained_by FROM change_sets s LEFT JOIN lenses l"
            " ON l.kind='series' AND l.set_id=s.id WHERE s.grows_from IS NOT NULL AND s.op='view' AND s.kind='list'"
            " AND s.status IS NOT 'discarded' ORDER BY s.id").fetchall():
        owner = _owner(con, owner)
        try:
            search = json.loads(grows)
            found = env.find(search, owner)
        except (ValueError, KeyError, TypeError):   # a search that no longer makes sense (a person renamed): nothing
            continue
        have = {x for x, in con.execute("SELECT sha256 FROM change_items WHERE set_id=?", (sid,))}
        got = [x for x in found if x in fresh and x not in have]
        if not got:
            continue
        what = f"“{title}”"
        if keeper is not None:                      # a family series: the estate's agent adds what belongs
            by = f"journal:{e['id']}/album:{sid}"
            if not _started(con, by):
                env.submit(_series_prompt(sid, title, search, got), None, by)
            continue
        if con.execute("SELECT 1 FROM journal WHERE kind='series.update_requested' AND subject=?"
                       " AND json_extract(data, '$.ingest')=?", (f"album:{sid}", e["id"])).fetchone():
            continue                                # handled already (at least once)
        with con:      # the item first, so the entry is never older than it (Changes.candidates reads from it on)
            # The write lock, and the album read again: one made no series while its search ran (given no search, put
            # up for hiding or away) is waited for by nothing (specs/ChangeSet.tla).
            if not con.in_transaction:
                con.execute("BEGIN IMMEDIATE")
            if not con.execute("SELECT 1 FROM change_sets WHERE id=? AND grows_from IS NOT NULL AND op='view' AND kind='list'"
                               " AND status IS NOT 'discarded'", (sid,)).fetchone():
                continue
            n = len(_named(con, sid, owner) | set(got[:MAX_NAMED]))
            member_inbox.put(con, [owner], "series_update", f"album:{sid}",
                             f"{n} new photo{'' if n == 1 else 's'} may belong in your series {what}")
            journal.append(con, None, "series.update_requested", f"album:{sid}",
                           {"ingest": e["id"], "photos": got[:MAX_NAMED], **({"more": len(got) - MAX_NAMED}
                                                                             if len(got) > MAX_NAMED else {})}, owner)
    return None


def _named(con, sid: int, owner: int | None) -> set[str]:
    """The photos the requests for a series name since its series_update item was opened (none open: none)."""
    row = con.execute("SELECT created_at FROM inbox_items WHERE member_id IS ? AND kind='series_update' AND ref=?"
                      " AND state='open'", (owner, f"album:{sid}")).fetchone()
    out: set[str] = set()
    if row is None:
        return out
    for data, in con.execute("SELECT data FROM journal WHERE kind='series.update_requested' AND subject=? AND at >= ?",
                             (f"album:{sid}", row[0])):
        out |= set((json.loads(data) or {}).get("photos") or [])
    return out


def _series_prompt(sid: int, title: str, search: dict, got: list[str]) -> str:
    words = f" (it grows from the search {json.dumps(search, ensure_ascii=False)})"
    more = f" There are {len(got) - MAX_NAMED} more; these are the best matches." if len(got) > MAX_NAMED else ""
    return (f"The family series “{title}” (album {sid}) is yours to keep up to date. {len(got)} new photos match it{words}."
            f"{more} Look at each (get_photos; get_album and similar_photos to compare with what it holds) and add those"
            f" that belong with amend_album {{\"album\": {sid}, \"add_ids\": [...], \"reason\": \"...\"}}. Leave out the"
            f" rest. The new photos: {', '.join(got[:MAX_NAMED])}")


def stories(con, e: dict, env: Env):
    """The stories listener (the module docstring)."""
    if e["kind"] not in ("story.told", "story.transcribed") or not str(e["subject"]).startswith("story:"):
        return None
    if e["kind"] == "story.told" and not (e.get("data") or {}).get("text"):
        return None                                 # a recording alone: woken when it is written out
    sid = int(e["subject"].partition(":")[2])
    row = con.execute("SELECT s.member_id, s.text, s.transcript, s.told_at, q.id, q.text, q.why, q.acting_for,"
                      " q.follow_up, m.kind, q.created_at FROM stories s JOIN questions q ON q.id=s.question_id"
                      " LEFT JOIN members m ON m.id=q.asked_by WHERE s.id=?", (sid,)).fetchone()
    if row is None:
        return None
    teller, text, transcript, at, qid, qtext, why, af, follow, kind, asked = row
    if not follow or kind != "agent":
        return None
    if con.execute("SELECT 1 FROM agent_tasks WHERE started_by LIKE ? LIMIT 1", (f"journal:%/story:{sid}",)).fetchone():
        return None                                 # this story has woken a run already
    agent = con.execute("SELECT id FROM members WHERE kind='agent' ORDER BY id LIMIT 1").fetchone()
    viewer = af if af is not None else (agent[0] if agent else None)
    story = stories_.story(con, sid, viewer, env.refused)
    if story is None:                               # not theirs to read (a photo of it is private to someone now)
        return None
    name = story["told_by"]["name"]
    said = " ".join(x for x in (text, f"(spoken, as transcribed:) {transcript}" if transcript else None) if x)
    prompt = (f"{name} has answered a question you asked on {(asked or '')[:10]} (question {qid}): “{qtext}”"
              + (f" You asked because: {why}" if why else "") +
              f"\n\nTheir answer, a story told on {at[:10]} (story {sid}), in their words: “{said}”\n\n"
              f"The photos: {', '.join(story['photos'])}. Act on it as you meant to when you asked. It is {name}'s"
              f" story: quote it, never change it.")
    env.submit(prompt, af, f"journal:{e['id']}/story:{sid}")
    return None


LISTENERS: tuple[tuple[str, Callable], ...] = (("series", series), ("stories", stories))


def cursor(con, name: str) -> int | None:
    row = con.execute("SELECT journal_id FROM listener_cursors WHERE name=?", (name,)).fetchone()
    return row[0] if row else None


def begin(con) -> None:
    """Every listener with no cursor yet starts at the end of the journal (in its own transaction)."""
    end = con.execute("SELECT coalesce(max(id), 0) FROM journal").fetchone()[0]
    with con:
        for name, _ in LISTENERS:
            con.execute("INSERT OR IGNORE INTO listener_cursors(name, journal_id) VALUES (?,?)", (name, end))


def run(con: sqlite3.Connection, env: Env, limit: int = 500) -> dict[str, int]:
    """One pass: each listener handles the entries after its cursor, oldest first, at most `limit`, and its cursor
    moves past each one handled (a handler's WAIT stops that listener here until the next pass)."""
    out = {}
    for name, handle in LISTENERS:
        at, n = cursor(con, name) or 0, 0
        for e in journal.since(con, at, limit):
            if handle(con, e, env) is WAIT:
                break
            with con:
                con.execute("INSERT INTO listener_cursors(name, journal_id) VALUES (?,?) ON CONFLICT(name) DO UPDATE"
                            " SET journal_id=excluded.journal_id", (name, e["id"]))
            n += 1
        out[name] = n
    return out
