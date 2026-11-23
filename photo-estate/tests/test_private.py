"""Private photos (multiplayer phase 5: private.py), on the demo library in process, as its three members: Ada
the sysadmin, Ben and Cleo. Every rule of who may be shown a photo with one, two and three sharers; every way
a photo, or anything worked out from it, leaves the app, as its sharer and as someone else, for a held photo
and a withdrawn one; a real HTTP server asked for every pixel and metadata route of a private photo, answering
exactly what it answers for a sha it does not have; People; release now and on a day (the timer, on a fake
clock); an agent acting for Cleo, and for nobody; ingest (two members sending the same bytes, a phone upload
private before any reload can show it, the old inbox layout); the hold's scores on the demo; the one-off run's
dry run and its second run.

  uv run pytest tests/test_private.py
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import embed  # noqa: E402
import estate  # noqa: E402
import faces  # noqa: E402
import private  # noqa: E402
from photo_app import PhotoApp  # noqa: E402
from test_multiplayer import ADA, ALL, BEN, CLEO, call, feed, mid, seen, settle, tool  # noqa: E402

MODEL = embed.DEFAULT_MODEL
UNKNOWN = "0" * 64


@pytest.fixture
def app():
    a = embed.parser().parse_args(["serve", "--demo", "--no-pregenerate"])
    app = PhotoApp(a)             # start() is not called: no model, no threads; its own scratch catalog
    app.feed_update()
    return app


def db(app) -> sqlite3.Connection:
    return sqlite3.connect(app.a.db)


def share(app, sha: str, *who) -> None:
    """These members shared the photo (as ingest records it: private.record_sharer)."""
    con = db(app)
    con.executemany("INSERT OR IGNORE INTO sharers VALUES (?,?,'2026-01-01')", ((sha, mid(app, w)) for w in who))
    con.commit()
    con.close()
    app.relook()


def withdraw(app, who, shas, until=None, expect=200, on=True):
    body = {"shas": list(shas), "private": on, **({"until": until} if until is not None else {})}
    got = call(app, "POST", "/api/private", who, body, expect=expect)
    settle(app)
    return got


def hold(app, who, sha) -> int:
    """Hold a photo for `who`, as private.py check does for a phone upload that looks like a receipt."""
    con = db(app)
    with con:
        batch = private._hold(con, mid(app, who), [sha], "upload", private.now())
    con.close()
    app.relook()
    settle(app)
    return batch


def raw(app, method, path, who, body=None):
    r = app.request(method, path, headers=who, body=body if body is not None else (b"" if method == "GET" else {}))
    return r.status, r.body


def pick(app, k: int) -> str:
    return app.live.cat.shas[k]


# -- the rule: who may be shown a photo -----------------------------------------------------------------

def test_shared_for_every_rule_with_one_two_and_three_sharers():
    p = private.Private(sysadmin=1)
    A, B, C, D = 1, 2, 3, 4

    def case(sharers, holders):
        p.rows = {"x": {m: ("withdrawn", None, None) for m in holders}} if holders else {}
        p.sharers = {"x": frozenset(sharers)} if sharers else {}
        return {m: not p.refused("x", m) for m in (A, B, C, D, private.NOBODY)}
    every = {A: True, B: True, C: True, D: True, private.NOBODY: True}
    # one sharer
    assert case({B}, set()) == every
    assert case({B}, {B}) == {A: False, B: True, C: False, D: False, private.NOBODY: False}
    # two: private to one of them, the other still shares it with the family; to both, those two only
    assert case({B, C}, {B}) == {**every, private.NOBODY: False}
    assert case({B, C}, {B, C}) == {A: False, B: True, C: True, D: False, private.NOBODY: False}
    # three
    assert case({B, C, D}, {B, C}) == {**every, private.NOBODY: False}
    assert case({B, C, D}, {B, C, D}) == {A: False, B: True, C: True, D: True, private.NOBODY: False}
    # no sharer row: the first sysadmin's; None (no member) reads as the first sysadmin
    assert case(set(), {A}) == {A: True, B: False, C: False, D: False, private.NOBODY: False}
    assert not p.refused("x", None)
    assert p.mine(A) == {"x": {"why": "withdrawn", "since": None, "until": None}} and p.mine(B) == {}


def test_withdrawn_by_ben_still_shown_to_all_by_both_to_those_two(app):
    x = pick(app, 10)
    share(app, x, BEN, CLEO)
    assert all(x in seen(app, w) for w in (ADA, BEN, CLEO))
    got = withdraw(app, BEN, [x])
    assert got["withdrawn"] == 1 and all(x in seen(app, w) for w in (ADA, BEN, CLEO)), "Cleo still shares it"
    why = call(app, "GET", f"/api/photos/{x}/why", BEN)["private"]
    assert why["sharer"] and why["mine"]["why"] == "withdrawn" and why["others_sharing"] == 1
    withdraw(app, CLEO, [x])
    assert x in seen(app, BEN) and x in seen(app, CLEO) and x not in seen(app, ADA)
    assert raw(app, "GET", f"/thumb/{x}", ADA) == (404, b"")
    withdraw(app, BEN, [x], on=False)                        # released by one: the family sees it again
    assert x in seen(app, ADA)
    # Ada is no sharer: she may not make it private, and Cleo may not release Ben's hold
    call(app, "POST", "/api/private", ADA, {"shas": [x], "private": True}, expect=403)


# -- every way out ---------------------------------------------------------------------------------------

def demo_trip(app):
    """A trip of two photos (each its own event) and the city of the first: made Ben's alone below."""
    c = app.live.cat
    for tid in c.trips:
        ids = np.flatnonzero(c.trip_of == tid)
        if len(ids) == 2:
            return c.shas[ids[0]], c.shas[ids[1]], tid, c.meta[c.shas[ids[0]]]
    raise AssertionError("the demo has a trip of two photos")


