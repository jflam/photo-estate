"""Inbox items and what they are about (member_inbox.py and its callers: members.py, lenses.py, stories.py), on a
scratch catalog: the ways the model of them (specs/InboxItem.tla, checked by TLC) found an item could be open about
nothing, closed with something still waiting in it, or put away after it was decided, each replayed on the real code.

Two need no bad timing: a sysadmin who stops being one, and a lens taken back. The races land the other request on
a `Gap` connection, as tests/test_lens_states.py does: a request reads what it decides on with no lock held, because
Python begins its transaction only at the first write, and the other request runs just before the first statement
that writes or takes the write lock, on a connection of its own. stories.deal writes first with executemany when a
question joins an item, so the Gap watches that too.

  uv run pytest tests/test_inbox_states.py
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
import stories  # noqa: E402
from core import schema  # noqa: E402

ADA, DEE, BEN, CLEO = 1, 2, 3, 4          # Ada and Dee are the sysadmins; Ben owns the filter and the album
WRITES = re.compile(r"\s*(BEGIN|INSERT|UPDATE|DELETE|REPLACE)\b", re.I)
PHOTO = "1" * 64
LID, SID = 1, 1                           # Ben's filter, and his album


class Gap(sqlite3.Connection):
    """A request's own connection, on which `during()` runs once: just before the first statement that writes or
    takes the write lock (by execute or executemany)."""
    during = None

    def _gap(self, sql: str) -> None:
        if self.during is not None and WRITES.match(sql):
            fn, self.during = self.during, None
            fn()

    def execute(self, sql, *args):        # type: ignore[override]
        self._gap(sql)
        return super().execute(sql, *args)

    def executemany(self, sql, *args):    # type: ignore[override]
        self._gap(sql)
        return super().executemany(sql, *args)


def gap(db: str, *steps) -> Gap:
    """A Gap connection whose gap runs each of `steps` (fn(con), each as another request of the app's, on a
    connection of its own), in order."""
    con = sqlite3.connect(db, factory=Gap, timeout=60)
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


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "estate.db")
    con = estate.connect(path)
    schema.ensure(con, *schema.CATALOG_AREAS)
    for name, role in (("Ada", "sysadmin"), ("Dee", "sysadmin"), ("Ben", "member"), ("Cleo", "member")):
        members.add(con, name, name.lower(), role=role, status="active")
    lenses.create(con, BEN, "No screenshots")
    con.execute("INSERT INTO change_sets(title, op, status, owner) VALUES ('Trips', 'view', 'open', ?)", (BEN,))
    con.commit()
    con.close()
    return path


@pytest.fixture
def con(db):
    con = estate.connect(db)
    yield con
    con.close()


def items(con, kind: str, member: int | None = None) -> list[tuple[int, int, str, str]]:
    """(id, member, ref, state) of every item of `kind` (or `member`'s), oldest first."""
    q = "SELECT id, member_id, ref, state FROM inbox_items WHERE kind=?" + (" AND member_id=?" if member else "") + " ORDER BY id"
    return con.execute(q, (kind, member) if member else (kind,)).fetchall()


def opened(con, kind: str, member: int | None = None) -> list[tuple[int, int, str, str]]:
    return [it for it in items(con, kind, member) if it[3] == "open"]


def ask(con, text: str = "Who is this?") -> int:
    """A question to Cleo about one photo, in the group "Trip" (the estate's agent asking, for no one)."""
    return stories.ask(con, None, None, [PHOTO], text, to=CLEO, group_key="Trip")["question"]


def deal(con) -> int:
    return stories.deal(con, CLEO)


def answer(con, qid: int):
    return stories.answer(con, CLEO, qid, lambda s, m: False, text="Grandpa, in 1971")


def waiting_in(con, iid: int) -> list[int]:
    """The questions dealt in item `iid` that wait on Cleo (neither answered nor put away)."""
    return [q for q, in con.execute("SELECT question_id FROM asked WHERE item_id=? AND answered_at IS NULL"
                                    " AND dismissed_at IS NULL", (iid,))]


def attempt(fn):
    """What fn() answered, or the error (InboxError, LensError) it raised."""
    try:
        return fn()
    except (member_inbox.InboxError, lenses.LensError) as e:
        return e


# -- the normal path ---------------------------------------------------------------------------------------------

def test_items_open_and_close_as_their_things_do(db, con):
    """What must not change: a request for both sysadmins, decided; a proposal, decided; a lens Cleo follows that
    changes, then left; two questions dealt into one item and answered; her agent's binding, allowed; and an item put
    away by its member."""
    x = members.request(con, "xan@example.com", "Xan")
    assert [m for _, m, _, _ in opened(con, "member_request")] == [ADA, DEE]
    members.change(con, x.id, ADA, status="active")
    assert opened(con, "member_request") == [] and [s for *_, s in items(con, "member_request")] == ["done", "done"]

    lenses.propose(con, BEN, LID)
    assert [m for _, m, _, _ in opened(con, "lens_proposed")] == [ADA, DEE]
    lenses.decide(con, DEE, LID, False)
    assert opened(con, "lens_proposed") == []
    lenses.subscribe(con, CLEO, LID, "live")
    with con:
        assert member_inbox.lens_changed(con, BEN, lens=LID)
    assert len(opened(con, "lens_changed", CLEO)) == 1
    lenses.unsubscribe(con, CLEO, LID)
    assert opened(con, "lens_changed") == []

    q1 = ask(con)
    assert deal(con) == 1
    q2 = ask(con, "And this?")
    assert deal(con) == 0                                   # (it joins the open item, for nothing)
    (iid, _, ref, _), = opened(con, "question", CLEO)
    assert ref == "ask:Trip" and sorted(waiting_in(con, iid)) == [q1, q2]
    answer(con, q1)
    assert opened(con, "question") != []
    answer(con, q2)
    assert opened(con, "question") == [] and items(con, "question")[0][3] == "done"

    b = members.bind(con, CLEO, "node-laptop", "laptop")
    (bid, *_), = opened(con, "binding", CLEO)
    members.decide_binding(con, CLEO, b["id"], True, "node-phone")
    assert opened(con, "binding") == [] and items(con, "binding")[0][3] == "done"

    lenses.subscribe(con, CLEO, LID, "live")
    with con:
        (lc,) = member_inbox.lens_changed(con, BEN, lens=LID)
    assert member_inbox.dismiss(con, CLEO, lc)["state"] == "dismissed"
    assert attempt(lambda: member_inbox.dismiss(con, CLEO, lc)).status == 409
    assert attempt(lambda: member_inbox.dismiss(con, CLEO, bid)).status == 409


# -- with nothing in between: an order the pages allow -----------------------------------------------------------

@pytest.mark.parametrize("change", [{"role": "member"}, {"status": "revoked"}])
def test_a_sysadmin_no_longer_one_has_nothing_of_theirs_to_decide(db, con, change):
    """AskedOfASysadmin: Xan asks to be let in and Ben proposes his filter: Ada and Dee each have a member_request and
    a lens_proposed item. Dee makes Ada a member (or revokes her). Ada's items stay open, opening /members (403 for her
    now) and a proposal lenses.decide refuses her (403), and no decision of hers can close them."""
    members.request(con, "xan@example.com", "Xan")
    lenses.propose(con, BEN, LID)
    members.change(con, ADA, DEE, **change)
    assert opened(con, "member_request", ADA) == [] and opened(con, "lens_proposed", ADA) == [], \
        "an item asking a sysadmin to decide is held only by an active sysadmin"
    assert len(opened(con, "member_request", DEE)) == 1 and len(opened(con, "lens_proposed", DEE)) == 1


@pytest.mark.parametrize("how", ["publish", "publish_album", "publish the series"])
def test_a_lens_taken_back_leaves_nobody_told_of_it(db, con, how):
    """ToldWhileFollowing: Cleo follows Ben's filter (proposed, so published: TLC's trace) or his album, published as a
    series, live, and it changes: her lens_changed item. Ben takes it back (lenses.publish, or publish_album, or publish
    of the series row). She no longer sees through it nor can open it, unsubscribe answers 404, and her item stays
    open about a lens she cannot see."""
    if how == "publish":
        lenses.propose(con, BEN, LID)
        lid = LID
    else:
        lid = lenses.publish_album(con, BEN, SID, True)["lens"]
    lenses.subscribe(con, CLEO, lid, "live")
    with con:
        assert member_inbox.lens_changed(con, BEN, **({"lens": LID} if how == "publish" else {"album": SID}))
    if how == "publish_album":
        lenses.publish_album(con, BEN, SID, False)
    else:
        lenses.publish(con, BEN, lid, False)
    assert opened(con, "lens_changed", CLEO) == [], "an open lens_changed item is about a lens its member follows"
    assert attempt(lambda: lenses.unsubscribe(con, CLEO, lid)).status == 404


# -- races: another request in the gap ---------------------------------------------------------------------------

def test_one_open_question_item_however_many_read_the_inbox_at_once(db, con):
    """OneOpenItem, QuestionWhileWaiting: a question waits for Cleo. Two of her requests read her inbox at once (two
    tabs, the phone; GET /api/inbox and GET /api/questions both deal): each finds no item for its group and inserts
    one. The second's asked row is ignored (the question is dealt already), so its item holds no question: never
    shown (keep_item), never done, and the group's next question joins it, as a second item for one group."""
    ask(con)
    deal(gap(db, deal))
    got = opened(con, "question", CLEO)
    assert len(got) == 1, "one open item per member, kind and ref"
    assert all(waiting_in(con, iid) for iid, *_ in got), "an open question item has a question waiting in it"


def test_a_question_is_dealt_into_an_item_still_open(db, con):
    """DoneWhenAnswered: Cleo's item holds one question; a second of its group is asked. Her deal reads the item open;
    before it writes, she answers the first, and _settle marks the item done. The deal then joins the second question
    to the done item: dealt, never shown (its item is not open) and never dealt again."""
    q1 = ask(con)
    deal(con)
    (iid, *_), = opened(con, "question", CLEO)
    q2 = ask(con, "And this?")
    deal(gap(db, lambda c: answer(c, q1)))
    done = [i for i, *_, s in items(con, "question") if s == "done"]
    assert not any(waiting_in(con, i) for i in done), "an item is done once each question in it is answered or put away"
    assert [w for i, *_ in opened(con, "question") for w in waiting_in(con, i)] == [q2]


def test_no_item_is_opened_for_a_question_answered_meanwhile(db, con):
    """QuestionWhileWaiting: a question waits for Cleo. One request of hers reads it, to deal it; before it writes,
    another deals it and she answers it (its item is done). The first then finds no open item, inserts one, and deals
    nothing into it (the question is dealt already): an open item with no question, shown to nobody, never done."""
    q1 = ask(con)
    deal(gap(db, deal, lambda c: answer(c, q1)))
    assert all(waiting_in(con, iid) for iid, *_ in opened(con, "question")), \
        "an open question item has a question waiting in it"


def test_an_item_put_away_was_open_when_it_was(db, con):
    """DismissOnlyOpen: Ada puts her member_request item away; dismiss reads it open, and before it writes, Dee turns
    Xan away (members.change closes every sysadmin's item: done). Ada's UPDATE then writes dismissed over done, and
    journals inbox.dismissed, where alone it would have answered 409."""
    x = members.request(con, "xan@example.com", "Xan")
    (iid, *_), = opened(con, "member_request", ADA)
    got = attempt(lambda: member_inbox.dismiss(gap(db, lambda c: members.change(c, x.id, DEE, status="revoked")), ADA, iid))
    assert items(con, "member_request", ADA)[0][3] == "done", "a dismiss puts away only an item that is open"
    assert isinstance(got, member_inbox.InboxError) and got.status == 409
    assert not con.execute("SELECT 1 FROM journal WHERE kind='inbox.dismissed'").fetchone()
