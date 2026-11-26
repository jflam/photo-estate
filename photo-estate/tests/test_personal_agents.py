"""A member's own agent (multiplayer phase 8: members.py's bindings, estate_mcp.py, journal.for_member), in process:
every kind of caller of /mcp (no login, a login with no member, a member's agent pending, allowed and turned off,
the estate's agent with its token, this computer itself); that nothing an agent sends binds it (allowing comes from
another device, on the web); no task tools; only its member's view, journal_since included; who gets each journal
kind; cursors; and a series kept up to date end to end (the listener's request, the agent catching up, adding what
belongs, closing the request).

  uv run pytest tests/test_personal_agents.py
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import embed  # noqa: E402
import journal  # noqa: E402
from photo_app import PhotoApp  # noqa: E402
from test_multiplayer import ADA, BEN, CLEO, bind, call, mid, settle  # noqa: E402

WRITERS = ("create_change_set", "amend_change_set", "create_album", "amend_album", "propose_hiding", "ask_member")
READERS = ("library_overview", "search_photos", "similar_photos", "get_photos", "list_change_sets", "get_change_set",
           "list_albums", "get_album", "journal_since")
ANY_ARGS = {"create_change_set": {"title": "x"}, "amend_change_set": {"change_set": 1}, "create_album": {"title": "x"},
            "amend_album": {"album": 1}, "propose_hiding": {"album": 1}, "ask_member": {"photos": ["0" * 64], "text": "?"}}


@pytest.fixture
def app():
    a = embed.parser().parse_args(["serve", "--demo", "--no-pregenerate"])
    app = PhotoApp(a)
    yield app
    app.tasks.stop()


@pytest.fixture
def real(tmp_path):
    """The demo catalog served as a real one (no --demo): Tailscale's headers, and a whois the test gives."""
    shutil.copyfile(ROOT / "demo" / "estate.db", tmp_path / "estate.db")
    app = PhotoApp(embed.parser().parse_args(["--db", str(tmp_path / "estate.db"), "serve", "--store", str(ROOT / "demo" / "store"),
                                              "--no-pregenerate", "--upload-user", "owner@example.com",
                                              "--upload-user", "kid@example.com"]))
    names = {"100.64.0.5": ("node:nKID1", "kid-macbook"), "100.64.0.9": ("node:nKID2", "kid-iphone")}
    app.whois = names.get
    yield app
    app.tasks.stop()


def rpc(app, headers, method, params=None, local=False, expect=200):
    r = app.request("POST", "/mcp", headers=headers, local=local,
                    body={"jsonrpc": "2.0", "id": 1, "method": method, **({"params": params} if params is not None else {})})
    assert r.status == expect, (r.status, r.body[:400])
    return r.json() if r.body else None


def use(app, headers, name, args=None, local=False):
    return rpc(app, headers, "tools/call", {"name": name, "arguments": args or {}}, local=local)["result"]


def names(app, headers, local=False):
    return [t["name"] for t in rpc(app, headers, "tools/list", local=local)["result"]["tools"]]


def rows(app, q, args=()):
    con = sqlite3.connect(app.a.db)
    try:
        return con.execute(q, args).fetchall()
    finally:
        con.close()


def withdraw(app, who, shas):
    con = sqlite3.connect(app.a.db)
    con.executemany("INSERT OR IGNORE INTO sharers VALUES (?,?,'2026-01-01')", ((s, mid(app, who)) for s in shas))
    con.commit()
    con.close()
    call(app, "POST", "/api/private", who, {"shas": list(shas), "private": True})
    settle(app)


# -- every kind of caller -------------------------------------------------------------------------------------

