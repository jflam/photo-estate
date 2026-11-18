#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy"]
# ///
"""
judge.py — aesthetics pilot: which of two of your own photos is better? Your answers measure how often the
teacher scorer and the student (ridge, held-out) agree with your taste (issue #44). Everything stays on this
Mac: the server binds 127.0.0.1 and reads only the pilot cache dir (private photos; never copy them anywhere).

  uv run judge.py serve  --dir ~/Library/Caches/photo-estate-aesthetics/pilot [--port 8770]
  uv run judge.py report --dir ~/Library/Caches/photo-estate-aesthetics/pilot

Round two: if <dir> holds a pairs.json (written by round2_pairs.py) it is the queue instead of the round-one strata;
oof.json is then optional (no student column without it), and the report adds self-consistency over the repeated pairs.

Answers are appended to <dir>/judgments.jsonl (never rewritten; undo appends an undo record), so a session
resumes where it stopped. The page never shows a score, stratum or filename.

A photo that should not be in the library's good photos at all (one of the school's, say) can be rejected instead
of judged: the pair is over with no pick, any later pair holding that photo is never shown, and <dir>/rejected.txt
lists the rejected shas (rewritten from the log on every reject and undo; undo takes a reject back like any answer).
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

SEED = 44
PER_STRATUM = 150
STRATA = ("random", "similar", "disagree", "good")
SIM_LO, SIM_HI = 0.60, 0.95
DISAGREE_MIN_GAP = 0.3
GOOD_MIN_SCORE = 4.0
MIN_PAIRS = 30
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
LOG = "judgments.jsonl"
PAIRS = "pairs.json"
REJECTED = "rejected.txt"
R2_STRATA = ("same_day", "similar", "good_random")


# ------------------------------------------------------------------ data

def load_pilot(d):
    """sha, vec, teacher, ridge in train_head.py's order: sample.npz rows that have a teacher score, in
    sample order; oof.json's arrays follow that order. Asserted, not assumed."""
    d = Path(d).expanduser()
    z = np.load(d / "sample.npz")
    sc = {r["sha"]: float(r["score"]) for r in csv.DictReader(open(d / "teacher.csv"))}
    keep = [i for i, s in enumerate(z["sha"]) if s in sc]
    sha = [str(s) for s in z["sha"][keep]]
    vec = z["vec"][keep].astype(np.float32)
    teacher = np.array([sc[s] for s in sha])
    oof = json.load(open(d / "oof.json"))
    assert len(oof["y"]) == len(sha) and len(oof["ridge"]) == len(sha), "oof.json length differs from sample"
    assert np.allclose(oof["y"], teacher), "oof.json y does not match teacher scores in sample order"
    return sha, vec, teacher, np.array(oof["ridge"])


def load_pairs(d):
    """Round-two queue from <dir>/pairs.json: [{index, stratum, sha_a, sha_b, left, repeat_of}], validated."""
    raw = json.load(open(Path(d).expanduser() / PAIRS))
    queue = []
    for k, e in enumerate(raw):
        a, b, left = e["sha_a"], e["sha_b"], e["left_sha"]
        assert e["index"] == k, f"pairs.json entry {k} has index {e['index']}"
        assert SHA_RE.match(a) and SHA_RE.match(b) and a != b and left in (a, b), f"pairs.json entry {k} malformed"
        rep = e.get("repeat_of")
        if rep is not None:
            o = raw[rep]
            assert isinstance(rep, int) and 0 <= rep < k and o.get("repeat_of") is None, f"entry {k}: bad repeat_of"
            assert {o["sha_a"], o["sha_b"]} == {a, b} and o["left_sha"] != left, f"entry {k}: not the swapped original"
        queue.append({"index": k, "stratum": e["stratum"], "sha_a": a, "sha_b": b,
                      "left": "a" if left == a else "b", "repeat_of": rep})
    return queue


def load_teacher_only(d):
    d = Path(d).expanduser()
    out = {}
    for r in csv.reader(open(d / "teacher.csv", newline="")):   # tolerate a torn or header row
        if len(r) == 2 and SHA_RE.match(r[0]):
            try:
                out[r[0]] = float(r[1])
            except ValueError:
                pass
    return out


