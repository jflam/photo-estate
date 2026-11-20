#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["numpy", "pillow>=10.0", "pillow-heif>=0.18"]
# ///
"""
sample_resize.py — aesthetics pilot step 1 (runs ON THE MINI; the catalog is opened read-only).

Picks N images uniformly at random (seed --seed, default 44; optional --exclude npz; resumable) among those with a SigLIP 2 base embedding, writes upright RGB
JPEGs (q90, shorter side 384, never upscaled) to OUT/img/<sha>.jpg and OUT/sample.npz.
Photos stay on the mini and on the owner's Mac cache only.

  nice -n 10 uv run sample_resize.py --out ~/aesthetic-pilot
"""
import argparse, sqlite3, sys, time, os
from multiprocessing import Pool
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # photo-estate/, for core/
from core.media import EMBED_SKIP_EXT as SKIP_EXT  # noqa: E402  (RAW: Pillow can't decode it)

MODEL = "google/siglip2-base-patch16-256"
DB = "file:/Volumes/estate/estate.db?mode=ro"
STORE = Path("/Volumes/estate/store")


def work(job):
    sha, src, dest = job
    try:
        from PIL import Image, ImageOps
        import pillow_heif
        pillow_heif.register_heif_opener()
        Image.MAX_IMAGE_PIXELS = None
        with Image.open(src) as im:
            im.draft("RGB", (768, 768))  # reduced-size JPEG decode, stays >= 2x the target
            im = ImageOps.exif_transpose(im).convert("RGB")
            w, h = im.size
            s = 384 / min(w, h)
            if s < 1:
                im = im.resize((max(1, round(w * s)), max(1, round(h * s))), Image.Resampling.LANCZOS)
            tmp = f"{dest}.tmp"
            im.save(tmp, "JPEG", quality=90)
            os.replace(tmp, dest)
        return sha, None
    except Exception as e:
        return sha, f"{type(e).__name__}: {e}"[:200]


def main():
    import numpy as np
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path.home() / "aesthetic-pilot"))
    ap.add_argument("-n", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=44)
    ap.add_argument("--exclude", default=None, help="npz whose shas are excluded from the draw (e.g. the pilot set)")
    ap.add_argument("--workers", type=int, default=3)
    a = ap.parse_args()
    out = Path(a.out).expanduser(); (out / "img").mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(DB, uri=True, timeout=60)
    rows = con.execute(
        "SELECT a.sha256, a.ext, a.width, a.height, a.favorited, a.taken_at, x.make, x.model, e.vec FROM assets a"
        " JOIN embeddings e ON e.sha256=a.sha256 AND e.model=? AND e.vec IS NOT NULL"
        " LEFT JOIN exif x ON x.sha256=a.sha256 WHERE a.kind='image' ORDER BY a.sha256", (MODEL,)).fetchall()
    rows = [r for r in rows if (r[1] or "").lower() not in SKIP_EXT]
    print(f"{len(rows)} candidates (non-RAW images with embedding)", flush=True)
    if a.exclude:
        ex = set(np.load(Path(a.exclude).expanduser())["sha"].tolist())
        rows = [r for r in rows if r[0] not in ex]
        print(f"excluded {len(ex)} shas; {len(rows)} candidates left", flush=True)
    order = np.random.default_rng(a.seed).permutation(len(rows))
    fail_log = out / "failures.tsv"

    ok, failed = [], []
    pos = 0
    t_work = 0.0
    with Pool(a.workers) as pool:
        while len(ok) < a.n and pos < len(order):
            need = a.n - len(ok)
            chunk = [rows[i] for i in order[pos:pos + need]]; pos += need
            jobs = [(r[0], str(STORE / r[0][:2] / r[0][2:4] / f"{r[0]}{r[1]}"), str(out / "img" / f"{r[0]}.jpg")) for r in chunk
                    if not (out / "img" / f"{r[0]}.jpg").exists()]  # resume: skip finished JPEGs
            t0 = time.time()
            res = {}
            for k, (sha, err) in enumerate(pool.imap_unordered(work, jobs, chunksize=4), 1):
                res[sha] = err
                if err:
                    with open(fail_log, "a") as f: f.write(f"{sha}\t{err}\n")
                if k % 500 == 0:
                    print(f"{len(ok) + k} / {a.n} chunk-progress, {k / (time.time() - t0):.2f} img/s", flush=True)
            t_work += time.time() - t0
            for r in chunk:
                (failed if res.get(r[0]) else ok).append(r)
                if res.get(r[0]): print("FAIL", r[0][:12], r[1], res[r[0]], flush=True)
    n_done = len(ok) + len(failed)
    print(f"resized {len(ok)} ok, {len(failed)} failed in {t_work:.1f}s -> {n_done / max(t_work, 1e-9):.2f} img/s (attempted this run, {a.workers} workers)")
    if a.exclude:
        assert not ({r[0] for r in ok} & ex), "overlap with excluded set"
        print("overlap with exclude set: 0")
    ok.sort(key=lambda r: r[0])
    np.savez(out / "sample.npz",
             sha=np.array([r[0] for r in ok]), vec=np.stack([np.frombuffer(r[8], dtype=np.float16) for r in ok]).astype(np.float32),
             favorited=np.array([r[4] or 0 for r in ok], dtype=np.int8), make=np.array([r[6] or "" for r in ok]),
             model=np.array([r[7] or "" for r in ok]), taken_at=np.array([r[5] or "" for r in ok]),
             width=np.array([r[2] or 0 for r in ok]), height=np.array([r[3] or 0 for r in ok]),
             ext=np.array([r[1] or "" for r in ok]))
    print("wrote", out / "sample.npz")


if __name__ == "__main__":
    main()
