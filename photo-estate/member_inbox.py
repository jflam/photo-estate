"""member_inbox.py — what each member has to decide: their inbox (standard library only). Not the upload
folder (`--inbox`, upload.IngestJob), which only shares the word; the table is `inbox_items` (core/schema.py).

An item is made by the code that needs the decision, in the same transaction as what caused it, and closed
by the decision itself, wherever it is made: the inbox only points at the screen that already decides it.
An item is open, done (decided, or no longer waiting on anyone) or dismissed (put away by its member).

  OPENS                                  kind -> what an item opens, from its ref (a kind is a row here)
  put(con, members, kind, ref, title)    one open item per member, kind and ref: made, or brought up to date
  close(con, kind, ref, member=None)     every open item of that kind and ref (or that member's) is done
  retitle(con, kind, ref, member, title) an open item says something new, and stays where it is in the list
  mine(con, member, keep=None) -> {items, open}   GET /api/inbox: the member's open items, newest first (keep: a
                                         filter, for the questions: stories.keep_item)
  dismiss(con, member, id) -> dict       POST /api/inbox/<id>/dismiss (InboxError: not theirs, or not open)
  sysadmins(con) -> [members.id]         the active sysadmins, whom a request or a proposal is for
  lens_changed(con, actor, lens=, album=)   a published lens's photos changed: its live subscribers are told
  InboxError

The kinds, who each is for, what makes and closes it (all in the caller's transaction):

  member_request  every active sysadmin   members.request (someone asked to be let in); closed when that
                  member:<id>             member is let in or turned away (members.change). Opens /members
  review          whom a task acted for   tasks.Tasks.move, when a task ends: each hiding set it made that has
                  album:<id>              photos and hides none yet; closed when the set is applied in full,
                                          taken down to an album or discarded (changes.py). Opens /changes/<id>
  lens_proposed   every active sysadmin   lenses.propose; closed when a sysadmin decides (lenses.decide) or
                  lens:<id>               the owner takes the filter back (lenses.publish off). Opens Albums
                                          ("Waiting for you to decide")
  lens_changed    each live subscriber    changes.py, when a published filter's hides change (apply, revert,
                  lens:<id> or album:<id> a hidden photo taken out) or a published series' photos or search do;
                                          one open item per member and lens, updated, never one per change.
                                          Nothing waits on it: it is closed when they leave the lens
                                          (lenses.unsubscribe, or a snapshot instead) or put away. Opens Albums
                                          (a filter) or the series (/?album=<id>)
  held            the photos' sharer      private.py, when a hold (a batch of phone uploads checked, or the
                  held:<batch>            one-off run over the library) kept photos private to them: one open
                                          item per member, which a later hold brings up to date (its ref stays
                                          that of the batch that opened it), saying how many wait to be looked
                                          over; done once none waits (looked over on /held, or released by any
                                          way), not once none is held. Opens the review of held photos (/held)

  question        the member it is dealt to stories.deal (when their inbox is read): one item per group of
                  ask:<group>             questions (a group_key, or q<id>), within their weekly allowance; done once
                                          each question in it is answered or put away (stories.py). Not shown while
                                          none of its questions may be shown to them. Opens /inbox?ask=<group>
  series_update   a series' owner         listeners.py, when new photos match a member's own series (an album that
                  album:<id>              grows from a search): its candidates are in series.update_requested; done
                                          when the owner adds them, or none (POST /api/albums/<id>/candidates).
                                          Opens the album with its candidates (/?album=<id>&candidates=1)
  binding         the member                members.bind, the first time their own agent calls /mcp from a device;
                  binding:<id>            done when they allow it or turn it off (members.decide_binding). Opens
                                          /agents ("Connect your agent")

Adding a kind (a later phase): a row in OPENS, and a put() and a close() in the code that makes and
decides it. Nothing else.

The journal (journal.py): an item made or done is not journaled, because the entry of what made it or
decided it (member.requested, lens.proposed, task.finished, album.applied, member.changed, lens.accepted,
...) already says so. Dismissing is the one thing only the inbox knows: inbox.dismissed {kind, ref}, by the
member, subject inbox:<id>.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from urllib.parse import quote

import journal


def _num(ref: str) -> str:
    return ref.partition(":")[2]


# kind -> what its item opens (an address on the app), from the item's ref
OPENS = {
    "member_request": lambda ref: "/members",
    "review": lambda ref: f"/changes/{_num(ref)}",
    "lens_proposed": lambda ref: "/?albums",
    "lens_changed": lambda ref: f"/?album={_num(ref)}" if ref.startswith("album:") else "/?albums",
    "held": lambda ref: "/held",
    "question": lambda ref: f"/inbox?ask={quote(ref.partition(':')[2])}",
    "series_update": lambda ref: f"/?album={_num(ref)}&candidates=1",
    "binding": lambda ref: "/agents",
}
STATES = ("open", "done", "dismissed")


class InboxError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sysadmins(con: sqlite3.Connection) -> list[int]:
    return [r[0] for r in con.execute("SELECT id FROM members WHERE role='sysadmin' AND status='active' ORDER BY id")]


def put(con: sqlite3.Connection, members, kind: str, ref: str, title: str) -> list[int]:
    """One open item of `kind` about `ref` for each of `members` (members.id; None is skipped): made, or, when
    one is open already, given this title and moved to the top. In the caller's transaction. Returns their ids."""
    assert kind in OPENS, f"not an inbox kind: {kind}"
    t, out = now(), []
    for m in dict.fromkeys(x for x in members if x is not None):
        row = con.execute("SELECT id FROM inbox_items WHERE member_id=? AND kind=? AND ref=? AND state='open'",
                          (m, kind, ref)).fetchone()
        if row:
            con.execute("UPDATE inbox_items SET title=?, updated_at=? WHERE id=?", (title[:300], t, row[0]))
            out.append(row[0])
        else:
            out.append(con.execute("INSERT INTO inbox_items(member_id, kind, ref, title, created_at, updated_at, state)"
                                   " VALUES (?,?,?,?,?,?,'open')", (m, kind, ref, title[:300], t, t)).lastrowid)
    return out