def test_every_kind_of_caller_of_mcp(real):
    owner, kid = {"Tailscale-User-Login": "owner@example.com"}, {"Tailscale-User-Login": "kid@example.com"}
    laptop, phone = {"X-Forwarded-For": "100.64.0.5"}, {"X-Forwarded-For": "100.64.0.9"}
    # no login, and a login with no member: refused, as every route
    rpc(real, laptop, "tools/list", expect=403)
    rpc(real, {"Tailscale-User-Login": "stranger@example.com", **laptop}, "tools/list", expect=403)
    assert rows(real, "SELECT count(*) FROM agent_bindings") == [(0,)]
    # this computer itself: the sysadmin, as before (every tool, writes, no binding)
    assert len(names(real, {}, local=True)) == 17 and "journal_since" not in names(real, {}, local=True)
    assert not use(real, {}, "create_album", {"title": "From the mini"}, local=True)["isError"]
    assert rows(real, "SELECT count(*) FROM agent_bindings") == [(0,)]
    # the kid's own agent, from their laptop: pending. It looks; every writing tool says to allow it first
    for _ in range(4):
        assert "search_photos" in names(real, {**kid, **laptop})
    for tool in READERS:
        args = {"id": real.live.cat.shas[0]} if tool in ("similar_photos",) else {"ids": [real.live.cat.shas[0]]} \
            if tool == "get_photos" else {"change_set": 1} if tool == "get_change_set" else {"album": 1} if tool == "get_album" else {}
        got = use(real, {**kid, **laptop}, tool, args)
        assert "has not allowed" not in json.dumps(got), tool
    for tool in WRITERS:
        got = use(real, {**kid, **laptop}, tool, ANY_ARGS[tool])
        assert got["isError"] and got["content"][0]["text"].startswith("Not yet: kid has not allowed this agent (on kid-macbook)"), tool
    (b,) = rows(real, "SELECT id, node, device, status FROM agent_bindings")
    assert b[1:] == ("node:nKID1", "kid-macbook", "pending")
    kid_id = real.request("GET", "/api/me", kid).json()["id"]
    items = real.request("GET", "/api/inbox", kid).json()["items"]
    assert [(i["kind"], i["title"], i["opens"]) for i in items] == \
        [("binding", "An agent connected as you from kid-macbook. Allow it?", "/agents")], "once, not once per call"
    assert rows(real, "SELECT actor, acting_for, kind FROM journal WHERE subject=?", (f"binding:{b[0]}",)) == \
        [(None, kid_id, "binding.requested")]
    # allowing it: never from the agent's own device (whatever it sends), never by another member, never through /mcp
    no = real.request("POST", f"/api/agents/{b[0]}", {**kid, **laptop}, body={"allow": True})
    assert no.status == 403 and "another device" in no.json()["error"]
    assert real.request("POST", f"/api/agents/{b[0]}", {**kid, **laptop, "X-Demo-Device": "kid-iphone"},
                        body={"allow": True}).status == 403, "the demo's device header is not read without --demo"
    assert real.request("POST", f"/api/agents/{b[0]}", {**owner, **phone}, body={"allow": True}).status == 404
    assert real.request("POST", f"/api/agents/{b[0]}", {}, body={"allow": True}, local=True).status == 404
    assert not [n for n in names(real, {**kid, **laptop}) if re.search("bind|allow|approve|agent", n)]
    assert rows(real, "SELECT status FROM agent_bindings") == [("pending",)]
    # from the kid's phone: allowed. It writes as the kid, and the journal names the binding
    got = real.request("POST", f"/api/agents/{b[0]}", {**kid, **phone}, body={"allow": True}).json()["binding"]
    assert got["status"] == "approved" and real.request("GET", "/api/inbox", kid).json()["items"] == []
    made = use(real, {**kid, **laptop}, "create_album", {"title": "Kid's album"})
    assert not made["isError"]
    sid = made["structuredContent"]["album"]
    assert rows(real, "SELECT owner FROM change_sets WHERE id=?", (sid,)) == [(kid_id,)]
    (entry,) = rows(real, "SELECT actor, acting_for, data FROM journal WHERE subject=?", (f"album:{sid}",))
    assert entry[:2] == (kid_id, None) and json.loads(entry[2])["binding"] == b[0]
    # a second device is a second agent, pending until allowed
    assert use(real, {**kid, **phone}, "create_album", {"title": "x"})["isError"]
    assert rows(real, "SELECT device, status FROM agent_bindings ORDER BY id") == [("kid-macbook", "approved"), ("kid-iphone", "pending")]
    # turned off (from anywhere, its own device too): every call refused, reads as well
    assert real.request("POST", f"/api/agents/{b[0]}", {**kid, **laptop}, body={"allow": False}).json()["binding"]["status"] == "revoked"
    off = rpc(real, {**kid, **laptop}, "tools/list", expect=403)
    assert "turned off their agent on kid-macbook" in off["error"]["message"]
    rpc(real, {**kid, **laptop}, "initialize", {"protocolVersion": "2025-06-18"}, expect=403)
    assert rows(real, "SELECT kind FROM journal WHERE subject=? ORDER BY id", (f"binding:{b[0]}",)) == \
        [("binding.requested",), ("binding.approved",), ("binding.revoked",)]
    # the owner's laptop is the owner's own agent like any other: pending, no task tools
    assert "submit_task" not in names(real, {**owner, "X-Forwarded-For": "100.64.0.7"})
    assert rows(real, "SELECT device FROM agent_bindings WHERE node='addr:100.64.0.7'") == [("100.64.0.7",)], \
        "no name from whois: the address is the device"