@pytest.mark.parametrize("how", ["held", "withdrawn"])
def test_every_way_out_for_the_sharer_and_for_anyone_else(app, how):
    x, other, tid, m = demo_trip(app)
    app.browse.text_vec = lambda text: app.live.cat.M[0]       # (no model here: words rank by any vector)
    eid, city = m.get("event"), (m.get("city") or "").split(",")[0]
    con = db(app)
    con.execute("UPDATE assets SET people='Zed Quux' WHERE sha256=?", (x,))    # a name only this photo carries
    con.commit()
    con.close()
    app.live.install(app.load_catalog(), then=app.caught_up, looks=app.with_private)   # (read whole: a tag changed)
    # Cleo's own album with the photo in it (made while the family could see it), and Ada's published series
    album = call(app, "POST", "/api/albums", CLEO, {"title": "Cleo's", "items": [{"sha": x}, {"sha": other}]}, expect=201)["id"]
    series = call(app, "POST", "/api/albums", ADA, {"title": "Ada's", "items": [{"sha": x}, {"sha": other}]}, expect=201)["id"]
    call(app, "POST", f"/api/albums/{series}/publish", ADA, {"on": True})
    hiding = call(app, "POST", "/api/changes", CLEO, {"title": "hide", "items": [{"sha": x}, {"sha": other}]}, expect=201)["id"]
    cleo_feed = feed(app, CLEO)
    assert x in cleo_feed
    share(app, x, BEN)
    if how == "held":
        hold(app, BEN, x)
    else:
        withdraw(app, BEN, [x])

    # Ben, its sharer, is shown all of it, marked
    rows = {r["sha"]: r for r in call(app, "GET", f"/api/search?{ALL}", BEN)["results"]}
    assert rows[x]["private"] == how and "private" not in rows[other]
    assert seen(app, BEN, "&q=is:private") == [x] and seen(app, BEN, f"&q=is:{how}") == [x]
    assert call(app, "GET", f"/api/photos/{x}/why", BEN)["private"]["mine"]["why"] == how
    for route in ("thumb", "preview", "original"):
        assert raw(app, "GET", f"/{route}/{x}", BEN)[0] == 200
    assert "Zed Quux" in [n for n, _ in call(app, "GET", "/api/facets", BEN)["facets"]["person"]]

    # for Ada and Cleo it does not exist
    total = app.live.cat.N
    for who in (ADA, CLEO):
        assert x not in seen(app, who) and seen(app, who, "&q=is:private") == []
        info = call(app, "GET", "/api/info", who)
        assert info["total"] == total - 1
        assert next(t for t in info["trips"] if t["id"] == tid)["n"] == 1 and all(t["cover"] != x for t in info["trips"])
        facets = call(app, "GET", "/api/facets", who)
        assert facets["matching"] == total - 1 and "Zed Quux" not in [n for n, _ in facets["facets"]["person"]]
        assert all(c["cover"] != x for k in ("years", "months") for c in call(app, "GET", "/api/periods", who)[k])
        assert x not in seen(app, who, "&hidden=1")
        for path in (f"/api/photos/{x}/why", f"/api/similar?sha={x}"):
            assert raw(app, "GET", path, who) == raw(app, "GET", path.replace(x, UNKNOWN), who) and raw(app, "GET", path, who)[0] == 404
        assert x not in [r["sha"] for r in call(app, "GET", f"/api/similar?sha={other}&limit=500", who)["results"]]
        # a stack named by it is as a sha the library does not have
        assert seen(app, who, f"&stack={x}") == seen(app, who, f"&stack={UNKNOWN}")
        # its event and trip: no title for an event only it is in, and the trip's count without it
        if eid is not None:
            r = call(app, "GET", f"/api/search?event={eid}", who)
            assert r["scope"] is None and r["results"] == []
        r = call(app, "GET", f"/api/search?trip={tid}", who)
        assert r["scope"]["n"] == 1 and [p["sha"] for p in r["results"]] == [other]
        # a name or place only it shows is a plain word
        assert call(app, "GET", "/api/search?q=Zed", who)["people"] == []
        if city:
            assert city not in call(app, "GET", f"/api/search?q={city}", who)["places"]
        for route in ("thumb", "preview", "large", "original", "video"):
            r = app.request("GET", f"/{route}/{x}", headers=who)
            u = app.request("GET", f"/{route}/{UNKNOWN}", headers=who)
            assert (r.status, r.body) == (u.status, u.body) and r.status == 404, route
        call(app, "POST", f"/api/photos/{x}/show", who, {"on": True}, expect=404)
        assert call(app, "POST", "/api/favorite", who, {"sha": x, "fav": True}, expect=400) == \
            call(app, "POST", "/api/favorite", who, {"sha": UNKNOWN, "fav": True}, expect=400)
        call(app, "POST", "/api/private", who, {"shas": [x], "private": False}, expect=404)
    if city:
        assert city in call(app, "GET", f"/api/search?q={city}", BEN)["places"]
    # albums: Cleo's own lists, counts, covers and zips it no more; Ada's series neither, for Cleo
    for sid in (album, series):
        got = call(app, "GET", f"/api/albums/{sid}/photos", CLEO)
        assert [r["sha"] for r in got["results"]] == [other] and got["matching"] == 1
        assert app.album_files(sid, mid(app, CLEO))[1] == [(app.images.original(other, app.live.cat.meta[other]["ext"]),
                                                             app.live.cat.meta[other]["path"].rsplit("/", 1)[-1])]
    cards = {s["id"]: s for s in call(app, "GET", "/api/albums", CLEO)["sets"]}
    assert cards[album]["n"] == 1 and x not in cards[album]["cover"] and x not in cards[hiding]["cover"]
    assert cards[hiding]["n"] == 1
    added = call(app, "POST", f"/api/albums/{album}/items", CLEO, {"items": [{"sha": x}]})
    assert added["unknown"] == 1 and added["added"] == 0, "adding it is adding a sha the library does not have"
    made = call(app, "POST", "/api/albums", CLEO, {"title": "new", "items": [{"sha": x}, {"sha": UNKNOWN}]}, expect=201)
    assert made["n"] == 0 and made["unknown"] == 2
    # the review page of Cleo's hiding set, and its check
    d = call(app, "GET", f"/api/changes/{hiding}", CLEO)
    assert [it["sha"] for it in d["items"]] == [other] and d["n"] == 1
    assert x not in json.dumps(call(app, "GET", f"/api/changes/{hiding}/check", CLEO))
    # the sync feed: it leaves Cleo's phone, not Ben's
    app.feed_update()
    assert x not in feed(app, CLEO) and x in feed(app, BEN)
    # the MCP tools, for Cleo: search, similar, get_photos, overview and albums
    ids = [p["id"] for p in tool(app, CLEO, "search_photos", {"limit": 200})["photos"]]
    assert x not in ids and len(ids) == total - 1
    assert tool(app, CLEO, "get_photos", {"ids": [x, UNKNOWN]})["photos"] == [{"id": x, "error": "no such photo"},
                                                                               {"id": UNKNOWN, "error": "no such photo"}]
    for args in ({"id": x}, {"id": UNKNOWN}):
        got = call(app, "POST", "/mcp", CLEO, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                "params": {"name": "similar_photos", "arguments": args}})
        assert got["result"]["isError"] and got["result"]["content"][0]["text"] == "no photo has that id"
    got = call(app, "POST", "/mcp", CLEO, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                           "params": {"name": "search_photos", "arguments": {"people": ["Zed Quux"]}}})
    assert got["result"]["isError"] and "nobody in the library is named" in got["result"]["content"][0]["text"]
    over = tool(app, CLEO, "library_overview")
    assert over["photos"] == total - 1 and "Zed Quux" not in [p["name"] for p in over["people"]]
    assert x not in json.dumps(tool(app, CLEO, "get_album", {"album": series}))
    assert x in json.dumps(tool(app, BEN, "get_photos", {"ids": [x]})) and \
        tool(app, BEN, "get_photos", {"ids": [x]})["photos"][0]["private"] == how


