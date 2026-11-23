"""The photo app as an object (photo_app.PhotoApp), on the demo library, in process: no subprocess, no
search model. Who may do what (only active members may do anything; a request from this computer is the
sysadmin; letting people in, intake and restart take a sysadmin; the request-access page, the home-screen
app files and /api/info's "the app is up" are open), the demo's members by header and cookie, the same
rules on a real catalog's first start from --upload-user, malformed requests answered 400/413 instead of
500, and the HTTP layer in front of it (a real ThreadingHTTPServer on a free port, in a thread).

  uv run pytest tests/test_photo_app.py
"""
import gzip
import json
import shutil
import socket
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import embed  # noqa: E402
import http_util  # noqa: E402
from photo_app import ROUTE_AUTH, PhotoApp, policy  # noqa: E402
from core.lifecycle import Lifecycle  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
# The demo's members (members.DEMO): Ada the sysadmin, Ben and Cleo members. Anyone else is a stranger.
AS_ADA, AS_BEN, AS_STRANGER = {"X-Demo-Member": "Ada"}, {"X-Demo-Member": "ben"}, {"X-Demo-Member": "stranger"}
HTML = {"Accept": "text/html,application/xhtml+xml"}


@pytest.fixture(scope="module")
def app():
    a = embed.parser().parse_args(["serve", "--demo", "--no-pregenerate"])
    app = PhotoApp(a)            # start() is not called: no search model, no faces, no threads
    app.feed_update()            # the sync feed, which start() fills in the background
    return app


def sha_of(app) -> str:
    return app.live.cat.shas[0]


# -- serving --------------------------------------------------------------------------------------

def test_basic_gets_in_process(app):
    r = app.request("GET", "/")
    assert r.status == 200 and r.content_type.startswith("text/html") and r.cache == "no-cache" and r.etag
    info = app.request("GET", "/api/info").json()
    assert info["total"] == app.live.cat.info["total"] > 0 and info["demo"] is True
    page = app.request("GET", "/api/search?limit=5&dedupe=0").json()
    assert len(page["results"]) == 5
    assert app.request("GET", "/api/sync?limit=3").json()["more"] is True
    assert app.request("GET", "/nothing-here").status == 404
    r = app.request("GET", f"/thumb/{'0' * 64}")
    assert r.status == 404


def test_sync_not_ready_says_retry_after(app, monkeypatch):
    monkeypatch.setattr(app.feed, "ready", False)
    r = app.request("GET", "/api/sync")
    assert r.status == 503 and dict(r.headers) == {"Retry-After": "5"}


# -- validation -----------------------------------------------------------------------------------

@pytest.mark.parametrize("target", ["/api/search?limit=abc", "/api/search?cursor=x", "/api/similar?limit=abc",
                                    "/api/sync?limit=abc"])
def test_bad_numbers_are_400(app, target):
    r = app.request("GET", target)
    assert r.status == 400 and r.content_type == "application/json", (target, r)
    assert "must be a whole number" in r.json()["error"]


def test_months_outside_1_to_12_are_no_filter(app):
    every = app.request("GET", "/api/search?limit=1&dedupe=0").json()["matching"]
    for bad in ("2020-00", "2020-99"):
        assert app.request("GET", f"/api/search?limit=1&dedupe=0&from={bad}").json()["matching"] == every


def test_bad_json_is_400(app):
    for path in ("/api/favorite", "/api/albums", "/api/people/label"):
        r = app.request("POST", path, body=b"{nope", local=True)
        assert r.status == 400 and r.json() == {"error": "the body is not JSON"}, path
    r = app.request("POST", "/mcp", body=b"{nope", local=True)   # JSON-RPC's own parse error
    assert r.status == 400 and r.json()["error"]["code"] == -32700
    assert app.request("POST", "/api/favorite", body=b"", local=True).status == 400   # {} names no photo


# -- who may do what -----------------------------------------------------------------------------

