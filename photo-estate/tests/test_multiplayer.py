"""What each member sees (multiplayer phase 3), on the demo library in process, as its three members: Ada the
sysadmin, Ben and Cleo. Every row of the rules: one member's filter hides for that member only, in search,
facets and counts, Years and Months, trips and their covers, someone else's album and series, the sync feed,
/api/info, the MCP tools and a task acting for them; an estate default hides for all until one opts out; a
snapshot does not follow the lens it was taken of and a live subscription does; "show it anyway"; favourites
per member; a stack whose best shot one member hid shows them the next one; why a photo is not seen; what a
catalog `estate.py migrate` has not been run on shows, and the migration itself. Last, a check that no route
module asks what a member sees any way but through catalog.Live.visible_for (mechanical: it reads the code).

  uv run pytest tests/test_multiplayer.py
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import catalog  # noqa: E402
import embed  # noqa: E402
import faces  # noqa: E402
import lenses  # noqa: E402
import members  # noqa: E402
from core import schema  # noqa: E402
from photo_app import PhotoApp  # noqa: E402

ADA, BEN, CLEO = {"X-Demo-Member": "ada"}, {"X-Demo-Member": "ben"}, {"X-Demo-Member": "cleo"}
ALL = "limit=500&dedupe=0"        # every photo a member sees, stacks and all


@pytest.fixture
def app():
    a = embed.parser().parse_args(["serve", "--demo", "--no-pregenerate"])
    app = PhotoApp(a)             # start() is not called: no model, no threads; its own scratch catalog
    app.feed_update()
    return app


def call(app, method, path, who, body=None, expect=200, local=False):
    r = app.request(method, path, headers=who, body=body if body is not None else (b"" if method == "GET" else {}),
                    local=local)
    assert r.status == expect, (method, path, who, r.status, r.body[:300])
    return r.json() if r.content_type.startswith("application/json") else r


def mid(app, who) -> int:
    return app.roster[who["X-Demo-Member"]].id


def settle(app):
    """A change set's apply is taken in by a background thread (PhotoApp.on_hidden_change): wait for it."""
    end = time.time() + 20
    while app.settling or app.reload_state["state"] != "idle":
        assert time.time() < end, "the app did not settle"
        time.sleep(0.01)


def seen(app, who, extra="") -> list[str]:
    return [r["sha"] for r in call(app, "GET", f"/api/search?{ALL}{extra}", who)["results"]]


def hide(app, who, shas, lens=None, title="hide these") -> int:
    sid = call(app, "POST", "/api/changes", who, {"title": title, "items": [{"sha": s} for s in shas]}, expect=201)["id"]
    got = call(app, "POST", f"/api/changes/{sid}/apply", who, {} if lens is None else {"lens": lens})
    assert got["hid"] == len(shas)
    settle(app)
    return sid


def tool(app, who, name, args=None, headers=None, local=False):
    got = call(app, "POST", "/mcp", {**who, **(headers or {})}, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                                 "params": {"name": name, "arguments": args or {}}},
               local=local)
    assert not got["result"]["isError"], got
    return got["result"]["structuredContent"]


def feed(app, who) -> dict:
    got = call(app, "GET", "/api/sync?limit=10000", who)
    return {r["sha"]: r for r in got["photos"]}


# -- one member's filter ----------------------------------------------------------------------------

