"""The catalog being served, raced on purpose (issue #83): Browse's cache across a reload or a favourite
(Live.generation), a favourite set while the catalog, or what anyone hides, is re-read (PhotoApp.set_favorites,
reload_catalog, relook), a favourite shown only with no request reading (the gate's write side), and which requests
hold the gate (PhotoApp.gated, with a query string). The demo library, in process, no model; the
interleavings are forced with threading.Event, never with sleeps. A wait's timeout only stops a
regression from hanging the run.

  uv run pytest tests/test_live_races.py
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import catalog  # noqa: E402
import embed  # noqa: E402
import lenses  # noqa: E402
from browse import Browse  # noqa: E402
from catalog import Gate, Live  # noqa: E402
from photo_app import PhotoApp  # noqa: E402

GUARD = 10   # seconds: only so that a hang fails the test instead of the run


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
    return box["value"]


@pytest.fixture
def app():
    a = embed.parser().parse_args(["serve", "--demo", "--no-pregenerate"])
    return PhotoApp(a)            # its own scratch copy of the demo catalog: these tests set favourites


def plain_photo(app) -> str:
    v = seen(app)
    return next(s for s in v.cat.shas if not v.fav[v.cat.index[s]])


def seen(app) -> catalog.View:
    """What the demo's sysadmin (whom a request from this computer is) sees now."""
    with app.live.gate:
        return app.live.visible_for(app.sysadmin.id)


def is_fav(v: catalog.View, sha) -> bool:
    return bool(v.fav[v.cat.index[sha]]) and v.row(sha)["fav"] is True


# -- (a) Browse's cache and the catalog generation ---------------------------------------------------

def bare_browse(live) -> Browse:
    return Browse(live, None, "", "", Path(os.devnull), 0.0, 0.0, 0.0)


def test_a_value_made_while_the_catalog_changed_is_not_kept():
    live = SimpleNamespace(generation=0)
    b = bare_browse(live)
    making, go = threading.Event(), threading.Event()

    def make():
        making.set()
        assert go.wait(GUARD)
        return "old catalog's"
    t, box = in_thread(b.remember, "key", make)
    assert making.wait(GUARD)                   # make() is working from the catalog it was given...
    live.generation += 1                        # ...when a reload swaps a new one in and forgets the cache
    b.forget()
    go.set()
    assert finish(t, box) == "old catalog's"    # its own caller gets what it asked for
    assert b.remember("key", lambda: "new catalog's") == "new catalog's", "a stale value was cached after forget()"


def test_nothing_cached_is_found_once_a_new_catalog_is_in():
    live = Live(object())                       # type: ignore[arg-type]
    b = bare_browse(live)
    assert b.remember("key", lambda: "old") == "old"
    live.install(object())                      # type: ignore[arg-type]  # before forget() is called...
    assert b.remember("key", lambda: "new") == "new"
    g = live.generation
    live.update(lambda c: None)                 # a favourite is a new generation too
    assert live.generation == g + 1 and b.remember("key", lambda: "newer") == "newer"
    assert b.remember("key", lambda: "again") == "newer"


# -- (b) a favourite set while the catalog, or what anyone hides, is re-read ------------------------
# (Multiplayer phase 3: a favourite is the member's, kept in the looks (lenses.State) that every View is worked
# out from, and the catalog no longer reads favourites at all, so a reload cannot miss one.)

def test_a_favourite_set_while_a_reload_reads_reaches_the_new_catalog(app, monkeypatch):
    old, sha = seen(app), plain_photo(app)
    read, go = threading.Event(), threading.Event()
    load = app.load_catalog

    def paused(prev=None):
        c = load(prev)
        read.set()
        assert go.wait(GUARD)
        return c
    monkeypatch.setattr(app, "load_catalog", paused)
    t, box = in_thread(app.reload_catalog)
    assert read.wait(GUARD)                      # the new catalog is read...
    assert app.set_favorites([sha], True, "", app.sysadmin.id) == 1
    assert is_fav(old, sha)                      # shown at once in the view being served
    go.set()
    finish(t, box)
    new = seen(app)
    assert new.cat is not old.cat and is_fav(new, sha), "the favourite was lost by the reload"
    assert app.set_favorites([sha], False, "", app.sysadmin.id) == 1
    assert not is_fav(new, sha) and is_fav(old, sha), "the replaced catalog's view was changed after the swap"
    assert new.info["fav"] == old.info["fav"] - 1


def test_a_favourite_half_set_when_a_reload_swaps_goes_to_the_new_catalog(app, monkeypatch):
    """The favourite began before the reload's swap and has not reached the table when the reload has its new catalog:
    the swap waits for it (fav_write, as a relook does: specs/Reload.tla), and it is shown in the view now served."""
    old, sha = seen(app), plain_photo(app)
    at_table, go = threading.Event(), threading.Event()
    now = lenses.now

    def paused_now():                            # set_favorites asks the time just before it writes the table
        if not at_table.is_set():
            at_table.set()
            assert go.wait(GUARD)
        return now()
    monkeypatch.setattr(lenses, "now", paused_now)
    t, box = in_thread(app.set_favorites, [sha], True, "", app.sysadmin.id)
    assert at_table.wait(GUARD)
    done, rbox = in_thread(app.reload_catalog)   # has its new catalog, and waits to swap it in
    done.join(0.2)
    assert done.is_alive() and seen(app).cat is old.cat, "a new catalog was swapped in while a favourite was half set"
    go.set()
    assert finish(t, box) == 1
    finish(done, rbox)
    new = seen(app)
    assert new.cat is not old.cat and is_fav(new, sha), "the favourite is shown in the catalog served"
    assert is_fav(old, sha) and old.info["fav"] == new.info["fav"]


