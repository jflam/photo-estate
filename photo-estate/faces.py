#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["onnxruntime>=1.17", "numpy", "pillow>=10.0", "pillow-heif>=0.18"]
# ///
"""
faces.py — Tier 4: find every face, and work out who it is with your help.

  run     detect and embed the faces in every image not scanned yet (resumable)
  stats   how many faces are found, named, and waiting for you
  serve   the naming page on its own (http://127.0.0.1:8768/people); `embed.py serve` also shows
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
and box, so the work survives a rescan with a better model.

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
from itertools import islice
from multiprocessing import Pool
from pathlib import Path

MODEL = "buffalo_l"
MODEL_URL = "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip"
MODEL_FILES = {  # name -> sha256
    "det_10g.onnx": "5838f7fe053675b1c7a08b633df49e7af5495cee0493c7dcf6697200b85b5b91",
    "w600k_r50.onnx": "4c06341c33c2ca1f86781dab0e829f88ad5b64be9fba56e56bc9ebdefc619e43",
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS faces(
  id INTEGER PRIMARY KEY, sha256 TEXT, idx INT, model TEXT,
  x REAL, y REAL, w REAL, h REAL,   -- the box, as fractions of the upright image
  score REAL,                       -- detector confidence, 0-1
  px INT,                           -- the box's longer side in pixels, at the size it was read
  vec BLOB,                         -- float16 x 512, L2-normalised
  person_id INT,                    -- persons.id; 0 = a face you chose not to name; NULL = unknown
  source TEXT,                      -- who says so: 'you', 'google' or 'auto'
  labeled_at TEXT);
CREATE INDEX IF NOT EXISTS faces_sha ON faces(sha256);
CREATE TABLE IF NOT EXISTS face_scans(   -- every image looked at, including those with no faces
  sha256 TEXT, model TEXT, n INT, error TEXT, created_at TEXT, PRIMARY KEY(sha256, model));
CREATE TABLE IF NOT EXISTS persons(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE, born TEXT, created_at TEXT);   -- born: 'YYYY' or 'YYYY-MM'
CREATE TABLE IF NOT EXISTS face_not(     -- "this face is not that person", so it's never suggested again
  face_id INT, person_id INT, PRIMARY KEY(face_id, person_id));
CREATE TABLE IF NOT EXISTS face_later(   -- "is this ...?" skipped for now: still unnamed, asked again later
  face_id INT, person_id INT, created_at TEXT, PRIMARY KEY(face_id, person_id));
"""
SKIP_EXT = {".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2", ".raf", ".gif"}
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
    con.executescript(SCHEMA)
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
        import onnxruntime as ort
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
            c = np.stack(np.mgrid[:n, :n][::-1], -1).reshape(-1, 2).astype(np.float32) * stride
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
    todo = con.execute(
        "SELECT a.sha256, a.ext FROM assets a WHERE a.kind='image' AND NOT EXISTS"
        " (SELECT 1 FROM face_scans s WHERE s.sha256=a.sha256 AND s.model=?) ORDER BY a.sha256", (MODEL,)).fetchall()
    todo = [(s, e) for s, e in todo if e not in SKIP_EXT][: a.limit or None]
    done, found = con.execute("SELECT count(*), coalesce(sum(n), 0) FROM face_scans WHERE model=?", (MODEL,)).fetchone()
    print(f"{done} images already scanned ({found} faces), {len(todo)} to do", flush=True)
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
                last = time.time()
        flush()
    dt = time.time() - t0
    print(f"done: {n} images in {dt / 3600:.2f} h ({n / dt:.1f} img/s), {nfaces} faces, {errors} undecodable", flush=True)


# ── who is who ──────────────────────────────────────────────────────────────────────────────────

NONE, AUTO_SRC, GOOGLE, YOU = 0, 1, 2, 3
SRC = {None: NONE, "auto": AUTO_SRC, "google": GOOGLE, "you": YOU}
SRC_NAME = {v: k for k, v in SRC.items()}