def load_scores(d, pilot=None):
    """(teacher_by_sha, ridge_by_sha or None) for the report. A pilot tuple's ridge may be None; oof.json is optional."""
    d = Path(d).expanduser()
    if pilot is not None:
        sha, _vec, teacher, ridge = pilot
        return dict(zip(sha, teacher)), (None if ridge is None else dict(zip(sha, ridge)))
    if (d / "oof.json").exists():
        sha, _vec, teacher, ridge = load_pilot(d)
        return dict(zip(sha, teacher)), dict(zip(sha, ridge))
    return load_teacher_only(d), None


# ------------------------------------------------------------------ queue

def _key(i, j):
    return (i, j) if i < j else (j, i)


def _random_pairs(rng, pool, n, used):
    out, tries = [], 0
    while len(out) < n and tries < 200 * max(n, 1) and len(pool) > 1:
        tries += 1
        i, j = (int(x) for x in rng.choice(pool, 2, replace=False))
        if _key(i, j) not in used:
            used.add(_key(i, j)); out.append((i, j))
    return out


def stratum_random(rng, vec, teacher, ridge, n, used):
    return _random_pairs(rng, np.arange(len(teacher)), n, used)


def stratum_good(rng, vec, teacher, ridge, n, used):
    return _random_pairs(rng, np.flatnonzero(teacher >= GOOD_MIN_SCORE), n, used)


def stratum_similar(rng, vec, teacher, ridge, n, used):
    out, N = [], len(teacher)
    for _ in range(20 * max(n, 1)):
        if len(out) >= n:
            break
        a = int(rng.integers(N))
        sims = vec @ vec[a]
        ok = (sims >= SIM_LO) & (sims <= SIM_HI)
        ok[a] = False
        cand = [int(c) for c in np.flatnonzero(ok) if _key(a, int(c)) not in used]
        if not cand:
            continue  # no usable neighbour: another anchor
        b = int(rng.choice(cand))
        used.add(_key(a, b)); out.append((a, b))
    return out


def stratum_disagree(rng, vec, teacher, ridge, n, used):
    dt = teacher[:, None] - teacher[None, :]
    ds = ridge[:, None] - ridge[None, :]
    ok = (dt * ds < 0) & (np.abs(dt) >= DISAGREE_MIN_GAP) & (np.abs(ds) >= DISAGREE_MIN_GAP)
    cand = np.argwhere(np.triu(ok, 1))
    out = []
    for k in rng.permutation(len(cand)):
        if len(out) >= n:
            break
        i, j = int(cand[k][0]), int(cand[k][1])
        if _key(i, j) not in used:
            used.add(_key(i, j)); out.append((i, j))
    return out


_BUILDERS = {"random": stratum_random, "similar": stratum_similar, "disagree": stratum_disagree, "good": stratum_good}


def build_queue(sha, vec, teacher, ridge, seed=SEED, per=PER_STRATUM):
    """Deterministic queue: up to `per` pairs from each stratum, interleaved round-robin, no pair repeated.
    Returns (queue, notes); queue items: {index, stratum, sha_a, sha_b, left} with left in {'a','b'}."""
    used, got, notes = set(), {}, []
    for k, name in enumerate(STRATA):
        rng = np.random.default_rng([seed, k])
        got[name] = _BUILDERS[name](rng, vec, teacher, ridge, per, used)
        if len(got[name]) < per:
            notes.append(f"stratum {name}: only {len(got[name])} of {per} pairs available")
    lrng = np.random.default_rng([seed, 99])
    queue = []
    for r in range(per):
        for name in STRATA:
            if r < len(got[name]):
                i, j = got[name][r]
                queue.append({"index": len(queue), "stratum": name, "sha_a": sha[i], "sha_b": sha[j],
                              "left": "a" if lrng.random() < 0.5 else "b"})
    return queue, notes


# ------------------------------------------------------------------ judgment log

def append_record(path, rec):
    with open(path, "a") as f:
        f.write(json.dumps(rec) + "\n")
        f.flush()


