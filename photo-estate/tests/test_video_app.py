"""Videos end to end with the search model: tools/video_demo.py's library (the demo with two videos and two
Live Photos, run through ingest, link, the pairing and `embed.py run`), served by the real app over HTTP.
A typed search finds a video by the scene that matches and says where it is (`at`); kind=video and
is:video; stills carry their clip and clips are in no list; posters; GET /video/<sha> with Range; the sync
feed; "more like this" from a video; the MCP search's kind; `embed.py stats`.

  uv run pytest tests/test_video_app.py        # about a minute: builds the library, then starts the app
"""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

pytestmark = [pytest.mark.slow, pytest.mark.server, pytest.mark.model]

ROOT = Path(__file__).resolve().parent.parent          # photo-estate/
sys.path.insert(0, str(ROOT))
ENV = {**os.environ, "HF_HUB_OFFLINE": "1"}
COAST, LIGHTHOUSE = "Coast to mountains.mp4", "Lighthouse.mov"


def video_demo():
    spec = importlib.util.spec_from_file_location("video_demo", ROOT / "tools" / "video_demo.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def library(tmp_path_factory):
    root = video_demo().build(tmp_path_factory.mktemp("video-demo") / "lib")
    con = sqlite3.connect(root / "estate.db")
    by_name = {fn: sha for fn, sha in con.execute("SELECT o.filename, o.sha256 FROM occurrences o")}
    con.close()
    return root, by_name


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class App:
    def __init__(self, port: int):
        self.base = f"http://127.0.0.1:{port}"

    def get(self, path: str, headers=None, body=None) -> tuple[int, dict, bytes]:
        req = urllib.request.Request(self.base + path, headers=headers or {},
                                     data=None if body is None else json.dumps(body).encode())
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
        except urllib.error.HTTPError as e:
            return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()

    def json(self, path: str, body=None):
        code, _, got = self.get(path, {"Content-Type": "application/json"} if body is not None else None, body)
        assert code == 200, (path, code, got[:300])
        return json.loads(got)

    def search(self, query: str) -> list[dict]:
        return self.json(f"/api/search?dedupe=0&limit=500&{query}")["results"]


@pytest.fixture(scope="module")
def app(library):
    root, _ = library
    port = free_port()
    proc = subprocess.Popen(["uv", "run", "--quiet", str(ROOT / "embed.py"), "--db", str(root / "estate.db"), "serve",
                             "--store", str(root / "store"), "--port", str(port), "--no-pregenerate"],
                            cwd=ROOT, env=ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    a = App(port)
    try:
        deadline = time.time() + 300
        while True:                       # the app answers...
            try:
                if a.get("/api/info")[0] == 200:
                    break
            except (urllib.error.URLError, ConnectionError):
                pass
            assert proc.poll() is None, proc.stdout.read().decode()[-2000:]   # type: ignore[union-attr]
            assert time.time() < deadline, "the app never answered"
            time.sleep(0.2)
        while a.get("/api/search?q=sea&limit=1")[0] == 503:   # ...and the text model is loaded
            assert time.time() < deadline, "the text model never loaded"
            time.sleep(0.5)
        yield a
    finally:
        proc.terminate()
        try:
            proc.wait(20)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_info_and_kind(app, library):
    _, sha = library
    assert app.json("/api/info")["videos"] == 2
    got = {r["sha"]: r for r in app.search("kind=video")}
    assert set(got) == {sha[COAST], sha[LIGHTHOUSE]}
    coast, light = got[sha[COAST]], got[sha[LIGHTHOUSE]]
    assert coast["kind"] == light["kind"] == "video"
    assert coast["dur"] == pytest.approx(12.0, abs=0.3) and light["dur"] == pytest.approx(5.0, abs=0.3)
    assert light["ar"] < 1 < coast["ar"], "the lighthouse is portrait"
    assert {r["sha"] for r in app.search("q=is:video")} == set(got)
    photos = app.search("kind=photo")
    assert photos and not any(r.get("kind") for r in photos) and not set(got) & {r["sha"] for r in photos}


@pytest.mark.parametrize("words,lo,hi", [("sunset over the sea", 0, 4), ("snowy mountain", 4, 8), ("waterfall", 8, 12)])
def test_a_search_finds_the_video_at_the_scene(app, library, words, lo, hi):
    _, sha = library
    rows = app.search("q=" + urllib.parse.quote(words))
    coast = next((r for r in rows if r["sha"] == sha[COAST]), None)
    assert coast is not None, f"{words!r} did not find the video among {len(rows)} results"
    assert lo <= coast["at"] < hi, (words, coast["at"])
    assert all("at" not in r for r in rows if r.get("kind") != "video")


def test_live_photos(app, library):
    _, sha = library
    every = app.search("")
    shown = {r["sha"]: r for r in every}
    for stem in ("IMG_9001", "IMG_9002"):
        still = next(s for fn, s in sha.items() if fn.startswith(stem) and not fn.endswith(".MOV"))
        assert shown[still]["live"] == sha[f"{stem}.MOV"]
        assert sha[f"{stem}.MOV"] not in shown
    assert not {sha["IMG_9001.MOV"], sha["IMG_9002.MOV"]} & {r["sha"] for r in app.search("kind=video")}
    rows = {r["sha"]: r for r in app.json("/api/sync?since=0&limit=10000")["photos"]}
    assert rows[sha[COAST]]["kind"] == "video" and rows[sha[COAST]]["dur"] == pytest.approx(12.0, abs=0.3)
    assert sha["IMG_9001.MOV"] not in rows
    assert sum("live" in r for r in rows.values()) == 2


def test_posters(app, library):
    _, sha = library
    for v in (sha[COAST], sha[LIGHTHOUSE]):
        code, h, body = app.get(f"/thumb/{v}")
        assert (code, h["content-type"], body[8:12]) == (200, "image/webp", b"WEBP")
        code, h, body = app.get(f"/preview/{v}")
        assert (code, h["content-type"], body[:2]) == (200, "image/jpeg", b"\xff\xd8")
        assert app.get(f"/original/{v}")[0] == 404


def test_video_with_ranges(app, library):
    root, sha = library
    v = sha[COAST]
    data = (root / "store" / v[:2] / v[2:4] / f"{v}.mp4").read_bytes()
    size = len(data)
    code, h, body = app.get(f"/video/{v}")
    assert (code, h["accept-ranges"], int(h["content-length"]), body == data) == (200, "bytes", size, True)
    code, h, body = app.get(f"/video/{v}", {"Range": "bytes=0-1"})
    assert (code, h["content-range"], body) == (206, f"bytes 0-1/{size}", data[:2])
    code, h, body = app.get(f"/video/{v}", {"Range": "bytes=-1000"})
    assert (code, body) == (206, data[-1000:])
    mid = size // 2
    code, h, body = app.get(f"/video/{v}", {"Range": f"bytes={mid}-{mid + 4095}"})
    assert (code, h["content-range"], body) == (206, f"bytes {mid}-{mid + 4095}/{size}", data[mid:mid + 4096])
    code, h, _ = app.get(f"/video/{v}", {"Range": f"bytes={size + 10}-"})
    assert (code, h["content-range"]) == (416, f"bytes */{size}")
    code, h, _ = app.get(f"/video/{sha[LIGHTHOUSE]}")
    assert (code, h["content-type"]) == (200, "video/mp4"), "a .mov goes out as video/mp4"
    assert app.get(f"/video/{sha['IMG_9001.MOV']}")[0] == 200, "a Live Photo's clip"
    still = next(s for fn, s in sha.items() if fn.startswith("IMG_9001") and not fn.endswith(".MOV"))
    assert app.get(f"/video/{still}")[0] == 404


def test_similar_to_a_video(app, library):
    """More like this from the coast video: photos, the demo ones it was made from among the nearest."""
    _, sha = library
    rows = app.json(f"/api/similar?sha={sha[COAST]}&limit=10")["results"]
    assert rows and sha[COAST] not in {r["sha"] for r in rows}
    assert any(r.get("kind") != "video" for r in rows)


def test_mcp_search_takes_kind(app, library):
    _, sha = library
    got = app.json("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": "search_photos", "arguments": {"kind": "video", "limit": 10}}})
    photos = got["result"]["structuredContent"]["photos"]
    assert {p["id"] for p in photos} == {sha[COAST], sha[LIGHTHOUSE]}
    assert all(p["kind"] == "video" and p["seconds"] > 0 for p in photos)
    got = app.json("/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                            "params": {"name": "search_photos", "arguments": {"kind": "photo", "limit": 500}}})
    rows = got["result"]["structuredContent"]["photos"]
    assert sum(1 for p in rows if p.get("live_photo")) == 2 and not any("kind" in p for p in rows)


def test_stats(library):
    root, _ = library
    r = subprocess.run(["uv", "run", "--quiet", str(ROOT / "embed.py"), "--db", str(root / "estate.db"), "stats"],
                       cwd=ROOT, env=ENV, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    assert "videos in catalog: 4 (2 are Live Photo clips, searched by their stills)" in r.stdout, r.stdout
    assert " video frames" in r.stdout, r.stdout
