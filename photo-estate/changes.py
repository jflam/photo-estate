"""Change sets: a bulk edit to the library, proposed first and applied only once a person has looked.

An agent (or a person) proposes a set of photos and what to do to them. Nothing in the library changes
until the set is applied. Before that, a person looks the set over on the review page (/changes/<id>)
and marks the exceptions: the photos to keep. Every photo not marked is done to as proposed, so the
review is a last look, not a decision per photo. Each mark is saved as it is made, so a review of ten
thousand photos can stop and carry on later. Applying can be done as often as wanted: it makes the
library match the marks made so far, for the whole set or only as far as the person has looked.
Undoing a set takes back everything it did, as one unit.

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
  POST /api/changes/<id>/discard        forget a set that hides nothing

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

import pages

SCHEMA = """
CREATE TABLE IF NOT EXISTS change_sets(
  id INTEGER PRIMARY KEY, title TEXT, prompt TEXT, op TEXT DEFAULT 'hide',
  status TEXT DEFAULT 'open',        -- open | reverted
  created_by TEXT, created_at TEXT, updated_at TEXT, applied_at TEXT, reverted_at TEXT);
CREATE TABLE IF NOT EXISTS change_items(
  set_id INT, sha256 TEXT,
  decision TEXT DEFAULT 'pending',   -- pending (nobody has said: as proposed) | accept | reject (keep it)
  applied INT DEFAULT 0,             -- 1 while this set is hiding the photo
  score REAL, reason TEXT, decided_at TEXT, PRIMARY KEY(set_id, sha256));
CREATE TABLE IF NOT EXISTS hidden(   -- a photo is hidden while any row names it
  sha256 TEXT, set_id INT, hidden_at TEXT, PRIMARY KEY(sha256, set_id));
