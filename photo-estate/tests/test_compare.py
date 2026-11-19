#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy"]
# ///
"""compare.py, end to end on the demo library: `embed.py serve --demo --compare-vectors` ranks a search by
a second model's vectors beside the app's own. The "second model" here is the app's own model, with its
vectors from the demo catalog written out in shards as tools/embed_squares.py writes them, so both
columns must agree exactly; some photos are left out of the shards, and both columns must leave them
out. Also: filters narrow both sides, no words is an error, votes are recorded, and without the flag
there are no compare routes. Starts the real app twice."""
import json, os, socket, sqlite3, subprocess, tempfile, time, urllib.error, urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
MODEL = "google/siglip2-base-patch16-256"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start(*extra):
    port = free_port()
    app = subprocess.Popen(["uv", "run", "--quiet", str(ROOT / "embed.py"), "serve", "--demo", "--port", str(port),
                            "--no-pregenerate", *extra], env={**os.environ, "HF_HUB_OFFLINE": "1"},
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    for _ in range(600):
        try:
            urllib.request.urlopen(base + "/api/info", timeout=5)
            return app, base
        except (urllib.error.URLError, ConnectionError):
            assert app.poll() is None, app.stdout.read().decode()[-3000:]
            time.sleep(0.2)
    raise AssertionError("the app did not start")


def call(base, path, body=None):
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}") if e.headers.get("Content-Type", "").startswith("application/json") else {}


def stop(app):
    app.terminate()
    try:
        app.wait(10)
    except subprocess.TimeoutExpired:
        app.kill()


with tempfile.TemporaryDirectory() as tmp:
    vec = Path(tmp) / "vectors"
    vec.mkdir()
    con = sqlite3.connect(f"file:{ROOT / 'demo' / 'estate.db'}?mode=ro", uri=True)
    rows = sorted(con.execute("SELECT sha256, vec FROM embeddings WHERE model=? AND vec IS NOT NULL", (MODEL,)))
    con.close()
    left_out = {s for s, _ in rows[::10]}           # every tenth photo has no vector from the "second model"
    kept = [(s, v) for s, v in rows if s not in left_out]
    for k, part in enumerate((kept[:40], kept[40:])):
        np.save(vec / f"shard-{k:04d}.npy", np.stack([np.frombuffer(v, np.float16) for _, v in part]))
        (vec / f"shard-{k:04d}.shas.txt").write_text("".join(f"{s}\n" for s, _ in part))
    (vec / "meta.json").write_text(json.dumps({"model": MODEL}))
    votes = Path(tmp) / "votes.jsonl"

    app, base = start("--compare-vectors", str(vec), "--compare-votes", str(votes))
    try:
        code, early = call(base, "/api/compare?q=a%20dog")
        assert code in (200, 503), (code, early)          # 503 while the second model loads, not a wait
        for _ in range(600):
            code, info = call(base, "/api/compare/info")
            if info["b"]["ready"]:
                break
            time.sleep(0.5)
        assert info["b"]["ready"], "the second model's text side never loaded"
        assert info["b"]["photos"] == len(kept) and info["both"] == len(kept), info
        assert info["a"]["dim"] == info["b"]["dim"] == 768, info

        code, got = call(base, "/api/compare?k=20&q=a%20dog")
        assert code == 200, got
        assert got["candidates"] == len(kept), got["candidates"]
        a, b = ([r["id"] for r in got[s]["results"]] for s in "ab")
        assert len(a) == 20 and a == b, "the same model and vectors must rank the same"
        assert not set(a) & left_out, "a photo with no second vector is in a column"
        assert all(abs(x["score"] - y["score"]) < 2e-3 for x, y in zip(got["a"]["results"], got["b"]["results"]))
        assert got["overlap"] == 20

        _, everything = call(base, "/api/compare?k=200&q=a%20dog")
        assert len(everything["a"]["results"]) == len(kept), "k is the most a column holds"
        year = json.loads(urllib.request.urlopen(base + "/api/periods").read())["years"][0]["key"]
        code, narrowed = call(base, f"/api/compare?k=200&q=year:{year}%20a%20dog")
        assert code == 200 and narrowed["applied"] == [f"year:{year}"] and narrowed["text"] == "a dog", narrowed
        assert 0 < narrowed["candidates"] < len(kept), narrowed["candidates"]
        assert len(narrowed["b"]["results"]) == narrowed["candidates"]

        code, err = call(base, f"/api/compare?q=year:{year}")
        assert code == 400 and "no words" in err["error"], (code, err)

        code, ok = call(base, "/api/compare/vote", {"q": "a dog", "winner": "b", "left": "a", "k": 20})
        assert code == 200 and ok["votes"] == {"a": 0, "b": 1, "same": 0}, ok
        assert call(base, "/api/compare/vote", {"q": "a dog", "winner": "left"})[0] == 400
        rec = [json.loads(l) for l in votes.read_text().splitlines()]
        assert len(rec) == 1 and rec[0]["winner"] == "b" and rec[0]["a"] == MODEL, rec

        page = urllib.request.urlopen(base + "/compare").read()
        assert b"Compare models" in page and b"/*@estate.css*/" not in page, "the page, with the stylesheet in"
    finally:
        stop(app)

    for extra in ([], ["--compare-vectors", str(Path(tmp) / "missing")]):   # no flag, or no vectors: no experiment
        app, base = start(*extra)                    # (and the app still starts)
        try:
            assert call(base, "/api/compare/info")[0] == 404, extra
            try:
                urllib.request.urlopen(base + "/compare")
                raise AssertionError(f"/compare is served with {extra}")
            except urllib.error.HTTPError as e:
                assert e.code == 404
        finally:
            stop(app)
print("compare: ok")
