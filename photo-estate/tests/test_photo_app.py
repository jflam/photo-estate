"""The photo app as an object (photo_app.PhotoApp), on the demo library, in process: no subprocess, no
search model. Who may write (each request that changes the library takes a --upload-user login or a
request from this computer itself), malformed requests answered 400/413 instead of 500, and the HTTP
layer in front of it (a real ThreadingHTTPServer on a free port, in a thread).

  uv run pytest tests/test_photo_app.py
"""
import gzip
import json
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

WRITER, VIEWER = "writer@example.com", "viewer@example.com"
AS_WRITER = {"Tailscale-User-Login": WRITER}
AS_VIEWER = {"Tailscale-User-Login": VIEWER}


@pytest.fixture(scope="module")
def app():
    a = embed.parser().parse_args(["serve", "--demo", "--no-pregenerate"])
    app = PhotoApp(a)            # start() is not called: no search model, no faces, no threads
    app.feed_update()            # the sync feed, which start() fills in the background
    return app


@pytest.fixture
def restricted(app, monkeypatch):
    """--upload-user writer@example.com: only that login, or this computer, may write."""
    monkeypatch.setattr(app.a, "upload_user", [WRITER])
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


# -- who may write --------------------------------------------------------------------------------

def test_every_write_is_local_or_writer():
    rules = {(m, p): rule for m, ps, rule in ROUTE_AUTH for p in ps}
    assert set(rules.values()) == {http_util.Auth.LOCAL_OR_WRITER}
    for method, path in [("POST", "/api/people/label"), ("POST", "/api/people/label-many"), ("POST", "/api/people/person"),
                         ("POST", "/api/favorite"), ("POST", "/api/albums"), ("POST", "/api/changes/3/apply"),
                         ("POST", "/api/tasks"), ("POST", "/api/intake/jobs"), ("POST", "/api/ingest"),
                         ("POST", "/api/restart"), ("PUT", "/api/upload/2020/01/01/a.jpg")]:
        assert policy(method, path) is http_util.Auth.LOCAL_OR_WRITER, (method, path)
    for method, path in [("GET", "/api/people"), ("GET", "/api/search"), ("POST", "/mcp"), ("GET", "/api/albums")]:
        assert policy(method, path) is http_util.Auth.PUBLIC, (method, path)


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
def with_people(restricted, monkeypatch):
    """The demo has no faces: a People page that records what reaches it."""
    stub = StubPeople()
    monkeypatch.setattr(restricted.live.cat, "has_faces", True)
    monkeypatch.setattr(restricted, "people", ready_people(stub))
    return restricted, stub


@pytest.mark.parametrize("path", ["/api/people/label", "/api/people/label-many", "/api/people/person"])
def test_people_writes_need_a_writer(with_people, path):
    app, stub = with_people
    r = app.request("POST", path, AS_VIEWER, body={"faces": [1], "person": "Someone"})
    assert r.status == 403 and r.json() == {"error": "not allowed"} and stub.calls == []
    r = app.request("POST", path, {}, body={"faces": [1], "person": "Someone"})     # through the proxy, no login
    assert r.status == 403 and stub.calls == []
    assert app.request("POST", path, AS_WRITER, body={"id": 1}).status == 200
    assert app.request("POST", path, {}, body={"id": 1}, local=True).status == 200   # the agent on this computer
    assert len(stub.calls) == 2
    assert app.request("GET", "/api/people", AS_VIEWER).status == 200               # reading stays open


def test_everyone_writes_without_upload_user(app, monkeypatch):
    stub = StubPeople()
    monkeypatch.setattr(app.live.cat, "has_faces", True)
    monkeypatch.setattr(app, "people", ready_people(stub))
    assert app.a.upload_user == []
    assert app.request("POST", "/api/people/label", AS_VIEWER, body={}).status == 200


def test_favorite_and_albums_take_the_same_rule(restricted):
    sha = sha_of(restricted)
    body = {"sha": sha, "fav": True}
    assert restricted.request("POST", "/api/favorite", AS_VIEWER, body=body).status == 403
    assert restricted.request("POST", "/api/favorite", AS_WRITER, body=body).status == 200
    assert restricted.request("POST", "/api/favorite", {}, body={"sha": sha, "fav": False}, local=True).status == 200
    album = {"title": "test", "items": [{"sha": sha}]}
    assert restricted.request("POST", "/api/albums", AS_VIEWER, body=album).status == 403
    made = restricted.request("POST", "/api/albums", AS_WRITER, body=album)
    assert made.status == 201
    sid = made.json()["id"]
    assert restricted.request("POST", f"/api/albums/{sid}/discard", AS_VIEWER, body={}).status == 403
    assert restricted.request("POST", f"/api/albums/{sid}/discard", {}, body={}, local=True).status == 200
    assert restricted.request("POST", "/api/tasks", AS_VIEWER, body={"prompt": "x"}).status == 403
    # MCP: anyone may search; proposing takes the same rule (its own isError answer)
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "create_album", "arguments": {"title": "t"}}}
    got = restricted.request("POST", "/mcp", AS_VIEWER, body=call).json()["result"]
    assert got["isError"] and "may look but not propose" in got["content"][0]["text"]


class StubIntake:
    def handle(self, method, path, qs, body, local):
        return 201, b'{"id":1}', "application/json"


class StubIngest:
    def start(self):
        return True

    def poke(self, *args, **kw):
        pass


def test_intake_and_admin_writes_let_this_computer_in(restricted, monkeypatch, tmp_path):
    """Before, these took only a --upload-user login: the browser and agent on this computer were refused."""
    monkeypatch.setattr(restricted, "intake", StubIntake())
    monkeypatch.setattr(restricted.admin, "ingest", StubIngest())
    monkeypatch.setattr(restricted.admin, "inbox", tmp_path)
    job = {"source": "folder", "config": {}}
    assert restricted.request("POST", "/api/intake/jobs", AS_VIEWER, body=job).status == 403
    assert restricted.request("POST", "/api/intake/jobs", AS_WRITER, body=job).status == 201
    assert restricted.request("POST", "/api/intake/jobs", {}, body=job, local=True).status == 201
    assert restricted.request("POST", "/api/ingest", AS_VIEWER).status == 403
    assert restricted.request("POST", "/api/ingest", {}, local=True).status == 202
    up = "/api/upload/2020/01/01/a.jpg"
    assert restricted.request("PUT", up, AS_VIEWER, body=b"abc").status == 403
    assert restricted.request("PUT", up, {}, body=b"abc", local=True).status == 201


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


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
