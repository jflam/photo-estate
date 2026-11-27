"""A member's phone's copy of the catalog, and the change feed it follows (sync.Feed; PhotoApp.feed_update,
set_favorites, the /api/sync route): each way the model of it (specs/PhoneReplica.tla, checked by TLC) found a phone
could be sent, or left with, other than what its member sees, replayed on a real PhotoApp on the demo library, in
process, with a phone that pages and applies as SyncEngine.run and Catalog.apply do (Phone below). The members are the
demo's: Ada (the sysadmin, sync-feed.db), Ben, Cleo (no feed until her first phone asks).

Each race holds one function between its read and its write (feed_update between reading a member's rows in the gate
and Feed.update; set_favorites before fav_write, or before its touch) while another runs to its end on a thread of its
own, as tests/test_reload_states.py does. Steps are placed with threading.Event, never with sleeps; a wait's timeout
(GUARD) only stops a regression from hanging the run, and fails the test.

The phone's own half (a page asked before a favourite the phone kept) is Swift: ios/Tests/run-replica.sh.

  uv run pytest tests/test_phone_replica_states.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import catalog  # noqa: E402
import embed  # noqa: E402
from estate import connect  # noqa: E402
from photo_app import PhotoApp  # noqa: E402
from sync import Feed  # noqa: E402
from test_multiplayer import ADA, BEN, CLEO, call, mid, seen, settle  # noqa: E402
from test_private import share  # noqa: E402
from test_reload_states import GUARD, Watched, finish, in_thread, plain  # noqa: E402


def make(*more) -> PhotoApp:
    """A PhotoApp on a scratch copy of the demo, its feeds brought up to date as start() does."""
    app = PhotoApp(embed.parser().parse_args(["serve", "--demo", "--no-pregenerate", *more]))   # no start(): no threads
    app.feed_update()
    return app


@pytest.fixture
def app():
    app = make()
    yield app
    app.tasks.stop()


class Phone:
    """A member's phone: its replica (sha -> row, with the epoch each came in) and its cursor, as SyncEngine keeps them.
    ask() is one GET /api/sync with the cursor as it is; apply() is the transaction SyncEngine.run commits with a page
    (Catalog.apply: deleted, then rows; the cursor; dropOtherEpochs when there is no more)."""

    def __init__(self, who: dict, limit: int = 2000):
        self.who, self.limit = who, limit
        self.epoch, self.since, self.after = "", 0, ""
        self.rows: dict[str, dict] = {}
        self.epochs: dict[str, str] = {}

    def ask(self, app) -> dict:
        r = app.request("GET", f"/api/sync?epoch={self.epoch}&since={self.since}&after={self.after}&limit={self.limit}",
                        headers=self.who, body=b"")
        assert r.status in (200, 503), (r.status, r.body[:300])
        return r.json()

    def apply(self, page: dict) -> None:
        for sha in page["deleted"]:
            self.rows.pop(sha, None)
            self.epochs.pop(sha, None)
        for row in page["photos"]:
            self.rows[row["sha"]] = row
            self.epochs[row["sha"]] = page["epoch"]
        self.epoch, self.since, self.after = page["epoch"], page["since"], page["after"]
        if not page["more"]:
            for sha in [s for s, e in self.epochs.items() if e != page["epoch"]]:
                del self.rows[sha], self.epochs[sha]

    def sync(self, app) -> None:
        """SyncEngine.run: pages until there is no more (a 503: asked again, a moment later: the app waits 3 s)."""
        for _ in range(1000):
            page = self.ask(app)
            if "error" in page:
                time.sleep(0.01)
                continue
            self.apply(page)
            if not page["more"]:
                return
        raise AssertionError("the sync never ended")


def sees(app, who) -> dict[str, dict]:
    """What the member sees, row for row, as the feed carries it (PhotoApp.feed_rows), through JSON as a page is."""
    with app.live.gate:
        rows = app.feed_rows(app.live.visible_for(mid(app, who)))
    return json.loads(json.dumps({s: dict(sha=s, **r) for s, r in rows.items()}))


def feed_of(app, who):
    feed = app.feed_of(mid(app, who))
    assert feed is not None
    return feed


def wait_for(cond, what: str):
    end = time.time() + GUARD
    while not cond():
        assert time.time() < end, what
        time.sleep(0.01)


def held_first(obj, name: str):
    """obj.name, held at its first call until go() (reached: an Event set when it is). Later calls go straight
    through. Returns (reached, go)."""
    real, reached, go, calls = getattr(obj, name), threading.Event(), threading.Event(), []

    def held(*a, **k):
        if not calls:
            calls.append(1)
            reached.set()
            assert go.wait(GUARD)
        return real(*a, **k)
    setattr(obj, name, held)
    return reached, go.set


# -- the normal path ---------------------------------------------------------------------------------------------

def test_one_at_a_time_the_phone_ends_as_its_member_sees(app):
    """What must not change: Ada's phone syncs in pages of 7; Ben makes a photo private and it leaves her phone; he
    lets it go and it comes back; she favourites one on the web and her phone has it at the next sync; nothing is
    sent twice to a phone that is up to date."""
    y, z = plain(app, 3), plain(app, 5)
    share(app, y, BEN)
    phone = Phone(ADA, limit=7)
    phone.sync(app)
    assert phone.rows == sees(app, ADA) and y in phone.rows
    call(app, "POST", "/api/private", BEN, {"shas": [y], "private": True})
    settle(app)
    phone.sync(app)
    assert y not in phone.rows and phone.rows == sees(app, ADA)
    call(app, "POST", "/api/private", BEN, {"shas": [y], "private": False})
    settle(app)
    call(app, "POST", "/api/favorite", ADA, {"sha": z, "fav": True})
    phone.sync(app)
    assert phone.rows == sees(app, ADA) and phone.rows[z]["fav"] is True and y in phone.rows
    again = phone.ask(app)
    assert again["photos"] == [] and again["deleted"] == [] and not again["more"]


def test_a_members_first_two_phones_asking_at_once_share_one_feed(app):
    """Hypothesis 4, refuted: Cleo has no feed; two of her phones ask at once. Her feed is made once (feed_of, under
    feeds_lock), from what she sees, inside the gate; each phone is answered from it (or told 503, and asks again), and
    both end as she sees."""
    assert app.feed_of(mid(app, CLEO)) is None
    phones = [Phone(CLEO, limit=7), Phone(CLEO, limit=7)]
    ts = [in_thread(p.sync, app) for p in phones]
    for t, box in ts:
        finish(t, box)
    assert sum(1 for k in app.feeds if k == mid(app, CLEO)) == 1
    assert phones[0].rows == phones[1].rows == sees(app, CLEO)


# -- races: what TLC found ---------------------------------------------------------------------------------------

def test_a_photo_made_private_is_not_sent_to_a_phone_before_its_feed_is_told(app, monkeypatch):
    """NeverSentUnseen: TLC's trace. Ben makes a photo he shared private: his relook takes it out of Ada's view, and the
    thread that tells the phones' feeds (hidden_changed's feed_update) has not run yet. Ada's phone asks: the photo is
    in her feed after her cursor, and the route asks only whether the catalog has it (`s in v.cat.meta`). Its row
    (place, people, description, path) is sent to a member who may not see it, and stays on her phone until a later
    sync deletes it."""
    y = plain(app, 3)
    share(app, y, BEN)
    monkeypatch.setattr(app, "feed_update", lambda: None)      # hidden_changed's feed_update, not yet run
    call(app, "POST", "/api/private", BEN, {"shas": [y], "private": True})
    settle(app)
    assert y not in seen(app, ADA)
    phone = Phone(ADA)
    page = phone.ask(app)
    assert y not in {r["sha"] for r in page["photos"]}, "a page never carries a row of a photo its member does not see"


def test_an_older_view_written_after_a_newer_one_does_not_take_a_photo_off_the_phone(app, monkeypatch):
    """AsSeenOnceQuiet: TLC's trace. Ben makes a photo private: the thread that tells the feeds reads Ada's rows (the
    photo not among them) and, before it writes them, Ben lets the photo go again. That relook's thread reads her rows
    (the photo back) and writes them: nothing changed. The first then writes the rows it read: the photo is gone from
    her feed. Ada sees it; her phone, synced once all is quiet, does not have it, and nothing will send it until the
    next change to what she sees."""
    y = plain(app, 3)
    share(app, y, BEN)
    phone = Phone(ADA)
    phone.sync(app)
    assert y in phone.rows
    reached, go = held_first(feed_of(app, ADA), "update")
    call(app, "POST", "/api/private", BEN, {"shas": [y], "private": True})
    assert reached.wait(GUARD)                                   # the first feed_update has read, and waits to write
    call(app, "POST", "/api/private", BEN, {"shas": [y], "private": False})
    wait_for(lambda: app.settling == 1, "the second relook's thread did not finish")
    go()
    settle(app)
    assert y in seen(app, ADA)
    phone.sync(app)
    assert phone.rows == sees(app, ADA), "once quiet, the phone has what its member sees"


def without(app, tmp_path, sha: str) -> catalog.Catalog:
    """The catalog as a reload would load it once `sha` is no longer in it (estate.py purge, then a reload): loaded from
    a copy of the scratch catalog with its rows deleted (so the store and the demo are not touched)."""
    copy = tmp_path / "without.db"
    src, dst = sqlite3.connect(app.a.db), sqlite3.connect(copy)
    src.backup(dst)
    src.close()
    for (name,) in dst.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
        dst.execute(f"DROP TRIGGER {name}")
    for (t,) in dst.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        if "sha256" in [r[1] for r in dst.execute(f"PRAGMA table_info({t})")]:
            dst.execute(f"DELETE FROM {t} WHERE sha256=?", (sha,))
    dst.commit()
    dst.close()
    return catalog.load(connect(str(copy)), app.a.model, app.cache, app.seed, None, demo=app.a.demo)


def test_a_cursor_never_passes_a_row_the_phone_was_not_sent(app, tmp_path, monkeypatch):
    """CursorNeverPasses: TLC's trace. A photo leaves the catalog (purged, then a reload swaps the catalog in) and the
    reload has not yet told the feeds. Ada's phone, new, asks: the feed still has the photo's row, the route skips it
    (not in the catalog) and answers a cursor past it. The phone's cursor says it has every row up to there, and it
    does not have that one (here harmless: the reload's feed_update then sends its deletion)."""
    w = plain(app, 6)
    gone = without(app, tmp_path, w)
    assert w not in gone.index
    monkeypatch.setattr(app, "load_catalog", lambda prev=None: gone)
    monkeypatch.setattr(app, "feed_update", lambda: None)      # the reload's feed_update, not yet run
    app.reload_catalog()
    assert w not in app.live.cat.index
    phone = Phone(ADA)
    phone.sync(app)
    feed = feed_of(app, ADA)
    passed = [s for s, (_, ver) in feed.rows.items() if (ver, s) <= (phone.since, phone.after) and s not in phone.rows]
    assert passed == [], "a cursor never passes a row of the feed the phone has not got"


def test_a_favourite_set_as_a_members_first_phone_asks_reaches_it(app, monkeypatch):
    """AsSeenOnceQuiet: TLC's trace. Cleo has no feed. She favourites a photo on the web: set_favorites asks for her
    feed (none), then waits for fav_write. Meanwhile her first phone asks: her feed is made from what she sees (the
    photo not a favourite). The favourite is then shown, and her feed not touched: set_favorites asked before it was
    made. Her phone never hears of the favourite, until something next changes that photo."""
    z = plain(app, 5)
    real, asked = app.fav_write, threading.Event()
    real.acquire()
    monkeypatch.setattr(app, "fav_write", Watched(real, asked))
    t, box = in_thread(call, app, "POST", "/api/favorite", CLEO, {"sha": z, "fav": True})
    assert asked.wait(GUARD)                                     # it has asked for Cleo's feed, and waits
    phone = Phone(CLEO)
    phone.sync(app)                                              # her first phone: her feed is made now
    real.release()
    assert finish(t, box)["fav"] is True
    phone.sync(app)
    assert phone.rows[z]["fav"] is True and phone.rows == sees(app, CLEO), "once quiet, the phone has what she sees"


def restarted(app) -> Feed:
    """Ada's feed as the app finds it when it starts again: read from its file (versions and digests as they were),
    not ready until start()'s feed_update has written."""
    feed = Feed(feed_of(app, ADA).path)
    app.feed = app.feeds[app.feed_member] = feed
    return feed


def test_a_favourite_set_while_a_feed_is_being_read_after_a_start_reaches_the_phone(app):
    """AsSeenOnceQuiet: TLC's trace. Ada's phone has synced. The app starts again: her feed is not ready, and start()'s
    feed_update has read her rows and not yet written them. She favourites a photo: set_favorites touches only a feed
    that is ready, so nothing is touched. The feed_update then writes the rows it read before the favourite: the
    photo's row is as the file had it, so nothing changed. Her phone never hears of the favourite."""
    z = plain(app, 5)
    phone = Phone(ADA)
    phone.sync(app)
    feed = restarted(app)
    assert not feed.ready
    reached, go = held_first(feed, "update")
    t, box = in_thread(app.feed_update)                          # start()'s
    assert reached.wait(GUARD)
    call(app, "POST", "/api/favorite", ADA, {"sha": z, "fav": True})
    go()
    finish(t, box)
    phone.sync(app)
    assert phone.rows[z]["fav"] is True and phone.rows == sees(app, ADA), "once quiet, the phone has what she sees"


def test_a_favourite_answers_what_the_photo_is_now(app):
    """AsSeenOnceQuiet (the phone's keep): TLC's trace. Ada's phone favourites a photo: set_favorites shows it and,
    before it answers, her laptop takes the favourite back (shown, and touched). The phone's answer says fav: true, and
    Favourites.set keeps what it asked for, so her phone says favourite though the server says not, and the feed,
    which sent nothing, gives it no reason to ask again."""
    z = plain(app, 5)
    reached, go = held_first(feed_of(app, ADA), "touch")
    t, box = in_thread(call, app, "POST", "/api/favorite", ADA, {"sha": z, "fav": True})
    assert reached.wait(GUARD)                                   # shown; its touch, and its answer, wait
    call(app, "POST", "/api/favorite", ADA, {"sha": z, "fav": False})
    go()
    got = finish(t, box)
    assert sees(app, ADA)[z]["fav"] is False
    assert got["fav"] is False, "the answer says what the photo is now, which is what the phone keeps"
