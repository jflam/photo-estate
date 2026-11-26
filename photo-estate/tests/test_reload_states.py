"""What the running app holds in memory from the catalog, and how it is replaced while requests are served
(catalog.Live; PhotoApp.reload_catalog, relook, set_favorites, on_hidden_change): each way the model of it
(specs/Reload.tla, checked by TLC) found the app could end up other than as the catalog says, replayed on a real
PhotoApp on the demo library, in process, with real requests as its members (Ada the sysadmin, Ben, Cleo); then
what the model shows the code does where the owner has a question to decide (those tests say what is, not what must
be); then who waits for whom (specs/ReloadLocks.tla) with real threads.

Each race holds one function between its read and its install (a relook between read_looks and its swap) while
another runs to its end on a thread of its own, as tests/test_member_states.py does for the roster. Steps are placed
with threading.Event, never with sleeps; a wait's timeout (GUARD) only stops a regression from hanging the run, and
fails the test.

  uv run pytest tests/test_reload_states.py
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import embed  # noqa: E402
import estate  # noqa: E402
import lenses  # noqa: E402
import photo_app  # noqa: E402
import private  # noqa: E402
from photo_app import PhotoApp  # noqa: E402
from test_multiplayer import ADA, BEN, CLEO, call, feed, mid, seen, settle  # noqa: E402
from test_private import MODEL, embed_as, jpeg, share  # noqa: E402

GUARD = 20   # seconds: only so that a hang fails the test instead of the run


def make(*more) -> PhotoApp:
    app = PhotoApp(embed.parser().parse_args(["serve", "--demo", "--no-pregenerate", *more]))   # no start(): no threads
    app.feed_update()
    return app


@pytest.fixture
def app():
    app = make()
    yield app
    app.tasks.stop()


def in_thread(fn, *args):
    """Run fn in a thread; returns (thread, box) where box gets "value" or "error"."""
    box: dict = {}

    def run():
        try:
            box["value"] = fn(*args)
        except BaseException as e:
            box["error"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, box


def finish(t, box):
    t.join(GUARD)
    assert not t.is_alive(), "still waiting"
    if "error" in box:
        raise box["error"]
    return box.get("value")


class Watched:
    """A lock standing in for one of the app's (fav_write), that tells `asked` when a thread has to wait for it."""
    def __init__(self, lock, asked: threading.Event):
        self.lock, self.asked = lock, asked

    def __enter__(self):
        if not self.lock.acquire(blocking=False):
            self.asked.set()
            self.lock.acquire()
        return self

    def __exit__(self, *exc):
        self.lock.release()


def ingest(app, tmp_path, who, seed: int, receipt: bool) -> str:
    """A phone upload from `who`, ingested as ingest-iphone.sh does (estate.py ingest --uploads: its `new` row, in the
    transaction that adds it), with the vector the app made when it arrived (like a receipt, or like a demo photo).
    Returns its sha."""
    import hashlib
    inbox, store = tmp_path / "iphone", tmp_path / "store"
    d = inbox / f"m{mid(app, who)}" / "2026" / "11" / "14"
    d.mkdir(parents=True, exist_ok=True)
    data = jpeg(seed)
    (d / f"IMG_{seed}.JPG").write_bytes(data)
    estate.cmd_ingest(argparse.Namespace(db=app.a.db, store=str(store), sources=[str(inbox)], force=True, facts=None,
                                         delete_after=False, progress=False, uploads=True))
    sha = hashlib.sha256(data).hexdigest()
    embed_as(app, sha, private.prompt_vectors(MODEL)[0] if receipt else app.live.cat.M[0])
    return sha


def check(app, sha: str) -> str | None:
    """`private.py check`, as the ingest script runs it before catalog_ready: held (a receipt), or let go (None)."""
    con = estate.connect(app.a.db)
    try:
        private.check(con, MODEL, out=lambda *a: None)
        got = con.execute("SELECT why FROM private WHERE sha256=?", (sha,)).fetchone()
    finally:
        con.close()
    return got[0] if got else None


