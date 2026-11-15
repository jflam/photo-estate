#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["pillow>=10.0", "pillow-heif>=0.18"]
# ///
"""bench_ingest.py: how long a photo sent from the phone takes to show up in the app. See ../PERFORMANCE.md.

  uv run tools/bench_ingest.py --work ~/bench-estate                 # 121k-photo estate, 9 photos, 60 s quiet
  uv run tools/bench_ingest.py --work ~/bench-estate --quiet 10 --runs 3

The first run builds a synthetic estate in --work: the demo catalog cloned up to --photos rows (assets,
occurrences, embeddings, exif with a realistically large JSON blob, face scans, places), so every stage
sees a library of the real size. Store files exist only for the demo's photos and the uploads.

Each run starts from a copy-on-write clone of that catalog and an empty thumbnail cache, starts
`embed.py serve` with the real ingest-iphone.sh, uploads --n full-size (4032x3024) HEIC photos the way
the phone does (POST /api/upload/..., with date and GPS headers and the batch headers), then reports:

  upload     sending all the photos
  visible    last upload answered -> /api/info counts the new photos (searchable)
  thumbs     the new photos' grid thumbnails, fetched right after they are visible (slowest one)
  run        the ingest script's own wall time, and the time of each stage it prints
  reload     the app's catalog reload, from its log
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import random
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent      # photo-estate/
DEMO = HERE / "demo"


def build(work: Path, n: int) -> None:
    """The synthetic estate: the demo's rows cloned under fresh hashes until there are n assets."""
    db = work / "estate.db"
    if db.exists():
        return
    print(f"building a {n}-photo synthetic estate in {work} (once)", flush=True)
    work.mkdir(parents=True, exist_ok=True)
    tmp = work / "estate.db.tmp"
    shutil.copyfile(DEMO / "estate.db", tmp)
    shutil.copytree(DEMO / "store", work / "store", dirs_exist_ok=True)
    c = sqlite3.connect(tmp)
    rnd = random.Random(1)
    cols = lambda t: [r[1] for r in c.execute(f"PRAGMA table_info({t})")]
    have = c.execute("SELECT count(*) FROM assets").fetchone()[0]
    fake = [hashlib.sha256(f"synthetic-{i}".encode()).hexdigest() for i in range(n - have)]

    def clone(table, edit=None):
        cs, rows = cols(table), c.execute(f"SELECT * FROM {table}").fetchall()
        out = []
        for i, sha in enumerate(fake):
            d = dict(zip(cs, rnd.choice(rows)))
            d["sha256"] = sha
            if edit:
                edit(d, i)
            out.append(tuple(d[k] for k in cs))
        c.executemany(f"INSERT OR IGNORE INTO {table} VALUES ({','.join('?' * len(cs))})", out)

    def asset(d, i):
        d["taken_at"] = (f"{rnd.randint(2005, 2026)}-{rnd.randint(1, 12):02d}-{rnd.randint(1, 28):02d}"
                         f"T{rnd.randint(0, 23):02d}:00:00+00:00")
        d["lat"] = rnd.uniform(30, 50) if rnd.random() < .6 else None
        d["lon"] = rnd.uniform(-120, -70) if d["lat"] else None

    def occurrence(d, i):
        d.update(id=None, source="Takeout", path=f"synthetic/{i}.jpg", dir="synthetic", filename=f"IMG_{i}.jpg")

    big = json.dumps({f"Tag{k}": "x" * 20 for k in range(250)})   # exiftool's dump is ~6 KB a photo
    clone("assets", asset)
    clone("occurrences", occurrence)
    clone("embeddings")
    clone("exif", lambda d, i: d.update(data=big))
    c.execute("CREATE TABLE IF NOT EXISTS face_scans(sha256 TEXT, model TEXT, n INT, error TEXT, created_at TEXT,"
              " PRIMARY KEY(sha256, model))")   # as faces.py makes it: every photo counts as already scanned
    c.execute("INSERT OR IGNORE INTO face_scans SELECT sha256, 'buffalo_l', 0, NULL, '2026-01-01' FROM assets")
    c.commit()
    c.close()
    places = subprocess.run(["uv", "run", "--quiet", "--script", str(HERE / "places.py"), "--db", str(tmp), "run"],
                            capture_output=True, text=True)
    assert places.returncode == 0, places.stderr
    shas = [r[0] for r in sqlite3.connect(tmp).execute("SELECT sha256 FROM assets")]
    (work / "thumb-dims.json").write_text(json.dumps({s: [360, 240] for s in shas}))
    os.replace(tmp, db)


