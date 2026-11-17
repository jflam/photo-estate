#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""The MCP endpoint (estate_mcp.py), end to end on the demo library, as an agent uses it: the
handshake, the tool list, finding photos, proposing a change set and amending it, and reading back
what a person kept. Checks that an agent can propose but not apply, and that no tool result carries
anything but ids, scores and facts. Starts the real app (`embed.py serve --demo`)."""
import json, os, socket, subprocess, sys, time, urllib.error, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    PORT = s.getsockname()[1]
BASE = f"http://127.0.0.1:{PORT}"
ids = iter(range(1, 10**6))


def http(path, body=None, expect=200, headers=None, raw=None, timeout=60):
    data = raw if raw is not None else None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json",
                                 "Accept": "application/json, text/event-stream", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            code, got = r.status, r.read()
    except urllib.error.HTTPError as e:
        code, got = e.code, e.read()
    assert code == expect, f"{path}: {code}, expected {expect}: {got[:300]!r}"
    return json.loads(got) if got else None


def rpc(method, params=None, **kw):
    got = http("/mcp", {"jsonrpc": "2.0", "id": next(ids), "method": method, **({"params": params} if params is not None else {})}, **kw)
    assert got["jsonrpc"] == "2.0"
    return got


def tool(name, args=None, error=False, **kw):
    got = rpc("tools/call", {"name": name, "arguments": args or {}}, **kw)["result"]
    assert got["isError"] == error, got
    if error:
        return got["content"][0]["text"]
    assert json.loads(got["content"][0]["text"]) == got["structuredContent"]
    return got["structuredContent"]


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

    # the handshake, and what is refused
    init = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}})["result"]
    assert init["protocolVersion"] == "2025-06-18" and "tools" in init["capabilities"] and init["serverInfo"]["name"] == "photo-estate"
    assert "never see a photo" in init["instructions"]
    assert rpc("initialize", {"protocolVersion": "1999-01-01"})["result"]["protocolVersion"] == "2025-06-18"
    assert http("/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"}, expect=202) is None
    assert rpc("ping")["result"] == {}
    assert rpc("resources/list")["error"]["code"] == -32601
    assert rpc("tools/call", {"name": "apply_change_set"})["error"]["code"] == -32602
    http("/mcp", expect=405)
    http("/mcp", raw=b"{nope", expect=400)
    http("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"Origin": "https://elsewhere.example"}, expect=403)
    assert rpc("ping", headers={"Origin": BASE})["result"] == {}
    batch = http("/mcp", [{"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"jsonrpc": "2.0", "method": "notifications/initialized"}])
    assert batch == [{"jsonrpc": "2.0", "id": 1, "result": {}}]

    tools = {t["name"]: t for t in rpc("tools/list")["result"]["tools"]}
    assert set(tools) == {"library_overview", "search_photos", "similar_photos", "get_photos", "list_change_sets",
                          "get_change_set", "create_change_set", "amend_change_set"}
    assert all(t["inputSchema"]["type"] == "object" and t["description"] for t in tools.values())
    assert not [n for n in tools if "apply" in n or "revert" in n or "hide" in n], "an agent proposes; a person applies"
    assert {n for n, t in tools.items() if not t["annotations"]["readOnlyHint"]} == {"create_change_set", "amend_change_set"}

    # finding: every match is summarised, a page of them is listed, and a row is facts only
    over = tool("library_overview")
    assert over["photos"] == total and over["hidden"] == 0 and sum(over["by_year"].values()) == total
    got = tool("search_photos", {"limit": 5})
    assert got["summary"]["matching"] == total and len(got["photos"]) == 5 and got["next_offset"] == 5
    assert sum(got["summary"]["by_year"].values()) == total and "score_at_rank" not in got["summary"]
    first = got["photos"][0]
    allowed = {"id", "date", "place", "people", "albums", "event", "event_id", "trip", "camera", "favorite", "screenshot",
               "source_path", "hidden", "score", "faces", "named_faces"}
    assert set(first) <= allowed and len(first["id"]) == 64 and "score" not in first
    assert [p["date"] for p in got["photos"]] == sorted((p["date"] for p in got["photos"]), reverse=True), "newest first"
    year = first["date"][:4]
    in_year = tool("search_photos", {"date_from": year, "date_to": year, "limit": 200})
    assert in_year["summary"]["matching"] == over["by_year"][year] and all(p["date"].startswith(year) for p in in_year["photos"])
    assert tool("search_photos", {"named_people": "some"})["summary"]["matching"] \
        + tool("search_photos", {"named_people": "none"})["summary"]["matching"] == total
    assert "library_overview lists the names" in tool("search_photos", {"people": ["Nobody Atall"]}, error=True)

    seed = first["id"]
    like = tool("similar_photos", {"id": seed, "limit": 200})
    assert like["summary"]["matching"] == total - 1 and seed not in {p["id"] for p in like["photos"]}
    scores = [p["score"] for p in like["photos"]]
    assert scores == sorted(scores, reverse=True) and like["summary"]["score_at_rank"]["1"] == scores[0]
    close = tool("similar_photos", {"id": seed, "min_score": scores[9]})
    assert close["summary"]["matching"] >= 10 and all(p["score"] >= scores[9] for p in close["photos"])
    assert "no photo has that id" in tool("similar_photos", {"id": "f" * 64}, error=True)
    assert "missing argument" in tool("similar_photos", {}, error=True)
    two = tool("get_photos", {"ids": [seed, "0" * 64]})["photos"]
    assert two[0]["id"] == seed and two[1] == {"id": "0" * 64, "error": "no such photo"}

    # proposing: a set, photos by search and by id, some taken out again; the library does not change
    assert tool("list_change_sets") == {"change_sets": []}
    made = tool("create_change_set", {"title": "Like the first one", "prompt": "the thirty most like it"})
    sid = made["change_set"]
    assert made["review"] == f"/changes/{sid}" and made["photos"] == 0
    assert "needs a title" in tool("create_change_set", {"title": " "}, error=True)
    r = tool("amend_change_set", {"change_set": sid, "add_search": {"like": seed, "max": 30}, "reason": "looks like the first"})
    assert (r["added"], r["photos"], r["add_search_matched"], r["hidden_now"]) == (30, 30, total - 1, 0)
    top = [p["id"] for p in like["photos"]]
    r = tool("amend_change_set", {"change_set": sid, "add_ids": top[:2] + [top[40], "0" * 64]})
    assert (r["added"], r["already_in_set"], r["unknown_ids"], r["photos"]) == (1, 2, 1, 31)
    r = tool("amend_change_set", {"change_set": sid, "remove_ids": [top[0]], "remove_search": {"like": seed, "max": 3}})
    assert (r["removed"], r["photos"]) == (3, 28), "the three most alike, one of them named twice"
    assert "no such change set" in tool("amend_change_set", {"change_set": 999, "add_ids": [seed]}, error=True)
    assert http("/api/info")["total"] == total and tool("library_overview")["hidden"] == 0, "proposing hides nothing"
    assert tool("list_change_sets")["change_sets"] == [{"change_set": sid, "title": "Like the first one", "state": "review",
                                                         "photos": 28, "hidden_now": 0, "kept_by_person": 0, "review": f"/changes/{sid}"}]

    # a person keeps two and applies; the agent can read which, and no longer finds the hidden ones
    http(f"/api/changes/{sid}/decide", {"shas": top[3:5], "decision": "reject"})
    http(f"/api/changes/{sid}/apply", {})
    for _ in range(300):
        if not http(f"/api/changes/{sid}/check")["busy"]:
            break
        time.sleep(0.2)
    got = tool("get_change_set", {"change_set": sid, "sample": 5})
    assert (got["state"], got["hidden_now"], got["kept_by_person"]) == ("applied", 26, 2)
    assert {p["id"] for p in got["kept"]} == set(top[3:5]) and got["kept"][0]["reason"] == "looks like the first"
    assert len(got["sample"]) == 5 and all(p["hidden"] for p in got["sample"])
    assert tool("search_photos")["summary"]["matching"] == total - 26 == tool("library_overview")["photos"]
    hidden = tool("search_photos", {"hidden_only": True, "limit": 200})
    assert {p["id"] for p in hidden["photos"]} == set(top[5:30]) | {top[40]}
    assert tool("similar_photos", {"id": seed, "limit": 200})["summary"]["matching"] == total - 1 - 26

    # words, if the text model is on this computer (it loads in the background; the demo needs it too)
    try:
        dogs = tool("search_photos", {"query": "a dog", "limit": 3}, timeout=120)
        assert dogs["summary"]["matching"] > 0 and dogs["photos"][0]["score"] == dogs["summary"]["score_at_rank"]["1"] > 0.05
        loose = tool("search_photos", {"query": "a dog", "min_score": -1})
        assert loose["summary"]["matching"] == total - 26, "with a floor below every score, everything shown matches"
        print("mcp: ok")
    except (TimeoutError, urllib.error.URLError):
        print("mcp: ok (searching by words not checked: no text model here)")
finally:
    app.terminate()
    try:
        app.wait(10)
    except subprocess.TimeoutExpired:
        app.kill()
