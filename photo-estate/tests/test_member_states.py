"""Who is let in (members.py, PhotoApp.who and load_members, members.bind and decide_binding, a run's token), on a
scratch catalog and through the real app: each way the model of it (specs/Member.tla, checked by TLC) found a member
or their agent could end up other than as last said, or a request let through on what the catalog no longer says,
replayed on the real code; and, last, what the model shows the code does now where the owner has a question to
decide (those tests say what is, not what must be).

Every one of the first kind needs bad timing. The races land the other request on a `Gap` connection, as
tests/test_inbox_states.py does: a request reads what it decides by with no lock held, because Python begins its
transaction only at the first write, and the other request runs just before the first statement that writes or takes
the write lock, on a connection of its own. The roster (PhotoApp.roster) is a copy in memory: its race is replayed
with a load_members held between its read and its assignment while another request runs on a thread of its own.

  uv run pytest tests/test_member_states.py
"""
from __future__ import annotations

import http.client
import json
import shutil
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import embed  # noqa: E402
import estate  # noqa: E402
import members  # noqa: E402
import photo_app  # noqa: E402
from core import schema  # noqa: E402
from photo_app import PhotoApp  # noqa: E402
from test_agent import task, tool  # noqa: E402
from test_inbox_states import Gap, gap, opened  # noqa: E402
from test_multiplayer import ADA, BEN, CLEO, bind, call, mid, settle  # noqa: E402

A, D, B = 1, 2, 3                      # Ada and Dee, the sysadmins; Ben, who asks in


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "estate.db")
    con = estate.connect(path)
    schema.ensure(con, *schema.CATALOG_AREAS)
    for name in ("Ada", "Dee"):
        members.add(con, name, name.lower(), role="sysadmin", status="active")
    members.request(con, "ben@example.com", "Ben")
    con.close()
    return path


@pytest.fixture
def con(db):
    con = estate.connect(db)
    yield con
    con.close()


@pytest.fixture
def app():
    app = PhotoApp(embed.parser().parse_args(["serve", "--demo", "--no-pregenerate"]))   # no start(): no threads
    yield app
    app.tasks.stop()


def row(con, mid_: int) -> tuple:
    return con.execute("SELECT name, role, status, joined_at FROM members WHERE id=?", (mid_,)).fetchone()


def change(**fields):
    """members.change of Ben by Ada, as another request of the app's (fn(con))."""
    return lambda c: members.change(c, B, A, **fields)


# -- the normal path ---------------------------------------------------------------------------------------------

def test_members_change_one_at_a_time_as_asked(db, con):
    """What must not change: Ben asks in (twice: answered with himself), is let in (joined_at), renamed, made a
    sysadmin and back, revoked and let in again (joined_at kept); a change that would leave no active sysadmin is
    refused; his agent is made pending, allowed from another device and turned off."""
    assert members.request(con, "ben@example.com", "Someone").id == B
    assert len(opened(con, "member_request")) == 2
    members.change(con, B, A, status="active")
    joined = row(con, B)[3]
    assert joined and opened(con, "member_request") == []
    members.change(con, B, D, name="Benjamin")
    members.change(con, B, A, role="sysadmin")
    members.change(con, B, A, role="member")
    members.change(con, B, A, status="revoked")
    members.change(con, B, A, status="active")
    assert row(con, B) == ("Benjamin", "member", "active", joined)
    members.change(con, D, A, role="member")
    with pytest.raises(ValueError, match="active sysadmin"):
        members.change(con, A, A, status="revoked")
    b = members.bind(con, B, "node:laptop", "laptop")
    assert members.decide_binding(con, B, b["id"], True, "node:phone")["status"] == "approved"
    assert members.decide_binding(con, B, b["id"], False, "node:laptop")["status"] == "revoked"
    assert con.execute("SELECT status FROM agent_bindings").fetchall() == [("revoked",)]


def test_a_revoke_is_refused_at_once_in_the_app(app):
    """What must not change: once Ada's revoke has answered, Ben's next request is refused (403, /api/me too), his own
    agent's as well, and a page is the request-access page saying he is not let in; let in again, he is in again."""
    call(app, "GET", "/api/search?limit=1", BEN)
    ben = mid(app, BEN)
    call(app, "POST", f"/api/members/{ben}", ADA, {"status": "revoked"})
    call(app, "GET", "/api/me", BEN, expect=403)
    call(app, "GET", "/api/search?limit=1", BEN, expect=403)
    page = app.request("GET", "/", {**BEN, "Accept": "text/html"})
    assert page.status == 403 and b"You are not let in" in page.body
    call(app, "POST", "/mcp", BEN, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, expect=403)
    call(app, "POST", f"/api/members/{ben}", ADA, {"status": "active"})
    call(app, "GET", "/api/search?limit=1", BEN)
    settle(app)


