"""Who each face is, in the catalog and in the People page's memory, while People is loaded again beside the one
answering (faces.People; PhotoApp.load_people, people_page, hidden_changed, reload_catalog): each way the model of it
(specs/People.tla, checked by TLC) found the two could part, replayed on a real PhotoApp on the demo library, in
process, with real requests as its members (Ada the sysadmin, Ben, Cleo); then what the model shows the code does where
the owner has a question to decide (those tests say what is, not what must be).

The demo has no faces, and the face model is not needed to name them: each test writes a few faces, with made-up
vectors, onto the demo's photos of 2016 in the app's scratch copy of the catalog (never demo/), as `faces.py run` would,
and the app loads a real People from it. Pat has six faces named; f and four more look alike (a group nobody has
named), and four others another group; Quinn is a person with no face yet.

Each race holds one step of a load (People.load, its read; seed; the clustering thread) or of a request (People.label,
after it took the People answering and before its lock) while the other runs on, with threading.Event, never with
sleeps; a wait's timeout (GUARD) only stops a regression from hanging the run, and fails the test.

  uv run pytest tests/test_people_states.py
"""
from __future__ import annotations

import sqlite3
import sys
import threading
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import faces  # noqa: E402
from core.states import ClusterState  # noqa: E402
from test_multiplayer import ADA, BEN, CLEO, call, mid, settle  # noqa: E402
from test_private import share  # noqa: E402
from test_reload_states import finish, in_thread, make  # noqa: E402

GUARD = 20   # seconds: only so that a hang fails the test instead of the run
PAT, QUINN = 1, 2
F = 10                               # the face Ada names
GROUP0 = [10, 11, 12, 13, 14]        # f and four more like it: nobody has named them
GROUP1 = [21, 22, 23, 24]            # another group
GROUP2 = [31, 32, 33, 34, 35, 36]    # a bigger one, that a scan adds later
T = 40                               # a face like f's, in a photo Google tagged Quinn, that a scan adds later


def vector(axis: int, k: int) -> bytes:
    """A face of the person (or group) `axis`: its axis, and a little of its own."""
    v = np.zeros(512, np.float32)
    v[axis] = 1.0
    v += np.random.default_rng(axis * 100 + k).standard_normal(512).astype(np.float32) * 0.01
    return (v / np.linalg.norm(v)).astype(np.float16).tobytes()


def photos(app) -> list[str]:
    """The demo's photos of 2016 that every member sees (some are in a member's own filters), as they were the first
    time this was asked."""
    if hasattr(app, "_photos_2016"):
        return app._photos_2016
    con = sqlite3.connect(app.a.db)
    try:
        shas = [s for s, in con.execute("SELECT sha256 FROM assets WHERE kind='image' AND taken_at LIKE '2016%'"
                                        " ORDER BY sha256")]
    finally:
        con.close()
    views = [app.live.visible_for(mid(app, who)) for who in (ADA, BEN, CLEO)]
    app._photos_2016 = [s for s in shas if all(v.visible[v.cat.index[s]] for v in views)]
    return app._photos_2016


def add_faces(app, rows, tag: tuple[str, str] | None = None) -> None:
    """Faces (id, photo, axis, person, source) written as faces.py run writes them (person and source NULL for a new
    one), and a photo's Google people tag (sha, name) as `estate.py link` writes it."""
    con = faces.connect(app.a.db)
    con.executemany("INSERT INTO faces(id, sha256, idx, model, x, y, w, h, score, px, vec, person_id, source)"
                    " VALUES (?,?,0,?,0.3,0.3,0.3,0.3,0.9,100,?,?,?)",
                    [(i, sha, faces.MODEL, vector(axis, i), p, s) for i, sha, axis, p, s in rows])
    if tag:
        con.execute("UPDATE assets SET people=? WHERE sha256=?", (tag[1], tag[0]))
    con.commit()
    con.close()


