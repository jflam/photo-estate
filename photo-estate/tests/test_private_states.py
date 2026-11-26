"""The life of one `private` row (private.py), on a scratch catalog: the seven ways the model of it
(specs/PrivatePhoto.tla, checked by TLC) found the row could end up against its sharer's word, each replayed on
the real code. Three need no bad timing: a photo still `new` whose sharer sets a release day, or makes it
private, before `check` has scored it. Four are races between something the app does by itself, which reads
the table, works for a while and then writes (`check`, `library`, the release timer), and the sharer's request
landing in between, on a connection of its own, as a request of the app's does.

judge() (the scoring between check's read and its write) is stood in for; in the races it is also where the
sharer's request lands.

  uv run pytest tests/test_private_states.py
"""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import estate  # noqa: E402
import members  # noqa: E402
import private  # noqa: E402
from core import schema  # noqa: E402

SHA = "a" * 64
quiet = lambda *a: None   # noqa: E731


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "estate.db")
    con = estate.connect(path)
    schema.ensure(con, *schema.CATALOG_AREAS)
    members.add(con, "Ada", "ada", role="sysadmin", status="active")
    members.add(con, "Ben", "ben", status="active")
    con.execute("INSERT INTO assets(sha256, ext, kind) VALUES (?, 'jpg', 'image')", (SHA,))
    con.commit()
    con.close()
    return path


@pytest.fixture
def con(db):
    con = estate.connect(db)
    yield con
    con.close()


ADA, BEN = 1, 2


def upload(con: sqlite3.Connection) -> None:
    """Ingest's half: Ben's phone sent the photo, so it is a `new` row of his."""
    assert private.record_sharer(con, SHA, BEN, new=True, image=True, upload=True)
    con.commit()


def shared(con: sqlite3.Connection) -> None:
    """A photo of Ben's that was in the library already: a sharer, and no private row."""
    con.execute("INSERT INTO sharers VALUES (?,?,?)", (SHA, BEN, private.now()))
    con.commit()


def row(con: sqlite3.Connection):
    """(why, until, released, reviewed) of Ben's row, or None."""
    r = con.execute("SELECT why, until, released_at IS NOT NULL, reviewed_at IS NOT NULL FROM private"
                    " WHERE sha256=? AND member_id=?", (SHA, BEN)).fetchone()
    return r and (r[0], r[1], bool(r[2]), bool(r[3]))


def scoring(monkeypatch, verdict: str, during=None) -> None:
    """judge() says `verdict` ("hold" | "letgo") of every photo; `during()` runs while it is scoring."""
    def judge(con, model, shas):
        if during:
            during()
        return {s: ((["a receipt"] if verdict == "hold" else []), [0.0]) for s in shas}
    monkeypatch.setattr(private, "judge", judge)


def meanwhile(db: str, fn):
    """fn(con) on a connection of its own, as a request of the app's: what the sharer does in between."""
    def go():
        c = estate.connect(db)
        try:
            fn(c)
        finally:
            c.close()
    return go


def kinds(con: sqlite3.Connection) -> list[str]:
    return [k for k, in con.execute("SELECT kind FROM journal WHERE kind LIKE 'photo.%' ORDER BY id")]


# -- no bad timing: the sharer's word about a photo still being checked -------------------------------------

def test_a_day_set_on_a_new_photo_keeps_it_private_when_check_lets_it_go(con, monkeypatch):
    upload(con)
    got = private.change(con, BEN, [SHA], True, "2099-01-01")
    assert got["changed"]
    scoring(monkeypatch, "letgo")
    private.check(con, "m", out=quiet)
    assert row(con) == ("withdrawn", "2099-01-01", False, False)
    assert private.read(con, ADA).refused(SHA, ADA)          # nobody else is shown it
    assert not private.read(con, ADA).refused(SHA, BEN)


