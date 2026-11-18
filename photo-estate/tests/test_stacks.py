#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy"]
# ///
"""stacks.py: shots taken together that look alike are one stack, a burst chains into one stack, photos that are
far apart in time or in look (or undated, or left out) stay on their own, and the photo on top is the best one,
or the earliest when there is nothing to rank by."""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import stacks  # noqa: E402


def vecs(*angles):
    """Unit vectors in a plane: two are cos(difference of angles) alike. 0.2 rad apart is 0.98; 0.3 is 0.955."""
    return np.array([[np.cos(a), np.sin(a), 0.0] for a in angles], np.float32)


def test_seconds():
    a = stacks.seconds("2020-01-05T10:00:00+00:00")
    assert stacks.seconds("2020-01-05T10:00:03+00:00") - a == 3
    assert stacks.seconds("2020-01-05T10:00:00Z") == a and stacks.seconds("2020-01-05T10:00:00") == a
    assert stacks.seconds("2020-01-05T12:00:00+02:00") == a
    assert all(np.isnan(stacks.seconds(x)) for x in ("", None, "sometime"))


def test_group():
    nan = float("nan")
    #            0     1     2     3      4      5     6      7     8
    secs = [100.0, 103.0, 109.0, 200.0, 205.0, 300.0, nan, 320.0, 101.0]
    M = vecs(0.0, 0.1, 0.25, 0.0, 1.0, 2.0, 2.0, 2.0, 3.0)
    got = stacks.group(secs, M)
    # 0-1-2 chain (0 and 2 are 0.969 alike, under the bar, but each is near 1); 8 is between them in time but looks
    # different; 3 and 4 are close in time but not alike; 5 and 7 are alike but 20 s apart; 6 has no date
    assert got.tolist() == [0, 0, 0, -1, -1, -1, -1, -1, -1], got
    assert stacks.group(secs, M, gap=30).tolist() == [0, 0, 0, -1, -1, 5, -1, 5, -1]
    assert stacks.group(secs, M, sim=0.5).tolist() == [0, 0, 0, 3, 3, -1, -1, -1, -1]
    use = np.ones(9, bool); use[1] = False                    # without photo 1 nothing joins 0 and 2
    assert (stacks.group(secs, M, use) == -1).all()
    assert stacks.group(secs, M, look=1).tolist() == [-1, 1, 1, -1, -1, -1, -1, -1, -1], \
        "photo 8 sits between 0 and 1 in time: looking back one photo only, 0 joins nothing"
    assert stacks.group([], np.zeros((0, 3), np.float32)).tolist() == []
    assert stacks.group([5.0], vecs(0.0)).tolist() == [-1]
    # the same shot saved twice at the same moment, given in any order: named by the earlier position in time order
    assert stacks.group([50.0, 10.0, 50.0], vecs(0.0, 1.0, 0.0)).tolist() == [0, -1, 0]
    # a 40-frame burst over 20 seconds is one stack
    burst = stacks.group(np.arange(40) * 0.5, vecs(*(np.arange(40) * 0.05)))
    assert (burst == 0).all()


def test_tops():
    stack_of = np.array([0, 0, 0, -1, 4, 4, -1])
    top, size = stacks.tops(stack_of)
    assert top.tolist() == [True, False, False, True, True, False, True], "no rank: the photo that names the stack"
    assert size.tolist() == [3, 3, 3, 1, 2, 2, 1]
    rank = np.array([5, 2, 9, 0, 7, 1, 3])                    # 0 is the best
    top, size = stacks.tops(stack_of, rank)
    assert top.tolist() == [False, True, False, True, False, True, True], "the best member of each stack"
    assert size.tolist() == [3, 3, 3, 1, 2, 2, 1]
    top, size = stacks.tops(np.full(4, -1))
    assert top.all() and (size == 1).all()
    assert stacks.tops(np.array([], np.int64))[0].tolist() == []
    # exactly one top per stack, whatever the ranks
    rng = np.random.default_rng(0)
    so = np.repeat(np.arange(0, 60, 3), 3)
    top, _ = stacks.tops(so, rng.permutation(60))
    assert np.bincount(so[top], minlength=60)[::3].tolist() == [1] * 20


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("stacks: ok")
