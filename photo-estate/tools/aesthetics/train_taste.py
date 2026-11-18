#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy"]
# ///
"""
train_taste.py — a scorer trained on the owner's own picks (issue #44). The judge showed that the teacher scorer
agrees with the owner on barely more than half of the pairs of good photos; this learns from the pairs instead.

  uv run train_taste.py [--cache ~/Library/Caches/photo-estate-aesthetics] [--rounds round2 round3] [--c 1.0] [--show 60]

Reads <cache>/<round>/judgments.jsonl for each round, and sample.npz + teacher.csv from the first round (the rounds
share one scored sample). A pick is used if it is decided (one photo chosen), is not a repeat, and neither photo is
hidden in the library (any <round>/hidden.txt) or was rejected while judging.

Model: one weight per embedding dimension; a photo's taste score is weights . embedding (unit length), and the
chance that A is picked over B is sigmoid(score A - score B). Fitted by L2-regularised logistic regression on the
embedding differences (Newton's method; numpy only). Reports accuracy on held-out pairs (10 folds, 5 shuffles)
beside the teacher's on the same pairs, then fits on every pair and writes, in <cache>/taste/:
  model.npz    w, c, the embedding model's name, pairs used
  scores.csv   sha, taste, teacher for every photo in the sample
  needed.txt   the images index.html shows (fetch_taste.sh brings them from the mini)
  index.html   the top and bottom --show photos by taste, among good photos that were in no pair

The picks were all between photos the teacher scores >= 5.0 with a camera make, so the scorer has only been taught
to rank those: the page shows that pool only, and a score for any other photo is a guess. Private photos: the page
is a local file beside its images and nothing leaves the cache dir.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path

import numpy as np

MODEL = "google/siglip2-base-patch16-256"
MIN_SCORE = 5.0
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


# ------------------------------------------------------------------ picks

def replay(path):
    """Answers still standing after undos, as judge.py reads its log."""
    answers = {}
    for line in Path(path).read_text().splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("event") == "undo":
            answers.pop(r.get("pair"), None)
        elif "choice" in r:
            answers.pop(r["pair"], None)
            answers[r["pair"]] = r
    return answers


def picks_from(answers, exclude=()):
    """(winner sha, loser sha) for each decided, first-time answer; none with a photo in `exclude` or one rejected
    in these answers. Also returns every sha the answers showed and the rejected ones."""
    recs = list(answers.values())
    rejected = {r["rejected"] for r in recs if r.get("choice") == "reject"}
    out = set(exclude) | rejected
    shown = {r[k] for r in recs for k in ("sha_a", "sha_b")}
    picks = []
    for r in recs:
        if r["choice"] not in ("a", "b") or r.get("repeat_of") is not None:
            continue
        a, b = r["sha_a"], r["sha_b"]
        if a in out or b in out:
            continue
        picks.append((a, b) if r["choice"] == "a" else (b, a))
    return picks, shown, rejected


# ------------------------------------------------------------------ model

def fit(D, c=1.0, iters=50):
    """w minimising sum(log(1 + exp(-D w))) + |w|^2 / (2c): each row of D is winner - loser."""
    n, k = D.shape
    D = D.astype(np.float64)
    w = np.zeros(k)
    for _ in range(iters):
        p = 1 / (1 + np.exp(D @ w))            # how wrong each pair still is
        g = -D.T @ p + w / c
        H = (D * (p * (1 - p))[:, None]).T @ D + np.eye(k) / c
        step = np.linalg.solve(H, g)
        w -= step
        if np.abs(step).max() < 1e-8:
            break
    return w


def cv_accuracy(D, c=1.0, folds=10, reps=5):
    """Share of held-out pairs whose winner the model scores higher, averaged over `reps` shuffles."""
    n = len(D)
    accs = []
    for rep in range(reps):
        f = np.random.default_rng(rep).permutation(n) % folds
        ok = np.zeros(n, bool)
        for j in range(folds):
            ok[f == j] = D[f == j] @ fit(D[f != j], c) > 0
        accs.append(ok.mean())
    return float(np.mean(accs))


# ------------------------------------------------------------------ files

def read_shas(path):
    return {s.removesuffix(".jpg") for s in Path(path).read_text().split() if SHA_RE.match(s.removesuffix(".jpg"))}


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


PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Your taste: top and bottom</title>
<style>
body{margin:0;padding:16px;background:#111;color:#bbb;font:14px system-ui,sans-serif}
h2{color:#ddd;font-size:18px;margin:24px 0 4px}p{margin:0 0 12px;max-width:70ch}
.g{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:4px}
.g img{width:100%%;aspect-ratio:1;object-fit:cover;background:#000;display:block}
</style>
<p>%(intro)s</p>
<h2>Top %(n)d</h2><p>What the scorer thinks you would pick most often, best first.</p><div class=g>%(top)s</div>
<h2>Bottom %(n)d</h2><p>What it thinks you would pick least often, worst first.</p><div class=g>%(bottom)s</div>
"""


