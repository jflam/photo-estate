"""lenses.py on a scratch catalog, with no app: which filters are on for whom (own, published and subscribed,
estate defaults and opting out, series never), what they hide (a set with no lens hides for everyone; "show it
anyway"), whose favourites a source's word is, the writes and what each refuses, one journal entry per write
and none for one that changes nothing, and the `why` of a hidden photo.

  uv run pytest tests/test_lenses.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import lenses  # noqa: E402
import members  # noqa: E402
from core import schema  # noqa: E402

SHAS = [f"{i:02x}" * 32 for i in range(6)]


@pytest.fixture
def db(tmp_path):
    con = sqlite3.connect(tmp_path / "estate.db")
    schema.ensure(con, *schema.CATALOG_AREAS)
    ada, ben, cleo = members.demo(con)
    yield SimpleNamespace(con=con, ada=ada.id, ben=ben.id, cleo=cleo.id)
    con.close()


def hiding(con, owner, lens, shas, sid=None) -> int:
    """A hiding set of `owner`'s, applied, hiding `shas` for `lens` (None: from before lenses)."""
    sid = con.execute("INSERT INTO change_sets(id, title, op, status, owner, lens_id) VALUES (?,?,?,?,?,?)",
                      (sid, "set", "hide", "open", owner, lens)).lastrowid
    con.executemany("INSERT INTO hidden VALUES (?,?,?)", ((s, sid, "2026-11-03T10:00:00Z") for s in shas))
    con.commit()
    return sid


def kinds(con) -> list[str]:
    return [k for k, in con.execute("SELECT kind FROM journal WHERE kind NOT LIKE 'member.%' ORDER BY id")]


def test_an_empty_catalog_hides_nothing(db):
    st = lenses.read(db.con)
    assert st.sysadmin == db.ada and st.on(db.ben) == set() and st.hidden_for(db.ben) == set()
    assert lenses.read(sqlite3.connect(":memory:")) == lenses.State()      # no tables at all: nothing, no error


def test_which_filters_are_on_for_whom(db):
    con = db.con
    ben_lens = lenses.add(con, db.ben, "Ben's", entry=False)
    cleo_lens = lenses.add(con, db.cleo, "Cleo's", entry=False)
    dflt = lenses.add(con, db.ada, "Estate defaults", published=True, estate_default=True, entry=False)
    series = lenses.add(con, db.cleo, "Trips", "series", set_id=99, published=True, entry=False)
    hiding(con, db.ben, ben_lens, SHAS[:1])
    hiding(con, db.cleo, cleo_lens, SHAS[1:2])
    hiding(con, db.ada, dflt, SHAS[2:3])
    hiding(con, db.ada, None, SHAS[3:4])                 # from before lenses: for everyone
    st = lenses.read(con)
    assert st.on(db.ben) == {ben_lens, dflt} and st.on(db.cleo) == {cleo_lens, dflt} and st.on(db.ada) == {dflt}
    assert st.on(None) == st.on(db.ada), "no member: the first sysadmin"
    assert st.hidden_for(db.ben) == {SHAS[0], SHAS[2], SHAS[3]} and st.hidden_for(db.ada) == {SHAS[2], SHAS[3]}
    assert st.default_hidden() == {SHAS[2], SHAS[3]}
    # a subscription to a lens that is not published does nothing; once published, a live one does
    con.execute("INSERT INTO subscriptions VALUES (?,?,?,?)", (db.ben, cleo_lens, "live", "t"))
    con.commit()
    assert cleo_lens not in lenses.read(con).on(db.ben)
    con.execute("UPDATE lenses SET published=1 WHERE id=?", (cleo_lens,))
    con.commit()
    st = lenses.read(con)
    assert cleo_lens in st.on(db.ben) and cleo_lens not in st.on(db.ada) and series not in st.on(db.cleo)
    # out of an estate default, by opting out or by taking a snapshot instead
    for mode in ("opted_out", "snapshot"):
        con.execute("INSERT OR REPLACE INTO subscriptions VALUES (?,?,?,?)", (db.ben, dflt, mode, "t"))
        con.commit()
        st = lenses.read(con)
        assert dflt not in st.on(db.ben) and SHAS[2] not in st.hidden_for(db.ben) and SHAS[3] in st.hidden_for(db.ben)
    # show it anyway, for one member
    con.execute("INSERT INTO shown VALUES (?,?,?)", (SHAS[0], db.ben, "t"))
    con.commit()
    st = lenses.read(con)
    assert SHAS[0] not in st.hidden_for(db.ben) and SHAS[0] in st.hidden_for(db.ben, anyway=False)
    assert st.hidden_for(db.ben, but=1, anyway=False) == {SHAS[1], SHAS[3]}


