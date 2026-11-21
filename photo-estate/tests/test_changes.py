#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""Change sets (changes.py), end to end on the demo library: a set of 50 photos is proposed, the ones
to keep are marked bit by bit, it is applied, changed, undone and applied again, and after each step the photos
it hides are in no view (search, More like this, the Library's months, the counts, the copy a phone
keeps) while every other photo still is. Starts the real app (`embed.py serve --demo`) and talks to
it over HTTP, as the page and an agent do."""
import json, os, socket, subprocess, time, urllib.error, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    PORT = s.getsockname()[1]
BASE = f"http://127.0.0.1:{PORT}"


def call(path, body=None, expect=200):
    req = urllib.request.Request(BASE + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            code, raw, ctype = r.status, r.read(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        code, raw, ctype = e.code, e.read(), e.headers.get("Content-Type", "")
    assert code == expect, f"{path}: {code}, expected {expect}: {raw[:300]!r}"
    return json.loads(raw) if ctype.startswith("application/json") else raw


def library(extra=""):
    """Every photo a view shows (no near-identical or screenshot filtering, so it is all of them)."""
    got = call(f"/api/search?limit=500&dedupe=0&shots=1{extra}")
    assert got["next"] is None
    return {r["sha"] for r in got["results"]}, got


def settled(sid):
    """Wait for the app to take in a change, then return the set's check."""
    for _ in range(300):
        c = call(f"/api/changes/{sid}/check")
        if not c["busy"]:
            return c
        time.sleep(0.2)
    raise AssertionError("the app never finished taking in the change")


def pull(phone):
    """One sync, as the iPhone app does it."""
    while True:
        d = call(f"/api/sync?epoch={phone['epoch']}&since={phone['since']}&after={phone['after']}&limit=40")
        if d["epoch"] != phone["epoch"]:
            phone["rows"] = {}
        for sha in d["deleted"]:
            phone["rows"].pop(sha, None)
        for row in d["photos"]:
            phone["rows"][row["sha"]] = row
        phone.update(epoch=d["epoch"], since=d["since"], after=d["after"])
        if not d["more"]:
            return set(phone["rows"])


def views_agree(hidden, everything, phone, sid=None):
    """The photos in `hidden`, and only those, are missing from every view."""
    shown = everything - hidden
    got, page = library()
    assert got == shown and page["total"] == len(shown) and page["matching"] == len(shown)
    assert library("&hidden=1")[0] == hidden, "hidden=1 lists exactly the hidden photos"
    info = call("/api/info")
    assert info["total"] == len(shown) and info["matching"] == len(shown)
    assert sum(info["months"]) <= len(shown)
    assert sum(n for _, n in info["facets"]["country"]) <= len(shown)
    periods = call("/api/periods?dedupe=0&shots=1")
    assert periods["matching"] == len(shown)
    assert not {c["cover"] for k in ("years", "months") for c in periods[k]} & hidden, "no hidden photo on a card"
    assert not {t["cover"] for t in info["trips"]} & hidden
    probe = sorted(shown)[0]
    like = {r["sha"] for r in call(f"/api/similar?sha={probe}&limit=500&dedupe=0")["results"]}
    assert like == shown - {probe}, "More like this finds every shown photo and no hidden one"
    assert pull(phone) == shown, "a phone ends with exactly the shown photos"
    if sid is not None:
        c = call(f"/api/changes/{sid}/check")
        assert c["ok"] and not c["wrong"], c


