#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["torch", "transformers>=4.49", "pillow>=10.0", "pillow-heif>=0.18", "sentencepiece", "protobuf"]
# ///
"""
embed.py — Tier 1: SigLIP 2 image embeddings for semantic search (kept apart from estate.py so the
catalog tools don't pull in PyTorch).

  run     embed every image in the catalog that has no embedding for this model yet (resumable)
  search  text query -> best-matching photos, by cosine similarity
  stats   how many images are embedded per model
  serve   local web page for trying searches (http://127.0.0.1:8766/)

  uv run embed.py --db /Volumes/estate/estate.db run --store /Volumes/estate/store
  uv run embed.py --db /Volumes/estate/estate.db search "kids at the lake"

Vectors are L2-normalised float16, one row per (sha256, model), so a better model can be added
later without touching the first. Queries must use the same model as the vectors they search.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from multiprocessing import Pool
from pathlib import Path

DEFAULT_MODEL = "google/siglip2-base-patch16-256"
SCHEMA = """
CREATE TABLE IF NOT EXISTS embeddings(
  sha256 TEXT, model TEXT, dim INT, vec BLOB,   -- float16, L2-normalised; NULL when undecodable
  error TEXT, created_at TEXT, PRIMARY KEY(sha256, model));
"""
SKIP_EXT = {".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2", ".raf"}  # Pillow can't decode RAW


def connect(db: str) -> sqlite3.Connection:
    con = sqlite3.connect(db, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    return con


def _decode(job: tuple[str, str, int]):
    """Worker: open one image, upright it and shrink to the model's input size. Never raises."""
    sha, path, side = job
    try:
        from PIL import Image, ImageOps
        import pillow_heif
        pillow_heif.register_heif_opener()
        Image.MAX_IMAGE_PIXELS = None
        with Image.open(path) as im:
            im.draft("RGB", (side * 2, side * 2))  # fast reduced-size JPEG decode
            im = ImageOps.exif_transpose(im).convert("RGB")
            im = im.resize((side, side), Image.Resampling.BICUBIC)  # SigLIP squashes, no crop
            return sha, im.tobytes(), None
    except Exception as e:
        return sha, None, f"{type(e).__name__}: {e}"[:300]


def _thumb(job: tuple[str, str, int]) -> bool:
    """Worker: write one JPEG thumbnail. Never raises."""
    src, dest, side = job
    try:
        from PIL import Image, ImageOps
        import pillow_heif
        pillow_heif.register_heif_opener()
        Image.MAX_IMAGE_PIXELS = None
        with Image.open(src) as im:
            im.draft("RGB", (side, side))
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((side, side))
            tmp = f"{dest}.{os.getpid()}.tmp"
            im.save(tmp, "JPEG", quality=82)
            os.replace(tmp, dest)
        return True
    except Exception:
        return False


def load_model(mid: str, threads: int):
    import torch
    from transformers import AutoModel, AutoProcessor
    torch.set_num_threads(threads)
    return torch, AutoModel.from_pretrained(mid, dtype=torch.float32).eval(), AutoProcessor.from_pretrained(mid)


def _features(torch, out):
    return out if torch.is_tensor(out) else out.pooler_output


