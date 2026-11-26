"""Who is let in (members.py, PhotoApp.who and load_members, members.bind and decide_binding, a run's token), on a
scratch catalog and through the real app: each way the model of it (specs/Member.tla, checked by TLC) found a member
or their agent could end up other than as last said, or a request let through on what the catalog no longer says,
replayed on the real code; and, last, what the model shows the code does now where the owner has a question to
decide (those tests say what is, not what must be).

All of the first kind but one need bad timing: an agent allowing itself from its own device needs only whois to
answer for one of its requests and not the other (or for neither, from its two addresses). The races land the other request on a `Gap` connection, as
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


# -- with no bad timing: allowing an agent from its own device ---------------------------------------------------

@pytest.fixture
def real(tmp_path):
    """The demo catalog served as a real one (no --demo): Tailscale's headers, and a whois the test gives."""
    shutil.copyfile(ROOT / "demo" / "estate.db", tmp_path / "estate.db")
    app = PhotoApp(embed.parser().parse_args(["--db", str(tmp_path / "estate.db"), "serve", "--store", str(ROOT / "demo" / "store"),
                                              "--no-pregenerate", "--upload-user", "owner@example.com",
                                              "--upload-user", "kid@example.com"]))
    yield app
    app.tasks.stop()


KID = {"Tailscale-User-Login": "kid@example.com"}
NAMES = {"100.64.0.5": ("node:nKID1", "kid-macbook"), "fd7a:115c:a1e0::5": ("node:nKID1", "kid-macbook"),
         "100.64.0.9": ("node:nKID2", "kid-iphone")}


def whois(app, answers: bool) -> None:
    """`tailscale whois` answering for the kid's devices, or not answering at all (and nothing kept from before)."""
    app.whois, app.whois_seen = (NAMES.get if answers else (lambda addr: None)), {}