def test_the_estates_agent_with_its_token_is_as_before(app):
    con = sqlite3.connect(app.a.db)
    tid = con.execute("INSERT INTO agent_tasks(prompt, status, created_by, created_at, acting_for) VALUES"
                      " ('look', 'running', '', '2026-01-01', ?)", (mid(app, BEN),)).lastrowid
    con.commit()
    con.close()
    h = {"X-Estate-Agent": app.tasks.mint(tid)}
    listed = names(app, h, local=True)
    assert len(listed) == 14 and "journal_since" not in listed and "ask_member" in listed
    made = use(app, h, "create_album", {"title": "By the estate's agent"}, local=True)["structuredContent"]
    assert rows(app, "SELECT actor, acting_for, data FROM journal WHERE subject=?", (f"album:{made['album']}",)) == \
        [(app.agent, mid(app, BEN), json.dumps({"title": "By the estate's agent", "op": "view", "kind": "list", "added": 0},
                                               separators=(",", ":")))]
    assert rows(app, "SELECT count(*) FROM agent_bindings") == [(0,)]


def test_no_task_tools_and_its_own_instructions(app):
    bind(app, BEN)
    listed = names(app, BEN)
    assert "journal_since" in listed and not [n for n in listed if n.endswith("_task")]
    for name, args in (("submit_task", {"request": "hide the blurry ones"}), ("get_task", {"task": 1}), ("cancel_task", {"task": 1})):
        got = use(app, BEN, name, args)
        assert got["isError"] and "not for a member's own agent" in got["content"][0]["text"], name
    assert rows(app, "SELECT count(*) FROM agent_tasks") == [(0,)]
    text = rpc(app, BEN, "initialize", {"protocolVersion": "2025-06-18"})["result"]["instructions"]
    assert "journal_since" in text and "request_done" in text and "submit_task gives" not in text
    assert "submit_task gives" in rpc(app, {}, "initialize", {"protocolVersion": "2025-06-18"})["result"]["instructions"]
    got = use(app, BEN, "ask_member", {"photos": [app.live.cat.shas[3]], "text": "Who?", "follow_up": True})
    assert got["isError"] and "owner's subscription" in got["content"][0]["text"]


def test_the_demo_binds_too_and_says_which_device(app):
    """--demo: the device is X-Demo-Device when sent, else this computer; the page's listing says where it is open."""
    use(app, BEN, "search_photos", {"limit": 1})
    use(app, {**BEN, "X-Demo-Device": "Ben-Laptop"}, "search_photos", {"limit": 1})
    got = call(app, "GET", "/api/agents", BEN)
    assert [(b["device"], b["status"]) for b in got["bindings"]] == [("Ben-Laptop", "pending"), ("this computer", "pending")] or \
        sorted((b["device"], b["status"]) for b in got["bindings"]) == [("Ben-Laptop", "pending"), ("this computer", "pending")]
    assert got["here"] == {"node": "local", "device": "this computer"} and got["url"] == "http://127.0.0.1:8767/mcp" and got["demo"]
    fwd = call(app, "GET", "/api/agents", {**BEN, "X-Forwarded-Host": "mini.example.ts.net:8768", "X-Forwarded-Proto": "https"})
    assert fwd["url"] == "https://mini.example.ts.net:8768/mcp"
    laptop = next(b for b in got["bindings"] if b["device"] == "Ben-Laptop")
    here = next(b for b in got["bindings"] if b["device"] == "this computer")
    call(app, "POST", f"/api/agents/{here['id']}", BEN, {"allow": True}, expect=403)        # its own device
    call(app, "POST", f"/api/agents/{laptop['id']}", BEN, {"allow": True})                  # from this computer: another one
    call(app, "POST", f"/api/agents/{laptop['id']}", CLEO, {"allow": False}, expect=404)
    call(app, "POST", f"/api/agents/{laptop['id']}", BEN, {"allow": "yes"}, expect=400)
    assert call(app, "GET", "/api/agents", CLEO)["bindings"] == []