# -- races: another request in the gap ---------------------------------------------------------------------------

@pytest.mark.parametrize("first, then, kept", [
    ({"role": "sysadmin"}, {"name": "Benjamin"}, ("Benjamin", "sysadmin", "pending")),   # TLC's trace
    ({"name": "Benjamin"}, {"status": "revoked"}, ("Benjamin", "member", "revoked")),   # a revoke undone by a rename
    ({"status": "active"}, {"role": "sysadmin"}, ("Ben", "sysadmin", "active")),        # a role change undone
])
def test_a_change_writes_only_the_field_it_was_asked_to(db, con, first, then, kept):
    """OnlyWhatWasAsked: two changes of Ben at once (two sysadmins, or one with two tabs). The first reads him; before
    it writes, the second changes him. The first then writes every field from what it read: the second's change is
    undone, and the journal says only the first changed anything. With a revoke, Ben is let in again, and the page
    that revoked him says he is revoked."""
    if "revoked" in then.values():
        members.change(con, B, A, status="active")
    members.change(gap(db, change(**then)), B, D, **first)
    assert row(con, B)[:3] == kept, "a change writes only the fields it was asked to change"


def test_joined_at_stays_once_let_in(db, con):
    """JoinedWhenFirstLetIn: Dee renames Ben, who waits to be let in; her change reads him pending. Before it writes,
    Ada lets him in (active, joined_at, his member_request items done). Dee's change then writes pending back over
    active, and joined_at back to none: he is waiting again, with no item for anyone to let him in by."""
    members.change(gap(db, change(status="active")), B, D, name="Benjamin")
    name, _, status, joined = row(con, B)
    assert (name, status) == ("Benjamin", "active") and joined, "joined_at is set once a member has been let in, and stays"


def test_a_binding_is_what_the_last_decision_answered(db, con):
    """BindingAsLastAnswered: Ben's agent on his laptop asks (pending). He allows it from his phone and turns it off
    from the laptop at once: both read pending. The turn-off writes revoked; he turns it off again (another tab), and
    that one reads revoked, so it writes nothing (only its inbox item, which waits for the lock). The allow, which
    read pending, writes approved. The second turn-off then answers revoked, and the agent is allowed."""
    members.change(con, B, A, status="active")
    bid = members.bind(con, B, "node:laptop", "laptop")["id"]
    reached, go, answers = threading.Event(), threading.Event(), {}

    def second_off():
        c = sqlite3.connect(db, factory=Gap, timeout=60)
        c.during = lambda: (reached.set(), go.wait(10))
        try:
            answers["off"] = members.decide_binding(c, B, bid, False, "node:laptop")["status"]
        finally:
            c.close()
    t = threading.Thread(target=second_off)

    def in_the_gap(c):
        members.decide_binding(c, B, bid, False, "node:laptop")      # the first turn-off
        t.start()
        assert reached.wait(10)
    answers["allow"] = members.decide_binding(gap(db, in_the_gap), B, bid, True, "node:phone")["status"]
    go.set()
    t.join(10)
    (status,), = con.execute("SELECT status FROM agent_bindings WHERE id=?", (bid,)).fetchall()
    assert answers == {"allow": "approved", "off": "revoked"}
    assert status == answers["off"], "the binding is what the last decision answered"


