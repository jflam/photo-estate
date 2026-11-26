"""An album or change set, its photos, its series row and the inbox items about it (changes.py, and the writers beside
it: lenses.py, the series listener), on a scratch catalog: the ways the model of them (specs/ChangeSet.tla, checked by
TLC) found they could end up where no one asked, each replayed on the real code.

Most need no bad timing: a request the page or an agent's tools send, in an order they allow. The races land the other
request on a `Gap` connection, as tests/test_lens_states.py does: a request reads what it decides on with no lock held,
because Python begins its transaction only at the first write, and the other request runs just before the first
statement that writes or takes the write lock, on a connection of its own. Changes.write opens its own connection, so
here it is a Changes whose next connection is a Gap (GapChanges); the series listener is handed one.

  uv run pytest tests/test_change_set_states.py
"""
import re
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import changes  # noqa: E402
import estate  # noqa: E402
import journal  # noqa: E402
import lenses  # noqa: E402
import listeners  # noqa: E402
import members  # noqa: E402
from core import schema  # noqa: E402

ADA, BEN = 1, 2                           # Ada is the sysadmin; Ben owns the album
S1, S2 = "1" * 64, "2" * 64               # what the album's search finds now, and a photo ingested later
WRITES = re.compile(r"\s*(BEGIN|INSERT|UPDATE|DELETE|REPLACE)\b", re.I)
TRIPS = {"query": "trips"}                # the search Ben's series grows from


class Gap(sqlite3.Connection):
    """A request's own connection, on which `during()` runs once: just before the first statement that writes or
    takes the write lock."""
    during = None

    def execute(self, sql, *args):        # type: ignore[override]
        if self.during is not None and WRITES.match(sql):
            fn, self.during = self.during, None
            fn()
        return super().execute(sql, *args)


def gap(db: str, *steps) -> Gap:
    """A Gap connection whose gap runs each of `steps` (fn(), each as another request of the app's), in order."""
    con = sqlite3.connect(db, factory=Gap, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.during = lambda: [fn() for fn in steps]
    return con


class GapChanges(changes.Changes):
    """changes.Changes, whose next connection (so its next write's) is a Gap running `steps` in its gap."""
    steps: tuple = ()

    def con(self) -> sqlite3.Connection:
        if not self.steps:
            return super().con()
        con, self.steps = gap(self.db, *self.steps), ()
        return con


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "estate.db")
    con = estate.connect(path)
    schema.ensure(con, *schema.CATALOG_AREAS)
    for name, role in (("Ada", "sysadmin"), ("Ben", "member")):
        members.add(con, name, name.lower(), role=role, status="active")
    con.executemany("INSERT INTO assets(sha256, ext, kind) VALUES (?, '.jpg', 'image')", ((S1,), (S2,)))
    con.commit()
    con.close()
    return path


@pytest.fixture
def con(db):
    con = estate.connect(db)
    yield con
    con.close()


@pytest.fixture
def c(db) -> GapChanges:
    """The app's Changes on the scratch catalog: both photos are in the library, and a search finds S1 (S2 is not
    in the catalog the app serves until the listener's ingest)."""
    return GapChanges(db, facts=lambda sha: {} if sha in (S1, S2) else None, shown=lambda sha, viewer: {},
                      find=lambda query, viewer: ([S1] if query.get("query") else [], None))


def other(db: str, fn):
    """fn(con) as another request of the app's, on a connection of its own."""
    def run():
        c = estate.connect(db)
        try:
            return fn(c)
        finally:
            c.close()
    return run


def ok(got) -> dict:
    body, code = got
    assert code in (200, 201), body
    return body


def refused(got, code: int = 409) -> bool:
    return got[1] == code


def series(c) -> int:
    """Ben's series: an album of what its search finds now (S1), that new photos join."""
    return ok(c.create({"title": "Trips", "op": "view", "grows_from": TRIPS}, actor=BEN))["id"]


def ingest(db: str, sid: int, con=None) -> None:
    """The series listener, after an ingest that added S2: its search finds S2, which the album does not hold, and
    Ben's series_update item is opened (listeners.series). On `con`, a connection of the caller's (a Gap)."""
    c0 = estate.connect(db)
    with c0:
        e = journal.append(c0, None, "ingest.finished", "source:iphone", {"new": 1, "assets": [1, 2]})
    c0.close()
    env = listeners.Env(find=lambda search, m: [S1, S2], loaded=lambda: 10**9, in_catalog=lambda sha: True,
                        refused=lambda sha, m: False, submit=lambda *a: None)
    use = con if con is not None else estate.connect(db)
    try:
        listeners.series(use, {"id": e, "kind": "ingest.finished", "data": {"assets": [1, 2]}}, env)
    finally:
        use.close()


