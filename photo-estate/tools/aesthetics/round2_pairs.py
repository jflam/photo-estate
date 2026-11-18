#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy"]
# ///
"""
round2_pairs.py — aesthetics pilot round two (issue #44): pairs drawn only from photos the teacher scores well, so
the judge measures taste rather than "is this a real photo". Reads <dir>/sample.npz and <dir>/teacher.csv, writes
<dir>/pairs.json (the queue judge.py serves) and <dir>/needed.txt (the shas whose images are needed).

  uv run round2_pairs.py ~/Library/Caches/photo-estate-aesthetics/round2 [--min-score 5.0] [--seed 46]

Pool: teacher score >= --min-score and a non-empty camera make. 500 pairs in which no photo appears twice:
  same_day    200  same calendar date, cosine < 0.92 (not near-identical frames), at most 3 pairs per date
  similar     150  different dates, cosine in [0.60, 0.92)
  good_random 150  random pool photos on different dates
(strata interleaved round-robin), then 50 `repeat` entries: pairs shown a second time, sides swapped, at least 100
positions after the original, to measure self-consistency. Photos without a taken_at date are not used (a date is
needed to say "same day" or "different dates"). A stratum that cannot be filled takes what exists and says so.
Refuses to replace a pairs.json that differs while judgments.jsonl exists.

A later round (fetch_round3.sh) passes --exclude FILE (one sha per line, a trailing .jpg allowed; may be given more
than once): those photos are left out of the pool. It is how a round leaves out the photos the library has since
hidden, the ones an earlier round already showed, and the ones rejected while judging.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

import numpy as np

SEED = 46
MIN_SCORE = 5.0
STRATA = ("same_day", "similar", "good_random")
COUNTS = {"same_day": 200, "similar": 150, "good_random": 150}
SIM_LO, SIM_HI = 0.60, 0.92
DAY_CAP = 3
N_REPEATS = 50
MIN_GAP = 100
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


# ------------------------------------------------------------------ pure pair logic

def day_of(taken_at):
    t = str(taken_at or "").strip()
    return t[:10] if DATE_RE.match(t) else None


def pick_same_day(rng, vec, dates, pool, used, n, cap=DAY_CAP, sim_max=SIM_HI):
    by_date = {}
    for i in pool:
        if dates[i]:
            by_date.setdefault(dates[i], []).append(int(i))
    keys = sorted(by_date)
    order = rng.permutation(len(keys))
    got = dict.fromkeys(keys, 0)
    out = []
    for rnd in range(cap):
        for k in order:
            if len(out) >= n:
                return out
            d = keys[k]
            if got[d] > rnd:
                continue
            free = [i for i in by_date[d] if i not in used]
            if len(free) < 2:
                continue
            free = [free[x] for x in rng.permutation(len(free))]
            for a in free[:20]:
                rest = np.array([j for j in free if j != a])
                cand = rest[(vec[rest] @ vec[a]) < sim_max]
                if len(cand):
                    b = int(cand[rng.integers(len(cand))])
                    used.update((a, b)); got[d] += 1; out.append((a, b))
                    break
    return out


def pick_similar(rng, vec, dates, pool, used, n, lo=SIM_LO, hi=SIM_HI):
    pool = np.array([i for i in pool if dates[i] and i not in used], dtype=int)
    if len(pool) < 2:
        return []
    V = vec[pool]
    D = np.array([dates[i] for i in pool])
    avail = np.ones(len(pool), bool)
    out = []
    for _ in range(20 * max(n, 1)):
        if len(out) >= n:
            break
        free = np.flatnonzero(avail)
        if len(free) < 2:
            break
        a = int(free[rng.integers(len(free))])
        sims = V @ V[a]
        ok = avail & (sims >= lo) & (sims < hi) & (D != D[a])
        cand = np.flatnonzero(ok)
        if not len(cand):
            continue
        b = int(cand[rng.integers(len(cand))])
        avail[a] = avail[b] = False
        used.update((int(pool[a]), int(pool[b])))
        out.append((int(pool[a]), int(pool[b])))
    return out


def pick_good_random(rng, vec, dates, pool, used, n):
    pool = np.array([i for i in pool if dates[i] and i not in used], dtype=int)
    avail = set(range(len(pool)))
    out = []
    for _ in range(50 * max(n, 1)):
        if len(out) >= n or len(avail) < 2:
            break
        a, b = (int(x) for x in rng.choice(len(pool), 2, replace=False))
        if a in avail and b in avail and dates[pool[a]] != dates[pool[b]]:
            avail -= {a, b}
            used.update((int(pool[a]), int(pool[b])))
            out.append((int(pool[a]), int(pool[b])))
    return out


def allocate(counts, total):
    """Split `total` across strata in proportion to counts (largest remainder), never beyond a stratum's count."""
    s = sum(counts.values())
    if s == 0:
        return dict.fromkeys(counts, 0)
    total = min(total, s)
    raw = {k: total * v / s for k, v in counts.items()}
    out = {k: int(raw[k]) for k in counts}
    for k in sorted(counts, key=lambda k: (-(raw[k] - out[k]), k))[: total - sum(out.values())]:
        out[k] += 1
    return out