# -- only its member's view ---------------------------------------------------------------------------------------

def test_another_members_private_photo_is_no_such_photo_in_every_tool_journal_since_included(app):
    x, y = app.live.cat.shas[60], app.live.cat.shas[61]
    withdraw(app, ADA, [x])
    call(app, "POST", "/api/favorite", ADA, {"sha": x, "fav": True})
    secret = call(app, "POST", "/api/albums", ADA, {"title": "Ada's secret album", "items": [{"sha": x}]}, expect=201)["id"]
    use(app, ADA, "list_albums")                       # (Ada's own agent, pending: it may look)
    call(app, "POST", "/api/stories", ADA, {"photos": [x], "text": "the receipt for Ben's present"}, expect=201)
    call(app, "POST", "/api/stories", ADA, {"photos": [y], "text": "the lake in 2004"}, expect=201)
    bind(app, BEN)
    got = use(app, BEN, "get_photos", {"ids": [x]})["structuredContent"]["photos"]
    assert got == [{"id": x, "error": "no such photo"}]
    assert use(app, BEN, "similar_photos", {"id": x})["content"][0]["text"] == "no photo has that id"
    assert x not in json.dumps(use(app, BEN, "search_photos", {"limit": 200}))
    assert "secret" not in json.dumps(use(app, BEN, "list_albums"))
    assert "no such album" in use(app, BEN, "get_album", {"album": secret})["content"][0]["text"]
    seen, cursor = [], 0
    while True:
        page = use(app, BEN, "journal_since", {"cursor": cursor, "limit": 7})["structuredContent"]
        seen += page["entries"]
        cursor = page["next_cursor"]
        if not page["more"]:
            break
    text = json.dumps(seen)
    assert x not in text and "secret" not in text and "receipt" not in text
    assert [e["kind"] for e in seen if e["kind"] == "story.told"] == ["story.told"], "the story about y, not the one about x"
    assert y in text
    mine = journal.for_member(sqlite3.connect(app.a.db), mid(app, ADA), 0, 500, refused=app.unseen,
                              readable=lambda sid: {"photos": []})
    assert any(e["subject"] == x for e in mine["entries"]), "Ada's own withdrawal is hers to know"