def waiting(con, sid: int) -> bool:
    """Ben has an open series_update item about the album."""
    return con.execute("SELECT 1 FROM inbox_items WHERE kind='series_update' AND ref=? AND state='open'",
                       (f"album:{sid}",)).fetchone() is not None


def row(con, sid: int) -> tuple:
    """(op, status, grows_from is set) of the album's change_sets row."""
    return con.execute("SELECT op, status, grows_from IS NOT NULL FROM change_sets WHERE id=?", (sid,)).fetchone()


def published(con, sid: int) -> bool:
    """The album's series row is published."""
    return bool((con.execute("SELECT published FROM lenses WHERE kind='series' AND set_id=?", (sid,)).fetchone() or [0])[0])


def a_series(con, sid: int) -> bool:
    """What the listener and Changes.candidates take a series to be: a list album to look at, with a search."""
    op, status, grows = row(con, sid)
    return op == "view" and status != "discarded" and bool(grows)


def summary(c, sid: int) -> dict:
    con = c.con()
    try:
        got = c.summary(con, sid, BEN)
        assert got is not None
        return got
    finally:
        con.close()


# -- the normal path ---------------------------------------------------------------------------------------------

def test_an_album_moves_as_its_owner_asks(db, con, c):
    """What must not change: a series is offered its new photos and takes them; then, with nothing waiting and not
    published, it is put up for hiding, applied, committed, reopened, reverted, made an album again, published, taken
    back and put away."""
    sid = series(c)
    ingest(db, sid)
    assert waiting(con, sid)
    assert ok(c.write(sid, "candidates", {"add": [S2]}, actor=BEN))["n"] == 2 and not waiting(con, sid)
    assert ok(c.write(sid, "hide", {}, actor=BEN))["state"] == "review"
    assert ok(c.write(sid, "apply", {}, actor=BEN))["state"] == "applied"
    assert ok(c.write(sid, "commit", {}, actor=BEN))["state"] == "done"
    assert refused(c.write(sid, "items", {"remove": [S1]}, actor=BEN))
    assert ok(c.write(sid, "reopen", {}, actor=BEN))["state"] == "applied"
    assert ok(c.write(sid, "revert", {}, actor=BEN))["state"] == "reverted"
    assert ok(c.write(sid, "view", {}, actor=BEN))["state"] == "album"
    assert lenses.publish_album(con, BEN, sid, True)["changed"] and published(con, sid)
    lid = lenses.publish_album(con, BEN, sid, False)["lens"]
    assert lenses.publish(con, BEN, lid, True)["changed"] and lenses.publish(con, BEN, lid, False)["changed"]
    assert ok(c.write(sid, "discard", {}, actor=BEN)) == {"discarded": sid}
    assert row(con, sid)[1] == "discarded" and not published(con, sid)


# -- with nothing in between: an order the page and the agent's tools allow ------------------------------------

def test_a_committed_set_is_as_committed(db, con, c):
    """DoneAsCommitted, DoneHasNothingToDo: Ben's series has new photos waiting (its series_update item). He puts it
    up for hiding, applies and commits it, then adds the new photo from the inbox (candidates): a set stored as done
    with a photo not yet as marked, which write's refusals, reopen and the review page no longer take to be done."""
    sid = series(c)
    ingest(db, sid)
    if not refused(c.write(sid, "hide", {}, actor=BEN)):
        ok(c.write(sid, "apply", {}, actor=BEN))
        ok(c.write(sid, "commit", {}, actor=BEN))
    c.write(sid, "candidates", {"add": [S2]}, actor=BEN)
    s = summary(c, sid)
    assert s["status"] != "done" or (s["state"] == "done" and s["n"] == 1), "nothing about it changes until it is reopened"


def test_an_album_with_new_photos_waiting_is_not_put_up_for_hiding(db, con, c):
    """WaitingForASeries: Ben's series has new photos waiting; he puts it up for hiding. The item stays open, for an
    album that is no series now, and adds its photos to the hiding set (the test above)."""
    sid = series(c)
    ingest(db, sid)
    got = c.write(sid, "hide", {}, actor=BEN)
    assert not waiting(con, sid) or a_series(con, sid), "new photos wait only for a series"
    assert refused(got) and row(con, sid) == ("view", "open", 1)


def test_an_album_with_new_photos_waiting_is_not_put_away(db, con, c):
    """WaitingForASeries: the same, discarded: the item opens an album that answers 404, and can never be done."""
    sid = series(c)
    ingest(db, sid)
    got = c.write(sid, "discard", {}, actor=BEN)
    assert not waiting(con, sid) or a_series(con, sid), "new photos wait only for a series"
    assert refused(got) and row(con, sid) == ("view", "open", 1)