def upload(app, tmp_path, who, seed: int, receipt: bool) -> str:
    """ingest, then check: held, or let go."""
    sha = ingest(app, tmp_path, who, seed, receipt)
    assert (check(app, sha) == "held") == receipt
    return sha


def plain(app, k: int) -> str:
    return app.live.cat.shas[k]


# -- the normal path ---------------------------------------------------------------------------------------------

def test_one_at_a_time_memory_is_what_the_catalog_says(app, tmp_path):
    """What must not change: Ben withdraws a photo and it leaves Ada's search before his answer; his phone upload, held
    by check, is shown to him alone once the catalog is reloaded, and one let go to everyone; Cleo's favourite shows
    at once; Ada's change set hides for her once taken in; /api/stats counts what Ada may be shown; when all is done
    nothing is busy and the reload is idle."""
    y = plain(app, 3)
    share(app, y, BEN)
    call(app, "POST", "/api/private", BEN, {"shas": [y], "private": True})
    assert y not in seen(app, ADA) and y in seen(app, BEN)
    x, w = upload(app, tmp_path, BEN, 1, receipt=True), upload(app, tmp_path, BEN, 2, receipt=False)
    app.reload_catalog()
    assert x in seen(app, BEN) and x not in seen(app, ADA) and x not in seen(app, CLEO) and not app.may_see(x, mid(app, ADA))
    assert w in seen(app, ADA) and w in seen(app, CLEO)
    z = plain(app, 5)
    assert call(app, "POST", "/api/favorite", CLEO, {"sha": z, "fav": True})["fav"] is True
    sid = call(app, "POST", "/api/changes", ADA, {"title": "hide", "items": [{"sha": plain(app, 7)}]}, expect=201)["id"]
    call(app, "POST", f"/api/changes/{sid}/apply", ADA, {})
    settle(app)
    assert plain(app, 7) not in seen(app, ADA) and plain(app, 7) in seen(app, CLEO)
    n = call(app, "GET", "/api/stats", ADA)["library"]
    assert n == call(app, "GET", "/api/stats", ADA)["library"]
    assert app.settling == 0 and app.reload_state["state"] == "idle" and not call(app, "GET", f"/api/changes/{sid}", ADA)["busy"]
    assert x not in feed(app, CLEO) and y not in feed(app, ADA)


# -- races -------------------------------------------------------------------------------------------------------

def relook_held(app, monkeypatch):
    """Ben's POST /api/private of a photo of his starts on a thread; its relook reads the looks and is held there,
    holding fav_write, before it installs them. Returns (resume, finish it): resume() lets it go on."""
    y = plain(app, 3)
    share(app, y, BEN)
    read, go = threading.Event(), threading.Event()
    real = app.read_looks

    def paused():
        got = real()
        read.set()
        assert go.wait(GUARD)
        return got
    monkeypatch.setattr(app, "read_looks", paused)
    t, box = in_thread(call, app, "POST", "/api/private", BEN, {"shas": [y], "private": True})
    assert read.wait(GUARD)
    monkeypatch.setattr(app, "read_looks", real)
    return go.set, lambda: finish(t, box)


def reload_meanwhile(app, monkeypatch):
    """reload_catalog on a thread of its own, until it is done or has to wait for fav_write (the relook holds it)."""
    moved = threading.Event()
    monkeypatch.setattr(app, "fav_write", Watched(app.fav_write, moved))
    t, box = in_thread(lambda: (app.reload_catalog(), moved.set()))
    assert moved.wait(GUARD), "the reload neither finished nor waited for fav_write"
    return t, box


