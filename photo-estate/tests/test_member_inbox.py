"""member_inbox.py on a scratch catalog, with no app: an item is one open row per member, kind and ref, brought
up to date rather than repeated; a member lists and dismisses only their own; only dismissing is journaled.
Each kind made by the code that needs the decision and closed by the decision: a request to be let in
(members.py), a filter proposed as an estate default (lenses.py), a published lens that changed (for its live
subscribers, once), and a hiding set a task made (tasks.py). The same kinds through the running app, and its
endpoints: tests/test_multiplayer.py's inbox tests; on the pages: tests/test_inbox_browser.py.

  uv run pytest tests/test_member_inbox.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import lenses  # noqa: E402
import member_inbox  # noqa: E402
import members  # noqa: E402
from core import schema  # noqa: E402
from tasks import Tasks  # noqa: E402

SHAS = [f"{i:02x}" * 32 for i in range(6)]


@pytest.fixture
def db(tmp_path):
    con = sqlite3.connect(tmp_path / "estate.db")
    schema.ensure(con, *schema.CATALOG_AREAS)
    ada, ben, cleo = members.demo(con)
    yield SimpleNamespace(con=con, ada=ada.id, ben=ben.id, cleo=cleo.id)
    con.close()


def items(con, member) -> list[tuple]:
    return [(x["kind"], x["ref"]) for x in member_inbox.mine(con, member)["items"]]


def rows(con, kind=None) -> list[tuple]:
    q = "SELECT member_id, kind, ref, state FROM inbox_items" + (" WHERE kind=?" if kind else "") + " ORDER BY id"
    return con.execute(q, (kind,) if kind else ()).fetchall()


def journal_kinds(con) -> list[str]:
    return [k for k, in con.execute("SELECT kind FROM journal ORDER BY id")]


def test_one_open_item_per_member_kind_and_ref_brought_up_to_date(db):
    con = db.con
    with con:
        a = member_inbox.put(con, [db.ben, db.cleo, None, db.ben], "lens_changed", "lens:7", "it hides 1 photo now")
        b = member_inbox.put(con, [db.ben], "lens_changed", "lens:7", "it hides 2 photos now")
        member_inbox.put(con, [db.ben], "lens_changed", "lens:8", "another lens")
    assert len(a) == 2 and b == a[:1], "None is no one, and Ben's item is updated, not made again"
    got = member_inbox.mine(con, db.ben)
    assert got["open"] == 2 and [x["ref"] for x in got["items"]] == ["lens:8", "lens:7"], "newest first"
    assert got["items"][1]["title"] == "it hides 2 photos now"
    assert got["items"][1]["opens"] == "/?albums"
    assert items(con, db.cleo) == [("lens_changed", "lens:7")] and items(con, db.ada) == []
    with con:
        assert member_inbox.close(con, "lens_changed", "lens:7", db.ben) == 1
    assert items(con, db.ben) == [("lens_changed", "lens:8")] and items(con, db.cleo) == [("lens_changed", "lens:7")]
    with con:
        member_inbox.put(con, [db.ben], "lens_changed", "lens:7", "again")
    assert len(rows(con)) == 4, "a closed item stays; a new change is a new open one"
    assert not [k for k in journal_kinds(con) if k.startswith("inbox")], "made and done are not journaled"


def test_what_each_kind_opens():
    o = member_inbox.OPENS
    assert o["member_request"]("member:4") == "/members"
    assert o["review"]("album:12") == "/changes/12"
    assert o["lens_proposed"]("lens:3") == "/?albums"
    assert o["lens_changed"]("lens:3") == "/?albums" and o["lens_changed"]("album:9") == "/?album=9"
    assert o["binding"]("binding:2") == "/agents"
    with pytest.raises(AssertionError):
        member_inbox.put(sqlite3.connect(":memory:"), [1], "photo_frame", "device:1", "not a kind yet")


def test_a_member_dismisses_only_their_own_and_it_is_journaled(db):
    con = db.con
    with con:
        (iid,) = member_inbox.put(con, [db.ben], "lens_changed", "lens:7", "changed")
    with pytest.raises(member_inbox.InboxError) as e:
        member_inbox.dismiss(con, db.cleo, iid)
    assert e.value.status == 404
    assert member_inbox.dismiss(con, db.ben, iid) == {"id": iid, "state": "dismissed", "open": 0}
    with pytest.raises(member_inbox.InboxError) as e:
        member_inbox.dismiss(con, db.ben, iid)
    assert e.value.status == 409
    assert con.execute("SELECT actor, kind, subject, data FROM journal WHERE kind LIKE 'inbox.%'").fetchall() == \
        [(db.ben, "inbox.dismissed", f"inbox:{iid}", '{"kind":"lens_changed","ref":"lens:7"}')]
    assert rows(con) == [(db.ben, "lens_changed", "lens:7", "dismissed")]


def test_a_request_to_be_let_in_is_in_every_sysadmins_inbox_until_decided(db):
    con = db.con
    members.change(con, db.cleo, db.ada, role="sysadmin")
    dana = members.request(con, "dana@example.com", "Dana")
    want = [(db.ada, "member_request", f"member:{dana.id}", "open"), (db.cleo, "member_request", f"member:{dana.id}", "open")]
    assert rows(con) == want
    assert member_inbox.mine(con, db.ada)["items"][0]["title"] == "Dana asks to be let in (dana@example.com)"
    assert items(con, db.ben) == [] and items(con, dana.id) == []
    members.request(con, "dana@example.com", "Dana")                      # asking again changes nothing
    members.change(con, dana.id, db.ada, name="Dana B")                    # nor does anything but deciding
    assert [r[3] for r in rows(con)] == ["open", "open"]
    members.change(con, dana.id, db.ada, status="active")                 # Ada lets her in: done for both
    assert [r[3] for r in rows(con)] == ["done", "done"]
    eve = members.request(con, "eve@example.com", "Eve")
    members.change(con, eve.id, db.cleo, status="revoked")                # turned away: decided too
    assert [r[3] for r in rows(con) if r[2] == f"member:{eve.id}"] == ["done", "done"]
    assert "member.requested" in journal_kinds(con) and not [k for k in journal_kinds(con) if k.startswith("inbox")]


def test_a_proposed_filter_waits_for_the_sysadmins_until_decided_or_taken_back(db):
    con = db.con
    lid = lenses.create(con, db.ben, "No screenshots")
    lenses.propose(con, db.ben, lid)
    assert rows(con) == [(db.ada, "lens_proposed", f"lens:{lid}", "open")]
    assert member_inbox.mine(con, db.ada)["items"][0]["title"] == "Ben proposes the filter “No screenshots” as an estate default"
    lenses.decide(con, db.ada, lid, accept=False)
    assert rows(con)[-1][3] == "done"
    lenses.propose(con, db.ben, lid)                                      # again: a new item
    assert [r[3] for r in rows(con)] == ["done", "open"]
    lenses.publish(con, db.ben, lid, False)                               # taken back: nothing to decide
    assert [r[3] for r in rows(con)] == ["done", "done"]
    own = lenses.create(con, db.ada, "Ada's own")
    lenses.propose(con, db.ada, own)                                      # a sysadmin's own proposal: not for herself
    assert len(rows(con)) == 2


def hiding(con, owner, lens, shas) -> int:
    sid = con.execute("INSERT INTO change_sets(title, op, status, owner, lens_id) VALUES ('set','hide','open',?,?)",
                      (owner, lens)).lastrowid
    con.executemany("INSERT INTO hidden VALUES (?,?,?)", ((s, sid, "2026-11-03T10:00:00Z") for s in shas))
    con.executemany("INSERT INTO change_items(set_id, sha256, applied) VALUES (?,?,1)", ((sid, s) for s in shas))
    con.commit()
    return sid


def test_a_published_lens_that_changes_tells_its_live_subscribers_once(db):
    con = db.con
    lid, _ = lenses.mine(con, db.ben)
    con.commit()
    hiding(con, db.ben, lid, SHAS[:2])
    with con:
        assert member_inbox.lens_changed(con, db.ben, lens=lid) == [], "not published: nobody follows it"
    lenses.publish(con, db.ben, lid, True)
    lenses.subscribe(con, db.cleo, lid, "live")
    lenses.subscribe(con, db.ada, lid, "snapshot")                        # a copy: what it does next is not hers
    for n in (3, 4):
        hiding(con, db.ben, lid, SHAS[n - 1:n])
        with con:
            member_inbox.lens_changed(con, db.ben, lens=lid)
    assert rows(con) == [(db.cleo, "lens_changed", f"lens:{lid}", "open")], "once, for Cleo, however often it changes"
    assert member_inbox.mine(con, db.cleo)["items"][0]["title"] == "Ben’s lens “Hidden photos” changed: it hides 4 photos now"
    with con:
        member_inbox.lens_changed(con, db.cleo, lens=lid)                 # (the actor is never told)
    lenses.unsubscribe(con, db.cleo, lid)                                 # leaving it: the item is done
    assert rows(con)[-1][3] == "done"
    # a series: its album's photos changed
    sid = con.execute("INSERT INTO change_sets(title, op, owner) VALUES ('Walks','view',?)", (db.ada,)).lastrowid
    con.commit()
    lenses.publish_album(con, db.ada, sid, True)
    series = lenses.get(con, con.execute("SELECT id FROM lenses WHERE set_id=?", (sid,)).fetchone()[0])
    lenses.subscribe(con, db.ben, series.id, "live")
    with con:
        member_inbox.lens_changed(con, db.ada, album=sid)
    got = member_inbox.mine(con, db.ben)["items"]
    assert [(x["kind"], x["ref"], x["opens"], x["title"]) for x in got] == \
        [("lens_changed", f"album:{sid}", f"/?album={sid}", "Ada’s series “Walks” changed")]
    lenses.subscribe(con, db.ben, series.id, "snapshot")                  # a copy instead: done
    assert member_inbox.mine(con, db.ben)["open"] == 0


def test_a_task_that_ends_puts_its_hiding_sets_up_for_review_for_whom_it_acted(db, tmp_path):
    con = db.con
    tid = con.execute("INSERT INTO agent_tasks(prompt, status, created_at, acting_for) VALUES ('hide the school', 'running', 'x', ?)",
                      (db.ben,)).lastrowid
    made = con.execute("INSERT INTO change_sets(title, op, created_by, owner) VALUES ('School','hide',?,?)", (f"task:{tid}", db.ben)).lastrowid
    con.executemany("INSERT INTO change_items(set_id, sha256) VALUES (?,?)", ((made, s) for s in SHAS[:3]))
    empty = con.execute("INSERT INTO change_sets(title, op, created_by, owner) VALUES ('Nothing','hide',?,?)", (f"task:{tid}", db.ben)).lastrowid
    album = con.execute("INSERT INTO change_sets(title, op, created_by, owner) VALUES ('Best','view',?,?)", (f"task:{tid}", db.ben)).lastrowid
    con.execute("INSERT INTO change_items(set_id, sha256) VALUES (?,?)", (album, SHAS[0]))
    con.commit()
    t = Tasks.__new__(Tasks)                       # only move(): no worker, no agent
    t.db = str(tmp_path / "estate.db")
    assert t.move(tid, "done", "running")
    assert rows(con) == [(db.ben, "review", f"album:{made}", "open")], "the set with photos, up for hiding; not an album"
    got = member_inbox.mine(con, db.ben)["items"][0]
    assert got["opens"] == f"/changes/{made}"
    assert got["title"] == "“School”: 3 photos the agent proposes to hide, for you to look over"
    assert items(con, db.ada) == [] and items(con, db.cleo) == [] and empty