def connect(app, addr: str) -> dict:
    """The kid's agent calls /mcp from `addr`: its binding (made pending the first time)."""
    app.request("POST", "/mcp", {**KID, "X-Forwarded-For": addr}, body={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    return status(app)[-1]


def status(app) -> list[dict]:
    con = sqlite3.connect(app.a.db)
    try:
        return [dict(zip(("id", "node", "status"), r)) for r in con.execute("SELECT id, node, status FROM agent_bindings ORDER BY id")]
    finally:
        con.close()


@pytest.mark.parametrize("made, allowed", [
    ((False, "100.64.0.5"), (True, "100.64.0.5")),            # TLC's trace: made while whois could not name the laptop
    ((True, "100.64.0.5"), (False, "100.64.0.5")),            # allowed while whois cannot name it
    ((False, "100.64.0.5"), (False, "fd7a:115c:a1e0::5")),    # unnamed both times, from its IPv4 and then its IPv6
], ids=["made-blind", "allowed-blind", "two-addresses"])
def test_an_agent_cannot_allow_itself_from_its_own_device(real, made, allowed):
    """OwnDeviceNeverAllows. The device rule compared two strings, and one laptop is node:<StableID> when `tailscale
    whois` answers for its address and addr:<address> when it does not (no CLI, a timeout, an error; a failure is kept
    a minute), and a laptop has two tailnet addresses. The kid's agent, which holds the kid's login on the laptop,
    calls /mcp from it, and then POSTs /api/agents/<id> {allow: true} from the same laptop, named the other way (or by
    its other address): the strings differed, and it was allowed, from its own device."""
    whois(real, made[0])
    b = connect(real, made[1])
    whois(real, allowed[0])
    got = real.request("POST", f"/api/agents/{b['id']}", {**KID, "X-Forwarded-For": allowed[1]}, body={"allow": True})
    assert got.status == 403, "allowing must come from another device than the agent's own"
    assert "could not be named" in got.json()["error"] and [x["status"] for x in status(real)] == ["pending"]


def test_an_agent_is_allowed_from_another_named_device_and_turned_off_from_anywhere(real):
    """What must not change: the agent on the laptop (whois names it) is allowed from the kid's phone (whois names
    it). An agent that connected while the laptop could not be named stays pending: it cannot be allowed (refused, with
    what to do), and when it calls again with whois answering it is a second entry, allowed the same way; the first
    stays pending, and is turned off from the laptop itself, unnamed, or from a request that names no device at all.
    This computer itself, with no login, is no member and has no agents (404), as before."""
    whois(real, False)
    blind = connect(real, "100.64.0.5")
    assert blind["node"] == "addr:100.64.0.5"
    whois(real, True)
    named = connect(real, "100.64.0.5")
    assert named["node"] == "node:nKID1" and named["id"] != blind["id"]
    no = real.request("POST", f"/api/agents/{blind['id']}", {**KID, "X-Forwarded-For": "100.64.0.9"}, body={"allow": True})
    assert no.status == 403 and "allow the new entry" in no.json()["error"]
    got = real.request("POST", f"/api/agents/{named['id']}", {**KID, "X-Forwarded-For": "100.64.0.9"}, body={"allow": True})
    assert (got.status, got.json()["binding"]["status"]) == (200, "approved")
    assert real.request("POST", f"/api/agents/{named['id']}", KID, body={"allow": True}).status == 403   # no device named
    whois(real, False)
    off = real.request("POST", f"/api/agents/{blind['id']}", {**KID, "X-Forwarded-For": "100.64.0.5"}, body={"allow": False})
    assert (off.status, off.json()["binding"]["status"]) == (200, "revoked")
    assert real.request("POST", f"/api/agents/{named['id']}", KID, body={"allow": False}).json()["binding"]["status"] == "revoked"
    assert real.request("POST", f"/api/agents/{named['id']}", {}, body={"allow": False}, local=True).status == 404


def test_the_demos_devices_still_allow(app):
    """What must not change: with --demo, X-Demo-Device names a device and a request with none is this computer; an
    agent on one is allowed from the other, both ways, and never from its own."""
    here = bind(app, BEN)                                                    # made from this computer, allowed from "phone"
    assert (here["node"], here["status"]) == ("local", "approved")
    call(app, "POST", "/mcp", {**CLEO, "X-Demo-Device": "Cleo-Laptop"}, {"jsonrpc": "2.0", "id": 0, "method": "ping"})
    b = next(x for x in call(app, "GET", "/api/agents", CLEO)["bindings"] if x["node"] == "demo:cleo-laptop")
    call(app, "POST", f"/api/agents/{b['id']}", {**CLEO, "X-Demo-Device": "Cleo-Laptop"}, {"allow": True}, expect=403)
    assert call(app, "POST", f"/api/agents/{b['id']}", CLEO, {"allow": True})["binding"]["status"] == "approved"


# -- what the owner decided (issues #130, #131, #132, #134), once "what is" tests: now what must hold -----------------


def test_a_revoked_members_agent_waits_to_be_allowed_again(app):
    """AllowedSinceLetIn: Ben's agent, allowed. Ada revokes Ben: its next call is refused at once, and the binding is
    pending again (binding.pending, and an item in Ben's inbox). Ada lets Ben in again: his agent may look and may not
    write, until he allows it again from another device."""
    b = bind(app, BEN, "Ben-Laptop")
    h = {**BEN, "X-Demo-Device": "Ben-Laptop"}
    ben = mid(app, BEN)
    call(app, "POST", f"/api/members/{ben}", ADA, {"status": "revoked"})
    call(app, "POST", "/mcp", h, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, expect=403)
    con = sqlite3.connect(app.a.db)
    assert con.execute("SELECT status FROM agent_bindings WHERE id=?", (b["id"],)).fetchone() == ("pending",)
    assert con.execute("SELECT kind, acting_for FROM journal WHERE subject=? ORDER BY id DESC LIMIT 1",
                       (f"binding:{b['id']}",)).fetchone() == ("binding.pending", ben)
    assert con.execute("SELECT count(*) FROM inbox_items WHERE member_id=? AND kind='binding' AND ref=? AND state='open'",
                       (ben, f"binding:{b['id']}")).fetchone() == (1,)
    con.close()
    call(app, "POST", f"/api/members/{ben}", ADA, {"status": "active"})
    make = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "create_album", "arguments": {"title": "After"}}}
    assert call(app, "POST", "/mcp", h, make)["result"]["isError"] is True          # not yet: he has not said so again
    call(app, "POST", f"/api/agents/{b['id']}", {**BEN, "X-Demo-Device": "Ben-Phone"}, {"allow": True})
    assert call(app, "POST", "/mcp", h, make)["result"]["isError"] is False
    settle(app)


