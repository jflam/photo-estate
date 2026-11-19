#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""How the photo app treats a connection, with the real app (`embed.py serve --demo`, with an inbox):

- an upload from a client that says `Connection: close` (urllib, and anything on HTTP/1.0) ends the
  connection, rather than the server waiting on a socket the client is closing;
- an upload on a kept-alive connection leaves it open for the next request, unless the server answered
  before reading the body (a refused upload), when it must close: the body is still on the wire;
- a client that resets a kept-alive connection while the server waits for its next request (as a phone
  that sleeps, or a proxy, does) is not an error: nothing is printed, and the server carries on;
- many uploads the way the phone and tools/bench_ingest.py send them print no traceback."""
import os, socket, struct, subprocess, tempfile, time, urllib.error, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    PORT = s.getsockname()[1]
BODY = os.urandom(200_000)


def connect():
    s = socket.create_connection(("127.0.0.1", PORT))
    s.settimeout(3)
    return s


def request(s, method, path, body=b"", headers=""):
    """Send one request on `s` and read its whole answer. Returns (status line, server closed after it)."""
    s.sendall(f"{method} {path} HTTP/1.1\r\nHost: test\r\nContent-Length: {len(body)}\r\n{headers}\r\n".encode() + body)
    got = b""
    while b"\r\n\r\n" not in got:
        got += s.recv(65536)
    head, rest = got.split(b"\r\n\r\n", 1)
    n = next(int(l.split(b":")[1]) for l in head.split(b"\r\n") if l.lower().startswith(b"content-length"))
    while len(rest) < n:
        rest += s.recv(65536)
    return head.split(b"\r\n")[0].decode()


def closed(s) -> bool:
    """Whether the server has ended the connection. A server that closes with part of a request unread
    (a refused upload's body) ends it with a reset rather than a clean close: either counts."""
    try:
        return s.recv(1) == b""
    except ConnectionResetError:
        return True
    except socket.timeout:
        return False


inbox = tempfile.mkdtemp()
app = subprocess.Popen(["uv", "run", "--quiet", str(ROOT / "embed.py"), "serve", "--demo", "--port", str(PORT),
                        "--no-pregenerate", "--inbox", inbox],
                       env={**os.environ, "HF_HUB_OFFLINE": "1"}, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
try:
    for _ in range(600):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/info", timeout=5).read()
            break
        except (urllib.error.URLError, ConnectionError):
            assert app.poll() is None, app.stdout.read().decode()[-2000:]
            time.sleep(0.2)

    s = connect()
    assert request(s, "POST", "/api/upload/2026/11/15/A0001.JPG", BODY, "Connection: close\r\n").endswith("201 Created")
    assert closed(s), "the client said Connection: close, but the server kept the connection open"
    s.close()

    s = connect()
    assert request(s, "POST", "/api/upload/2026/11/15/A0002.JPG", BODY).endswith("201 Created")
    assert not closed(s), "a kept-alive upload that was read in full should leave the connection open"
    assert request(s, "GET", "/api/info").endswith("200 OK"), "the kept-alive connection should take another request"
    s.close()

    s = connect()
    assert request(s, "POST", "/api/upload/../escape.JPG", BODY).split()[1] == "400"
    assert closed(s), "a refused upload leaves its body on the wire, so the connection must close"
    s.close()

    s = connect()
    assert request(s, "POST", "/api/upload/2026/11/15/A0003.JPG", BODY).endswith("201 Created")
    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    s.close()   # a reset, not a close, while the server waits for the next request
    time.sleep(0.5)
    assert urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/info", timeout=5).status == 200

    for i in range(30):   # as the phone's tools send them: urllib, Connection: close, the answer's body unread
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/upload/2026/11/15/U{i:04d}.JPG", data=BODY, method="POST")
        with urllib.request.urlopen(req, timeout=60) as r:
            assert r.status == 201
    time.sleep(1)
finally:
    app.terminate()
    out = app.communicate(timeout=30)[0].decode()

assert "Exception occurred" not in out and "Traceback" not in out, out[-3000:]
print("connections: ok")