def test_bens_filter_hides_for_ben_only(app):
    info = call(app, "GET", "/api/info", ADA)
    total = info["total"]
    trip = next(t for t in info["trips"] if t["n"] > 1)
    x = trip["cover"]                               # Ada's cover of a trip: Ben is to hide it
    month = next(m for m in call(app, "GET", "/api/periods", ADA)["months"]
                 if x in seen(app, ADA, f"&from={m['key']}&to={m['key']}"))
    sid = hide(app, BEN, [x])

    assert x not in seen(app, BEN) and x in seen(app, ADA) and x in seen(app, CLEO)
    assert call(app, "GET", "/api/info", BEN)["total"] == total - 1 == call(app, "GET", "/api/info", ADA)["total"] - 1
    assert call(app, "GET", "/api/facets", BEN)["matching"] == total - 1
    assert call(app, "GET", "/api/facets", CLEO)["matching"] == total
    assert seen(app, BEN, "&hidden=1") == [x] and seen(app, ADA, "&hidden=1") == []
    # Years and Months: the month is one smaller for Ben, and none of Ben's covers is the photo
    bens = call(app, "GET", "/api/periods", BEN)
    assert next(m["n"] for m in bens["months"] if m["key"] == month["key"]) == month["n"] - 1
    assert x not in {c["cover"] for k in ("years", "months") for c in bens[k]}
    assert sum(m["n"] for m in bens["months"]) == sum(m["n"] for m in call(app, "GET", "/api/periods", ADA)["months"]) - 1
    # trips: Ben's card of it is one smaller, with another cover; Ada's is as it was
    ben_trip = next(t for t in call(app, "GET", "/api/info", BEN)["trips"] if t["id"] == trip["id"])
    assert ben_trip["n"] == trip["n"] - 1 and ben_trip["cover"] != x
    assert next(t for t in call(app, "GET", "/api/info", ADA)["trips"] if t["id"] == trip["id"]) == trip
    # the sync feed: Ben's (made the first time his phone asks) has no row for it; Ada's has
    assert x not in feed(app, BEN) and len(feed(app, BEN)) == total - 1
    app.feed_update()
    assert x in feed(app, ADA) and len(feed(app, ADA)) == total
    assert (app.feed_dir / f"sync-feed-{mid(app, BEN)}.db").exists() and not (app.feed_dir / f"sync-feed-{mid(app, ADA)}.db").exists()
    # the MCP tools, as Ben's login and as Ada's
    assert x not in [p["id"] for p in tool(app, BEN, "search_photos", {"limit": 200})["photos"]]
    assert x in [p["id"] for p in tool(app, ADA, "search_photos", {"limit": 200})["photos"]]
    assert tool(app, BEN, "get_photos", {"ids": [x]})["photos"][0].get("hidden") is True
    assert "hidden" not in tool(app, ADA, "get_photos", {"ids": [x]})["photos"][0]
    over_b, over_a = tool(app, BEN, "library_overview"), tool(app, ADA, "library_overview")
    assert (over_b["photos"], over_b["hidden"], over_a["photos"], over_a["hidden"]) == (total - 1, 1, total, 0)
    assert [p["id"] for p in tool(app, BEN, "search_photos", {"hidden_only": True})["photos"]] == [x]
    # a change set is its owner's: Ada does not see Ben's, and cannot change it
    assert [s["id"] for s in call(app, "GET", "/api/changes", BEN)["sets"]] == [sid]
    assert call(app, "GET", "/api/changes", ADA)["sets"] == [] and call(app, "GET", "/api/changes", ADA)["hidden"] == 0
    call(app, "GET", f"/api/changes/{sid}", ADA, expect=404)
    call(app, "POST", f"/api/changes/{sid}/revert", ADA, expect=404)
    assert call(app, "GET", f"/api/changes/{sid}/check", BEN)["hidden"] == 1
    # it went to Ben's own filter, made on first use
    got = call(app, "GET", "/api/lenses", BEN)
    mine = next(lens for lens in got["lenses"] if lens["id"] == got["mine"])
    assert (mine["title"], mine["kind"], mine["published"], mine["on"], mine["photos"], mine["sets"]) == \
        ("Hidden photos", "filter", False, True, 1, [sid])
    assert call(app, "GET", "/api/lenses", ADA)["lenses"] == []      # not published: Ada does not see it
    # reverting it shows the photo again, to Ben
    call(app, "POST", f"/api/changes/{sid}/revert", BEN)
    settle(app)
    assert x in seen(app, BEN)


def test_why_says_whose_lens(app):
    x = app.live.cat.shas[7]
    assert call(app, "GET", f"/api/photos/{x}/why", BEN) == {"sha": x, "hidden": False, "shown_anyway": False,
                                                              "reasons": [], "text": None, "visible": True}
    sid = hide(app, BEN, [x])
    why = call(app, "GET", f"/api/photos/{x}/why", BEN)
    assert why["hidden"] and not why["visible"] and why["reasons"][0]["set"]["id"] == sid
    assert why["text"].startswith("Hidden by your lens “Hidden photos” on "), why
    assert call(app, "GET", f"/api/photos/{x}/why", ADA)["reasons"] == []     # Ben's hides are not Ada's business
    call(app, "GET", f"/api/photos/{'0' * 64}/why", BEN, expect=404)


