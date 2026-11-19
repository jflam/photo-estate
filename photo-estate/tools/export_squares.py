#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["pillow>=10.0", "pillow-heif>=0.18", "numpy"]
# ///
"""export_squares.py: every photo the Library shows, as a square JPEG, in tar shards, for embedding
experiments away from the library's computer.

  uv run tools/export_squares.py --db /Volumes/estate/estate.db --store /Volumes/estate/store --count
  uv run tools/export_squares.py --db ... --store ... --sample 1000 --out ~/photo-estate-lab/sample1k
  nice -n 10 uv run tools/export_squares.py --db ... --store ... --out ~/photo-estate-lab/squares512

Which photos: images in the catalog with an embedding for --model (what the app loads), less those a
change set hides (changes.hidden_shas), screenshots (catalog.is_screenshot), and the photos behind
another in a stack of near-identical shots (stacks.py, the top chosen by scores.py as the app does):
what the Library's grid shows. --with-stacked keeps the photos behind a stack's top too.
Each is decoded as embed.py's _decode does (draft decode, exif_transpose, RGB, bicubic resize to a
square with no crop), but at --side (512), and saved as JPEG quality 92.

Output, in --out: shard-0000.tar, shard-0001.tar, ... of --shard-size (2,000) photos each, members
<sha>.jpg, in store-path order (by sha, so the USB hard disk reads in order); parts/shard-NNNN.csv for
each finished shard; manifest.csv (sha, ext, width, height, shard, status) joining them, where width
and height are the original's, upright, and status is "ok" or the error. A shard is written to a
.part file and renamed when done, so a rerun skips finished shards and redoes the one it was in.
The shard list is fixed by the photos at the start of a run: rerun after an ingest and the shards
from the first new photo on change, so start a fresh --out instead.

The catalog is opened read-only (mode=ro). --code is the folder holding catalog.py and changes.py
(default: this repo's), so the script can run from a scratch folder against another checkout.
It lowers its own priority (nice 10) and uses cpu_count - 2 workers.
"""
from __future__ import annotations

import argparse
import csv
import fcntl
import io
import os
import random
import shutil
import sqlite3
import sys
import tarfile
import time
from multiprocessing import Pool
from pathlib import Path

DEFAULT_MODEL = "google/siglip2-base-patch16-256"   # embed.py's DEFAULT_MODEL
FIELDS = ["sha", "ext", "width", "height", "shard", "status"]


def blob_path(store: Path, sha: str, ext: str) -> Path:   # estate.blob_path (estate.py needs Pillow at import)
    return store / sha[:2] / sha[2:4] / f"{sha}{ext}"


def visible(db: str, model: str, with_stacked: bool = False):
    """(counts, [(sha, ext)] sorted by sha) for what the Library shows."""
    import numpy as np
    from catalog import is_screenshot
    from changes import hidden_shas
    from scores import best_first, load as load_scorers
    from stacks import group, seconds, tops
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60)
    images = {sha: {"ext": ext, "w": w, "h": h, "path": None, "taken": taken} for sha, ext, w, h, taken in
              con.execute("SELECT sha256, ext, width, height, taken_at FROM assets WHERE kind='image'")}
    rows = sorted(r for r in con.execute(
        "SELECT sha256, vec FROM embeddings WHERE model=? AND vec IS NOT NULL", (model,)) if r[0] in images)
    shas = [r[0] for r in rows]
    M = np.frombuffer(b"".join(r[1] for r in rows), np.float16).reshape(len(shas), -1).astype(np.float32)
    del rows
    embedded = set(shas)
    for sha, path in con.execute("SELECT sha256, dir || '/' || filename FROM occurrences ORDER BY id"):
        m = images.get(sha)
        if m is not None and m["path"] is None:   # the first place the photo was seen, as catalog.load
            m["path"] = path
    try:
        for sha, camera, lens in con.execute("SELECT sha256, camera, lens FROM exif"):
            if sha in images:
                images[sha].update(camera=camera, lens=lens)
    except sqlite3.OperationalError:
        pass
    hidden = hidden_shas(con) & embedded
    scorers = load_scorers(con, model)
    con.close()
    shots = {s for s in embedded - hidden if is_screenshot(images[s])}
    # Stacks as catalog.load makes them: over every photo not hidden (screenshots included), top by rank.
    order = best_first(M, scorers)
    rank = None
    if order is not None:
        rank = np.empty(len(shas), np.int64)
        rank[order] = np.arange(len(shas))
    stack_of = group(np.array([seconds(images[s]["taken"]) for s in shas]), M,
                     np.array([s not in hidden for s in shas], bool))
    top, _ = tops(stack_of, rank)
    behind = {s for s, t in zip(shas, top) if not t} - hidden - shots
    keep = sorted(embedded - hidden - shots - (set() if with_stacked else behind))
    counts = {"images": len(images), "in_library": len(embedded), "hidden": len(hidden),
              "screenshots": len(shots), "behind": len(behind), "export": len(keep)}
    return counts, [(s, images[s]["ext"]) for s in keep]


def _square(job: tuple[str, str, int]):
    """Worker: (sha, ext, w, h, jpeg bytes or None, error or None). Never raises."""
    sha, path, side = job
    ext = Path(path).suffix
    try:
        from PIL import Image, ImageOps
        import pillow_heif
        pillow_heif.register_heif_opener()
        Image.MAX_IMAGE_PIXELS = None
        with Image.open(path) as im:
            w, h = im.size
            if im.getexif().get(0x0112) in (5, 6, 7, 8):   # turned a quarter: the upright size is the other way
                w, h = h, w
            im.draft("RGB", (side * 2, side * 2))  # as embed.py's _decode
            im = ImageOps.exif_transpose(im).convert("RGB")
            im = im.resize((side, side), Image.Resampling.BICUBIC)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=92)
            return sha, ext, w, h, buf.getvalue(), None
    except Exception as e:
        return sha, ext, "", "", None, f"{type(e).__name__}: {e}"[:300].replace("\n", " ")