class People:
    """Every face in memory, who each one is, and the questions worth asking. Used by `faces.py serve`
    and mounted at /people by `embed.py serve`."""

    def __init__(self, db: str, store: str, caches: list, log=print):
        import numpy as np
        self.np, self.db, self.store, self.caches = np, db, Path(store), [Path(c) for c in caches]
        self.lock = threading.RLock()
        self.journal = Path(db).resolve().parent / "face-labels.jsonl"
        self.page = Path(__file__).with_name("people.html")
        t0 = time.time()
        con = connect(db)
        rows = con.execute(
            "SELECT f.id, f.sha256, f.idx, f.x, f.y, f.w, f.h, f.score, f.px, f.vec, f.person_id, f.source,"
            " a.taken_at, a.people, a.ext FROM faces f JOIN assets a USING(sha256) WHERE f.model=? ORDER BY f.id",
            (MODEL,)).fetchall()
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
        self.clusters, self.clusters_state = [], "waiting"
        self.seed(con)
        self.rescore(con)
        con.close()
        log(f"people: {n} faces, {len(self.persons)} people, {int((self.pid > 0).sum())} named,"
            f" {int(self.pending().sum())} to review, in {time.time() - t0:.1f}s")
        threading.Thread(target=self.cluster, daemon=True).start()

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
                cp.append(p), cy.append(int(y)), C.append(v / np.linalg.norm(v))
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

    def rescore(self, con=None):
        """Recompute every suggestion and every automatic name from the faces named so far."""
        np = self.np
        with self.lock:
            rows = np.flatnonzero(self.src < GOOGLE)   # everything you or Google haven't settled
            old = self.pid[rows].copy()
            self.sug[:], self.sug_score[:], self.sug_far[:] = -1, 0, False
            self.pid[rows], self.src[rows] = -1, NONE
            pids, S, far = self.scores(rows)
            if pids:
                at = {int(r): k for k, r in enumerate(rows)}
                for j, p in enumerate(pids):
                    ks = [at[r] for r in self.nots.get(p, ()) if r in at]
                    S[ks, j] = -1
                col = {p: j for j, p in enumerate(pids)}
                for k in np.flatnonzero((self.src >= GOOGLE) & (self.pid > 0)):   # nobody is in a photo twice
                    j = col.get(int(self.pid[k]))
                    if j is not None:
                        for o in self.by_sha[self.sha[k]]:
                            if o != k and o in at:
                                S[at[o], j] = -1
                best = S.argmax(1)
                top = S[np.arange(len(rows)), best]
                S[np.arange(len(rows)), best] = -1
                second = S.max(1)
                self.sug[rows] = np.array(pids, np.int32)[best]
                self.sug_score[rows] = top
                self.sug_far[rows] = far[np.arange(len(rows)), best]
                sure = (top >= AUTO) & (top - second >= AUTO_MARGIN) & ~self.sug_far[rows] & (self.px[rows] >= 40)
                keep = {}                                   # two sure faces of one person in a photo: only the closer
                for i in np.flatnonzero(sure):
                    key = (self.sha[rows[i]], int(self.sug[rows[i]]))
                    if key not in keep or top[i] > top[keep[key]]:
                        keep[key] = i
                sure[:] = False
                sure[list(keep.values())] = True
                self.pid[rows[sure]], self.src[rows[sure]] = self.sug[rows[sure]], AUTO_SRC
            changed = rows[self.pid[rows] != old]
            if len(changed):
                own = con is None
                con = con or connect(self.db)
                con.executemany("UPDATE faces SET person_id=?, source=?, labeled_at=? WHERE id=?",
                                [(int(self.pid[r]) if self.pid[r] > 0 else None, "auto" if self.pid[r] > 0 else None,
                                  now(), int(self.ids[r])) for r in changed])
                con.commit()
                if own:
                    con.close()

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
        free = lambda k, p: self.src[k] < GOOGLE and k not in self.nots.get(p, ())
        for s, ks in self.by_sha.items():       # one tag, one face
            tags = self.tags[ks[0]]
            if len(tags) == 1 and len(ks) == 1 and good[ks[0]] and free(ks[0], have[tags[0]]):
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
                for k in rows[own < 0.2]:
                    self.pid[k], self.src[k] = -1, NONE
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
                    used.add(k), taken.add(p)
                    self.pid[k], self.src[k] = p, GOOGLE
                    new.append(k)
        con.executemany("UPDATE faces SET person_id=?, source='google', labeled_at=? WHERE id=?",
                        [(int(self.pid[k]), now(), int(self.ids[k])) for k in new if self.src[k] == GOOGLE])
        con.commit()

    # -- faces nobody has named ----------------------------------------------------------------

    def cluster(self):
        """Group the faces that resemble nobody known into people who appear often. Each group is one
        face and everything close to it, so a group never chains from one person into another."""
        np = self.np
        self.clusters_state = "working"
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
        self.clusters, self.clusters_state = out, "ready"

    # -- the page's API ------------------------------------------------------------------------

    def face(self, k: int) -> dict:
        k = int(k)
        return {"id": int(self.ids[k]), "sha": self.sha[k], "date": self.date[k], "year": int(self.year[k]),
                "score": round(float(self.sug_score[k]), 3), "src": SRC_NAME[int(self.src[k])],
                "person": int(self.pid[k]), "sug": int(self.sug[k]), "far": bool(self.sug_far[k]),
                "box": [round(float(v), 4) for v in self.box[k]]}

    def overview(self) -> dict:
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

    def review(self, p: int, year, limit: int, skipped: bool = False) -> dict:
        """Faces waiting for p, most likely first; the ones you skipped are kept apart (skipped=True)."""
        np = self.np
        with self.lock:
            pend, later = self.pending(p), self.skipped(p)
            m = pend & later if skipped else pend & ~later
            total = int(m.sum())
            if year is not None:
                m = m & (self.year == year)
            rows = np.flatnonzero(m)
            rows = rows[np.argsort(-self.sug_score[rows])][:limit]
            return {"person": {"id": p, **self.persons[p]}, "total": total, "matching": int(m.sum()),
                    "skipped": int((pend & later).sum()), "faces": [self.face(k) for k in rows]}

    def person_faces(self, p: int, year, limit: int) -> dict:
        """A person's named faces: a few per year for the timeline, or all of one year."""
        np = self.np
        with self.lock:
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

    def odd(self, p: int, year, offset: int, limit: int) -> dict:
        """A person's named faces, least like the rest of them first: each face against the mean of their
        other named faces from within WINDOW years (every year, if the photo is undated). A face with no
        others near it in time comes first too. This is where the mistakes are."""
        np = self.np
        with self.lock:
            mine = np.flatnonzero(self.pid == p)
            ys = self.year[mine]
            sums = {int(y): self.V[mine[ys == y]].sum(0) for y in np.unique(ys)}
            total = sum(sums.values()) if sums else np.zeros(self.V.shape[1], np.float32)
            fit = np.empty(len(mine), np.float32)
            for y in sums:
                at = np.flatnonzero(ys == y)
                W = total if y == 0 else sum(v for yy, v in sums.items() if yy and abs(yy - y) <= WINDOW)
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
        np = self.np
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
            return {"state": self.clusters_state, "groups": out}

    def photo(self, face_id: int) -> dict:
        with self.lock:
            k = self.row[face_id]
            others = [dict(self.face(j), name=self.persons.get(int(self.pid[j]), {}).get("name"))
                      for j in self.by_sha[self.sha[k]]]
            return {"face": self.face(k), "faces": others}

    def log(self, op: str, rows, **extra):
        try:
            with self.journal.open("a") as f:
                f.write(json.dumps({"ts": now(), "op": op, **extra,
                                    "faces": [[self.sha[k], *[round(float(v), 4) for v in self.box[k]]] for k in rows]},
                                   ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError as e:
            print(f"face-labels.jsonl: {e}", flush=True)

    def person_id(self, con, who) -> int:
        """An id, or a name: an existing person's, or a new person's."""
        if isinstance(who, int):
            return who
        name = " ".join(str(who).split())
        for p, info in self.persons.items():
            if info["name"].lower() == name.lower():
                return p
        p = con.execute("INSERT INTO persons(name, created_at) VALUES (?,?)", (name, now())).lastrowid
        self.persons[p] = {"name": name, "born": None}
        return p

    def label_many(self, body: dict) -> dict:
        """{items: [label bodies]}: a page of decisions, applied in order, then one rescore."""
        with self.lock:
            for item in body.get("items") or []:
                if isinstance(item, dict):
                    self.label(item, rescore=False)
            self.rescore()
            return {"ok": True, "pending": int(self.pending().sum())}

    def label(self, body: dict, rescore: bool = True) -> dict:
        """{faces: [ids], person: id | "name" | 0 (don't name) | null (forget), cluster: n, not: [ids], not_person: id}.
        {pending: {person: id, year: y | null}} in place of faces: every face waiting for that person (in that
        year), less the ones in `not`, so you can say yes to all of them and find the mistakes afterwards.
        undo: [ids] takes back what was said about those faces first: their name, and "not" or "later" for
        undo_person (for everyone, without it).
        later: [ids], later_person: id puts those faces aside, unnamed, to be asked about again later."""
        with self.lock:
            con = connect(self.db)
            rows = [self.row[i] for i in body.get("faces") or [] if i in self.row]
            pend = body.get("pending")
            if isinstance(pend, dict) and pend.get("person") in self.persons:
                m = self.pending(pend["person"]) & ~self.skipped(pend["person"])
                if pend.get("year") is not None:
                    m = m & (self.year == int(pend["year"]))
                skip = {self.row[i] for i in body.get("not") or [] if i in self.row}
                rows = [int(k) for k in self.np.flatnonzero(m) if int(k) not in skip]
            if body.get("cluster") is not None and 0 <= body["cluster"] < len(self.clusters):
                c = self.clusters[body["cluster"]]      # the whole group, not just the faces shown
                rows = [int(k) for k in c[(self.pid[c] == -1) & (self.sug_score[c] < SUGGEST)]]
            undo = [self.row[i] for i in body.get("undo") or [] if i in self.row]
            if undo:                                    # take back whatever was said about these faces
                up = body.get("undo_person")             # only what was said about this person, if given
                for k in undo:
                    for q in [up] if up is not None else list(self.nots) + list(self.later):
                        self.nots.get(q, set()).discard(k)
                        self.later.get(q, set()).discard(k)
                    self.pid[k], self.src[k] = -1, NONE
                where, extra = ("face_id=? AND person_id=?", (up,)) if up is not None else ("face_id=?", ())
                for t in ("face_not", "face_later"):
                    con.executemany(f"DELETE FROM {t} WHERE {where}", [(int(self.ids[k]), *extra) for k in undo])
                con.executemany("UPDATE faces SET person_id=NULL, source=NULL, labeled_at=? WHERE id=?",
                                [(now(), int(self.ids[k])) for k in undo])
                self.log("undo", undo)
            who = body.get("person")
            if rows and who is None:
                for k in rows:                          # "that's not them": forget the name, and don't offer it again
                    if self.pid[k] > 0:
                        self.nots.setdefault(int(self.pid[k]), set()).add(k)
                        con.execute("INSERT OR IGNORE INTO face_not VALUES (?,?)", (int(self.ids[k]), int(self.pid[k])))
                    self.pid[k], self.src[k] = -1, NONE
                con.executemany("UPDATE faces SET person_id=NULL, source=NULL, labeled_at=? WHERE id=?",
                                [(now(), int(self.ids[k])) for k in rows])
                self.log("forget", rows)
            elif rows:
                p = 0 if who == 0 else self.person_id(con, who)
                for k in rows:
                    self.pid[k], self.src[k] = p, YOU
                con.executemany("UPDATE faces SET person_id=?, source='you', labeled_at=? WHERE id=?",
                                [(p, now(), int(self.ids[k])) for k in rows])
                self.log("name", rows, person=self.persons[p]["name"] if p else None)
            later, lp = [self.row[i] for i in body.get("later") or [] if i in self.row], body.get("later_person")
            if later and lp in self.persons:
                self.later.setdefault(lp, set()).update(later)
                con.executemany("INSERT OR IGNORE INTO face_later VALUES (?,?,?)", [(int(self.ids[k]), lp, now()) for k in later])
                self.log("later", later, person=self.persons[lp]["name"])
            wrong, np_ = [self.row[i] for i in body.get("not") or [] if i in self.row], body.get("not_person")
            if wrong and np_ in self.persons:
                self.nots.setdefault(np_, set()).update(wrong)
                con.executemany("INSERT OR IGNORE INTO face_not VALUES (?,?)", [(int(self.ids[k]), np_) for k in wrong])
                self.log("not", wrong, person=self.persons[np_]["name"])
            con.commit()
            if rescore:
                self.rescore(con)
            con.close()
            return {"ok": True, "pending": int(self.pending().sum())}

    def edit_person(self, body: dict) -> dict:
        """{id, name?, born?}: rename (onto an existing name = merge the two) or set when they were born."""
        with self.lock:
            p = body.get("id")
            if p not in self.persons:
                return {"error": "unknown person"}
            con = connect(self.db)
            if "born" in body:
                born = (body["born"] or "").strip()[:7] or None
                self.persons[p]["born"] = born
                con.execute("UPDATE persons SET born=? WHERE id=?", (born, p))
            name = " ".join(str(body.get("name") or "").split())
            if name and name != self.persons[p]["name"]:
                other = next((q for q, i in self.persons.items() if q != p and i["name"].lower() == name.lower()), None)
                if other is None:
                    self.persons[p]["name"] = name
                    con.execute("UPDATE persons SET name=? WHERE id=?", (name, p))
                else:                                   # merge p into the person who already has that name
                    self.pid[self.pid == p] = other
                    con.execute("UPDATE faces SET person_id=? WHERE person_id=?", (other, p))
                    con.execute("UPDATE OR IGNORE face_not SET person_id=? WHERE person_id=?", (other, p))
                    con.execute("DELETE FROM face_not WHERE person_id=?", (p,))
                    con.execute("DELETE FROM persons WHERE id=?", (p,))
                    self.nots.setdefault(other, set()).update(self.nots.pop(p, set()))
                    del self.persons[p]
                    p = other
            con.commit()
            self.rescore(con)
            con.close()
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

    def handle(self, method: str, path: str, qs: dict, body: dict | None):
        """One request -> (status, body bytes, content type, Cache-Control), or None if it isn't ours."""
        one = lambda k, d="": (qs.get(k) or [d])[0]
        num = lambda k: int(one(k)) if one(k).lstrip("-").isdigit() else None
        js = lambda o, code=200: (code, json.dumps(o, separators=(",", ":")).encode(), "application/json", "no-store")
        if method == "GET":
            if path in ("/people", "/people/"):
                return 200, self.page.read_bytes(), "text/html; charset=utf-8", "no-cache"
            if path.startswith("/face/"):
                body_ = self.crop(int(path[6:])) if path[6:].isdigit() else None
                return (200, body_, "image/jpeg", "public, max-age=86400") if body_ else (404, b"", "text/plain", "no-store")
            if path == "/api/people":
                return js(self.overview())
            if path == "/api/people/review" and num("person") in self.persons:
                return js(self.review(num("person"), num("year"), min(num("limit") or 60, 400), one("skipped") == "1"))
            if path == "/api/people/person" and num("person") in self.persons:
                return js(self.person_faces(num("person"), num("year"), min(num("limit") or 12, 2000)))
            if path == "/api/people/odd" and num("person") in self.persons:
                return js(self.odd(num("person"), num("year"), max(num("offset") or 0, 0), min(num("limit") or 60, 400)))
            if path == "/api/people/unknown":
                return js(self.unknown(min(num("limit") or 12, 100)))
            if path == "/api/people/photo" and num("face") in self.row:
                return js(self.photo(num("face")))
        elif method == "POST" and body is not None:
            if path == "/api/people/label":
                return js(self.label(body))
            if path == "/api/people/label-many":
                return js(self.label_many(body))
            if path == "/api/people/person":
                return js(self.edit_person(body))
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
                if u.path.startswith("/preview/"):
                    body = preview(u.path[9:]) if len(u.path) == 73 and u.path[9:].isalnum() else None
                    return self.reply((200, body, "image/jpeg", "public, max-age=86400") if body else None)
                self.reply(people.handle("GET", u.path, parse_qs(u.query), None))
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            u = urlparse(self.path)
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except ValueError:
                body = None
            self.reply(people.handle("POST", u.path, parse_qs(u.query), body))

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
    s.add_argument("--threads", type=int, default=6, help="onnxruntime threads")
    s.add_argument("--limit", type=int, default=0)
    s.set_defaults(fn=cmd_run)
    sub.add_parser("stats").set_defaults(fn=cmd_stats)
    s = sub.add_parser("serve", help="the naming page")
    s.add_argument("--store", required=True)
    s.add_argument("--cache", help="thumbnail cache dir (default: <db dir>/review)")
    s.add_argument("--port", type=int, default=8768)
    s.set_defaults(fn=cmd_serve)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
