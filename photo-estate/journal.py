"""journal.py — what happened in the estate, and who did it: one append-only log (standard library only).

  append(con, actor, kind, subject, data=None, acting_for=None) -> id   one entry, in the caller's transaction
  since(con, id=0, limit=1000) -> [dict]                                the entries after `id`, oldest first
  by_agent(binding)                     a context: every entry appended in it (in this thread) is a member's own
                                        agent's, and carries {"binding": <id>} in its data (estate_mcp.py)
  for_member(con, member, cursor, limit, refused=, readable=, loaded=) -> {entries, next_cursor, more}
                                        what a member may know of the journal after `cursor`: the MCP tool
                                        journal_since (the last column of the kinds below)

The table is the members area's `journal` (core/schema.py); triggers refuse any UPDATE or DELETE of it, so
undoing something is a new entry, never a changed one. The state tables (change_sets, hidden, favorites,
faces, ...) stay the truth for what the app shows; the journal is the history beside them. One entry per
action, not per photo: applying a set that hides 10,000 photos is one entry naming the set, and a write
that changes nothing appends nothing. `append` never commits: the entry lands with the change it records,
or not at all. The listeners (listeners.py) read it with `since`, each from its own cursor.

`actor` is members.id of whoever did it: the member a request is for (PhotoApp.who), so a request from
this computer itself is the first sysadmin; NULL when there is no member to name (no sysadmin yet, or the
app itself, as when a task's run ends or a listener writes). `acting_for` is the member an agent's run works
for: every write a run of the estate's agent makes through /mcp names the agent (members.agent) as the actor,
acting for the member the task is for (agent_tasks.acting_for; NULL for the family archivist; estate_mcp.py).
Entries written before phase 7 by a task's agent have a NULL actor. A member's own agent (multiplayer phase 8,
members.py: an approved binding) writes as its member: the actor is the member, and the entry's data carries
{"binding": <agent_bindings.id>}, which says "by Alice's agent on alice-macbook" (by_agent). `data` is
JSON, whatever else the kind needs, small: counts and titles, never a list of every photo.

**Who may read what** (for_member, the MCP tool journal_since; multiplayer phase 8). The journal is the whole
estate's history, so a member's agent is given only entries about what that member may know of: their own doings
and things, published lenses and series, stories they may read, and new photos they may be shown. A kind not
listed as returned is never returned; one whose subject is a photo is returned only while the member may be shown
it (refused). "Theirs" below: the entry's actor or acting_for is the member. The last column of the list says who
gets each kind:

  member.*                     no one (who is let in is the sysadmin's business; the member's own role is in /api/me)
  album.* (all of them)        the album's owner, with its data; and, for an album that is a published series now,
                               everyone, the kinds album.created, .items, .renamed, .query, .grows, .published only,
                               with no data (counts could include photos private to its owner)
  favorite.set                 the member whose favourites they are (data.member, else the actor); a photo subject
                               only while they may be shown it
  lens.created, .published, .unpublished, .proposed, .accepted, .declined   the lens's owner, and everyone while
                               the lens is published (titles only)
  lens.subscribed, .unsubscribed  theirs only (another member's subscriptions are theirs)
  photo.shown, .unshown        theirs, while they may be shown the photo
  lenses.migrated, sharers.migrated   no one
  faces.labeled, person.changed  no one (face ids, of photos private to someone among them; names reach an agent
                               through get_photos)
  task.submitted, task.finished  no one (a run is the estate's agent's; a personal agent submits none)
  ingest.finished              everyone, its data replaced by the new photos the member may be shown
                               ({photos: at most 200, new, more?}); left out when there are none. Not returned
                               until the catalog being served was read after it (loaded): a new photo is
                               searchable only then, so the page stops before it (waiting)
  photo.purged                 no one
  photo.held                   theirs (acting for the sharer)
  photo.withdrawn, .release_set, .released   theirs
  photo.reviewed               no one (the review's own bookkeeping: what it kept is private as before)
  tombstones.changed           no one (the phone's opt-outs)
  series.update_requested      theirs (acting for the series' owner), its photos those they may be shown, and
                               `waiting`: whether their series_update item is still open
  question.asked               theirs (they asked, or the estate's agent asked acting for them)
  question.dismissed           theirs
  story.told, story.transcribed  whoever may read the story (stories.story), with its photos
  inbox.dismissed              theirs
  binding.requested, .approved, .revoked   theirs

Kinds, with their subject and where each is appended:

  member.added, member.requested, member.changed   member:<id>    members.py
  album.created                    album:<id>   changes.py: an album or change set made {title, op, kind, added}
  album.items                      album:<id>   photos added or taken out {added, removed}
  album.decided                    album:<id>   marks on the review page {decision, decided}
  album.applied                    album:<id>   {hid, showed, lens, through, lens_made?}: lens, the filter it hides
                                                for (lenses.py); lens_made when this apply made the member's own
  album.reverted                   album:<id>   {showed}
  album.committed, album.reopened  album:<id>
  album.hidden, album.viewed       album:<id>   put up for hiding, or a plain album again {added}
  album.renamed                    album:<id>   {title, prompt}: [before, after] of what changed
  album.query                      album:<id>   a live album's search {query}
  album.discarded                  album:<id>   marked discarded: its rows are kept, it is in no listing
  favorite.set                     <sha> or album:<id>   photo_app.set_favorites {fav, photos}: the actor's own
                                                favourites (member_favorites)
  lens.created                     lens:<id>    lenses.py: a filter lens made {title, kind, estate_default}
  lens.subscribed                  lens:<id>    {mode: live | snapshot, copy?, album?, photos?}: a snapshot names the
                                                lens and the album it was copied into
  lens.unsubscribed                lens:<id>    {was, mode}: mode opted_out for an estate default
  lens.published, lens.unpublished lens:<id>
  lens.proposed                    lens:<id>    {title}: proposed as an estate default
  lens.accepted, lens.declined     lens:<id>    {title}: a sysadmin's answer
  album.published, album.unpublished  album:<id>   {lens}: an album published as a series
  photo.shown, photo.unshown       <sha>        "show it anyway", and taking it back
  lenses.migrated                  lenses       estate.py migrate {lens, made, hiding_sets, owned, favorites}
  faces.labeled                    people       faces.People: a page of names, "not them", "later", undo
                                                {ops: [{op, faces, person}]}, beside face-labels.jsonl
  person.changed                   person:<id>  faces.People: renamed, merged, born {ops: [...]}
  task.submitted                   task:<id>    tasks.py {prompt}: the actor is who asked
  task.finished                    task:<id>    tasks.py {status, error?}: done, failed or cancelled (the
                                                actor is who cancelled; NULL for a run's own end)
  ingest.finished                  source:<name>   estate.py ingest, a source that added anything
                                                {new, dup, refused, new_bytes, assets?: [after, last]}: assets, the
                                                rowids of the photos it added (after < rowid <= last; listeners.py)
  photo.purged                     <sha>        estate.py purge {reason, ext, kind, size, taken_at, first_seen,
                                                sources: [[source, path]]}
  sharers.migrated                 sharers      estate.py migrate {member, photos}
  photo.held                       <sha> or photos   private.py: photos kept private to their sharer at ingestion
                                                (`check`) or by the one-off run {photos, how: upload | library}: no
                                                actor, acting for the sharer; its id is the batch (private.batch, the
                                                inbox item held:<id>)
  photo.withdrawn                  <sha> or photos   POST /api/private: a sharer took photos back from the family {photos, until?}
  photo.release_set                <sha> or photos   the day private photos are released on, set or cleared {photos, until}
  photo.released                   <sha> or photos   released by their sharer {photos}, or by the app's timer on their
                                                day {photos, auto: true, until}: no actor, acting for the sharer
  photo.reviewed                   <sha> or photos   held photos their sharer looked over and kept private (/held,
                                                private.review: one entry per submit) {photos}; those it released are
                                                a photo.released of their own
  album.grows                      album:<id>   changes.py: the search a series grows from, set or cleared {grows_from}
  series.update_requested          album:<id>   listeners.py: new photos match a member's own series {ingest, photos,
                                                more?}: no actor, acting for its owner (their series_update item)
  question.asked                   question:<id>  stories.ask {to, photos, group_key}: by the asker (the estate's
                                                agent, acting for whom its run acts for; or a member)
  question.dismissed               question:<id>  stories.dismiss {question}: "I don't know", by the member
  story.told                       story:<id>   stories.tell {question, photos, recording, text}: by the teller
  story.transcribed                story:<id>   stories.transcribed {chars}: no actor, acting for the teller
  inbox.dismissed                  inbox:<id>   member_inbox.dismiss {kind, ref}: an item put away by its member (an
                                                item made or done is not journaled: the entry of what made or decided
                                                it says so)
  tombstones.changed               tombstones   private.py: POST /api/tombstones {added, lifted}: the member's opt-outs
  binding.requested                binding:<id>  members.bind: a member's own agent called from a new device {device}:
                                                no actor, acting for the member
  binding.approved, binding.revoked  binding:<id>  members.decide_binding {device, was}: by the member
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

_agent = threading.local()      # by_agent: the binding of the member's own agent this thread is writing for


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append(con: sqlite3.Connection, actor: int | None, kind: str, subject: str, data=None,
           acting_for: int | None = None) -> int:
    """One entry, in the caller's open transaction (it does not commit). Returns its id."""
    binding = getattr(_agent, "binding", None)
    if binding is not None and (data is None or isinstance(data, dict)):
        data = {**(data or {}), "binding": binding}
    cur = con.execute("INSERT INTO journal(at, actor, acting_for, kind, subject, data) VALUES (?,?,?,?,?,?)",
                      (now(), actor, acting_for, kind, subject,
                       None if data is None else json.dumps(data, separators=(",", ":"), ensure_ascii=False, default=str)))
    assert cur.lastrowid is not None
    return cur.lastrowid