def test_a_live_photos_clip_and_a_face_crop_follow_the_still(app, tmp_path):
    """Over real HTTP: as Cleo, every pixel and metadata route of a photo private to Ben answers exactly what
    it answers for a sha (or a face) that does not exist, the Live Photo clip of the still and a crop of a face
    in it included; Ben is shown them."""
    x, clip = pick(app, 20), "c" * 64
    store = tmp_path / "store"                       # the demo store is never written to: the still's original and
    for sha, ext, src in ((x, app.live.cat.meta[x]["ext"], None), (pick(app, 21), app.live.cat.meta[pick(app, 21)]["ext"], None),
                          (clip, ".mov", b"not really a movie")):   # the clip, here
        dest = store / sha[:2] / sha[2:4] / f"{sha}{ext}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(src if src is not None else app.images.original(sha, ext).read_bytes())
    app.images.store = store
    con = faces.connect(app.a.db)
    con.execute("INSERT INTO assets(sha256, ext, kind) VALUES (?, '.mov', 'video')", (clip,))
    con.execute("INSERT INTO live_pairs(still, clip) VALUES (?,?)", (x, clip))
    v = np.ones(512, np.float16) / np.sqrt(512)
    for fid, sha in ((1, x), (2, pick(app, 21))):
        con.execute("INSERT INTO faces(id, sha256, idx, model, x, y, w, h, score, px, vec) VALUES (?,?,0,?,0.1,0.1,0.2,0.2,0.9,100,?)",
                    (fid, sha, faces.MODEL, v.tobytes()))
        crop = faces.crop_path(app.cache, sha, 0)
        crop.parent.mkdir(parents=True, exist_ok=True)
        crop.write_bytes(b"\xff\xd8 a face")
    con.commit()
    con.close()
    share(app, x, BEN)
    app.reload_catalog()
    app.load_people()
    assert app.live.cat.stills.get(clip) == (x,)
    srv = app.make_server("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def get(path, who, method="GET", body=None):
        req = urllib.request.Request(base + path, method=method, headers={**who, "Content-Type": "application/json"},
                                     data=None if body is None else json.dumps(body).encode())
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.headers.get("Content-Type"), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type"), e.read()
    routes = ["/thumb/{s}", "/preview/{s}", "/large/{s}", "/original/{s}", "/video/{s}", "/video/{s}?original=1",
              "/api/photos/{s}/why", "/api/similar?sha={s}", "/api/search?stack={s}&limit=1"]
    try:
        assert get(f"/video/{clip}", CLEO)[0] == 200 and get("/face/1", CLEO)[0] == 200, "shared: everyone sees them"
        monkey = app.load_people
        app.load_people = lambda: None               # People keeps the face a moment longer: the crop itself refuses
        try:
            assert get("/api/private", BEN, "POST", {"shas": [x], "private": True})[0] == 200
            settle(app)
        finally:
            app.load_people = monkey
        for path in routes + ["/video/" + clip]:
            got = get(path.format(s=x) if "{s}" in path else path, CLEO)
            none = get(path.format(s=UNKNOWN) if "{s}" in path else path.replace(clip, UNKNOWN), CLEO)
            assert got == none, (path, got[:2], none[:2])
            if not path.startswith("/api/search"):
                assert got[0] == 404, path
        assert get("/face/1", CLEO) == get("/face/999", CLEO) and get("/face/1", CLEO)[0] == 404
        assert get("/face/2", CLEO)[0] == 200
        assert get(f"/video/{clip}", BEN)[0] == 200 and get(f"/thumb/{x}", BEN)[0] == 200 and get("/face/1", BEN)[0] == 200
        assert get(f"/api/photos/{x}/show", CLEO, "POST", {"on": True}) == get(f"/api/photos/{UNKNOWN}/show", CLEO, "POST", {"on": True})
        # People itself: built again, it has no face of a private photo, for anyone (its sharer too)
        app.load_people()
        for who in (BEN, CLEO):
            assert get("/face/1", who)[0] == 404 and get("/api/people/photo?face=1", who)[0] == 404
        assert get("/face/2", BEN)[0] == 200
        # the album zip, over HTTP: Ben's album of both photos, shared with nobody; Cleo's own of them
        sid = call(app, "POST", "/api/albums", CLEO, {"title": "two", "items": [{"sha": x}, {"sha": pick(app, 21)}]}, expect=201)["id"]
        status, _, body = get(f"/api/albums/{sid}/download", CLEO)
        names = zipfile.ZipFile(io.BytesIO(body)).namelist()
        assert status == 200 and names == [app.live.cat.meta[pick(app, 21)]["path"].rsplit("/", 1)[-1]]
    finally:
        srv.shutdown()


def test_people_is_built_without_private_photos_and_follows_a_release(app):
    x = pick(app, 30)
    share(app, x, BEN)
    con = db(app)
    assert x not in faces.default_hidden(con)
    con.close()
    loads = []
    app.load_people = lambda: loads.append(1)     # (the demo has no faces: count the reloads instead)
    app.live.cat.has_faces = True
    withdraw(app, BEN, [x])
    con = db(app)
    assert x in faces.default_hidden(con), "a private photo's faces are in People for no one, its sharer too"
    con.close()
    assert loads == [1]
    withdraw(app, BEN, [x], on=False)
    con = db(app)
    assert x not in faces.default_hidden(con)
    con.close()
    assert loads == [1, 1], "a release brings its faces back"


# -- release, now and on a day --------------------------------------------------------------------------

def test_release_now_and_on_a_day_by_the_timer(app):
    x, y = pick(app, 40), pick(app, 41)
    share(app, x, BEN)
    share(app, y, BEN)
    hold(app, BEN, x)
    withdraw(app, BEN, [y], until="2026-12-26")
    assert x not in seen(app, CLEO) and y not in seen(app, CLEO)
    items = call(app, "GET", "/api/inbox", BEN)["items"]
    assert [(i["kind"], i["opens"]) for i in items] == [("held", "/?q=is:held")]
    withdraw(app, BEN, [x], until="2026-12-26")                     # the held photo gets a day too
    con = db(app)
    assert con.execute("SELECT why, until FROM private WHERE sha256=?", (x,)).fetchone() == ("held", "2026-12-26")
    con.close()
    call(app, "POST", "/api/private", BEN, {"shas": [y], "private": True, "until": "Dec 26"}, expect=400)
    # the timer, on a fake clock: nothing on Christmas Day, both on the 26th
    app.today = lambda: "2026-12-25"
    assert app.release_due() == {}
    app.today = lambda: "2026-12-26"
    app.release_every = 0.02
    timer = threading.Thread(target=app.release_timer, daemon=True)
    timer.start()
    end = time.time() + 10
    while y not in seen(app, CLEO) or x not in seen(app, CLEO):
        assert time.time() < end, "the timer did not release them"
        time.sleep(0.02)
    app.release_stop.set()
    timer.join(5)
    settle(app)
    assert call(app, "GET", "/api/inbox", BEN)["items"] == [], "nothing it held is held: the item is done"
    con = db(app)
    entries = con.execute("SELECT actor, acting_for, kind, data FROM journal WHERE kind LIKE 'photo.%' ORDER BY id").fetchall()
    con.close()
    kinds = [k for _, _, k, _ in entries]
    assert kinds == ["photo.held", "photo.withdrawn", "photo.release_set", "photo.released"]
    assert entries[-1][:2] == (None, mid(app, BEN)) and json.loads(entries[-1][3]) == {"photos": 2, "auto": True, "until": "2026-12-26"}
    # released now, and in a batch from the held view (which: held)
    hold(app, BEN, x)
    got = call(app, "POST", "/api/private", BEN, {"which": "held", "private": False})
    assert got["released"] == 1 and got["photos"] == 1
    settle(app)
    assert x in seen(app, CLEO)


# -- an agent ------------------------------------------------------------------------------------------

def test_an_agent_acting_for_cleo_or_for_nobody(app):
    x, y = pick(app, 50), pick(app, 51)
    share(app, x, BEN)
    share(app, y, BEN, CLEO)
    withdraw(app, BEN, [x, y])               # x: Ben's alone; y: Cleo still shares it
    con = db(app)
    task = lambda af: con.execute("INSERT INTO agent_tasks(prompt, status, created_by, created_at, acting_for) VALUES"   # noqa: E731
                                  " ('look', 'done', 'x', '2026-01-01', ?)", (af,)).lastrowid
    cleo, nobody = task(mid(app, CLEO)), task(None)
    con.commit()
    total = app.live.cat.N
    for tid, want in ((cleo, {y}), (nobody, set())):
        h = {"X-Estate-Task": str(tid)}
        ids = {p["id"] for p in tool(app, {}, "search_photos", {"limit": 200}, headers=h, local=True)["photos"]}
        assert x not in ids and (y in ids) == bool(want) and len(ids) == total - 1 - (not want)
        assert tool(app, {}, "get_photos", {"ids": [x]}, headers=h, local=True)["photos"] == [{"id": x, "error": "no such photo"}]
        assert tool(app, {}, "library_overview", headers=h, local=True)["photos"] == total - 1 - (not want)
    assert y in seen(app, ADA), "acting for nobody is stricter than any member"
    # and a task's own record (its events: what its agent saw) is its member's only
    con.execute("INSERT INTO agent_events(task_id, at, kind, text) VALUES (?, '', 'tool', ?)", (cleo, f"found {y}"))
    con.commit()
    con.close()
    assert call(app, "GET", f"/api/tasks/{cleo}", CLEO)["events"][0]["text"] == f"found {y}"
    call(app, "GET", f"/api/tasks/{cleo}", BEN, expect=404)
    assert cleo not in [t["id"] for t in call(app, "GET", "/api/tasks", BEN)["tasks"]]


# -- ingest ------------------------------------------------------------------------------------------------

def jpeg(seed: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), ((seed * 70) % 256, (seed * 30) % 256, (seed * 110) % 256)).save(buf, "JPEG", quality=90)
    return buf.getvalue()