def add_repeats(originals, seed=SEED, n=N_REPEATS, gap=MIN_GAP):
    """originals: list of dicts (stratum, sha_a, sha_b, left_sha). Returns the final entry list: originals in order
    with `n` repeats (same pair, sides swapped, stratified over the strata) each inserted at least `gap` positions
    after its original. Entries get index = position; a repeat's repeat_of is its original's index."""
    rng = np.random.default_rng([seed, 7])
    N = len(originals)
    eligible = {s: [i for i in range(max(0, N - gap + 1)) if originals[i]["stratum"] == s] for s in STRATA}
    quota = allocate({s: sum(o["stratum"] == s for o in originals) for s in STRATA}, n)   # proportional to the strata
    quota = {s: min(quota[s], len(eligible[s])) for s in STRATA}
    chosen = []
    for s in STRATA:
        pick = rng.permutation(len(eligible[s]))[: quota[s]]
        chosen += [eligible[s][int(k)] for k in pick]
    rng.shuffle(chosen)
    seq = [{"orig": i, "rep": False} for i in range(N)]
    for i in chosen:
        p = next(k for k, e in enumerate(seq) if e["orig"] == i and not e["rep"])
        # insertions elsewhere never shrink the gap between an original and its repeat
        q = int(rng.integers(p + gap, len(seq) + 1))
        seq.insert(q, {"orig": i, "rep": True})
    pos = {e["orig"]: k for k, e in enumerate(seq) if not e["rep"]}
    out = []
    for k, e in enumerate(seq):
        o = originals[e["orig"]]
        left = o["left_sha"]
        if e["rep"]:
            left = o["sha_b"] if o["left_sha"] == o["sha_a"] else o["sha_a"]
        out.append({"index": k, "stratum": o["stratum"], "sha_a": o["sha_a"], "sha_b": o["sha_b"],
                    "left_sha": left, "repeat_of": pos[e["orig"]] if e["rep"] else None})
    return out