def replay(path):
    """Answers still standing after undos: {pair index: record}, in answer order (dicts keep insertion order).
    An undo removes the pair's current answer; a later answer to the same pair replaces it."""
    answers = {}
    p = Path(path)
    if not p.exists():
        return answers
    for line in p.read_text().splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue  # a torn last line
        if r.get("event") == "undo":
            answers.pop(r.get("pair"), None)
        elif "choice" in r:
            answers.pop(r["pair"], None)
            answers[r["pair"]] = r
    return answers


def rejected_shas(answers):
    """The photos rejected by the answers still standing (replay's dict, or its records)."""
    recs = answers.values() if isinstance(answers, dict) else answers
    return {r["rejected"] for r in recs if r.get("choice") == "reject"}


# ------------------------------------------------------------------ report

def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, centre - half), min(1.0, centre + half))


def report_rows(answers, teacher_by_sha, ridge_by_sha, strata=STRATA):
    """One row per stratum present plus 'overall'. Agreement uses decided answers (a/b) only; a model that
    scores the two photos identically counts as disagreeing. A pair ended by rejecting a photo is no answer."""
    answers = [r for r in answers if r["choice"] != "reject"]
    rows = {}
    for r in answers:
        for name in (r["stratum"], "overall"):
            rows.setdefault(name, {"n": 0, "t": 0, "s": 0, "same": 0, "skip": 0, "gaps": []})
    for r in answers:
        a, b, ch = r["sha_a"], r["sha_b"], r["choice"]
        for name in (r["stratum"], "overall"):
            row = rows[name]
            if ch == "skip":
                row["skip"] += 1
            elif ch == "same":
                row["same"] += 1; row["gaps"].append(abs(teacher_by_sha[a] - teacher_by_sha[b]))
            elif ch in ("a", "b"):
                pick, other = (a, b) if ch == "a" else (b, a)
                row["n"] += 1
                row["t"] += teacher_by_sha[pick] > teacher_by_sha[other]
                if ridge_by_sha is not None:
                    row["s"] += ridge_by_sha[pick] > ridge_by_sha[other]
    order = [s for s in strata if s in rows] + ["overall"]
    out = []
    for name in order:
        if name not in rows:
            continue
        row = rows[name]
        out.append({"stratum": name, "n": row["n"], "teacher": row["t"], "student": row["s"],
                    "teacher_ci": wilson(row["t"], row["n"]), "student_ci": wilson(row["s"], row["n"]),
                    "same": row["same"], "skip": row["skip"],
                    "same_gap": float(np.median(row["gaps"])) if row["gaps"] else None})
    return out


def consistency(answers, queue):
    """Self-consistency over repeat entries. answers: {pair index: record} (replay). Returns (same, both_decided,
    repeats_in_queue): of the repeats whose original and repeat both got a decided answer (a/b), how many times
    the same photo was picked."""
    same = n = total = 0
    for q in queue:
        o = q.get("repeat_of")
        if o is None:
            continue
        total += 1
        r1, r2 = answers.get(o), answers.get(q["index"])
        if r1 and r2 and r1["choice"] in ("a", "b") and r2["choice"] in ("a", "b"):
            n += 1
            same += r1["sha_" + r1["choice"]] == r2["sha_" + r2["choice"]]
    return same, n, total