def library(app) -> list[str]:
    """Pat's six faces (named by you), the group of f, the second group, and Quinn with no face; the catalog reloaded
    (it has faces now) and People loaded, its groups worked out. Returns the photos, by position."""
    s = photos(app)
    con = faces.connect(app.a.db)
    con.executemany("INSERT INTO persons(id, name, created_at) VALUES (?,?,'')", [(PAT, "Pat"), (QUINN, "Quinn")])
    con.commit()
    con.close()
    add_faces(app, [(i, s[k], 0, PAT, "you") for i, k in ((1, 0), (2, 1), (3, 2), (4, 21), (5, 22), (6, 23))]
              + [(i, s[3 + n], 1, None, None) for n, i in enumerate(GROUP0)]
              + [(i, s[8 + n], 2, None, None) for n, i in enumerate(GROUP1)])
    app.reload_catalog()
    assert app.live.cat.has_faces
    app.load_people()
    grouped(people(app))
    return s


def people(app) -> faces.People:
    p = app.people.snapshot().value
    assert p is not None
    return p


def catalog(app, ids) -> dict[int, tuple]:
    """faces.person_id, source by id, as the catalog has them."""
    con = sqlite3.connect(app.a.db)
    try:
        return {i: (p, s) for i, p, s in con.execute(
            f"SELECT id, person_id, source FROM faces WHERE id IN ({','.join('?' * len(ids))})", list(ids))}
    finally:
        con.close()


def shown(app, who, face: int) -> dict | None:
    """GET /api/people/photo?face=: the face as `who` is shown it, or None (a 404: not in People, or not theirs)."""
    r = app.request("GET", f"/api/people/photo?face={face}", headers=who, body=b"")
    return r.json()["face"] if r.status == 200 else None


def groups(app, who) -> list[dict]:
    got = call(app, "GET", "/api/people/unknown?limit=20", who)
    assert got["state"] == ClusterState.READY, got
    return got["groups"]


# -- holding a step -----------------------------------------------------------------------------------------------

CLUSTERED: dict[int, threading.Event] = {}


def grouped(p: faces.People) -> None:
    """Wait for this People's clustering thread to end."""
    assert CLUSTERED.setdefault(id(p), threading.Event()).wait(GUARD), "the faces were not grouped"


class Hold:
    """The next call of a People method, held before or after the method itself runs, until go()."""
    def __init__(self, monkeypatch, name: str, after: bool):
        self.reached, self.went, self.armed, self.lock = threading.Event(), threading.Event(), True, threading.Lock()
        real = getattr(faces.People, name)

        def held(pe, *a, **k):
            mine = self.take()
            if mine and not after:
                self.wait()
            got = real(pe, *a, **k)
            if mine and after:
                self.wait()
            return got
        monkeypatch.setattr(faces.People, name, held)

    def take(self) -> bool:
        with self.lock:
            mine, self.armed = self.armed, False
            return mine

    def wait(self) -> None:
        self.reached.set()
        assert self.went.wait(GUARD), "never let go"

    def go(self) -> None:
        self.went.set()


@pytest.fixture
def app(monkeypatch):
    real = faces.People.cluster

    def cluster(pe):
        try:
            real(pe)
        finally:
            CLUSTERED.setdefault(id(pe), threading.Event()).set()
    monkeypatch.setattr(faces.People, "cluster", cluster)
    app = make()
    yield app
    app.tasks.stop()


def load_held(app, monkeypatch, step: str = "load", after: bool = True):
    """load_people on a thread of its own, held at `step` of the new People (after People.load: its read and seed;
    before seed: its read only). Returns (go, done)."""
    h = Hold(monkeypatch, step, after)
    t, box = in_thread(app.load_people)
    assert h.reached.wait(GUARD), "the load did not get there"
    return h.go, lambda: finish(t, box)


def label(app, who, body, expect=200):
    return call(app, "POST", "/api/people/label", who, body, expect=expect)


# -- the normal path ---------------------------------------------------------------------------------------------

