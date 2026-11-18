#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""/api/layout, end to end on the demo library: the Library's photos' shapes, a byte each, in the order
/api/search pages through them, for every order and view option the page can ask for. The page lays a
month out from these before it knows the photos, so a byte in the wrong place is a tile of the wrong
shape. Starts the real app (`embed.py serve --demo`)."""
import json, os, socket, subprocess, time, urllib.error, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    PORT = s.getsockname()[1]
BASE = f"http://127.0.0.1:{PORT}"


def get(path, headers=None):
    with urllib.request.urlopen(urllib.request.Request(BASE + path, headers=headers or {}), timeout=60) as r:
        return r.read(), r.headers


app = subprocess.Popen(["uv", "run", "--quiet", str(ROOT / "embed.py"), "serve", "--demo", "--port", str(PORT), "--no-pregenerate"],
                       env={**os.environ, "HF_HUB_OFFLINE": "1"}, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
try:
    for _ in range(600):
        try:
            get("/api/info")
            break
        except (urllib.error.URLError, ConnectionError):
            assert app.poll() is None, app.stdout.read().decode()[-2000:]
            time.sleep(0.2)

    for view in ("dedupe=1&shots=0", "dedupe=0&shots=1", "dedupe=1&shots=0&sort=old", "dedupe=1&shots=0&sort=best-month"):
        shapes, headers = get("/api/layout?" + view)
        periods = json.loads(get("/api/periods?" + view)[0])
        assert len(shapes) == periods["matching"], f"{view}: {len(shapes)} shapes for {periods['matching']} photos"
        rows, cursor = [], 0
        while cursor is not None:
            page = json.loads(get(f"/api/search?{view}&limit=40&cursor={cursor}")[0])
            rows += page["results"]
            cursor = page["next"]
        assert len(rows) == len(shapes), f"{view}: the list has {len(rows)} photos"
        for i, (r, b) in enumerate(zip(rows, shapes)):   # as search.html reads a byte back
            want, got = min(2.6, max(0.5, r["ar"])), 0.5 + b * 2.1 / 255
            assert abs(want - got) <= 2.1 / 255 / 2 + 1e-4, f"{view}: photo {i} is {want}, its shape says {got:.3f}"
        for m in periods["months"]:                      # a month's card says where its photos start
            assert (rows[m["at"]].get("local") or rows[m["at"]]["taken"]).startswith(m["key"]), (view, m["key"])
        assert headers["ETag"], "the page asks again with If-None-Match"
        try:
            get("/api/layout?" + view, {"If-None-Match": headers["ETag"]})
            raise AssertionError("an unchanged layout is sent again")
        except urllib.error.HTTPError as e:
            assert e.code == 304
    print("layout: ok")
finally:
    app.terminate()
    try:
        app.wait(10)
    except subprocess.TimeoutExpired:
        app.kill()
