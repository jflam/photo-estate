#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy"]
# ///
"""
scores.py: which photos are best (issue #44). The catalog keeps the *scorers*, not a score per photo: a scorer is
one weight per embedding dimension (and an offset), so a photo's score is a dot product with the embedding the app
already holds in memory. The app works the scores out when it loads the catalog (milliseconds for 100k photos), so a
photo that arrives tomorrow is scored with no pipeline step, and a retrained scorer takes effect on the next load.

Two scorers, both made in tools/aesthetics:
  quality  is this a real photo or junk: a general aesthetic model's opinion (score_library.py's quality.npz),
           on its scale of about 1 to 7
  taste    which of two good photos the owner would pick (train_taste.py's model.npz); taught only on photos
           with quality >= GOOD, so it means something only among those

"Best first" is therefore: the good photos (quality >= GOOD) by taste, then the rest by quality.

  uv run scores.py --db /Volumes/estate/estate.db install --quality quality.npz --taste model.npz
  uv run scores.py --db /Volumes/estate/estate.db show

The table lives in the catalog. A catalog with no scorers (the demo) has no "Best first"; nothing else changes.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone

import numpy as np

from core import schema

SCHEMA = schema.SCORES
NAMES = ("quality", "taste")
GOOD = 5.0


def save(con: sqlite3.Connection, name: str, model: str, w, b: float = 0.0, info: dict | None = None) -> None:
    if name not in NAMES:
        raise ValueError(f"unknown scorer {name!r}")
    w = np.asarray(w, dtype=np.float32).ravel()
    if not len(w) or not np.isfinite(w).all() or not np.isfinite(b):
        raise ValueError(f"{name}: weights must be finite numbers")
    schema.ensure(con, "scores")
    con.execute("INSERT OR REPLACE INTO scorers VALUES (?,?,?,?,?,?)",
                (name, model, w.tobytes(), float(b), json.dumps(info or {}),
                 datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
    con.commit()


def load(con: sqlite3.Connection, model: str) -> dict:
    """{name: (weights, offset)} for this embedding model. Empty for a catalog that has never had a scorer."""
    try:
        rows = con.execute("SELECT name, w, b FROM scorers WHERE model=?", (model,)).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {name: (np.frombuffer(w, dtype=np.float32), float(b or 0.0)) for name, w, b in rows}


def best(M, scorers: dict):
    """A number per photo (row of M, unit-length embeddings), larger is better; None unless both scorers are
    there and fit M. Good photos sort by taste, above every other photo; the rest sort by quality."""
    if any(n not in scorers or len(scorers[n][0]) != M.shape[1] for n in NAMES) or not len(M):
        return None
    (qw, qb), (tw, tb) = scorers["quality"], scorers["taste"]
    quality = M @ qw.astype(M.dtype) + qb
    taste = M @ tw.astype(M.dtype) + tb
    return np.where(quality >= GOOD, 1000.0 + taste.astype(np.float64), quality.astype(np.float64))


def best_first(M, scorers: dict):
    """Every row of M, best first (ties keep their order), or None when there is nothing to go by."""
    key = best(M, scorers)
    return None if key is None else np.argsort(-key, kind="stable")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="estate.db")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("install", help="put scorers made in tools/aesthetics into the catalog")
    i.add_argument("--quality", help="score_library.py's quality.npz")
    i.add_argument("--taste", help="train_taste.py's model.npz")
    sub.add_parser("show", help="the scorers the catalog holds")
    a = ap.parse_args()
    con = sqlite3.connect(a.db, timeout=60)
    if a.cmd == "install":
        for name, path in (("quality", a.quality), ("taste", a.taste)):
            if not path:
                continue
            z = np.load(path)
            info = {k: (z[k].item() if z[k].ndim == 0 else None) for k in z.files if k not in ("w", "b", "model")}
            save(con, name, str(z["model"]), z["w"], float(z["b"]) if "b" in z.files else 0.0, info)
            print(f"installed {name}: {len(z['w'])} weights for {z['model']} {info}")
    try:
        rows = con.execute("SELECT name, model, length(w) / 4, b, info, updated_at FROM scorers ORDER BY name").fetchall()
    except sqlite3.OperationalError:
        rows = []
    for r in rows:
        print("  ".join(str(x) for x in r))
    if not rows:
        print("no scorers in this catalog")


if __name__ == "__main__":
    main()