def test_one_at_a_time_people_is_what_the_catalog_says(app):
    """What must not change: Ada names f, and it is named in the catalog and on People; Cleo names a group she is shown,
    and its faces are named, f among them no more (named already); Ben makes f's photo private and People is loaded
    again without it, nobody else being shown f meanwhile; he releases it and f is back. Once all is done, People's
    memory is what a People read afresh from the catalog holds."""
    s = library(app)
    label(app, ADA, {"faces": [F], "person": PAT})
    assert catalog(app, [F])[F] == (PAT, "you") and shown(app, CLEO, F)["person"] == PAT
    g = groups(app, CLEO)
    assert [sorted(f["id"] for f in x["faces"]) for x in g] == [GROUP0[1:], GROUP1]
    label(app, CLEO, {"cluster": g[0]["cluster"], "person": "Quinn"})
    assert {i: v for i, v in catalog(app, GROUP0).items()} == {F: (PAT, "you"), **{i: (QUINN, "you") for i in GROUP0[1:]}}
    share(app, s[3], BEN)
    call(app, "POST", "/api/private", BEN, {"shas": [s[3]], "private": True})
    assert shown(app, CLEO, F) is None and shown(app, ADA, F) is None        # before People is loaded again
    settle(app)
    assert F not in people(app).row
    call(app, "POST", "/api/private", BEN, {"shas": [s[3]], "private": False})
    settle(app)
    assert shown(app, CLEO, F)["person"] == PAT
    fresh = faces.People(app.a.db, app.a.store, [app.cache], log=lambda *a: None)
    p = people(app)
    assert p.pid.tolist() == fresh.pid.tolist() and p.src.tolist() == fresh.src.tolist() and p.persons == fresh.persons


# -- a load beside the People answering --------------------------------------------------------------------------

def test_a_name_given_while_people_loads_is_kept_in_the_catalog(app, monkeypatch):
    """NameKept: TLC's trace. A scan adds T, a face like f's in a photo Google tagged Quinn, and People is loaded again
    (the ingest's on_faces). The load reads the catalog (f unnamed) and names T Quinn (seed); before it works out the
    automatic names (rescore), Ada names f Pat on the People answering, in the catalog. The load's rescore then finds f
    close to Quinn and writes Quinn (auto) over Ada's word, which is gone from the catalog: People and every later one
    say Quinn, and a rescore may yet take that away. A load writes no name where the catalog has a member's."""
    s = library(app)
    add_faces(app, [(T, s[12], 1, None, None)], tag=(s[12], "Quinn"))
    go, done = load_held(app, monkeypatch)
    label(app, ADA, {"faces": [F], "person": PAT})
    go()
    done()
    assert catalog(app, [T])[T] == (QUINN, "google")
    assert catalog(app, [F])[F] == (PAT, "you")
    assert shown(app, ADA, F)["person"] == PAT


def test_google_names_no_face_a_member_named_while_people_loads(app, monkeypatch):
    """NameKept, through seed: f's photo has just been tagged Quinn (estate.py link), and People is loading. It has
    read the catalog (f unnamed); Ada names f Pat; the load's seed then names f Quinn from the tag (google) over her
    word. A load writes no name where the catalog has a member's."""
    s = library(app)
    add_faces(app, [], tag=(s[3], "Quinn"))
    go, done = load_held(app, monkeypatch, "seed", after=False)
    label(app, ADA, {"faces": [F], "person": PAT})
    go()
    done()
    assert catalog(app, [F])[F] == (PAT, "you")
    assert shown(app, ADA, F)["person"] == PAT


def test_google_names_no_face_to_someone_a_member_said_it_is_not(app, monkeypatch):
    """NameKept, through seed: as above, but Ada says f is not Quinn (face_not) while the load reads. The tag then names
    f Quinn (google, which nothing works out again) though she said it is not. A load names no face to a person the
    catalog says it is not."""
    s = library(app)
    add_faces(app, [], tag=(s[3], "Quinn"))
    go, done = load_held(app, monkeypatch, "seed", after=False)
    label(app, ADA, {"not": [F], "not_person": QUINN})
    go()
    done()
    assert catalog(app, [F])[F] == (None, None)
    assert shown(app, ADA, F)["person"] == -1