def test_a_photo_ingested_while_the_looks_are_read_again_is_shown_only_to_its_sharer(app, tmp_path, monkeypatch):
    """ShownAsTheCatalogSays: TLC's trace. Ben makes a photo private; his request's relook reads the looks (read_looks)
    and, before it installs them, his phone upload (a receipt) is ingested with its `new` row, check holds it, and the
    ingest's reload swaps the new catalog in with the private rows read again. The relook then installs the looks it
    read before the upload: the catalog has the photo, and the looks have no row for it. It is shown to everyone, in
    search and in their phones' feeds, until something next relooks or reloads. The relook must not put an older copy
    over a newer one: the reload's install waits for it (fav_write)."""
    resume, done = relook_held(app, monkeypatch)
    x = upload(app, tmp_path, BEN, 1, receipt=True)
    t, box = reload_meanwhile(app, monkeypatch)
    resume()
    done()
    finish(t, box)
    settle(app)
    assert x in app.live.cat.index and x in seen(app, BEN)
    assert x not in seen(app, ADA) and x not in seen(app, CLEO), "a held photo is shown only to its sharer"
    assert not app.may_see(x, mid(app, CLEO)) and x not in feed(app, CLEO)


def test_a_photo_let_go_while_the_looks_are_read_again_is_shown_to_everyone(app, tmp_path, monkeypatch):
    """AsTheCatalogOnceQuiet: TLC's trace. Ben's phone upload is ingested (its `new` row); Ben makes a photo private and
    his relook reads the looks, the upload's row with them; before it installs them, check lets the upload go (its row
    deleted) and the ingest's reload swaps the catalog in with the private rows read again. The relook installs the
    row check deleted: the photo is shown to Ben alone, though the catalog says everyone, until the next relook."""
    x = ingest(app, tmp_path, BEN, 2, receipt=False)
    resume, done = relook_held(app, monkeypatch)
    assert check(app, x) is None
    t, box = reload_meanwhile(app, monkeypatch)
    resume()
    done()
    finish(t, box)
    settle(app)
    assert x in seen(app, BEN)
    assert x in seen(app, ADA) and x in seen(app, CLEO), "once nothing is reloaded or relooked, memory is the catalog"


def test_a_request_is_not_answered_from_numbers_of_the_looks_before(app, monkeypatch):
    """NoStaleWork: TLC's trace. Ada's /stats has its library numbers (Admin.st["lib"], from what she may be shown).
    Ben makes a photo of his private: his relook swaps the new looks in, and lets go of the numbers only after the swap.
    A /api/stats from Ada in between is answered from the numbers worked out before, counting the photo she may no
    longer be shown."""
    y = plain(app, 3)
    share(app, y, BEN)
    before = call(app, "GET", "/api/stats", ADA)["library"]
    swapped, go = threading.Event(), threading.Event()
    real = app.live.relook

    def paused(*a, **k):
        real(*a, **k)
        swapped.set()
        assert go.wait(GUARD)
    monkeypatch.setattr(app.live, "relook", paused)
    t, box = in_thread(call, app, "POST", "/api/private", BEN, {"shas": [y], "private": True})
    assert swapped.wait(GUARD)
    between = call(app, "GET", "/api/stats", ADA)["library"]
    go.set()
    finish(t, box)
    after = call(app, "GET", "/api/stats", ADA)["library"]
    assert before != after
    assert between == after, "nothing worked out in one generation is used in a later one"


def test_a_failed_reload_after_another_still_says_restart(tmp_path, monkeypatch):
    """FreshTold: TLC's trace. An ingest run tells the app (IngestJob.fresh, then on_added: reload_catalog). Its reload
    puts the catalog in and goes idle; before it reads reload_state's error after its loop, a second run tells the app
    and its reload starts. The first reads no error and clears fresh; the second then fails. /stats says nothing is
    waiting to be loaded, and the second run's photos are not in the catalog served."""
    app = make("--inbox", str(tmp_path / "iphone"))
    try:
        job, lock = app.ingest, app.reload_lock
        assert job is not None
        idle, go_a, loading, go_b, paused = (threading.Event() for _ in range(5))

        class Held:      # reload_lock: the first call held just after it lets the lock go, idle
            def __enter__(self):
                lock.acquire()

            def __exit__(self, *exc):
                lock.release()
                if app.reload_state["state"] == "idle" and not paused.is_set():
                    paused.set()
                    idle.set()
                    assert go_a.wait(GUARD)
        monkeypatch.setattr(app, "reload_lock", Held())
        job.fresh = True                             # the first run's catalog_ready
        a, abox = in_thread(app.reload_catalog)
        assert idle.wait(GUARD)

        def failing(prev=None):
            loading.set()
            assert go_b.wait(GUARD)
            raise sqlite3.OperationalError("disk I/O error")
        monkeypatch.setattr(app, "load_catalog", failing)
        job.fresh = True                             # the second run's
        b, bbox = in_thread(app.reload_catalog)
        assert loading.wait(GUARD)
        go_a.set()
        finish(a, abox)
        go_b.set()
        finish(b, bbox)
        assert app.reload_state["state"] == "idle" and app.reload_state["error"]
        assert job.status()["restart_needed"], "fresh is false only once every run's photos are in the catalog served"
    finally:
        app.tasks.stop()