"""
OPS = ("hide",)
DECISIONS = ("pending", "accept", "reject")
SHA = re.compile(r"^[0-9a-f]{64}$")
ROUTE = re.compile(r"^/api/changes(?:/(\d+)(?:/(items|decide|apply|revert|discard|check))?)?$")


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
    the app is still taking in the last change; `on_change()` is called when the hidden photos change."""

    def __init__(self, db: str, facts, shown, busy=lambda: False, on_change=lambda: None):
        self.db, self.facts, self.shown, self.busy, self.on_change = db, facts, shown, busy, on_change
        self.lock = threading.Lock()   # one writer at a time; reads take no lock (WAL)
        con = self.con()
        con.executescript(SCHEMA)
        con.commit()
        con.close()

    def con(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db, timeout=60)
        con.execute("PRAGMA journal_mode=WAL")
        return con

    # -- reading ---------------------------------------------------------------------------------

    def summary(self, con, sid: int) -> dict | None:
        row = con.execute("SELECT id, title, prompt, op, status, created_by, created_at, updated_at, applied_at,"
                          " reverted_at FROM change_sets WHERE id=?", (sid,)).fetchone()
        if not row:
            return None
        s = dict(zip(("id", "title", "prompt", "op", "status", "created_by", "created_at", "updated_at",
                      "applied_at", "reverted_at"), row))
        n = {d: 0 for d in DECISIONS}
        applied = todo = 0
        for decision, was_applied, k in con.execute(
                "SELECT decision, applied, count(*) FROM change_items WHERE set_id=? GROUP BY 1, 2", (sid,)):
            n[decision] += k
            applied += k if was_applied else 0
            todo += k if (decision != "reject") != bool(was_applied) else 0
        total = sum(n.values())
        # what the set has come to: nothing in it, undone, still being worked on, or finished
        s["state"] = ("empty" if not total else "reverted" if s["status"] == "reverted" and not applied
                      else "review" if todo else "applied")
        return {**s, "n": total, **n, "applied": applied, "todo": todo}

    def listing(self) -> dict:
        con = self.con()
        try:
            ids = [r[0] for r in con.execute("SELECT id FROM change_sets ORDER BY id DESC")]
            sets = [self.summary(con, i) for i in ids]
            for s in sets:   # a few photos to show on the set's card
                s["cover"] = [r[0] for r in con.execute(
                    "SELECT sha256 FROM change_items WHERE set_id=? ORDER BY score DESC, sha256 LIMIT 6", (s["id"],))]
            return {"sets": sets, "hidden": len(hidden_shas(con)), "busy": bool(self.busy())}
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
        for it in items or []:
            if isinstance(it, str):
                it = {"sha": it}
            sha = it.get("sha") if isinstance(it, dict) else None
            if not isinstance(sha, str) or not SHA.match(sha) or self.facts(sha) is None:
                unknown += 1
                continue
            score = it.get("score")
            added += con.execute("INSERT OR IGNORE INTO change_items(set_id, sha256, score, reason) VALUES (?,?,?,?)",
                                 (sid, sha, float(score) if isinstance(score, (int, float)) else None,
                                  str(it["reason"])[:500] if it.get("reason") else None)).rowcount
        return {"added": added, "unknown": unknown}

    def create(self, body: dict, who: str = "") -> tuple[dict, int]:
        title = str(body.get("title") or "").strip()
        op = body.get("op") or "hide"
        if not title:
            return {"error": "a change set needs a title"}, 400
        if op not in OPS:
            return {"error": f"unknown operation {op!r}; known: {', '.join(OPS)}"}, 400
        with self.lock:
            con = self.con()
            try:
                t = now()
                sid = con.execute("INSERT INTO change_sets(title, prompt, op, created_by, created_at, updated_at)"
                                  " VALUES (?,?,?,?,?,?)", (title[:200], body.get("prompt"), op, who or None, t, t)).lastrowid
                got = self.add_items(con, sid, body.get("items"))
                con.commit()
                return {**self.summary(con, sid), **got}, 201
            finally:
                con.close()

    def write(self, sid: int, what: str, body: dict) -> tuple[dict, int]:
        changed = False
        with self.lock:
            con = self.con()
            try:
                if not self.summary(con, sid):
                    return {"error": "no such change set"}, 404
                extra, t = {}, now()
                if what == "items":
                    extra = self.add_items(con, sid, body.get("items"))
                    gone = [s for s in body.get("remove") or [] if isinstance(s, str)]
                    # a photo the set is hiding is shown again before it leaves the set
                    changed = bool(con.executemany("DELETE FROM hidden WHERE set_id=? AND sha256=?",
                                                   ((sid, s) for s in gone)).rowcount)
                    extra["removed"] = con.executemany("DELETE FROM change_items WHERE set_id=? AND sha256=?",
                                                       ((sid, s) for s in gone)).rowcount
                elif what == "decide":
                    decision, shas = body.get("decision"), body.get("shas")
                    if decision not in DECISIONS or not isinstance(shas, list):
                        return {"error": "decide takes {shas: [...], decision: accept | reject | pending}"}, 400
                    extra["decided"] = con.executemany(
                        "UPDATE change_items SET decision=?, decided_at=? WHERE set_id=? AND sha256=? AND decision != ?",
                        ((decision, t, sid, s, decision) for s in shas if isinstance(s, str))).rowcount
                elif what == "apply":
                    rows = con.execute("SELECT sha256, decision, applied FROM change_items WHERE set_id=?", (sid,)).fetchall()
                    through = body.get("through")
                    if through is not None:   # only as far as this photo; what comes after it is left alone
                        if through not in {r[0] for r in rows}:
                            return {"error": "`through` is not a photo of this set"}, 400
                        cut = place(self.when(through), through)
                        rows = [r for r in rows if place(self.when(r[0]), r[0]) <= cut]
                    hide = [sha for sha, decision, applied in rows if decision != "reject" and not applied]
                    show = [sha for sha, decision, applied in rows if decision == "reject" and applied]
                    con.executemany("INSERT OR IGNORE INTO hidden VALUES (?,?,?)", ((s, sid, t) for s in hide))
                    con.executemany("DELETE FROM hidden WHERE sha256=? AND set_id=?", ((s, sid) for s in show))
                    con.executemany("UPDATE change_items SET applied=1 WHERE set_id=? AND sha256=?", ((sid, s) for s in hide))
                    con.executemany("UPDATE change_items SET applied=0 WHERE set_id=? AND sha256=?", ((sid, s) for s in show))
                    con.execute("UPDATE change_sets SET status='open', applied_at=? WHERE id=?", (t, sid))
                    extra, changed = {"hid": len(hide), "showed": len(show)}, bool(hide or show)
                elif what == "revert":
                    shown = con.execute("DELETE FROM hidden WHERE set_id=?", (sid,)).rowcount
                    con.execute("UPDATE change_items SET applied=0 WHERE set_id=?", (sid,))
                    con.execute("UPDATE change_sets SET status='reverted', reverted_at=? WHERE id=?", (t, sid))
                    extra, changed = {"showed": shown}, bool(shown)
                elif what == "discard":
                    if con.execute("SELECT 1 FROM hidden WHERE set_id=? LIMIT 1", (sid,)).fetchone():
                        return {"error": "this set is hiding photos; undo it first"}, 409
                    con.execute("DELETE FROM change_items WHERE set_id=?", (sid,))
                    con.execute("DELETE FROM change_sets WHERE id=?", (sid,))
                    con.commit()
                    return {"discarded": sid}, 200
                else:
                    return {"error": "not found"}, 404
                con.execute("UPDATE change_sets SET updated_at=? WHERE id=?", (t, sid))
                con.commit()
                out = {**self.summary(con, sid), **extra}
            finally:
                con.close()
        if changed:
            self.on_change()
        return {**out, "busy": bool(self.busy())}, 200

    # -- http ------------------------------------------------------------------------------------

    def handle(self, method: str, path: str, qs: dict, body: dict | None, who: str = ""):
        """One request -> (status, body bytes, content type, Cache-Control), or None if it isn't ours."""
        js = lambda o, code=200: (code, json.dumps(o, separators=(",", ":")).encode(), "application/json", "no-store")
        if method == "GET" and re.fullmatch(r"/changes(/\d*)?", path):
            return 200, pages.page("changes.html"), "text/html; charset=utf-8", "no-cache"
        m = ROUTE.match(path)
        if not m:
            return None
        sid, what = int(m[1]) if m[1] else None, m[2]
        if method == "GET":
            got = self.listing() if sid is None else self.check(sid) if what == "check" else self.detail(sid) if not what else None
            return js(got) if got is not None else js({"error": "not found"}, 404)
        if method == "POST":
            if not isinstance(body, dict):
                return js({"error": "expected a JSON object"}, 400)
            if sid is None:
                return js(*self.create(body, who))
            if what and what != "check":
                return js(*self.write(sid, what, body))
        return js({"error": "not found"}, 404)