def test_show_it_anyway(app):
    x = app.live.cat.shas[3]
    hide(app, BEN, [x])
    assert call(app, "POST", f"/api/photos/{x}/show", BEN, {})["changed"]
    assert x in seen(app, BEN) and x not in seen(app, BEN, "&hidden=1")
    why = call(app, "GET", f"/api/photos/{x}/why", BEN)
    assert why["shown_anyway"] and not why["hidden"] and why["visible"] and why["text"].startswith("Shown anyway")
    assert not call(app, "POST", f"/api/photos/{x}/show", BEN, {})["changed"]     # said twice is said once
    call(app, "POST", f"/api/photos/{x}/show", BEN, {"on": False})
    assert x not in seen(app, BEN)
    call(app, "POST", f"/api/photos/{x}/show", BEN, {"on": "yes"}, expect=400)
    kinds = [k for k, in sqlite3.connect(app.a.db).execute("SELECT kind FROM journal WHERE kind LIKE 'photo.%' ORDER BY id")]
    assert kinds == ["photo.shown", "photo.unshown"]


# -- estate defaults ---------------------------------------------------------------------------------

def test_an_estate_default_hides_for_all_until_one_opts_out(app):
    z = app.live.cat.shas[11]
    call(app, "POST", "/api/changes/" + str(call(app, "POST", "/api/changes", BEN, {"title": "x", "items": [z]}, expect=201)["id"])
         + "/apply", BEN, {"lens": "estate"}, expect=403)      # only a sysadmin hides for everyone
    hide(app, ADA, [z], lens="estate")
    assert all(z not in seen(app, who) for who in (ADA, BEN, CLEO))
    dflt = next(lens for lens in call(app, "GET", "/api/lenses", CLEO)["lenses"] if lens["estate_default"])
    assert (dflt["title"], dflt["published"], dflt["on"], dflt["mine"], dflt["owner"]["name"]) == \
        ("Estate defaults", True, True, False, "Ada")
    why = call(app, "GET", f"/api/photos/{z}/why", CLEO)
    assert why["text"].startswith("Hidden by Ada's lens “Estate defaults” on "), why
    assert z in faces.default_hidden(sqlite3.connect(app.a.db)), "People is built without it"
    # Cleo opts out: she sees it, nobody else does
    assert call(app, "POST", f"/api/lenses/{dflt['id']}/unsubscribe", CLEO)["mode"] == "opted_out"
    assert z in seen(app, CLEO) and z not in seen(app, BEN) and z not in seen(app, ADA)
    assert not next(x for x in call(app, "GET", "/api/lenses", CLEO)["lenses"] if x["id"] == dflt["id"])["on"]
    # and takes it back
    call(app, "POST", f"/api/lenses/{dflt['id']}/subscribe", CLEO, {"mode": "live"})
    assert z not in seen(app, CLEO)
    call(app, "POST", f"/api/lenses/{dflt['id']}/publish", ADA, {"on": False}, expect=409)   # it stays published


def test_a_proposed_filter_becomes_an_estate_default_when_a_sysadmin_accepts(app):
    y = app.live.cat.shas[20]
    hide(app, CLEO, [y])
    lid = call(app, "GET", "/api/lenses", CLEO)["mine"]
    assert call(app, "POST", f"/api/lenses/{lid}/propose", CLEO)["changed"]
    assert y in seen(app, BEN), "proposing changes nothing for anyone else"
    assert lid in call(app, "GET", "/api/lenses", ADA)["proposed"]
    call(app, "POST", f"/api/lenses/{lid}/decide", BEN, {"accept": True}, expect=403)
    call(app, "POST", f"/api/lenses/{lid}/decide", ADA, {"accept": True})
    assert y not in seen(app, BEN) and y not in seen(app, ADA)
    assert y in faces.default_hidden(sqlite3.connect(app.a.db))