def test_the_route_rules():
    Auth = http_util.Auth
    assert {rule for _, _, rule in ROUTE_AUTH} == {Auth.PUBLIC, Auth.SYSADMIN}   # everything else: MEMBER
    for method, path in [("POST", "/api/people/label"), ("POST", "/api/favorite"), ("POST", "/api/albums"),
                         ("POST", "/api/changes/3/apply"), ("POST", "/api/tasks"), ("POST", "/api/ingest"),
                         ("PUT", "/api/upload/2020/01/01/a.jpg"), ("GET", "/"), ("GET", "/api/search"), ("GET", "/thumb/x"),
                         ("GET", "/api/sync"), ("GET", "/api/me"), ("POST", "/mcp"), ("GET", "/app/dom.js"),
                         ("GET", "/api/albums/1/download"), ("GET", "/video/x"), ("OPTIONS", "/api/upload/x")]:
        assert policy(method, path) is Auth.MEMBER, (method, path)
    for method, path in [("GET", "/members"), ("GET", "/api/members"), ("POST", "/api/members/2"), ("GET", "/setup"),
                         ("POST", "/api/intake/jobs"), ("GET", "/api/intake/jobs"), ("POST", "/api/restart")]:
        assert policy(method, path) is Auth.SYSADMIN, (method, path)
    for method, path in [("GET", "/request-access"), ("POST", "/api/request-access"), ("GET", "/api/info"),
                         ("GET", "/manifest.webmanifest"), ("GET", "/sw.js"), ("GET", "/icon-192.png")]:
        assert policy(method, path) is Auth.PUBLIC, (method, path)


def test_auth_rules_by_caller():
    import members
    Auth = http_util.Auth
    local = members.Member(1, "this computer", role="sysadmin", local=True)
    admin, member = members.Member(1, "Ada", role="sysadmin"), members.Member(2, "Ben")
    pending, revoked = members.Member(3, "Dana", status="pending"), members.Member(4, "Eve", status="revoked")
    fallen = members.Member(5, "Old", role="sysadmin", status="revoked")
    table = {Auth.PUBLIC: [True] * 6, Auth.MEMBER: [True, True, True, False, False, False],
             Auth.SYSADMIN: [True, True, False, False, False, False], Auth.LOCAL: [True, False, False, False, False, False]}
    for rule, want in table.items():
        assert [rule.allows(x) for x in (local, admin, member, pending, revoked, fallen)] == want, rule
        assert rule.allows(None) is (rule is Auth.PUBLIC)


def test_demo_members_by_header_or_cookie(app):
    me = lambda h: app.request("GET", "/api/me", h).json()
    assert me({})["name"] == "Ada" and me({})["role"] == "sysadmin"                 # neither: the demo's sysadmin
    assert me(AS_BEN)["name"] == "Ben" and me(AS_BEN)["role"] == "member"
    assert me({"Cookie": "other=1; demo_member=cleo"})["name"] == "Cleo"
    assert me({**AS_BEN, "Cookie": "demo_member=cleo"})["name"] == "Ben"          # the header first
    assert me({"Tailscale-User-Login": "ben"})["name"] == "Ada", "with --demo the Tailscale header is not read"
    assert me({**AS_BEN, "X-Forwarded-For": "100.64.0.9"})["upload_policy"] == "review"
    assert app.request("GET", "/api/me", AS_STRANGER).status == 403
    assert app.request("GET", "/api/me", AS_BEN, local=True).json()["name"] == "Ben", "a demo member, even from here"


def test_a_stranger_is_refused_everything_but_asking(app):
    sha = sha_of(app)
    for path in ("/", "/api/search?limit=1", f"/thumb/{sha}", f"/original/{sha}", "/api/sync", "/app/dom.js", "/stats",
                 "/api/albums", "/changes", "/api/me", "/people"):
        r = app.request("GET", path, AS_STRANGER)
        assert r.status == 403 and r.json() == {"error": "not allowed"}, path
    for method, path in (("POST", "/api/favorite"), ("POST", "/api/albums"), ("POST", "/mcp"), ("POST", "/api/tasks")):
        assert app.request(method, path, AS_STRANGER, body={}).status == 403, path
    assert app.request("GET", "/api/info", AS_STRANGER).json() == {"member": False}     # the app is up, and no more
    for path in ("/sw.js", "/manifest.webmanifest", "/icon-192.png", "/request-access"):
        assert app.request("GET", path, AS_STRANGER).status == 200, path
    page = app.request("GET", "/", {**AS_STRANGER, **HTML})                           # a browser is shown why
    assert page.status == 403 and page.content_type.startswith("text/html")
    assert b"Ask Ada to let you in" in page.body and b'action="/api/request-access"' in page.body
    assert b"<b>stranger</b>" in page.body and b"<script" not in page.body