def test_journal_since_gives_each_kind_only_to_whom_it_is_for(app):
    """Every kind journal.py's docstring lists, appended as each member, and who is told of it: the docstring's table."""
    ada, ben, cleo = mid(app, ADA), mid(app, BEN), mid(app, CLEO)
    x, y = app.live.cat.shas[62], app.live.cat.shas[63]
    withdraw(app, ADA, [x])
    a_own = call(app, "POST", "/api/albums", ADA, {"title": "Ada's own"}, expect=201)["id"]
    a_series = call(app, "POST", "/api/albums", ADA, {"title": "Ada's series"}, expect=201)["id"]
    call(app, "POST", f"/api/albums/{a_series}/publish", ADA, {"on": True})
    b_own = call(app, "POST", "/api/albums", BEN, {"title": "Ben's own"}, expect=201)["id"]
    l_own = call(app, "POST", "/api/lenses", ADA, {"title": "Ada's filter"})["lens"]
    l_pub = call(app, "POST", "/api/lenses", ADA, {"title": "Ada's published filter"})["lens"]
    call(app, "POST", f"/api/lenses/{l_pub}/publish", ADA, {"on": True})
    on_y = call(app, "POST", "/api/stories", ADA, {"photos": [y], "text": "a story about y"}, expect=201)["id"]
    on_x = call(app, "POST", "/api/stories", ADA, {"photos": [x], "text": "a story about x"}, expect=201)["id"]
    span = sorted(r for r, in rows(app, "SELECT rowid FROM assets WHERE sha256 IN (?,?)", (x, y)))
    span = [span[0] - 1, span[1]]
    con = sqlite3.connect(app.a.db)
    start = con.execute("SELECT max(id) FROM journal").fetchone()[0]
    # (actor, acting_for, kind, subject, data, Ada told?, Ben told?)
    table = [
        (ada, None, "member.added", f"member:{ben}", {}, False, False),
        (None, None, "member.requested", f"member:{ben}", {}, False, False),
        (ada, None, "member.changed", f"member:{ben}", {}, False, False),
        (ada, None, "favorite.set", y, {"fav": True}, True, False),
        (ben, None, "favorite.set", y, {"fav": True}, False, True),
        (ben, None, "favorite.set", x, {"fav": True}, False, False),
        (app.agent, None, "favorite.set", f"album:{b_own}", {"fav": True, "member": ben}, False, True),
        (ada, None, "lens.subscribed", f"lens:{l_pub}", {}, True, False),
        (ben, None, "lens.subscribed", f"lens:{l_pub}", {}, False, True),
        (ben, None, "lens.unsubscribed", f"lens:{l_pub}", {}, False, True),
        (ada, None, "photo.shown", y, None, True, False),
        (ben, None, "photo.unshown", y, None, False, True),
        (ada, None, "lenses.migrated", "lenses", {}, False, False),
        (ada, None, "sharers.migrated", "sharers", {}, False, False),
        (ada, None, "faces.labeled", "people", {}, False, False),
        (ada, None, "person.changed", "person:1", {}, False, False),
        (ben, None, "task.submitted", "task:1", {}, False, False),
        (None, ben, "task.finished", "task:1", {}, False, False),
        (None, None, "ingest.finished", "source:iphone", {"new": 2, "assets": span}, True, True),
        (None, None, "ingest.finished", "source:old", {"new": 2}, False, False),
        (ada, None, "photo.purged", y, {}, False, False),
        (None, ada, "photo.held", x, {}, True, False),
        (ada, None, "photo.withdrawn", x, {}, True, False),
        (ada, None, "photo.release_set", "photos", {}, True, False),
        (None, ada, "photo.released", "photos", {}, True, False),
        (ben, None, "photo.released", "photos", {}, False, True),
        (ben, None, "photo.reviewed", "photos", {"photos": 3}, False, False),
        (ben, None, "tombstones.changed", "tombstones", {}, False, False),
        (None, ben, "series.update_requested", f"album:{b_own}", {"photos": [x, y]}, False, True),
        (None, ada, "series.update_requested", f"album:{a_own}", {"photos": [x]}, True, False),
        (ada, None, "question.asked", "question:1", {}, True, False),
        (app.agent, ben, "question.asked", "question:2", {}, False, True),
        (ben, None, "question.dismissed", "question:2", {}, False, True),
        (ada, None, "story.told", f"story:{on_y}", {}, True, True),
        (ada, None, "story.told", f"story:{on_x}", {}, True, False),
        (None, ada, "story.transcribed", f"story:{on_y}", {}, True, True),
        (ada, None, "inbox.dismissed", "inbox:1", {}, True, False),
        (None, ben, "binding.requested", "binding:1", {}, False, True),
        (ben, None, "binding.approved", "binding:1", {}, False, True),
        (ada, None, "binding.revoked", "binding:2", {}, True, False),
    ]
    album_kinds = ("album.created", "album.items", "album.decided", "album.applied", "album.reverted", "album.committed",
                   "album.reopened", "album.hidden", "album.viewed", "album.renamed", "album.query", "album.discarded",
                   "album.grows", "album.published", "album.unpublished")
    for k in album_kinds:
        table += [(ada, None, k, f"album:{a_own}", {"n": 1}, True, False),
                  (ada, None, k, f"album:{a_series}", {"n": 1}, True, k in journal.SERIES_KINDS),
                  (ben, None, k, f"album:{b_own}", {"n": 1}, False, True)]
    for k in journal.LENS_KINDS:
        table += [(ada, None, k, f"lens:{l_own}", {"title": "t"}, True, False),
                  (ada, None, k, f"lens:{l_pub}", {"title": "t"}, True, True)]
    with con:
        ids = [journal.append(con, actor, kind, subject, data, af) for actor, af, kind, subject, data, _, _ in table]
    # every kind the docstring lists has its rows here
    listed = set()
    for line in journal.__doc__.split("Kinds, with their subject")[1].splitlines():
        if m := re.match(r"  ([a-z_]+\.[a-z_]+(?:, [a-z_]+\.[a-z_]+)*)\s", line):
            listed |= {k.strip() for k in m[1].split(",")}
    assert listed == {t[2] for t in table}, listed ^ {t[2] for t in table}
    app.loaded_through = app.journal_end()           # (the served catalog was read after the ingests: they are returned)
    told = {}
    for who in (ada, ben, cleo):
        got = app.journal_for(who, start, 500)
        assert not got["more"]
        told[who] = {e["id"]: e["kind"] for e in got["entries"]}
    for i, (actor, af, kind, subject, data, to_ada, to_ben) in zip(ids, table):
        assert (i in told[ada]) == to_ada, ("Ada", kind, subject, actor, af)
        assert (i in told[ben]) == to_ben, ("Ben", kind, subject, actor, af)
    # what the data says: an ingest's photos are those each may be shown; another's series, no data
    ben_view = {(e["kind"], e["subject"]): e for e in app.journal_for(ben, start, 500)["entries"]}
    ingest = ben_view[("ingest.finished", "source:iphone")]["data"]
    assert y in ingest["photos"] and x not in ingest["photos"] and set(ingest) <= {"photos", "new", "more"}
    assert x in next(e for e in app.journal_for(ada, start, 500)["entries"] if e["kind"] == "ingest.finished")["data"]["photos"]
    assert ben_view[("series.update_requested", f"album:{b_own}")]["data"] == {"photos": [y], "waiting": False}
    assert ben_view[("album.items", f"album:{a_series}")]["data"] is None
    assert ben_view[("story.told", f"story:{on_y}")]["data"]["photos"] == [y]
    assert ben_view[("question.asked", "question:2")]["by"] == "The estate's agent" and ben_view[("question.asked", "question:2")]["for"] == "Ben"
    # cleo, who did nothing: the shared things only
    assert set(told[cleo].values()) <= {"ingest.finished", "story.told", "story.transcribed", *journal.SERIES_KINDS, *journal.LENS_KINDS}