# -- subscriptions -----------------------------------------------------------------------------------

def test_a_snapshot_does_not_follow_the_lens_and_a_live_subscription_does(app):
    p1, p2 = app.live.cat.shas[30], app.live.cat.shas[31]
    first = hide(app, CLEO, [p1])
    lid = call(app, "GET", "/api/lenses", CLEO)["mine"]
    call(app, "POST", f"/api/lenses/{lid}/subscribe", BEN, {"mode": "live"}, expect=404)   # not published yet
    assert call(app, "POST", f"/api/lenses/{lid}/publish", CLEO, {"on": True})["published"]
    assert call(app, "POST", f"/api/lenses/{lid}/subscribe", BEN, {"mode": "live"})["changed"]
    snap = call(app, "POST", f"/api/lenses/{lid}/subscribe", ADA, {"mode": "snapshot"})
    assert snap["photos"] == 1 and snap["copy"] != lid
    call(app, "POST", f"/api/lenses/{lid}/subscribe", CLEO, {"mode": "live"}, expect=409)  # her own: always on
    assert p1 not in seen(app, BEN) and p1 not in seen(app, ADA) and p1 not in seen(app, CLEO)
    hide(app, CLEO, [p2])                         # the lens changes: a second set, to the same lens
    assert p2 not in seen(app, BEN), "a live subscription follows the lens"
    assert p2 in seen(app, ADA), "a snapshot does not"
    call(app, "POST", f"/api/changes/{first}/revert", CLEO)
    settle(app)
    assert p1 in seen(app, BEN) and p1 not in seen(app, ADA), "Ada's copy is her own now"
    assert call(app, "POST", f"/api/lenses/{lid}/unsubscribe", BEN)["changed"]
    assert p2 in seen(app, BEN)
    call(app, "POST", f"/api/lenses/{lid}/subscribe", BEN, {"mode": "live"})
    call(app, "POST", f"/api/lenses/{lid}/publish", CLEO, {"on": False})
    assert p2 in seen(app, BEN), "an unpublished lens hides nothing for its subscribers"
    copy = next(x for x in call(app, "GET", "/api/lenses", ADA)["lenses"] if x["id"] == snap["copy"])
    assert copy["mine"] and copy["on"] and copy["sets"] == [snap["album"]]


# -- albums and series -------------------------------------------------------------------------------

def test_an_album_is_its_owners_and_a_series_shows_what_the_viewer_sees(app):
    s = app.live.cat.shas
    x, y, z = s[40], s[41], s[42]
    aid = call(app, "POST", "/api/albums", ADA, {"title": "Ada's", "items": [x, y]}, expect=201)["id"]
    call(app, "GET", f"/api/albums/{aid}/photos", BEN, expect=404)          # private until published
    assert aid not in [a["id"] for a in call(app, "GET", "/api/albums", BEN)["sets"]]
    got = call(app, "POST", f"/api/albums/{aid}/publish", ADA, {})
    assert got["published"] and got["lens"]
    listed = next(a for a in call(app, "GET", "/api/albums", BEN)["sets"] if a["id"] == aid)
    assert (listed["mine"], listed["published"], listed["owner"], listed["series"]) == (False, True, mid(app, ADA), got["lens"])
    hide(app, BEN, [x])
    assert [r["sha"] for r in call(app, "GET", f"/api/albums/{aid}/photos", BEN)["results"]] == [y]
    assert [r["sha"] for r in call(app, "GET", f"/api/albums/{aid}/photos", ADA)["results"]] == [x, y]
    assert next(a for a in call(app, "GET", "/api/albums", BEN)["sets"] if a["id"] == aid)["n"] == 1
    assert [f for _, f in app.album_files(aid, mid(app, BEN))[1]] == [app.live.cat.meta[y]["path"].rsplit("/", 1)[-1]]
    call(app, "POST", f"/api/albums/{aid}/items", BEN, {"items": [z]}, expect=403)   # only its owner changes it
    call(app, "POST", f"/api/albums/{aid}/publish", BEN, {}, expect=403)
    # a series Cleo publishes: Ben's filters hide in it too
    cid = call(app, "POST", "/api/albums", CLEO, {"title": "Cleo's", "items": [z, x]}, expect=201)["id"]
    lens = call(app, "POST", f"/api/albums/{cid}/publish", CLEO, {})["lens"]
    assert [r["sha"] for r in call(app, "GET", f"/api/albums/{cid}/photos", BEN)["results"]] == [z]
    got = tool(app, BEN, "get_album", {"album": cid})
    assert [p["id"] for p in got["photos"]] == [z]
    # subscribing to a series: live puts it in Ben's Albums; a snapshot copies what Ben sees of it
    assert call(app, "POST", f"/api/lenses/{lens}/subscribe", BEN, {"mode": "live"})["changed"]
    assert next(x for x in call(app, "GET", "/api/lenses", BEN)["lenses"] if x["id"] == lens)["subscription"] == "live"
    snap = call(app, "POST", f"/api/lenses/{lens}/subscribe", BEN, {"mode": "snapshot"})
    mine = call(app, "GET", f"/api/albums/{snap['album']}/photos", BEN)
    assert mine["album"]["mine"] and [r["sha"] for r in mine["results"]] == [z]
    # favouriting an album's photos is the viewer's own, on any album they see
    assert call(app, "POST", f"/api/albums/{cid}/favorite", BEN, {"fav": True})["changed"] == 1
    assert seen(app, BEN, "&fav=1") == [z] and z not in seen(app, ADA, "&fav=1")