def test_a_favourite_is_not_lost_by_reading_the_looks_again(app, monkeypatch):
    """What anyone hides changed (relook reads lenses.State again) while a favourite was being set: the two
    take turns (PhotoApp.fav_write), so the looks read afterwards have the favourite, and so does every view."""
    sha = plain_photo(app)
    at_table, go = threading.Event(), threading.Event()
    now = lenses.now

    def paused_now():
        if not at_table.is_set():
            at_table.set()
            assert go.wait(GUARD)
        return now()
    monkeypatch.setattr(lenses, "now", paused_now)
    t, box = in_thread(app.set_favorites, [sha], True, "", app.sysadmin.id)
    assert at_table.wait(GUARD)
    r, rbox = in_thread(app.relook)
    r.join(0.2)
    assert r.is_alive(), "the looks were read again while a favourite was half set"
    go.set()
    assert finish(t, box) == 1
    finish(r, rbox)
    assert is_fav(seen(app), sha), "the favourite was lost when the looks were read again"


# -- (c) a favourite is shown with no request reading ------------------------------------------------

def test_show_favorites_runs_on_the_gates_write_side(app, monkeypatch):
    seen_at = []
    show = catalog.View.show_favorites

    def spy(self, pairs):
        g = app.live.gate
        seen_at.append((g.writing, g.readers))
        return show(self, pairs)
    monkeypatch.setattr(catalog.View, "show_favorites", spy)
    sha = plain_photo(app)
    r = app.request("POST", "/api/favorite", body={"sha": sha, "fav": True}, local=True)
    assert r.status == 200 and r.json()["fav"] is True and r.json()["favourites"] == seen(app).info["fav"]
    aid = app.request("POST", "/api/albums", body={"title": "races", "items": [sha, app.live.cat.shas[1]]},
                      local=True).json()["id"]
    r = app.request("POST", f"/api/albums/{aid}/favorite", body={"fav": False}, local=True)
    assert r.status == 200 and r.json()["changed"] >= 1, r.json()
    app.reload_catalog()
    assert seen_at and all(x == (True, 0) for x in seen_at), f"(writing, readers) while shown: {seen_at}"


def test_a_favourite_waits_for_the_requests_reading(app, monkeypatch):
    v, sha = seen(app), plain_photo(app)
    gate, asked = app.live.gate, threading.Event()
    swap = gate.swap
    monkeypatch.setattr(gate, "swap", lambda fn: (asked.set(), swap(fn))[1])
    with gate:                                   # a request reading the catalog
        t, box = in_thread(lambda: app.request("POST", "/api/favorite", body={"sha": sha, "fav": True}, local=True))
        assert asked.wait(GUARD), "the favourite did not ask for the gate's write side"
        assert not v.fav[v.cat.index[sha]], "shown while a request was reading"
    assert finish(t, box).status == 200 and is_fav(v, sha)


def test_a_swap_from_inside_the_gate_raises():
    g = Gate()

    def nested_in_reader():
        with g:
            return g.swap(lambda: None)
    t, box = in_thread(nested_in_reader)
    with pytest.raises(RuntimeError):
        finish(t, box)
    t, box = in_thread(g.swap, lambda: g.swap(lambda: None))
    with pytest.raises(RuntimeError):
        finish(t, box)
    assert g.swap(lambda: 7) == 7 and (g.readers, g.writing, g.waiting) == (0, False, 0)


# -- (d) which requests hold the gate ----------------------------------------------------------------

class CountingGate(Gate):
    def __init__(self):
        super().__init__()
        self.entered = 0

    def __enter__(self):
        self.entered += 1
        return super().__enter__()


@pytest.mark.parametrize("method,target,gated", [
    ("GET", "/api/albums/1/download?dl=1", False),
    ("GET", "/api/albums/1/download", False),
    ("GET", "/api/info?x=1", True),
    ("GET", "/api/search?q=&limit=1", True),
    ("GET", "/api/albums?x=/download", True),
])
def test_downloads_skip_the_gate_with_a_query_string(app, monkeypatch, method, target, gated):
    g = CountingGate()
    monkeypatch.setattr(app.live, "gate", g)
    app.request(method, target)
    assert (g.entered > 0) == gated, (target, g.entered)


def test_the_gate_rule():
    assert not PhotoApp.gated("POST", "/api/favorite") and not PhotoApp.gated("POST", "/api/albums/3/favorite")
    assert PhotoApp.gated("GET", "/api/albums/3/favorite") and PhotoApp.gated("POST", "/api/albums/3/items")
    assert not PhotoApp.gated("GET", "/setup") and not PhotoApp.gated("PUT", "/api/upload/x.jpg")