def close(con: sqlite3.Connection, kind: str, ref: str, member: int | None = None) -> int:
    """The decision was made (or is no longer anyone's to make): every open item of `kind` about `ref`, or only
    `member`'s, is done. In the caller's transaction. Returns how many."""
    q, args = "UPDATE inbox_items SET state='done', done_at=? WHERE kind=? AND ref=? AND state='open'", [now(), kind, ref]
    if member is not None:
        q, args = q + " AND member_id=?", args + [member]
    return con.execute(q, args).rowcount


def retitle(con: sqlite3.Connection, kind: str, ref: str, member: int, title: str) -> int:
    """`member`'s open item of `kind` about `ref` says `title` now, without being moved to the top (put() moves it:
    something new happened; this is the same thing, counted again). In the caller's transaction. How many changed."""
    return con.execute("UPDATE inbox_items SET title=? WHERE member_id=? AND kind=? AND ref=? AND state='open' AND title IS NOT ?",
                       (title[:300], member, kind, ref, title[:300])).rowcount


def _item(row) -> dict:
    i, kind, ref, title, created, updated = row
    return {"id": i, "kind": kind, "ref": ref, "title": title, "created_at": created, "updated_at": updated,
            "opens": OPENS[kind](ref) if kind in OPENS else None}


def mine(con: sqlite3.Connection, member: int | None, keep=None) -> dict:
    """GET /api/inbox: `member`'s open items, newest first (an item brought up to date counts as new), each with
    the address it opens; `open`, how many. `keep(item)`: False leaves an item out (stories.keep_item)."""
    rows = con.execute("SELECT id, kind, ref, title, created_at, updated_at FROM inbox_items WHERE member_id IS ?"
                       " AND state='open' ORDER BY updated_at DESC, id DESC", (member,)).fetchall()
    items = [it for it in (_item(r) for r in rows) if keep is None or keep(it)]
    return {"items": items, "open": len(items)}


def dismiss(con: sqlite3.Connection, member: int | None, iid: int) -> dict:
    """POST /api/inbox/<id>/dismiss: the member puts one of their own open items away. inbox.dismissed in the
    journal. InboxError 404 for an item that is not theirs (as if there were none), 409 for one not open."""
    with con:
        row = con.execute("SELECT kind, ref, state FROM inbox_items WHERE id=? AND member_id IS ?", (iid, member)).fetchone()
        if row is None:
            raise InboxError(404, "no such item")
        kind, ref, state = row
        if state != "open":
            raise InboxError(409, f"this item is {state} already")
        con.execute("UPDATE inbox_items SET state='dismissed', done_at=? WHERE id=?", (now(), iid))
        journal.append(con, member, "inbox.dismissed", f"inbox:{iid}", {"kind": kind, "ref": ref})
    return {"id": iid, "state": "dismissed", "open": mine(con, member)["open"]}


def lens_changed(con: sqlite3.Connection, actor: int | None, lens: int | None = None, album: int | None = None) -> list[int]:
    """A lens's photos changed (a filter: what its hiding sets hide, `lens`; a series: its album's photos or
    search, `album`). If it is published, each member subscribed to it live, but its owner and `actor`, has
    one open lens_changed item about it, made or brought up to date with what it is now. The change itself
    applies at once: the item only tells them. In the caller's transaction."""
    if lens is not None:
        row = con.execute("SELECT id, owner, title, kind, published FROM lenses WHERE id=? AND kind='filter'", (lens,)).fetchone()
        ref = f"lens:{lens}"
    else:
        row = con.execute("SELECT id, owner, title, kind, published FROM lenses WHERE kind='series' AND set_id=?", (album,)).fetchone()
        ref = f"album:{album}"
    if not row or not row[4]:
        return []
    lid, owner, title, kind, _ = row
    if owner is None:
        owner = (sysadmins(con) or [None])[0]
    subs = [m for m, in con.execute("SELECT member_id FROM subscriptions WHERE lens_id=? AND mode='live'", (lid,))
            if m not in (owner, actor)]
    if not subs:
        return []
    name = (con.execute("SELECT name FROM members WHERE id=?", (owner,)).fetchone() or ["Someone"])[0]
    if kind == "filter":       # (counting no photo private to anyone: the same title goes to every subscriber)
        q = "SELECT count(DISTINCT h.sha256) FROM hidden h JOIN change_sets s ON s.id = h.set_id WHERE s.lens_id=?"
        try:
            n = con.execute(q + " AND h.sha256 NOT IN (SELECT sha256 FROM private WHERE released_at IS NULL)", (lid,)).fetchone()[0]
        except sqlite3.OperationalError:     # a catalog from before private photos
            n = con.execute(q, (lid,)).fetchone()[0]
        what = f"{name}’s lens “{title}” changed: it hides {n} photo{'' if n == 1 else 's'} now"
    else:
        what = f"{name}’s series “{title}” changed"
    return put(con, subs, "lens_changed", ref, what)