def test_asking_waiting_being_let_in_and_turned_off(app):
    who, form = {"X-Demo-Member": "dana"}, {"Content-Type": "application/x-www-form-urlencoded"}
    r = app.request("POST", "/api/request-access", {**who, **form}, body=b"name=Dana+Smith")
    assert r.status == 303 and r.headers["Location"] == "/request-access"
    waiting = app.request("GET", "/request-access", who)
    assert b"Ada will let you in" in waiting.body and b"<form" not in waiting.body
    assert app.request("POST", "/api/request-access", who, body={"name": "again"}).json() == {"status": "pending"}
    assert app.request("GET", "/api/search?limit=1", who).status == 403
    listed = app.request("GET", "/api/members").json()
    dana = next(m for m in listed["members"] if m["logins"][0]["login"] == "dana")
    assert (dana["name"], dana["status"], listed["members"][0]["status"]) == ("Dana Smith", "pending", "pending")
    assert app.request("POST", f"/api/members/{dana['id']}", AS_BEN, body={"status": "active"}).status == 403
    got = app.request("POST", f"/api/members/{dana['id']}", AS_ADA, body={"status": "active"})
    assert got.status == 200 and got.json()["member"]["status"] == "active" and got.json()["member"]["joined_at"]
    assert app.request("GET", "/api/search?limit=1", who).status == 200
    assert app.request("GET", "/api/me", who).json()["status"] == "active"
    assert app.request("POST", f"/api/members/{dana['id']}", {}, body={"status": "revoked"}).status == 200
    assert app.request("GET", "/api/search?limit=1", who).status == 403
    assert b"You are not let in" in app.request("GET", "/", {**who, **HTML}).body
    assert app.request("POST", "/api/request-access", who, body={}).json() == {"status": "revoked"}   # asking again: still out


def test_members_changes_are_the_sysadmins(app):
    ada = app.request("GET", "/api/me").json()
    assert app.request("GET", "/api/members", AS_BEN).status == 403
    member_page = app.request("GET", "/members", {**AS_BEN, **HTML})
    assert member_page.status == 403 and b"Only Ada can open this" in member_page.body
    assert app.request("POST", f"/api/members/{ada['id']}", AS_ADA, body={"status": "revoked"}).json() == \
        {"error": "the estate must keep an active sysadmin"}
    assert app.request("POST", f"/api/members/{ada['id']}", AS_ADA, body={"role": "viewer"}).status == 400
    assert app.request("POST", f"/api/members/{ada['id']}", AS_ADA, body={"shoe": 1}).status == 400
    assert app.request("POST", "/api/members/999", AS_ADA, body={"status": "active"}).status == 404
    assert app.request("POST", f"/api/members/{ada['id']}", AS_ADA, body=[1]).status == 400
    listed = app.request("GET", "/api/members", AS_ADA).json()
    assert listed["me"] == ada["id"] and listed["demo"] is True and listed["persons"] == []
    assert listed["roles"] == ["sysadmin", "member"] and listed["policies"] == ["everything", "review", "review_flagged"]


def test_be_this_member_sets_the_demo_cookie(app):
    r = app.request("GET", "/members?be=ben")
    assert r.status == 303 and r.headers["Location"] == "/" and r.headers["Set-Cookie"].startswith("demo_member=ben;")
    r = app.request("GET", "/members?be=", AS_BEN)                                    # back to the demo's sysadmin
    assert r.status == 303 and r.headers["Location"] == "/members" and "Max-Age=0" in r.headers["Set-Cookie"]
    assert app.request("GET", "/members?be=ben", {"Tailscale-User-Login": "x"}).status == 303


def ready_people(stub) -> Lifecycle:
    people = Lifecycle("the People page")
    people.begin()
    people.succeed(stub)
    return people