def test_busy_ends_when_the_thread_cannot_be_started(app, monkeypatch):
    """NotBusyOnceSettled: TLC's trace. Ada applies a change set; on_hidden_change counts settling up and starts the
    thread that takes it in, and the thread cannot be started (too many threads). The request is answered an error.
    settling stays at one: Changes' busy is true for ever, and the review page says "Still updating" until a restart."""
    sid = call(app, "POST", "/api/changes", ADA, {"title": "hide", "items": [{"sha": plain(app, 7)}]}, expect=201)["id"]
    real = threading.Thread

    class NoThread(real):
        def start(self):
            if getattr(self, "_target", None) == app.hidden_changed:
                raise RuntimeError("can't start new thread")
            super().start()
    monkeypatch.setattr(photo_app.threading, "Thread", NoThread)
    with pytest.raises(RuntimeError):
        app.request("POST", f"/api/changes/{sid}/apply", headers=ADA, body={})
    monkeypatch.setattr(photo_app.threading, "Thread", real)
    assert app.settling == 0, "busy is true while, and only while, a change is still settling"
    assert not call(app, "GET", f"/api/changes/{sid}", ADA)["busy"]


# -- what is: questions for the owner ----------------------------------------------------------------------------

def test_what_is_a_relook_that_raises_after_its_commit_leaves_the_photo_shown(app, monkeypatch):
    """PrivateEvenWhenARelookFails (not in Reload.cfg). Ben makes a photo private: private.change commits, then his
    relook raises (here: lenses.read finds the catalog locked). His request is answered an error (through the HTTP
    server: no answer, the connection dropped). The catalog says private; the app goes on showing it to Ada, and
    nothing tries again: it lasts until something else relooks or reloads (any lens, private or change-set write, a
    release, a members change, an ingest, a restart)."""
    y = plain(app, 3)
    share(app, y, BEN)

    def locked():
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(app, "read_looks", locked)
    with pytest.raises(sqlite3.OperationalError):
        app.request("POST", "/api/private", headers=BEN, body={"shas": [y], "private": True})
    monkeypatch.undo()
    con = estate.connect(app.a.db)
    assert con.execute("SELECT why FROM private WHERE sha256=?", (y,)).fetchone() == ("withdrawn",)
    con.close()
    assert y in seen(app, ADA) and app.may_see(y, mid(app, ADA))     # what is
    assert app.settling == 0 and app.reload_state["state"] == "idle"   # nothing is under way to bring it up to date
    app.reload_catalog()                                                # (the next ingest's reload does)
    assert y not in seen(app, ADA)


def test_what_is_a_reload_that_raises_is_not_tried_again(app, tmp_path, monkeypatch):
    """AsTheCatalogEvenAfterAFailure (not in Reload.cfg). An ingest's reload raises: the old catalog keeps answering,
    reload_state says the error, loaded_through stays where it was (the listeners wait for it), and nothing tries
    again: the run's photos are searchable at the next reload (the next ingest that adds photos) or a restart."""
    x = upload(app, tmp_path, BEN, 2, receipt=False)
    mark = app.loaded_through

    def broken(prev=None):
        raise MemoryError("out of memory")
    monkeypatch.setattr(app, "load_catalog", broken)
    app.reload_catalog()
    monkeypatch.undo()
    assert app.reload_state["state"] == "idle" and "MemoryError" in app.reload_state["error"]
    assert x not in app.live.cat.index and x not in seen(app, ADA) and app.loaded_through == mark   # what is
    app.reload_catalog()
    assert x in seen(app, ADA) and app.loaded_through > mark