def test_a_name_given_while_people_loads_is_in_the_people_that_answers_next(app, monkeypatch):
    """WordsAsTheCatalog: People is loading (any reason: an ingest's faces, a private photo) and has read the catalog;
    Ada names f Pat on the People answering, in the catalog and in its memory. The new one, read before her word, is
    swapped in without it: People says f is nobody, the catalog says Pat, until the next load. The new one takes the
    names written while it loaded."""
    library(app)
    go, done = load_held(app, monkeypatch)
    label(app, ADA, {"faces": [F], "person": PAT})
    go()
    done()
    assert catalog(app, [F])[F] == (PAT, "you")
    assert shown(app, ADA, F)["person"] == PAT
    assert F not in [f["id"] for g in groups(app, CLEO) for f in g["faces"]]


WRITES = {"label": ("/api/people/label", {"faces": [F], "person": PAT}),
          "label_many": ("/api/people/label-many", {"items": [{"faces": [F], "person": PAT}]}),
          "edit_person": ("/api/people/person", {"id": PAT, "name": "Patricia"})}


@pytest.mark.parametrize("write", WRITES)
def test_a_change_made_to_the_people_being_replaced_reaches_the_one_that_replaced_it(app, monkeypatch, write):
    """WordsAsTheCatalog and ShownUnnamed: TLC's trace. Ada's request takes the People answering (PhotoApp.people_page)
    and, before it has its lock, a load swaps the next one in and its clustering puts f in a group. Ada's change (a
    name for f, a page of them, Pat renamed) then goes into the catalog and into the People replaced, which nothing
    reads. The People answering says f is nobody, and shows it to Cleo among the faces nobody has named (or still calls
    Pat Pat). A People replaced passes a change on to the one that replaced it."""
    library(app)
    h = Hold(monkeypatch, write, after=False)
    path, body = WRITES[write]
    t, box = in_thread(call, app, "POST", path, ADA, body)
    assert h.reached.wait(GUARD)
    app.load_people()
    grouped(people(app))
    h.go()
    finish(t, box)
    if write == "edit_person":
        assert {p["id"]: p["name"] for p in call(app, "GET", "/api/people", ADA)["people"]}[PAT] == "Patricia"
        return
    assert catalog(app, [F])[F] == (PAT, "you")
    assert shown(app, ADA, F)["person"] == PAT
    assert F not in [f["id"] for g in groups(app, CLEO) for f in g["faces"]]


def test_a_load_during_which_a_member_made_the_person_a_tag_names_still_loads(app, monkeypatch):
    """A scan adds T in a photo Google tagged "Zed", a person nobody has made yet, and People loads. After its read,
    Ada names f "Zed", a new person (persons.name is UNIQUE). The load's seed then makes "Zed" too: SQLite refuses it,
    the load fails, and People stays as it was (no T) until the next load. The tag's person is the one she made."""
    s = library(app)
    add_faces(app, [(T, s[12], 1, None, None)], tag=(s[12], "Zed"))
    before = people(app)
    go, done = load_held(app, monkeypatch, "seed", after=False)
    label(app, ADA, {"faces": [F], "person": "Zed"})
    go()
    done()
    assert people(app) is not before and app.people.snapshot().error is None
    zed = {f: p for f, (p, _) in catalog(app, [F, T]).items()}
    assert zed[F] == zed[T] and shown(app, ADA, T)["person"] == zed[F]


# -- a group named from the page ---------------------------------------------------------------------------------