def ingest(app, inbox: Path, store: Path) -> None:
    estate.cmd_ingest(argparse.Namespace(db=app.a.db, store=str(store), sources=[str(inbox)], force=True, facts=None,
                                         delete_after=False, progress=False, uploads=True))


def embed_as(app, sha: str, vec) -> None:
    v = np.asarray(vec, np.float32)
    v = (v / np.linalg.norm(v)).astype(np.float16)
    con = db(app)
    con.execute("INSERT OR REPLACE INTO embeddings VALUES (?,?,?,?,NULL,'2026-11-22')", (sha, MODEL, v.shape[0], v.tobytes()))
    con.commit()
    con.close()


def test_ingest_records_the_sharer_and_holds_before_any_reload_can_show_it(app, tmp_path):
    inbox, store = tmp_path / "iphone", tmp_path / "store"
    ben, cleo, ada = mid(app, BEN), mid(app, CLEO), mid(app, ADA)
    receipt, beach, old = jpeg(1), jpeg(2), jpeg(3)
    # through the upload route, as an old iPhone app sends them (its path is unchanged): each lands in its
    # member's folder; one from before member folders is at the inbox's top, and is the first sysadmin's
    app.admin.inbox = inbox
    for who, data, name in ((BEN, receipt, "IMG_1.JPG"), (CLEO, receipt, "IMG_9.JPG"), (BEN, beach, "IMG_2.JPG")):
        r = app.request("PUT", f"/api/upload/2026/11/14/{name}", headers=who, body=data)
        assert r.status == 201, r.body
    assert sorted(p.relative_to(inbox).as_posix() for p in inbox.rglob("*.JPG")) == \
        [f"m{ben}/2026/11/14/IMG_1.JPG", f"m{ben}/2026/11/14/IMG_2.JPG", f"m{cleo}/2026/11/14/IMG_9.JPG"]
    (inbox / "2026" / "11" / "13").mkdir(parents=True)
    (inbox / "2026" / "11" / "13" / "IMG_0.JPG").write_bytes(old)
    assert app.request("PUT", "/api/upload/2026/11/13/IMG_0.JPG", headers=ADA, body=old).status == 200, \
        "the sysadmin's upload sent again before member folders is found where it was"
    ingest(app, inbox, store)
    r_sha, b_sha, o_sha = (hashlib.sha256(d).hexdigest() for d in (receipt, beach, old))
    con = db(app)
    assert con.execute("SELECT count(*) FROM assets WHERE sha256=?", (r_sha,)).fetchone()[0] == 1
    sharers = lambda s: {m for m, in con.execute("SELECT member_id FROM sharers WHERE sha256=?", (s,))}   # noqa: E731
    assert sharers(r_sha) == {ben, cleo} and sharers(b_sha) == {ben} and sharers(o_sha) == {ada}
    rows = sorted(con.execute("SELECT sha256, member_id, why FROM private").fetchall())
    assert rows == sorted([(r_sha, ben, "new"), (r_sha, cleo, "new"), (b_sha, ben, "new"), (o_sha, ada, "new")])
    assert {a for a, in con.execute("SELECT album FROM occurrences WHERE source='iphone'")} == {None}, "a member folder is no album"
    con.close()
    # the worst moment: their vectors are in (the app embeds uploads as they arrive) and the catalog is reloaded
    # before `private.py check` has run. Nobody but each photo's sharer is shown it.
    P = private.prompt_vectors(MODEL)
    embed_as(app, r_sha, P[0])                                   # looks exactly like "a receipt"
    embed_as(app, b_sha, app.live.cat.M[0])                      # looks like a demo landscape
    embed_as(app, o_sha, app.live.cat.M[1])
    app.reload_catalog()
    assert {r_sha, b_sha, o_sha} <= set(app.live.cat.index)
    assert r_sha in seen(app, BEN) and r_sha in seen(app, CLEO) and b_sha in seen(app, BEN) and o_sha in seen(app, ADA)
    assert r_sha not in seen(app, ADA) and b_sha not in seen(app, CLEO) and b_sha not in seen(app, ADA)
    assert o_sha not in seen(app, BEN) and not app.may_see(b_sha, ada)
    # and an app started now (a restart at that moment) is no different
    fresh = PhotoApp(embed.parser().parse_args(["--db", app.a.db, "serve", "--store", str(store), "--no-pregenerate"]))
    try:
        assert b_sha not in [s for s, ok in zip(fresh.live.cat.shas, fresh.live.visible_for(cleo).visible) if ok]
        assert b_sha in [s for s, ok in zip(fresh.live.cat.shas, fresh.live.visible_for(ben).visible) if ok]
    finally:
        fresh.tasks.stop()            # (its worker reads the scratch catalog, which goes when the test does)
    # the check: the receipt is held, for both its sharers; the rest is let go
    con = db(app)
    got = private.check(con, MODEL, out=lambda *a: None)
    assert got == {"checked": 4, "held": 2, "waiting": 0}
    assert sorted(con.execute("SELECT sha256, member_id, why FROM private").fetchall()) == \
        sorted([(r_sha, ben, "held"), (r_sha, cleo, "held")])
    assert [(k, json.loads(d)) for k, d in con.execute("SELECT kind, data FROM journal WHERE kind='photo.held'")] == \
        [("photo.held", {"photos": 1, "how": "upload"})] * 2
    con.close()
    app.reload_catalog()
    assert b_sha in seen(app, CLEO) and o_sha in seen(app, BEN) and r_sha not in seen(app, ADA)
    assert [i["kind"] for i in call(app, "GET", "/api/inbox", BEN)["items"]] == ["held"]
    # a photo the family already sees is not held when another member sends it too
    (inbox / f"m{cleo}" / "2026" / "11" / "15").mkdir(parents=True)
    (inbox / f"m{cleo}" / "2026" / "11" / "15" / "B.JPG").write_bytes(beach)
    ingest(app, inbox, store)
    con = db(app)
    assert sharers(b_sha) == {ben, cleo} and not con.execute("SELECT 1 FROM private WHERE sha256=?", (b_sha,)).fetchone()
    con.close()


