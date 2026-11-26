"""A lens's row, a subscription to it and an album's series row (lenses.py), on a scratch catalog: the ways the
model of them (specs/Lens.tla, checked by TLC) found they could end up where no one asked, each replayed on the
real code. Every one is a race: a request reads the lens (or the album's series row, or the member's subscription)
before its transaction, which Python's `with con:` begins only at the first write, and another request commits
in between, on a connection of its own, as two requests of the app's do on its threads.

There is no call of the code's own between those reads and the first write to stand the other request in, as
`judge()` is in tests/test_private_states.py, so the request runs on a `Gap` connection instead: the other request
runs just before the first statement that writes or takes the write lock, which is the end of what the request
reads with no lock held. A test that needs it earlier (before the request's second read) names that read too.
Once the code reads inside its transaction, that moment is before all its reads, and the other request simply
comes first.

  uv run pytest tests/test_lens_states.py
"""
import re
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import estate  # noqa: E402
import lenses  # noqa: E402
import member_inbox  # noqa: E402
import members  # noqa: E402
from core import schema  # noqa: E402

ADA, BEN, CLEO, DEE = 1, 2, 3, 4          # Ada and Dee are the sysadmins; Ben owns the filter and the album
WRITES = re.compile(r"\s*(BEGIN|INSERT|UPDATE|DELETE|REPLACE)\b", re.I)


class Gap(sqlite3.Connection):
    """A request's own connection, on which `during()` runs once: just before the first statement that writes or
    takes the write lock, or one that matches `before` if that comes first."""
    during = None
    before: str | None = None

    def execute(self, sql, *args):        # type: ignore[override]
        if self.during is not None and (WRITES.match(sql) or self.before and re.search(self.before, sql)):
            fn, self.during = self.during, None
            fn()
        return super().execute(sql, *args)


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "estate.db")
    con = estate.connect(path)
    schema.ensure(con, *schema.CATALOG_AREAS)
    for name, role in (("Ada", "sysadmin"), ("Ben", "member"), ("Cleo", "member"), ("Dee", "sysadmin")):
        members.add(con, name, name.lower(), role=role, status="active")
    lenses.create(con, BEN, "No screenshots")
    con.execute("INSERT INTO change_sets(title, op, status, owner) VALUES ('Trips', 'view', 'open', ?)", (BEN,))
    con.commit()
    con.close()
    return path


LID, SID = 1, 1                           # Ben's filter, and his album


@pytest.fixture
def con(db):
    con = estate.connect(db)
    yield con
    con.close()


def request(db: str, *steps):
    """A request of the app's on a Gap connection: each of `steps` (fn(c), on a connection of its own, as another
    request of the app's) runs in its gap, in order."""
    con = sqlite3.connect(db, factory=Gap)
    con.execute("PRAGMA journal_mode=WAL")

    def others():
        for fn in steps:
            c = estate.connect(db)
            try:
                fn(c)
            finally:
                c.close()
    con.during = others
    return con


def attempt(fn):
    """What fn() answered, or the LensError it raised."""
    try:
        return fn()
    except lenses.LensError as e:
        return e


def refused(got, status: int = 409) -> bool:
    return isinstance(got, lenses.LensError) and got.status == status


def flags(con) -> tuple[bool, bool, bool]:
    """(published, proposed, estate default) of Ben's filter."""
    lens = lenses.get(con, LID)
    assert lens is not None
    return lens.published, bool(lens.proposed_at), lens.estate_default


def kinds(con, like: str = "lens.%") -> list[str]:
    """The journal's entries since the fixture made the lens (lens.created)."""
    return [k for k, in con.execute("SELECT kind FROM journal WHERE kind LIKE ? AND kind != 'lens.created' ORDER BY id", (like,))]


def asked(con) -> list[int]:
    """Who has an open lens_proposed item about Ben's filter."""
    return [m for m, in con.execute("SELECT member_id FROM inbox_items WHERE kind='lens_proposed' AND ref=? AND state='open'"
                                    " ORDER BY member_id", (f"lens:{LID}",))]


def series(con) -> list[tuple[int, bool]]:
    """(id, published) of each series row of Ben's album."""
    return [(i, bool(p)) for i, p in con.execute("SELECT id, published FROM lenses WHERE kind='series' AND set_id=? ORDER BY id", (SID,))]