def test_a_run_acting_for_a_revoked_member_is_over(app):
    """RunsOnlyForActive: a run of the estate's agent acting for Ben is under way (its token), and he has another task
    queued. Ada revokes Ben: both tasks are cancelled, and the run's next call is refused."""
    ben = mid(app, BEN)
    running, queued = task(app, ben), task(app, ben)
    h = {"X-Estate-Agent": app.tasks.mint(running)}
    call(app, "POST", f"/api/members/{ben}", ADA, {"status": "revoked"})
    con = sqlite3.connect(app.a.db)
    assert {s for s, in con.execute("SELECT status FROM agent_tasks WHERE id IN (?,?)", (running, queued))} == {"cancelled"}
    con.close()
    got = app.request("POST", "/mcp", h, body={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, local=True)
    assert got.status == 403 and b"no longer let in" in got.body
    settle(app)


def test_a_task_queued_for_a_member_revoked_since_never_starts(app):
    """The worker's own check (a listener may queue a task for a member, and a revoke may lose a race with the claim):
    a task queued for someone not active is cancelled where it would have been claimed."""
    ben = mid(app, BEN)
    con = sqlite3.connect(app.a.db)
    con.execute("UPDATE members SET status='revoked' WHERE id=?", (ben,))     # (as if the route's cancel never ran)
    con.commit()
    tid = task(app, ben)
    assert not app.tasks.lets(ben) and app.tasks.lets(None)
    con.execute("UPDATE agent_tasks SET status='queued' WHERE id=?", (tid,))
    con.commit()
    app.tasks.stopping.clear()
    worker = threading.Thread(target=app.tasks.worker, daemon=True)
    worker.start()
    for _ in range(100):
        if con.execute("SELECT status FROM agent_tasks WHERE id=?", (tid,)).fetchone() == ("cancelled",):
            break
        threading.Event().wait(0.05)
    assert con.execute("SELECT status FROM agent_tasks WHERE id=?", (tid,)).fetchone() == ("cancelled",)
    con.close()
    app.tasks.stop()


def test_nobody_goes_back_to_waiting(db, con):
    """PendingOnlyBeforeLetIn (core.states.MEMBER_MOVES): let in or turned away, then revoked, then let in again; never
    pending again, from active or from revoked, and nothing is written by the refused change."""
    members.change(con, B, A, status="active")
    with pytest.raises(ValueError, match="is not made pending"):
        members.change(con, B, A, status="pending")
    members.change(con, B, A, status="revoked")
    with pytest.raises(ValueError, match="is not made pending"):
        members.change(con, B, A, status="pending", name="Benjamin")
    assert row(con, B)[0] == "Ben" and row(con, B)[2] == "revoked"
    members.change(con, B, A, status="active")
    assert row(con, B)[2] == "active"


def test_only_a_sysadmin_changes_a_member(db, con, app):
    """members.change checks who asks, whatever its caller checked: a member, a revoked sysadmin and someone unknown
    are Forbidden; this computer itself before there are members (None) and a sysadmin are not. The route answers 403."""
    members.change(con, B, A, status="active")
    for who in (B, 99):
        with pytest.raises(members.Forbidden):
            members.change(con, A, who, name="Mine now")
    members.change(con, D, A, status="revoked")
    with pytest.raises(members.Forbidden):
        members.change(con, B, D, status="revoked")
    assert row(con, A)[0] == "Ada" and row(con, B)[2] == "active"
    assert members.change(con, B, None, name="Benny").name == "Benny"


def test_this_computer_lists_and_turns_off_the_first_sysadmins_agents(real):
    """Issue #135: the owner at the computer itself (no tailnet login) is the first sysadmin, on /agents too: it sees
    their agents and turns one off. It cannot allow one: nothing names the computer as a device."""
    owner = {"Tailscale-User-Login": "owner@example.com"}
    whois(real, True)
    real.request("POST", "/mcp", {**owner, "X-Forwarded-For": "100.64.0.5"}, body={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    bid = status(real)[-1]["id"]
    assert real.request("POST", f"/api/agents/{bid}", {**owner, "X-Forwarded-For": "100.64.0.9"}, body={"allow": True}).status == 200
    import json
    listed = json.loads(real.request("GET", "/api/agents", {}, local=True).body)["bindings"]
    assert [b["id"] for b in listed] == [bid]
    assert real.request("POST", f"/api/agents/{bid}", {}, body={"allow": False}, local=True).status == 200
    assert status(real)[-1]["status"] == "revoked"
    assert real.request("POST", f"/api/agents/{bid}", {}, body={"allow": True}, local=True).status == 403


def test_two_first_starts_at_once_make_one_first_sysadmin(tmp_path):
    """Issue #137: two apps starting on an empty catalog. The second's check for members is made with the write lock
    held, so it waits for the first and finds its members."""
    path = str(tmp_path / "estate.db")
    c1 = estate.connect(path)
    schema.ensure(c1, *schema.CATALOG_AREAS)
    c2 = estate.connect(path)
    real_add, went = members._add, []

    def add(con, *a, **k):
        if not went:                       # the first, about to insert: the second starts now, on another thread
            went.append(threading.Thread(target=lambda: went.append(members.first_start(c2x(), ["bo@example.com"]))))
            went[0].start()
            went[0].join(0.3)              # it waits for the write lock (or, unfixed, reads no members and goes on)
        return real_add(con, *a, **k)

    def c2x():
        return estate.connect(path)

    members._add = add
    try:
        members.first_start(c1, ["al@example.com"])
    finally:
        members._add = real_add
    went[0].join(10)
    assert c1.execute("SELECT count(*) FROM members WHERE role='sysadmin'").fetchone() == (1,)
    c1.close()
    c2.close()