def cmd_run(a) -> None:
    con = connect(a.db)
    store = Path(a.store)
    todo = con.execute(
        "SELECT a.sha256, a.ext FROM assets a WHERE a.kind='image' AND NOT EXISTS"
        " (SELECT 1 FROM embeddings e WHERE e.sha256=a.sha256 AND e.model=?) ORDER BY a.sha256",
        (a.model,)).fetchall()
    skipped = [(s, e) for s, e in todo if e in SKIP_EXT]
    todo = [(s, e) for s, e in todo if e not in SKIP_EXT][: a.limit or None]
    done = con.execute("SELECT count(*) FROM embeddings WHERE model=?", (a.model,)).fetchone()[0]
    print(f"{a.model}: {done} already embedded, {len(todo)} to do"
          f" ({len(skipped)} RAW files skipped)", flush=True)
    if not todo:
        return
    torch, model, proc = load_model(a.model, a.threads)
    side = proc.image_processor.size["height"]
    import numpy as np
    from PIL import Image

    jobs = [(s, str(store / s[:2] / s[2:4] / f"{s}{e}"), side) for s, e in todo]
    t0 = last = time.time()
    n = errors = 0
    batch_ims, batch_shas, rows = [], [], []

    def flush():
        nonlocal batch_ims, batch_shas, rows
        if batch_ims:
            with torch.no_grad():
                px = proc(images=batch_ims, return_tensors="pt")["pixel_values"]
                v = torch.nn.functional.normalize(_features(torch, model.get_image_features(pixel_values=px)), dim=-1)
            for s, vec in zip(batch_shas, v.numpy().astype(np.float16)):
                rows.append((s, a.model, vec.shape[0], vec.tobytes(), None, now()))
        con.executemany("INSERT OR REPLACE INTO embeddings VALUES (?,?,?,?,?,?)", rows)
        con.commit()
        batch_ims, batch_shas, rows = [], [], []

    with Pool(a.workers) as pool:
        for sha, raw, err in pool.imap(_decode, jobs, chunksize=4):
            n += 1
            if err:
                errors += 1
                rows.append((sha, a.model, None, None, err, now()))
            else:
                batch_ims.append(Image.frombytes("RGB", (side, side), raw))
                batch_shas.append(sha)
            if len(batch_ims) >= a.batch:
                flush()
            if time.time() - last >= 60:
                rate = n / (time.time() - t0)
                eta = (len(jobs) - n) / rate / 3600
                print(f"{datetime.now():%H:%M:%S} {n}/{len(jobs)} {rate:.1f} img/s,"
                      f" {errors} undecodable, ~{eta:.1f} h left", flush=True)
                last = time.time()
        flush()
    dt = time.time() - t0
    print(f"done: {n} images in {dt / 3600:.2f} h ({n / dt:.1f} img/s), {errors} undecodable", flush=True)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def cmd_search(a) -> None:
    import numpy as np
    con = connect(a.db)
    rows = con.execute("SELECT sha256, vec FROM embeddings WHERE model=? AND vec IS NOT NULL",
                       (a.model,)).fetchall()
    if not rows:
        sys.exit(f"no embeddings for {a.model}; run `embed.py run` first")
    shas = [r[0] for r in rows]
    M = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float16).reshape(len(rows), -1).astype(np.float32)
    torch, model, proc = load_model(a.model, a.threads)
    with torch.no_grad():
        ti = proc(text=[a.query.lower()], padding="max_length", max_length=64, return_tensors="pt")
        q = torch.nn.functional.normalize(_features(torch, model.get_text_features(**ti)), dim=-1)[0].numpy()
    sims = M @ q
    top = np.argsort(-sims)[: a.limit]
    print(f"{a.query!r}: top {len(top)} of {len(rows)} images ({a.model})")
    for i in top:
        r = con.execute("SELECT a.taken_at, (SELECT dir || '/' || filename FROM occurrences o"
                        " WHERE o.sha256=a.sha256 ORDER BY id LIMIT 1) FROM assets a WHERE sha256=?",
                        (shas[i],)).fetchone()
        print(f"  {sims[i]:.3f}  {(r[0] or '')[:10]:10}  {shas[i][:12]}  {r[1]}")


def _jpeg_size(path: str):
    """(width, height) from a JPEG's header, or None. Never raises."""
    try:
        with open(path, "rb") as f:
            d = f.read(8192)
        i = 2
        while i < len(d) - 9:
            if d[i] != 0xFF or d[i + 1] == 0xFF:
                i += 1
                continue
            if d[i + 1] in (0xC0, 0xC1, 0xC2):
                return int.from_bytes(d[i + 7:i + 9], "big"), int.from_bytes(d[i + 5:i + 7], "big")
            i += 2 + int.from_bytes(d[i + 2:i + 4], "big")
    except OSError:
        pass
    return None