def test_cursors_page_oldest_first_and_bounded(app, monkeypatch):
    con = sqlite3.connect(app.a.db)
    start = con.execute("SELECT max(id) FROM journal").fetchone()[0]
    ben = mid(app, BEN)
    with con:
        for i in range(9):
            journal.append(con, ben if i % 3 else mid(app, ADA), "inbox.dismissed", f"inbox:{i}", {})
    whole = app.journal_for(ben, start, 500)["entries"]
    assert [e["subject"] for e in whole] == [f"inbox:{i}" for i in range(9) if i % 3]
    got, cursor = [], start
    while True:
        page = app.journal_for(ben, cursor, 2)
        assert len(page["entries"]) <= 2
        got += page["entries"]
        assert page["next_cursor"] >= cursor
        cursor = page["next_cursor"]
        if not page["more"]:
            break
    assert got == whole, "no entry twice, none missed, oldest first"
    monkeypatch.setattr(journal, "SCAN", 2)          # a call looks at so many at most, and says there is more
    page = app.journal_for(ben, start, 500)
    assert page["more"] and page["next_cursor"] == start + 2 and len(page["entries"]) == 1
    # an ingest the served catalog was not read after yet: the page stops before it, and says why
    with con:
        late = journal.append(con, None, "ingest.finished", "source:iphone", {"assets": [0, 3]})
    monkeypatch.setattr(journal, "SCAN", 5000)
    app.loaded_through = late - 1
    page = app.journal_for(ben, late - 1, 500)
    assert page["entries"] == [] and page["more"] and page["next_cursor"] == late - 1 and "waiting" in page
    app.loaded_through = late
    assert [e["kind"] for e in app.journal_for(ben, late - 1, 500)["entries"]] == ["ingest.finished"]