def build_pairs(sha, vec, make, taken_at, score, min_score=MIN_SCORE, seed=SEED, counts=None, n_repeats=N_REPEATS, gap=MIN_GAP,
                exclude=()):
    """Returns (entries, notes, pool_size). All inputs are parallel arrays in sample order. No photo whose sha is
    in `exclude` is used."""
    counts = counts or COUNTS
    N = len(sha)
    vec = np.asarray(vec, dtype=np.float32)
    nrm = np.linalg.norm(vec, axis=1, keepdims=True)
    vec = vec / np.where(nrm == 0, 1, nrm)
    score = np.asarray(score, dtype=float)
    dates = [day_of(t) for t in taken_at]
    pool = [i for i in range(N) if score[i] >= min_score and str(make[i]).strip()]
    notes = []
    if exclude:
        exclude = set(exclude)
        kept = [i for i in pool if sha[i] not in exclude]
        notes.append(f"{len(pool) - len(kept)} pool photos are excluded (hidden, already shown, or rejected)")
        pool = kept
    undated = sum(1 for i in pool if not dates[i])
    if undated:
        notes.append(f"{undated} pool photos have no taken_at date and are not used")
    used, got = set(), {}
    picks = {"same_day": pick_same_day, "similar": pick_similar, "good_random": pick_good_random}
    for k, name in enumerate(STRATA):
        rng = np.random.default_rng([seed, k])
        got[name] = picks[name](rng, vec, dates, pool, used, counts[name])
        if len(got[name]) < counts[name]:
            notes.append(f"stratum {name}: only {len(got[name])} of {counts[name]} pairs available")
    lrng = np.random.default_rng([seed, 99])
    originals = []
    for r in range(max(counts.values())):
        for name in STRATA:
            if r < len(got[name]):
                i, j = got[name][r]
                a, b = sha[i], sha[j]
                originals.append({"stratum": name, "sha_a": a, "sha_b": b, "left_sha": a if lrng.random() < 0.5 else b})
    entries = add_repeats(originals, seed, n_repeats, gap)
    if sum(e["repeat_of"] is not None for e in entries) < n_repeats:
        notes.append(f"only {sum(e['repeat_of'] is not None for e in entries)} of {n_repeats} repeats could be placed")
    return entries, notes, len(pool)


# ------------------------------------------------------------------ files

def read_teacher(path):
    """sha -> score. The file may still be growing: rows that are torn or malformed are skipped."""
    sc = {}
    with open(path, newline="") as f:
        for r in csv.reader(f):
            if len(r) == 2 and SHA_RE.match(r[0]):
                try:
                    sc[r[0]] = float(r[1])
                except ValueError:
                    pass
    return sc


def read_shas(path):
    """The shas in a file of one per line (a trailing .jpg allowed); anything else on a line is an error."""
    out = set()
    for line in Path(path).expanduser().read_text().split():
        s = line.removesuffix(".jpg")
        if not SHA_RE.match(s):
            sys.exit(f"{path}: not a sha: {line[:80]!r}")
        out.add(s)
    return out


def run(d, min_score=MIN_SCORE, seed=SEED, exclude=()):
    d = Path(d).expanduser()
    z = np.load(d / "sample.npz")
    sc = read_teacher(d / "teacher.csv")
    sha = [str(s) for s in z["sha"]]
    have = np.array([s in sc for s in sha])
    score = np.array([sc.get(s, -1e9) for s in sha])
    print(f"sample {len(sha)} photos; teacher scores for {int(have.sum())}")
    entries, notes, pool = build_pairs(sha, z["vec"], z["make"], z["taken_at"], score, min_score, seed, exclude=exclude)
    print(f"pool (score >= {min_score}, non-empty make{', not excluded' if exclude else ''}): {pool}")
    for n in notes:
        print("NOTE:", n)
    orig = [e for e in entries if e["repeat_of"] is None]
    print(f"{len(orig)} pairs " + str({s: sum(e['stratum'] == s for e in orig) for s in STRATA})
          + f" + {len(entries) - len(orig)} repeats")
    body = json.dumps(entries, indent=1) + "\n"
    target = d / "pairs.json"
    if target.exists() and target.read_text() != body:
        if (d / "judgments.jsonl").exists():
            sys.exit(f"REFUSING: {target} exists with different content and judgments.jsonl exists; "
                     "regenerating would orphan your answers")
    target.write_text(body)
    needed = sorted({e[k] for e in entries for k in ("sha_a", "sha_b")})
    (d / "needed.txt").write_text("".join(f"{s}.jpg\n" for s in needed))
    print(f"wrote {target} and {d / 'needed.txt'} ({len(needed)} distinct images)")
    return entries, notes, pool


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dir")
    ap.add_argument("--min-score", type=float, default=MIN_SCORE)
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--exclude", action="append", default=[], metavar="FILE", help="shas to leave out, one per line")
    a = ap.parse_args()
    run(a.dir, a.min_score, a.seed, set().union(*(read_shas(f) for f in a.exclude)))


if __name__ == "__main__":
    main()