def since(con: sqlite3.Connection, id: int = 0, limit: int = 1000) -> list[dict]:
    """The entries after `id`, oldest first, at most `limit`: a listener keeps the last id it handled."""
    return [{"id": i, "at": at, "actor": actor, "acting_for": af, "kind": kind, "subject": subject,
             "data": json.loads(data) if data else None}
            for i, at, actor, af, kind, subject, data in con.execute(
                "SELECT id, at, actor, acting_for, kind, subject, data FROM journal WHERE id > ? ORDER BY id LIMIT ?",
                (id, limit))]


@contextmanager
def by_agent(binding: int | None):
    """Every entry appended inside, in this thread, is by the member's own agent of this binding (None: no one's)."""
    was, _agent.binding = getattr(_agent, "binding", None), binding
    try:
        yield
    finally:
        _agent.binding = was


# -- what a member may know of it (journal_since) ------------------------------------------------------------

SCAN = 5000            # entries looked at by one call at most; the rest is the next call's (more)
NAMED = 200            # photo ids one returned entry names at most
SERIES_KINDS = ("album.created", "album.items", "album.renamed", "album.query", "album.grows", "album.published")
THEIRS = ("favorite.set", "lens.subscribed", "lens.unsubscribed", "photo.shown", "photo.unshown", "photo.held",
          "photo.withdrawn", "photo.release_set", "photo.released", "question.asked", "question.dismissed",
          "inbox.dismissed", "binding.requested", "binding.approved", "binding.revoked", "series.update_requested")
