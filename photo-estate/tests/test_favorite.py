#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""Favourites set in the app (POST /api/favorite), end to end on the demo library: the photo's row says
so at once, it becomes its month's cover, the count and the favourites-only list follow, a phone's
change feed carries it, a reload of the catalog keeps it, and taking it back puts everything as it was.
Starts the real app (`embed.py serve --demo`)."""
import json, os, socket, subprocess, time, urllib.error, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    PORT = s.getsockname()[1]
BASE = f"http://127.0.0.1:{PORT}"
VIEW = "dedupe=0&shots=1"


def http(path, body=None, expect=200):
    req = urllib.request.Request(BASE + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            code, got = r.status, r.read()
    except urllib.error.HTTPError as e:
        code, got = e.code, e.read()
    assert code == expect, f"{path}: {code}, expected {expect}: {got[:300]!r}"
    return json.loads(got) if got else None


def month(key):
    return next(m for m in http("/api/periods?" + VIEW)["months"] if m["key"] == key)


app = subprocess.Popen(["uv", "run", "--quiet", str(ROOT / "embed.py"), "serve", "--demo", "--port", str(PORT), "--no-pregenerate"],
                       env={**os.environ, "HF_HUB_OFFLINE": "1"}, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
try:
    for _ in range(600):
        try:
            before = http("/api/info")["fav"]
            break
        except (urllib.error.URLError, ConnectionError):
            assert app.poll() is None, app.stdout.read().decode()[-2000:]
            time.sleep(0.2)

    for m in sorted(http("/api/periods?" + VIEW)["months"], key=lambda m: -m["n"]):   # a month with no favourite yet
        rows = http(f"/api/search?{VIEW}&limit={m['n']}&cursor={m['at']}")["results"]
        if m["n"] > 1 and not any(r["fav"] for r in rows):
            break
    pick = next(r for r in rows if r["sha"] != m["cover"] and not r["fav"])
    other = next(r for r in http(f"/api/search?{VIEW}&limit=500")["results"] if not r["local"].startswith(m["key"]))
    while not http("/api/sync?since=0&limit=10000").get("epoch"):
        time.sleep(0.2)
    feed = http("/api/sync?since=0&limit=10000")

    for bad in ({}, {"sha": pick["sha"]}, {"sha": pick["sha"], "fav": "yes"}, {"sha": "0" * 64, "fav": True}, [1]):
        http("/api/favorite", bad, expect=400)

    got = http("/api/favorite", {"sha": pick["sha"], "fav": True})
    assert got == {"sha": pick["sha"], "fav": True, "favourites": before + 1}, got
    assert month(m["key"])["cover"] == pick["sha"], "a month's favourite is its cover"
    assert http("/api/info")["fav"] == before + 1
    assert pick["sha"] in [r["sha"] for r in http(f"/api/search?{VIEW}&fav=1&limit=500")["results"]]
    assert next(r for r in http(f"/api/search?{VIEW}&limit={m['n']}&cursor={m['at']}")["results"] if r["sha"] == pick["sha"])["fav"]
    news = http(f"/api/sync?epoch={feed['epoch']}&since={feed['since']}&after=~")
    assert [(r["sha"], r["fav"]) for r in news["photos"]] == [(pick["sha"], True)], "a phone hears of it, and of nothing else"
    http("/api/favorite", {"sha": pick["sha"], "fav": True})     # said twice is said once
    assert http("/api/info")["fav"] == before + 1

    # a reload of the catalog (here: another photo is hidden by a change set) reads it back
    total = http("/api/periods?" + VIEW)["matching"]
    cs = http("/api/changes", {"title": "test", "items": [{"sha": other["sha"]}]}, expect=201)
    http(f"/api/changes/{cs['id']}/apply", {})
    for _ in range(300):
        if http("/api/periods?" + VIEW)["matching"] == total - 1:
            break
        time.sleep(0.2)
    else:
        raise AssertionError("the catalog was not reloaded")
    assert month(m["key"])["cover"] == pick["sha"] and http("/api/info")["fav"] == before + 1, "kept across a reload"

    assert http("/api/favorite", {"sha": pick["sha"], "fav": False})["favourites"] == before
    assert month(m["key"])["cover"] == m["cover"], "taken back: the cover is the one it was"
    assert pick["sha"] not in [r["sha"] for r in http(f"/api/search?{VIEW}&fav=1&limit=500")["results"]]
    print("favourites: ok")
finally:
    app.terminate()
    try:
        app.wait(10)
    except subprocess.TimeoutExpired:
        app.kill()
