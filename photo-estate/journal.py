"""journal.py — what happened in the estate, and who did it: one append-only log (standard library only).

  append(con, actor, kind, subject, data=None, acting_for=None) -> id   one entry, in the caller's transaction
  since(con, id=0, limit=1000) -> [dict]                                the entries after `id`, oldest first

The table is the members area's `journal` (core/schema.py); triggers refuse any UPDATE or DELETE of it, so
undoing something is a new entry, never a changed one. The state tables (change_sets, hidden, favorites,
faces, ...) stay the truth for what the app shows; the journal is the history beside them. One entry per
action, not per photo: applying a set that hides 10,000 photos is one entry naming the set, and a write
that changes nothing appends nothing. `append` never commits: the entry lands with the change it records,
or not at all. Nothing reads the journal yet (the
listeners of the multiplayer plan's phase 7 will, with `since`).

`actor` is members.id of whoever did it: the member a request is for (PhotoApp.who), so a request from
this computer itself is the first sysadmin; NULL when there is no member to name (no sysadmin yet, or the
app itself, as when a task's run ends). `acting_for` is the member an agent's run works for. `data` is
JSON, whatever else the kind needs, small: counts and titles, never a list of every photo.

Kinds, with their subject and where each is appended:

  member.added, member.requested, member.changed   member:<id>    members.py
  album.created                    album:<id>   changes.py: an album or change set made {title, op, kind, added}
  album.items                      album:<id>   photos added or taken out {added, removed}
  album.decided                    album:<id>   marks on the review page {decision, decided}
  album.applied                    album:<id>   {hid, showed, through}
  album.reverted                   album:<id>   {showed}
  album.committed, album.reopened  album:<id>
  album.hidden, album.viewed       album:<id>   put up for hiding, or a plain album again {added}
  album.renamed                    album:<id>   {title, prompt}: [before, after] of what changed
  album.query                      album:<id>   a live album's search {query}
  album.discarded                  album:<id>   marked discarded: its rows are kept, it is in no listing
  favorite.set                     <sha> or album:<id>   photo_app.set_favorites {fav, photos}
  faces.labeled                    people       faces.People: a page of names, "not them", "later", undo
                                                {ops: [{op, faces, person}]}, beside face-labels.jsonl
  person.changed                   person:<id>  faces.People: renamed, merged, born {ops: [...]}
  task.submitted                   task:<id>    tasks.py {prompt}: the actor is who asked
  task.finished                    task:<id>    tasks.py {status, error?}: done, failed or cancelled (the
                                                actor is who cancelled; NULL for a run's own end)
  ingest.finished                  source:<name>   estate.py ingest, a source that added anything
                                                {new, dup, refused, new_bytes}
  photo.purged                     <sha>        estate.py purge {reason, ext, kind, size, taken_at, first_seen,
                                                sources: [[source, path]]}
  sharers.migrated                 sharers      estate.py migrate {member, photos}
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append(con: sqlite3.Connection, actor: int | None, kind: str, subject: str, data=None,
           acting_for: int | None = None) -> int:
    """One entry, in the caller's open transaction (it does not commit). Returns its id."""
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