LENS_KINDS = ("lens.created", "lens.published", "lens.unpublished", "lens.proposed", "lens.accepted", "lens.declined")
SHA = re.compile(r"[0-9a-f]{64}")


def _first_admin(con) -> int | None:
    row = con.execute("SELECT id FROM members WHERE role='sysadmin' AND status='active' ORDER BY id LIMIT 1").fetchone()
    return row[0] if row else None


def _visible_entry(con, e: dict, member: int, admin: int | None, refused, readable) -> dict | None:
    """The entry as `member` may be told it (its data possibly cut down), or None: the rules in the docstring."""
    kind, subject, data = e["kind"], str(e["subject"] or ""), e["data"]
    theirs = member in (e["actor"], e["acting_for"])
    num = subject.partition(":")[2]
    if SHA.fullmatch(subject) and refused(subject, member):
        return None
    if kind == "favorite.set":
        whose = (data or {}).get("member", e["actor"])
        return e if whose == member else None
    if kind == "series.update_requested":
        if e["acting_for"] != member:
            return None
        sid = int(num)
        open_ = con.execute("SELECT 1 FROM inbox_items WHERE member_id=? AND kind='series_update' AND ref=? AND state='open'",
                            (member, f"album:{sid}")).fetchone() is not None
        return {**e, "data": {**(data or {}), "photos": [x for x in (data or {}).get("photos") or [] if not refused(x, member)],
                              "waiting": open_}}
    if kind in THEIRS:
        return e if theirs else None
    if kind.startswith("album."):
        row = con.execute("SELECT owner FROM change_sets WHERE id=?", (int(num),)).fetchone() if num.isdigit() else None
        if row is None:
            return None
        if (row[0] if row[0] is not None else admin) == member:
            return e
        published = con.execute("SELECT 1 FROM lenses WHERE kind='series' AND set_id=? AND published=1", (int(num),)).fetchone()
        return {**e, "data": None} if published and kind in SERIES_KINDS else None
    if kind in LENS_KINDS:
        row = con.execute("SELECT owner, published FROM lenses WHERE id=?", (int(num),)).fetchone() if num.isdigit() else None
        return e if row and ((row[0] if row[0] is not None else admin) == member or row[1]) else None
    if kind == "ingest.finished":
        span = (data or {}).get("assets")
        if not span:
            return None
        new = [s for s, in con.execute("SELECT sha256 FROM assets WHERE rowid > ? AND rowid <= ? ORDER BY rowid", span)
               if not refused(s, member)]
        if not new:
            return None
        return {**e, "data": {"photos": new[:NAMED], "new": len(new), **({"more": len(new) - NAMED} if len(new) > NAMED else {})}}
    if kind in ("story.told", "story.transcribed"):
        story = readable(int(num)) if num.isdigit() else None
        return {**e, "data": {**(data or {}), "photos": story["photos"]}} if story else None
    return None