def photos(work: Path, n: int) -> list[Path]:
    """n distinct 12-megapixel HEICs, like an iPhone's (made once)."""
    from PIL import Image, ImageDraw
    import pillow_heif
    pillow_heif.register_heif_opener()
    d = work / "uploads"
    d.mkdir(exist_ok=True)
    out = []
    for i in range(n):
        p = d / f"IMG_{9000 + i}.HEIC"
        if not p.exists():
            rnd = random.Random(i)
            src = sorted((DEMO / "store").rglob("*.jpg"))[i % 50]
            im = Image.open(src).convert("RGB").resize((4032, 3024))
            dr = ImageDraw.Draw(im)
            for _ in range(20):
                x, y = rnd.randrange(4032), rnd.randrange(3024)
                dr.ellipse([x, y, x + 300, y + 300], fill=tuple(rnd.randrange(256) for _ in range(3)))
            im.save(p, "HEIF", quality=80)
        out.append(p)
    return out


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=120) as r:
        return r.read()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--work", required=True, type=Path)
    p.add_argument("--photos", type=int, default=121_000)
    p.add_argument("--n", type=int, default=9, help="photos to upload")
    p.add_argument("--quiet", type=float, default=60, help="the server's --ingest-quiet")
    p.add_argument("--runs", type=int, default=1)
    p.add_argument("--port", type=int, default=8798)
    p.add_argument("--check", action="store_true", help="the app also does a full reload and compares (slower)")
    a = p.parse_args()
    work = a.work.expanduser().resolve()
    build(work, a.photos)
    files = photos(work, a.n)
    base = f"http://127.0.0.1:{a.port}"
    results = []
    for k in range(a.runs):
        run = work / "run"
        shutil.rmtree(run, ignore_errors=True)
        (run / "review").mkdir(parents=True)
        subprocess.run(["cp", "-c", str(work / "estate.db"), str(run / "estate.db")], check=True)  # APFS clone
        shutil.copyfile(work / "thumb-dims.json", run / "review" / "thumb-dims.json")
        (run / "store").symlink_to(work / "store")
        log = open(run / "serve.log", "w")
        env = dict(os.environ, HF_HUB_OFFLINE="1", **({"PHOTO_ESTATE_CHECK_RELOAD": "1"} if a.check else {}))
        srv = subprocess.Popen(["uv", "run", "--quiet", "--script", str(HERE / "embed.py"), "--db", str(run / "estate.db"),
                                "serve", "--store", str(run / "store"), "--port", str(a.port), "--no-pregenerate",
                                "--ingest-quiet", str(a.quiet)], stdout=log, stderr=subprocess.STDOUT, env=env,
                               start_new_session=True)
        try:
            for _ in range(600):
                if "text encoder ready" in (run / "serve.log").read_text():
                    break
                time.sleep(0.2)
            else:
                sys.exit(f"server did not start; see {run / 'serve.log'}")
            before = json.loads(get(base, "/api/info"))["total"]
            batch = uuid.uuid4().hex[:12]
            t0 = time.time()
            shas = []
            for i, f in enumerate(files):
                body = f.read_bytes()
                shas.append(hashlib.sha256(body).hexdigest())
                req = urllib.request.Request(f"{base}/api/upload/2026/11/15/{f.stem}-{i:07x}.HEIC", data=body, method="POST",
                                             headers={"Content-Length": str(len(body)), "X-Taken": "2026-11-15T18:30:00Z",
                                                      "X-Latitude": "47.6205", "X-Longitude": "-122.3493",
                                                      "X-Batch": f"{batch}/{len(files)}"})
                with urllib.request.urlopen(req, timeout=120) as r:
                    assert r.status == 201, r.status
            t_up = time.time()
            while json.loads(get(base, "/api/info"))["total"] < before + len(files):
                time.sleep(0.05)
            t_vis = time.time()
            th = []
            for s in shas:
                t = time.time()
                get(base, f"/thumb/{s}")
                th.append(time.time() - t)
            while json.loads(get(base, "/api/ingest"))["running"]:
                time.sleep(0.2)
            st = json.loads(get(base, "/api/ingest"))
            stamps = [(m.group(1), m.group(2)) for x in st["log"] if (m := re.match(r"== (\d\d:\d\d:\d\d) (\S+)", x))]
            txt = (run / "serve.log").read_text()
            reload_s = [float(x) for x in re.findall(r"catalog reloaded: \d+ images in ([\d.]+)s", txt)]
            r = {"upload": t_up - t0, "visible": t_vis - t_up, "thumb_max": max(th), "thumb_sum": sum(th),
                 "run": st["finished_at"] - st["started_at"], "run_start": st["started_at"] - t_up,
                 "reload": reload_s, "stages": stamps, "exit": st["exit_code"]}
            results.append(r)
            print(f"run {k + 1}: upload {r['upload']:.1f}s | visible {r['visible']:.1f}s after the last upload"
                  f" | thumbs slowest {r['thumb_max'] * 1000:.0f} ms, all {r['thumb_sum']:.2f}s"
                  f" | script started {r['run_start']:.1f}s after, ran {r['run']:.1f}s (exit {r['exit']})"
                  f" | reload {', '.join(f'{x:.1f}s' for x in reload_s)}", flush=True)
            print("   stages: " + "  ".join(f"{t} {s}" for t, s in stamps), flush=True)
            for line in re.findall(r"reload check: .*", txt):
                print("   " + line, flush=True)
        finally:
            os.killpg(srv.pid, signal.SIGTERM)
            srv.wait()
            log.close()
    if a.runs > 1:
        med = lambda k: sorted(r[k] for r in results)[len(results) // 2]
        print(f"median: visible {med('visible'):.1f}s, run {med('run'):.1f}s, thumbs slowest {med('thumb_max') * 1000:.0f} ms")


if __name__ == "__main__":
    main()
