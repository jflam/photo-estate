"""The catalog being served, raced on purpose (issue #83): Browse's cache across a reload or a favourite
(Live.generation), a favourite set while the catalog is re-read (PhotoApp.set_favorites, reload_catalog
and fav_log), a favourite shown only with no request reading (the gate's write side), and which requests
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
import photo_app  # noqa: E402
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
    c = app.live.cat
    return next(s for s in c.shas if not c.fav[c.index[s]])


def is_fav(c, sha) -> bool:
    return bool(c.fav[c.index[sha]]) and c.meta[sha]["fav"] is True


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


# -- (b) a favourite set while the catalog is re-read ------------------------------------------------

def test_a_favourite_set_while_a_reload_reads_reaches_the_new_catalog(app, monkeypatch):
    """The reload read the table before the favourite: it is shown to the new catalog as it is swapped in."""
    old, sha = app.live.cat, plain_photo(app)
    read, go = threading.Event(), threading.Event()
    load = app.load_catalog

    def paused(prev=None):
        c = load(prev)
        read.set()
        assert go.wait(GUARD)
        return c
    monkeypatch.setattr(app, "load_catalog", paused)
    t, box = in_thread(app.reload_catalog)
    assert read.wait(GUARD)                      # the new catalog is read, without the favourite
    assert app.set_favorites([sha], True, "") == 1
    assert is_fav(old, sha)                      # shown at once in the one being served
    go.set()
    finish(t, box)
    new = app.live.cat
    assert new is not old and is_fav(new, sha), "the favourite was lost by the reload"
    assert not app.fav_log, f"fav_log kept what the new catalog already shows: {app.fav_log}"
    assert app.set_favorites([sha], False, "") == 1
    assert not is_fav(new, sha) and is_fav(old, sha), "the replaced catalog was changed after the swap"
    assert new.info["fav"] == old.info["fav"] - 1


def test_a_favourite_recorded_after_the_swap_goes_to_the_new_catalog(app, monkeypatch):
    """The favourite began before the reload but reaches the table after the reload read it, and the
    catalog after the swap: it must be shown to the catalog now served, not to the one it began with."""
    old, sha = app.live.cat, plain_photo(app)
    at_table, go = threading.Event(), threading.Event()
    now = photo_app.now

    def paused_now():                            # set_favorites asks the time just before it writes the table
        if not at_table.is_set():
            at_table.set()
            assert go.wait(GUARD)
        return now()
    monkeypatch.setattr(photo_app, "now", paused_now)
    t, box = in_thread(app.set_favorites, [sha], True, "")
    assert at_table.wait(GUARD)
    done, rbox = in_thread(app.reload_catalog)   # reads the table without it, and swaps the new catalog in
    finish(done, rbox)
    new = app.live.cat
    assert new is not old and not is_fav(new, sha)
    go.set()
    assert finish(t, box) == 1
    assert is_fav(new, sha), "the favourite went to the catalog it began with, not the one served"
    assert not is_fav(old, sha), "the replaced catalog was changed after the swap"
    assert not app.fav_log


def test_the_log_is_only_kept_while_a_reload_reads(app):
    sha = plain_photo(app)
    app.set_favorites([sha], True, "")
    assert app.fav_log is None                   # no reload: nothing to replay, nothing kept
    app.reload_catalog()
    assert is_fav(app.live.cat, sha) and app.fav_log is None


# -- (c) a favourite is shown with no request reading ------------------------------------------------

def test_show_favorites_runs_on_the_gates_write_side(app, monkeypatch):
    seen = []
    show = catalog.Catalog.show_favorites

    def spy(self, pairs):
        g = app.live.gate
        seen.append((g.writing, g.readers))
        return show(self, pairs)
    monkeypatch.setattr(catalog.Catalog, "show_favorites", spy)
    sha = plain_photo(app)
    r = app.request("POST", "/api/favorite", body={"sha": sha, "fav": True}, local=True)
    assert r.status == 200 and r.json()["fav"] is True and r.json()["favourites"] == app.live.cat.info["fav"]
    aid = app.request("POST", "/api/albums", body={"title": "races", "items": [sha, app.live.cat.shas[1]]},
                      local=True).json()["id"]
    r = app.request("POST", f"/api/albums/{aid}/favorite", body={"fav": False}, local=True)
    assert r.status == 200 and r.json()["changed"] >= 1, r.json()
    app.reload_catalog()
    assert seen and all(s == (True, 0) for s in seen), f"(writing, readers) while shown: {seen}"


def test_a_favourite_waits_for_the_requests_reading(app, monkeypatch):
    c, sha = app.live.cat, plain_photo(app)
    gate, asked = app.live.gate, threading.Event()
    swap = gate.swap
    monkeypatch.setattr(gate, "swap", lambda fn: (asked.set(), swap(fn))[1])
    with gate:                                   # a request reading the catalog
        t, box = in_thread(lambda: app.request("POST", "/api/favorite", body={"sha": sha, "fav": True}, local=True))
        assert asked.wait(GUARD), "the favourite did not ask for the gate's write side"
        assert not c.fav[c.index[sha]] and not c.meta[sha]["fav"], "shown while a request was reading"
    assert finish(t, box).status == 200 and is_fav(c, sha)


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
