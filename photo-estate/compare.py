"""
compare.py — an experiment in the photo app (`embed.py serve --compare-vectors DIR`): the same search ranked
by the app's own image vectors and by a second model's, side by side, so a person can judge which finds
their photos better before the app ever switches models.

  GET  /compare               the page: a search, two unlabelled columns in random order, a vote
  GET  /api/compare/info      both models, how many photos each covers, whether the second is loaded, the votes
  GET  /api/compare           ?q= (and the filters /api/search takes) &k= -> the top k by each model
  POST /api/compare/vote      {q, winner: "a"|"b"|"same", left: "a"|"b"} -> appended to the votes file

  Compare(live, browse, vectors, encoder, votes)   .handle(method, path, qs, body, who) -> reply or None

The second model's vectors are files, never the catalog: a folder of shard-NNNN.npy (float16, unit rows)
and shard-NNNN.shas.txt (the sha of each row), with meta.json naming the model ("model"), as
tools/embed_squares.py writes them from tools/export_squares.py's shards. Its text side loads in the
background, like the app's own (encoder.py).

The query is read as /api/search reads it (browse.select): names, places and key:value filters choose the
photos, and only the words left over are ranked. Both columns rank the same photos: those the filters
select that both models have a vector for (the export leaves out screenshots and the photos behind a
stack's top, so the app's side is held to those too). Each column is the k closest by its own model, with
no match floor: the app's floors are measured for its own model, and the second model's scores sit on
another scale.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from browse import js
from catalog import Catalog
from core.lifecycle import Phase
from http_util import int_param


def load_vectors(folder: Path) -> tuple[list[str], np.ndarray, dict]:
    """(shas, float16 matrix, meta) from a folder of shard-NNNN.npy + shard-NNNN.shas.txt."""
    meta = json.loads((folder / "meta.json").read_text())
    shas, parts = [], []
    for npy in sorted(folder.glob("shard-*.npy")):
        names = (folder / f"{npy.stem}.shas.txt").read_text().split()
        m = np.load(npy)
        if len(names) != len(m):
            raise ValueError(f"{npy.name}: {len(m)} vectors but {len(names)} shas")
        shas += names
        parts.append(m)
    if not parts:
        raise ValueError(f"no shard-*.npy in {folder}")
    return shas, np.concatenate(parts), meta


class Compare:
    def __init__(self, live, browse, vectors: Path, encoder, votes: Path):
        self.live, self.browse, self.encoder, self.votes = live, browse, encoder, votes
        t = time.time()
        shas, MB, self.meta = load_vectors(vectors)
        self.MB = MB.astype(np.float32)   # once, not per query (83,000 x 1,024 is 340 MB)
        self.row_of = {s: i for i, s in enumerate(shas)}
        self.model_b = self.meta.get("model", "?")
        self.lock, self.cache, self.tally = threading.Lock(), {}, {"a": 0, "b": 0, "same": 0}
        try:
            for line in votes.read_text().splitlines():
                w = json.loads(line).get("winner")
                if w in self.tally:
                    self.tally[w] += 1
        except (OSError, ValueError):
            pass
        self._aligned: tuple[Catalog | None, NDArray[np.int64]] = (None, np.zeros(0, np.int64))
        print(f"compare: {len(shas)} vectors of {self.model_b} ({self.MB.shape[1]} numbers each) "
              f"loaded in {time.time() - t:.1f}s", flush=True)

    def aligned(self, c: Catalog) -> NDArray[np.int64]:
        """For the catalog being served: each photo's row in the second model's matrix, or -1."""
        if self._aligned[0] is not c:
            self._aligned = (c, np.array([self.row_of.get(s, -1) for s in c.shas], np.int64))
        return self._aligned[1]

    def text_b(self, text: str):
        with self.lock:
            if text in self.cache:
                return self.cache[text]
        v = self.encoder.text(text)
        with self.lock:
            self.cache[text] = v
            if len(self.cache) > 64:
                self.cache.pop(next(iter(self.cache)))
        return v

    def info(self) -> dict:
        self.encoder.ensure()   # the page asks this first, and until b.ready: opening /compare is what loads the model
        c = self.live.cat
        covered = int((self.aligned(c) >= 0).sum())
        return {"a": {"model": self.browse.model, "photos": int(c.N), "dim": int(c.M.shape[1])},
                "b": {"model": self.model_b, "photos": len(self.row_of), "dim": int(self.MB.shape[1]),
                      "ready": self.encoder.state.snapshot().phase is Phase.READY, "meta": self.meta},
                "both": covered, "votes": dict(self.tally)}

    def search(self, qs: dict) -> tuple:
        c = self.live.cat
        one = lambda k, d="": (qs.get(k) or [d])[0]
        k = int_param(qs, "k", 48, min=1, max=200)   # not a number: a 400 (http_util)
        masks, text, applied, *_ = self.browse.select(c, qs)
        if not text:
            return js({"error": "no words to rank by: the filters alone choose the same photos for both"}, 400)
        b = self.encoder.state.snapshot()   # don't wait for it here: a request holds the app's gate (catalog.Gate)
        if b.phase is Phase.FAILED:
            return js({"error": f"the second model failed to load: {b.error}"}, 500)
        if b.phase is not Phase.READY:
            self.encoder.ensure()   # let go after a while unused (encoder.py): asking for it loads it again
            return js({"error": "the second model is loading (it is let go when /compare has not been used"
                                " for a while); try again in a minute"}, 503)
        rows = self.aligned(c)
        mask = c.combine(masks) & (rows >= 0)
        n = int(mask.sum())
        out = {"q": one("q"), "text": text, "applied": applied, "candidates": n}
        ids = np.flatnonzero(mask)
        for side in ("a", "b"):
            t = time.time()   # reading the words (cached after the first time), then ranking the photos
            vec = self.browse.text_vec(text) if side == "a" else self.text_b(text)
            t1 = time.time()
            # the whole matrix times the query, then the chosen photos: no copy of the matrix per query
            sims = (c.M @ vec)[ids] if side == "a" else (self.MB @ vec)[rows[ids]]
            top = np.argsort(-sims, kind="stable")[:k]
            out[side] = {"model": self.browse.model if side == "a" else self.model_b,
                         "text_ms": round((t1 - t) * 1000, 1), "rank_ms": round((time.time() - t1) * 1000, 1),
                         "results": [{"id": c.shas[ids[i]], "score": round(float(sims[i]), 4)} for i in top]}
        out["overlap"] = len({r["id"] for r in out["a"]["results"]} & {r["id"] for r in out["b"]["results"]})
        return js(out)

    def vote(self, body, who: str) -> tuple:
        if not isinstance(body, dict) or body.get("winner") not in ("a", "b", "same") or not body.get("q"):
            return js({"error": "expected {q, winner: a|b|same}"}, 400)
        rec = {"t": datetime.now(timezone.utc).isoformat(timespec="seconds"), "q": str(body["q"])[:500],
               "winner": body["winner"], "left": body.get("left") if body.get("left") in ("a", "b") else None,
               "k": body.get("k") if isinstance(body.get("k"), int) else None,
               "a": self.browse.model, "b": self.model_b, "who": who or None}
        with self.lock:
            with open(self.votes, "a") as f:
                f.write(json.dumps({k: v for k, v in rec.items() if v is not None}) + "\n")
            self.tally[body["winner"]] += 1
            tally = dict(self.tally)
        return js({"ok": True, "votes": tally})

    def handle(self, method: str, path: str, qs: dict, body, who: str = ""):
        if method == "GET" and path == "/api/compare/info":
            return js(self.info())
        if method == "GET" and path == "/api/compare":
            return self.search(qs)
        if method == "POST" and path == "/api/compare/vote":
            return self.vote(body, who)
        return None
