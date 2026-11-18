"""
stacks.py: near-identical shots as one stack (issue #45, the first part). A burst, or the same shot saved twice,
shows as one photo with the others behind it; nothing is deleted or changed.

Two photos belong together when they were taken within GAP seconds of each other and their embeddings are at
least SIM alike; a stack is everything joined that way, so a long burst is one stack although its first and last
frames are far apart. On a 106,000-photo library this makes about 13,200 stacks holding 34,600 photos. Each photo
is compared with the LOOK photos taken just before it, so the cost follows the library, not its square.

The photo on top is the best one (scores.py's order), or the earliest in a catalog with no scorers.

Like the scores, stacks are worked out from what the app already holds when it loads the catalog (well under a
second), not stored: a photo that arrives tomorrow joins its stack with no pipeline step. A stack is named by any
of its photos. Still to come from the issue: burst ids, RAW+JPEG pairs, and the owner's own cover and splits.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

GAP = 10.0     # seconds
SIM = 0.97     # cosine similarity; what "Hide near-identical shots" has always gone by
LOOK = 8


def seconds(taken: str) -> float:
    """When a photo was taken, as a number (nan if it has no usable date). A time with no zone is read as UTC:
    only differences between neighbours matter."""
    try:
        d = datetime.fromisoformat((taken or "").replace("Z", "+00:00"))
    except ValueError:
        return float("nan")
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


def group(secs, M, use=None, gap: float = GAP, sim: float = SIM, look: int = LOOK):
    """Stack per photo: an array the length of `secs`, -1 for a photo on its own, else a number shared by the
    photos of one stack (the position of its earliest member). `M` holds unit-length embeddings, a row per
    photo; `use` (bool per photo) leaves photos out altogether, as do missing dates."""
    secs = np.asarray(secs, dtype=np.float64)
    n = len(secs)
    out = np.full(n, -1, np.int64)
    ok = ~np.isnan(secs) if use is None else (~np.isnan(secs) & np.asarray(use, bool))
    ids = np.flatnonzero(ok)
    ids = ids[np.argsort(secs[ids], kind="stable")]
    if len(ids) < 2:
        return out
    t, V = secs[ids], M[ids]
    parent = np.arange(len(ids))

    def root(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for k in range(1, min(look, len(ids) - 1) + 1):
        near = (t[k:] - t[:-k] <= gap) & (np.einsum("ij,ij->i", V[k:], V[:-k]) >= sim)
        for i in np.flatnonzero(near).tolist():
            a, b = root(i + k), root(i)
            if a != b:
                parent[max(a, b)] = min(a, b)   # the earliest member names the stack
    roots = np.array([root(i) for i in range(len(ids))])
    stacked = np.bincount(roots, minlength=len(ids))[roots] > 1
    out[ids[stacked]] = ids[roots[stacked]]
    return out


def tops(stack_of, rank=None):
    """(top, size): `top` is true for the photo shown for its stack and for every photo on its own; `size` is how
    many photos the stack holds (1 for a photo on its own). The top is the member with the lowest `rank` (best
    first), or the one that names the stack (the earliest) when there is no rank."""
    stack_of = np.asarray(stack_of)
    n = len(stack_of)
    top, size = stack_of < 0, np.ones(n, np.int64)
    members = np.flatnonzero(stack_of >= 0)
    if not len(members):
        return top, size
    key = rank[members] if rank is not None else members != stack_of[members]
    order = members[np.lexsort((members, key, stack_of[members]))]
    first = np.concatenate([[True], np.diff(stack_of[order]) != 0])
    top[order[first]] = True
    size[members] = np.bincount(stack_of[members], minlength=n)[stack_of[members]]
    return top, size