def test_the_ingest_script_checks_before_it_tells_the_app():
    """ingest-iphone.sh: the ingest marks phone uploads (--uploads), and the check runs ahead of catalog_ready
    (the reload); the order is not what keeps a new photo private, the `new` row is, but it is the order."""
    lines = (ROOT / "ingest-iphone.sh").read_text().splitlines()
    at = lambda s: next(i for i, x in enumerate(lines) if s in x and not x.lstrip().startswith("#"))   # noqa: E731
    assert "--uploads" in lines[at("run estate.py ingest")]
    assert at("run embed.py run") < at("run private.py check") < at("event catalog_ready")


def test_the_inbox_sweep_counts_member_folders(tmp_path):
    from upload import IngestJob
    inbox = tmp_path / "iphone"
    for rel in ("m2/2026/01/01/a.jpg", "2025/01/01/b.jpg"):
        (inbox / rel).parent.mkdir(parents=True, exist_ok=True)
        (inbox / rel).write_bytes(b"x")
    job = IngestJob(tmp_path / "x.sh", tmp_path, str(tmp_path / "estate.db"), tmp_path / "store", inbox)
    assert job.waiting()["files"] == 2


# -- the hold's scores, and the one-off run -----------------------------------------------------------------

def test_the_prompts_vectors_are_the_prompts_and_the_demo_holds_nothing(app):
    got = json.loads(private.VECTORS.read_text())
    assert set(got[MODEL]) == set(private.PROMPTS)
    P = private.prompt_vectors(MODEL)
    assert P.shape == (len(private.PROMPTS), app.live.cat.M.shape[1]) and np.allclose(np.linalg.norm(P, axis=1), 1, atol=2e-3)
    S = private.scores(app.live.cat.M, MODEL)
    assert S.max() < private.HOLD_AT - 0.01, f"a demo landscape scores {S.max():.3f}"
    con = db(app)
    judged = private.judge(con, MODEL, app.live.cat.shas)
    con.close()
    assert len(judged) == app.live.cat.N and not any(why for why, _ in judged.values()), "the demo holds nothing"
    # a phone screenshot is held by its shape alone; a camera photo of that size is not
    assert private.reasons([0, 0, 0], True) == ["is_screenshot"] and private.reasons([0.08, 0, 0], False) == ["a receipt"]
    from catalog import is_screenshot
    assert is_screenshot({"ext": ".png", "w": 1170, "h": 2532, "path": "m2/2026/11/14/IMG_0042.PNG"})
    assert not is_screenshot({"ext": ".heic", "w": 1170, "h": 2532, "camera": "iPhone 15", "path": "m2/x.HEIC"})


