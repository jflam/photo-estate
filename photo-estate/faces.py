#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["onnxruntime>=1.17", "numpy", "pillow>=10.0", "pillow-heif>=0.18"]
# ///
"""
faces.py — Tier 4: find every face, and work out who it is with your help.

  run     detect and embed the faces in every image not scanned yet (resumable)
  stats   how many faces are found, named, and waiting for you
  serve   the naming page on its own (http://127.0.0.1:8764/people); `embed.py serve` also shows
          it at /people once faces have been scanned

  uv run faces.py --db /Volumes/estate/estate.db run --store /Volumes/estate/store
  uv run faces.py --db /Volumes/estate/estate.db serve --store /Volumes/estate/store

How a child is followed from a baby to an adult: a face is never compared with everything known
about a person, only with that person's named faces from about the same time (a mean face per
person per year, and only the years within WINDOW of the photo). A face changes little in two
years, so each year you confirm lets the next year over be recognised, and two siblings who looked
alike at the same age, years apart, are never compared. Where a person has no named faces near a
photo's date (the years before Google's tags start, say), the nearest years they do have are used
and nothing is named without you.

Names come from three places, kept in `faces.source`:
  google  the photo carries a Google people tag and the face matches (or is the only face)
  you     you said so on the naming page; these and `google` are what faces are compared with
  auto    close enough to a person's named faces of that time that it isn't worth asking
Everything you decide is also appended to face-labels.jsonl next to the catalog, keyed by photo
and box, so the work survives a rescan with a better model. Each change is also one entry in the
estate's journal (journal.py: faces.labeled, person.changed), in the same transaction: who did it and
what, in counts and names, beside the per-face lines of face-labels.jsonl rather than instead of them.

Models: InsightFace buffalo_l (SCRFD-10G detector, ArcFace R50 embeddings), run with onnxruntime.
The weights are for non-commercial use only.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from collections import deque
from contextlib import closing
from itertools import islice
from multiprocessing import Pool
from pathlib import Path
from typing import Literal

import journal
import pages   # the pages, with the shared stylesheet put in
from core import config, events, schema
from core.media import FACES_SKIP_EXT as SKIP_EXT   # RAW and GIF (core/media.py says why)
from core.states import ClusterState

MODEL = "buffalo_l"
MODEL_URL = "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip"
MODEL_FILES = {  # name -> sha256
    "det_10g.onnx": "5838f7fe053675b1c7a08b633df49e7af5495cee0493c7dcf6697200b85b5b91",
    "w600k_r50.onnx": "4c06341c33c2ca1f86781dab0e829f88ad5b64be9fba56e56bc9ebdefc619e43",
}
SCHEMA = schema.FACES
FaceOp = Literal["undo", "forget", "name", "later", "not", "rename", "merge", "born"]   # what face-labels.jsonl records
SIDE = 1280        # images are read at this size (longer side): enough for faces a few % of the frame
MIN_PX = 28        # smaller faces than this give embeddings that match anyone
CROP = 160         # the face thumbnails the naming page shows
# Cosine similarity between a face and a person's mean face of about the same time. Measured on a
# sample of 1,700 photos across 2005-2026 with Google's tags as the truth: see `faces.py stats`.
WINDOW = 2         # years either side of the photo whose named faces are compared
SUGGEST = 0.30     # at or above this, the face is put to you as "is this ...?"
AUTO = 0.50        # at or above this, and AUTO_MARGIN clear of the next person, it's named without asking
AUTO_MARGIN = 0.12
ARC_TEMPLATE = [[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366], [41.5493, 92.3655], [70.7299, 92.2041]]


def connect(db: str) -> sqlite3.Connection:
    con = sqlite3.connect(db, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    schema.ensure(con, "faces", "members")   # members: the journal
    return con


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def crop_path(cache: Path, sha: str, idx: int) -> Path:
    return cache / "faces" / sha[:2] / f"{sha}-{idx}.jpg"


def face_crop(im, x: float, y: float, w: float, h: float):
    """The square thumbnail of one face: the box with room around it for hair, hats and chins."""
    from PIL import Image
    W, H = im.size
    side = max(w * W, h * H) * 1.7
    cx, cy = (x + w / 2) * W, (y + h / 2) * H
    box = (round(cx - side / 2), round(cy - side / 2), round(cx + side / 2), round(cy + side / 2))
    return im.crop(box).resize((CROP, CROP), Image.Resampling.LANCZOS)


def _decode(job: tuple[str, str]):
    """Worker: open one image upright at SIDE. Never raises."""
    sha, path = job
    try:
        from PIL import Image, ImageOps
        import pillow_heif
        pillow_heif.register_heif_opener()
        Image.MAX_IMAGE_PIXELS = None
        with Image.open(path) as im:
            im.draft("RGB", (SIDE, SIDE))
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((SIDE, SIDE))
            return sha, im.size, im.tobytes(), None
    except Exception as e:
        return sha, None, None, f"{type(e).__name__}: {e}"[:300]


# ── the models ──────────────────────────────────────────────────────────────────────────────────

def model_dir() -> Path:
    d = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "photo-estate" / MODEL
    if all((d / f).exists() for f in MODEL_FILES):
        return d
    import hashlib
    import io
    import urllib.request
    import zipfile
    d.mkdir(parents=True, exist_ok=True)
    print(f"downloading {MODEL} (280 MB, once) from {MODEL_URL}", flush=True)
    with urllib.request.urlopen(MODEL_URL) as r:
        z = zipfile.ZipFile(io.BytesIO(r.read()))
    for f, want in MODEL_FILES.items():
        data = z.read(f)
        if hashlib.sha256(data).hexdigest() != want:
            sys.exit(f"{f}: unexpected contents; not using it")
        (d / f).write_bytes(data)
    return d


class Models:
    def __init__(self, threads: int, det_size: int = 640):
        import numpy as np
        import onnxruntime as ort   # pyright: ignore[reportMissingImports]  (the faces group: not in the dev environment)
        ort.set_default_logger_severity(3)   # w600k_r50 declares batch 1 but takes any; it warns on every call
        self.np, d = np, model_dir()
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads
        self.det = ort.InferenceSession(str(d / "det_10g.onnx"), so, providers=["CPUExecutionProvider"])
        self.rec = ort.InferenceSession(str(d / "w600k_r50.onnx"), so, providers=["CPUExecutionProvider"])
        self.det_in, self.rec_in = self.det.get_inputs()[0].name, self.rec.get_inputs()[0].name
        self.rec_batch = not isinstance(self.rec.get_inputs()[0].shape[0], int)
        self.size = det_size
        self.anchors = {}
        for stride in (8, 16, 32):  # two anchors at every cell of each feature map
            n = det_size // stride
            c = np.stack(list(np.mgrid[:n, :n][::-1]), -1).reshape(-1, 2).astype(np.float32) * stride
            self.anchors[stride] = np.repeat(c, 2, axis=0)

    def detect(self, im, thresh: float = 0.5):
        """PIL image -> (boxes [n,4] x1 y1 x2 y2, scores [n], five landmarks [n,5,2]), in image pixels."""
        np = self.np
        from PIL import Image
        scale = self.size / max(im.size)
        small = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.Resampling.BILINEAR)
        canvas = np.zeros((self.size, self.size, 3), np.float32)
        canvas[:small.height, :small.width] = np.asarray(small)
        blob = ((canvas - 127.5) / 128.0).transpose(2, 0, 1)[None]
        outs = self.det.run(None, {self.det_in: blob})
        boxes, scores, kps = [], [], []
        for i, stride in enumerate((8, 16, 32)):
            s = outs[i].reshape(-1)
            keep = s >= thresh
            if not keep.any():
                continue
            c = self.anchors[stride][keep]
            d = outs[i + 3].reshape(-1, 4)[keep] * stride
            k = outs[i + 6].reshape(-1, 10)[keep] * stride
            boxes.append(np.stack([c[:, 0] - d[:, 0], c[:, 1] - d[:, 1], c[:, 0] + d[:, 2], c[:, 1] + d[:, 3]], 1))
            kps.append(k.reshape(-1, 5, 2) + c[:, None, :])
            scores.append(s[keep])
        if not boxes:
            return np.zeros((0, 4)), np.zeros(0), np.zeros((0, 5, 2))
        boxes, scores, kps = np.concatenate(boxes) / scale, np.concatenate(scores), np.concatenate(kps) / scale
        order, keep = scores.argsort()[::-1], []
        area = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
        while len(order):  # non-maximum suppression
            i, rest = order[0], order[1:]
            keep.append(i)
            xx1, yy1 = np.maximum(boxes[i, 0], boxes[rest, 0]), np.maximum(boxes[i, 1], boxes[rest, 1])
            xx2, yy2 = np.minimum(boxes[i, 2], boxes[rest, 2]), np.minimum(boxes[i, 3], boxes[rest, 3])
            inter = np.clip(xx2 - xx1, 0, None) * np.clip(yy2 - yy1, 0, None)
            order = rest[inter / (area[i] + area[rest] - inter) <= 0.4]
        return boxes[keep], scores[keep], kps[keep]

    def aligned(self, im, kps):
        """The 112x112 crop ArcFace expects: eyes, nose and mouth corners moved onto fixed positions."""
        np = self.np
        from PIL import Image
        src, dst = np.asarray(kps, np.float64), np.asarray(ARC_TEMPLATE)
        ms, md = src.mean(0), dst.mean(0)
        s, d = src - ms, dst - md
        U, S, Vt = np.linalg.svd(d.T @ s / len(src))
        D = np.diag([1.0, -1.0 if np.linalg.det(U) * np.linalg.det(Vt) < 0 else 1.0])
        A = (S * D.diagonal()).sum() / s.var(0).sum() * (U @ D @ Vt)   # dst = A @ src + t
        t = md - A @ ms
        inv = np.linalg.inv(A)
        off = -inv @ t
        return im.transform((112, 112), Image.Transform.AFFINE,
                            (inv[0, 0], inv[0, 1], off[0], inv[1, 0], inv[1, 1], off[1]), Image.Resampling.BILINEAR)

    def embed(self, crops):
        np = self.np
        blob = ((np.stack([np.asarray(c, np.float32) for c in crops]) - 127.5) / 127.5).transpose(0, 3, 1, 2)
        if self.rec_batch:
            v = self.rec.run(None, {self.rec_in: blob})[0]
        else:
            v = np.concatenate([self.rec.run(None, {self.rec_in: blob[i:i + 1]})[0] for i in range(len(blob))])
        return v / np.linalg.norm(v, axis=1, keepdims=True)


def _bounded(pool, jobs, ahead: int):
    """Decoded images, in order, with at most `ahead` decoded or decoding at once. Pool.imap queues
    every job up front and the decoders outrun the models, so its finished images pile up in memory
    until macOS kills the process."""
    it, pending = iter(jobs), deque()
    for job in islice(it, ahead):
        pending.append(pool.apply_async(_decode, (job,)))
    while pending:
        result = pending.popleft().get()
        for job in islice(it, 1):
            pending.append(pool.apply_async(_decode, (job,)))
        yield result


def cmd_run(a) -> None:
    import numpy as np
    from PIL import Image
    con = connect(a.db)
    store = Path(a.store)
    cache = Path(a.cache or Path(a.db).resolve().parent / "review")
    todo = con.execute(   # --after-rowid: only new assets, a few index lookups instead of a scan (see embed.py run)
        "SELECT a.sha256, a.ext FROM assets a WHERE a.kind='image' AND a.rowid > ? AND NOT EXISTS"
        " (SELECT 1 FROM face_scans s WHERE s.sha256=a.sha256 AND s.model=?) ORDER BY a.sha256",
        (a.after_rowid or 0, MODEL)).fetchall()
    todo = [(s, e) for s, e in todo if e not in SKIP_EXT][: a.limit or None]
    if a.after_rowid is None:
        done, found = con.execute("SELECT count(*), coalesce(sum(n), 0) FROM face_scans WHERE model=?", (MODEL,)).fetchone()
        print(f"{done} images already scanned ({found} faces), {len(todo)} to do", flush=True)
    else:
        print(f"{len(todo)} new images to scan", flush=True)
    events.emit_progress("faces", 0, len(todo))
    if not todo:
        return
    models = Models(a.threads)
    jobs = [(s, str(store / s[:2] / s[2:4] / f"{s}{e}")) for s, e in todo]
    t0 = last = time.time()
    n = nfaces = errors = 0
    scans, rows = [], []

    def flush():
        nonlocal scans, rows
        con.executemany("INSERT INTO faces(sha256, idx, model, x, y, w, h, score, px, vec) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        con.executemany("INSERT OR REPLACE INTO face_scans VALUES (?,?,?,?,?)", scans)
        con.commit()
        scans, rows = [], []

    with Pool(a.workers) as pool:
        for sha, size, raw, err in _bounded(pool, jobs, ahead=4 * a.workers):
            n += 1
            if err:
                errors += 1
                scans.append((sha, MODEL, 0, err, now()))
                continue
            im = Image.frombytes("RGB", size, raw)
            boxes, scores, kps = models.detect(im)
            big = [i for i in range(len(boxes)) if max(boxes[i, 2] - boxes[i, 0], boxes[i, 3] - boxes[i, 1]) >= MIN_PX]
            if big:
                vecs = models.embed([models.aligned(im, kps[i]) for i in big])
                for idx, (i, v) in enumerate(zip(big, vecs)):
                    x1, y1, x2, y2 = (float(c) for c in boxes[i])
                    x, y, w, h = x1 / size[0], y1 / size[1], (x2 - x1) / size[0], (y2 - y1) / size[1]
                    rows.append((sha, idx, MODEL, round(x, 5), round(y, 5), round(w, 5), round(h, 5),
                                 round(float(scores[i]), 4), int(max(x2 - x1, y2 - y1)), v.astype(np.float16).tobytes()))
                    dest = crop_path(cache, sha, idx)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    face_crop(im, x, y, w, h).save(dest, "JPEG", quality=82)
            nfaces += len(big)
            scans.append((sha, MODEL, len(big), None, now()))
            if len(scans) >= 64:
                flush()
            if time.time() - last >= 60:
                rate = n / (time.time() - t0)
                print(f"{datetime.now():%H:%M:%S} {n}/{len(jobs)} {rate:.1f} img/s, {nfaces} faces,"
                      f" {errors} undecodable, ~{(len(jobs) - n) / rate / 3600:.1f} h left", flush=True)
                events.emit_progress("faces", n, len(jobs))
                last = time.time()
        flush()
    dt = time.time() - t0
    print(f"done: {n} images in {dt / 3600:.2f} h ({n / dt:.1f} img/s), {nfaces} faces, {errors} undecodable", flush=True)


# ── who is who ──────────────────────────────────────────────────────────────────────────────────

NONE, AUTO_SRC, GOOGLE, YOU = 0, 1, 2, 3
SRC = {None: NONE, "auto": AUTO_SRC, "google": GOOGLE, "you": YOU}
SRC_NAME = {v: k for k, v in SRC.items()}


def default_hidden(con) -> set[str]:
    """The photos no face of is on People: what the estate defaults hide (lenses.py), and the hiding sets from
    before lenses, which hide for everyone. People is built once, for every member, so a member's own filters
    are not here: they apply as a person's faces are listed (People.handle's `keep`)."""
    for q in ("SELECT DISTINCT h.sha256 FROM hidden h JOIN change_sets s ON s.id = h.set_id"
              " LEFT JOIN lenses l ON l.id = s.lens_id WHERE s.lens_id IS NULL OR l.estate_default = 1",
              "SELECT sha256 FROM hidden"):         # (a catalog from before lenses: every hiding set is for everyone)
        try:
            return {s for s, in con.execute(q)}
        except sqlite3.OperationalError:
            continue
    return set()


def face_rows(con) -> list[tuple]:
    """Every face of MODEL with its photo's date, people tags and extension, in id order:
    (id, sha256, idx, x, y, w, h, score, px, vec, person_id, source, taken_at, people, ext).
    Faces in photos the estate defaults hide (default_hidden) are left out: nobody is asked who they are.

    Each table is read once, front to back, and they are put together here. Asked of SQLite as one join,
    every face is a lookup of its photo by sha256, somewhere else in `assets` each time: 120,000 of them
    took 38 s of a 41 s start on a catalog on a USB disk, where the three passes take under 4 s."""
    hidden = default_hidden(con)
    photo = {s: (t, p, e) for s, t, p, e in con.execute("SELECT sha256, taken_at, people, ext FROM assets")}
    return [r + photo[r[1]] for r in con.execute(
        "SELECT id, sha256, idx, x, y, w, h, score, px, vec, person_id, source FROM faces WHERE model=? ORDER BY id", (MODEL,))
        if r[1] in photo and r[1] not in hidden]


class Draft:
    """A copy of what a change touches (who each face is, the not/later sets, the people) and the journal
    records it will write: changed while the change's SQLite transaction runs, and swapped into People
    (People.commit) only once that commits."""

    def __init__(self, pe: People):
        self.pid, self.src = pe.pid.copy(), pe.src.copy()
        self.nots = {q: set(s) for q, s in pe.nots.items()}
        self.later = {q: set(s) for q, s in pe.later.items()}
        self.persons = {q: dict(i) for q, i in pe.persons.items()}
        self.logs: list[tuple[FaceOp, list, dict]] = []


class People:
    """Every face in memory, who each one is, and the questions worth asking. Used by `faces.py serve`
    and mounted at /people by `embed.py serve`."""

    def __init__(self, db: str, store: str, caches: list, log=print):
        import numpy as np
        self.np, self.db, self.store, self.caches = np, db, Path(store), [Path(c) for c in caches]
        self.lock = threading.RLock()
        self.journal = Path(db).resolve().parent / "face-labels.jsonl"
        t0 = time.time()
        with closing(connect(db)) as con:
            self.load(con)
        self.rescore()
        log(f"people: {self.n} faces, {len(self.persons)} people, {int((self.pid > 0).sum())} named,"
            f" {int(self.pending().sum())} to review, in {time.time() - t0:.1f}s")
        threading.Thread(target=self.cluster, daemon=True).start()

    def load(self, con):
        """Read every face, person and not/later decision, and name what Google's tags say. Only __init__
        calls it: a People that fails here is never used, and its connection is closed (closing())."""
        import numpy as np
        rows = face_rows(con)
        self.n = n = len(rows)
        self.ids = np.array([r[0] for r in rows], np.int64)
        self.row = {int(i): k for k, i in enumerate(self.ids)}
        self.sha = [r[1] for r in rows]
        self.idx = [r[2] for r in rows]
        self.box = np.array([r[3:7] for r in rows], np.float32).reshape(n, 4)
        self.score = np.array([r[7] for r in rows], np.float32)
        self.px = np.array([r[8] for r in rows], np.int32)
        self.V = np.frombuffer(b"".join(r[9] for r in rows), np.float16).reshape(n, -1).astype(np.float32)
        self.pid = np.array([-1 if r[10] is None else r[10] for r in rows], np.int32)
        self.src = np.array([SRC.get(r[11], NONE) for r in rows], np.int8)
        self.date = [(r[12] or "")[:10] for r in rows]
        self.year = np.array([int(d[:4]) if d[:4].isdigit() and 1900 < int(d[:4]) < 2100 else 0 for d in self.date], np.int32)
        self.tags = [[p.strip() for p in (r[13] or "").split(",") if p.strip()] for r in rows]
        self.ext = [r[14] for r in rows]
        self.by_sha = {}
        for k, s in enumerate(self.sha):
            self.by_sha.setdefault(s, []).append(k)
        self.persons = {i: {"name": nm, "born": b} for i, nm, b in con.execute("SELECT id, name, born FROM persons")}
        self.nots = {}
        for f, p in con.execute("SELECT face_id, person_id FROM face_not"):
            if f in self.row:
                self.nots.setdefault(p, set()).add(self.row[f])
        self.later = {}
        for f, p in con.execute("SELECT face_id, person_id FROM face_later"):
            if f in self.row:
                self.later.setdefault(p, set()).add(self.row[f])
        self.sug = np.full(n, -1, np.int32)        # the person a face most resembles, of those not ruled out
        self.sug_score = np.zeros(n, np.float32)
        self.sug_far = np.zeros(n, bool)           # ...judged from named faces more than WINDOW years away
        self.clusters, self.clusters_state, self.clusters_error = [], ClusterState.WAITING, None   # under self.lock
        self.seed(con)

    # -- scoring -------------------------------------------------------------------------------

    def centroids(self):
        """A mean face per person per year, from the faces you or Google named."""
        np = self.np
        trusted = (self.pid > 0) & (self.src >= GOOGLE)
        cp, cy, C = [], [], []
        for p in self.persons:
            mine = trusted & (self.pid == p)
            for y in np.unique(self.year[mine]):
                v = self.V[mine & (self.year == y)].sum(0)
                cp.append(p)
                cy.append(int(y))
                C.append(v / np.linalg.norm(v))
        return np.array(cp, np.int32), np.array(cy, np.int32), np.array(C, np.float32).reshape(len(C), self.V.shape[1])

    def scores(self, rows, cents=None):
        """rows -> (person ids [P], similarity [n,P], far [n,P]): each face against each person's mean
        faces from within WINDOW years of it, or from the nearest years that person has when none are."""
        np = self.np
        cp, cy, C = cents or self.centroids()
        pids = sorted(set(cp.tolist()))
        S = np.full((len(rows), len(pids)), -1.0, np.float32)
        far = np.zeros((len(rows), len(pids)), bool)
        if not len(rows) or not pids:
            return pids, S, far
        t = self.year[rows]
        for lo in range(0, len(rows), 20000):
            sl = slice(lo, lo + 20000)
            sims, ty = self.V[rows[sl]] @ C.T, t[sl]
            for j, p in enumerate(pids):
                cols = np.flatnonzero(cp == p)
                gap = np.abs(ty[:, None] - cy[cols][None, :])
                gap[ty == 0] = 0                                       # undated: compare with every year
                near = gap <= WINDOW
                none = ~near.any(1)
                near[none] = gap[none] <= gap[none].min(1, keepdims=True) + 1
                S[sl, j] = np.where(near, sims[:, cols], -1).max(1)
                far[sl, j] = none
                born = (self.persons[p].get("born") or "")[:4]
                if born.isdigit():
                    S[sl, j][(ty > 0) & (ty < int(born))] = -1          # not before they were born
        return pids, S, far

    def rescore(self):
        """Recompute every suggestion and every automatic name from the faces named so far. Worked out on
        new arrays, the changed names written in one transaction, and the arrays swapped in after it
        commits, all under self.lock: if SQLite raises, the old suggestions and names stand."""
        np = self.np
        with self.lock:
            rows = np.flatnonzero(self.src < GOOGLE)   # everything you or Google haven't settled
            pid, src = self.pid.copy(), self.src.copy()
            sug, sug_score, sug_far = np.full(self.n, -1, np.int32), np.zeros(self.n, np.float32), np.zeros(self.n, bool)
            pid[rows], src[rows] = -1, NONE
            pids, S, far = self.scores(rows)           # reads only the trusted names, which this leaves alone
            if pids:
                at = {int(r): k for k, r in enumerate(rows)}
                for j, p in enumerate(pids):
                    ks = [at[r] for r in self.nots.get(p, ()) if r in at]
                    S[ks, j] = -1
                col = {p: j for j, p in enumerate(pids)}
                for k in np.flatnonzero((src >= GOOGLE) & (pid > 0)):   # nobody is in a photo twice
                    j = col.get(int(pid[k]))
                    if j is not None:
                        for o in self.by_sha[self.sha[k]]:
                            if o != k and o in at:
                                S[at[o], j] = -1
                best = S.argmax(1)
                top = S[np.arange(len(rows)), best]
                S[np.arange(len(rows)), best] = -1
                second = S.max(1)
                sug[rows] = np.array(pids, np.int32)[best]
                sug_score[rows] = top
                sug_far[rows] = far[np.arange(len(rows)), best]
                sure = (top >= AUTO) & (top - second >= AUTO_MARGIN) & ~sug_far[rows] & (self.px[rows] >= 40)
                keep = {}                                   # two sure faces of one person in a photo: only the closer
                for i in np.flatnonzero(sure):
                    key = (self.sha[rows[i]], int(sug[rows[i]]))
                    if key not in keep or top[i] > top[keep[key]]:
                        keep[key] = i
                sure[:] = False
                sure[list(keep.values())] = True
                pid[rows[sure]], src[rows[sure]] = sug[rows[sure]], AUTO_SRC
            changed = rows[pid[rows] != self.pid[rows]]
            if len(changed):
                with closing(connect(self.db)) as con, con:
                    con.executemany("UPDATE faces SET person_id=?, source=?, labeled_at=? WHERE id=?",
                                    [(int(pid[r]) if pid[r] > 0 else None, "auto" if pid[r] > 0 else None,
                                      now(), int(self.ids[r])) for r in changed])
            self.pid, self.src, self.sug, self.sug_score, self.sug_far = pid, src, sug, sug_score, sug_far

    def skipped(self, p):
        """Faces you put off deciding about for this person."""
        m = self.np.zeros(self.n, bool)
        m[list(self.later.get(p, ()))] = True
        return m

    def pending(self, p=None):
        """Faces worth asking you about: they resemble someone, and nobody has said who they are."""
        m = (self.pid == -1) & (self.sug_score >= SUGGEST)
        return m & (self.sug == p) if p is not None else m

    # -- Google's tags -------------------------------------------------------------------------

    def seed(self, con):
        """Name faces from Google's people tags. A tag is on the photo, not on a face, so: a photo with
        one tag and one face names that face; after that, each tagged name goes to the face in its
        photo that best matches what is already known of that person."""
        np = self.np
        names = sorted({t for ts in self.tags for t in ts})
        if not names:
            return
        have = {p["name"]: i for i, p in self.persons.items()}
        for nm in names:
            if nm not in have:
                have[nm] = con.execute("INSERT INTO persons(name, created_at) VALUES (?,?)", (nm, now())).lastrowid
                self.persons[have[nm]] = {"name": nm, "born": None}
        good = (self.score >= 0.6) & (self.px >= 40)
        new = []
        was = {}                                # what a face named here was before, to put back if the tag is not of it
        free = lambda k, p: self.src[k] < GOOGLE and k not in self.nots.get(p, ())
        for s, ks in self.by_sha.items():       # one tag, one face
            tags = self.tags[ks[0]]
            if len(tags) == 1 and len(ks) == 1 and good[ks[0]] and free(ks[0], have[tags[0]]):
                was[ks[0]] = (self.pid[ks[0]], self.src[ks[0]])
                self.pid[ks[0]], self.src[ks[0]] = have[tags[0]], GOOGLE
                new.append(ks[0])
        for rnd in range(3):
            cents = self.centroids()
            if not len(cents[0]):
                break
            if rnd == 0 and new:               # a lone face that isn't the person tagged (they had their back turned)
                rows = np.array(new)
                pids, S, _ = self.scores(rows, cents)
                own = S[np.arange(len(rows)), [pids.index(int(p)) for p in self.pid[rows]]]
                # Put back as it was read, not as unknown: rescore() names it again either way, and writes
                # only what differs from here. Left unknown, a face the catalog already names (auto) was
                # written again, with a new labeled_at, at every start.
                for k in rows[own < 0.2]:
                    self.pid[k], self.src[k] = was[int(k)]
                new = [k for k in new if self.src[k] == GOOGLE]
                cents = self.centroids()
            open_ = np.flatnonzero((self.src < GOOGLE) & good & np.array([bool(t) for t in self.tags]))
            pids, S, _ = self.scores(open_, cents)       # every unnamed face in a tagged photo, in one go
            col, at = {p: j for j, p in enumerate(pids)}, {int(k): i for i, k in enumerate(open_)}
            for s, ks in self.by_sha.items():
                tags = self.tags[ks[0]]
                if not tags:
                    continue
                taken = {int(self.pid[k]) for k in ks if self.src[k] >= GOOGLE}
                want = [have[t] for t in tags if have[t] not in taken and have[t] in col]
                cand = [k for k in ks if k in at and self.src[k] < GOOGLE]
                if not want or not cand:
                    continue
                used = set()
                for sc, k, p in sorted(((float(S[at[k], col[p]]), k, p) for k in cand for p in want), reverse=True):
                    if sc < 0.4:
                        break
                    if k in used or p in taken or not free(k, p):
                        continue
                    used.add(k)
                    taken.add(p)
                    self.pid[k], self.src[k] = p, GOOGLE
                    new.append(k)
        with con:   # the new people and Google's names together, or neither (and then no People at all)
            con.executemany("UPDATE faces SET person_id=?, source='google', labeled_at=? WHERE id=?",
                            [(int(self.pid[k]), now(), int(self.ids[k])) for k in new if self.src[k] == GOOGLE])

    # -- faces nobody has named ----------------------------------------------------------------

    def cluster(self):
        """Group the faces that resemble nobody known into people who appear often. Each group is one
        face and everything close to it, so a group never chains from one person into another. Runs in its
        own thread; the state (working, then ready or failed with clusters_error) changes under self.lock,
        and the groups are swapped in with it, so a reader sees the old groups or the new, never half."""
        with self.lock:
            self.clusters_state, self.clusters_error = ClusterState.WORKING, None
        try:
            out = self.find_clusters()
        except Exception as e:   # the page stops asking and says so, instead of "Grouping..." forever
            with self.lock:
                self.clusters_state, self.clusters_error = ClusterState.FAILED, f"{type(e).__name__}: {e}"
            print(f"people: grouping the unnamed faces failed: {self.clusters_error}", flush=True)
            return
        with self.lock:
            self.clusters, self.clusters_state = out, ClusterState.READY

    def find_clusters(self) -> list:
        np = self.np
        with self.lock:
            cand = np.flatnonzero((self.pid == -1) & (self.sug_score < SUGGEST) & (self.score >= 0.65) & (self.px >= 56))
            cand = cand[np.argsort(-(self.px[cand] * self.score[cand]), kind="stable")[:60000]]   # the clearest; keeps this under a minute
        X = self.V[cand]
        dense = np.zeros(len(cand), np.int32)
        for lo in range(0, len(cand), 4096):
            dense[lo:lo + 4096] = (X[lo:lo + 4096] @ X.T >= 0.5).sum(1)
        left, out = np.ones(len(cand), bool), []
        for i in np.argsort(-dense):
            if dense[i] < 4 or len(out) >= 400:
                break
            if not left[i]:
                continue
            members = np.flatnonzero(left & (X @ X[i] >= 0.5))
            left[i] = False
            if len(members) < 4:
                continue
            left[members] = False
            out.append(cand[members[np.argsort(-(X[members] @ X[i]))]])
        return out

    # -- the page's API ------------------------------------------------------------------------

    def face(self, k: int) -> dict:
        k = int(k)
        return {"id": int(self.ids[k]), "sha": self.sha[k], "date": self.date[k], "year": int(self.year[k]),
                "score": round(float(self.sug_score[k]), 3), "src": SRC_NAME[int(self.src[k])],
                "person": int(self.pid[k]), "sug": int(self.sug[k]), "far": bool(self.sug_far[k]),
                "box": [round(float(v), 4) for v in self.box[k]]}

    def overview(self, keep=None) -> dict:
        """Everyone on the page, with their counts (over every face People holds) and a cover: with `keep`
        (sha -> bool), a face in a photo it keeps, when the person has one."""
        np = self.np
        with self.lock:
            people = []
            for p, info in self.persons.items():
                later = self.pending(p) & self.skipped(p)
                mine, pend = self.pid == p, self.pending(p) & ~later
                if not mine.any() and not pend.any() and not later.any():
                    continue
                years = {}
                for y, c in zip(*np.unique(self.year[mine], return_counts=True)):
                    years.setdefault(int(y), [0, 0])[0] = int(c)
                for y, c in zip(*np.unique(self.year[pend], return_counts=True)):
                    years.setdefault(int(y), [0, 0])[1] = int(c)
                best = np.flatnonzero(mine & (self.src >= GOOGLE))
                if not len(best):
                    best = np.flatnonzero(mine | pend | later)
                if keep is not None:
                    best = np.array([k for k in best if keep(self.sha[k])], np.int64) if len(best) else best
                    if not len(best):
                        best = np.array([k for k in np.flatnonzero(mine | pend | later) if keep(self.sha[k])], np.int64)
                    if not len(best):
                        continue            # every face of them is in a photo this member does not see
                recent = best[self.year[best] >= self.year[best].max() - 1]
                cover = recent[np.argmax(self.px[recent] * self.score[recent])]
                people.append({"id": p, "name": info["name"], "born": info.get("born"),
                               "named": int(mine.sum()), "you": int((mine & (self.src == YOU)).sum()),
                               "google": int((mine & (self.src == GOOGLE)).sum()),
                               "auto": int((mine & (self.src == AUTO_SRC)).sum()),
                               "pending": int(pend.sum()), "skipped": int(later.sum()),
                               "years": years, "cover": int(self.ids[cover])})
            people.sort(key=lambda x: -x["named"] - x["pending"])
            return {"faces": self.n, "photos": len(self.by_sha), "named": int((self.pid > 0).sum()),
                    "ignored": int((self.pid == 0).sum()), "pending": int(self.pending().sum()),
                    "people": people, "unknown": self.clusters_state}

    # review, person_faces, odd and photo look the person or face up under self.lock and return None (a 404)
    # when it isn't there: a merge can delete a person between the request arriving and the lock.

    def review(self, p: int, year, limit: int, skipped: bool = False) -> dict | None:
        """Faces waiting for p, most likely first; the ones you skipped are kept apart (skipped=True)."""
        np = self.np
        with self.lock:
            if p not in self.persons:
                return None
            pend, later = self.pending(p), self.skipped(p)
            m = pend & later if skipped else pend & ~later
            total = int(m.sum())
            if year is not None:
                m = m & (self.year == year)
            rows = np.flatnonzero(m)
            rows = rows[np.argsort(-self.sug_score[rows])][:limit]
            return {"person": {"id": p, **self.persons[p]}, "total": total, "matching": int(m.sum()),
                    "skipped": int((pend & later).sum()), "faces": [self.face(k) for k in rows]}

    def person_faces(self, p: int, year, limit: int) -> dict | None:
        """A person's named faces: a few per year for the timeline, or all of one year."""
        np = self.np
        with self.lock:
            if p not in self.persons:
                return None
            mine = self.pid == p
            if year is not None:
                rows = np.flatnonzero(mine & (self.year == year))
                rows = rows[np.argsort([self.date[k] for k in rows], kind="stable")]
                return {"person": {"id": p, **self.persons[p]}, "faces": [self.face(k) for k in rows[:limit]],
                        "matching": int(len(rows))}
            out = {}
            for y in np.unique(self.year[mine]):
                rows = np.flatnonzero(mine & (self.year == y))
                rows = rows[np.argsort(-(self.px[rows] * self.score[rows] * (1 + (self.src[rows] >= GOOGLE))))][:limit]
                out[int(y)] = [self.face(k) for k in sorted(rows, key=lambda k: self.date[k])]
            return {"person": {"id": p, **self.persons[p]}, "years": out}

    def odd(self, p: int, year, offset: int, limit: int) -> dict | None:
        """A person's named faces, least like the rest of them first: each face against the mean of their
        other named faces from within WINDOW years (every year, if the photo is undated). A face with no
        others near it in time comes first too. This is where the mistakes are."""
        np = self.np
        with self.lock:
            if p not in self.persons:
                return None
            mine = np.flatnonzero(self.pid == p)
            ys = self.year[mine]
            sums = {int(y): self.V[mine[ys == y]].sum(0) for y in np.unique(ys)}
            total = sum(sums.values(), np.zeros(self.V.shape[1], np.float32))
            fit = np.empty(len(mine), np.float32)
            for y in sums:
                at = np.flatnonzero(ys == y)
                W = total if y == 0 else sum((v for yy, v in sums.items() if yy and abs(yy - y) <= WINDOW),
                                             np.zeros(self.V.shape[1], np.float32))   # y's own is always in it
                v = self.V[mine[at]]
                dot, vv = v @ W, (v * v).sum(1)
                rest = np.sqrt(np.maximum(float(W @ W) - 2 * dot + vv, 0))     # |W - v|: the others, without this face
                fit[at] = np.where(rest > 1e-3, (dot - vv) / (np.sqrt(vv) * np.maximum(rest, 1e-3)), -1)
            for i, k in enumerate(mine):                 # named twice in one photo: one of them is wrong
                if any(o != k and self.pid[o] == p for o in self.by_sha[self.sha[k]]):
                    fit[i] = -2
            sel = np.ones(len(mine), bool) if year is None else ys == year
            order = np.flatnonzero(sel)[np.argsort(fit[sel], kind="stable")]
            page = order[offset:offset + limit]
            return {"person": {"id": p, **self.persons[p]}, "matching": int(sel.sum()),
                    "faces": [dict(self.face(mine[i]), fit=round(float(fit[i]), 3)) for i in page]}

    def unknown(self, limit: int) -> dict:
        with self.lock:
            out = []
            cents = self.centroids()
            for ci, rows in enumerate(self.clusters):
                rows = rows[(self.pid[rows] == -1) & (self.sug_score[rows] < SUGGEST)]
                if len(rows) < 3:
                    continue
                ys = self.year[rows][self.year[rows] > 0]
                guess = None
                if len(cents[0]):
                    pids, S, _ = self.scores(rows[:12], cents)
                    mean = S.mean(0)
                    j = int(mean.argmax())
                    if mean[j] >= 0.25:
                        guess = {"id": pids[j], "name": self.persons[pids[j]]["name"], "score": round(float(mean[j]), 2)}
                out.append({"cluster": ci, "n": int(len(rows)), "faces": [self.face(k) for k in rows[:14]],
                            "from": int(ys.min()) if len(ys) else 0, "to": int(ys.max()) if len(ys) else 0, "guess": guess})
                if len(out) >= limit:
                    break
            return {"state": self.clusters_state, "groups": out,
                    **({"error": self.clusters_error} if self.clusters_state == ClusterState.FAILED else {})}

    def photo(self, face_id: int) -> dict | None:
        with self.lock:
            if (k := self.row.get(face_id)) is None:
                return None
            others = [dict(self.face(j), name=self.persons.get(int(self.pid[j]), {}).get("name"))
                      for j in self.by_sha[self.sha[k]]]
            return {"face": self.face(k), "faces": others}

    def log(self, op: FaceOp, rows, **extra):
        try:
            with self.journal.open("a") as f:
                f.write(json.dumps({"ts": now(), "op": op, **extra,
                                    "faces": [[self.sha[k], *[round(float(v), 4) for v in self.box[k]]] for k in rows]},
                                   ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError as e:
            print(f"face-labels.jsonl: {e}", flush=True)

    def person_id(self, con, who, d: Draft) -> int:
        """An id, or a name: an existing person's, or a new person's (in the draft, until it commits)."""
        if isinstance(who, int):
            return who
        name = " ".join(str(who).split())
        for p, info in d.persons.items():
            if info["name"].lower() == name.lower():
                return p
        p = con.execute("INSERT INTO persons(name, created_at) VALUES (?,?)", (name, now())).lastrowid
        d.persons[p] = {"name": name, "born": None}
        return p

    # Every change is worked out on a Draft (a copy of what it touches) while its SQL runs in one
    # transaction; the draft is swapped in, and the journal written, only once that commits. All of it
    # happens under self.lock, so changes reach SQLite in the order they reach memory and the two never
    # disagree; if SQLite raises (locked, disk full), memory is as it was and the connection is closed.

    @staticmethod
    def journaled(con, d: Draft, actor: int | None, kind: str, subject: str) -> None:
        """The one journal entry for a change (journal.py), in its transaction: what face-labels.jsonl
        gets, in counts and names. Nothing when it changed nothing."""
        ops = [{"op": op, **({"faces": len(rows)} if rows else {}), **extra} for op, rows, extra in d.logs]
        if ops:
            journal.append(con, actor, kind, subject, {"ops": ops})

    def commit(self, d: Draft) -> None:
        """Swap in a draft whose transaction committed, and journal what it did. Under self.lock."""
        self.pid, self.src, self.nots, self.later, self.persons = d.pid, d.src, d.nots, d.later, d.persons
        for op, rows, extra in d.logs:
            self.log(op, rows, **extra)

    def label_many(self, body: dict, actor: int | None = None) -> dict:
        """{items: [label bodies]}: a page of decisions, applied in order in one transaction, then one rescore.
        `actor` (members.id) is who the journal names."""
        with self.lock:
            d = Draft(self)
            with closing(connect(self.db)) as con, con:
                for item in body.get("items") or []:
                    if isinstance(item, dict):
                        self.decide(item, d, con)
                self.journaled(con, d, actor, "faces.labeled", "people")
            self.commit(d)
            self.rescore()
            return {"ok": True, "pending": int(self.pending().sum())}

    def label(self, body: dict, actor: int | None = None) -> dict:
        """{faces: [ids], person: id | "name" | 0 (don't name) | null (forget), cluster: n, not: [ids], not_person: id}.
        {pending: {person: id, year: y | null}} in place of faces: every face waiting for that person (in that
        year), less the ones in `not`, so you can say yes to all of them and find the mistakes afterwards.
        undo: [ids] takes back what was said about those faces first: their name, and "not" or "later" for
        undo_person (for everyone, without it).
        later: [ids], later_person: id puts those faces aside, unnamed, to be asked about again later."""
        with self.lock:
            d = Draft(self)
            with closing(connect(self.db)) as con, con:
                self.decide(body, d, con)
                self.journaled(con, d, actor, "faces.labeled", "people")
            self.commit(d)
            self.rescore()
            return {"ok": True, "pending": int(self.pending().sum())}

    def decide(self, body: dict, d: Draft, con) -> None:
        """One label body (label's docstring) applied to the draft and written to con's open transaction."""
        np = self.np
        rows = [self.row[i] for i in body.get("faces") or [] if i in self.row]
        pend = body.get("pending")
        if isinstance(pend, dict) and pend.get("person") in d.persons:
            pp = pend["person"]           # pending(pp) less skipped(pp), as the draft has them so far
            m = (d.pid == -1) & (self.sug_score >= SUGGEST) & (self.sug == pp)
            m[list(d.later.get(pp, ()))] = False
            year = pend.get("year")       # null: every year; a number (or one as text); anything else: none
            if year is not None:
                ok = isinstance(year, int) or isinstance(year, str) and year.lstrip("-").isdigit()
                m = m & (self.year == int(year)) if ok else m & False
            skip = {self.row[i] for i in body.get("not") or [] if i in self.row}
            rows = [int(k) for k in np.flatnonzero(m) if int(k) not in skip]
        if isinstance(body.get("cluster"), int) and 0 <= body["cluster"] < len(self.clusters):
            c = self.clusters[body["cluster"]]      # the whole group, not just the faces shown
            rows = [int(k) for k in c[(d.pid[c] == -1) & (self.sug_score[c] < SUGGEST)]]
        undo = [self.row[i] for i in body.get("undo") or [] if i in self.row]
        if undo:                                    # take back whatever was said about these faces
            up = body.get("undo_person")             # only what was said about this person, if given
            for k in undo:
                for q in [up] if up is not None else list(d.nots) + list(d.later):
                    d.nots.get(q, set()).discard(k)
                    d.later.get(q, set()).discard(k)
                d.pid[k], d.src[k] = -1, NONE
            where, extra = ("face_id=? AND person_id=?", (up,)) if up is not None else ("face_id=?", ())
            for t in ("face_not", "face_later"):
                con.executemany(f"DELETE FROM {t} WHERE {where}", [(int(self.ids[k]), *extra) for k in undo])
            con.executemany("UPDATE faces SET person_id=NULL, source=NULL, labeled_at=? WHERE id=?",
                            [(now(), int(self.ids[k])) for k in undo])
            d.logs.append(("undo", undo, {}))
        who = body.get("person")
        if rows and who is None:
            for k in rows:                          # "that's not them": forget the name, and don't offer it again
                if d.pid[k] > 0:
                    d.nots.setdefault(int(d.pid[k]), set()).add(k)
                    con.execute("INSERT OR IGNORE INTO face_not VALUES (?,?)", (int(self.ids[k]), int(d.pid[k])))
                d.pid[k], d.src[k] = -1, NONE
            con.executemany("UPDATE faces SET person_id=NULL, source=NULL, labeled_at=? WHERE id=?",
                            [(now(), int(self.ids[k])) for k in rows])
            d.logs.append(("forget", rows, {}))
        elif rows:
            p = 0 if who == 0 else self.person_id(con, who, d)
            for k in rows:
                d.pid[k], d.src[k] = p, YOU
            con.executemany("UPDATE faces SET person_id=?, source='you', labeled_at=? WHERE id=?",
                            [(p, now(), int(self.ids[k])) for k in rows])
            d.logs.append(("name", rows, {"person": d.persons[p]["name"] if p else None}))
        later, lp = [self.row[i] for i in body.get("later") or [] if i in self.row], body.get("later_person")
        if later and lp in d.persons:
            d.later.setdefault(lp, set()).update(later)
            con.executemany("INSERT OR IGNORE INTO face_later VALUES (?,?,?)", [(int(self.ids[k]), lp, now()) for k in later])
            d.logs.append(("later", later, {"person": d.persons[lp]["name"]}))
        wrong, np_ = [self.row[i] for i in body.get("not") or [] if i in self.row], body.get("not_person")
        if wrong and np_ in d.persons:
            d.nots.setdefault(np_, set()).update(wrong)
            con.executemany("INSERT OR IGNORE INTO face_not VALUES (?,?)", [(int(self.ids[k]), np_) for k in wrong])
            d.logs.append(("not", wrong, {"person": d.persons[np_]["name"]}))

    def edit_person(self, body: dict, actor: int | None = None) -> dict:
        """{id, name?, born?}: rename (onto an existing name = merge the two) or set when they were born.
        A merge moves everything of p's to the other person: their faces, and p's face_not and face_later
        rows (a face already ruled out or put aside for both keeps one row), in SQLite and in memory, and
        leaves nothing that names p. Journalled as born, rename and merge records."""
        with self.lock:
            p = body.get("id")
            if p not in self.persons:
                return {"error": "unknown person"}
            d = Draft(self)
            with closing(connect(self.db)) as con, con:
                if "born" in body:
                    born = (body["born"] or "").strip()[:7] or None
                    if born != d.persons[p]["born"]:
                        d.logs.append(("born", [], {"person": d.persons[p]["name"], "born": born}))
                    d.persons[p]["born"] = born
                    con.execute("UPDATE persons SET born=? WHERE id=?", (born, p))
                name = " ".join(str(body.get("name") or "").split())
                if name and name != d.persons[p]["name"]:
                    other = next((q for q, i in d.persons.items() if q != p and i["name"].lower() == name.lower()), None)
                    if other is None:
                        d.logs.append(("rename", [], {"person": d.persons[p]["name"], "name": name}))
                        d.persons[p]["name"] = name
                        con.execute("UPDATE persons SET name=? WHERE id=?", (name, p))
                    else:                                   # merge p into the person who already has that name
                        d.pid[d.pid == p] = other
                        con.execute("UPDATE faces SET person_id=? WHERE person_id=?", (other, p))
                        for t in ("face_not", "face_later"):   # (face_id, person_id) is the key: a row both have stays once
                            con.execute(f"UPDATE OR IGNORE {t} SET person_id=? WHERE person_id=?", (other, p))
                            con.execute(f"DELETE FROM {t} WHERE person_id=?", (p,))
                        con.execute("DELETE FROM persons WHERE id=?", (p,))
                        for sets in (d.nots, d.later):
                            if moved := sets.pop(p, None):
                                sets.setdefault(other, set()).update(moved)
                        d.logs.append(("merge", [], {"person": d.persons.pop(p)["name"], "into": d.persons[other]["name"]}))
                        p = other
                self.journaled(con, d, actor, "person.changed", f"person:{body['id']}")
            self.commit(d)
            self.rescore()
            return {"ok": True, "id": p}

    def crop(self, face_id: int):
        k = self.row.get(face_id)
        if k is None:
            return None
        for c in self.caches:
            f = crop_path(c, self.sha[k], self.idx[k])
            if f.exists():
                return f.read_bytes()
        try:  # the cache was cleared: cut it from the original again
            from PIL import Image, ImageOps
            import pillow_heif
            pillow_heif.register_heif_opener()
            s = self.sha[k]
            with Image.open(self.store / s[:2] / s[2:4] / f"{s}{self.ext[k]}") as im:
                im.draft("RGB", (SIDE, SIDE))
                im = ImageOps.exif_transpose(im).convert("RGB")
                dest = crop_path(self.caches[0], s, self.idx[k])
                dest.parent.mkdir(parents=True, exist_ok=True)
                face_crop(im, *(float(v) for v in self.box[k])).save(dest, "JPEG", quality=82)
            return dest.read_bytes()
        except Exception:
            return None

    @staticmethod
    def only(o, keep):
        """A GET's answer with the faces in photos `keep` (sha -> bool) does not keep left out: `faces` lists,
        a timeline's `years`, the unnamed `groups`; a `face` that is not kept is not there at all (None)."""
        if keep is None or not isinstance(o, dict):
            return o
        if isinstance(o.get("face"), dict) and not keep(o["face"]["sha"]):
            return None
        out = dict(o)
        if isinstance(o.get("faces"), list):
            out["faces"] = [f for f in o["faces"] if keep(f["sha"])]
        if isinstance(o.get("years"), dict) and all(isinstance(v, list) for v in o["years"].values()):
            out["years"] = {y: kept for y, fs in o["years"].items() if (kept := [f for f in fs if keep(f["sha"])])}
        if isinstance(o.get("groups"), list):
            out["groups"] = [g for g in (People.only(g, keep) for g in o["groups"]) if g and g.get("faces")]
        return out

    def handle(self, method: str, path: str, qs: dict, body: dict | None, actor: int | None = None, keep=None):
        """One request -> (status, body bytes, content type, Cache-Control), or None if it isn't ours. `actor`:
        the member a write is for (members.id), for the journal. `keep` (sha -> bool): the photos the member
        asking sees (photo_app.py); the faces of any other are left out of what a GET lists (only)."""
        one = lambda k, d="": (qs.get(k) or [d])[0]
        num = lambda k: int(one(k)) if one(k).lstrip("-").isdigit() else None
        js = lambda o, code=200: (code, json.dumps(o, separators=(",", ":")).encode(), "application/json", "no-store")
        found = lambda o: None if (o := self.only(o, keep)) is None else js(o)   # None: no such person or face (or not theirs to see)
        if method == "GET":
            if path in ("/people", "/people/"):
                return 200, pages.page("people.html"), "text/html; charset=utf-8", "no-cache"
            if path.startswith("/face/"):
                body_ = self.crop(int(path[6:])) if path[6:].isdigit() else None
                return (200, body_, "image/jpeg", "public, max-age=86400") if body_ else (404, b"", "text/plain", "no-store")
            if path == "/api/people":
                return js(self.overview(keep))
            person, face = num("person"), num("face")
            if path == "/api/people/review" and person is not None:
                return found(self.review(person, num("year"), min(num("limit") or 60, 400), one("skipped") == "1"))
            if path == "/api/people/person" and person is not None:
                return found(self.person_faces(person, num("year"), min(num("limit") or 12, 2000)))
            if path == "/api/people/odd" and person is not None:
                return found(self.odd(person, num("year"), max(num("offset") or 0, 0), min(num("limit") or 60, 400)))
            if path == "/api/people/unknown":
                return js(self.only(self.unknown(min(num("limit") or 12, 100)), keep))
            if path == "/api/people/photo" and face is not None:
                return found(self.photo(face))
        elif method == "POST" and body is not None:
            if path == "/api/people/label":
                return js(self.label(body, actor))
            if path == "/api/people/label-many":
                return js(self.label_many(body, actor))
            if path == "/api/people/person":
                return js(self.edit_person(body, actor))
        return None


def cmd_stats(a) -> None:
    con = connect(a.db)
    scanned, faces = con.execute("SELECT count(*), coalesce(sum(n), 0) FROM face_scans WHERE model=?", (MODEL,)).fetchone()
    images = con.execute("SELECT count(*) FROM assets WHERE kind='image'").fetchone()[0]
    print(f"{scanned} of {images} images scanned, {faces} faces")
    for name, src, n in con.execute(
            "SELECT coalesce(p.name, CASE WHEN f.person_id=0 THEN '(not named, by you)' ELSE '(unknown)' END),"
            " coalesce(f.source, ''), count(*) FROM faces f LEFT JOIN persons p ON p.id=f.person_id"
            " GROUP BY 1, 2 ORDER BY 1, 2"):
        print(f"  {name:28} {src:7} {n}")


def cmd_serve(a) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse

    from http_util import HTTPError, read_json
    cache = Path(a.cache or Path(a.db).resolve().parent / "review")
    people = People(a.db, a.store, [cache])
    meta = dict(connect(a.db).execute("SELECT sha256, ext FROM assets WHERE kind='image'"))

    def preview(sha: str):
        dest = cache / "previews" / f"{sha}.jpg"
        if not dest.exists() and sha in meta:
            from PIL import Image, ImageOps
            import pillow_heif
            pillow_heif.register_heif_opener()
            dest.parent.mkdir(parents=True, exist_ok=True)
            with Image.open(Path(a.store) / sha[:2] / sha[2:4] / f"{sha}{meta[sha]}") as im:
                im.draft("RGB", (1600, 1600))
                im = ImageOps.exif_transpose(im).convert("RGB")
                im.thumbnail((1600, 1600))
                im.save(dest, "JPEG", quality=82)
        return dest.read_bytes() if dest.exists() else None

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def reply(self, got):
            code, body, ctype, cc = got or (404, b"not found", "text/plain", "no-store")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cc)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            try:
                if u.path == "/":
                    self.send_response(302)
                    self.send_header("Location", "/people")
                    self.send_header("Content-Length", "0")
                    return self.end_headers()
                if (mod := pages.module(u.path)) is not None:     # what people.html imports
                    return self.reply((200, mod, pages.MODULE_TYPE, "no-cache"))
                if u.path.startswith("/preview/"):
                    body = preview(u.path[9:]) if len(u.path) == 73 and u.path[9:].isalnum() else None
                    return self.reply((200, body, "image/jpeg", "public, max-age=86400") if body else None)
                self.reply(people.handle("GET", u.path, parse_qs(u.query), None))
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            u = urlparse(self.path)
            try:   # 400 for a bad Content-Length or a body that isn't JSON, 413 over http_util.MAX_BODY
                body = read_json(self.rfile, self.headers)
            except HTTPError as e:
                self.close_connection = True           # the body may be unread: don't take it for the next request
                r = e.response
                return self.reply((r.status, r.body, r.content_type, r.cache))
            self.reply(people.handle("POST", u.path, parse_qs(u.query), body if isinstance(body, dict) else None))

    class Server(ThreadingHTTPServer):
        request_queue_size = 256
        daemon_threads = True

    srv = Server(("127.0.0.1", a.port), Handler)
    print(f"Open http://127.0.0.1:{a.port}/people  (Ctrl-C to stop)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default="estate.db")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("run", help="find and embed the faces in images not scanned yet")
    s.add_argument("--store", required=True)
    s.add_argument("--cache", help="where face thumbnails go (default: <db dir>/review, the photo app's cache)")
    s.add_argument("--workers", type=int, default=3, help="decoder processes, in parallel with the models")
    s.add_argument("--after-rowid", type=int, help="only assets added after this rowid (incremental runs: a few index lookups, not a scan)")
    s.add_argument("--threads", type=int, default=6, help="onnxruntime threads")
    s.add_argument("--limit", type=int, default=0)
    s.set_defaults(fn=cmd_run)
    sub.add_parser("stats").set_defaults(fn=cmd_stats)
    s = sub.add_parser("serve", help="the naming page")
    s.add_argument("--store", required=True)
    s.add_argument("--cache", help="thumbnail cache dir (default: <db dir>/review)")
    s.add_argument("--port", type=int, default=config.FACES)
    s.set_defaults(fn=cmd_serve)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
