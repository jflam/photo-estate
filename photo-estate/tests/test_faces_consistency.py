"""faces.People keeps SQLite (faces, persons, face_not, face_later), its in-memory copies and the journal
(face-labels.jsonl) in agreement (issue #87): a merge moves everything of the merged person, deduped,
and leaves nothing naming them; a write that SQLite refuses changes nothing in memory and closes its
connection; renames and merges are journalled; and a request for a person a merge is deleting gets a
404, not a KeyError. A tiny synthetic faces DB with the real schema; no model, no ONNX. The race is
forced with threading.Event, never with sleeps.

  uv run pytest tests/test_faces_consistency.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import faces  # noqa: E402
from core import schema  # noqa: E402
from http_util import Response  # noqa: E402

GUARD = 10   # seconds: only so that a hang fails the test instead of the run
ALICE, ALLY, BOB = 1, 2, 3
F1, F2, F3, F4, F5 = 11, 12, 13, 14, 15     # face ids; one face per photo, sha "a".."e"


@pytest.fixture
def db(tmp_path, monkeypatch) -> str:
    """Alice (F1) and Ally (F2) named by you; F3-F5 unnamed. Ally has later rows for F3 and F4 (Alice
    also has F4: the duplicate), and not rows for F3 and F5 (Alice also has F5)."""
    monkeypatch.setattr(faces.People, "cluster", lambda self: None)   # no grouping thread
    path = str(tmp_path / "estate.db")
    con = faces.connect(path)
    schema.ensure(con, "catalog")
    rng = np.random.default_rng(87)
    for i, (f, sha) in enumerate(zip((F1, F2, F3, F4, F5), "abcde")):
        v = rng.standard_normal(512).astype(np.float32)
        v /= np.linalg.norm(v)
        con.execute("INSERT INTO assets(sha256, ext, kind, taken_at) VALUES (?,?,?,?)", (sha, ".jpg", "image", "2020-06-01"))
        con.execute("INSERT INTO faces(id, sha256, idx, model, x, y, w, h, score, px, vec, person_id, source)"
                    " VALUES (?,?,0,?,?,0.2,0.1,0.1,0.9,100,?,?,?)",
                    (f, sha, faces.MODEL, 0.1 * i, v.astype(np.float16).tobytes(),
                     {F1: ALICE, F2: ALLY}.get(f), "you" if f in (F1, F2) else None))
    con.executemany("INSERT INTO persons(id, name, created_at) VALUES (?,?,'')", [(ALICE, "Alice"), (ALLY, "Ally"), (BOB, "Bob")])
    con.executemany("INSERT INTO face_later VALUES (?,?,'')", [(F3, ALLY), (F4, ALLY), (F4, ALICE)])
    con.executemany("INSERT INTO face_not VALUES (?,?)", [(F3, ALLY), (F5, ALLY), (F5, ALICE)])
    con.commit()
    con.close()
    return path


def people(db: str) -> faces.People:
    return faces.People(db, "/nonexistent", [], log=lambda *a: None)


def memory(pe: faces.People) -> dict:
    """What People holds, by face id: comparable with a fresh load and with a snapshot."""
    fid = lambda ks: sorted(int(pe.ids[k]) for k in ks)
    return {"pid": pe.pid.tolist(), "src": pe.src.tolist(), "persons": {p: dict(i) for p, i in pe.persons.items()},
            "nots": {p: fid(s) for p, s in pe.nots.items() if s}, "later": {p: fid(s) for p, s in pe.later.items() if s}}


def tables(db: str) -> dict:
    con = sqlite3.connect(db)
    try:
        return {"faces": con.execute("SELECT id, person_id, source FROM faces ORDER BY id").fetchall(),
                "persons": con.execute("SELECT id, name, born FROM persons ORDER BY id").fetchall(),
                "face_not": con.execute("SELECT face_id, person_id FROM face_not ORDER BY 1, 2").fetchall(),
                "face_later": con.execute("SELECT face_id, person_id FROM face_later ORDER BY 1, 2").fetchall()}
    finally:
        con.close()


def journal(db: str) -> list[dict]:
    f = Path(db).parent / "face-labels.jsonl"
    return [json.loads(line) for line in f.read_text().splitlines()] if f.exists() else []


# -- (a) a merge moves everything --------------------------------------------------------------------

def test_merge_moves_later_and_not_rows_deduped(db):
    pe = people(db)
    assert pe.edit_person({"id": ALLY, "name": "alice"}) == {"ok": True, "id": ALICE}   # case-insensitive: a merge
    t = tables(db)
    assert t["face_later"] == [(F3, ALICE), (F4, ALICE)]            # F4 was put aside for both: one row
    assert t["face_not"] == [(F3, ALICE), (F5, ALICE)]
    assert [p for p, *_ in t["persons"]] == [ALICE, BOB]
    assert all(p != ALLY for _, p, _ in t["faces"]) and dict((f, p) for f, p, _ in t["faces"])[F2] == ALICE
    m = memory(pe)
    assert m["later"] == {ALICE: [F3, F4]} and m["nots"] == {ALICE: [F3, F5]} and ALLY not in m["persons"]
    assert ALLY not in pe.pid.tolist() and ALLY not in pe.sug.tolist()
    assert memory(people(db)) == m                                  # memory is what a fresh load reads


# -- (b) a write SQLite refuses changes nothing ------------------------------------------------------

@pytest.fixture
def tracked(monkeypatch):
    """Every connection faces.connect makes, so the test can see they were all closed."""
    made, real = [], faces.connect
    monkeypatch.setattr(faces, "connect", lambda path: made.append(real(path)) or made[-1])
    return made


def refuse(db: str, table: str) -> None:
    """From now on SQLite refuses every UPDATE of the table, as a full disk or a locked database would."""
    con = sqlite3.connect(db)
    con.execute(f"CREATE TRIGGER refuse BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT, 'database or disk is full'); END")
    con.commit()
    con.close()


@pytest.mark.parametrize("table, call", [
    ("faces", lambda pe: pe.label({"faces": [F3], "person": "Carol"})),            # a new person, then the faces write fails
    ("faces", lambda pe: pe.label({"faces": [F1], "person": None})),               # face_not row, then the faces write fails
    ("faces", lambda pe: pe.label({"undo": [F3], "later": [F5], "later_person": BOB})),
    ("faces", lambda pe: pe.label_many({"items": [{"later": [F5], "later_person": BOB},
                                                  {"faces": [F3], "person": "Carol"}]})),   # the page is one transaction
    ("persons", lambda pe: pe.edit_person({"id": ALLY, "name": "Allie"})),          # rename
    ("persons", lambda pe: pe.edit_person({"id": ALICE, "born": "1990"})),
    ("faces", lambda pe: pe.edit_person({"id": ALLY, "name": "Alice"})),            # merge
], ids=["name", "forget", "undo-later", "label-many", "rename", "born", "merge"])
def test_refused_write_leaves_memory_and_db_unchanged(db, tracked, table, call):
    pe = people(db)
    before, rows = memory(pe), tables(db)
    refuse(db, table)
    with pytest.raises(sqlite3.Error, match="disk is full"):
        call(pe)
    for con in tracked:                                              # none left open
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            con.execute("SELECT 1")
    assert memory(pe) == before and tables(db) == rows and journal(db) == []


# -- (c) renames and merges are journalled, and replay --------------------------------------------------

def replay(records: list[dict]) -> dict:
    """Who each journalled face is, by photo, from the records alone: what a rescan would rebuild (#31)."""
    who: dict = {}
    for r in records:
        if r["op"] == "name":
            who.update({f[0]: r["person"] for f in r["faces"]})
        elif r["op"] in ("forget", "undo"):
            for f in r["faces"]:
                who.pop(f[0], None)
        elif r["op"] in ("rename", "merge"):
            to = r["name"] if r["op"] == "rename" else r["into"]
            who = {s: to if n == r["person"] else n for s, n in who.items()}
    return who


def test_rename_and_merge_are_journalled(db):
    pe = people(db)
    pe.label({"faces": [F4], "person": "Ally"})
    pe.edit_person({"id": ALLY, "name": "Allie", "born": "2001-04"})
    pe.label({"faces": [F3], "person": "Allie"})
    pe.edit_person({"id": ALLY, "name": "Alice"})
    pe.edit_person({"id": ALICE, "name": "Alice"})                  # no change: no record
    recs = journal(db)
    assert all(isinstance(r.pop("ts"), str) for r in recs)
    assert [r for r in recs if r["op"] in ("rename", "merge", "born")] == [
        {"op": "born", "person": "Ally", "born": "2001-04", "faces": []},
        {"op": "rename", "person": "Ally", "name": "Allie", "faces": []},
        {"op": "merge", "person": "Allie", "into": "Alice", "faces": []}]
    named = {sha: name for sha, name in sqlite3.connect(db).execute(
        "SELECT f.sha256, p.name FROM faces f JOIN persons p ON p.id = f.person_id WHERE f.source='you'")}
    assert replay(recs) == {"c": "Alice", "d": "Alice"} == {s: named[s] for s in "cd"}


# -- (d) a request for a person a merge is deleting ------------------------------------------------------

class HeldLock:
    """People.lock, saying when a thread other than the test's has asked for it."""
    def __init__(self):
        self.inner, self.asked, self.main = threading.RLock(), threading.Event(), threading.current_thread()

    def __enter__(self):
        if threading.current_thread() is not self.main:
            self.asked.set()
        self.inner.acquire()

    def __exit__(self, *exc):
        self.inner.release()


@pytest.mark.parametrize("path", ["/api/people/review", "/api/people/person", "/api/people/odd"])
def test_request_during_merge_is_a_404(db, path):
    pe = people(db)
    pe.lock = lock = HeldLock()
    box: dict = {}

    def request():
        try:
            box["value"] = pe.handle("GET", path, {"person": [str(ALLY)]}, None)
        except BaseException as e:
            box["error"] = e
    t = threading.Thread(target=request, daemon=True)
    with lock:                                   # a merge holds the lock while the request arrives
        t.start()
        assert lock.asked.wait(GUARD)            # the request is past its checks, waiting for the lock
        pe.edit_person({"id": ALLY, "name": "Alice"})
    t.join(GUARD)
    assert not t.is_alive() and "error" not in box, box.get("error")
    assert box["value"] is None and Response.of(box["value"]).status == 404
    assert pe.handle("GET", "/api/people/photo", {"face": ["999"]}, None) is None
    assert pe.handle("GET", path, {"person": [str(ALICE)]}, None)[0] == 200
