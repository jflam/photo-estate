"""
compare.py — an experiment in the photo app (`embed.py serve --compare-vectors DIR`): the same searches
ranked by the app's own image vectors and by a second model's, side by side, so a person can judge which
finds their photos better before the app ever switches models.

A run goes through a fixed list of searches (compare-terms.txt, or terms.txt beside the vectors). For each
the page shows two unlabelled columns in random order and takes a vote; which model was which is not said
until the end. Finishing the run writes a report (the counts, a sign test, each search's result, how much
the two columns shared, how long each model took) and lets the second model go (encoder.py: it was loaded
for the run, and is memory nobody is using once it ends). Every run asks the same searches, so the reports
of two candidate models can be read side by side.

  GET  /compare                 the page: start or carry on a run, vote, read the report
  GET  /api/compare/info        both models, the searches, whether the second model is loaded, the run under
                                way, the reports of finished runs, the votes
  POST /api/compare/run         {fresh?} -> the run under way, or a new one; loads the second model
  GET  /api/compare             ?q= (and the filters /api/search takes) &k= -> the top k by each model
  POST /api/compare/vote        {q, winner: "a"|"b"|"same"|"skip", left, run?, overlap?, a_ms?, b_ms?}
                                -> appended to the votes file, and kept in the run
  POST /api/compare/run/finish  {run, early?} -> the report; the second model is let go
  GET  /api/compare/report      ?run=ID -> a finished run's report

  Compare(live, browse, vectors, encoder, votes)   .handle(method, path, qs, body, who) -> reply or None
  read_terms(folder)   report(run)   sign_test(a, b)

A run is a file, runs/run-<id>.json beside the votes, written again at every vote, so a run can be left and
carried on (also after a restart); its report is in it once it is finished, and beside it as <id>.md.

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
import math
import os
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


TERMS = Path(__file__).resolve().parent / "compare-terms.txt"
K = 24                      # photos a column, in a run: enough to judge, few enough to look at each one
OUTCOMES = ("a", "b", "same", "skip")


def read_terms(folder: Path | None = None) -> list[str]:
    """The searches a run asks: terms.txt in the vectors folder if there is one, else compare-terms.txt.
    One a line; blank lines and lines starting with # are left out, and a search said twice is asked once."""
    f = folder / "terms.txt" if folder and (folder / "terms.txt").exists() else TERMS
    out: list[str] = []
    for line in f.read_text().splitlines():
        t = line.strip()
        if t and not t.startswith("#") and t not in out:
            out.append(t[:500])
    return out


def sign_test(a: int, b: int) -> float:
    """How often a split at least this uneven comes up when the two are equally good (two-sided; the
    searches judged the same are left out, as the sign test does). 1.0 with nothing to go on."""
    n, k = a + b, max(a, b)
    if n == 0:
        return 1.0
    return min(1.0, 2 * sum(math.comb(n, i) for i in range(k, n + 1)) / 2 ** n)


def report(run: dict) -> dict:
    """What a run found, from its answers: {a, b, counts, p, verdict, mean overlap and times, rows}."""
    rows, count = [], {o: 0 for o in OUTCOMES}
    for t in run["terms"]:
        ans = run["answers"].get(t)
        if ans:
            count[ans["winner"]] += 1
        rows.append({"term": t, **{f: (ans or {}).get(f) for f in ("winner", "left", "overlap", "a_ms", "b_ms")}})
    mean = lambda f: (lambda v: round(sum(v) / len(v), 1) if v else None)([r[f] for r in rows if isinstance(r[f], (int, float))])
    a, b, p = count["a"], count["b"], sign_test(count["a"], count["b"])
    judged = a + b + count["same"]
    short = lambda m: m.split("/")[-1]
    if not judged:
        verdict = "No search was judged."
    elif a == b:
        verdict = f"No difference: each model was better on {a} of {judged} searches."
    else:
        win, lose = (run["b"], run["a"]) if b > a else (run["a"], run["b"])
        sure = ("a clear difference" if p < 0.05 else "it leans that way, but as uneven a split comes up by chance about"
                f" {'1 time in ' + str(round(1 / p)) if p < 0.5 else 'half the time'} between equal models")
        verdict = (f"{short(win)} was better on {max(a, b)} of {judged} searches, {short(lose)} on {min(a, b)},"
                   f" and {count['same']} were the same: {sure}.")
    return {"id": run["id"], "started": run["started"], "finished": run.get("finished"), "who": run.get("who"),
            "a": run["a"], "b": run["b"], "k": run["k"], "photos": run.get("photos"), "terms": len(run["terms"]),
            "count": count, "unanswered": len(run["terms"]) - sum(count.values()), "p": round(p, 4), "verdict": verdict,
            "overlap": mean("overlap"), "a_ms": mean("a_ms"), "b_ms": mean("b_ms"), "rows": rows}