class StubPeople:
    def __init__(self):
        self.calls = []

    def handle(self, method, path, qs, body):
        self.calls.append((method, path, body))
        return 200, b'{"ok":true}', "application/json", "no-store"


@pytest.fixture
def with_people(app, monkeypatch):
    """The demo has no faces: a People page that records what reaches it."""
    stub = StubPeople()
    monkeypatch.setattr(app.live.cat, "has_faces", True)
    monkeypatch.setattr(app, "people", ready_people(stub))
    return app, stub


@pytest.mark.parametrize("path", ["/api/people/label", "/api/people/label-many", "/api/people/person"])
def test_people_writes_need_a_member(with_people, path):
    app, stub = with_people
    r = app.request("POST", path, AS_STRANGER, body={"faces": [1], "person": "Someone"})
    assert r.status == 403 and r.json() == {"error": "not allowed"} and stub.calls == []
    assert app.request("POST", path, AS_BEN, body={"id": 1}).status == 200
    assert app.request("POST", path, {}, body={"id": 1}, local=True).status == 200   # the agent on this computer
    assert len(stub.calls) == 2
    assert app.request("GET", "/api/people", AS_STRANGER).status == 403              # reading takes a member too
    assert app.request("GET", "/api/people", AS_BEN).status == 200


def test_favorite_albums_tasks_and_mcp_take_a_member(app, monkeypatch):
    sha = sha_of(app)
    body = {"sha": sha, "fav": True}
    assert app.request("POST", "/api/favorite", AS_STRANGER, body=body).status == 403
    assert app.request("POST", "/api/favorite", AS_BEN, body=body).status == 200
    assert app.request("POST", "/api/favorite", {}, body={"sha": sha, "fav": False}, local=True).status == 200
    album = {"title": "test", "items": [{"sha": sha}]}
    assert app.request("POST", "/api/albums", AS_STRANGER, body=album).status == 403
    made = app.request("POST", "/api/albums", AS_BEN, body=album)
    assert made.status == 201 and made.json()["created_by"] == "ben"
    sid = made.json()["id"]
    assert app.request("POST", f"/api/albums/{sid}/discard", AS_STRANGER, body={}).status == 403
    assert app.request("POST", f"/api/albums/{sid}/discard", {}, body={}, local=True).status == 200
    # a task records the member who asked for it (agent_tasks.acting_for), from the page and from /mcp
    asked = []
    monkeypatch.setattr(app.tasks, "submit", lambda prompt, who="", acting_for=None: (asked.append((prompt, who, acting_for)),
                                                                                       ({"error": "not here"}, 409))[1])
    ben = app.request("GET", "/api/me", AS_BEN).json()["id"]
    assert app.request("POST", "/api/tasks", AS_STRANGER, body={"prompt": "x"}).status == 403
    assert app.request("POST", "/api/tasks", AS_BEN, body={"prompt": "x"}).status == 409
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "submit_task", "arguments": {"request": "y"}}}
    assert app.request("POST", "/mcp", AS_BEN, body=call).json()["result"]["isError"]
    assert asked == [("x", "ben", ben), ("y", "ben", ben)]
    assert app.request("POST", "/mcp", AS_STRANGER, body=call).status == 403          # /mcp takes a member, as every route
    listing = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert len(app.request("POST", "/mcp", AS_BEN, body=listing).json()["result"]["tools"]) == 16


def test_a_task_keeps_who_it_acts_for(tmp_path):
    import sqlite3
    import tasks
    t = tasks.Tasks(str(tmp_path / "estate.db"), "http://127.0.0.1:1/mcp", command="/usr/bin/false")
    t.wake.clear()
    got, code = t.submit("find the receipts", "ben", 2)
    assert code == 201 and got["acting_for"] == 2 and got["created_by"] == "ben"
    con = sqlite3.connect(tmp_path / "estate.db")
    assert con.execute("SELECT acting_for FROM agent_tasks WHERE id=?", (got["id"],)).fetchone() == (2,)
    con.close()


class StubIntake:
    def handle(self, method, path, qs, body, local):
        return 201, b'{"id":1}', "application/json"


class StubIngest:
    def start(self):
        return True

    def poke(self, *args, **kw):
        pass