def sub(con, member=CLEO):
    row = con.execute("SELECT mode FROM subscriptions WHERE member_id=? AND lens_id=?", (member, LID)).fetchone()
    return row and row[0]


# -- the normal path, with nothing in between ------------------------------------------------------------------

def test_the_lens_moves_as_its_owner_and_a_sysadmin_ask(db, con):
    """What must not change: each request alone, and a gap that nothing lands in."""
    assert lenses.publish(request(db), BEN, LID, True)["changed"]
    assert lenses.subscribe(request(db), CLEO, LID, "live")["changed"]
    assert not lenses.subscribe(request(db), CLEO, LID, "live")["changed"]
    assert lenses.propose(request(db), BEN, LID)["changed"] and asked(con) == [ADA, DEE]
    assert lenses.decide(request(db), ADA, LID, True)["estate_default"]
    assert flags(con) == (True, False, True) and asked(con) == []
    assert refused(attempt(lambda: lenses.publish(request(db), BEN, LID, False)))
    assert refused(attempt(lambda: lenses.decide(request(db), DEE, LID, False)))
    assert lenses.unsubscribe(request(db), CLEO, LID)["mode"] == "opted_out" and sub(con) == "opted_out"
    assert lenses.subscribe(request(db), CLEO, LID, "live")["changed"] and sub(con) is None
    assert lenses.publish_album(request(db), BEN, SID, True)["changed"]
    assert not lenses.publish_album(request(db), BEN, SID, True)["changed"]
    assert lenses.publish_album(request(db), BEN, SID, False)["changed"] and [p for _, p in series(con)] == [False]
    assert kinds(con) == ["lens.published", "lens.subscribed", "lens.proposed", "lens.accepted", "lens.unsubscribed",
                          "lens.subscribed"]
    assert kinds(con, "album.%") == ["album.published", "album.unpublished"]


# -- the owner and a sysadmin at once ---------------------------------------------------------------------------

def test_a_proposal_taken_back_as_a_sysadmin_accepts_it_is_not_accepted(db, con):
    """DefaultAsProposed: Ada reads the proposal; Ben takes the filter back; Ada's accept writes over it."""
    lenses.propose(con, BEN, LID)
    got = attempt(lambda: lenses.decide(request(db, lambda c: lenses.publish(c, BEN, LID, False)), ADA, LID, True))
    assert flags(con) == (False, False, False), "taken back, so not an estate default"
    assert refused(got) and kinds(con) == ["lens.proposed", "lens.unpublished"]


def test_a_proposal_taken_back_as_a_sysadmin_declines_it_is_not_declined(db, con):
    """NoEmptyEntry: the same, declined: a lens.declined entry for a proposal that was no longer there."""
    lenses.propose(con, BEN, LID)
    got = attempt(lambda: lenses.decide(request(db, lambda c: lenses.publish(c, BEN, LID, False)), ADA, LID, False))
    assert kinds(con) == ["lens.proposed", "lens.unpublished"]
    assert refused(got) and flags(con) == (False, False, False)


def test_a_filter_accepted_as_its_owner_takes_it_back_stays_published(db, con):
    """DefaultStaysPublished: Ben reads his filter, proposed; Ada accepts it; Ben's unpublish writes over it."""
    lenses.propose(con, BEN, LID)
    got = attempt(lambda: lenses.publish(request(db, lambda c: lenses.decide(c, ADA, LID, True)), BEN, LID, False))
    assert flags(con) == (True, False, True), "an estate default stays published"
    assert lenses.read(con).on(CLEO) == {LID}
    assert refused(got) and kinds(con) == ["lens.proposed", "lens.accepted"]


def test_a_filter_accepted_as_its_owner_proposes_it_again_is_not_proposed(db, con):
    """DefaultNotProposed: Ben proposes twice at once; between the second's read and its write the first lands and
    Ada accepts it; the second proposes an estate default."""
    got = attempt(lambda: lenses.propose(request(db, lambda c: lenses.propose(c, BEN, LID),
                                                 lambda c: lenses.decide(c, ADA, LID, True)), BEN, LID))
    assert flags(con) == (True, False, True) and asked(con) == [], "an estate default is not waiting to be decided"
    assert refused(got) and kinds(con) == ["lens.proposed", "lens.accepted"]