def cmd_serve(a) -> None:
    """The photo app: describe a photo and/or narrow by place, person, album, trip, camera and date."""
    import gzip
    import hashlib
    import json
    import re
    import threading
    from collections import OrderedDict
    from concurrent.futures import ThreadPoolExecutor
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, quote, urlparse

    import numpy as np
    from PIL import Image, ImageOps
    import pillow_heif
    pillow_heif.register_heif_opener()
    Image.MAX_IMAGE_PIXELS = None

    store = Path(a.store)
    seed = Path(a.cache or Path(a.db).resolve().parent / "review")
    # On the Mac mini the estate disk is a USB hard disk: a cold thumbnail costs a ~9 ms seek, so a
    # screenful of 150 takes most of a second. --fast-cache puts the cache on the internal SSD and
    # fills it from the disk's cache (`seed`) on first use, decoding the original only if neither has it.
    cache = Path(a.fast_cache) if a.fast_cache else seed
    con = connect(a.db)
    t0 = time.time()
    # Startup reads every query below in table order and sorts in memory: on a hard disk, walking an
    # index (ORDER BY sha256, a correlated subquery) turns each row into a seek, ~18 s on the Mac mini.
    rows = sorted(con.execute("SELECT sha256, vec FROM embeddings WHERE model=? AND vec IS NOT NULL",
                              (a.model,)).fetchall())
    if not rows:
        sys.exit(f"no embeddings for {a.model}; run `embed.py run` first")
    shas = [r[0] for r in rows]
    N = len(shas)
    index = {s: i for i, s in enumerate(shas)}
    M = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float16).reshape(N, -1).astype(np.float32)
    del rows
    meta = {}
    for sha, ext, taken, lat, lon, desc, people, fav, w, h in con.execute(
            "SELECT sha256, ext, taken_at, lat, lon, description, people, favorited, width, height"
            " FROM assets WHERE kind='image'"):
        if sha in index:
            meta[sha] = {"ext": ext, "taken": taken, "lat": lat, "lon": lon, "desc": desc,
                         "people": [p.strip() for p in (people or "").split(",") if p.strip()],
                         "albums": [], "fav": bool(fav), "w": w, "h": h, "path": None}
    for sha, path, album in con.execute("SELECT sha256, dir || '/' || filename, album FROM occurrences ORDER BY id"):
        if (m := meta.get(sha)) is not None:
            if m["path"] is None:  # the first place the photo was seen
                m["path"] = path
            if album and album not in m["albums"]:
                m["albums"].append(album)

    def optional(q, args=()):  # tables filled by `estate.py exif` / `places.py run`; the page works without them
        try:
            return con.execute(q, args).fetchall()
        except sqlite3.OperationalError:
            return []

    # exif is mostly exiftool's JSON dump (~700 MB for 120k photos); this index holds just the columns
    # read here, so startup scans ~15 MB instead. Built once, then kept up to date by SQLite.
    optional("CREATE INDEX IF NOT EXISTS exif_app ON exif(sha256, camera, lens, focal, focal35, fnumber, exposure, iso)")
    for sha, camera, lens, focal, focal35, fnum, expo, iso in optional(
            "SELECT sha256, camera, lens, focal, focal35, fnumber, exposure, iso FROM exif"):
        if sha in meta:
            meta[sha].update(camera=camera, lens=lens, focal=focal, focal35=focal35, fnumber=fnum,
                             exposure=expo, iso=iso)
    events = {eid: {"title": t, "start": s0, "end": s1, "n": n, "trip": tid}
              for eid, t, s0, s1, n, tid in optional("SELECT id, title, start_local, end_local, n, trip_id FROM events")}
    trips = {tid: {"title": t, "start": s0, "end": s1, "n": n}
             for tid, t, s0, s1, n in optional("SELECT id, title, start_local, end_local, n FROM trips")}
    join = lambda *xs: ", ".join(dict.fromkeys(x for x in xs if x)) or None
    for sha, local, tz, city, locality, admin2, admin1, country, psrc, eid in optional(
            "SELECT sha256, local_time, tz, city, locality, admin2, admin1, country, place_source, event_id"
            " FROM places"):
        if sha in meta:
            ev = events.get(eid) or {}
            meta[sha].update(local=local, tz=tz, place=join(city, admin1, country),
                             country=country or None, region=join(admin1, country) if admin1 else None,
                             city=join(city, admin1, country) if city else None,
                             county=" ".join(x for x in (locality, admin2) if x), place_inferred=psrc == "event",
                             event=eid, event_title=ev.get("title"),
                             trip=ev.get("trip"), trip_title=(trips.get(ev.get("trip")) or {}).get("title"))
    con.close()

    # Thumbnails are upright; the catalog's width/height are not always. The grid needs the real shape.
    dims_file = cache / "thumb-dims.json"
    try:
        dims = json.loads((dims_file if dims_file.exists() else seed / "thumb-dims.json").read_text())
    except (OSError, ValueError):
        dims = {}
    missing = [s for s in shas if s not in dims]
    if missing:
        def size_of(s):
            return _jpeg_size(str(cache / "thumbs" / f"{s}.jpg")) or _jpeg_size(str(seed / "thumbs" / f"{s}.jpg"))
        with ThreadPoolExecutor(16) as ex:
            for s, d in zip(missing, ex.map(size_of, missing)):
                if d and d[0] and d[1]:
                    dims[s] = d
        try:
            cache.mkdir(parents=True, exist_ok=True)
            dims_file.write_text(json.dumps(dims, separators=(",", ":")))
        except OSError:
            pass
    for s in shas:
        m = meta[s]
        tw, th = dims.get(s) or (m["w"] or 4, m["h"] or 3)
        m["ar"] = round(tw / th, 4)
    del dims

    col = lambda f: np.array([f(meta[s]) for s in shas], dtype=object)
    when = col(lambda m: m.get("local") or m["taken"] or "")
    year = np.array([int(w[:4] or 0) for w in when])

    def month_abs(w: str) -> int:
        try:
            y, mo = int(w[:4]), int(w[5:7])
        except ValueError:
            return -1
        return y * 12 + mo - 1 if y > 1900 and 1 <= mo <= 12 else -1

    ym = np.array([month_abs(w) for w in when])
    m0 = int(ym[ym >= 0].min()) if (ym >= 0).any() else 0     # first month with a photo, as year*12+month-1
    nmonths = int(ym.max()) - m0 + 1 if (ym >= 0).any() else 1
    ym = np.where(ym >= 0, ym - m0, -1)

    cols = {  # lower-cased text per photo, for the typed `key:value` substring filters
        "place": col(lambda m: f"{m.get('place') or ''} {m.get('county') or ''}".lower()),
        "camera": col(lambda m: f"{m.get('camera') or ''} {m.get('lens') or ''}".lower()),
        "album": col(lambda m: " | ".join(m["albums"]).lower()),
        "person": col(lambda m: " | ".join(m["people"]).lower()),
    }
    event_of = col(lambda m: m.get("event"))
    trip_of = col(lambda m: m.get("trip"))
    fav = np.array([meta[s]["fav"] for s in shas])
    oldest = np.argsort(when, kind="stable")
    oldest = np.concatenate([oldest[when[oldest] != ""], oldest[when[oldest] == ""]])   # undated last, either way
    newest = np.concatenate([oldest[when[oldest] != ""][::-1], oldest[when[oldest] == ""]])

    # Facets. `single`: one value per photo, choosing several means any of them. `multi`: a photo can
    # have several (albums: any of the chosen; people: all of the chosen, i.e. photos of them together).
    def coded(values):
        labels = sorted({v for v in values if v})
        ix = {v: i for i, v in enumerate(labels)}
        return {"labels": labels, "ix": ix, "codes": np.array([ix.get(v, -1) for v in values], np.int32)}

    single = {k: coded([meta[s].get(k) for s in shas]) for k in ("country", "region", "city", "camera", "lens")}
    single["trip"] = coded([str(t) if t is not None else None for t in trip_of])
    multi = {}
    for key, field in (("album", "albums"), ("person", "people")):
        labels = sorted({v for s in shas for v in meta[s][field]})
        ix = {v: i for i, v in enumerate(labels)}
        multi[key] = {"labels": labels, "ix": ix,
                      "rows": np.array([i for i, s in enumerate(shas) for _ in meta[s][field]], np.int64),
                      "codes": np.array([ix[v] for s in shas for v in meta[s][field]], np.int64)}

    def combine(masks: dict, skip: str | None = None):
        out = np.ones(N, bool)
        for k, m in masks.items():
            if k != skip:
                out &= m
        return out

    def facet_counts(masks: dict) -> dict:
        """Counts per facet value under the current filters; each facet ignores its own selection."""
        ranked = lambda labels, c: sorted(((labels[j], int(n)) for j, n in enumerate(c) if n),
                                          key=lambda x: (-x[1], x[0]))
        out = {}
        for k, f in single.items():
            base = combine(masks, skip=k)
            out[k] = ranked(f["labels"], np.bincount(f["codes"][base & (f["codes"] >= 0)],
                                                     minlength=len(f["labels"])))
        for k, f in multi.items():
            base = combine(masks, skip=None if k == "person" else k)
            out[k] = ranked(f["labels"], np.bincount(f["codes"][base[f["rows"]]], minlength=len(f["labels"])))
        base = combine(masks, skip="date")
        return {"facets": out, "months": np.bincount(ym[base & (ym >= 0)], minlength=nmonths).tolist(),
                "fav": int((combine(masks, skip="fav") & fav).sum()), "matching": int(combine(masks).sum())}

    trip_list = []
    for tid, t in sorted(trips.items(), key=lambda kv: kv[1]["start"] or "", reverse=True):
        ids = np.flatnonzero(trip_of == tid)
        if len(ids):
            ids = ids[np.argsort(when[ids], kind="stable")]
            favs = ids[fav[ids]]
            cover = int(favs[len(favs) // 2]) if len(favs) else int(ids[len(ids) // 2])
            trip_list.append({"id": tid, "title": t["title"], "start": t["start"], "end": t["end"],
                              "n": int(len(ids)), "cover": shas[cover]})
    info = {"total": N, "model": a.model, "m0": m0, "months": nmonths, "trips": trip_list, **facet_counts({})}

    # Importing PyTorch and loading the text encoder is most of the startup time, and only typed
    # queries need it, so it loads while the page, filters and thumbnails are already being served.
    encoder, encoder_ready, model_lock = {}, threading.Event(), threading.Lock()

    def load_encoder():
        t = time.time()
        import torch
        from transformers import AutoModel, AutoTokenizer
        torch.set_num_threads(a.threads)
        encoder.update(torch=torch, model=AutoModel.from_pretrained(a.model, dtype=torch.float32).eval(),
                       tok=AutoTokenizer.from_pretrained(a.model))
        encoder_ready.set()
        text_vec("warm-up")  # the first forward pass is ~1 s slower; take it here, not on a user's query
        print(f"text encoder ready in {time.time() - t:.1f}s", flush=True)

    threading.Thread(target=load_encoder, daemon=True).start()
    print(f"{N} images loaded in {time.time() - t0:.1f}s ({len(events)} events, {len(trips)} trips)", flush=True)
    page = Path(__file__).with_name("search.html")
    # Home-screen app files (manifest, icons, service worker), served from the site root: a service
    # worker only controls pages at or below its own path.
    app_dir = Path(__file__).with_name("app")
    app_types = {".webmanifest": "application/manifest+json", ".js": "text/javascript; charset=utf-8",
                 ".png": "image/png"}
    app_files = {f"/{p.name}": p for p in app_dir.iterdir() if p.suffix in app_types} if app_dir.is_dir() else {}
    original_types = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".heic": "image/heic", ".png": "image/png",
                      ".gif": "image/gif", ".webp": "image/webp"}
    sha_re = re.compile(r"^[0-9a-f]{64}$")
    filter_re = re.compile(r'\b(place|camera|album|person|people|year|date|event|trip|is):("([^"]*)"|(\S+))', re.I)
    lru, lru_lock = OrderedDict(), threading.Lock()

    def remember(key, make):
        """Small cache so paging through one search doesn't redo the text encoding and the ranking."""
        with lru_lock:
            if key in lru:
                lru.move_to_end(key)
                return lru[key]
        val = make()
        with lru_lock:
            lru[key] = val
            while len(lru) > 24:
                lru.popitem(last=False)
        return val

    def text_vec(q: str):
        # average the raw query with a "a photo of ..." prompt: SigLIP was trained on captions
        prompts = [q.lower(), f"a photo of {q.lower()}"]
        encoder_ready.wait()
        torch, model = encoder["torch"], encoder["model"]
        with model_lock, torch.no_grad():
            ti = encoder["tok"](prompts, padding="max_length", max_length=64, return_tensors="pt")
            v = torch.nn.functional.normalize(_features(torch, model.get_text_features(**ti)), dim=-1).mean(0)
        v = v.numpy()
        return v / np.linalg.norm(v)

    def scope_of(kind: str, ident: int):
        got = (events if kind == "event" else trips).get(ident)
        return {"kind": kind, "id": ident, **got} if got else None

    def parse(q: str):
        """Split 'beach place:oregon year:2010' into the free text and a mask for the typed filters."""
        mask, applied, scope = np.ones(N, bool), [], None
        for m in filter_re.finditer(q):
            key, val = m.group(1).lower(), (m.group(3) if m.group(3) is not None else m.group(4))
            v = val.lower().strip()
            if key in ("place", "camera", "album", "person", "people"):
                arr = cols["person" if key == "people" else key]
                mask &= np.fromiter((v in x for x in arr), bool, len(arr))
            elif key == "year":
                lo, _, hi = v.partition("-")
                if lo.isdigit():
                    mask &= (year >= int(lo)) & (year <= int(hi if hi.isdigit() else lo))
            elif key == "date":
                mask &= np.fromiter((w.startswith(v) for w in when), bool, len(when))
            elif key in ("event", "trip") and v.lstrip("-").isdigit():
                mask &= (event_of if key == "event" else trip_of) == int(v)
                scope = scope_of(key, int(v)) or scope
                val = (scope or {}).get("title") or val
            elif key == "is" and v in ("fav", "favorite", "favourite"):
                mask &= fav
            else:
                continue
            applied.append(f"{key}:{val}")
        text = re.sub(r"\s+", " ", filter_re.sub(" ", q)).strip()
        return text, mask, applied, scope

    def month_index(s: str, end: bool = False):
        m = re.fullmatch(r"(\d{4})(?:-(\d{1,2}))?", s or "")
        if not m:
            return None
        return int(m[1]) * 12 + (int(m[2]) if m[2] else (12 if end else 1)) - 1 - m0

    def select(qs: dict):
        """Query string -> one mask per kind of filter, the free text, and the event/trip in scope."""
        masks = {}
        for k, f in single.items():
            if qs.get(k):
                masks[k] = np.isin(f["codes"], [f["ix"][v] for v in qs[k] if v in f["ix"]])
        for k, f in multi.items():
            if qs.get(k):
                each = []
                for v in qs[k]:
                    m = np.zeros(N, bool)
                    if v in f["ix"]:
                        m[f["rows"][f["codes"] == f["ix"][v]]] = True
                    each.append(m)
                masks[k] = (np.logical_and if k == "person" else np.logical_or).reduce(each)
        lo, hi = month_index((qs.get("from") or [""])[0]), month_index((qs.get("to") or [""])[0], end=True)
        if lo is not None or hi is not None:
            masks["date"] = (ym >= (lo if lo is not None else 0)) & (ym <= (hi if hi is not None else nmonths))
        if (qs.get("fav") or ["0"])[0] == "1":
            masks["fav"] = fav
        scope = None
        ev = (qs.get("event") or [""])[0]
        if ev.lstrip("-").isdigit():
            masks["event"] = event_of == int(ev)
            scope = scope_of("event", int(ev))
        elif len(qs.get("trip") or []) == 1 and qs["trip"][0].lstrip("-").isdigit():
            scope = scope_of("trip", int(qs["trip"][0]))
        text, typed, applied, typed_scope = parse((qs.get("q") or [""])[0].strip())
        if applied:
            masks["typed"] = typed
        return masks, text, applied, scope or typed_scope

    def take(order, start: int, limit: int, dedupe: bool):
        """One page from a ranked list, skipping copies of a shot just shown (Google + iCloud, bursts)."""
        kept, hidden, pos = [], 0, start
        while pos < len(order) and len(kept) < limit:
            i = int(order[pos])
            pos += 1
            if dedupe and kept and float(np.max(M[kept[-8:]] @ M[i])) > 0.97:
                hidden += 1
                continue
            kept.append(i)
        return [dict(sha=shas[i], **meta[shas[i]]) for i in kept], hidden, pos if pos < len(order) else None

    def image(sha: str, kind: str, side: int) -> bytes | None:
        dest = cache / kind / f"{sha}.jpg"
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            if cache != seed and (seed / kind / f"{sha}.jpg").exists():
                body = (seed / kind / f"{sha}.jpg").read_bytes()
                tmp = dest.with_suffix(f".{threading.get_ident()}.tmp")
                tmp.write_bytes(body)
                os.replace(tmp, dest)
                return body
            m = meta[sha]
            try:
                with Image.open(store / sha[:2] / sha[2:4] / f"{sha}{m['ext']}") as im:
                    im.draft("RGB", (side, side))
                    im = ImageOps.exif_transpose(im).convert("RGB")
                    im.thumbnail((side, side))
                    tmp = dest.with_suffix(f".{threading.get_ident()}.tmp")
                    im.save(tmp, "JPEG", quality=90 if side > 2000 else 82)
                    os.replace(tmp, dest)
            except Exception:
                return None
        return dest.read_bytes()

    # Every search, page of results, and photo opened or shared from one, one JSON object per line.
    # This is the raw material for choosing evaluation queries from real use; nothing reads it back.
    qlog = Path(a.query_log or Path(a.db).resolve().parent / "query-log.jsonl")
    qlog_lock = threading.Lock()
    unlogged = {"cursor", "limit", "dedupe", "ev", "sha", "pos"}

    def record(req, ev: str, qs: dict, **fields):
        line = {"ts": now(), "ev": ev,
                "params": {k: v if len(v) > 1 else v[0] for k, v in qs.items() if k not in unlogged},
                **{k: v for k, v in fields.items() if v is not None},
                "who": req.headers.get("Tailscale-User-Login"), "ua": req.headers.get("User-Agent")}
        try:
            with qlog_lock, qlog.open("a") as f:
                f.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError as e:  # never fail a search because the log can't be written
            print(f"query log: {e}", flush=True)

    class Handler(BaseHTTPRequestHandler):
        # HTTP/1.1 keeps connections open: `tailscale serve` (and browsers on plain http) reuse them
        # instead of paying a new connection and thread per request. Idle ones close after `timeout`.
        protocol_version = "HTTP/1.1"
        timeout = 60
        disable_nagle_algorithm = True  # headers and body go out as separate writes; don't hold the second

        def log_message(self, *args):
            pass

        def send(self, code: int, body: bytes, ctype: str, cache: str = "no-store", etag: str | None = None):
            if etag and self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", cache)
                return self.end_headers()
            text = ctype.startswith(("application/json", "text/"))
            gzipped = text and len(body) > 1024 and "gzip" in self.headers.get("Accept-Encoding", "")
            if gzipped:  # JSON and the page shrink 4-6x; JPEGs don't
                body = gzip.compress(body, 5)
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            if etag:
                self.send_header("ETag", etag)
            if text:
                self.send_header("Vary", "Accept-Encoding")
            if gzipped:
                self.send_header("Content-Encoding", "gzip")
            for k, val in getattr(self, "extra_headers", {}).items():
                self.send_header(k, val)
            self.extra_headers = {}
            self.end_headers()
            self.wfile.write(body)

        def json(self, body, code: int = 200):
            self.send(code, json.dumps(body, separators=(",", ":")).encode(), "application/json")

        def do_GET(self):
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            one = lambda k, d="": (qs.get(k) or [d])[0]
            try:
                if u.path in ("/", "/index.html"):  # revalidated each load, so edits show up at once
                    body = page.read_bytes()
                    return self.send(200, body, "text/html; charset=utf-8", "no-cache",
                                     etag=f'"{hashlib.md5(body).hexdigest()}"')
                if u.path in ("/api/search", "/api/similar"):
                    t = time.time()
                    limit = max(1, min(int(one("limit", "120")), 500))
                    start = max(0, int(one("cursor", "0")))
                    masks, text, applied, scope = select(qs)
                    mask = combine(masks)
                    sha = one("sha")
                    if u.path == "/api/similar" and sha not in index:
                        return self.json({"error": "unknown photo"}, 404)
                    sort = one("sort") or ("old" if scope else "new")
                    key = (u.path, sha, text, sort, tuple(sorted((k, tuple(v)) for k, v in qs.items()
                                                                 if k not in ("cursor", "limit", "dedupe"))))

                    def ranked():
                        if u.path == "/api/similar":
                            vec = M[index[sha]]
                        elif text:
                            vec = remember(("text", text), lambda: text_vec(text))
                        else:  # nothing to rank by: what the filters select, by time
                            by_time = oldest if sort == "old" else newest
                            return by_time[mask[by_time]]
                        sims = np.where(mask, M @ vec, -np.inf)
                        if u.path == "/api/similar":
                            sims[index[sha]] = -np.inf
                        return np.argsort(-sims)[: int(np.isfinite(sims).sum())]

                    order = remember(key, ranked)
                    results, hidden, nxt = take(order, start, limit, one("dedupe", "1") == "1")
                    record(self, "similar" if u.path == "/api/similar" else "search", qs, sha=one("sha") or None, text=text,
                           filters=applied, cursor=start, matching=int(len(order)),
                           ms=round((time.time() - t) * 1000),
                           top=[r["sha"] for r in results[:20]] if start == 0 else None)
                    return self.json({"results": results, "next": nxt, "hidden": hidden, "total": N,
                                      "matching": int(len(order)), "text": text, "filters": applied,
                                      "scope": scope, "sort": sort, "ms": round((time.time() - t) * 1000)})
                if u.path == "/api/log":  # the page reports photos opened or shared from a result list
                    if one("ev") in ("open", "share") and sha_re.match(one("sha")):
                        pos = one("pos")
                        record(self, one("ev"), qs, sha=one("sha"), pos=int(pos) if pos.isdigit() else None)
                    return self.send(204, b"", "text/plain")
                if u.path == "/api/facets":
                    return self.json(facet_counts(select(qs)[0]))
                if u.path == "/api/info":
                    return self.json(info)
                if u.path in app_files:
                    body = app_files[u.path].read_bytes()
                    return self.send(200, body, app_types[app_files[u.path].suffix], "no-cache",
                                     etag=f'"{hashlib.md5(body).hexdigest()}"')
                if u.path.startswith("/original/"):  # the file as stored, for Save to Photos / share
                    sha = u.path[len("/original/"):]
                    m = meta.get(sha) if sha_re.match(sha) else None
                    src = m and store / sha[:2] / sha[2:4] / f"{sha}{m['ext']}"
                    if not m or m["ext"] not in original_types or not src.exists():
                        return self.send(404, b"", "text/plain")
                    name = (m["path"] or "").rsplit("/", 1)[-1] or f"{sha[:12]}{m['ext']}"
                    self.extra_headers = {"Content-Disposition": f"inline; filename*=UTF-8''{quote(name)}"}
                    return self.send(200, src.read_bytes(), original_types[m["ext"]], "public, max-age=2592000, immutable")
                # /large/ is for big high-density displays, where a 1600 px preview is visibly soft
                for prefix, kind, side in (("/thumb/", "thumbs", 360), ("/preview/", "previews", 1600),
                                           ("/large/", "large", 4096)):
                    if u.path.startswith(prefix):
                        sha = u.path[len(prefix):]
                        if sha_re.match(sha) and sha in meta and (body := image(sha, kind, side)):
                            # named by the original's hash, so a browser never needs to ask again
                            return self.send(200, body, "image/jpeg", "public, max-age=2592000, immutable")
                        return self.send(404, b"", "text/plain")
                self.send(404, b"not found", "text/plain")
            except (BrokenPipeError, ConnectionResetError):
                pass

    def pregenerate():  # every thumbnail once, so scrolling results never waits on HEIC decoding
        (cache / "thumbs").mkdir(parents=True, exist_ok=True)
        have = {p.name[:64] for p in (cache / "thumbs").iterdir()}
        if cache != seed and (seed / "thumbs").is_dir():  # copy what the disk's cache has, newest first
            seeded = {p.name[:64] for p in (seed / "thumbs").iterdir()} - have
            copy = [s for s in (shas[i] for i in newest) if s in seeded]
            if copy:
                print(f"copying {len(copy)} thumbnails to {cache}", flush=True)
                t = time.time()
                for s in copy:
                    image(s, "thumbs", 360)
                print(f"thumbnails: {len(copy)} copied in {time.time() - t:.0f}s", flush=True)
            have |= seeded
        todo = [(str(store / s[:2] / s[2:4] / f"{s}{meta[s]['ext']}"), str(cache / "thumbs" / f"{s}.jpg"), 360)
                for s in shas if s not in have]
        if not todo:
            return
        print(f"making {len(todo)} thumbnails in the background", flush=True)
        t = time.time()
        import multiprocessing as mp  # forkserver: no fork() of a process that holds torch threads
        with mp.get_context("forkserver").Pool(max(1, (os.cpu_count() or 4) - 2)) as pool:
            ok = sum(pool.imap_unordered(_thumb, todo, chunksize=32))
        print(f"thumbnails: {ok} made in {(time.time() - t) / 60:.0f} min", flush=True)

    if not a.no_pregenerate:
        threading.Thread(target=pregenerate, daemon=True).start()
    class Server(ThreadingHTTPServer):
        # The default listen backlog is 5. Behind `tailscale serve`, every HTTP/2 stream becomes its own
        # connection here, so a screenful of thumbnails overflows it and the proxy answers 502.
        request_queue_size = 256
        daemon_threads = True

    srv = Server(("127.0.0.1", a.port), Handler)
    print(f"Open http://127.0.0.1:{a.port}/  (Ctrl-C to stop)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def cmd_stats(a) -> None:
    con = connect(a.db)
    images = con.execute("SELECT count(*) FROM assets WHERE kind='image'").fetchone()[0]
    print(f"images in catalog: {images}")
    for m, ok, bad in con.execute("SELECT model, count(vec), count(error) FROM embeddings GROUP BY model"):
        print(f"  {m}: {ok} embedded, {bad} undecodable")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default="estate.db")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--threads", type=int, default=8, help="PyTorch threads (8 measured fastest)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("run", help="embed images that have no embedding for --model yet")
    s.add_argument("--store", required=True)
    s.add_argument("--workers", type=int, default=3, help="decoder processes, in parallel with the model")
    s.add_argument("--batch", type=int, default=16)
    s.add_argument("--limit", type=int, default=0)
    s.set_defaults(fn=cmd_run)
    s = sub.add_parser("search", help="semantic search")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=15)
    s.set_defaults(fn=cmd_search)
    sub.add_parser("stats").set_defaults(fn=cmd_stats)
    s = sub.add_parser("serve", help="local search page")
    s.add_argument("--store", required=True)
    s.add_argument("--port", type=int, default=8766)
    s.add_argument("--cache", help="thumbnail cache dir (default: <db dir>/review, shared with estate.py review)")
    s.add_argument("--fast-cache", help="cache dir on a faster disk (e.g. the internal SSD), filled from --cache")
    s.add_argument("--no-pregenerate", action="store_true", help="don't build missing thumbnails in the background")
    s.add_argument("--query-log", help="where to record searches and photos opened (default: <db dir>/query-log.jsonl)")
    s.set_defaults(fn=cmd_serve)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
