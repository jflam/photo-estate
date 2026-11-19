#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""Albums (changes.py), end to end on the demo library: an album of chosen photos in sections, a live
album that is a search, favouriting and downloading an album's photos, putting an album up for hiding
(it becomes a change set, reviewed and applied as any other) and taking it down again; and the same
through the MCP tools an agent in a chat client uses. Starts the real app (`embed.py serve --demo`)."""
import io, json, os, socket, subprocess, time, urllib.error, urllib.request, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    PORT = s.getsockname()[1]
BASE = f"http://127.0.0.1:{PORT}"
ids = iter(range(1, 10**6))


def http(path, body=None, expect=200, raw=False):
    req = urllib.request.Request(BASE + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            code, got, headers = r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        code, got, headers = e.code, e.read(), e.headers
    assert code == expect, f"{path}: {code}, expected {expect}: {got[:300]!r}"
    return (got, headers) if raw else json.loads(got) if got else None


def tool(name, args=None, error=False):
    got = http("/mcp", {"jsonrpc": "2.0", "id": next(ids), "method": "tools/call", "params": {"name": name, "arguments": args or {}}})["result"]
    assert got["isError"] == error, got
    return got["content"][0]["text"] if error else got["structuredContent"]


def settled():
    for _ in range(300):
        if not http("/api/albums")["busy"]:
            return
        time.sleep(0.2)
    raise AssertionError("the app never finished taking in the change")


app = subprocess.Popen(["uv", "run", "--quiet", str(ROOT / "embed.py"), "serve", "--demo", "--port", str(PORT), "--no-pregenerate"],
                       env={**os.environ, "HF_HUB_OFFLINE": "1"}, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
try:
    for _ in range(600):
        try:
            total = http("/api/info")["total"]
            break
        except (urllib.error.URLError, ConnectionError):
            assert app.poll() is None, app.stdout.read().decode()[-2000:]
            time.sleep(0.2)
    rows = http("/api/search?dedupe=0&shots=1&limit=500")["results"]
    by_year = {}
    for r in rows:
        by_year.setdefault(r["local"][:4], []).append(r["sha"])
    y1, y2 = sorted(by_year, key=lambda y: -len(by_year[y]))[:2]

    # an album of chosen photos, in sections, in the order given
    assert http("/api/albums") == {"sets": [], "hidden": 0, "busy": False}
    http("/api/albums", {"title": " "}, expect=400)
    a = http("/api/albums", {"title": "Two years", "prompt": "three of each"}, expect=201)
    assert (a["op"], a["kind"], a["state"], a["n"]) == ("view", "list", "empty", 0)
    aid = a["id"]
    got = http(f"/api/albums/{aid}/items", {"items": [{"sha": s, "section": y2, "reason": "second"} for s in by_year[y2][:3]]})
    assert got["added"] == 3 and got["state"] == "album"
    http(f"/api/albums/{aid}/items", {"items": [{"sha": s, "section": y1} for s in by_year[y1][:3]] + [{"sha": "0" * 64}]})
    http(f"/api/albums/{aid}/items", {"items": [{"sha": by_year[y2][3], "section": y2}]})     # a section's photos stay together
    p = http(f"/api/albums/{aid}/photos?limit=4")
    assert p["matching"] == 7 and p["next"] == 4 and p["sections"] == [{"title": y2, "n": 4}, {"title": y1, "n": 3}], p["sections"]
    assert [r["sha"] for r in p["results"]] == by_year[y2][:4] and p["results"][0]["reason"] == "second"
    assert {"ar", "local", "fav", "section"} <= set(p["results"][0]), "a photo as /api/search gives it"
    assert [r["sha"] for r in http(f"/api/albums/{aid}/photos?cursor=4")["results"]] == by_year[y1][:3]
    assert http("/api/changes")["sets"] == [], "an album to look at is not a change set"
    assert [s["id"] for s in http("/api/albums")["sets"]] == [aid]
    # its front is a favourite of it if it has one (as a month's is), whichever photo that is
    front = lambda: http("/api/albums")["sets"][-1]["cover"][0]
    mine = by_year[y2][:4] + by_year[y1][:3]
    was, favs0 = front(), [r["sha"] for r in http(f"/api/albums/{aid}/photos")["results"] if r["fav"]]
    assert was in mine and (was in favs0 if favs0 else True)
    for sha in favs0:
        http("/api/favorite", {"sha": sha, "fav": False})
    pick = next(s for s in reversed(mine) if s != front())
    http("/api/favorite", {"sha": pick, "fav": True})
    assert front() == pick, "the one favourite in the album is on its front"
    http("/api/favorite", {"sha": pick, "fav": False})
    assert front() != pick
    for sha in favs0:
        http("/api/favorite", {"sha": sha, "fav": True})
    http(f"/api/albums/{aid}/apply", {}, expect=409)       # not up for hiding
    assert http(f"/api/albums/{aid}/rename", {"title": "Two good years"})["title"] == "Two good years"
    got = http(f"/api/albums/{aid}/rename", {"prompt": "the best of each"})       # its note alone; the title stays
    assert (got["title"], got["prompt"]) == ("Two good years", "the best of each")
    assert http(f"/api/albums/{aid}/rename", {"title": " ", "prompt": ""})["title"] == "Two good years", "an album keeps a title"
    assert http(f"/api/albums/{aid}/photos?limit=1")["album"]["prompt"] == "", "a note can be taken away"

    # every photo in it a favourite, and back
    fav0 = http("/api/info")["fav"]
    already = sum(r["fav"] for r in http(f"/api/albums/{aid}/photos")["results"])
    got = http(f"/api/albums/{aid}/favorite", {"fav": True})
    assert got["changed"] == 7 - already and http("/api/info")["fav"] == fav0 + 7 - already
    assert all(r["fav"] for r in http(f"/api/albums/{aid}/photos")["results"])
    http(f"/api/albums/{aid}/favorite", {"fav": "yes"}, expect=400)

    # its original files, as one zip
    body, headers = http(f"/api/albums/{aid}/download", raw=True)
    assert headers["Content-Type"] == "application/zip" and "Two%20good%20years.zip" in headers["Content-Disposition"]
    z = zipfile.ZipFile(io.BytesIO(body))
    assert len(z.namelist()) == 7 and z.testzip() is None and len(set(z.namelist())) == 7
    http("/api/albums/999/download", expect=404)

    # a live album: a search, and whatever it finds
    http("/api/albums", {"title": "bad", "query": {"people": ["Nobody Atall"]}}, expect=400)
    live = http("/api/albums", {"title": y1, "query": {"date_from": y1, "date_to": y1}}, expect=201)
    assert (live["kind"], live["n"], live["state"]) == ("live", len(by_year[y1]), "album")
    assert {r["sha"] for r in http(f"/api/albums/{live['id']}/photos?limit=500")["results"]} == set(by_year[y1])
    http(f"/api/albums/{live['id']}/items", {"items": [{"sha": rows[0]["sha"]}]}, expect=409)
    assert http(f"/api/albums/{live['id']}/query", {"query": {"date_from": y2, "date_to": y2}})["n"] == len(by_year[y2])

    # an album of everything a search finds, as the page makes one: the whole answer of /api/search, a page at
    # a time, becomes a fixed album of those photos in that order (not a live one, and not up for hiding)
    found, cursor, first = [], 0, None
    while cursor is not None:
        d = http(f"/api/search?q=latte+art&dedupe=1&shots=0&limit=4&cursor={cursor}")
        first = first or d
        found += [r["sha"] for r in d["results"]]
        cursor = d["next"]
    assert first["next"] == 4 and len(first["results"]) == 4, "the answer comes in pages"
    assert len(found) == first["matching"] == 6 and len(set(found)) == 6, "the pages together are the whole answer, each photo once"
    made = http("/api/albums", {"title": "latte art", "prompt": "From the search “latte art”", "items": [{"sha": s} for s in found]}, expect=201)
    assert (made["op"], made["kind"], made["state"], made["n"], made["added"], made["unknown"]) == ("view", "list", "album", 6, 6, 0), made
    p = http(f"/api/albums/{made['id']}/photos?limit=500")
    assert [r["sha"] for r in p["results"]] == found and p["matching"] == 6 and p["album"]["title"] == "latte art"
    assert http("/api/search?q=kangaroo&limit=4")["matching"] == 0, "a search that finds nothing has nothing to make an album of"
    assert http("/api/changes")["sets"] == []
    http(f"/api/albums/{made['id']}/discard", {})

    # up for hiding: the album is a change set now, reviewed and applied as one; then a plain album again
    got = http(f"/api/albums/{aid}/hide", {})
    assert (got["op"], got["state"], got["todo"]) == ("hide", "review", 7)
    assert [s["id"] for s in http("/api/changes")["sets"]] == [aid]
    keep = by_year[y2][0]
    http(f"/api/changes/{aid}/decide", {"shas": [keep], "decision": "reject"})
    assert http(f"/api/changes/{aid}/apply", {})["hid"] == 6
    settled()
    assert http("/api/info")["total"] == total - 6
    p = http(f"/api/albums/{aid}/photos")
    assert p["matching"] == 7 and sum(bool(r.get("hidden")) for r in p["results"]) == 6, "the album still shows what it hides"
    http(f"/api/albums/{aid}/view", {}, expect=409)        # it is hiding photos
    http(f"/api/changes/{aid}/revert", {})
    settled()
    assert http(f"/api/albums/{aid}/view", {})["state"] == "album" and http("/api/changes")["sets"] == []
    assert http("/api/info")["total"] == total
    got = http(f"/api/albums/{live['id']}/hide", {})       # a live one is fixed as what it finds now
    assert (got["kind"], got["op"], got["n"]) == ("list", "hide", len(by_year[y2]))
    http(f"/api/albums/{live['id']}/discard", {})

    # the same, as an agent does it
    assert "no quality scores" in tool("search_photos", {"order": "best"}, error=True)   # (the demo has no scorers)
    made = tool("create_album", {"title": "By year", "prompt": "two of each of two years"})
    mid = made["album"]
    assert made["view"] == f"/?album={mid}" and made["photos"] == 0
    for y in (y1, y2):
        r = tool("amend_album", {"album": mid, "add_search": {"date_from": y, "date_to": y, "distinct": True, "max": 2},
                                 "section": y, "reason": f"from {y}"})
        assert r["added"] == 2 and r["view"] == f"/?album={mid}" and "review" not in r
    got = tool("get_album", {"album": mid})
    assert got["sections"] == [{"title": y1, "n": 2}, {"title": y2, "n": 2}] and got["photos"][0]["section"] == y1
    assert got["photos"][0]["reason"] == f"from {y1}" and got["photos"][0]["date"].startswith(y1)
    assert tool("amend_album", {"album": mid, "title": "By year, briefly"})["title"] == "By year, briefly"
    lv = tool("create_album", {"title": "Live", "search": {"date_from": y1, "date_to": y1}})
    assert lv["live"] is True and lv["photos"] == len(by_year[y1])
    assert "live album" in tool("amend_album", {"album": lv["album"], "add_ids": [rows[0]["sha"]]}, error=True)
    listed = {x["album"]: x for x in tool("list_albums")["albums"]}
    assert listed[mid]["photos"] == 4 and listed[aid]["title"] == "Two good years" and "up_for_hiding" not in listed[mid]
    hid = tool("propose_hiding", {"album": mid})
    assert hid["up_for_hiding"] and hid["review"] == f"/changes/{mid}" and hid["hidden_now"] == 0
    assert [s["change_set"] for s in tool("list_change_sets")["change_sets"]] == [mid]
    assert "no such album" in tool("get_album", {"album": 999}, error=True)
    print("albums: ok")
finally:
    app.terminate()
    try:
        app.wait(10)
    except subprocess.TimeoutExpired:
        app.kill()
