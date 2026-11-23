"""journal.py, and the one entry each write appends: an entry lands in its caller's transaction (or not at
all), `since` reads them in order, the table refuses UPDATE and DELETE, and change sets and albums,
favourites, People and tasks each append one entry per action, naming the member it was done for.

  uv run pytest tests/test_journal.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import embed  # noqa: E402
import faces  # noqa: E402
import journal  # noqa: E402
import tasks  # noqa: E402
from core import schema  # noqa: E402
from core.states import TaskStatus  # noqa: E402
from photo_app import PhotoApp  # noqa: E402

AS_BEN = {"X-Demo-Member": "ben"}


def entries(db, after: int = 0) -> list[tuple]:
    con = sqlite3.connect(db)
    try:
        return [(e["actor"], e["acting_for"], e["kind"], e["subject"], e["data"]) for e in journal.since(con, after)]
    finally:
        con.close()


def last_id(db) -> int:
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT coalesce(max(id), 0) FROM journal").fetchone()[0]
    finally:
        con.close()


# -- the module ---------------------------------------------------------------------------------------

def test_append_is_in_the_callers_transaction_and_since_reads_in_order(tmp_path):
    con = sqlite3.connect(tmp_path / "estate.db")
    schema.ensure(con, "members")
    with pytest.raises(RuntimeError):
        with con:                      # the change it records fails: the entry goes with it
            journal.append(con, 1, "album.created", "album:1", {"title": "x"})
            raise RuntimeError("the write after it failed")
    assert journal.since(con) == []
    with con:
        a = journal.append(con, 1, "album.created", "album:1", {"title": "Ünïcode", "n": 2})
        b = journal.append(con, None, "task.finished", "task:3", None, acting_for=2)
    got = journal.since(con)
    assert [(e["id"], e["actor"], e["acting_for"], e["kind"], e["subject"], e["data"]) for e in got] == [
        (a, 1, None, "album.created", "album:1", {"title": "Ünïcode", "n": 2}),
        (b, None, 2, "task.finished", "task:3", None)]
    assert got[0]["at"].endswith("Z") and journal.since(con, a) == got[1:] and journal.since(con, 0, limit=1) == got[:1]
    con.close()


def test_the_journal_refuses_update_and_delete(tmp_path):
    con = sqlite3.connect(tmp_path / "estate.db")
    schema.ensure(con, "members")
    with con:
        journal.append(con, 1, "member.added", "member:1")
    for sql in ("UPDATE journal SET kind='x'", "UPDATE journal SET data=NULL WHERE id=1", "DELETE FROM journal",
                "DELETE FROM journal WHERE id=1"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            con.execute(sql)
    assert [e["kind"] for e in journal.since(con)] == ["member.added"]
    con.close()


# -- change sets, albums and favourites, through the app ------------------------------------------------

@pytest.fixture(scope="module")
def app():
    a = embed.parser().parse_args(["serve", "--demo", "--no-pregenerate"])
    app = PhotoApp(a)            # start() is not called: no search model, no faces, no threads
    app.feed_update()
    return app


def call(app, path, body=None, headers=AS_BEN, expect=200):
    r = app.request("POST" if body is not None else "GET", path, headers, body=body if body is not None else b"")
    assert r.status == expect, (path, r.status, r.body[:300])
    return r.json()


def test_a_change_set_is_one_entry_per_action_by_the_member_it_was_for(app):
    db, ben = app.a.db, app.roster["ben"].id
    shas = list(app.live.cat.shas[:12])
    start = last_id(db)
    sid = call(app, "/api/changes", {"title": "Twelve", "items": [{"sha": s} for s in shas]}, expect=201)["id"]
    call(app, f"/api/changes/{sid}/decide", {"shas": shas[:2], "decision": "reject"})
    call(app, f"/api/changes/{sid}/decide", {"shas": shas[:2], "decision": "reject"})    # the same again: nothing changes
    call(app, f"/api/changes/{sid}/apply", {})
    call(app, f"/api/changes/{sid}/commit", {})
    call(app, f"/api/changes/{sid}/reopen", {})
    call(app, f"/api/changes/{sid}/revert", {})
    call(app, f"/api/changes/{sid}/items", {"remove": [shas[11]]})
    call(app, f"/api/albums/{sid}/rename", {"title": "Eleven"})
    call(app, f"/api/albums/{sid}/view", {})
    call(app, f"/api/albums/{sid}/favorite", {"fav": True})
    call(app, "/api/favorite", {"sha": shas[0], "fav": False})
    call(app, f"/api/albums/{sid}/discard", {})
    a = f"album:{sid}"
    assert entries(db, start) == [
        (ben, None, "album.created", a, {"title": "Twelve", "op": "hide", "kind": "list", "added": 12}),
        (ben, None, "album.decided", a, {"decision": "reject", "decided": 2}),
        (ben, None, "album.applied", a, {"hid": 10, "showed": 0, "through": None}),    # one entry, not one per photo
        (ben, None, "album.committed", a, None),
        (ben, None, "album.reopened", a, None),
        (ben, None, "album.reverted", a, {"showed": 10}),
        (ben, None, "album.items", a, {"added": 0, "removed": 1}),
        (ben, None, "album.renamed", a, {"title": ["Twelve", "Eleven"]}),
        (ben, None, "album.viewed", a, None),
        (ben, None, "favorite.set", a, {"fav": True, "photos": 11}),
        (ben, None, "favorite.set", shas[0], {"fav": False, "photos": 1}),
        (ben, None, "album.discarded", a, {"title": "Eleven"})]


def test_discarding_marks_the_set_and_keeps_its_rows(app):
    db = app.a.db
    shas = list(app.live.cat.shas[20:23])
    sid = call(app, "/api/albums", {"title": "Put away", "items": [{"sha": s} for s in shas]}, expect=201)["id"]
    other = call(app, "/api/albums", {"title": "Kept"}, expect=201)["id"]
    assert call(app, f"/api/albums/{sid}/discard", {}) == {"discarded": sid}
    con = sqlite3.connect(db)
    try:
        assert con.execute("SELECT title, status FROM change_sets WHERE id=?", (sid,)).fetchone() == ("Put away", "discarded")
        assert con.execute("SELECT count(*) FROM change_items WHERE set_id=?", (sid,)).fetchone()[0] == 3
    finally:
        con.close()
    assert sid not in [s["id"] for s in call(app, "/api/albums")["sets"]] and other in [s["id"] for s in call(app, "/api/albums")["sets"]]
    assert sid not in [s["id"] for s in call(app, "/api/changes")["sets"]]
    for path in (f"/api/albums/{sid}", f"/api/albums/{sid}/photos", f"/api/changes/{sid}/check"):
        call(app, path, expect=404)
    call(app, f"/api/albums/{sid}/discard", {}, expect=404)          # gone, as far as anyone asking can tell
    call(app, f"/api/albums/{sid}/items", {"items": [{"sha": shas[0]}]}, expect=404)
    nxt = call(app, "/api/albums", {"title": "After"}, expect=201)["id"]
    assert nxt > max(sid, other), "a discarded set's id is never given to another"


def test_the_mcp_tools_write_as_the_member_that_called(app):
    db, ben = app.a.db, app.roster["ben"].id
    start = last_id(db)
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "create_album", "arguments": {"title": "By an agent"}}}
    r = app.request("POST", "/mcp", {**AS_BEN, "Content-Type": "application/json"}, body=msg)
    assert r.status == 200 and not r.json()["result"]["isError"], r.body
    assert [(e[0], e[2]) for e in entries(db, start)] == [(ben, "album.created")]


# -- tasks ----------------------------------------------------------------------------------------------

class Quiet(tasks.Tasks):
    def worker(self):           # no agent: the test moves the tasks itself
        pass


def test_tasks_journal_who_asked_and_how_each_ended(tmp_path):
    db = str(tmp_path / "estate.db")
    t = Quiet(db, "http://127.0.0.1:1/mcp", command=sys.executable)
    one, _ = t.submit("hide the blurry ones", "ben@example.com", acting_for=5)
    two, _ = t.submit("and the dark ones", "", acting_for=6)
    three, _ = t.submit("a third", "", acting_for=5)
    t.cancel(one["id"], 7)
    assert t.move(two["id"], TaskStatus.RUNNING, TaskStatus.QUEUED)
    assert t.move(two["id"], TaskStatus.DONE, TaskStatus.RUNNING, result="did it")
    assert not t.move(two["id"], TaskStatus.FAILED, TaskStatus.RUNNING)                  # lost: no second entry
    assert t.move(three["id"], TaskStatus.RUNNING, TaskStatus.QUEUED)
    Quiet(db, "http://127.0.0.1:1/mcp", command=sys.executable)                       # a restart: running -> failed
    why = "the app restarted while this was running"
    assert entries(db) == [
        (5, None, "task.submitted", f"task:{one['id']}", {"prompt": "hide the blurry ones"}),
        (6, None, "task.submitted", f"task:{two['id']}", {"prompt": "and the dark ones"}),
        (5, None, "task.submitted", f"task:{three['id']}", {"prompt": "a third"}),
        (7, 5, "task.finished", f"task:{one['id']}", {"status": "cancelled"}),
        (None, 6, "task.finished", f"task:{two['id']}", {"status": "done"}),
        (None, 5, "task.finished", f"task:{three['id']}", {"status": "failed", "error": why})]


# -- People ---------------------------------------------------------------------------------------------

@pytest.fixture
def people(tmp_path, monkeypatch):
    """Two photos with a face each, Alice and Bob named, as test_faces_consistency.py makes them."""
    monkeypatch.setattr(faces.People, "cluster", lambda self: None)
    db = str(tmp_path / "estate.db")
    con = faces.connect(db)
    schema.ensure(con, "catalog")
    rng = np.random.default_rng(2)
    for f, sha, p in ((11, "a", 1), (12, "b", None), (13, "c", 2)):
        v = rng.standard_normal(512).astype(np.float32)
        con.execute("INSERT INTO assets(sha256, ext, kind, taken_at) VALUES (?,?,?,?)", (sha, ".jpg", "image", "2020-06-01"))
        con.execute("INSERT INTO faces(id, sha256, idx, model, x, y, w, h, score, px, vec, person_id, source)"
                    " VALUES (?,?,0,?,0.1,0.2,0.1,0.1,0.9,100,?,?,?)",
                    (f, sha, faces.MODEL, (v / np.linalg.norm(v)).astype(np.float16).tobytes(), p, "you" if p else None))
    con.executemany("INSERT INTO persons(id, name, created_at) VALUES (?,?,'')", [(1, "Alice"), (2, "Bob")])
    con.commit()
    con.close()
    return db, faces.People(db, "/nonexistent", [], log=lambda *a: None)


def test_people_writes_are_one_entry_beside_the_face_labels_log(people):
    db, pe = people
    pe.label({"faces": [12], "person": "Carol"}, actor=4)
    pe.label_many({"items": [{"faces": [12], "person": None}, {"later": [11], "later_person": 2}]}, actor=4)
    pe.label({"faces": []}, actor=4)                                     # nothing said: nothing journaled
    pe.edit_person({"id": 2, "born": "1990", "name": "Robert"}, actor=5)
    pe.edit_person({"id": 2, "name": "alice"}, actor=5)                 # onto an existing name: a merge
    assert entries(db) == [
        (4, None, "faces.labeled", "people", {"ops": [{"op": "name", "faces": 1, "person": "Carol"}]}),
        (4, None, "faces.labeled", "people", {"ops": [{"op": "forget", "faces": 1}, {"op": "later", "faces": 1, "person": "Bob"}]}),
        (5, None, "person.changed", "person:2", {"ops": [{"op": "born", "person": "Bob", "born": "1990"},
                                                          {"op": "rename", "person": "Bob", "name": "Robert"}]}),
        (5, None, "person.changed", "person:2", {"ops": [{"op": "merge", "person": "Robert", "into": "Alice"}]})]
    lines = [json.loads(x) for x in (Path(db).parent / "face-labels.jsonl").read_text().splitlines()]
    assert [x["op"] for x in lines] == ["name", "forget", "later", "born", "rename", "merge"], "face-labels.jsonl is kept as it was"


def test_a_people_write_sqlite_refuses_journals_nothing(people):
    db, pe = people
    con = sqlite3.connect(db)
    con.execute("CREATE TRIGGER refuse BEFORE UPDATE ON faces BEGIN SELECT RAISE(ABORT, 'disk full'); END")
    con.commit()
    con.close()
    with pytest.raises(sqlite3.IntegrityError):
        pe.label({"faces": [12], "person": "Carol"}, actor=4)
    assert entries(db) == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