def test_the_one_off_run_dry_run_then_twice(app):
    ada = mid(app, ADA)
    r_sha = pick(app, 60)
    embed_as(app, r_sha, private.prompt_vectors(MODEL)[2])        # a demo photo made to look like "a document"
    con = db(app)
    before = [con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("private", "journal", "inbox_items")]
    said: list[str] = []
    got = private.library(con, ada, MODEL, dry_run=True, out=said.append)
    assert [con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("private", "journal", "inbox_items")] == before
    assert got["held"] == 1 and got["by_reason"]["a document"] == 1 and got["photos"] == app.live.cat.N
    assert [x["id"] for x in got["sample_held"]] == [r_sha] and sum(b["photos"] for b in got["near"]) >= 1
    text = "\n".join(said)
    assert r_sha in text and "nothing is changed" in text
    names = [fn for fn, in con.execute("SELECT filename FROM occurrences")]
    assert not any(n in text for n in names), "no file names"
    got = private.library(con, ada, MODEL, out=lambda *a: None)
    assert got["held"] == 1
    assert con.execute("SELECT why, batch FROM private WHERE sha256=?", (r_sha,)).fetchone() == ("held", got["batch"])
    assert [json.loads(d) for d, in con.execute("SELECT data FROM journal WHERE kind='photo.held'")] == [{"photos": 1, "how": "library"}]
    assert private.library(con, ada, MODEL, out=lambda *a: None)["held"] == 0, "the second run holds nothing again"
    private.change(con, ada, [r_sha], False)
    assert private.library(con, ada, MODEL, out=lambda *a: None)["held"] == 0, "nor what she released"
    assert con.execute("SELECT count(*) FROM journal WHERE kind='photo.held'").fetchone()[0] == 1
    assert con.execute("SELECT state FROM inbox_items WHERE kind='held'").fetchall() == [("done",)]
    con.close()


