"""The estate's agent as a member (multiplayer phase 7: tasks.py, estate_mcp.py), on the demo library in process:
a run's token (honoured from this computer only, only while the run lasts, only for its member; X-Estate-Task no
longer); and each kind of caller of the MCP tools, what it sees and what it may write: the agent acting for a
member, acting for no one (the family archivist), and a member's own login.

  uv run pytest tests/test_agent.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import embed  # noqa: E402
import members  # noqa: E402
from photo_app import PhotoApp  # noqa: E402
from test_multiplayer import ADA, BEN, CLEO, call, mid, seen, settle  # noqa: E402


@pytest.fixture
def app():
    a = embed.parser().parse_args(["serve", "--demo", "--no-pregenerate"])
    app = PhotoApp(a)             # start() is not called: no model, no threads; its own scratch catalog
    yield app
    app.tasks.stop()


def task(app, acting_for, by=None) -> int:
    """A task row as a run sees it (status running: the worker leaves it be)."""
    con = sqlite3.connect(app.a.db)
    tid = con.execute("INSERT INTO agent_tasks(prompt, status, created_by, created_at, acting_for, started_by)"
                      " VALUES ('look', 'running', '', '2026-01-01', ?, ?)", (acting_for, by)).lastrowid
    con.commit()
    con.close()
    return tid


def mcp(app, headers, method="tools/call", name=None, args=None, local=True, expect=200):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, **({"params": {"name": name, "arguments": args or {}}} if name else {})}
    r = app.request("POST", "/mcp", headers=headers, body=body, local=local)
    assert r.status == expect, (r.status, r.body[:300])
    return r.json()


def tool(app, headers, name, args=None, local=True, error=False):
    got = mcp(app, headers, name=name, args=args, local=local)["result"]
    assert got["isError"] == error, got
    return got["content"][0]["text"] if error else got["structuredContent"]


def withdraw(app, who, shas):
    con = sqlite3.connect(app.a.db)
    con.executemany("INSERT OR IGNORE INTO sharers VALUES (?,?,'2026-01-01')", ((s, mid(app, who)) for s in shas))
    con.commit()
    con.close()
    call(app, "POST", "/api/private", who, {"shas": list(shas), "private": True})
    settle(app)


def journal(app, subject):
    con = sqlite3.connect(app.a.db)
    try:
        return con.execute("SELECT actor, acting_for, kind FROM journal WHERE subject=? ORDER BY id", (subject,)).fetchall()
    finally:
        con.close()


# -- the token ------------------------------------------------------------------------------------------

def test_a_runs_token_works_from_here_while_the_run_lasts_for_its_member_only(app):
    x = app.live.cat.shas[70]
    withdraw(app, BEN, [x])
    ben_task, cleo_task = task(app, mid(app, BEN)), task(app, mid(app, CLEO))
    ben, cleo = app.tasks.mint(ben_task), app.tasks.mint(cleo_task)
    assert ben != cleo and len(ben) > 40
    ids = lambda t: {p["id"] for p in tool(app, {"X-Estate-Agent": t}, "search_photos", {"limit": 200})["photos"]}   # noqa: E731
    assert x in ids(ben) and x not in ids(cleo), "each token is its own member's eyes"
    names = [t["name"] for t in mcp(app, {"X-Estate-Agent": ben}, "tools/list")["result"]["tools"]]
    assert "ask_member" in names and not [n for n in names if n.endswith("_task")], "a run has no task tools"
    # from another computer (through `tailscale serve`), the token is nothing
    assert mcp(app, {"X-Estate-Agent": ben, **BEN}, "tools/list", local=False, expect=403)["error"]["code"] == -32001
    # once the run has ended
    app.tasks.forget(ben)
    assert mcp(app, {"X-Estate-Agent": ben}, "tools/list", expect=403)["error"]["code"] == -32001
    assert mcp(app, {"X-Estate-Agent": "a guess"}, "tools/list", expect=403)
    # the task number a run carried before is refused, never taken for this computer's sysadmin
    assert mcp(app, {"X-Estate-Task": str(ben_task)}, "tools/list", expect=403)["error"]["code"] == -32001
    assert len(mcp(app, {}, "tools/list")["result"]["tools"]) == 17, "this computer with no token: the sysadmin"
    # a real run mints and forgets (tasks.Tasks.run)
    assert app.tasks.run_of(cleo).acting_for == mid(app, CLEO)
    app.tasks.forget(cleo)
    assert app.tasks.run_of(cleo) is None and app.tasks.runs == {}


def test_the_agent_is_a_member_made_once_and_never_a_person():
    import tempfile
    con = sqlite3.connect(Path(tempfile.mkdtemp()) / "estate.db")
    from core import schema
    schema.ensure(con, "members")
    a = members.agent(con)
    assert (a.kind, a.role, a.status, a.name) == ("agent", "member", "active", "The estate's agent")
    assert members.agent(con).id == a.id
    assert members.first_start(con, ["owner@example.com"])[0].role == "sysadmin", "the agent alone is no members yet"
    assert [m.name for m in members.family(con)] == ["owner"]
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("DELETE FROM members WHERE id=?", (a.id,))
    with pytest.raises(ValueError, match="not changed on /members"):
        members.change(con, a.id, None, role="sysadmin")          # nor made a sysadmin, nor revoked


# -- each kind of caller ----------------------------------------------------------------------------------

def test_the_agent_acting_for_ben_sees_and_writes_as_ben_and_applies_nothing(app):
    x = app.live.cat.shas[71]
    withdraw(app, BEN, [x])
    h = {"X-Estate-Agent": app.tasks.mint(task(app, mid(app, BEN)))}
    assert tool(app, h, "get_photos", {"ids": [x]})["photos"][0]["private"] == "withdrawn"
    made = tool(app, h, "create_album", {"title": "Ben's beach"})
    tool(app, h, "amend_album", {"album": made["album"], "add_ids": [x]})
    assert made["album"] in [a["id"] for a in call(app, "GET", "/api/albums", BEN)["sets"]]
    assert journal(app, f"album:{made['album']}") == [(app.agent, mid(app, BEN), "album.created"),
                                                       (app.agent, mid(app, BEN), "album.items")]
    # Ada's album is not Ben's to change, and there is no tool that applies a change set
    ada_album = call(app, "POST", "/api/albums", ADA, {"title": "Ada's"}, expect=201)["id"]
    assert "no such" in tool(app, h, "amend_album", {"album": ada_album, "add_ids": [x]}, error=True)
    hs = tool(app, h, "create_change_set", {"title": "Blurry"})
    tool(app, h, "amend_change_set", {"change_set": hs["change_set"], "add_ids": [app.live.cat.shas[72]]})
    names = [t["name"] for t in mcp(app, h, "tools/list")["result"]["tools"]]
    assert not [n for n in names if "apply" in n or "commit" in n]
    assert app.live.cat.shas[72] in seen(app, BEN), "proposed, not applied"


def test_the_archivist_acting_for_no_one_sees_what_all_may_and_makes_family_series(app):
    x, y = app.live.cat.shas[73], app.live.cat.shas[74]
    withdraw(app, BEN, [x])
    con = sqlite3.connect(app.a.db)
    con.executemany("INSERT OR IGNORE INTO sharers VALUES (?,?,'2026-01-01')", ((y, mid(app, c)) for c in (BEN, CLEO)))
    con.commit()
    con.close()
    withdraw(app, CLEO, [y])            # y: Cleo holds it private, Ben still shares it: the family sees it
    assert y in seen(app, ADA)
    h = {"X-Estate-Agent": app.tasks.mint(task(app, None, f"member:{mid(app, CLEO)}"))}
    ids = {p["id"] for p in tool(app, h, "search_photos", {"limit": 200})["photos"]}
    assert x not in ids and y not in ids, "only what is private to nobody: stricter than any member"
    assert tool(app, h, "get_photos", {"ids": [x, y]})["photos"] == [{"id": x, "error": "no such photo"},
                                                                    {"id": y, "error": "no such photo"}]
    seed = app.live.cat.shas[5]
    made = tool(app, h, "create_album", {"title": "Grandma over the years", "search": {"like": seed, "max": 5}, "grow": True})
    assert made["family_series"] and made["grows_from"] == {"like": seed, "max": 5} and made["photos"] > 0
    for who in (ADA, BEN, CLEO):        # published for the family, at once
        assert made["album"] in [a["id"] for a in call(app, "GET", "/api/albums", who)["sets"]]
    call(app, "POST", f"/api/albums/{made['album']}/items", BEN, {"items": [{"sha": seed}]}, expect=403)
    assert journal(app, f"album:{made['album']}")[:2] == [(app.agent, None, "album.created"), (app.agent, None, "album.published")]
    con = sqlite3.connect(app.a.db)
    assert con.execute("SELECT owner, maintained_by, published FROM lenses WHERE set_id=?", (made["album"],)).fetchone() == \
        (app.agent, app.agent, 1)
    con.close()
    # it may ask the family about a photo everyone sees, and no one about one that is private to anyone
    got = tool(app, h, "ask_member", {"photos": [seed], "text": "Who is this?", "group_key": "Grandma"})
    assert got["to"] == "the family" and got["status"] == "queued"
    assert "no such photo" in tool(app, h, "ask_member", {"photos": [y], "text": "?", "to": "Ben"}, error=True)
    assert [m["name"] for m in tool(app, h, "library_overview")["members"]] == ["Ada", "Ben", "Cleo"]
    # the run's task is for whoever started it, and the sysadmins
    tid = int(made["album"]) and app.tasks.runs[h["X-Estate-Agent"]].task
    assert call(app, "GET", f"/api/tasks/{tid}", CLEO)["id"] == tid and call(app, "GET", f"/api/tasks/{tid}", ADA)["id"] == tid
    call(app, "GET", f"/api/tasks/{tid}", BEN, expect=404)


def test_a_members_own_login_asks_as_themselves(app):
    x = app.live.cat.shas[75]
    got = tool(app, BEN, "ask_member", {"photos": [x], "text": "Is this the lake house?", "to": "Cleo"}, local=False)
    assert got["to"] == "Cleo"
    con = sqlite3.connect(app.a.db)
    assert con.execute("SELECT asked_by, acting_for FROM questions").fetchall() == [(mid(app, BEN), None)]
    con.close()
    assert [i["title"] for i in call(app, "GET", "/api/inbox", CLEO)["items"]] == ["Is this the lake house?"]
    q = call(app, "GET", "/api/questions", CLEO)["items"][0]["questions"][0]
    assert q["asked_by"]["name"] == "Ben" and q["for"] is None
    call(app, "POST", f"/api/questions/{q['id']}/answer", CLEO, {"text": "Yes, in 2004"}, expect=201)
    photo = tool(app, BEN, "get_photos", {"ids": [x]}, local=False)["photos"][0]
    assert photo["stories"] == [{"told_by": "Cleo", "told_at": photo["stories"][0]["told_at"], "text": "Yes, in 2004",
                                 "question": "Is this the lake house?", "photos": [x]}]
    # a family question a member files is not dealt to themselves
    tool(app, BEN, "ask_member", {"photos": [x], "text": "Who built it?"}, local=False)
    assert [i["title"] for i in call(app, "GET", "/api/inbox", BEN)["items"]] == []
    assert "Who built it?" in [i["title"] for i in call(app, "GET", "/api/inbox", ADA)["items"]]


def test_any_member_starts_a_run_for_themselves_or_for_the_family(tmp_path):
    import tasks
    t = tasks.Tasks(str(tmp_path / "estate.db"), "http://127.0.0.1:1/mcp", command=sys.executable, log=lambda *a: None)
    t.stop()                       # (no run is wanted here: only what submit records)
    mine, _ = t.submit("hide the blurry ones", "ben", acting_for=5)
    fam, _ = t.submit("a series of Grandma over the years", "ben", acting_for=5, family=True)
    heard, _ = t.submit("an answer came", "", acting_for=6, started_by="journal:9/story:2")
    got = {x["id"]: (x["acting_for"], x["started_by"]) for x in (t.get(i, None) for i in (mine["id"], fam["id"], heard["id"]))}
    assert got == {mine["id"]: (5, "member:5"), fam["id"]: (None, "member:5"), heard["id"]: (6, "journal:9/story:2")}
    assert t.brief(mine["id"]) == tasks.BRIEF and t.brief(fam["id"]) == t.brief(heard["id"]) == tasks.WORK_BRIEF
    assert t.get(fam["id"], None, 5) and t.get(fam["id"], None, 7) is None, "the family's run: whoever started it"
    con = sqlite3.connect(tmp_path / "estate.db")
    assert con.execute("SELECT actor, acting_for, data FROM journal WHERE kind='task.submitted' ORDER BY id").fetchall() == [
        (5, None, '{"prompt":"hide the blurry ones"}'),
        (5, None, '{"prompt":"a series of Grandma over the years","started_by":"member:5"}'),
        (None, 6, '{"prompt":"an answer came","started_by":"journal:9/story:2"}')]
    con.close()


def test_a_list_album_is_given_a_search_to_grow_from_by_its_owner(app):
    x = app.live.cat.shas[76]
    sid = call(app, "POST", "/api/albums", BEN, {"title": "Kept", "items": [{"sha": x}]}, expect=201)["id"]
    got = call(app, "POST", f"/api/albums/{sid}/grow", BEN, {"grows_from": {"like": x}})
    assert got["grows_from"] == {"like": x} and got["n"] == 1, "it keeps what it holds"
    call(app, "POST", f"/api/albums/{sid}/grow", CLEO, {"grows_from": None}, expect=404)
    call(app, "POST", f"/api/albums/{sid}/grow", BEN, {"grows_from": {"people": ["Nobody Atall"]}}, expect=400)
    live = call(app, "POST", "/api/albums", BEN, {"title": "Live", "query": {"like": x}}, expect=201)["id"]
    call(app, "POST", f"/api/albums/{live}/grow", BEN, {"grows_from": {"like": x}}, expect=409)
    assert call(app, "POST", f"/api/albums/{sid}/grow", BEN, {"grows_from": None})["grows_from"] is None
    assert [k for _, _, k in journal(app, f"album:{sid}")] == ["album.created", "album.grows", "album.grows"]
    assert call(app, "GET", f"/api/albums/{sid}/candidates", BEN)["results"] == []
    call(app, "GET", f"/api/albums/{sid}/candidates", CLEO, expect=404)