def test_intake_restart_take_a_sysadmin_and_ingest_and_uploads_a_member(app, monkeypatch, tmp_path):
    monkeypatch.setattr(app, "intake", StubIntake())
    monkeypatch.setattr(app.admin, "ingest", StubIngest())
    monkeypatch.setattr(app.admin, "inbox", tmp_path)
    job = {"source": "folder", "config": {}}
    assert app.request("POST", "/api/intake/jobs", AS_BEN, body=job).status == 403      # a job reads any folder here
    assert app.request("POST", "/api/intake/jobs", AS_ADA, body=job).status == 201
    assert app.request("POST", "/api/intake/jobs", {}, body=job, local=True).status == 201
    assert app.request("POST", "/api/restart", AS_BEN).status == 403
    assert app.request("POST", "/api/ingest", AS_STRANGER).status == 403
    assert app.request("POST", "/api/ingest", AS_BEN).status == 202
    up = "/api/upload/2020/01/01/a.jpg"
    assert app.request("PUT", up, AS_STRANGER, body=b"abc").status == 403
    assert app.request("PUT", up, AS_BEN, body=b"abc").status == 201
    assert app.request("PUT", "/api/upload/2020/01/01/b.jpg", {}, body=b"abc", local=True).status == 201


# -- a real catalog: the first start from --upload-user, and the Tailscale header ----------------

@pytest.fixture
def real(tmp_path):
    """A copy of the demo catalog served as a real one: no --demo, two --upload-user logins."""
    shutil.copyfile(ROOT / "demo" / "estate.db", tmp_path / "estate.db")
    def make(*extra):
        return PhotoApp(embed.parser().parse_args(["--db", str(tmp_path / "estate.db"), "serve", "--store", str(ROOT / "demo" / "store"),
                                                   "--no-pregenerate", *extra]))
    return make


def test_first_start_makes_the_upload_users_members(real):
    app = real("--upload-user", "Owner@Example.com", "--upload-user", "kid@example.com")
    owner, kid = {"Tailscale-User-Login": "owner@example.com"}, {"Tailscale-User-Login": "kid@example.com"}
    me = app.request("GET", "/api/me", owner).json()
    assert (me["name"], me["role"], me["upload_policy"], me["login"]) == ("owner", "sysadmin", "everything", "owner@example.com")
    assert app.request("GET", "/api/me", kid).json()["role"] == "member"
    assert app.request("GET", "/api/members", owner).status == 200 and app.request("GET", "/api/members", kid).status == 403
    assert app.request("GET", "/api/search?limit=1", kid).status == 200
    assert app.request("POST", "/api/favorite", kid, body={"sha": sha_of(app), "fav": True}).status == 200
    local = app.request("GET", "/api/me", local=True).json()                       # this computer: the sysadmin
    assert local["local"] and local["role"] == "sysadmin" and local["id"] == me["id"] and local["login"] == ""
    for nobody in ({}, {"Tailscale-User-Login": "other@example.com"}, {"X-Demo-Member": "owner@example.com"},
                   {"Cookie": "demo_member=owner@example.com"}):                  # the demo's ways in are not honoured
        assert app.request("GET", "/api/search?limit=1", nobody).status == 403, nobody
    assert b"Who is asking?" in app.request("GET", "/", HTML).body                  # through the proxy with no login
    assert b"Ask owner to let you in" in app.request("GET", "/", {"Tailscale-User-Login": "other@example.com", **HTML}).body
    assert app.request("GET", "/members?be=owner@example.com").status == 403       # no demo switching
    # a second start: members are kept on /members now, not taken from the options again
    again = real("--upload-user", "third@example.com", "--sysadmin", "third@example.com")
    assert again.request("GET", "/api/search?limit=1", {"Tailscale-User-Login": "third@example.com"}).status == 403
    assert again.request("GET", "/api/me", owner).json()["role"] == "sysadmin"


def test_first_start_with_a_sysadmin_named_and_with_nobody(real, tmp_path):
    app = real("--upload-user", "a@example.com", "--sysadmin", "b@example.com")
    assert app.request("GET", "/api/me", {"Tailscale-User-Login": "b@example.com"}).json()["role"] == "sysadmin"
    assert app.request("GET", "/api/me", {"Tailscale-User-Login": "a@example.com"}).json()["role"] == "member"