# -- favourites -----------------------------------------------------------------------------------

def test_favourites_are_each_members_own(app):
    v = app.live.visible_for(mid(app, ADA))
    said = int(v.fav.sum())
    assert said == int(app.live.cat.said_fav.sum()) > 0, "what a source said is the sysadmin's (no sharers yet)"
    plain = next(s for s in app.live.cat.shas if not v.fav[v.cat.index[s]])
    assert call(app, "GET", "/api/info", BEN)["fav"] == 0
    got = call(app, "POST", "/api/favorite", BEN, {"sha": plain, "fav": True})
    assert got == {"sha": plain, "fav": True, "favourites": 1}
    assert call(app, "GET", "/api/info", BEN)["fav"] == 1 and call(app, "GET", "/api/info", ADA)["fav"] == said
    assert seen(app, BEN, "&fav=1") == [plain] and plain not in seen(app, ADA, "&fav=1")
    assert next(r for r in call(app, "GET", f"/api/search?{ALL}", BEN)["results"] if r["sha"] == plain)["fav"] is True
    assert next(r for r in call(app, "GET", f"/api/search?{ALL}", ADA)["results"] if r["sha"] == plain)["fav"] is False
    assert seen(app, BEN, "&q=is:fav") == [plain]
    assert tool(app, BEN, "get_photos", {"ids": [plain]})["photos"][0]["favorite"] is True
    assert feed(app, BEN)[plain]["fav"] is True
    app.feed_update()
    assert feed(app, ADA)[plain]["fav"] is False
    con = sqlite3.connect(app.a.db)
    assert con.execute("SELECT count(*) FROM favorites").fetchone()[0] == 0, "the old table is no longer written"
    assert con.execute("SELECT member_id, fav FROM member_favorites WHERE sha256=?", (plain,)).fetchall() == [(mid(app, BEN), 1)]
    actor, data = con.execute("SELECT actor, data FROM journal WHERE kind='favorite.set' ORDER BY id DESC").fetchone()
    assert actor == mid(app, BEN) and json.loads(data) == {"fav": True, "photos": 1}
    app.reload_catalog()                          # a reload keeps them: the looks, not the catalog, hold them
    assert call(app, "GET", "/api/info", BEN)["fav"] == 1 and call(app, "GET", "/api/info", ADA)["fav"] == said


# -- stacks -------------------------------------------------------------------------------------------