def markdown(r: dict) -> str:
    """The report as a file to keep (runs/<id>.md)."""
    c, cell = r["count"], lambda v: "" if v is None else str(v)
    who = {"a": r["a"], "b": r["b"], "same": "the same", "skip": "skipped", None: "not asked"}
    lines = [f"# Model comparison {r['id']}", "",
             f"- The app's model (a): `{r['a']}`", f"- The model compared (b): `{r['b']}`",
             f"- {r['terms']} searches, the top {r['k']} photos of each, over {cell(r['photos'])} photos both models have vectors for",
             f"- Run {r['started']} to {cell(r['finished'])}" + (f" by {r['who']}" if r.get("who") else ""), "",
             f"**{r['verdict']}**", "",
             "| Better | Searches |", "|---|---|", f"| `{r['b']}` | {c['b']} |", f"| `{r['a']}` | {c['a']} |",
             f"| The same | {c['same']} |", f"| Skipped | {c['skip']} |", f"| Not asked | {r['unanswered']} |", "",
             f"Sign test over the {c['a'] + c['b']} searches with a winner: p = {r['p']}.",
             f"Photos in both columns, on average: {cell(r['overlap'])} of {r['k']}."
             f" Reading the words took {cell(r['a_ms'])} ms (a) and {cell(r['b_ms'])} ms (b) on average.", "",
             "| Search | Better | In both columns | a, ms | b, ms |", "|---|---|---|---|---|"]
    lines += [f"| {x['term']} | {who[x['winner']]} | {cell(x['overlap'])} | {cell(x['a_ms'])} | {cell(x['b_ms'])} |" for x in r["rows"]]
    return "\n".join(lines) + "\n"


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
        self.terms, self.runs = read_terms(vectors), votes.parent / "runs"
        self.run = self._unfinished()       # the run under way, if one was left (under self.lock)
        print(f"compare: {len(shas)} vectors of {self.model_b} ({self.MB.shape[1]} numbers each) "
              f"loaded in {time.time() - t:.1f}s", flush=True)

    def aligned(self, c: Catalog) -> NDArray[np.int64]:
        """For the catalog being served: each photo's row in the second model's matrix, or -1."""
        if self._aligned[0] is not c:
            self._aligned = (c, np.array([self.row_of.get(s, -1) for s in c.shas], np.int64))
        return self._aligned[1]

    def read(self, side: str, text: str):
        """(the query's vector by that side's model, the ms its model took to read the words). Kept per search,
        so asking again costs nothing and still says what the reading took: the app keeps its own model's
        vectors too (browse.text_vec), and a time taken there is the time of a lookup, 0 ms."""
        with self.lock:
            if (side, text) in self.cache:
                return self.cache[side, text]
        t = time.time()
        v = (self.browse.encoder if side == "a" else self.encoder).text(text)
        got = (v, round((time.time() - t) * 1000, 1))
        with self.lock:
            self.cache[side, text] = got
            if len(self.cache) > 128:
                self.cache.pop(next(iter(self.cache)))
        return got

    # -- runs ----------------------------------------------------------------------------------

    def _unfinished(self) -> dict | None:
        for f in sorted(self.runs.glob("run-*.json"), reverse=True):
            try:
                run = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if not run.get("finished"):
                return run
        return None

    def _save(self, run: dict) -> None:          # under self.lock; whole or not at all
        self.runs.mkdir(parents=True, exist_ok=True)
        tmp = self.runs / f".run-{run['id']}.tmp"
        tmp.write_text(json.dumps(run, indent=1))
        os.replace(tmp, self.runs / f"run-{run['id']}.json")

    def _brief(self, run: dict) -> dict:
        return {"id": run["id"], "started": run["started"], "terms": run["terms"], "k": run["k"],
                "answers": {t: a["winner"] for t, a in run["answers"].items()}}

    def reports(self) -> list[dict]:
        """The finished runs, newest first: enough of each to list it."""
        out = []
        for f in sorted(self.runs.glob("run-*.json"), reverse=True):
            try:
                r = json.loads(f.read_text()).get("report")
            except (OSError, ValueError):
                continue
            if r:
                out.append({k: r[k] for k in ("id", "finished", "a", "b", "count", "terms", "verdict")})
        return out

    def run_start(self, body, who: str) -> tuple:
        """The run under way, or a new one ({fresh: true} gives up the one under way). Starts loading the
        second model: the page waits for b.ready before it asks the first search."""
        fresh = isinstance(body, dict) and body.get("fresh") is True
        with self.lock:
            if self.run and fresh:
                self.run["finished"], self.run["abandoned"] = self._now(), True
                self._save(self.run)
                self.run = None
            if not self.run:
                c = self.live.cat
                rid = stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                n = 1
                while (self.runs / f"run-{rid}.json").exists():   # two in a second: never write over a run
                    n += 1
                    rid = f"{stamp}-{n}"
                self.run = {"id": rid, "started": self._now(), "who": who or None,
                            "a": self.browse.model, "b": self.model_b, "k": K, "terms": list(self.terms),
                            "photos": int((self.aligned(c) >= 0).sum()), "answers": {}}
                self._save(self.run)
            brief = self._brief(self.run)
        self.encoder.ensure()
        return js(brief)

    def run_finish(self, body, who: str) -> tuple:
        """End the run: its report, written into the run and beside it as Markdown, and the second model let
        go. Every search must have an answer, unless {early: true}."""
        with self.lock:
            run = self.run
            if not run or not isinstance(body, dict) or body.get("run") != run["id"]:
                return js({"error": "no such run under way"}, 404)
            left = [t for t in run["terms"] if t not in run["answers"]]
            if left and body.get("early") is not True:
                return js({"error": f"{len(left)} searches have no answer yet", "left": left}, 409)
            run["finished"] = self._now()
            run["report"] = rep = report(run)
            self._save(run)
            (self.runs / f"{run['id']}.md").write_text(markdown(rep))
            self.run = None
            self.cache.clear()
        self.encoder.let_go()               # the experiment is over: its model is memory nobody is using
        return js(rep)

    def report_of(self, qs: dict) -> tuple:
        rid = (qs.get("run") or [""])[0]
        f = self.runs / f"run-{rid}.json"
        if not rid.replace("-", "").isdigit() or not f.exists():
            return js({"error": "no such run"}, 404)
        rep = json.loads(f.read_text()).get("report")
        return js(rep) if rep else js({"error": "that run is not finished"}, 409)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def info(self) -> dict:
        c = self.live.cat
        covered = int((self.aligned(c) >= 0).sum())
        with self.lock:
            run = self._brief(self.run) if self.run else None
        return {"a": {"model": self.browse.model, "photos": int(c.N), "dim": int(c.M.shape[1])},
                "b": {"model": self.model_b, "photos": len(self.row_of), "dim": int(self.MB.shape[1]),
                      "ready": self.encoder.state.snapshot().phase is Phase.READY,
                      "failed": self.encoder.state.snapshot().error if self.encoder.state.snapshot().phase is Phase.FAILED else None,
                      "meta": self.meta},
                "both": covered, "votes": dict(self.tally), "terms": list(self.terms), "k": K,
                "run": run, "reports": self.reports()}

    def search(self, qs: dict, member: int | None = None) -> tuple:
        v = self.live.visible_for(member)   # the photos `member` sees, as every route has them
        c = v.cat
        one = lambda k, d="": (qs.get(k) or [d])[0]
        k = int_param(qs, "k", 48, min=1, max=200)   # not a number: a 400 (http_util)
        masks, text, applied, *_ = self.browse.select(v, qs)
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
        mask = v.combine(masks) & (rows >= 0)
        n = int(mask.sum())
        out = {"q": one("q"), "text": text, "applied": applied, "candidates": n}
        ids = np.flatnonzero(mask)
        for side in ("a", "b"):
            vec, text_ms = self.read(side, text)   # reading the words (kept after the first time), then ranking the photos
            t1 = time.time()
            # the whole matrix times the query, then the chosen photos: no copy of the matrix per query
            sims = (c.M @ vec)[ids] if side == "a" else (self.MB @ vec)[rows[ids]]
            top = np.argsort(-sims, kind="stable")[:k]
            out[side] = {"model": self.browse.model if side == "a" else self.model_b,
                         "text_ms": text_ms, "rank_ms": round((time.time() - t1) * 1000, 1),
                         "results": [{"id": c.shas[ids[i]], "score": round(float(sims[i]), 4)} for i in top]}
        out["overlap"] = len({r["id"] for r in out["a"]["results"]} & {r["id"] for r in out["b"]["results"]})
        return js(out)

    def vote(self, body, who: str) -> tuple:
        """A vote. With `run`, it is that run's answer to one of its searches (and may be "skip": not judged);
        the same search answered again replaces the answer, and counts once."""
        rid = body.get("run") if isinstance(body, dict) else None
        if not isinstance(body, dict) or not body.get("q") or body.get("winner") not in (OUTCOMES if rid else OUTCOMES[:3]):
            return js({"error": "expected {q, winner: a|b|same} (or skip, in a run)"}, 400)
        num = lambda k: body[k] if isinstance(body.get(k), (int, float)) and not isinstance(body[k], bool) else None
        q, winner = str(body["q"])[:500], body["winner"]
        rec = {"t": self._now(), "q": q, "winner": winner, "left": body.get("left") if body.get("left") in ("a", "b") else None,
               "k": body.get("k") if isinstance(body.get("k"), int) else None,
               "a": self.browse.model, "b": self.model_b, "who": who or None, "run": rid}
        with self.lock:
            run, was = None, None
            if rid is not None:
                run = self.run
                if not run or run["id"] != rid:
                    return js({"error": "no such run under way"}, 404)
                if q not in run["terms"]:
                    return js({"error": "that search is not one of this run's"}, 400)
                was = run["answers"].get(q, {}).get("winner")
                run["answers"][q] = {"winner": winner, "left": rec["left"], "overlap": num("overlap"),
                                     "a_ms": num("a_ms"), "b_ms": num("b_ms"), "t": rec["t"]}
                self._save(run)
            if winner != "skip":
                with open(self.votes, "a") as f:
                    f.write(json.dumps({k: v for k, v in rec.items() if v is not None}) + "\n")
            if was in self.tally:
                self.tally[was] -= 1
            if winner in self.tally:
                self.tally[winner] += 1
            out = {"ok": True, "votes": dict(self.tally)}
            if run:
                out.update(done=len(run["answers"]), of=len(run["terms"]))
        return js(out)

    def handle(self, method: str, path: str, qs: dict, body, who: str = "", member: int | None = None):
        if method == "GET" and path == "/api/compare/info":
            return js(self.info())
        if method == "GET" and path == "/api/compare":
            return self.search(qs, member)
        if method == "GET" and path == "/api/compare/report":
            return self.report_of(qs)
        if method == "POST" and path == "/api/compare/vote":
            return self.vote(body, who)
        if method == "POST" and path == "/api/compare/run":
            return self.run_start(body, who)
        if method == "POST" and path == "/api/compare/run/finish":
            return self.run_finish(body, who)
        return None
