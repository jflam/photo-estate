"""http_util.py and the request checks built on it, in process: Response is a value that can't be changed,
query parameters and bodies that can't be read are 400s (413 when too large) instead of exceptions, a
month outside 1-12 is no month, and the setup server (intake/server.py) bounds the bodies it reads.

  uv run pytest tests/test_http_util.py
"""
import dataclasses
import io
import json
import socket
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import catalog  # noqa: E402
import http_util  # noqa: E402
from http_util import Auth, HTTPError, Response, int_param, parse_json, read_body, read_json  # noqa: E402
from intake.api import IntakeAPI, Library  # noqa: E402
from intake.server import make_handler  # noqa: E402


# -- Response ---------------------------------------------------------------------------------------

def test_response_is_frozen():
    r = Response(200, b"x", "text/plain", "no-store", {"ETag": '"1"'})
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.status = 500
    with pytest.raises(TypeError):
        r.headers["X-Other"] = "1"          # the headers can't be changed either
    assert r.etag == '"1"'


def test_response_of_module_tuples():
    assert Response.of(None) == http_util.NOT_FOUND
    r = Response.of((201, b"{}", "application/json"))
    assert (r.status, r.cache, dict(r.headers)) == (201, "no-store", {})
    r = Response.of((200, b"", "text/html", "no-cache", {"ETag": '"a"', "Allow": "GET"}))
    assert (r.cache, r.etag, r.headers["Allow"]) == ("no-cache", '"a"', "GET")


# -- query parameters -------------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["abc", "x", "1.5", "1e3", "", " "])
def test_int_param_garbage_is_400_or_default(raw):
    qs = {"limit": [raw]}
    if raw.strip():
        with pytest.raises(HTTPError) as e:
            int_param(qs, "limit", 10)
        assert e.value.status == 400 and "limit" in e.value.message
        assert e.value.response.status == 400 and e.value.response.json() == {"error": "limit must be a whole number"}
    else:
        assert int_param(qs, "limit", 10) == 10


def test_int_param_clamps_like_before():
    assert int_param({}, "limit", 120, min=1, max=500) == 120
    assert int_param({"limit": ["0"]}, "limit", 120, min=1, max=500) == 1
    assert int_param({"limit": ["9999"]}, "limit", 120, min=1, max=500) == 500
    assert int_param({"cursor": ["-4"]}, "cursor", 0, min=0) == 0
    assert int_param({"n": ["-4"]}, "n", 0) == -4


# -- bodies -----------------------------------------------------------------------------------------

def test_bodies():
    assert parse_json(b"") == {} and parse_json(b"  ") == {}      # a POST that takes no arguments sends none
    assert parse_json(b'{"a":1}') == {"a": 1}
    for bad in (b"{nope", b"\xff\xfe", b"[1,"):
        with pytest.raises(HTTPError) as e:
            parse_json(bad)
        assert e.value.status == 400
    assert read_json(io.BytesIO(b'{"a":1}'), {"Content-Length": "7"}) == {"a": 1}
    assert read_body(io.BytesIO(b""), {}) == b""
    for bad in ("abc", "-1", "1e3", "²"):
        with pytest.raises(HTTPError) as e:
            read_body(io.BytesIO(b"x"), {"Content-Length": bad})
        assert e.value.status == 400
    f = io.BytesIO(b"x" * 100)
    with pytest.raises(HTTPError) as e:
        read_body(f, {"Content-Length": "100"}, limit=99)
    assert e.value.status == 413 and f.tell() == 0                 # nothing was read


def test_auth_policy():
    from members import Member
    local, admin, member = Member(1, "here", role="sysadmin", local=True), Member(1, "Ada", role="sysadmin"), Member(2, "Ben")
    pending = Member(3, "Dana", status="pending")
    assert Auth.PUBLIC.allows(None) and Auth.PUBLIC.allows(pending)
    assert [Auth.MEMBER.allows(x) for x in (local, admin, member, pending, None)] == [True, True, True, False, False]
    assert [Auth.SYSADMIN.allows(x) for x in (local, admin, member, pending, None)] == [True, True, False, False, False]
    assert [Auth.LOCAL.allows(x) for x in (local, admin, member, pending, None)] == [True, False, False, False, False]


# -- catalog.month_index ----------------------------------------------------------------------------

def test_month_index_rejects_months_outside_1_to_12():
    c = SimpleNamespace(m0=2020 * 12)
    mi = lambda s, end=False: catalog.Catalog.month_index(c, s, end)
    assert mi("2020-00") is None and mi("2020-99") is None and mi("2020-13") is None and mi("2020-0") is None
    assert mi("2020-01") == 0 and mi("2020-12") == 11 and mi("2020-7") == 6
    assert mi("2020") == 0 and mi("2020", end=True) == 11 and mi("2021-01") == 12
    assert mi("") is None and mi("abc") is None


# -- intake/api.py: job and after -------------------------------------------------------------------

class StubEngine:
    def view(self, jid, after):
        return {"id": jid, "after": after}