def for_member(con: sqlite3.Connection, member: int | None, cursor: int = 0, limit: int = 100, *,
               refused, readable, loaded: int | None = None) -> dict:
    """The entries after `cursor` that `member` may know of (the docstring's rules), oldest first, at most `limit`,
    looking at no more than SCAN entries: {entries, next_cursor, more, waiting?}. `refused(sha, member)`: they may
    not be shown that photo now; `readable(story id)`: the story as they may read it, or None; `loaded`: the newest
    entry the catalog being served was read after (an ingest.finished after it stops the page: waiting). Each
    entry: {id, at, kind, subject, by, for, data}, by and for being members' names."""
    if member is None:
        return {"entries": [], "next_cursor": cursor, "more": False}
    admin = _first_admin(con)
    names = dict(con.execute("SELECT id, name FROM members"))
    out, at, waiting = [], cursor, False
    for e in since(con, cursor, SCAN):
        if e["kind"] == "ingest.finished" and loaded is not None and e["id"] > loaded:
            waiting = True
            break
        at = e["id"]
        got = _visible_entry(con, e, member, admin, refused, readable)
        if got is not None:
            out.append({"id": got["id"], "at": got["at"], "kind": got["kind"], "subject": got["subject"],
                        "by": names.get(got["actor"]), "for": names.get(got["acting_for"]), "data": got["data"]})
            if len(out) >= limit:
                break
    more = waiting or con.execute("SELECT 1 FROM journal WHERE id > ? LIMIT 1", (at,)).fetchone() is not None
    return {"entries": out, "next_cursor": at, "more": more,
            **({"waiting": "new photos are being made searchable: ask again in a minute"} if waiting else {})}