def run(cache, rounds, c, show):
    cache = Path(cache).expanduser()
    first = cache / rounds[0]
    z = np.load(first / "sample.npz")
    sha = [str(s) for s in z["sha"]]
    ix = {s: i for i, s in enumerate(sha)}
    V = z["vec"].astype(np.float32)
    V /= np.where((nrm := np.linalg.norm(V, axis=1, keepdims=True)) == 0, 1, nrm)
    teacher = read_teacher(first / "teacher.csv")
    T = np.array([teacher.get(s, np.nan) for s in sha])

    hidden = set().union(*(read_shas(f) for f in cache.glob("*/hidden.txt")))
    picks, shown, rejected = [], set(), set()
    for r in rounds:
        p, s, rej = picks_from(replay(cache / r / "judgments.jsonl"), hidden)
        print(f"{r}: {len(p)} picks used, {len(rej)} photos rejected")
        picks += p; shown |= s; rejected |= rej
    picks = [(a, b) for a, b in picks if a not in rejected and b not in rejected]
    W = np.array([ix[a] for a, _ in picks]); L = np.array([ix[b] for _, b in picks])
    D = V[W] - V[L]
    print(f"{len(picks)} picks in all; {len(hidden)} hidden photos left out")
    print(f"teacher agrees with the picks:        {np.mean(T[W] > T[L]):.1%}")
    print(f"taste scorer on held-out picks (c={c}): {cv_accuracy(D, c):.1%}")

    w = fit(D, c)
    taste = V.astype(np.float64) @ w
    out = cache / "taste"
    (out / "img").mkdir(parents=True, exist_ok=True)
    np.savez(out / "model.npz", w=w.astype(np.float32), c=c, model=MODEL, pairs=len(picks))
    with open(out / "scores.csv", "w", newline="") as f:
        wr = csv.writer(f); wr.writerow(["sha", "taste", "teacher"])
        wr.writerows((s, f"{t:.4f}", "" if np.isnan(x) else f"{x:.4f}") for s, t, x in zip(sha, taste, T))

    make = z["make"]
    pool = [i for i, s in enumerate(sha) if T[i] >= MIN_SCORE and str(make[i]).strip()
            and s not in hidden and s not in shown]
    order = sorted(pool, key=lambda i: -taste[i])
    top, bottom = order[:show], order[::-1][:show]
    cells = lambda idx: "".join(f'<img loading=lazy src="img/{sha[i]}.jpg" alt="">' for i in idx)
    intro = (f"Scored by a model trained on {len(picks)} of your picks. These are from the {len(pool):,} good photos "
             f"in the sample that you were never shown in a pair, so none of them taught it anything.")
    (out / "index.html").write_text(PAGE % {"intro": intro, "n": len(top), "top": cells(top), "bottom": cells(bottom)})
    (out / "needed.txt").write_text("".join(f"{sha[i]}.jpg\n" for i in sorted(set(top) | set(bottom))))
    print(f"wrote {out}/model.npz, scores.csv, index.html; {len(set(top) | set(bottom))} images needed (needed.txt)")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", default=str(Path.home() / "Library/Caches/photo-estate-aesthetics"))
    ap.add_argument("--rounds", nargs="+", default=["round2", "round3"])
    ap.add_argument("--c", type=float, default=1.0)
    ap.add_argument("--show", type=int, default=60)
    a = ap.parse_args()
    run(a.cache, a.rounds, a.c, a.show)


if __name__ == "__main__":
    main()