def test_with_no_members_only_this_computer_may(tmp_path):
    shutil.copyfile(ROOT / "demo" / "estate.db", tmp_path / "estate.db")
    app = PhotoApp(embed.parser().parse_args(["--db", str(tmp_path / "estate.db"), "serve", "--store", str(ROOT / "demo" / "store"),
                                              "--no-pregenerate"]))
    assert app.request("GET", "/api/search?limit=1", {"Tailscale-User-Login": "x@example.com"}).status == 403
    me = app.request("GET", "/api/me", local=True).json()
    assert me["id"] is None and me["role"] == "sysadmin" and me["local"]
    page = app.request("GET", "/request-access", {"Tailscale-User-Login": "x@example.com"}).body
    assert b"Ask whoever looks after this library to let you in" in page


# -- the HTTP layer -------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def server(app):
    srv = app.make_server("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def raw(port: int, head: str, body: bytes = b"") -> tuple[int, dict, bytes]:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(head.encode() + b"\r\nHost: 127.0.0.1\r\n\r\n" + body)
        got = b""
        while b"\r\n\r\n" not in got:
            chunk = s.recv(65536)
            if not chunk:
                break
            got += chunk
        top, _, rest = got.partition(b"\r\n\r\n")
        lines = top.decode().split("\r\n")
        headers = {k.lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
        while len(rest) < int(headers.get("content-length", 0)):
            chunk = s.recv(65536)
            if not chunk:
                break
            rest += chunk
        return int(lines[0].split()[1]), headers, rest


def test_http_bodies(server):
    code, _, body = raw(server, "POST /api/favorite HTTP/1.1\r\nContent-Length: 5", b"{nope")
    assert code == 400 and json.loads(body) == {"error": "the body is not JSON"}
    code, _, body = raw(server, "POST /api/favorite HTTP/1.1\r\nContent-Length: abc", b"{}")
    assert code == 400 and "Content-Length" in json.loads(body)["error"]
    code, h, body = raw(server, f"POST /api/favorite HTTP/1.1\r\nContent-Length: {http_util.MAX_BODY + 1}")
    assert code == 413 and json.loads(body)["error"].startswith("the body is over")
    code, _, body = raw(server, "POST /api/favorite HTTP/1.1\r\nContent-Length: 2", b"{}")
    assert code == 400 and "say which photo" in json.loads(body)["error"]


def test_http_headers_unchanged(server):
    base = f"http://127.0.0.1:{server}"
    with urllib.request.urlopen(base + "/") as r:
        etag, cache = r.headers["ETag"], r.headers["Cache-Control"]
    assert etag and cache == "no-cache"
    req = urllib.request.Request(base + "/", headers={"If-None-Match": etag})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 304 and e.value.headers["ETag"] == etag
    req = urllib.request.Request(base + "/api/search?limit=50", headers={"Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req) as r:
        assert r.headers["Content-Encoding"] == "gzip" and r.headers["Vary"] == "Accept-Encoding"
        assert len(json.loads(gzip.decompress(r.read()))["results"]) == 50
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(base + "/api/sync?limit=abc")
    assert e.value.code == 400
    code, h, _ = raw(server, "OPTIONS /api/upload/x.jpg HTTP/1.1")
    assert code == 501 and h["allow"] == "OPTIONS, PUT, POST"     # send each file in one request


def test_http_streamed_routes_take_a_member(server, app):
    """The album download and /video/ are sent by the handler, past route(): it asks refusal() first."""
    base, sha = f"http://127.0.0.1:{server}", sha_of(app)
    for path in ("/api/albums/1/download", f"/video/{sha}"):
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(urllib.request.Request(base + path, headers=AS_STRANGER))
        assert e.value.code == 403 and json.loads(e.value.read()) == {"error": "not allowed"}, path
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(urllib.request.Request(base + path, headers=AS_BEN))
        assert e.value.code == 404, path                                           # let in: there is no such album or video
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(urllib.request.Request(base + "/", headers={**AS_STRANGER, **HTML}))
    assert e.value.code == 403 and b"to let you in" in e.value.read()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