def test_whose_favourite_a_photo_is(db):
    con = db.con
    cat = SimpleNamespace(N=4, shas=SHAS[:4], index={s: i for i, s in enumerate(SHAS[:4])},
                          said_fav=np.array([True, True, True, False]),
                          fav_sharers={SHAS[0]: (db.ben,), SHAS[1]: (db.ben, db.cleo)})   # SHAS[2]: no sharer yet
    con.execute("INSERT INTO favorites VALUES (?, 1, 't', 'x')", (SHAS[3],))     # the old table: the sysadmin's
    con.execute("INSERT INTO member_favorites VALUES (?,?,?,?)", (SHAS[1], db.cleo, 0, "t"))   # Cleo's own word wins
    con.commit()
    st = lenses.read(con)
    fav = lambda m: st.masks(cat, m)[2].tolist()   # noqa: E731
    assert fav(db.ben) == [True, True, False, False]
    assert fav(db.cleo) == [False, False, False, False]
    assert fav(db.ada) == [False, False, True, True] == fav(None)
    st.set_favorites(db.ben, [SHAS[3]], True)
    assert fav(db.ben)[3] and not st.masks(cat, db.cleo)[2][3]
    shared, hide, _ = st.masks(cat, db.ben)
    assert shared.all() and not hide.any()


def test_the_writes_and_what_they_refuse(db):
    con = db.con
    mine, made = lenses.mine(con, db.ben)
    con.commit()
    assert made and lenses.mine(con, db.ben) == (mine, False) and lenses.get(con, mine).title == "Ben's hidden photos"
    hiding(con, db.ben, mine, SHAS[:2])
    with pytest.raises(lenses.LensError) as e:
        lenses.subscribe(con, db.cleo, mine, "live")
    assert e.value.status == 404                                  # not published: as if it were not there
    assert lenses.publish(con, db.ben, mine, True)["changed"] and not lenses.publish(con, db.ben, mine, True)["changed"]
    with pytest.raises(lenses.LensError) as e:
        lenses.publish(con, db.cleo, mine, False)
    assert e.value.status == 403
    for bad, status in (({"mode": "both"}, 400), ({"mode": "live", "member": db.ben}, 409)):
        with pytest.raises(lenses.LensError) as e:
            lenses.subscribe(con, bad.get("member", db.cleo), mine, bad["mode"])
        assert e.value.status == status
    assert lenses.subscribe(con, db.cleo, mine, "live")["changed"]
    assert not lenses.subscribe(con, db.cleo, mine, "live")["changed"]        # again: nothing
    snap = lenses.subscribe(con, db.ada, mine, "snapshot")
    assert snap["photos"] == 2 and lenses.hides(con, snap["copy"]) == SHAS[:2]
    hiding(con, db.ben, mine, SHAS[2:3])                          # the lens grows: the live one follows, the copy not
    st = lenses.read(con)
    assert SHAS[2] in st.hidden_for(db.cleo) and SHAS[2] not in st.hidden_for(db.ada) and SHAS[0] in st.hidden_for(db.ada)
    assert lenses.unsubscribe(con, db.cleo, mine)["changed"] and not lenses.unsubscribe(con, db.cleo, mine)["changed"]
    # proposing, and a sysadmin deciding
    with pytest.raises(lenses.LensError):
        lenses.decide(con, db.ada, mine, True)                    # not proposed
    assert lenses.propose(con, db.ben, mine)["changed"] and not lenses.propose(con, db.ben, mine)["changed"]
    with pytest.raises(lenses.LensError) as e:
        lenses.decide(con, db.cleo, mine, True)
    assert e.value.status == 403
    lenses.decide(con, db.ada, mine, True)
    assert SHAS[2] in lenses.read(con).hidden_for(db.cleo), "an estate default now: on for everyone"
    with pytest.raises(lenses.LensError) as e:
        lenses.publish(con, db.ben, mine, False)
    assert e.value.status == 409
    # show it anyway, said twice
    assert lenses.show(con, db.cleo, SHAS[2], True)["changed"] and not lenses.show(con, db.cleo, SHAS[2], True)["changed"]
    assert lenses.show(con, db.cleo, SHAS[2], False)["changed"]
    assert kinds(con) == ["lens.published", "lens.subscribed", "lens.subscribed", "lens.unsubscribed", "lens.proposed",
                          "lens.accepted", "photo.shown", "photo.unshown"]
    actor, data = con.execute("SELECT actor, data FROM journal WHERE kind='lens.subscribed' ORDER BY id DESC").fetchone()
    assert actor == db.ada and json.loads(data) == {"mode": "snapshot", "copy": snap["copy"], "album": snap["album"], "photos": 2}