def test_a_published_series_is_not_put_up_for_hiding(db, con, c):
    """PublishedIsAnAlbum: Ben publishes his album as a series, then puts it up for hiding. Its series stays
    published, its subscribers' album is gone for them, and publish_album (409 for a set up for hiding) cannot take
    it back."""
    sid = ok(c.create({"title": "Trips", "op": "view", "items": [S1]}, actor=BEN))["id"]
    lenses.publish_album(con, BEN, sid, True)
    got = c.write(sid, "hide", {}, actor=BEN)
    assert not published(con, sid) or row(con, sid)[0] == "view", "a set up for hiding is not published"
    assert refused(got) and published(con, sid)


def test_a_published_series_is_not_put_away(db, con, c):
    """PublishedIsAnAlbum: the same, discarded: the series stays published, in every member's lenses, for an album
    that answers no request, and publish_album (404 for a discarded album) cannot take it back."""
    sid = ok(c.create({"title": "Trips", "op": "view", "items": [S1]}, actor=BEN))["id"]
    lenses.publish_album(con, BEN, sid, True)
    got = c.write(sid, "discard", {}, actor=BEN)
    assert not published(con, sid) or row(con, sid)[1] != "discarded", "a discarded album is published nowhere"
    assert refused(got) and published(con, sid)


@pytest.mark.parametrize("what", ["hide", "discard"])
def test_the_series_of_an_album_up_for_hiding_or_put_away_is_not_published(db, con, c, what):
    """PublishedIsAnAlbum: Ben publishes his album, takes it back, puts it up for hiding (or away), then publishes its
    series by the lens's own route (POST /api/lenses/<id>/publish), which reads nothing of the album."""
    sid = ok(c.create({"title": "Trips", "op": "view", "items": [S1]}, actor=BEN))["id"]
    lenses.publish_album(con, BEN, sid, True)
    lid = lenses.publish_album(con, BEN, sid, False)["lens"]
    ok(c.write(sid, what, {}, actor=BEN))
    try:
        lenses.publish(con, BEN, lid, True)
        got = None
    except lenses.LensError as e:
        got = e.status
    assert not published(con, sid) or (row(con, sid)[0] == "view" and row(con, sid)[1] != "discarded"), \
        "a published series is an album there to look at"
    assert got == 409


# -- races: another request in the gap -------------------------------------------------------------------------

@pytest.mark.parametrize("what", ["grow", "hide", "discard"])
def test_new_photos_wait_only_for_a_series(db, con, c, what):
    """WaitingForASeries: the series listener reads Ben's series and runs its search; before it opens his item, he
    gives the album no search (grow), puts it up for hiding or away. The item is opened anyway (for a hiding set, it
    then adds its photos to the set even once committed: test_a_committed_set_is_as_committed)."""
    sid = series(c)
    body = {"grows_from": None} if what == "grow" else {}
    ingest(db, sid, gap(db, lambda: ok(c.write(sid, what, body, actor=BEN))))
    assert not waiting(con, sid) or a_series(con, sid), "new photos wait only for a series"
    assert not a_series(con, sid)


@pytest.mark.parametrize("what", ["hide", "discard"])
def test_an_album_published_as_it_is_put_up_for_hiding_or_away_is_one_or_the_other(db, con, c, what):
    """PublishedIsAnAlbum, once hide and discard look at the series: Ben's hide (or discard) reads the album, not
    published; his publish_album from another tab lands; the write goes by what it read."""
    sid = ok(c.create({"title": "Trips", "op": "view", "items": [S1]}, actor=BEN))["id"]
    c.steps = (other(db, lambda c2: lenses.publish_album(c2, BEN, sid, True)),)
    got = c.write(sid, what, {}, actor=BEN)
    assert not published(con, sid) or (row(con, sid)[0] == "view" and row(con, sid)[1] != "discarded"), \
        "a published series is an album there to look at"
    assert refused(got) and published(con, sid)


@pytest.mark.parametrize("what", ["hide", "discard"])
def test_an_album_new_photos_are_found_for_as_it_is_put_up_for_hiding_or_away_is_one_or_the_other(db, con, c, what):
    """WaitingForASeries, once hide and discard look at the series_update item: Ben's hide (or discard) reads the
    album, nothing waiting; the listener opens his item (it is a series still); the write goes by what it read."""
    sid = series(c)
    c.steps = (lambda: ingest(db, sid),)
    got = c.write(sid, what, {}, actor=BEN)
    assert not waiting(con, sid) or a_series(con, sid), "new photos wait only for a series"
    assert refused(got) and waiting(con, sid)
