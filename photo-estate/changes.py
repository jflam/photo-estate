"""Albums: named sets of photos, and what can be done with one. Hiding a set of photos is one of those
things, so a change set (a bulk edit, proposed first and applied only once a person has looked) is an
album that has been put up for hiding.

An album is a title, a note on what was asked for (`prompt`), and its photos. A `list` album holds the
photos it was given, in the order given, each optionally under a section ("2014", "2015", ...) and with
a reason a person can read. A `live` album holds a search instead (`query`, as an agent's search_photos
takes it), and is whatever that search finds now. What an album is for is its `op`:

  view   an album to look at. It changes nothing about the library.
  hide   a change set: everything below applies. Putting an album up for hiding (`hide`) makes it one
         (a live album is first fixed as the photos it finds now); `view` makes it a plain album again
         once it hides nothing.

Besides hiding, an album's photos can be made favourites together (or have that taken back), and
their original files downloaded as one zip (`embed.py serve` answers that: it has the store).

  GET  /api/albums                      every album, newest first (/api/changes: only those up for hiding)
  GET  /api/albums/<id>/photos?cursor=&limit=   its photos as /api/search gives them, with section and reason
  POST /api/albums           {title, prompt?, query?, items?}   a new album (with `query`, a live one)
  POST /api/albums/<id>/items    {items?: [{sha, section?, reason?, score?}], remove?: [sha]}
  POST /api/albums/<id>/rename   {title?, prompt?}
  POST /api/albums/<id>/query    {query}             what a live album searches for
  POST /api/albums/<id>/favorite {fav: true|false}   every photo in it
  POST /api/albums/<id>/hide | /view                 put it up for hiding, or take it down again
  POST /api/albums/<id>/discard                      put it away (it must hide nothing): marked discarded

Every /api/changes address below also answers under /api/albums. The tables keep their first names
(change_sets, change_items): an album is a row of change_sets.

Change sets: a bulk edit to the library, proposed first and applied only once a person has looked.

An agent (or a person) proposes a set of photos and what to do to them. Nothing in the library changes
until the set is applied. Before that, a person looks the set over on the review page (/changes/<id>)
and marks the exceptions: the photos to keep. Every photo not marked is done to as proposed, so the
review is a last look, not a decision per photo. Each mark is saved as it is made, so a review of ten
thousand photos can stop and carry on later. Applying can be done as often as wanted: it makes the
library match the marks made so far, for the whole set or only as far as the person has looked.
Reverting a set takes back everything it did, as one unit. When the library is as the person wants it,
they commit the set: it is done, and nothing about it can change until it is reopened.

The first (and so far only) operation is `hide`: the photo stays in the store and in the catalog, and
is left out of every view (search, the library, People, the copy a phone keeps). Files are never
touched.

  GET  /changes, /changes/<id>          the page
  GET  /api/changes                     every set, newest first, with its counts
  GET  /api/changes/<id>                one set and its photos
  GET  /api/changes/<id>/check          is every photo of the set where the set says it should be?
  POST /api/changes          {title, prompt?, op?, items: [{sha, score?, reason?}]}   propose a set
  POST /api/changes/<id>/items   {items?: [...], remove?: [sha]}    add to it, or take photos out of it
  POST /api/changes/<id>/decide  {shas: [...], decision: accept | reject | pending}
  POST /api/changes/<id>/apply   {through?: sha}   do it to every photo not marked reject; put back those
                                        that are. With `through`, only as far as that photo, in the order
                                        the page shows them: the rest of the set is left as it is
  POST /api/changes/<id>/revert         undo everything the set did (the decisions are kept)
  POST /api/changes/<id>/commit         the set is done: possible once nothing is left to apply
  POST /api/changes/<id>/reopen         a done set can be changed (or reverted) again
  POST /api/changes/<id>/discard        put away a set that hides nothing

Nothing is deleted from an album's history. Discarding marks the set `discarded` (ChangeStatus.DISCARDED):
its rows stay, and it is in no listing and answers no request, as if it were gone. Every write that changes
something appends one journal entry (journal.py: album.created, album.items, album.applied, ...) in its
own transaction, naming the set (album:<id>) and the member it was done for (`actor`, members.id), never
one per photo; a write that changes nothing appends nothing.

Like faces.People and intake's API it is one object with a `handle` method, mounted by `embed.py serve`.
The tables live in the catalog. `hidden` is keyed by photo and set, so a photo two sets hide stays
hidden until both are undone; hiding for one person only (once the app has users) is one more column.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

import journal
import pages
from core import schema
from core.states import AlbumKind, ChangeOp, ChangeStatus, Decision, SetState

SCHEMA, ADDED = schema.CHANGES, schema.ADDED["changes"]   # the tables, and columns newer than a catalog may be
OPS = tuple(ChangeOp)          # ("hide", "view")
LIVE_MAX = 20000   # a live album is at most this many of what its search finds
DECISIONS = tuple(Decision)    # ("pending", "accept", "reject")
SHA = re.compile(r"^[0-9a-f]{64}$")
# The journal kind of each write (journal.py); discard and favorite append their own.
KINDS = {"rename": "album.renamed", "query": "album.query", "hide": "album.hidden", "view": "album.viewed",
         "items": "album.items", "decide": "album.decided", "apply": "album.applied", "revert": "album.reverted",
         "commit": "album.committed", "reopen": "album.reopened"}
ROUTE = re.compile(r"^/api/(changes|albums)(?:/(\d+)(?:/(items|decide|apply|revert|commit|reopen|discard|check|photos|rename|query|favorite|hide|view))?)?$")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def place(when: str, sha: str) -> tuple:
    """Where a photo comes in a set: oldest first, those with no date last."""
    return (when == "", when, sha)


def hidden_shas(con: sqlite3.Connection) -> set[str]:
    """The photos left out of every view. Empty for a catalog that has never had a change set."""
    try:
        return {r[0] for r in con.execute("SELECT DISTINCT sha256 FROM hidden")}
    except sqlite3.OperationalError:
        return set()


class Changes:
    """`facts(sha)` is what the app knows of a photo (None if it has no such photo); `shown(sha)` says
    where the running app is showing it now ({"library": bool, "sync": bool}); `busy()` is true while
    the app is still taking in the last change; `on_change()` is called when the hidden photos change.
    `find(query)` is an agent's search, ([ids best first], scores), for live albums; `favorite(shas, on,
    who, actor, subject)` makes photos favourites or not and returns how many changed; `cover(shas)` picks the photo
    for an album's front as the app picks a month's (a favourite if there is one)."""

    def __init__(self, db: str, facts, shown, busy=lambda: False, on_change=lambda: None, find=None, favorite=None,
                 cover=None):
        self.db, self.facts, self.shown, self.busy, self.on_change = db, facts, shown, busy, on_change
        self.find, self.favorite, self.cover = find, favorite, cover
        self.lock = threading.Lock()   # one writer at a time; reads take no lock (WAL)
        con = self.con()
        schema.ensure(con, "changes", "members")   # with the columns older catalogs lack (ADDED); members: the journal
        con.close()

    def con(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db, timeout=60)
        con.execute("PRAGMA journal_mode=WAL")
        return con

    # -- reading ---------------------------------------------------------------------------------

    def summary(self, con, sid: int) -> dict | None:
        row = con.execute("SELECT id, title, prompt, op, status, created_by, created_at, updated_at, applied_at,"
                          " reverted_at, done_at, kind, query FROM change_sets WHERE id=? AND status IS NOT ?",
                          (sid, ChangeStatus.DISCARDED)).fetchone()
        if not row:
            return None
        s: dict[str, Any] = dict(zip(("id", "title", "prompt", "op", "status", "created_by", "created_at", "updated_at",
                      "applied_at", "reverted_at", "done_at", "kind", "query"), row))
        s["kind"] = s["kind"] or AlbumKind.LIST
        s["query"] = json.loads(s["query"]) if s["query"] else None
        if s["op"] == ChangeOp.VIEW:     # an album to look at: it is what it holds, and nothing is pending
            n = len(self.members(con, s)) if s["kind"] == AlbumKind.LIVE else con.execute(
                "SELECT count(*) FROM change_items WHERE set_id=?", (sid,)).fetchone()[0]
            return {**s, "state": SetState.ALBUM if n or s["kind"] == AlbumKind.LIVE else SetState.EMPTY, "n": n,
                    **{d: 0 for d in DECISIONS}, "applied": 0, "todo": 0}
        n = {d: 0 for d in DECISIONS}
        applied = todo = 0
        for decision, was_applied, k in con.execute(
                "SELECT decision, applied, count(*) FROM change_items WHERE set_id=? GROUP BY 1, 2", (sid,)):
            n[decision] += k
            applied += k if was_applied else 0
            todo += k if (decision != Decision.REJECT) != bool(was_applied) else 0
        total = sum(n.values())
        # what the set has come to: nothing in it, reverted, still being looked over, applied in full
        # and waiting to be committed or reverted, or done
        s["state"] = (SetState.EMPTY if not total else SetState.DONE if s["status"] == ChangeStatus.DONE and not todo
                      else SetState.REVERTED if s["status"] == ChangeStatus.REVERTED and not applied
                      else SetState.REVIEW if todo else SetState.APPLIED)
        return {**s, "n": total, **n, "applied": applied, "todo": todo}

    def members(self, con, s: dict) -> list[dict]:
        """An album's photos in its order: a list album's as given (a section's photos together, sections
        in the order they were first used), a live album's as its search finds them now. Photos no
        longer in the library are left out."""
        if s["kind"] == AlbumKind.LIVE:
            try:
                ids, scores = self.find(s["query"] or {}) if self.find else ([], None)
            except (ValueError, KeyError, TypeError):      # a search that no longer makes sense (a person renamed)
                ids, scores = [], None
            return [{"sha": sha, "score": scores[i] if scores else None} for i, sha in enumerate(ids[:LIVE_MAX])]
        rows = con.execute("SELECT sha256, section, reason, score, decision, applied, position, rowid FROM change_items"
                           " WHERE set_id=?", (s["id"],)).fetchall()
        rows = [r for r in rows if self.facts(r[0]) is not None]
        rows.sort(key=lambda r: (r[6] is None, r[6] or 0, r[7]))
        first = {}
        for i, r in enumerate(rows):
            first.setdefault(r[1] or "", i)
        rows.sort(key=lambda r: first[r[1] or ""])     # stable: a section's photos keep their order
        return [{"sha": sha, "section": section, "reason": reason, "score": score, "decision": decision, "applied": bool(applied)}
                for sha, section, reason, score, decision, applied, _, _ in rows]

    def listing(self, albums: bool = False) -> dict:
        """Every album, newest first; without `albums`, only those up for hiding (the change sets)."""
        con = self.con()
        try:
            ids = [r[0] for r in con.execute("SELECT id FROM change_sets WHERE status IS NOT ?" + ("" if albums else " AND op != 'view'")
                                             + " ORDER BY id DESC", (ChangeStatus.DISCARDED,))]
            sets = [s for i in ids if (s := self.summary(con, i)) is not None]
            for s in sets:   # a few photos to show on the set's card
                if s["op"] == ChangeOp.VIEW:     # its front: a favourite of it if it has one, as a month's is
                    mem = [it["sha"] for it in self.members(con, s)]
                    front = self.cover(mem) if self.cover and mem else None
                    s["cover"] = ([front] if front else []) + [x for x in mem[:6] if x != front][:5 if front else 6]
                else:
                    s["cover"] = [r[0] for r in con.execute(
                        "SELECT sha256 FROM change_items WHERE set_id=? ORDER BY score DESC, sha256 LIMIT 6", (s["id"],))]
            return {"sets": sets, "hidden": len(hidden_shas(con)), "busy": bool(self.busy())}
        finally:
            con.close()

    def photos(self, sid: int, start: int = 0, limit: int = 150) -> dict | None:
        """A page of an album's photos, each as /api/search gives a photo, with its section and reason,
        and `hidden` when no view but this one shows it."""
        con = self.con()
        try:
            s = self.summary(con, sid)
            if not s:
                return None
            mem, hidden = self.members(con, s), hidden_shas(con)
        finally:
            con.close()
        sections = []
        for it in mem:
            title = it.get("section") or ""
            if not sections or sections[-1]["title"] != title:
                sections.append({"title": title, "n": 0})
            sections[-1]["n"] += 1
        rows = []
        for it in mem[start:start + limit]:
            row = {"sha": it["sha"], **self.facts(it["sha"]), "section": it.get("section") or ""}
            for k in ("reason", "score"):
                if it.get(k) is not None:
                    row[k] = it[k]
            if it["sha"] in hidden:
                row["hidden"] = True
            rows.append(row)
        return {"album": s, "results": rows, "next": start + limit if start + limit < len(mem) else None,
                "matching": len(mem), "sections": sections if any(x["title"] for x in sections) else []}

    def shas(self, sid: int):
        """(title, every photo id in order), or None: what a download is made of."""
        con = self.con()
        try:
            s = self.summary(con, sid)
            return (s["title"], [it["sha"] for it in self.members(con, s)]) if s else None
        finally:
            con.close()

    def detail(self, sid: int) -> dict | None:
        con = self.con()
        try:
            s = self.summary(con, sid)
            if not s:
                return None
            others = {r[0] for r in con.execute("SELECT sha256 FROM hidden WHERE set_id != ?", (sid,))}
            items = []
            for sha, decision, applied, score, reason in con.execute(
                    "SELECT sha256, decision, applied, score, reason FROM change_items WHERE set_id=?", (sid,)):
                m = self.facts(sha) or {}
                it = {"sha": sha, "decision": decision, "applied": bool(applied), "score": score, "reason": reason,
                      "when": self.when(sha), "ar": m.get("ar") or 1.5,
                      "place": m.get("place"), "event": m.get("event"), "event_title": m.get("event_title"),
                      "people": m.get("people") or []}
                if sha in others:
                    it["also_hidden"] = True     # another set hides it too
                if not m:
                    it["missing"] = True         # no longer a photo of the library
                items.append(it)
            items.sort(key=lambda it: place(it["when"], it["sha"]))
            return {**s, "items": items, "busy": bool(self.busy())}
        finally:
            con.close()

    def check(self, sid: int) -> dict | None:
        """Compare the set with what the running app shows: a photo the set hides must be in neither
        the library nor the copy phones keep, and every other photo of the set must be in both (unless
        another set hides it). `wrong` lists the photos for which that is not so."""
        con = self.con()
        try:
            s = self.summary(con, sid)
            if not s:
                return None
            hidden = hidden_shas(con)
            rows = con.execute("SELECT sha256, decision, applied FROM change_items WHERE set_id=?", (sid,)).fetchall()
        finally:
            con.close()
        out = {"hidden": 0, "visible": 0, "missing": 0, "wrong": []}
        for sha, decision, applied in rows:
            if self.facts(sha) is None:
                out["missing"] += 1
                continue
            want_hidden = sha in hidden
            got = self.shown(sha)
            out["hidden" if want_hidden else "visible"] += 1
            if got["library"] == want_hidden or got["sync"] == want_hidden:
                out["wrong"].append({"sha": sha, "decision": decision, "should_be": "hidden" if want_hidden else "visible",
                                     "in_library": got["library"], "in_sync": got["sync"]})
        busy = bool(self.busy())
        return {"id": sid, "busy": busy, "ok": not busy and not out["wrong"], "todo": s["todo"], "n": s["n"], **out}

    def when(self, sha: str) -> str:
        m = self.facts(sha) or {}
        return m.get("local") or m.get("taken") or ""

    # -- writing ---------------------------------------------------------------------------------

    def add_items(self, con, sid: int, items) -> dict:
        added = unknown = 0
        at = con.execute("SELECT coalesce(max(position), 0) FROM change_items WHERE set_id=?", (sid,)).fetchone()[0]
        for it in items or []:
            if isinstance(it, str):
                it = {"sha": it}
            sha = it.get("sha") if isinstance(it, dict) else None
            if not isinstance(sha, str) or not SHA.match(sha) or self.facts(sha) is None:
                unknown += 1
                continue
            score, at = it.get("score"), at + 1
            added += con.execute("INSERT OR IGNORE INTO change_items(set_id, sha256, score, reason, section, position)"
                                 " VALUES (?,?,?,?,?,?)",
                                 (sid, sha, float(score) if isinstance(score, (int, float)) else None,
                                  str(it["reason"])[:500] if it.get("reason") else None,
                                  str(it["section"])[:120] if it.get("section") else None, at)).rowcount
        return {"added": added, "unknown": unknown}

    def create(self, body: dict, who: str = "", actor: int | None = None) -> tuple[dict, int]:
        """A new album or change set. `who` is the login (or task:<id>) kept as created_by; `actor` the
        member it is for (members.id), who the journal names."""
        title = str(body.get("title") or "").strip()
        op = body.get("op") or ChangeOp.HIDE
        if not title:
            return {"error": "an album needs a title" if op == ChangeOp.VIEW else "a change set needs a title"}, 400
        if op not in OPS:
            return {"error": f"unknown operation {op!r}; known: {', '.join(OPS)}"}, 400
        query = body.get("query")
        if query is not None:            # a live album: its search must be one that can be run
            if op != ChangeOp.VIEW or body.get("items"):
                return {"error": "a live album is a search and nothing else: no items, and it is not up for hiding"}, 400
            bad = self.bad_query(query)
            if bad:
                return {"error": bad}, 400
        with self.lock:
            con = self.con()
            try:
                t = now()
                sid = con.execute("INSERT INTO change_sets(title, prompt, op, created_by, created_at, updated_at, kind, query)"
                                  " VALUES (?,?,?,?,?,?,?,?)",
                                  (title[:200], body.get("prompt"), op, who or None, t, t, AlbumKind.LIVE if query is not None else AlbumKind.LIST,
                                   json.dumps(query) if query is not None else None)).lastrowid
                assert sid is not None
                got = self.add_items(con, sid, body.get("items"))
                journal.append(con, actor, "album.created", f"album:{sid}",
                               {"title": title[:200], "op": op, "kind": AlbumKind.LIVE if query is not None else AlbumKind.LIST,
                                "added": got["added"]})
                con.commit()
                return {**(self.summary(con, sid) or {}), **got}, 201
            finally:
                con.close()

    def bad_query(self, query) -> str | None:
        if not isinstance(query, dict) or not self.find:
            return "a live album's `query` is a search, as search_photos takes it"
        try:
            self.find(query)
        except (ValueError, KeyError, TypeError) as e:
            return str(e)
        return None

    def write(self, sid: int, what: str, body: dict, who: str = "", actor: int | None = None) -> tuple[dict, int]:
        """One write to a set; `actor` (members.id) is who the journal entry names."""
        changed = False
        if what == "favorite":               # not a change to the album: to its photos, in the library
            if not isinstance(body.get("fav"), bool) or not self.favorite:
                return {"error": "favorite takes {fav: true | false}"}, 400
            got = self.shas(sid)
            if not got:
                return {"error": "no such album"}, 404
            n = self.favorite(got[1], body["fav"], who, actor, f"album:{sid}")
            return {"id": sid, "photos": len(got[1]), "fav": body["fav"], "changed": n}, 200
        with self.lock:
            con = self.con()
            try:
                was = self.summary(con, sid)
                if not was:
                    return {"error": "no such change set"}, 404
                if was["state"] == SetState.DONE and what in ("items", "decide", "apply", "revert", "hide", "view", "query"):
                    return {"error": "this set is done; reopen it to change it"}, 409
                if was["op"] == ChangeOp.VIEW and what in ("decide", "apply", "revert", "commit", "reopen"):
                    return {"error": "this album is not up for hiding"}, 409
                if was["kind"] == AlbumKind.LIVE and what == "items":
                    return {"error": "a live album holds what its search finds; change its search (query), not its photos"}, 409
                extra, t = {}, now()
                entry: dict | None = None        # the journal entry's data, when the write changed something
                if what == "rename":
                    title = str(body.get("title") or was["title"]).strip()[:200] or was["title"]
                    prompt = body.get("prompt", was["prompt"])
                    con.execute("UPDATE change_sets SET title=?, prompt=? WHERE id=?", (title, prompt, sid))
                    entry = {k: [old, new] for k, old, new in (("title", was["title"], title), ("prompt", was["prompt"], prompt))
                             if old != new} or None
                elif what == "query":
                    bad = "only a live album has a search" if was["kind"] != AlbumKind.LIVE else self.bad_query(body.get("query"))
                    if bad:
                        return {"error": bad}, 409 if was["kind"] != AlbumKind.LIVE else 400
                    con.execute("UPDATE change_sets SET query=? WHERE id=?", (json.dumps(body["query"]), sid))
                    entry = {"query": body["query"]} if body["query"] != was["query"] else None
                elif what == "hide":             # the album becomes a change set: reviewed, then applied, as any other
                    if was["op"] != ChangeOp.HIDE:
                        if was["kind"] == AlbumKind.LIVE:    # fixed as what it finds now: a review needs photos that stay put
                            extra = self.add_items(con, sid, self.members(con, was))
                        con.execute("UPDATE change_sets SET op='hide', kind='list', status='open' WHERE id=?", (sid,))
                        entry = {"added": extra.get("added", 0)}
                elif what == "view":             # a plain album again, once it hides nothing
                    if con.execute("SELECT 1 FROM hidden WHERE set_id=? LIMIT 1", (sid,)).fetchone():
                        return {"error": "this album is hiding photos; undo that first"}, 409
                    con.execute("UPDATE change_sets SET op='view', status='open', done_at=NULL WHERE id=?", (sid,))
                    con.execute("UPDATE change_items SET decision='pending', applied=0 WHERE set_id=?", (sid,))
                    entry = {} if was["op"] != ChangeOp.VIEW else None
                elif what == "items":
                    extra = self.add_items(con, sid, body.get("items"))
                    gone = [s for s in body.get("remove") or [] if isinstance(s, str)]
                    # a photo the set is hiding is shown again before it leaves the set
                    changed = bool(con.executemany("DELETE FROM hidden WHERE set_id=? AND sha256=?",
                                                   ((sid, s) for s in gone)).rowcount)
                    extra["removed"] = con.executemany("DELETE FROM change_items WHERE set_id=? AND sha256=?",
                                                       ((sid, s) for s in gone)).rowcount
                    entry = {"added": extra["added"], "removed": extra["removed"]} if extra["added"] or extra["removed"] else None
                elif what == "decide":
                    decision, shas = body.get("decision"), body.get("shas")
                    if decision not in DECISIONS or not isinstance(shas, list):
                        return {"error": "decide takes {shas: [...], decision: accept | reject | pending}"}, 400
                    extra["decided"] = con.executemany(
                        "UPDATE change_items SET decision=?, decided_at=? WHERE set_id=? AND sha256=? AND decision != ?",
                        ((decision, t, sid, s, decision) for s in shas if isinstance(s, str))).rowcount
                    entry = {"decision": decision, "decided": extra["decided"]} if extra["decided"] else None
                elif what == "apply":
                    rows = con.execute("SELECT sha256, decision, applied FROM change_items WHERE set_id=?", (sid,)).fetchall()
                    through = body.get("through")
                    if through is not None:   # only as far as this photo; what comes after it is left alone
                        if through not in {r[0] for r in rows}:
                            return {"error": "`through` is not a photo of this set"}, 400
                        cut = place(self.when(through), through)
                        rows = [r for r in rows if place(self.when(r[0]), r[0]) <= cut]
                    hide = [sha for sha, decision, applied in rows if decision != Decision.REJECT and not applied]
                    show = [sha for sha, decision, applied in rows if decision == Decision.REJECT and applied]
                    con.executemany("INSERT OR IGNORE INTO hidden VALUES (?,?,?)", ((s, sid, t) for s in hide))
                    con.executemany("DELETE FROM hidden WHERE sha256=? AND set_id=?", ((s, sid) for s in show))
                    con.executemany("UPDATE change_items SET applied=1 WHERE set_id=? AND sha256=?", ((sid, s) for s in hide))
                    con.executemany("UPDATE change_items SET applied=0 WHERE set_id=? AND sha256=?", ((sid, s) for s in show))
                    con.execute("UPDATE change_sets SET status='open', applied_at=? WHERE id=?", (t, sid))
                    extra, changed = {"hid": len(hide), "showed": len(show)}, bool(hide or show)
                    entry = ({**extra, "through": through} if changed or was["status"] != ChangeStatus.OPEN else None)
                elif what == "revert":
                    shown = con.execute("DELETE FROM hidden WHERE set_id=?", (sid,)).rowcount
                    con.execute("UPDATE change_items SET applied=0 WHERE set_id=?", (sid,))
                    con.execute("UPDATE change_sets SET status='reverted', reverted_at=? WHERE id=?", (t, sid))
                    extra, changed = {"showed": shown}, bool(shown)
                    entry = {"showed": shown} if shown or was["status"] != ChangeStatus.REVERTED else None
                elif what == "commit":
                    if was["todo"] or not was["n"]:
                        return {"error": "apply the set first: some photos are not yet as marked"
                                if was["n"] else "there is nothing in this set"}, 409
                    con.execute("UPDATE change_sets SET status='done', done_at=? WHERE id=?", (t, sid))
                    entry = {} if was["status"] != ChangeStatus.DONE else None
                elif what == "reopen":
                    if was["state"] == SetState.DONE:
                        con.execute("UPDATE change_sets SET status='open', done_at=NULL WHERE id=?", (sid,))
                        entry = {}
                elif what == "discard":          # marked, never deleted: its rows stay, and it is in no listing
                    if con.execute("SELECT 1 FROM hidden WHERE set_id=? LIMIT 1", (sid,)).fetchone():
                        return {"error": "this set is hiding photos; undo it first"}, 409
                    con.execute("UPDATE change_sets SET status=?, updated_at=? WHERE id=?", (ChangeStatus.DISCARDED, t, sid))
                    journal.append(con, actor, "album.discarded", f"album:{sid}", {"title": was["title"]})
                    con.commit()
                    return {"discarded": sid}, 200
                else:
                    return {"error": "not found"}, 404
                if entry is not None:
                    journal.append(con, actor, KINDS[what], f"album:{sid}", entry or None)
                con.execute("UPDATE change_sets SET updated_at=? WHERE id=?", (t, sid))
                con.commit()
                out = {**(self.summary(con, sid) or {}), **extra}
            finally:
                con.close()
        if changed:
            self.on_change()
        return {**out, "busy": bool(self.busy())}, 200

    # -- http ------------------------------------------------------------------------------------

    def handle(self, method: str, path: str, qs: dict, body: dict | None, who: str = "", actor: int | None = None):
        """One request -> (status, body bytes, content type, Cache-Control), or None if it isn't ours. `who` is
        the login kept as created_by; `actor` the member the request is for (members.id), for the journal."""
        js = lambda o, code=200: (code, json.dumps(o, separators=(",", ":")).encode(), "application/json", "no-store")
        if method == "GET" and re.fullmatch(r"/changes(/\d*)?", path):
            return 200, pages.page("changes.html"), "text/html; charset=utf-8", "no-cache"
        m = ROUTE.match(path)
        if not m:
            return None
        albums, sid, what = m[1] == "albums", int(m[2]) if m[2] else None, m[3]
        one = lambda k, d: int(v) if (v := (qs.get(k) or [""])[0]).isdigit() else d
        if method == "GET":
            got = (self.listing(albums) if sid is None else self.check(sid) if what == "check"
                   else self.photos(sid, one("cursor", 0), max(1, min(one("limit", 150), 500))) if what == "photos"
                   else self.detail(sid) if not what else None)
            return js(got) if got is not None else js({"error": "not found"}, 404)
        if method == "POST":
            if not isinstance(body, dict):
                return js({"error": "expected a JSON object"}, 400)
            if sid is None:
                return js(*self.create({"op": ChangeOp.VIEW, **body} if albums else body, who, actor))
            if what and what not in ("check", "photos"):
                return js(*self.write(sid, what, body, who, actor))
        return js({"error": "not found"}, 404)