def format_report(rows, cons=None, student=True, w=9, rejected=0):
    said = f"\nRejected photos (no pick recorded for their pairs; listed in {REJECTED}): {rejected}\n" if rejected else ""
    if not rows:
        return "No answers yet.\n" + said

    def cell(k, n, ci):
        return "-" if n == 0 else f"{k / n:5.1%} [{ci[0]:.1%}, {ci[1]:.1%}]"

    head = f"{'stratum':{w}}{'pairs':>6}  {'teacher agrees (95% CI)':27}" + (f"{'student agrees (95% CI)':27}" if student else "") \
        + f"{'same':>5}{'skip':>5}  median |teacher gap| on same"
    lines = ["Agreement with your picks (decided answers only; Wilson 95% intervals)", head, "-" * len(head)]
    small = []
    for r in rows:
        gap = "-" if r["same_gap"] is None else f"{r['same_gap']:.2f}"
        flag = "  <30 pairs: too few to conclude" if r["n"] < MIN_PAIRS else ""
        if flag:
            small.append(r["stratum"])
        lines.append(f"{r['stratum']:{w}}{r['n']:>6}  {cell(r['teacher'], r['n'], r['teacher_ci']):27}"
                     + (f"{cell(r['student'], r['n'], r['student_ci']):27}" if student else "")
                     + f"{r['same']:>5}{r['skip']:>5}  {gap}{flag}")
    if small:
        lines.append(f"\nRows with fewer than {MIN_PAIRS} decided pairs ({', '.join(small)}) have wide intervals; do not read them as results.")
    if cons is not None:
        k, n, total = cons
        lo, hi = wilson(k, n)
        lines.append("\nSelf-consistency (repeated pairs, sides swapped; both answers decided; agreement rows above use the original answer only)")
        lines.append(f"picked the same photo both times: {'-' if n == 0 else f'{k / n:.1%}'} ({k} of {n}; Wilson 95% [{lo:.1%}, {hi:.1%}]); "
                     f"{total} repeats in the queue")
        if n < MIN_PAIRS:
            lines.append(f"Fewer than {MIN_PAIRS} comparable repeats: too few to conclude.")
    return "\n".join(lines) + "\n" + said


def _round_context(d, pilot=None):
    """(queue or None, strata, teacher_by_sha, ridge_by_sha) for the report."""
    d = Path(d).expanduser()
    teacher, ridge = load_scores(d, pilot)
    if (d / PAIRS).exists():
        return load_pairs(d), R2_STRATA, teacher, ridge
    return None, STRATA, teacher, ridge


def make_report(d, pilot=None):
    d = Path(d).expanduser()
    queue, strata, teacher, ridge = _round_context(d, pilot)
    answers = replay(d / LOG)
    nrej = len(rejected_shas(answers))
    if queue is None:
        return format_report(report_rows(list(answers.values()), teacher, ridge, strata), rejected=nrej)
    reps = {q["index"] for q in queue if q["repeat_of"] is not None}
    first = [r for k, r in answers.items() if k not in reps]   # repeats are never double-counted in agreement
    return format_report(report_rows(first, teacher, ridge, strata), consistency(answers, queue), student=ridge is not None, w=12,
                         rejected=nrej)


# ------------------------------------------------------------------ app state

class Judge:
    def __init__(self, d, pilot=None):
        self.dir = Path(d).expanduser()
        if (self.dir / PAIRS).exists():
            self.queue, self.notes = load_pairs(self.dir), []
            self.shas = {q[k] for q in self.queue for k in ("sha_a", "sha_b")}
            self.pilot = pilot or self._round2_scores()
            self.strata = R2_STRATA
        else:
            sha, vec, teacher, ridge = pilot or load_pilot(self.dir)
            self.pilot = (sha, vec, teacher, ridge)
            self.shas = set(sha)
            self.queue, self.notes = build_queue(sha, vec, teacher, ridge)
            self.strata = STRATA
        self.log = self.dir / LOG
        self.lock = threading.Lock()

    def _round2_scores(self):
        teacher, ridge = load_scores(self.dir)
        shas = list(teacher)
        return (shas, None, np.array([teacher[s] for s in shas]), None if ridge is None else np.array([ridge[s] for s in shas]))

    def _view(self, item):
        if item is None:
            return None
        l, r = (item["sha_a"], item["sha_b"]) if item["left"] == "a" else (item["sha_b"], item["sha_a"])
        return {"index": item["index"], "left": f"/img/{l}.jpg", "right": f"/img/{r}.jpg"}

    def state(self):
        done = replay(self.log)
        gone = rejected_shas(done)
        left = [q for q in self.queue if q["index"] not in done]
        todo = [q for q in left if q["sha_a"] not in gone and q["sha_b"] not in gone]   # a rejected photo is not shown again
        return {"done": not todo, "answered": len(done), "total": len(self.queue) - (len(left) - len(todo)),
                "rejected": len(gone),
                "pair": self._view(todo[0] if todo else None),
                "upcoming": self._view(todo[1] if len(todo) > 1 else None)}

    def answer(self, index, choice, ms):
        """choice: left | right | same | skip | reject_left | reject_right. Returns False if the pair is unknown or
        already answered."""
        with self.lock:
            if not isinstance(index, int) or not 0 <= index < len(self.queue) or index in replay(self.log):
                return False
            q = self.queue[index]
            left_sha = q["sha_a"] if q["left"] == "a" else q["sha_b"]
            right_sha = q["sha_b"] if q["left"] == "a" else q["sha_a"]
            rejected = {"reject_left": left_sha, "reject_right": right_sha}.get(choice)
            c = "reject" if rejected else {"left": q["left"], "right": "b" if q["left"] == "a" else "a"}.get(choice, choice)
            if c not in ("a", "b", "same", "skip", "reject"):
                return False
            rec = {"event": "answer", "ts": datetime.now(timezone.utc).isoformat(),
                   "pair": index, "stratum": q["stratum"], "sha_a": q["sha_a"], "sha_b": q["sha_b"],
                   "left_sha": left_sha, "choice": c, "ms": int(ms) if isinstance(ms, (int, float)) else None}
            if q.get("repeat_of") is not None:
                rec["repeat_of"] = q["repeat_of"]
            if rejected:
                rec["rejected"] = rejected
            append_record(self.log, rec)
            if rejected:
                self._write_rejected()
            return True

    def _write_rejected(self):
        """rejected.txt follows the log: the photos rejected by the answers still standing, one sha per line."""
        tmp = self.dir / (REJECTED + ".tmp")
        tmp.write_text("".join(s + "\n" for s in sorted(rejected_shas(replay(self.log)))))
        tmp.replace(self.dir / REJECTED)

    def undo(self):
        with self.lock:
            done = replay(self.log)
            if not done:
                return False
            last = list(done)[-1]
            append_record(self.log, {"event": "undo", "ts": datetime.now(timezone.utc).isoformat(), "pair": last})
            if done[last]["choice"] == "reject":
                self._write_rejected()
            return True

    def report(self):
        return make_report(self.dir, self.pilot)


