#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy"]
# ///
"""
score_library.py — score every photo in the library that has an embedding (issue #44; runs ON THE MINI; the catalog
is opened read-only and nothing is written to it).

  nice -n 10 uv run score_library.py [--sample ~/aesthetic-20k] [--taste ~/aesthetic-library/model.npz] [--out ~/aesthetic-library]

Two scores per photo, both a weighted sum of its stored SigLIP 2 embedding (unit length), so the whole library takes
seconds:
  quality  the teacher scorer's opinion, learnt from the photos it scored (<sample>/sample.npz + teacher.csv) by
           ridge regression. It says "is this a real photo or junk"; it is on the teacher's scale (about 1 to 7).
  taste    the owner's own picks (train_taste.py's model.npz). It was taught only on photos with quality >= 5, so
           it means something only among those: gate on quality first, then rank by taste.
Writes <out>/library_scores.csv (sha, quality, taste; sorted by sha) and prints how well the ridge reproduces the
teacher on photos it was not fitted on.
"""
from __future__ import annotations

import argparse
import csv
import re
import sqlite3
from pathlib import Path

import numpy as np

MODEL = "google/siglip2-base-patch16-256"
DB = "file:/Volumes/estate/estate.db?mode=ro"
ALPHAS = (0.01, 0.03, 0.1, 0.3, 1.0, 3.0)
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def unit(V):
    V = np.asarray(V, dtype=np.float32)
    n = np.linalg.norm(V, axis=1, keepdims=True)
    return V / np.where(n == 0, 1, n)


def fit_ridge(X, y, alpha):
    """(w, b) minimising |X w + b - y|^2 + alpha |w|^2."""
    X = np.asarray(X, dtype=np.float64)
    mx, my = X.mean(0), float(np.mean(y))
    Xc = X - mx
    w = np.linalg.solve(Xc.T @ Xc + alpha * np.eye(X.shape[1]), Xc.T @ (np.asarray(y) - my))
    return w, my - mx @ w


def spearman(a, b):
    ra, rb = np.argsort(np.argsort(a)), np.argsort(np.argsort(b))
    return float(np.corrcoef(ra, rb)[0, 1])


def fit_quality(X, y, alphas=ALPHAS, seed=44):
    """Pick alpha on a held-out fifth, report that fifth's fit, then refit on everything.
    Returns (w, b, {alpha, spearman, mae, n_held})."""
    hold = np.random.default_rng(seed).permutation(len(y)) % 5 == 0
    best = None
    for a in alphas:
        w, b = fit_ridge(X[~hold], y[~hold], a)
        p = X[hold] @ w + b
        r = {"alpha": a, "spearman": spearman(p, y[hold]), "mae": float(np.mean(np.abs(p - y[hold]))), "n_held": int(hold.sum())}
        if best is None or r["mae"] < best["mae"]:
            best = r
    w, b = fit_ridge(X, y, best["alpha"])
    return w, b, best


def read_teacher(path):
    sc = {}
    with open(path, newline="") as f:
        for r in csv.reader(f):
            if len(r) == 2 and SHA_RE.match(r[0]):
                try:
                    sc[r[0]] = float(r[1])
                except ValueError:
                    pass
    return sc


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sample", default=str(Path.home() / "aesthetic-20k"))
    ap.add_argument("--taste", default=str(Path.home() / "aesthetic-library" / "model.npz"))
    ap.add_argument("--out", default=str(Path.home() / "aesthetic-library"))
    ap.add_argument("--db", default=DB)
    a = ap.parse_args()
    sample, out = Path(a.sample).expanduser(), Path(a.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)

    z = np.load(sample / "sample.npz")
    sc = read_teacher(sample / "teacher.csv")
    keep = [i for i, s in enumerate(z["sha"]) if str(s) in sc]
    X = unit(z["vec"][keep])
    y = np.array([sc[str(z["sha"][i])] for i in keep])
    w, b, fit = fit_quality(X, y)
    print(f"quality: ridge on {len(y)} teacher-scored photos, alpha {fit['alpha']}; on {fit['n_held']} held out: "
          f"Spearman {fit['spearman']:.3f}, mean error {fit['mae']:.2f} points", flush=True)

    m = np.load(Path(a.taste).expanduser())
    assert str(m["model"]) == MODEL, f"taste model was trained on {m['model']}, the catalog holds {MODEL}"
    tw = m["w"].astype(np.float64)
    assert tw.shape == w.shape, "taste model and embeddings differ in size"
    print(f"taste: model trained on {int(m['pairs'])} picks", flush=True)

    con = sqlite3.connect(a.db, uri=True, timeout=60)
    rows = con.execute("SELECT a.sha256, e.vec FROM assets a JOIN embeddings e ON e.sha256=a.sha256 AND e.model=?"
                       " AND e.vec IS NOT NULL WHERE a.kind='image' ORDER BY a.sha256", (MODEL,)).fetchall()
    con.close()
    shas = [r[0] for r in rows]
    V = unit(np.stack([np.frombuffer(r[1], dtype=np.float16) for r in rows])).astype(np.float64)
    quality, taste = V @ w + b, V @ tw
    tmp = out / "library_scores.csv.tmp"
    with open(tmp, "w", newline="") as f:
        wr = csv.writer(f); wr.writerow(["sha", "quality", "taste"])
        wr.writerows((s, f"{q:.4f}", f"{t:.4f}") for s, q, t in zip(shas, quality, taste))
    tmp.replace(out / "library_scores.csv")
    np.savez(out / "quality.npz", w=w.astype(np.float32), b=b, alpha=fit["alpha"], model=MODEL, photos=len(y))
    good = quality >= 5.0
    print(f"scored {len(shas)} photos -> {out / 'library_scores.csv'}")
    print(f"quality >= 5.0: {int(good.sum())} ({good.mean():.1%}); quality percentiles 5/50/95: "
          + " / ".join(f"{v:.2f}" for v in np.percentile(quality, [5, 50, 95])))


if __name__ == "__main__":
    main()
