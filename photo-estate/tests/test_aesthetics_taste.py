#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy"]
# ///
"""The library's quality ridge (tools/aesthetics/score_library.py) recovers a planted score and picks its own
alpha. The taste scorer (tools/aesthetics/train_taste.py): which answers count as picks (decided, first-time, with no
hidden or rejected photo), a fit that recovers a planted taste and beats chance on held-out pairs, and the files a
run writes, whose page shows only good photos that were in no pair. Synthetic data only."""
import csv, json, sys, tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools" / "aesthetics"))
import train_taste as tt  # noqa: E402
import score_library as sl  # noqa: E402


def rec(pair, a, b, choice, **more):
    return {"event": "answer", "pair": pair, "stratum": "similar", "sha_a": a, "sha_b": b, "choice": choice, **more}


def test_picks():
    A, B, C, D, E, F, G, H = (f"{i:064x}" for i in range(8))
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "judgments.jsonl"
        lines = [rec(0, A, B, "a"), rec(1, C, D, "b"), rec(2, E, F, "same"), rec(3, G, H, "reject", rejected=H),
                 rec(4, A, B, "b", repeat_of=0), rec(5, E, G, "a"), {"event": "undo", "pair": 5}, rec(5, E, G, "b"),
                 rec(6, B, H, "a")]
        log.write_text("".join(json.dumps(l) + "\n" for l in lines) + '{"torn')
        ans = tt.replay(log)
        picks, shown, rejected = tt.picks_from(ans)
        # 0 and 1 decided; 2 is "same"; 3 a reject; 4 a repeat; 5 re-answered after undo; 6 holds the rejected H
        assert picks == [(A, B), (D, C), (G, E)], picks
        assert rejected == {H} and shown == {A, B, C, D, E, F, G, H}
        assert tt.picks_from(ans, exclude={C})[0] == [(A, B), (G, E)], "a hidden photo's pair is no pick"


def test_fit_recovers_a_taste():
    rng = np.random.default_rng(5)
    k, n = 24, 600
    V = rng.normal(size=(2 * n, k)); V /= np.linalg.norm(V, axis=1, keepdims=True)
    true = rng.normal(size=k)
    s = V @ true * 4
    a, b = np.arange(n), np.arange(n, 2 * n)
    a_wins = rng.random(n) < 1 / (1 + np.exp(-(s[a] - s[b])))     # a noisy judge
    D = np.where(a_wins[:, None], V[a] - V[b], V[b] - V[a])
    w = tt.fit(D, c=10.0)
    cos = w @ true / np.linalg.norm(w) / np.linalg.norm(true)
    assert cos > 0.9, cos
    acc = tt.cv_accuracy(D, c=10.0, folds=5, reps=2)
    assert 0.65 < acc < 0.95, acc
    assert abs(tt.cv_accuracy(rng.normal(size=(300, k)), c=1.0, folds=5, reps=2) - 0.5) < 0.1, "no signal: chance"
    assert np.linalg.norm(tt.fit(D, c=0.01)) < np.linalg.norm(w), "smaller c shrinks the weights"


def test_run_writes_files():
    rng = np.random.default_rng(7)
    n, k = 400, 16
    V = rng.normal(size=(n, k)).astype(np.float32)
    sha = [f"{i:064x}" for i in range(n)]
    teacher = rng.uniform(3.0, 7.0, n)
    make = np.where(np.arange(n) % 10 == 0, "", "Apple")
    true = rng.normal(size=k)
    s = (V / np.linalg.norm(V, axis=1, keepdims=True)) @ true
    with tempfile.TemporaryDirectory() as tmp:
        cache = Path(tmp); r = cache / "r1"; r.mkdir()
        np.savez(r / "sample.npz", sha=np.array(sha), vec=V, make=make)
        with open(r / "teacher.csv", "w", newline="") as f:
            csv.writer(f).writerows([["sha", "score"], *zip(sha, teacher)])
        hidden = set(sha[300:320])
        (r / "hidden.txt").write_text("\n".join(sorted(hidden)) + "\n")
        lines = [rec(p, sha[2 * p], sha[2 * p + 1], "a" if s[2 * p] > s[2 * p + 1] else "b") for p in range(100)]
        lines.append(rec(100, sha[200], sha[201], "reject", rejected=sha[201]))
        (r / "judgments.jsonl").write_text("".join(json.dumps(l) + "\n" for l in lines))
        out = tt.run(cache, ["r1"], 1.0, 10)
        m = np.load(out / "model.npz")
        assert m["w"].shape == (k,) and int(m["pairs"]) == 100 and str(m["model"]) == tt.MODEL
        rows = list(csv.DictReader(open(out / "scores.csv")))
        assert [x["sha"] for x in rows] == sha
        taste = np.array([float(x["taste"]) for x in rows])
        assert np.corrcoef(taste, s)[0, 1] > 0.5, "the scores follow the planted taste"
        needed = (out / "needed.txt").read_text().split()
        page = (out / "index.html").read_text()
        assert len(needed) == 20 and all(f"img/{x}" in page for x in needed)
        shown = set(sha[:202])
        for x in needed:
            i = sha.index(x.removesuffix(".jpg"))
            assert teacher[i] >= tt.MIN_SCORE and make[i] and sha[i] not in hidden and sha[i] not in shown
        assert "teacher" not in page, "the page says nothing of the teacher's scores"


def test_library_quality_ridge():
    rng = np.random.default_rng(9)
    n, k = 2000, 16
    X = sl.unit(rng.normal(size=(n, k)))
    assert np.allclose(np.linalg.norm(X, axis=1), 1, atol=1e-5) and sl.unit(np.zeros((1, k))).sum() == 0
    true = rng.normal(size=k)
    y = X @ true * 2 + 4.5 + rng.normal(0, 0.2, n)
    w, b = sl.fit_ridge(X, y, 1e-6)
    assert np.allclose(w, true * 2, atol=0.15) and abs(b - 4.5) < 0.05, "nearly no penalty: least squares"
    assert np.linalg.norm(sl.fit_ridge(X, y, 100.0)[0]) < np.linalg.norm(w), "a larger alpha shrinks the weights"
    w, b, fit = sl.fit_quality(X, y)
    assert fit["alpha"] in sl.ALPHAS and fit["n_held"] == n // 5
    assert fit["spearman"] > 0.8 and fit["mae"] < 0.3, fit
    assert abs(sl.spearman(np.arange(50), np.arange(50) ** 3) - 1) < 1e-9
    assert abs(sl.spearman(np.arange(50), -np.arange(50)) + 1) < 1e-9


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("aesthetics taste: ok")