# -- a series kept up to date by its member's own agent -----------------------------------------------------------

def test_a_series_update_from_the_listener_to_the_agent_and_closed(app):
    seed = app.live.cat.shas[10]
    made = call(app, "POST", "/api/albums", BEN, {"title": "Like this one", "grows_from": {"like": seed, "min_score": 0.6}}, expect=201)
    sid = made["id"]
    held = [s for s, in rows(app, "SELECT sha256 FROM change_items WHERE set_id=? ORDER BY position", (sid,))]
    assert 4 <= len(held) < 60, len(held)
    out = held[-3:]                                   # taken out of the series: "new" again when an ingest brings them
    call(app, "POST", f"/api/albums/{sid}/items", BEN, {"remove": out})
    span = sorted(r for r, in rows(app, f"SELECT rowid FROM assets WHERE sha256 IN ({','.join('?' * len(out))})", out))
    con = sqlite3.connect(app.a.db)
    with con:
        entry = journal.append(con, None, "ingest.finished", "source:iphone", {"new": 3, "assets": [span[0] - 1, span[-1]]})
    con.close()
    app.loaded_through = entry                         # the served catalog was read after it
    app.listen_once()
    items = [i for i in call(app, "GET", "/api/inbox", BEN)["items"] if i["kind"] == "series_update"]
    assert len(items) == 1 and set(out) <= {r["sha"] for r in call(app, "GET", f"/api/albums/{sid}/candidates", BEN)["results"]}
    # Ben's own agent connects, is allowed, and catches up
    bind(app, BEN, "ben-laptop")
    entries, cursor = [], 0
    while True:
        page = use(app, {**BEN, "X-Demo-Device": "ben-laptop"}, "journal_since", {"cursor": cursor})["structuredContent"]
        entries += page["entries"]
        cursor = page["next_cursor"]
        if not page["more"]:
            break
    (req,) = [e for e in entries if e["kind"] == "series.update_requested"]
    assert req["subject"] == f"album:{sid}" and set(out) <= set(req["data"]["photos"]) and req["data"]["waiting"]
    # it adds what belongs, and says it is done: the item closes, nothing waits any more
    h = {**BEN, "X-Demo-Device": "ben-laptop"}
    got = use(app, h, "amend_album", {"album": sid, "add_ids": out[:2], "reason": "the same lake", "request_done": True})
    assert not got["isError"] and got["structuredContent"]["added"] == 2 and got["structuredContent"]["request_done"]
    assert [i for i in call(app, "GET", "/api/inbox", BEN)["items"] if i["kind"] == "series_update"] == []
    assert call(app, "GET", f"/api/albums/{sid}/candidates", BEN)["results"] == []
    assert set(out[:2]) <= {s for s, in rows(app, "SELECT sha256 FROM change_items WHERE set_id=?", (sid,))}
    assert out[2] not in {s for s, in rows(app, "SELECT sha256 FROM change_items WHERE set_id=?", (sid,))}, "the rest stay out"
    again = use(app, h, "journal_since", {"cursor": req["id"] - 1, "limit": 1})["structuredContent"]["entries"]
    assert again[0]["data"]["waiting"] is False
    (added,) = [json.loads(d) for d, in rows(app, "SELECT data FROM journal WHERE kind='album.items' AND subject=? ORDER BY id DESC LIMIT 1",
                                             (f"album:{sid}",))]
    assert added["binding"] == bind(app, BEN, "ben-laptop")["id"]
    # done with nothing to add, or on someone else's series: refused for another's, harmless for one with nothing waiting
    assert not use(app, h, "amend_album", {"album": sid, "request_done": True})["isError"]
    ada_album = call(app, "POST", "/api/albums", ADA, {"title": "Ada's"}, expect=201)["id"]
    assert use(app, h, "amend_album", {"album": ada_album, "request_done": True})["isError"]