def test_a_group_shown_by_the_people_before_is_not_named_as_if_it_were(app, monkeypatch):
    """GroupAsShown: TLC's trace. Cleo is shown the groups (cluster 0 is f's); People is loaded again (an ingest's
    faces, a private photo), and the new one is still grouping. She names group 0 "Quinn": the label's cluster is a
    place in the new People's groups, which has none yet, so nothing is named, and the answer says it was. A group
    that is not the People answering's own is refused, and nothing named."""
    library(app)
    n = groups(app, CLEO)[0]["cluster"]
    h = Hold(monkeypatch, "cluster", after=False)
    app.load_people()
    assert h.reached.wait(GUARD)
    try:
        label(app, CLEO, {"cluster": n, "person": "Quinn"}, expect=409)
        assert all(v == (None, None) for v in catalog(app, GROUP0).values())
    finally:
        h.go()
    grouped(people(app))


def test_a_group_shown_by_the_people_before_does_not_name_another(app):
    """GroupAsShown: Cleo is shown the groups (cluster 0 is f's group); a scan adds six faces alike, and People is loaded
    again; its groups are worked out, the new six first. She names group 0 "Quinn": the six she never saw are named
    Quinn, by her, and f's group not at all. A group that is not the People answering's own is refused."""
    s = library(app)
    n = groups(app, CLEO)[0]["cluster"]
    add_faces(app, [(i, s[13 + k], 3, None, None) for k, i in enumerate(GROUP2)])
    app.load_people()
    grouped(people(app))
    label(app, CLEO, {"cluster": n, "person": "Quinn"}, expect=409)
    assert all(v == (None, None) for v in catalog(app, GROUP0 + GROUP2).values())
    g = groups(app, CLEO)[0]                                 # what she is shown now is hers to name
    label(app, CLEO, {"cluster": g["cluster"], "person": "Quinn"})
    assert all(v == (QUINN, "you") for v in catalog(app, GROUP2).values())


# -- what People is built without --------------------------------------------------------------------------------

def private_with_a_reload(app, monkeypatch, sha: str, on: bool) -> threading.Event:
    """Ben's POST /api/private of `sha`, with a reload of the catalog (an ingest's photos are in) between his commit and
    his relook: the reload's swap reads the private rows again (with_private). Returns an Event set when a load of
    People that began after it has ended."""
    real_relook, real_load, loaded = app.relook, app.load_people, threading.Event()
    armed = [True]

    def relook():
        if armed.pop() if armed else False:
            app.reload_catalog()
        return real_relook()

    def load_people():
        real_load()
        loaded.set()
    monkeypatch.setattr(app, "relook", relook)
    monkeypatch.setattr(app, "load_people", load_people)
    call(app, "POST", "/api/private", BEN, {"shas": [sha], "private": on})
    settle(app)
    return loaded


def test_a_photo_released_while_the_catalog_reloads_is_on_people(app, monkeypatch):
    """AsTheCatalogOnceQuiet, the other way: f's photo is private to Ben, and People is built without it. He releases
    it, and an ingest's reload takes the release into the looks before his relook, which then finds nothing changed
    and does not load People. f is shown to nobody, its sharer included, until the next load of People (another
    ingest's faces, a private change, a restart). A reload that changes what People is built without loads People."""
    s = photos(app)
    share(app, s[3], BEN)
    call(app, "POST", "/api/private", BEN, {"shas": [s[3]], "private": True})
    settle(app)
    library(app)
    assert shown(app, BEN, F) is None and F not in people(app).row
    loaded = private_with_a_reload(app, monkeypatch, s[3], on=False)
    assert loaded.wait(GUARD)
    assert shown(app, ADA, F) is not None and shown(app, BEN, F) is not None