def test_a_stack_whose_best_shot_ben_hid_shows_him_the_next(app):
    """Three photos made near-identical shots of one moment (one vector, a second apart): a stack, its first
    shot on top (the demo has no scorers). Ben hides that one: he sees the stack of the other two, the
    second on top; Ada still sees all three, the first on top."""
    c = app.live.cat
    a, b, d = c.shas[50], c.shas[51], c.shas[52]
    con = sqlite3.connect(app.a.db)
    vec = con.execute("SELECT vec FROM embeddings WHERE sha256=?", (a,)).fetchone()[0]
    for k, s in enumerate((a, b, d)):
        con.execute("UPDATE embeddings SET vec=? WHERE sha256=?", (vec, s))
        con.execute("UPDATE assets SET taken_at=? WHERE sha256=?", (f"2001-02-03T04:05:0{k}+00:00", s))
    con.commit()
    app.live.install(app.load_catalog())          # read again from scratch (an incremental load reads only new rows)
    ada = app.live.visible_for(mid(app, ADA))
    ia, ib, idd = (ada.cat.index[s] for s in (a, b, d))
    assert ada.stack_n[ia] == 3 and not ada.dup[ia] and ada.dup[ib] and ada.dup[idd]
    hide(app, BEN, [a])
    ben = app.live.visible_for(mid(app, BEN))
    assert ben.stack_n[ib] == 2 and not ben.dup[ib] and ben.dup[idd] and ben.stack_of[ib] == ib
    rows = {r["sha"]: r for r in call(app, "GET", "/api/search?limit=500", BEN)["results"]}    # stacks shown as their top
    assert b in rows and rows[b]["stack"] == 2 and a not in rows and d not in rows
    rows = {r["sha"]: r for r in call(app, "GET", "/api/search?limit=500", ADA)["results"]}
    assert a in rows and rows[a]["stack"] == 3 and b not in rows
    assert feed(app, BEN)[b].get("dup") is None and feed(app, BEN)[d]["dup"] is True


# -- People ----------------------------------------------------------------------------------------------

def test_people_lists_a_persons_faces_through_the_members_filters():
    """People is built once (faces.face_rows leaves out only what the estate defaults hide: tested above); a
    member's own filters leave out of what they are shown the faces in photos they do not see."""
    face = lambda k, sha: {"id": k, "sha": sha}      # noqa: E731
    keep = lambda sha: sha != "hid"                    # noqa: E731
    got = faces.People.only({"person": {"id": 1}, "faces": [face(1, "a"), face(2, "hid")], "total": 2}, keep)
    assert got == {"person": {"id": 1}, "faces": [face(1, "a")], "total": 2}, "the counts stay People's own"
    got = faces.People.only({"years": {2001: [face(1, "hid")], 2002: [face(2, "a"), face(3, "hid")]}}, keep)
    assert got == {"years": {2002: [face(2, "a")]}}
    got = faces.People.only({"state": "ready", "groups": [{"faces": [face(1, "hid")]}, {"faces": [face(2, "a")], "n": 1}]}, keep)
    assert got == {"state": "ready", "groups": [{"faces": [face(2, "a")], "n": 1}]}
    assert faces.People.only({"face": face(1, "hid"), "faces": [face(1, "hid")]}, keep) is None   # a photo they do not see: 404
    assert faces.People.only({"face": face(1, "a"), "faces": [face(1, "a"), face(2, "hid")]}, keep)["faces"] == [face(1, "a")]


# -- the agent ------------------------------------------------------------------------------------------

def test_a_task_acts_for_the_member_who_submitted_it(app):
    x = app.live.cat.shas[60]
    hide(app, BEN, [x])
    con = sqlite3.connect(app.a.db)
    tid = con.execute("INSERT INTO agent_tasks(prompt, status, created_by, created_at, acting_for) VALUES"
                      " ('find the beach', 'done', 'ben', '2026-01-01', ?)", (mid(app, BEN),)).lastrowid
    con.commit()
    task = {"X-Estate-Task": str(tid)}             # honoured only from this computer, as the estate's agent calls
    ids = [p["id"] for p in tool(app, {}, "search_photos", {"limit": 200}, headers=task, local=True)["photos"]]
    assert x not in ids and len(ids) == app.live.cat.N - 1, "the task searches as Ben, not as the sysadmin"
    assert tool(app, {}, "library_overview", headers=task, local=True)["hidden"] == 1
    made = tool(app, {}, "create_album", {"title": "Beach"}, headers=task, local=True)
    tool(app, {}, "amend_album", {"album": made["album"], "add_ids": [x, app.live.cat.shas[61]]}, headers=task, local=True)
    assert made["album"] in [a["id"] for a in call(app, "GET", "/api/albums", BEN)["sets"]], "the album is Ben's"
    assert made["album"] not in [a["id"] for a in call(app, "GET", "/api/albums", ADA)["sets"]]
    rows = con.execute("SELECT actor, acting_for, kind FROM journal WHERE subject=? ORDER BY id", (f"album:{made['album']}",)).fetchall()
    assert rows == [(None, mid(app, BEN), "album.created"), (None, mid(app, BEN), "album.items")]
    # without the header, a call from this computer is the sysadmin, who sees the photo
    assert x in [p["id"] for p in tool(app, {}, "search_photos", {"limit": 200}, local=True)["photos"]]