HELD_SCRIPT = """# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["torch", "transformers>=4.49", "pillow>=10.0", "pillow-heif>=0.18", "sentencepiece", "protobuf", "numpy"]
# ///
import sys
sys.path.insert(0, sys.argv[1])
import numpy as np
from PIL import Image, ImageDraw
import embed, encoder, private
MODEL = embed.DEFAULT_MODEL
vecs = embed.text_vectors(list(private.PROMPTS), MODEL, 4, "cpu")
assert np.allclose(vecs, private.prompt_vectors(MODEL), atol=2e-3), "hold-prompts.json is not what the model makes"
e = encoder.Encoder(MODEL, 4, images=True, device="cpu")
e.load()
side = e.side
LINES = {"receipt": "ITEM {i}   {p:6.2f}      TOTAL", "screenshot": "9:41    Messages    See you at {i}!",
         "document": "The tenant shall pay the amount due on day {i} of each month"}
for kind, (w, h) in (("receipt", (600, 1400)), ("screenshot", (1170, 2532)), ("document", (1700, 2200))):
    im = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(im)
    for i in range(40):
        d.text((40, 60 + i * (h // 45)), LINES[kind].format(i=i, p=i * 1.37), fill="black")
    v = e.image(im.resize((side, side))).astype(np.float32)
    s = private.scores(v[None] / np.linalg.norm(v), MODEL)[0]
    print(kind, [round(float(x), 4) for x in s])
    assert s.max() >= private.HOLD_AT, (kind, s)
print("ok")
"""