def test_a_photo_made_private_while_the_catalog_reloads_leaves_people(app, monkeypatch):
    """AsTheCatalogOnceQuiet: TLC's trace. Ben makes f's photo private, and an ingest's reload takes it into the looks
    before his relook, which then finds nothing changed: People is not loaded again, and keeps f's faces. Nobody else is
    shown them (each request's keep and see are the looks'), but People is not what it is said to be built without
    (its counts and its groups' guesses go on counting f) until the next load. People is loaded again."""
    s = library(app)
    share(app, s[3], BEN)
    loaded = private_with_a_reload(app, monkeypatch, s[3], on=True)
    for who in (ADA, CLEO):
        assert shown(app, who, F) is None
        assert app.request("GET", f"/face/{F}", headers=who, body=b"").status == 404
    assert loaded.wait(GUARD)
    assert F not in people(app).row


def test_a_face_of_a_photo_made_private_is_shown_to_nobody_else_before_people_loads_again(app, monkeypatch):
    """NoPrivateFaceShown: Ben makes f's photo private; the load of People it asks for is held, so the People answering
    still has f. After his answer nobody else is shown f by any People route: the photo, its crop, the timeline, the
    groups, the faces waiting for Pat, the covers. (Ben, its sharer, still is until the load: his own photo.)"""
    s = library(app)
    label(app, ADA, {"faces": [11], "person": PAT})        # f's group: one of them Pat's now, the rest "is this Pat?"
    share(app, s[3], BEN)
    h = Hold(monkeypatch, "load", after=False)
    call(app, "POST", "/api/private", BEN, {"shas": [s[3]], "private": True})
    assert h.reached.wait(GUARD)
    try:
        assert F in people(app).row
        for who in (ADA, CLEO):
            assert shown(app, who, F) is None
            assert app.request("GET", f"/face/{F}", headers=who, body=b"").status == 404
            ids = [f["id"] for fs in call(app, "GET", f"/api/people/person?person={PAT}", who)["years"].values() for f in fs]
            for route in ("review", "odd"):
                ids += [f["id"] for f in call(app, "GET", f"/api/people/{route}?person={PAT}", who)["faces"]]
            ids += [f["id"] for g in call(app, "GET", "/api/people/unknown?limit=20", who)["groups"] for f in g["faces"]]
            ids += [p["cover"] for p in call(app, "GET", "/api/people", who)["people"]]
            assert ids and F not in ids
        assert shown(app, BEN, F) is not None
    finally:
        h.go()
    settle(app)
    assert F not in people(app).row and shown(app, BEN, F) is None


# -- what the owner decided (issues #142 to #147): once "what is" tests ------------------------------------------

def ready(app, state=ClusterState.READY) -> dict:
    """GET /api/people/unknown once this People's grouping has come to `state`."""
    for _ in range(GUARD * 20):
        got = call(app, "GET", "/api/people/unknown?limit=20", CLEO)
        if got["state"] == state:
            return got
        threading.Event().wait(0.05)
    raise AssertionError(got)


def test_a_load_of_people_that_fails_is_tried_again(app, monkeypatch):
    """AsTheCatalogEvenAfterAFailure: Ben releases f's photo; the load of People that asks for raises (a locked
    catalog, out of memory). The People before keeps answering, without f. Before the fix nothing loaded it again
    until the next ingest's faces, private change or restart. The minute timer does (PhotoApp.mend): f is on People."""
    s = photos(app)
    share(app, s[3], BEN)
    call(app, "POST", "/api/private", BEN, {"shas": [s[3]], "private": True})
    settle(app)
    library(app)
    before, real = people(app), faces.People.load

    def broken(pe, con):
        monkeypatch.setattr(faces.People, "load", real)
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(faces.People, "load", broken)
    call(app, "POST", "/api/private", BEN, {"shas": [s[3]], "private": False})
    settle(app)
    assert people(app) is before and app.people.snapshot().error and app.people_state() == "ready" and app.people_failed
    assert shown(app, BEN, F) is None
    app.mend()["people"].join(GUARD)
    assert not app.people_failed and people(app) is not before and shown(app, BEN, F) is not None
    assert "people" not in app.mend()