def test_asking_in_twice_at_once_is_answered_both_times(monkeypatch):
    """AskedOnceAnswered: two POST /api/request-access from one login at once (a double tap; the form posted from
    two tabs), through the app's HTTP server. Both look the login up and find none; the second INSERTs first, and the
    first's INSERT of the login then raises sqlite3.IntegrityError (member_logins_no_replace), which nothing catches:
    the server drops the connection with no answer (behind `tailscale serve`, a 502)."""
    app = PhotoApp(embed.parser().parse_args(["serve", "--demo", "--no-pregenerate"]))
    srv = app.make_server("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port, zed = srv.server_address[1], {"X-Demo-Member": "zed@example.com", "Content-Type": "application/json"}

    def ask() -> tuple[int, dict]:
        h = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        try:
            h.request("POST", "/api/request-access", body=json.dumps({"name": "Zed"}), headers=zed)
            r = h.getresponse()
            return r.status, json.loads(r.read())
        finally:
            h.close()
    real, armed = photo_app.connect, []

    def connect(path):     # the first request's own connection is a Gap, and the second runs in it
        if armed:
            return real(path)
        armed.append(True)
        c = sqlite3.connect(path, factory=Gap, timeout=60)
        c.during = lambda: armed.append(ask())
        return c
    monkeypatch.setattr(photo_app, "connect", connect)
    try:
        try:
            first = ask()
        except (http.client.RemoteDisconnected, ConnectionError) as e:
            first = (None, {"error": type(e).__name__})
        assert armed[1] == (200, {"status": "pending"})
        assert first == (200, {"status": "pending"}), "a login that is already a member's is answered with that member"
        con = sqlite3.connect(app.a.db)
        assert con.execute("SELECT count(*) FROM member_logins WHERE login='zed@example.com'").fetchone() == (1,)
        con.close()
    finally:
        srv.shutdown()
        srv.server_close()
        app.tasks.stop()


def race_the_roster(app, monkeypatch, first, then) -> None:
    """`first()` (a request that ends by reading the roster again: load_members) reads it, and before it puts what it
    read in place, `then()` runs to its end on a thread of its own (a second request of the app's). With members_lock,
    then's own load waits for first's to be done, and the test goes on after two seconds."""
    real, fired, others = members.by_login, [], []

    def by_login(con):
        got = real(con)
        if not fired:
            fired.append(True)
            t = threading.Thread(target=then)
            others.append(t)
            t.start()
            t.join(2)
        return got
    monkeypatch.setattr(members, "by_login", by_login)
    first()
    for t in others:
        t.join(20)
    monkeypatch.setattr(members, "by_login", real)
    settle(app)


def test_a_sysadmin_made_a_member_is_one_at_once(app, monkeypatch):
    """RosterAsCatalog, LetAsTheCatalogSays (TLC's trace): Ben is a second sysadmin. Ada renames Cleo; her request
    reads the roster again; before it puts it in place, Ada makes herself a member (another tab), and that request
    reads the roster and puts it in place. The first then puts its copy over it: the app keeps Ada as a sysadmin, and
    lets her change members, until a member next changes or someone asks in (or the app restarts)."""
    ada, cleo = mid(app, ADA), mid(app, CLEO)
    call(app, "POST", f"/api/members/{mid(app, BEN)}", ADA, {"role": "sysadmin"})
    settle(app)
    race_the_roster(app, monkeypatch, lambda: call(app, "POST", f"/api/members/{cleo}", ADA, {"name": "Cleo B."}),
                    lambda: call(app, "POST", f"/api/members/{ada}", ADA, {"role": "member"}))
    assert app.roster["ada"].role == "member", "once every change is read again, the roster is what the catalog says"
    call(app, "POST", f"/api/members/{cleo}", ADA, {"name": "Cleo"}, expect=403)


def test_a_member_revoked_while_someone_asks_in_is_refused(app, monkeypatch):
    """RosterAsCatalog, LetAsTheCatalogSays: a newcomer asks in (POST /api/request-access, open to anyone on the
    tailnet), which reads the roster again; before it puts it in place, Ada revokes Ben. The newcomer's copy goes in
    last: Ben is let through, and /api/me tells him he is active, until a member next changes."""
    zed = {"X-Demo-Member": "zed@example.com"}
    race_the_roster(app, monkeypatch, lambda: call(app, "POST", "/api/request-access", zed, {"name": "Zed"}),
                    lambda: call(app, "POST", f"/api/members/{mid(app, BEN)}", ADA, {"status": "revoked"}))
    assert app.roster["ben"].status == "revoked", "once every change is read again, the roster is what the catalog says"
    call(app, "GET", "/api/me", BEN, expect=403)
    call(app, "GET", "/api/search?limit=1", BEN, expect=403)


# -- what is, where the owner decides (specs/Member.tla's invariants not in Member.cfg) -------------------------

@pytest.fixture
def real(tmp_path):
    """The demo catalog served as a real one (no --demo): Tailscale's headers, and a whois the test gives."""
    shutil.copyfile(ROOT / "demo" / "estate.db", tmp_path / "estate.db")
    app = PhotoApp(embed.parser().parse_args(["--db", str(tmp_path / "estate.db"), "serve", "--store", str(ROOT / "demo" / "store"),
                                              "--no-pregenerate", "--upload-user", "owner@example.com",
                                              "--upload-user", "kid@example.com"]))
    yield app
    app.tasks.stop()


@pytest.mark.parametrize("first, then", [(None, "node:nKID1"), ("node:nKID1", None)], ids=["made-blind", "allowed-blind"])
def test_an_agent_allows_itself_from_its_own_device_when_whois_answers_for_one_request_only(real, first, then):
    """OwnDeviceNeverAllows, what is. A HOLE, left for the owner: the device rule compares two strings, and one laptop is
    node:<StableID> when `tailscale whois` answers for its address and addr:<ip> when it does not (no CLI, a timeout,
    an error; a failure is kept a minute, WHOIS_FOR / 10). The kid's agent, which holds the kid's login on the laptop,
    calls /mcp from it while whois does not answer for it (or does), and then POSTs /api/agents/<id> {allow: true}
    from the same laptop when whois does (or does not): the two differ, so it is allowed, from its own device."""
    kid, laptop = {"Tailscale-User-Login": "kid@example.com"}, {"X-Forwarded-For": "100.64.0.5"}
    names = {"100.64.0.5": ("node:nKID1", "kid-macbook")}
    real.whois = (lambda addr: names.get(addr)) if first else (lambda addr: None)
    real.request("POST", "/mcp", {**kid, **laptop}, body={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    con = sqlite3.connect(real.a.db)
    (bid, node), = con.execute("SELECT id, node FROM agent_bindings").fetchall()
    con.close()
    assert node == (first or "addr:100.64.0.5")
    real.whois, real.whois_seen = (lambda addr: names.get(addr)) if then else (lambda addr: None), {}
    got = real.request("POST", f"/api/agents/{bid}", {**kid, **laptop}, body={"allow": True})
    assert (got.status, got.json()["binding"]["status"]) == (200, "approved")   # what is: allowed from its own device


def test_a_revoked_members_agent_is_refused_and_back_with_them(app):
    """AllowedSinceLetIn, what is: Ben's agent, allowed. Ada revokes Ben: its next call is refused at once (the roster
    says revoked; the binding stays approved). Ada lets Ben in again: his agent writes as him again, with no word from
    him. A revoked binding stays revoked on its next call (bind writes only seen_at and device), and a pending one
    may only look (tests/test_personal_agents.py)."""
    b = bind(app, BEN, "Ben-Laptop")
    h = {**BEN, "X-Demo-Device": "Ben-Laptop"}
    ben = mid(app, BEN)
    call(app, "POST", f"/api/members/{ben}", ADA, {"status": "revoked"})
    call(app, "POST", "/mcp", h, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, expect=403)
    con = sqlite3.connect(app.a.db)
    assert con.execute("SELECT status FROM agent_bindings WHERE id=?", (b["id"],)).fetchone() == ("approved",)
    con.close()
    call(app, "POST", f"/api/members/{ben}", ADA, {"status": "active"})
    got = call(app, "POST", "/mcp", h, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                        "params": {"name": "create_album", "arguments": {"title": "After"}}})
    assert got["result"]["isError"] is False
    settle(app)


def test_a_run_acting_for_a_revoked_member_goes_on(app):
    """RunsOnlyForActive, what is: a run of the estate's agent acting for Ben is under way (its token). Ada revokes
    Ben. The run's calls still act for him until it ends (its timeout is an hour): it searches what Ben sees and makes
    albums that are his. A task he queued before is started after (Tasks.worker reads no member)."""
    h = {"X-Estate-Agent": app.tasks.mint(task(app, mid(app, BEN)))}
    call(app, "POST", f"/api/members/{mid(app, BEN)}", ADA, {"status": "revoked"})
    made = tool(app, h, "create_album", {"title": "For Ben, revoked"})
    con = sqlite3.connect(app.a.db)
    assert con.execute("SELECT owner FROM change_sets WHERE id=?", (made["album"],)).fetchone() == (mid(app, BEN),)
    con.close()
    settle(app)


def test_a_member_let_in_may_be_set_waiting_again(db, con):
    """PendingOnlyBeforeLetIn, what is: any status may follow any other (the raw API; members.html offers let in, turn
    away, turn off, and let in again). Ben let in, then set to pending: he waits again, with no member_request item for
    anyone (/members lists him first, with Let in), and his joined_at is kept. Revoked to pending is the same."""
    members.change(con, B, A, status="active")
    members.change(con, B, A, status="pending")
    assert row(con, B)[2] == "pending" and row(con, B)[3] and opened(con, "member_request") == []
    members.change(con, B, A, status="revoked")
    members.change(con, B, A, status="pending")
    assert row(con, B)[2] == "pending"