def test_a_day_set_on_a_new_photo_is_kept_when_check_would_hold_it(con, monkeypatch):
    upload(con)
    private.change(con, BEN, [SHA], True, "2099-01-01")
    scoring(monkeypatch, "hold")
    assert private.check(con, "m", out=quiet)["held"] == 0   # theirs now by their own word: not the app's to hold
    assert row(con) == ("withdrawn", "2099-01-01", False, False)
    assert private.counts(con, BEN) == {"held": 0, "waiting": 0}


def test_making_a_new_photo_private_withdraws_it(con, monkeypatch):
    upload(con)
    got = private.change(con, BEN, [SHA], True)
    assert (got["withdrawn"], got["changed"]) == (1, True)
    assert kinds(con) == ["photo.withdrawn"]
    scoring(monkeypatch, "letgo")
    private.check(con, "m", out=quiet)
    assert row(con) == ("withdrawn", None, False, False)
    assert private.read(con, ADA).refused(SHA, ADA)


def test_a_new_photo_nobody_touched_is_still_checked(con, monkeypatch):
    """What must not change: check holds, or lets go, a `new` row its sharer said nothing about."""
    upload(con)
    scoring(monkeypatch, "hold")
    assert private.check(con, "m", out=quiet)["held"] == 1
    assert row(con) == ("held", None, False, False)
    con.execute("DELETE FROM private")
    con.execute("INSERT INTO private(sha256, member_id, why, since) VALUES (?,?,'new',?)", (SHA, BEN, private.now()))
    con.commit()
    scoring(monkeypatch, "letgo")
    private.check(con, "m", out=quiet)
    assert row(con) is None
    assert not private.read(con, ADA).refused(SHA, ADA)


# -- races: the sharer's request lands between the app's read and its write ---------------------------------

def test_a_photo_released_while_check_scores_it_is_not_held(db, con, monkeypatch):
    upload(con)
    scoring(monkeypatch, "hold", meanwhile(db, lambda c: private.change(c, BEN, [SHA], False)))
    got = private.check(con, "m", out=quiet)
    assert got["held"] == 0
    assert row(con) == ("new", None, True, False)            # released, and the row says so
    assert private.counts(con, BEN) == {"held": 0, "waiting": 0}
    assert kinds(con) == ["photo.released"]                  # no photo.held for a hold that did not happen
    assert not con.execute("SELECT 1 FROM inbox_items WHERE kind='held' AND state='open'").fetchone()


def test_a_photo_released_while_check_scores_it_keeps_its_row_when_let_go(db, con, monkeypatch):
    upload(con)
    scoring(monkeypatch, "letgo", meanwhile(db, lambda c: private.change(c, BEN, [SHA], False)))
    private.check(con, "m", out=quiet)
    assert row(con) == ("new", None, True, False)            # the record that they let it go stays
    scoring(monkeypatch, "hold")
    assert private.library(con, BEN, "m", out=quiet)["held"] == 0   # so the library run never holds it


def test_a_photo_withdrawn_while_library_scores_it_is_not_held(db, con, monkeypatch):
    shared(con)
    scoring(monkeypatch, "hold", meanwhile(db, lambda c: private.change(c, BEN, [SHA], True, "2099-01-01")))
    got = private.library(con, BEN, "m", out=quiet)
    assert got["held"] == 0 and "batch" not in got
    assert row(con) == ("withdrawn", "2099-01-01", False, False)
    assert kinds(con) == ["photo.withdrawn"]


def test_a_day_cleared_as_the_timer_fires_is_not_released(db, con, monkeypatch):
    shared(con)
    private.change(con, BEN, [SHA], True, "2026-11-25")
    real, once = private.now, []

    def now():       # release_due asks the time between reading what is due and writing it
        if not once:
            once.append(1)
            meanwhile(db, lambda c: private.change(c, BEN, [SHA], True, None))()
        return real()
    monkeypatch.setattr(private, "now", now)
    assert private.release_due(con, "2026-11-25") == {}
    assert row(con) == ("withdrawn", None, False, False)
    assert "photo.released" not in kinds(con)