def write_shard(pool, store: Path, out: Path, k: int, rows, side: int) -> tuple[int, int, int]:
    """One shard: tar and its csv part, both renamed into place when complete. (ok, errors, bytes)."""
    name = f"shard-{k:04d}"
    tmp, part = out / f"{name}.tar.part", out / "parts" / f"{name}.csv.part"
    ok = bad = size = 0
    jobs = [(sha, str(blob_path(store, sha, ext)), side) for sha, ext in rows]
    with tarfile.open(tmp, "w", format=tarfile.PAX_FORMAT) as tar, open(part, "w", newline="") as f:
        w = csv.writer(f)
        for sha, ext, sw, sh, data, err in pool.imap(_square, jobs, chunksize=4):
            if data is not None:
                info = tarfile.TarInfo(f"{sha}.jpg")
                info.size, info.mtime, info.mode = len(data), 0, 0o644
                tar.addfile(info, io.BytesIO(data))
                ok, size = ok + 1, size + len(data)
            else:
                bad += 1
            w.writerow([sha, ext, sw, sh, name, "ok" if err is None else err])
    tmp.rename(out / f"{name}.tar")      # the tar first: a csv part means its tar is whole
    part.rename(out / "parts" / f"{name}.csv")
    return ok, bad, size


def join_manifest(out: Path) -> None:
    tmp = out / "manifest.csv.part"
    with open(tmp, "w", newline="") as f:
        f.write(",".join(FIELDS) + "\n")
        for p in sorted((out / "parts").glob("shard-*.csv")):
            f.write(p.read_text())
    tmp.rename(out / "manifest.csv")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--db", required=True)
    p.add_argument("--store", required=True)
    p.add_argument("--out", default="~/photo-estate-lab/squares512")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--side", type=int, default=512)
    p.add_argument("--shard-size", type=int, default=2000)
    p.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 2))
    p.add_argument("--sample", type=int, help="a random N of the photos instead of all of them")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--with-stacked", action="store_true", help="keep the photos behind a stack's top")
    p.add_argument("--count", action="store_true", help="print the counts and stop")
    p.add_argument("--need-gb", type=float, default=15, help="free space --out's disk must have")
    p.add_argument("--code", default=str(Path(__file__).resolve().parent.parent),
                   help="folder with catalog.py and changes.py")
    a = p.parse_args()
    os.nice(max(0, 10 - os.nice(0)))   # nice 10, whether or not it was started under nice
    sys.path.insert(0, a.code)

    t0 = time.time()
    counts, rows = visible(a.db, a.model, a.with_stacked)
    print(f"catalog: {counts['images']} images, {counts['in_library']} in the Library (embedded), "
          f"{counts['hidden']} hidden, {counts['screenshots']} screenshots, {counts['behind']} behind a stack's top"
          f"{' (kept)' if a.with_stacked else ''}, {counts['export']} to export "
          f"({time.time() - t0:.1f} s)", flush=True)
    if a.count:
        return
    if a.sample:
        rows = sorted(random.Random(a.seed).sample(rows, min(a.sample, len(rows))))

    out = Path(a.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    (out / "parts").mkdir(exist_ok=True)
    free = shutil.disk_usage(out).free / 1e9
    print(f"output: {out} ({free:.0f} GB free)", flush=True)
    if free < a.need_gb:
        sys.exit(f"{out} has {free:.1f} GB free, less than --need-gb {a.need_gb}: pass --out on another disk")
    lock = open(out / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(f"another export is writing {out}")
    (out / "shas.txt").write_text("".join(f"{s}\n" for s, _ in rows))   # what this run's shards are cut from

    shards = [rows[i:i + a.shard_size] for i in range(0, len(rows), a.shard_size)]
    todo = [k for k in range(len(shards)) if not (out / "parts" / f"shard-{k:04d}.csv").exists()]
    print(f"{len(shards)} shards of {a.shard_size}, {len(shards) - len(todo)} already done, "
          f"{a.workers} workers, side {a.side}", flush=True)
    t1, done, total_bytes, total_bad = time.time(), 0, 0, 0
    with Pool(a.workers) as pool:
        for k in todo:
            ts = time.time()
            ok, bad, size = write_shard(pool, Path(a.store), out, k, shards[k], a.side)
            done, total_bytes, total_bad = done + len(shards[k]), total_bytes + size, total_bad + bad
            left = sum(len(shards[j]) for j in todo if j > k)
            per = (time.time() - t1) / done
            print(f"shard-{k:04d}: {ok} ok, {bad} errors, {size / 1e6:.0f} MB, {time.time() - ts:.0f} s | "
                  f"{done}/{done + left} photos, {per * 1000:.0f} ms/photo, avg {total_bytes / max(1, done - total_bad) / 1e3:.0f} KB, "
                  f"ETA {left * per / 3600:.1f} h", flush=True)
            join_manifest(out)
    join_manifest(out)
    print(f"done: {done} photos in {time.time() - t1:.0f} s, {total_bytes / 1e9:.2f} GB, {total_bad} errors", flush=True)


if __name__ == "__main__":
    main()