app = subprocess.Popen(["uv", "run", "--quiet", str(ROOT / "embed.py"), "serve", "--demo", "--port", str(PORT), "--no-pregenerate"],
                       env={**os.environ, "HF_HUB_OFFLINE": "1"}, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
try:
    for _ in range(600):
        try:
            call("/api/info")
            break
        except (urllib.error.URLError, ConnectionError):
            assert app.poll() is None, app.stdout.read().decode()[-2000:]
            time.sleep(0.2)
    for _ in range(300):   # the change feed is built just after startup
        try:
            call("/api/sync?epoch=&since=0&after=&limit=1")
            break
        except AssertionError:
            time.sleep(0.2)

    everything, _ = library()
    assert len(everything) > 60, "the demo library has about a hundred photos"
    phone = {"epoch": "", "since": 0, "after": "", "rows": {}}
    views_agree(set(), everything, phone)
    assert call("/api/changes") == {"sets": [], "hidden": 0, "busy": False}

    # a set of 50 is proposed (with one photo the library does not have): nothing is hidden by that
    fifty = sorted(everything)[:50]
    made = call("/api/changes", {"title": "Fifty to try", "prompt": "hide the first fifty", "items":
                                 [{"sha": s, "score": i / 50, "reason": f"number {i}"} for i, s in enumerate(fifty)]
                                 + [{"sha": "0" * 64}, {"sha": "nope"}]}, expect=201)
    sid = made["id"]
    assert (made["added"], made["unknown"], made["n"], made["pending"], made["state"]) == (50, 2, 50, 50, "review")
    call("/api/changes", {"title": ""}, expect=400)
    call("/api/changes", {"title": "x", "op": "delete"}, expect=400)
    views_agree(set(), everything, phone, sid)

    # marks are saved one request at a time and read back as they were left; marking alone hides nothing
    hide, keep = fifty[:30], fifty[30:35]
    for s in hide[:3]:
        call(f"/api/changes/{sid}/decide", {"shas": [s], "decision": "accept"})
    call(f"/api/changes/{sid}/decide", {"shas": hide[3:], "decision": "accept"})
    call(f"/api/changes/{sid}/decide", {"shas": keep, "decision": "reject"})
    call(f"/api/changes/{sid}/decide", {"shas": keep, "decision": "maybe"}, expect=400)
    d = call(f"/api/changes/{sid}")
    assert (d["accept"], d["reject"], d["pending"], d["applied"], d["todo"]) == (30, 5, 15, 0, 45)
    assert {it["sha"]: it["decision"] for it in d["items"]} == {
        **{s: "accept" for s in hide}, **{s: "reject" for s in keep}, **{s: "pending" for s in fifty[35:]}}
    assert d["items"][0]["reason"].startswith("number ") and "ar" in d["items"][0]
    views_agree(set(), everything, phone, sid)

    # applying hides every photo of the set not marked to keep (looked at or not), everywhere
    r = call(f"/api/changes/{sid}/apply", {})
    assert (r["hid"], r["showed"], r["applied"], r["todo"], r["state"]) == (45, 0, 45, 0, "applied")
    c = settled(sid)
    assert c["ok"] and (c["hidden"], c["visible"]) == (45, 5), c
    now_hidden = set(fifty) - set(keep)
    views_agree(now_hidden, everything, phone, sid)
    assert call(f"/thumb/{hide[0]}")[8:12] == b"WEBP", "a hidden photo can still be looked at by the review page"

    # changing one's mind after applying: three are kept after all
    call(f"/api/changes/{sid}/decide", {"shas": hide[:3], "decision": "reject"})
    d = call(f"/api/changes/{sid}")
    assert (d["todo"], d["state"]) == (3, "review")
    views_agree(now_hidden, everything, phone)    # ...but not until it is applied
    r = call(f"/api/changes/{sid}/apply", {})
    assert (r["hid"], r["showed"]) == (0, 3)
    settled(sid)
    now_hidden -= set(hide[:3])
    views_agree(now_hidden, everything, phone, sid)
    assert call(f"/api/changes/{sid}/apply", {})["hid"] == 0, "applying again changes nothing"

    # a second set that hides one of the same photos, and one more
    extra = sorted(everything)[50]
    other = call("/api/changes", {"title": "Overlap", "items": [hide[5], extra]}, expect=201)["id"]
    call(f"/api/changes/{other}/apply", {})
    settled(other)
    views_agree(now_hidden | {extra}, everything, phone, other)
    assert call("/api/changes")["hidden"] == 43

    # undoing the first set brings back all it hid, except the photo the second set also hides
    call(f"/api/changes/{sid}/discard", {}, expect=409)
    r = call(f"/api/changes/{sid}/revert", {})
    assert (r["showed"], r["applied"], r["state"], r["reject"]) == (42, 0, "reverted", 8)
    settled(sid)
    views_agree({hide[5], extra}, everything, phone, sid)

    # the marks were kept, so it can be applied again; taking a photo out of a set shows it again
    assert call(f"/api/changes/{sid}/apply", {})["hid"] == 42
    settled(sid)
    r = call(f"/api/changes/{sid}/items", {"remove": [hide[10]], "items": [fifty[49], fifty[48]]})
    assert (r["removed"], r["added"], r["n"]) == (1, 0, 49)
    settled(sid)
    views_agree((now_hidden | {extra}) - {hide[10]}, everything, phone, sid)

    # undo both; a set that hides nothing can be forgotten
    call(f"/api/changes/{sid}/revert", {})
    call(f"/api/changes/{other}/revert", {})
    settled(sid)
    views_agree(set(), everything, phone, sid)
    assert call(f"/api/changes/{other}/discard", {}) == {"discarded": other}
    call(f"/api/changes/{other}", expect=404)
    assert [s["id"] for s in call("/api/changes")["sets"]] == [sid]

    # a long set, a stretch at a time: applying through a photo does what the marks say as far as that
    # photo, in the order the page lists them, and leaves the rest as it was
    long = call("/api/changes", {"title": "In sittings", "items": sorted(everything)[60:100]}, expect=201)["id"]
    order = [it["sha"] for it in call(f"/api/changes/{long}")["items"]]
    call(f"/api/changes/{long}/decide", {"shas": [order[2], order[30]], "decision": "reject"})
    call(f"/api/changes/{long}/apply", {"through": order[0][::-1]}, expect=400)
    r = call(f"/api/changes/{long}/apply", {"through": order[9]})
    assert (r["hid"], r["showed"], r["applied"], r["todo"], r["state"]) == (9, 0, 9, 29, "review")
    settled(long)
    first = set(order[:10]) - {order[2]}
    views_agree(first, everything, phone, long)
    d = {it["sha"]: it["applied"] for it in call(f"/api/changes/{long}")["items"]}
    assert {s for s, on in d.items() if on} == first
    assert call(f"/api/changes/{long}/apply", {"through": order[9]})["hid"] == 0, "the same stretch again changes nothing"
    call(f"/api/changes/{long}/decide", {"shas": [order[4]], "decision": "reject"})   # a change of mind in the part done
    r = call(f"/api/changes/{long}/apply", {"through": order[19]})
    assert (r["hid"], r["showed"], r["applied"], r["todo"]) == (10, 1, 18, 19)
    r = call(f"/api/changes/{long}/apply", {})                                       # and the rest in one go
    assert (r["hid"], r["applied"], r["todo"], r["state"]) == (19, 37, 0, "applied")
    settled(long)
    views_agree(set(order) - {order[2], order[4], order[30]}, everything, phone, long)

    # commit: the set is done, and stays as it is until it is reopened; neither moves a photo
    assert (call(f"/api/changes/{long}")["state"], call(f"/api/changes/{long}")["done_at"]) == ("applied", None)
    call(f"/api/changes/{long}/decide", {"shas": [order[5]], "decision": "reject"})
    assert "apply the set first" in call(f"/api/changes/{long}/commit", {}, expect=409)["error"]
    call(f"/api/changes/{long}/decide", {"shas": [order[5]], "decision": "accept"})
    r = call(f"/api/changes/{long}/commit", {})
    assert (r["state"], r["applied"], bool(r["done_at"])) == ("done", 37, True)
    for what, body in (("decide", {"shas": [order[6]], "decision": "reject"}), ("items", {"remove": [order[6]]}),
                       ("apply", {}), ("revert", {})):
        assert "reopen" in call(f"/api/changes/{long}/{what}", body, expect=409)["error"]
    call(f"/api/changes/{long}/discard", {}, expect=409)
    d = call(f"/api/changes/{long}")
    assert (d["state"], d["applied"], d["reject"], d["n"]) == ("done", 37, 3, 40)
    assert [x["state"] for x in call("/api/changes")["sets"] if x["id"] == long] == ["done"]
    views_agree(set(order) - {order[2], order[4], order[30]}, everything, phone, long)
    r = call(f"/api/changes/{long}/reopen", {})
    assert (r["state"], r["done_at"], r["applied"]) == ("applied", None, 37)
    empty = call("/api/changes", {"title": "Nothing"}, expect=201)["id"]
    call(f"/api/changes/{empty}/commit", {}, expect=409)
    call(f"/api/changes/{empty}/discard", {})
    assert call(f"/api/changes/{long}/revert", {})["showed"] == 37
    settled(long)
    assert call(f"/api/changes/{long}/discard", {}) == {"discarded": long}

    # the page
    assert b"<title>Changes" in call("/changes") and b"<title>Changes" in call(f"/changes/{sid}")
    call("/api/changes/999", expect=404)
    print("change sets: ok")
finally:
    app.terminate()
    try:
        app.wait(10)
    except subprocess.TimeoutExpired:
        app.kill()