# -- a catalog from before lenses, and the migration ----------------------------------------------------

def test_before_and_after_migrate(tmp_path):
    import estate
    db = tmp_path / "estate.db"
    shutil.copyfile(ROOT / "demo" / "estate.db", db)
    con = sqlite3.connect(db)
    schema.ensure(con, *schema.CATALOG_AREAS)
    ada, ben, _ = members.demo(con)
    shas = [r[0] for r in con.execute("SELECT sha256 FROM assets ORDER BY sha256 LIMIT 3")]
    # as the owner's catalog is before the migration: a hiding set and an album with no owner or lens, an old favourite
    con.execute("INSERT INTO change_sets(id, title, op, status) VALUES (1, 'old hiding', 'hide', 'open'), (2, 'old album', 'view', 'open')")
    con.execute("INSERT INTO change_items(set_id, sha256, applied) VALUES (1, ?, 1)", (shas[0],))
    con.execute("INSERT INTO hidden VALUES (?, 1, '2025-05-05T00:00:00Z')", (shas[0],))
    con.execute("INSERT INTO favorites VALUES (?, 1, '2025-05-05', 'owner@example.com')", (shas[1],))
    con.commit()
    st = lenses.read(con)
    assert shas[0] in st.hidden_for(ben.id) and shas[0] in st.hidden_for(ada.id), "a set with no lens hides for everyone"
    cat = catalog.load(sqlite3.connect(db), "google/siglip2-base-patch16-256", tmp_path, tmp_path)
    _, hide_, fav = st.masks(cat, ada.id)
    assert fav[cat.index[shas[1]]] and not st.masks(cat, ben.id)[2][cat.index[shas[1]]], "an old favourite is the sysadmin's"
    assert int(fav.sum()) == int(cat.said_fav.sum()) + (0 if cat.said_fav[cat.index[shas[1]]] else 1)
    with pytest.raises(lenses.LensError):        # nothing to opt out of yet
        lenses.unsubscribe(con, ben.id, 1)
    con.close()

    estate.cmd_migrate(argparse.Namespace(db=str(db)))
    con = sqlite3.connect(db)
    lid, title, owner, pub, dflt = con.execute("SELECT id, title, owner, published, estate_default FROM lenses").fetchone()
    assert (title, owner, pub, dflt) == ("Estate defaults", ada.id, 1, 1)
    assert con.execute("SELECT id, lens_id, owner FROM change_sets ORDER BY id").fetchall() == [(1, lid, ada.id), (2, None, ada.id)]
    assert con.execute("SELECT sha256, member_id, fav FROM member_favorites").fetchall() == [(shas[1], ada.id, 1)]
    entries = [k for k, in con.execute("SELECT kind FROM journal WHERE kind IN ('sharers.migrated', 'lenses.migrated')")]
    assert entries == ["sharers.migrated", "lenses.migrated"]
    con.close()
    estate.cmd_migrate(argparse.Namespace(db=str(db)))          # again: nothing changes, nothing is journaled
    con = sqlite3.connect(db)
    assert [k for k, in con.execute("SELECT kind FROM journal WHERE kind IN ('sharers.migrated', 'lenses.migrated')")] == entries
    assert con.execute("SELECT count(*) FROM lenses").fetchone()[0] == 1
    st = lenses.read(con)
    assert shas[0] in st.hidden_for(ben.id) and lid in st.on(ben.id)
    lenses.unsubscribe(con, ben.id, lid)                          # now an estate default: Ben may opt out
    assert shas[0] not in lenses.read(con).hidden_for(ben.id) and shas[0] in lenses.read(con).hidden_for(ada.id)