def test_a_grouping_that_fails_is_tried_again(app, monkeypatch):
    """The clustering thread raises: /api/people/unknown says failed, with why. Before the fix that People never
    grouped again (and the page said only a restart would). The minute timer runs it again, on the same People."""
    real = faces.People.find_clusters
    monkeypatch.setattr(faces.People, "find_clusters", lambda pe: (_ for _ in ()).throw(MemoryError("no room")))
    library(app)
    got = ready(app, ClusterState.FAILED)
    assert "MemoryError" in got["error"] and got["groups"] == []
    was = people(app)
    assert "groups" in app.mend()                             # (still failing: it stays to be done)
    ready(app, ClusterState.FAILED)
    monkeypatch.setattr(faces.People, "find_clusters", real)
    assert "groups" in app.mend()
    assert ready(app)["groups"] and people(app) is was and "groups" not in app.mend()


def test_loads_of_people_asked_for_during_one_are_one_more(app, monkeypatch):
    """Issue #145: three loads are asked for while one runs (an ingest's faces, two private changes). Each returns at
    once, and one more load runs after the first, which reads all three's changes: two Peoples made, not four."""
    library(app)
    made, init = [], faces.People.__init__

    def counted(pe, *a, **k):
        made.append(pe)
        init(pe, *a, **k)
    monkeypatch.setattr(faces.People, "__init__", counted)
    go, done = load_held(app, monkeypatch)
    for _ in range(3):
        app.load_people()                                     # (returns at once: the one running takes note)
    assert len(made) == 1 and app.people_asked == "rerun"
    go()
    done()
    assert len(made) == 2 and app.people_asked == "idle" and people(app) is made[-1]


def test_a_face_id_freed_by_a_purge_names_no_other_face(app):
    """Issue #142: estate.py purge deletes a photo's faces; the People answering still has them until it is loaded
    again. The next scan's face is given the id the purge freed (faces.id is the largest plus one). Before the fix a
    label of the purged face, by id, named the new face in another photo. Every write says which photo it means."""
    s = library(app)
    con = faces.connect(app.a.db)
    con.execute("DELETE FROM faces WHERE id=?", (GROUP1[-1],))           # (GROUP1's last is the largest id)
    con.execute("INSERT INTO faces(sha256, idx, model, x, y, w, h, score, px, vec) VALUES (?,0,?,0.3,0.3,0.3,0.3,0.9,100,?)",
                (s[20], faces.MODEL, vector(5, 0)))
    con.commit()
    reused, = con.execute("SELECT id FROM faces WHERE sha256=?", (s[20],)).fetchone()
    con.close()
    assert reused == GROUP1[-1] and people(app).sha[people(app).row[reused]] == s[11]
    label(app, ADA, {"faces": [reused], "person": PAT})
    label(app, ADA, {"faces": [reused], "person": None})                  # (forget), and undo: none reaches it either
    label(app, ADA, {"undo": [reused]})
    label(app, ADA, {"faces": [reused], "person": PAT})
    con = sqlite3.connect(app.a.db)
    assert con.execute("SELECT sha256, person_id, source FROM faces WHERE id=?", (reused,)).fetchone() == (s[20], None, None)
    con.close()


def add_maybe_pats(app, rows) -> None:
    """Faces (id, photo) that resemble Pat enough to be asked about and not enough to be named without asking."""
    def maybe(i: int) -> bytes:
        v = np.zeros(512, np.float32)
        v[0], v[100 + i] = 0.4, (1 - 0.16) ** 0.5
        return v.astype(np.float16).tobytes()
    con = faces.connect(app.a.db)
    con.executemany("INSERT INTO faces(id, sha256, idx, model, x, y, w, h, score, px, vec) VALUES (?,?,0,?,0.3,0.3,0.3,0.3,0.9,100,?)",
                    [(i, sha, faces.MODEL, maybe(i)) for i, sha in rows])
    con.commit()
    con.close()


def waiting(app, who, person=PAT) -> dict:
    return call(app, "GET", f"/api/people/review?person={person}&limit=60", who)