# ------------------------------------------------------------------ http

PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Which is better?</title>
<style>
html,body{margin:0;height:100%;background:#111;color:#bbb;font:14px system-ui,sans-serif}
body{display:flex;flex-direction:column}
#stage{flex:1;display:flex;min-height:0;gap:2px}
.pane{flex:1;min-width:0;min-height:0;display:flex;cursor:pointer;background:#000;position:relative}
.rej{position:absolute;top:10px;right:10px;background:rgba(20,20,20,.75);opacity:.55}
.rej:hover,.rej:focus-visible{opacity:1;background:#622;border-color:#a55;color:#fff}
.pane img{width:100%;height:100%;object-fit:contain}
.pane:hover{outline:2px solid #345}
@media (max-width:700px),(max-aspect-ratio:1/1){#stage{flex-direction:column}}
#bar{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;justify-content:center;padding:8px}
button{background:#222;color:#ccc;border:1px solid #444;border-radius:4px;padding:6px 12px;font:inherit;cursor:pointer}
#msg{margin:auto;font-size:20px;text-align:center}a{color:#8ab}
</style>
<div id=stage><div class=pane id=L><img id=li alt=""><button class=rej id=xl title="Not a photo to judge: leave it out (Shift+Left)">&times; Reject</button></div><div class=pane id=R><img id=ri alt=""><button class=rej id=xr title="Not a photo to judge: leave it out (Shift+Right)">&times; Reject</button></div></div>
<div id=bar><b id=count></b>
<span>&larr; left better &nbsp; &rarr; right better &nbsp; &darr; about the same &nbsp; &uarr; can't tell / skip &nbsp; shift+&larr; / shift+&rarr; reject that photo &nbsp; u undo</span>
<button id=bs>About the same</button><button id=bk>Can't tell</button><button id=bu>Undo</button><span id=rej></span></div>
<script>
let S=null,shownAt=0,busy=false;
const $=id=>document.getElementById(id);
function pre(p){if(p){const a=new Image();a.src=p.left;const b=new Image();b.src=p.right;}}
function wait(img){return img.decode?img.decode().catch(()=>{}):Promise.resolve()}
async function show(s){
  S=s;
  $('rej').textContent=s.rejected?s.rejected+' rejected':'';
  if(s.done){$('stage').innerHTML='<div id=msg>Done. '+s.total+' pairs judged. <a href=/report>See the report</a></div>';$('count').textContent=s.total+' / '+s.total;return}
  if(!$('li')){location.reload();return}
  $('count').textContent=(s.answered+1)+' / '+s.total;
  $('li').src=s.pair.left;$('ri').src=s.pair.right;
  await Promise.all([wait($('li')),wait($('ri'))]);
  shownAt=performance.now();pre(s.upcoming);
}
async function post(url,body){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});return r.json()}
async function answer(choice){
  if(busy||!S||S.done)return;busy=true;
  try{show(await post('/api/answer',{index:S.pair.index,choice:choice,ms:Math.round(performance.now()-shownAt)}))}finally{busy=false}
}
async function undo(){if(busy)return;busy=true;try{show(await post('/api/undo'))}finally{busy=false}}
$('L').onclick=()=>answer('left');$('R').onclick=()=>answer('right');
$('xl').onclick=e=>{e.stopPropagation();e.currentTarget.blur();answer('reject_left')};
$('xr').onclick=e=>{e.stopPropagation();e.currentTarget.blur();answer('reject_right')};
$('bs').onclick=()=>answer('same');$('bk').onclick=()=>answer('skip');$('bu').onclick=undo;
addEventListener('keydown',e=>{
  const k=e.key;
  if(e.metaKey||e.ctrlKey||e.altKey)return;
  if(k==='ArrowLeft')answer(e.shiftKey?'reject_left':'left');else if(k==='ArrowRight')answer(e.shiftKey?'reject_right':'right');
  else if(k==='ArrowDown')answer('same');else if(k==='ArrowUp')answer('skip');
  else if(k==='u'||k==='Backspace')undo();else return;
  e.preventDefault();
});
fetch('/api/state').then(r=>r.json()).then(show);
</script>"""


def make_handler(app):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
            if isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store" if ctype.startswith(("text", "application")) else "private, max-age=3600")
            self.end_headers()
            self.wfile.write(body)

        def json(self, obj, code=200):
            self.send(code, json.dumps(obj), "application/json")

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/":
                self.send(200, PAGE)
            elif path == "/api/state":
                self.json(app.state())
            elif path == "/report":
                text = app.report().replace("&", "&amp;").replace("<", "&lt;")
                self.send(200, f"<!doctype html><meta charset=utf-8><body style='background:#111;color:#ccc;padding:16px'><pre>{text}</pre><a href=/ style=color:#8ab>Back</a>")
            elif path.startswith("/img/"):
                name = path[5:]
                sha = name[:-4] if name.endswith(".jpg") else ""
                f = app.dir / "img" / f"{sha}.jpg"
                if SHA_RE.match(sha) and sha in app.shas and f.is_file():
                    self.send(200, f.read_bytes(), "image/jpeg")
                else:
                    self.send(404, "not found", "text/plain")
            else:
                self.send(404, "not found", "text/plain")

        def do_POST(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self.json({"error": "bad json"}, 400)
            if self.path == "/api/answer":
                if not app.answer(body.get("index"), body.get("choice"), body.get("ms")):
                    return self.json({"error": "rejected"}, 409)
            elif self.path == "/api/undo":
                app.undo()
            else:
                return self.json({"error": "not found"}, 404)
            self.json(app.state())

    return H


def serve(d, port):
    app = Judge(d)
    for n in app.notes:
        print("NOTE:", n)
    counts = {s: sum(q["stratum"] == s for q in app.queue) for s in app.strata}
    print(f"{len(app.queue)} pairs queued {counts}; {len(replay(app.log))} already answered")
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    print(f"judging at http://127.0.0.1:{port}/  (report at /report)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def main():
    default = str(Path.home() / "Library/Caches/photo-estate-aesthetics/pilot")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve"); s.add_argument("--dir", default=default); s.add_argument("--port", type=int, default=8770)
    r = sub.add_parser("report"); r.add_argument("--dir", default=default)
    a = ap.parse_args()
    if a.cmd == "serve":
        serve(a.dir, a.port)
    else:
        print(make_report(a.dir), end="")


if __name__ == "__main__":
    main()