# -- the one way to ask ----------------------------------------------------------------------------------

ROUTES = ("browse.py", "photo_app.py", "images.py", "admin.py", "compare.py", "changes.py", "estate_mcp.py",
          "faces.py", "tasks.py", "lenses.py", "sync.py", "stats.py", "pages.py", "upload.py", "embed.py")


def test_no_route_reads_a_mask_but_through_visible_for():
    """What a member sees is asked of catalog.Live.visible_for (or shared_for), and only there: a later phase
    that reads a mask, a baked stack, a trip card, a count or a favourite any other way fails here."""
    fields = {f.name for f in dataclasses.fields(catalog.Catalog)}
    assert not fields & {"visible", "fav", "dup", "stack_n", "stack_of", "stack_live", "trip_list", "info"}, \
        "the Catalog is the same for every member: what one sees is a View"
    banned = [
        (r"\bcat\.(visible|fav|dup|stack_n|stack_of|stack_live|trip_list|info)\b", "a member's arrays are a View's"),
        (r"\bView\(", "a View is made only by Catalog.view, for Live.visible_for"),
        (r"\b(cat|c|self\.live\.cat)\.view\(", "a View is asked of Live.visible_for, which keeps it"),
        (r"""(meta\[[^\]]*\]|meta\.get\([^)]*\)|\bm|facts\([^)]*\))\[["']fav["']\]""",
         "a photo's facts have no favourite: View.fav, or a row from View.row / feed_row"),
        (r"hidden_shas\(", "what is hidden is a member's: lenses.State, through Live.visible_for"),
        (r"\blooks\(\s*self\.live\.cat", "the looks are read by Live.visible_for alone"),
    ]
    allowed_hidden_sql = {"changes.py", "lenses.py", "faces.py"}   # the tables' own code: hiding, lenses, People
    bad = []
    for name in ROUTES:
        src = (ROOT / name).read_text()
        for pattern, why in banned:
            if name == "changes.py" and pattern.startswith("hidden_shas") and src.count("hidden_shas(") == 1:
                continue      # its definition, kept for tools/export_squares.py
            for m in re.finditer(pattern, src):
                line = src.count("\n", 0, m.start()) + 1
                bad.append(f"{name}:{line}: {m.group(0)!r}: {why}")
        if name not in allowed_hidden_sql:
            for m in re.finditer(r"FROM\s+hidden\b", src):
                bad.append(f"{name}:{src.count(chr(10), 0, m.start()) + 1}: reads the hidden table itself")
    # and each route module that answers from the catalog asks for the member's view
    for name in ("browse.py", "photo_app.py", "compare.py", "admin.py", "images.py", "changes.py"):
        src = (ROOT / name).read_text()
        if not re.search(r"visible_for|shared_for|may_see", src):
            bad.append(f"{name}: never asks Live.visible_for what a member sees")
    assert not bad, "\n".join(bad)


def test_the_view_is_kept_per_member_and_let_go_on_a_change(tmp_path):
    shutil.copyfile(ROOT / "demo" / "estate.db", tmp_path / "estate.db")      # never the demo itself: load() adds an index
    c = catalog.load(sqlite3.connect(tmp_path / "estate.db"), "google/siglip2-base-patch16-256", tmp_path, tmp_path)
    asked = []

    def looks(cat, member):
        asked.append(member)
        hide = np.zeros(cat.N, bool)
        hide[: member or 0] = True
        return np.ones(cat.N, bool), hide, cat.said_fav
    live = catalog.Live(c, looks)
    one, two = live.visible_for(1), live.visible_for(2)
    assert live.visible_for(1) is one and asked == [1, 2]
    assert one.info["total"] == c.N - 1 and two.info["total"] == c.N - 2 and bool(live.shared_for(2).all())
    live.relook()
    assert live.visible_for(1) is not one and asked == [1, 2, 1]
    live.install(c)
    live.visible_for(1)
    assert asked == [1, 2, 1, 1]