@pytest.mark.slow
@pytest.mark.model
def test_made_receipts_screenshots_and_documents_are_held(tmp_path):
    """With the model, in its own environment (`uv run --script`, as embed.py runs): the prompts' vectors in
    hold-prompts.json are what the model makes of PROMPTS, and images made to look like a receipt, a phone
    screenshot and a document score at or over HOLD_AT."""
    import subprocess
    script = tmp_path / "held.py"
    script.write_text(HELD_SCRIPT)
    r = subprocess.run(["uv", "run", "--quiet", "--script", str(script), str(ROOT)], capture_output=True, text=True,
                       timeout=900, env={**__import__("os").environ, "HF_HUB_OFFLINE": "1"})
    assert r.returncode == 0 and r.stdout.strip().endswith("ok"), r.stdout[-2000:] + r.stderr[-3000:]


def test_prompt_vectors_missing_keeps_new_photos_private(app, monkeypatch, tmp_path):
    x = pick(app, 70)
    con = db(app)
    con.execute("INSERT INTO private(sha256, member_id, why, since) VALUES (?,?,'new','')", (x, mid(app, ADA)))
    con.commit()
    monkeypatch.setattr(private, "VECTORS", tmp_path / "none.json")
    with pytest.raises(private.MissingVectors):
        private.check(con, MODEL, out=lambda *a: None)
    assert con.execute("SELECT why FROM private WHERE sha256=?", (x,)).fetchone() == ("new",)
    con.close()