def test_two_sysadmins_deciding_at_once_decide_once(db, con):
    """DefaultAsProposed: Ada and Dee read the proposal; Ada declines; Dee's accept writes over the decline."""
    lenses.propose(con, BEN, LID)
    got = attempt(lambda: lenses.decide(request(db, lambda c: lenses.decide(c, ADA, LID, False)), DEE, LID, True))
    assert flags(con) == (True, False, False), "declined: it stays Ben's, published"
    assert refused(got) and kinds(con) == ["lens.proposed", "lens.declined"]


# -- the owner, twice at once (two tabs, the phone and the page, the API) ---------------------------------------

def test_a_filter_taken_back_as_it_is_proposed_leaves_no_question_open(db, con):
    """AskedWhileProposed: Ben's unpublish reads his filter, not proposed; his propose lands; the unpublish clears
    the proposal and leaves the sysadmins' lens_proposed items open, about nothing."""
    lenses.publish(con, BEN, LID, True)
    lenses.publish(request(db, lambda c: lenses.propose(c, BEN, LID)), BEN, LID, False)
    assert flags(con) == (False, False, False)
    assert asked(con) == [], "nothing is left for a sysadmin to decide"


def test_publishing_twice_at_once_appends_one_entry(db, con):
    """NoEmptyEntry: two publishes read the filter unpublished; both write, and both append lens.published."""
    got = lenses.publish(request(db, lambda c: lenses.publish(c, BEN, LID, True)), BEN, LID, True)
    assert kinds(con) == ["lens.published"], "a write that changes nothing appends nothing"
    assert not got["changed"] and flags(con) == (True, False, False)


def test_an_album_published_twice_at_once_is_one_series(db, con):
    """OneSeriesRow: two publish_album read no series row; each adds one."""
    got = lenses.publish_album(request(db, lambda c: lenses.publish_album(c, BEN, SID, True)), BEN, SID, True)
    assert len(series(con)) == 1, "its lenses row, made the first time"
    assert not got["changed"] and kinds(con, "album.%") == ["album.published"]


def test_an_album_taken_back_while_published_twice_has_one_word(db, con):
    """SeriesAsAsked: two publish_album read no series row; the first adds it, Ben takes the album back, and the
    second adds another, published, which the album's page (it reads the first) never shows and cannot take back."""
    lenses.publish_album(request(db, lambda c: lenses.publish_album(c, BEN, SID, True),
                                 lambda c: lenses.publish_album(c, BEN, SID, False)), BEN, SID, True)
    rows = series(con)
    assert len(rows) == 1 and rows[0][1], "one series row, as Ben last said"
    assert lenses.publish_album(con, BEN, SID, False)["changed"]
    assert not any(p for _, p in series(con)), "taken back, it is published nowhere"


# -- a member, twice at once, as the filter becomes an estate default ------------------------------------------

def test_leaving_a_filter_as_it_becomes_an_estate_default_leaves_it(db, con):
    """LeftNotTold: Cleo follows Ben's filter live and has an item saying it changed. Her unsubscribe reads the
    filter (not an estate default); Ada accepts it, and Cleo's subscribe (from another tab) takes it back as an
    estate default, deleting her row; her unsubscribe then reads no row, and answers that there was nothing to do.
    She still sees through the filter, and her lens_changed item is still open."""
    lenses.propose(con, BEN, LID)
    lenses.subscribe(con, CLEO, LID, "live")
    with con:
        assert member_inbox.lens_changed(con, BEN, lens=LID)
    c = request(db, lambda c: lenses.decide(c, ADA, LID, True), lambda c: lenses.subscribe(c, CLEO, LID, "live"))
    c.before = r"SELECT mode FROM subscriptions"
    got = lenses.unsubscribe(c, CLEO, LID)
    assert LID not in lenses.read(con).on(CLEO), "she left it"
    assert sub(con) == "opted_out" and got["changed"]
    assert not con.execute("SELECT 1 FROM inbox_items WHERE kind='lens_changed' AND member_id=? AND state='open'",
                           (CLEO,)).fetchone()