def test_intake_numbers_are_400(tmp_path):
    api = IntakeAPI(Library(None), lambda lib: None, setup_mode=True, practice=True)
    code, body, _ = api.handle("GET", "/setup/practice-consent", {"job": ["x"]}, None, False)
    assert code == 400 and json.loads(body)["error"] == "job must be a whole number"
    assert api.handle("GET", "/setup/practice-consent", {"job": ["3"]}, None, False)[0] == 200

    api = IntakeAPI(Library(tmp_path), lambda lib: StubEngine(), setup_mode=False)
    code, body, _ = api.handle("GET", "/api/intake/jobs/1", {"after": ["x"]}, None, False)
    assert code == 400 and json.loads(body)["error"] == "after must be a whole number"
    code, body, _ = api.handle("GET", "/api/intake/jobs/1", {"after": ["5"]}, None, False)
    assert code == 200 and json.loads(body) == {"id": 1, "after": 5}


# -- intake/server.py: the setup server's bodies ----------------------------------------------------

def raw_request(port: int, head: str, body: bytes = b"", timeout: float = 5.0) -> tuple[int, bytes]:
    """Send `head` (and `body`) as they are, and read the status and body of the answer."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.sendall(head.encode() + b"\r\n\r\n" + body)
        got = b""
        while b"\r\n\r\n" not in got:
            chunk = s.recv(65536)
            if not chunk:
                break
            got += chunk
        head_, _, rest = got.partition(b"\r\n\r\n")
        n = int(next((ln.split(b":", 1)[1] for ln in head_.split(b"\r\n") if ln.lower().startswith(b"content-length:")), b"0"))
        while len(rest) < n:
            chunk = s.recv(65536)
            if not chunk:
                break
            rest += chunk
        return int(head_.split()[1]), rest


@pytest.fixture
def setup_server():
    calls = []

    class Api:
        def handle(self, method, path, qs, body, local):
            calls.append(body)
            return 200, json.dumps({"got": body}).encode(), "application/json"

    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Api()))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], calls
    srv.shutdown()
    srv.server_close()


def test_setup_server_bounds_bodies(setup_server):
    port, calls = setup_server
    post = "POST /api/intake/preview HTTP/1.1\r\nHost: x\r\nContent-Type: application/json"
    code, body = raw_request(port, post + "\r\nContent-Length: 7", b'{"a":1}')
    assert code == 200 and json.loads(body) == {"got": {"a": 1}}
    code, body = raw_request(port, post + "\r\nContent-Length: 5", b"{nope")
    assert code == 400 and json.loads(body)["error"] == "the body is not JSON"
    code, body = raw_request(port, post + "\r\nContent-Length: abc", b"{}")
    assert code == 400 and "Content-Length" in json.loads(body)["error"]
    # Over the limit: answered at once, without waiting for (or reading) the body
    code, body = raw_request(port, post + f"\r\nContent-Length: {http_util.MAX_BODY + 1}")
    assert code == 413 and "over" in json.loads(body)["error"]
    assert calls == [{"a": 1}]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


# -- byte_range: GET /video/<sha>'s Range header ------------------------------------------------------

@pytest.mark.parametrize("header,size,want", [
    (None, 1000, None),                       # no header: the whole file
    ("", 1000, None),
    ("items=0-1", 1000, None),                # not bytes=: ignored (RFC 9110)
    ("bytes=0-1", 1000, (0, 1)),
    ("Bytes= 0-1 ", 1000, (0, 1)),
    ("bytes=0-0", 1000, (0, 0)),
    ("bytes=5-", 1000, (5, 999)),
    ("bytes=999-", 1000, (999, 999)),
    ("bytes=-500", 1000, (500, 999)),         # the last 500 bytes
    ("bytes=-5000", 1000, (0, 999)),          # a suffix longer than the file: all of it
    ("bytes=900-5000", 1000, (900, 999)),     # an end beyond the file is clamped
    ("bytes=0-1,5-6", 1000, None),            # several ranges: the whole file
    ("bytes=-", 1000, None),
    ("bytes=a-b", 1000, None),
    ("bytes=1", 1000, None),
    ("bytes=٣-٥", 1000, None),                # digits, but not ASCII ones
    ("bytes=+1-2", 1000, None),
    ("bytes=5-3", 1000, None),                # last before first: not a valid range, so ignored
    (None, 0, None),
    ("bytes=0-", 1, (0, 0)),
])
def test_byte_range(header, size, want):
    assert http_util.byte_range(header, size) == want


@pytest.mark.parametrize("header,size", [
    ("bytes=1000-", 1000),                    # starts at the end
    ("bytes=2000-3000", 1000),                # starts after it
    ("bytes=-0", 1000),                       # the last 0 bytes
    ("bytes=0-", 0),                          # an empty file has no byte 0
    ("bytes=-5", 0),
])
def test_byte_range_unsatisfiable(header, size):
    with pytest.raises(ValueError):
        http_util.byte_range(header, size)