def test_an_album_published_is_a_series(db):
    con = db.con
    sid = con.execute("INSERT INTO change_sets(title, op, status, owner) VALUES ('Trips', 'view', 'open', ?)", (db.cleo,)).lastrowid
    hid = con.execute("INSERT INTO change_sets(title, op, status, owner) VALUES ('Hide', 'hide', 'open', ?)", (db.cleo,)).lastrowid
    con.commit()
    with pytest.raises(lenses.LensError):
        lenses.publish_album(con, db.ben, sid, True)              # Cleo's
    with pytest.raises(lenses.LensError):
        lenses.publish_album(con, db.cleo, hid, True)             # a change set is not a series
    got = lenses.publish_album(con, db.cleo, sid, True)
    lens = lenses.get(con, got["lens"])
    assert (lens.kind, lens.set_id, lens.published, lens.title) == ("series", sid, True, "Trips")
    assert lenses.publish_album(con, db.cleo, sid, False)["lens"] == got["lens"] and not lenses.get(con, got["lens"]).published
    assert lenses.publish_album(con, db.cleo, sid, True)["lens"] == got["lens"]       # the same row again
    copy = lenses.subscribe(con, db.ben, got["lens"], "snapshot", shas=SHAS[:3])
    assert con.execute("SELECT owner, op FROM change_sets WHERE id=?", (copy["album"],)).fetchone() == (db.ben, "view")
    assert [s for s, in con.execute("SELECT sha256 FROM change_items WHERE set_id=? ORDER BY position", (copy["album"],))] == SHAS[:3]


def test_why(db):
    con = db.con
    dflt = lenses.add(con, db.ada, "Estate defaults", published=True, estate_default=True, entry=False)
    mine = lenses.add(con, db.ben, "No screenshots", entry=False)
    hiding(con, db.ada, dflt, SHAS[:1])
    hiding(con, db.ben, mine, SHAS[:2])
    st = lenses.read(con)
    got = lenses.why(con, st, db.ben, SHAS[0])
    assert got["hidden"] and [r["lens"]["title"] for r in got["reasons"]] == ["No screenshots", "Estate defaults"]
    assert got["text"] == "Hidden by your No screenshots lens on 2026-11-03"
    assert lenses.why(con, st, db.cleo, SHAS[0])["text"] == "Hidden by Ada's Estate defaults lens on 2026-11-03"
    assert lenses.why(con, st, db.cleo, SHAS[1]) == {"sha": SHAS[1], "hidden": False, "shown_anyway": False,
                                                       "reasons": [], "text": None}
    listing = lenses.listing(con, st, db.cleo)
    assert [x["title"] for x in listing["lenses"]] == ["Estate defaults"] and listing["mine"] is None
    assert [x["title"] for x in lenses.listing(con, st, db.ben)["lenses"]] == ["Estate defaults", "No screenshots"]
