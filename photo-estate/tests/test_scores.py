#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy"]
# ///
"""scores.py: scorers are kept in the catalog per embedding model and replaced by name; a catalog without them
(or with only one, or with weights of another size) has no best-first order; with both, good photos come first
by taste and the rest follow by quality."""
import sqlite3, sys, tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import scores  # noqa: E402

MODEL = "some/embedding-model"


def test_save_and_load():
    with tempfile.TemporaryDirectory() as tmp:
        con = sqlite3.connect(Path(tmp) / "estate.db")
        assert scores.load(con, MODEL) == {}, "a catalog that never had a scorer"
        scores.save(con, "quality", MODEL, [1, 2, 3], 4.5, {"photos": 9})
        scores.save(con, "taste", MODEL, np.array([0.5, 0, -0.5]))
        scores.save(con, "taste", "another/model", [9, 9])
        got = scores.load(con, MODEL)
        assert set(got) == {"quality", "taste"} and got["quality"][1] == 4.5 and got["taste"][1] == 0.0
        assert got["quality"][0].tolist() == [1, 2, 3] and got["quality"][0].dtype == np.float32
        scores.save(con, "taste", MODEL, [1, 1, 1])               # replaced, not added
        assert scores.load(con, MODEL)["taste"][0].tolist() == [1, 1, 1]
        assert con.execute("SELECT count(*) FROM scorers").fetchone()[0] == 3
        assert list(scores.load(con, "another/model")) == ["taste"]
        for bad in (("colour", [1.0], 0.0), ("taste", [], 0.0), ("taste", [float("nan")], 0.0), ("taste", [1.0], float("inf"))):
            try:
                scores.save(con, bad[0], MODEL, bad[1], bad[2])
            except ValueError:
                pass
            else:
                raise AssertionError(f"must refuse {bad}")


def test_best_first():
    # two dimensions: the first is quality, the second taste
    M = np.array([[1.0, 0.0], [0.8, 0.6], [0.6, 0.8], [0.0, 1.0], [0.6, -0.8], [-1.0, 0.0]], np.float32)
    q = (np.array([2.0, 0.0], np.float32), 4.0)       # quality 6.0, 5.6, 5.2, 4.0, 5.2, 2.0
    t = (np.array([0.0, 1.0], np.float32), 0.0)       # taste   0.0, 0.6, 0.8, 1.0, -0.8, 0.0
    both = {"quality": q, "taste": t}
    key = scores.best(M, both)
    # good photos (0, 1, 2, 4) by taste; then photo 3 (the best taste of all, but not a good photo), then 5
    assert scores.best_first(M, both).tolist() == [2, 1, 0, 4, 3, 5], scores.best_first(M, both)
    assert key[3] < key[4], "taste never lifts a photo over the quality bar"
    assert scores.best_first(M, {"quality": q}) is None and scores.best_first(M, {}) is None
    assert scores.best_first(M, {"quality": q, "taste": (np.ones(3, np.float32), 0.0)}) is None, "weights of another size"
    assert scores.best_first(np.zeros((0, 2), np.float32), both) is None
    same = scores.best_first(np.repeat(M[:1], 4, axis=0), both)
    assert same.tolist() == [0, 1, 2, 3], "ties keep their order"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("scores: ok")