def test_everything_waiting_is_what_waited_when_the_page_was_shown(app):
    """Issue #144: Ada opens Pat's faces to check: none wait. A scan adds two faces like Pat's and People is loaded
    again: they wait now. Her page's "name everything waiting" names nothing she was not shown (409: look again);
    asked again from the page as it is now, it names them."""
    s = library(app)
    before = waiting(app, ADA)
    add_maybe_pats(app, [(51, s[15]), (52, s[16])])
    app.load_people()
    assert not before["faces"] and catalog(app, [51, 52]) == {51: (None, None), 52: (None, None)}
    label(app, ADA, {"pending": {"person": PAT, "year": None, "as_of": before["as_of"]}, "person": PAT}, expect=409)
    label(app, ADA, {"pending": {"person": PAT, "year": None}, "person": PAT}, expect=409)      # (a page from before)
    assert catalog(app, [51, 52]) == {51: (None, None), 52: (None, None)}
    now_ = waiting(app, ADA)
    assert {f["id"] for f in now_["faces"]} == {51, 52}
    label(app, ADA, {"pending": {"person": PAT, "year": None, "as_of": now_["as_of"]}, "person": PAT})
    assert catalog(app, [51, 52]) == {51: (PAT, "you"), 52: (PAT, "you")}


def test_everything_waiting_names_no_face_in_a_photo_the_member_is_not_shown(app):
    """Issue #144: two faces like Pat's wait, one in a photo Cleo's own filter hides. Her "name everything waiting"
    names the one she is shown, and leaves the other waiting (for Ada, who sees both)."""
    s = library(app)
    add_maybe_pats(app, [(51, s[15]), (52, s[16])])
    app.load_people()
    cleo = mid(app, CLEO)
    sid = app.changes.create({"title": "Not for me", "op": "hide", "items": [s[16]]}, "", cleo)[0]["id"]
    assert app.changes.write(sid, "apply", {}, "", cleo)[1] == 200
    settle(app)
    mine = waiting(app, CLEO)
    assert {f["id"] for f in mine["faces"]} == {51}
    label(app, CLEO, {"pending": {"person": PAT, "year": None, "as_of": mine["as_of"]}, "person": PAT})
    assert catalog(app, [51, 52]) == {51: (PAT, "you"), 52: (None, None)}
    assert {f["id"] for f in waiting(app, ADA)["faces"]} == {52}


def test_a_purged_photos_face_has_no_crop(app, tmp_path):
    """Issue #146: a face's crop is cached. Its photo is purged by hand (its tombstone; here without the rows going,
    as People still holds the face until it is loaded again): the app's next look at the journal learns of it, and
    the crop is a 404 for everyone."""
    s = library(app)
    pe = people(app)
    faces.crop_path(pe.caches[0], s[3], 0).parent.mkdir(parents=True, exist_ok=True)
    faces.crop_path(pe.caches[0], s[3], 0).write_bytes(b"\xff\xd8 a crop")
    assert app.request("GET", f"/face/{F}", headers=ADA, body=b"").status == 200
    con = faces.connect(app.a.db)
    con.execute("INSERT INTO tombstones(sha256, member_id, why, at) VALUES (?, 0, 'purged', '2026-11-26')", (s[3],))
    con.commit()
    con.close()
    app.purged = app.read_purged()
    assert app.request("GET", f"/face/{F}", headers=ADA, body=b"").status == 404


def test_a_name_saved_is_answered_as_saved_when_the_suggestions_fail_after(app, monkeypatch):
    """Issue #147: Ada names f; the name is committed; working the suggestions out again then fails (a locked
    catalog). Before the fix the request answered 500 and the page said it could not save. It answers ok: it is saved."""
    library(app)
    monkeypatch.setattr(faces.People, "rescore", lambda pe: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
    label(app, ADA, {"faces": [F], "person": PAT})
    assert catalog(app, [F]) == {F: (PAT, "you")} and shown(app, ADA, F)["person"] == PAT