# -- who waits for whom (specs/ReloadLocks.tla) ------------------------------------------------------------------

def test_a_favourite_a_relook_and_a_reload_at_once_all_finish(app, monkeypatch):
    """No two wait for each other for ever (ReloadLocks.cfg): Cleo's favourite holds fav_write, half set (at the
    table); Ben's request's relook and a reload of the catalog both wait for it, and a request reads meanwhile. Let go,
    each is done in turn: fav_write, then the gate, everywhere."""
    z = plain(app, 5)
    at_table, go, asked = threading.Event(), threading.Event(), threading.Event()
    now = lenses.now

    def paused_now():
        if not at_table.is_set():
            at_table.set()
            assert go.wait(GUARD)
        return now()
    monkeypatch.setattr(lenses, "now", paused_now)
    monkeypatch.setattr(app, "fav_write", Watched(app.fav_write, asked))
    f, fbox = in_thread(call, app, "POST", "/api/favorite", CLEO, {"sha": z, "fav": True})
    assert at_table.wait(GUARD)
    y = plain(app, 3)
    con = sqlite3.connect(app.a.db)
    con.execute("INSERT OR IGNORE INTO sharers VALUES (?,?,'2026-01-01')", (y, mid(app, BEN)))
    con.commit()
    con.close()
    r, rbox = in_thread(call, app, "POST", "/api/private", BEN, {"shas": [y], "private": True})
    assert asked.wait(GUARD)
    asked.clear()
    lt, lbox = in_thread(app.reload_catalog)
    call(app, "GET", "/api/info", ADA)                 # requests are served meanwhile
    go.set()
    assert finish(f, fbox)["fav"] is True
    for t, box in ((r, rbox), (lt, lbox)):
        finish(t, box)
    settle(app)
    with app.live.gate:
        v = app.live.visible_for(mid(app, CLEO))
    assert v.fav[v.cat.index[z]] and y not in seen(app, ADA)


def test_what_is_a_request_waiting_on_the_ingests_write_lock_holds_up_every_request(app, monkeypatch):
    """NothingWaitsOnTheIngest (not in ReloadLocks.cfg). The ingest script holds SQLite's write lock (estate.py ingest:
    up to 200 files copied into the store between commits). Ben's POST /api/tombstones, inside the gate, waits for it;
    a relook (here Cleo's lens change, or any) waits for Ben's request to leave the gate; and Ada's GET /api/info, which
    reads nothing from SQLite, waits for the relook: the whole app waits on the ingest, up to the 60 s busy timeout,
    which then fails Ben's request. No deadlock: it is over once the ingest commits."""
    ingest = sqlite3.connect(app.a.db, timeout=1)
    ingest.execute("BEGIN IMMEDIATE")                 # the ingest's batch
    writing, swapping = threading.Event(), threading.Event()
    now, swap = private.now, app.live.gate.swap
    monkeypatch.setattr(private, "now", lambda: (writing.set(), now())[1])
    monkeypatch.setattr(app.live.gate, "swap", lambda fn: (swapping.set(), swap(fn))[1])
    w, wbox = in_thread(call, app, "POST", "/api/tombstones", BEN, {"add": ["ab" * 32]})
    assert writing.wait(GUARD)
    r, rbox = in_thread(app.relook)
    assert swapping.wait(GUARD)
    g, gbox = in_thread(call, app, "GET", "/api/info", ADA)
    g.join(0.5)
    held_up = g.is_alive() and w.is_alive() and r.is_alive()
    ingest.rollback()
    ingest.close()
    for t, box in ((w, wbox), (r, rbox), (g, gbox)):
        finish(t, box)
    assert held_up                                    # what is
