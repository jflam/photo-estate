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
  uv run embed.py serve --demo     # the photo app on the ~100 public-domain photos in demo/

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
# A typed search shows only photos that match it, not the whole library in ranked order. Measured
# for siglip2-base-patch16-256 with the two-prompt average in text_vec (cosine between query and
# photo), on the demo and on a 100k-photo library:
#   - a photo of the thing scores 0.085-0.16; unrelated photos stay under ~0.07, so a search for
#     something that isn't there ("kangaroo" in the demo: best 0.069) finds nothing;
#   - in a big library scores fall smoothly with no gap, and ~0.04 below the best match is where
#     results drift from the thing asked for to its surroundings ("latte art" -> plain coffee,
#     "unicorn" -> horses). Narrower drops real matches from a small library.
MATCH_FLOOR = 0.085
MATCH_WINDOW = 0.04
# Shown best first, a search's matches are no longer in order of how well they match, so the loose ones at
# the end of the list are no longer out of the way: the best of them rise to the top. Measured on "mountain"
# in a 108k-photo library (3,614 matches): photos of slot canyons and desert rock are 1-2% of the matches
# within 0.025 of the best, and 7-15% of those beyond it; best first, half of the first 60 photos came from
# the loosest third. So best first keeps only what is within this of the best match.
BEST_WINDOW = 0.025
# First names that are also everyday words: read as a person only when capitalized ("Sunny") or "Sunny's"
COMMON_WORDS = set("""sunny lily rose rosie daisy iris violet ivy holly heather hazel jasmine poppy ruby amber pearl
    jade crystal coral olive berry autumn summer winter april may june august dawn eve faith hope joy grace
    honey candy ginger pepper cookie buddy rocky max jack bill mark will pat sue bob rob rod guy ray art
    frank earl duke king prince queen bishop hunter chase page brook river sky storm rain snow forest
    glen dale cliff rocco sandy dusty misty penny chip bud bunny kitty angel star sterling destiny""".split())
# Countries and states that are also everyday words: read as a place only when capitalized
COMMON_PLACES = {"china", "turkey", "chad", "jordan", "georgia", "guinea", "japan", "chile", "cuba", "wales", "jersey"}

# Screenshots, told from what the catalog already holds, as the iPhone app does (ios/.../Screenshots.swift): a
# camera always writes its make and model and a screenshot never has one, so a screenshot is a photo with no
# camera that is exactly a phone or tablet screen's size (either way round), or whose name says it is one.
# Camera photos that lost their EXIF come back at 4:3 or 16:9, which are also iPad and old Android sizes; a
# real screenshot of those is a PNG, so for JPEG and HEIC those two shapes don't count.
SCREEN_SIZES = {
    (320, 480), (640, 960), (640, 1136), (750, 1334), (828, 1792), (1080, 1920), (1242, 2208), (1125, 2436),
    (1242, 2688), (1080, 2340), (1170, 2532), (1284, 2778), (1179, 2556), (1290, 2796), (1206, 2622),
    (1320, 2868), (1260, 2736),
    (768, 1024), (1536, 2048), (1668, 2224), (1668, 2388), (2048, 2732), (1620, 2160), (1640, 2360),
    (1488, 2266), (2064, 2752), (1668, 2420),
    (720, 1280), (1080, 2160), (1080, 2220), (1080, 2280), (1080, 2316), (1080, 2400), (1440, 2560),
    (1440, 2960), (1440, 3040), (1440, 3120), (1440, 3200),
}


def is_screenshot(m: dict) -> bool:
    if m.get("camera") or m.get("lens"):
        return False
    path = (m.get("path") or "").lower()
    if "screenshot" in path or "screen shot" in path:
        return True
    w, h = m.get("w") or 0, m.get("h") or 0
    a, b = min(w, h), max(w, h)
    return (a, b) in SCREEN_SIZES and ((m.get("ext") or "").lower() == ".png" or (a * 4 != b * 3 and a * 16 != b * 9))
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".webp", ".tif", ".tiff", ".avif"}  # decodable stills
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
    # Each lookup below walks the embeddings index: over the whole library that is ~10 s when it is not
    # cached and the catalog is on a hard disk, so an incremental run (--after-rowid) looks up only new assets.
    todo = con.execute(
        "SELECT a.sha256, a.ext FROM assets a WHERE a.kind='image' AND a.rowid > ? AND NOT EXISTS"
        " (SELECT 1 FROM embeddings e WHERE e.sha256=a.sha256 AND e.model=?) ORDER BY a.sha256",
        (a.after_rowid or 0, a.model)).fetchall()
    skipped = [(s, e) for s, e in todo if e in SKIP_EXT]
    todo = [(s, e) for s, e in todo if e not in SKIP_EXT][: a.limit or None]
    done = "" if a.after_rowid is not None else \
        f"{con.execute('SELECT count(*) FROM embeddings WHERE model=?', (a.model,)).fetchone()[0]} already embedded, "
    print(f"{a.model}: {done}{len(todo)} to do ({len(skipped)} RAW files skipped)", flush=True)
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


def _has_photos(db: str | None, model: str) -> bool:
    if not db or not Path(db).exists():
        return False
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
        try:
            return c.execute("SELECT 1 FROM embeddings WHERE model=? AND vec IS NOT NULL LIMIT 1", (model,)).fetchone() is not None
        finally:
            c.close()
    except sqlite3.Error:
        return False


def _library_or_setup(a) -> bool:
    """Where the library is: --root, else --store with --db (as before), else the one chosen on the setup
    page last time. With photos in it, fill in a.db/a.store and return True. Without (a first run), serve
    the setup page instead (intake/server.py), which becomes the full app once the first photos are in;
    return False when it stops."""
    import atexit
    import shutil
    import tempfile
    from intake.api import Library, saved_root
    from intake.server import run_setup
    explicit_db = a.db != "estate.db"
    if a.sandbox and not a.root:   # practice: a scratch library, gone when the app stops
        a.root = tempfile.mkdtemp(prefix="photo-estate-sandbox-")
    if a.sandbox and Path(a.root).name.startswith("photo-estate-sandbox-"):
        import signal
        atexit.register(shutil.rmtree, a.root, ignore_errors=True)
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))   # stopped by a service manager: still clean up
    root = Path(a.root).expanduser().resolve() if a.root else \
        Path(a.db).resolve().parent if (a.store or explicit_db) else saved_root()
    db = a.db if explicit_db or not root else str(root / "estate.db")
    store = a.store or (str(root / "store") if root else None)
    if root and _has_photos(db, a.model):
        a.db, a.store, a.root = db, store, str(root)
        return True
    port = a.port or (8768 if a.sandbox else 8766)

    def app_argv(lib: Library) -> list[str]:
        argv = [sys.executable, str(Path(__file__).resolve()), "--db", lib.db, "--model", a.model,
                "--threads", str(a.threads), "serve", "--root", str(lib.root), "--store", str(lib.store), "--port", str(port)]
        return argv + (["--sandbox"] if a.sandbox else []) + (["--fast-cache", a.fast_cache] if a.fast_cache else [])

    lib = Library(root, db, store, fixed=bool(a.root and not a.sandbox) or bool(a.store) or explicit_db,
                  remember=not a.sandbox)
    run_setup(lib, port, app_argv, practice=a.sandbox)
    return False


def cmd_serve(a) -> None:
    """The photo app: describe a photo and/or narrow by place, person, album, trip, camera and date."""
    if not a.demo and not _library_or_setup(a):
        return
    import gzip
    import hashlib
    import json
    import re
    import threading
    from collections import Counter, OrderedDict
    from concurrent.futures import ThreadPoolExecutor
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, quote, unquote, urlparse

    from changes import Changes, hidden_shas
    from scores import best as best_score, best_first, load as load_scorers
    from stacks import group as stack_group, seconds as stack_seconds, tops as stack_tops
    from estate_mcp import Server as MCPServer
    from estate import facts_of as estate_facts
    from sync import Feed
    from upload import IngestJob, save_upload, staging_dir
    import numpy as np
    from PIL import Image, ImageOps
    import pillow_heif
    pillow_heif.register_heif_opener()
    Image.MAX_IMAGE_PIXELS = None

    if a.demo:  # the committed demo estate, served from a scratch copy so the repo is never written to
        import atexit
        import shutil
        import tempfile
        demo = Path(__file__).resolve().parent / "demo"
        if not (demo / "estate.db").exists():
            sys.exit(f"no demo estate in {demo}; build it with `uv run tools/make_demo.py`")
        scratch = Path(tempfile.mkdtemp(prefix="photo-estate-demo-"))
        atexit.register(shutil.rmtree, scratch, ignore_errors=True)
        shutil.copyfile(demo / "estate.db", scratch / "estate.db")
        a.db, a.store, a.cache, a.fast_cache = str(scratch / "estate.db"), str(demo / "store"), None, None
        a.query_log = a.query_log or os.devnull
    store = Path(a.store)
    # Phone uploads land in a plain folder that `estate.py ingest` reads like any other source. The demo
    # takes none unless --inbox says where; otherwise the inbox sits beside the catalog.
    inbox = Path(a.inbox) if a.inbox else None if a.demo else Path(a.db).resolve().parent / "iphone"
    ingest = (IngestJob(Path(a.ingest_script or Path(__file__).with_name("ingest-iphone.sh")), Path(a.db).resolve().parent,
                        a.db, store, inbox, quiet=a.ingest_quiet, max_wait=a.ingest_max_wait) if inbox else None)
    seed = Path(a.cache or Path(a.db).resolve().parent / "review")
    # On the Mac mini the estate disk is a USB hard disk: a cold thumbnail costs a ~9 ms seek, so a
    # screenful of 150 takes most of a second. --fast-cache puts the cache on the internal SSD and
    # fills it from the disk's cache (`seed`) on first use, decoding the original only if neither has it.
    cache = Path(a.fast_cache) if a.fast_cache else seed
    t0 = time.time()

    def chunks(xs, n=500):
        xs = list(xs)
        return (xs[i:i + n] for i in range(0, len(xs), n))

    def load_state(prev: dict | None = None) -> dict:
        """Everything the app keeps in memory, built from the catalog. Startup runs it once; a finished
        ingest runs it again in the background and swaps the result in (install), so new photos appear
        without a restart.

        With `prev` (what the last load read, its "_raw") it reads only what an ingest can have changed:
        embeddings, assets and occurrences added since (by rowid), the photos those touch, and the small
        or derived tables (places, events, trips, named faces), applying only rows that differ. Photos it
        does not touch keep their per-photo dict, so the cost follows the new photos, not the library.
        Dicts are never changed in place: the old state keeps answering requests until install()."""
        con = connect(a.db)

        def optional(q, args=()):  # tables filled by `estate.py exif` / `places.py run`; the page works without them
            try:
                return con.execute(q, args).fetchall()
            except sqlite3.OperationalError:
                return []

        def where_in(q, shas):     # q's rows for these photos (q ends in "sha256 IN")
            return [r for c in chunks(shas) for r in con.execute(f"{q} ({','.join('?' * len(c))})", c)]

        def read_meta(want):
            """Per-photo dicts from assets, occurrences and exif. `want` None: every image, by scanning
            the tables in order (startup: on a hard disk, walking an index turns each row into a seek,
            ~18 s on the Mac mini); else just those photos, by key."""
            got, tagged = {}, {}
            q_assets = "SELECT sha256, ext, taken_at, lat, lon, description, people, favorited, width, height FROM assets"
            q_occ = "SELECT sha256, dir || '/' || filename, album FROM occurrences"
            q_exif = "SELECT sha256, camera, lens, focal, focal35, fnumber, exposure, iso FROM exif"
            for sha, ext, taken, lat, lon, desc, people, fav, w, h in (
                    con.execute(q_assets + " WHERE kind='image'") if want is None else where_in(q_assets + " WHERE sha256 IN", want)):
                if sha in index_of:
                    tagged[sha] = [p.strip() for p in (people or "").split(",") if p.strip()]
                    got[sha] = {"ext": ext, "taken": taken, "lat": lat, "lon": lon, "desc": desc,
                                "people": list(tagged[sha]), "albums": [], "fav": bool(fav), "w": w, "h": h, "path": None}
            occ = con.execute(q_occ + " ORDER BY id") if want is None else sorted(
                where_in("SELECT id, sha256, dir || '/' || filename, album FROM occurrences WHERE sha256 IN", got))
            for *_, sha, path, album in occ:
                if (m := got.get(sha)) is not None:
                    if m["path"] is None:  # the first place the photo was seen
                        m["path"] = path
                    if album and album not in m["albums"]:
                        m["albums"].append(album)
            # exif is mostly exiftool's JSON dump (~700 MB for 120k photos); this index holds just the columns
            # read here, so startup scans ~15 MB instead. Built once, then kept up to date by SQLite.
            optional("CREATE INDEX IF NOT EXISTS exif_app ON exif(sha256, camera, lens, focal, focal35, fnumber, exposure, iso)")
            try:
                ex = optional(q_exif) if want is None else where_in(q_exif + " WHERE sha256 IN", got)
            except sqlite3.OperationalError:
                ex = []
            for sha, camera, lens, focal, focal35, fnum, expo, iso in ex:
                if sha in got:
                    got[sha].update(camera=camera, lens=lens, focal=focal, focal35=focal35, fnumber=fnum,
                                    exposure=expo, iso=iso)
            return got, tagged

        q_emb = "SELECT rowid, sha256, vec FROM embeddings WHERE model=? AND vec IS NOT NULL"
        marks = {k: con.execute(q).fetchone()[0] or 0 for k, q in (
            ("emb", "SELECT max(rowid) FROM embeddings"), ("asset", "SELECT max(rowid) FROM assets"),
            ("occ", "SELECT max(id) FROM occurrences"))}
        if prev is None:
            images = {r[0] for r in con.execute("SELECT sha256 FROM assets WHERE kind='image'")}
            rows = sorted((sha, vec) for rid, sha, vec in con.execute(q_emb + " AND rowid <= ?", (a.model, marks["emb"])))
            orphans = {sha: vec for sha, vec in rows if sha not in images}   # embedded on upload, not ingested yet
            rows = [r for r in rows if r[0] in images]
            if not rows:
                raise RuntimeError(f"no embeddings for {a.model}; run `embed.py run` first")
            shas = [r[0] for r in rows]
            index_of = {s: i for i, s in enumerate(shas)}
            M = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float16).reshape(len(shas), -1).astype(np.float32)
            del rows
            meta, tagged = read_meta(None)
            fresh = set(meta)
        else:
            old = prev["index"]
            vecs = dict(prev["orphans"])            # embeddings made before their photo was ingested
            vecs.update((sha, vec) for _, sha, vec in con.execute(q_emb + " AND rowid > ? AND rowid <= ?",
                                                                  (a.model, prev["marks"]["emb"], marks["emb"])))
            touched = {r[0] for r in con.execute("SELECT sha256 FROM assets WHERE rowid > ? AND rowid <= ?",
                                                 (prev["marks"]["asset"], marks["asset"]))}
            touched |= {r[0] for r in con.execute("SELECT sha256 FROM occurrences WHERE id > ? AND id <= ?",
                                                  (prev["marks"]["occ"], marks["occ"]))}
            images = {r[0] for r in where_in("SELECT sha256 FROM assets WHERE kind='image' AND sha256 IN", vecs.keys() | touched)}
            orphans = {sha: v for sha, v in vecs.items() if sha not in images and sha not in old}
            added = sorted(sha for sha in vecs if sha in images and sha not in old)
            replaced = [sha for sha in vecs if sha in old]
            if added or replaced:
                shas = sorted(prev["shas"] + added)
                index_of = {s: i for i, s in enumerate(shas)}
                new = np.frombuffer(b"".join(vecs[s] for s in added + replaced), dtype=np.float16)
                new = new.reshape(len(added) + len(replaced), -1).astype(np.float32)
                M = np.concatenate([prev["M"], new[:len(added)]])
                M[[old[s] for s in replaced]] = new[len(added):]
                pos = {s: len(old) + i for i, s in enumerate(added)}
                M = M[[old[s] if s in old else pos[s] for s in shas]] if added else M
            else:
                shas, index_of, M = prev["shas"], old, prev["M"]
            fresh = {sha for sha in touched | set(added) if sha in index_of}
            got, tagged_new = read_meta(fresh)
            meta = dict(prev["meta"])
            meta.update(got)
            tagged = dict(prev["tagged"])
            tagged.update(tagged_new)
        N, index = len(shas), index_of

        events = {eid: {"title": t, "start": s0, "end": s1, "n": n, "trip": tid}
                  for eid, t, s0, s1, n, tid in optional("SELECT id, title, start_local, end_local, n, trip_id FROM events")}
        trips = {tid: {"title": t, "start": s0, "end": s1, "n": n}
                 for tid, t, s0, s1, n in optional("SELECT id, title, start_local, end_local, n FROM trips")}

        def own(sha):              # this load's own copy of a photo's dict, to change
            if sha not in fresh:
                meta[sha] = dict(meta[sha], people=list(meta[sha]["people"]))
                fresh.add(sha)
            return meta[sha]

        join = lambda *xs: ", ".join(dict.fromkeys(x for x in xs if x)) or None
        place_key = {}
        old_keys = prev["place_key"] if prev else {}
        for sha, local, tz, city, locality, admin2, admin1, country, psrc, eid in optional(
                "SELECT sha256, local_time, tz, city, locality, admin2, admin1, country, place_source, event_id"
                " FROM places"):
            if sha in meta:
                ev = events.get(eid) or {}
                trip_title = (trips.get(ev.get("trip")) or {}).get("title")
                key = place_key[sha] = (local, tz, city, locality, admin2, admin1, country, psrc, eid,
                                        ev.get("title"), ev.get("trip"), trip_title)
                if sha in fresh or old_keys.get(sha) != key:
                    own(sha).update(local=local, tz=tz, place=join(city, admin1, country),
                                    country=country or None, region=join(admin1, country) if admin1 else None,
                                    city=join(city, admin1, country) if city else None,
                                    county=" ".join(x for x in (locality, admin2) if x), place_inferred=psrc == "event",
                                    event=eid, event_title=ev.get("title"), trip=ev.get("trip"), trip_title=trip_title)
        for sha in old_keys.keys() - place_key.keys():   # no longer placed (never, in practice)
            for k in ("local", "tz", "place", "country", "region", "city", "county", "place_inferred", "event",
                      "event_title", "trip", "trip_title"):
                own(sha).pop(k, None)
        # Faces named on the People page (faces.py) count as people in the photo, alongside Google's tags.
        face_names = {}
        for sha, name in optional("SELECT DISTINCT f.sha256, p.name FROM faces f JOIN persons p ON p.id=f.person_id"):
            if sha in meta:
                face_names.setdefault(sha, []).append(name)
        old_names = prev["face_names"] if prev else {}
        for sha in (face_names.keys() | old_names.keys()) if prev else face_names:
            if sha in fresh or face_names.get(sha) != old_names.get(sha):
                own(sha)["people"] = list(dict.fromkeys(tagged[sha] + face_names.get(sha, [])))
        has_faces = bool(optional("SELECT 1 FROM faces LIMIT 1"))
        # A favourite set in the app (set_favorite) takes the place of what the photo's source said.
        for sha, on in optional("SELECT sha256, fav FROM favorites"):
            if sha in meta and meta[sha]["fav"] != bool(on):
                own(sha)["fav"] = bool(on)
        hidden = hidden_shas(con)   # photos a change set hides (changes.py): in the catalog, in no view
        scorers = load_scorers(con, a.model)   # what "Best first" goes by (scores.py); none in a catalog without them
        con.close()
        _raw = {"shas": shas, "index": index, "M": M, "meta": meta, "tagged": tagged, "orphans": orphans,
                "marks": marks, "place_key": place_key, "face_names": face_names}

        # Thumbnails are upright; the catalog's width/height are not always. The grid needs the real shape.
        # Worked out for the photos this load read or changed, and for those that had no thumbnail last time.
        need = [s for s in shas if "ar" not in meta[s] or s in (prev or {}).get("no_dims", ())]
        dims_file = cache / "thumb-dims.json"
        dims = {}
        if need:
            try:
                dims = json.loads((dims_file if dims_file.exists() else seed / "thumb-dims.json").read_text())
            except (OSError, ValueError):
                pass
        missing = [s for s in need if s not in dims]
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
        for s in need:
            m = own(s)
            tw, th = dims.get(s) or (m["w"] or 4, m["h"] or 3)
            m["ar"] = round(tw / th, 4)
        _raw["no_dims"] = {s for s in need if s not in dims}
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
        shot = np.array([is_screenshot(meta[s]) for s in shas], bool)
        visible = np.array([s not in hidden for s in shas], bool)
        ars = np.array([meta[s]["ar"] for s in shas], np.float32)
        oldest = np.argsort(when, kind="stable")
        oldest = np.concatenate([oldest[when[oldest] != ""], oldest[when[oldest] == ""]])   # undated last, either way
        newest = np.concatenate([oldest[when[oldest] != ""][::-1], oldest[when[oldest] == ""]])
        score = best_score(M, scorers)  # what "Best first" sorts by, larger is better; None with no scorers
        best = best_first(M, scorers)   # every photo, best first; None when the catalog has no scorers
        rank = None                     # each photo's place in that order (0 is the best)
        if best is not None:
            rank = np.empty(len(shas), np.int64)
            rank[best] = np.arange(len(shas))
        # Near-identical shots taken together are one stack (stacks.py): the best one shows, the rest are behind it.
        # By the instant it was taken (`taken`), not the local time a view sorts by: two shots a second apart
        # must not come out hours apart because only one of them has a place, and so a time zone.
        stack_of = stack_group(np.array([stack_seconds(meta[x]["taken"]) for x in shas]), M, visible)
        top, stack_n = stack_tops(stack_of, rank)
        dup = ~top                      # behind another photo of its stack

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

        trip_list = []
        for tid, t in sorted(trips.items(), key=lambda kv: kv[1]["start"] or "", reverse=True):
            ids = np.flatnonzero((trip_of == tid) & visible)
            if len(ids):
                ids = ids[np.argsort(when[ids], kind="stable")]
                favs = ids[fav[ids]]
                pick = favs if len(favs) else ids   # a favourite if there is one: the best of them, or the middle one
                cover = int(pick[np.argmin(rank[pick])]) if rank is not None else int(pick[len(pick) // 2])
                trip_list.append({"id": tid, "title": t["title"], "start": t["start"], "end": t["end"],
                                  "n": int(len(ids)), "cover": shas[cover]})
        people_ix = multi["person"]["ix"]
        firsts = {}
        for n in people_ix:
            firsts.setdefault(n.split()[0].lower(), []).append(n)
        who = {f: ns[0] for f, ns in firsts.items() if len(ns) == 1}
        who.update({n.lower(): n for n in people_ix})
        where, cities = {}, set()
        for k in ("city", "region", "country"):
            for label in single[k]["labels"]:
                name = label.split(",")[0].strip()
                where.setdefault(name.lower(), name)
                if k == "city":
                    cities.add(name.lower())
        for k in [k for k in where if k in who]:   # a person first
            del where[k]
        try:
            lower_words = {w for w in Path("/usr/share/dict/words").read_text().split() if w.islower()}
        except OSError:
            lower_words = set()
        common_place = {k for k in where if " " not in k and (k in COMMON_WORDS or k in COMMON_PLACES
                                                              or (k in cities and k in lower_words))}

        def alt(keys):
            return "|".join(re.escape(k) for k in sorted(keys, key=len, reverse=True))
        word_re = re.compile(r"(?<![\w'’])(?:(?P<who>" + (alt(who) or "(?!)") + r")|(?P<where>" + (alt(where) or "(?!)") +
                             r"))(['’]s)?(?![\w'’])", re.I)
        return {k: v for k, v in locals().items() if k in STATE or k == "_raw"}

    def combine(masks: dict, skip: str | None = None):
        # Hidden photos are in no view, unless the hidden ones are what was asked for (`hidden=1`).
        out = np.ones(N, bool) if "hidden" in masks else visible.copy()
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

    # The names load_state() produces. Handlers read them as plain variables; install() rebinds them all
    # at once while no request is being served, so a search never sees half of an old and half of a new catalog.
    STATE = ('shas', 'N', 'index', 'M', 'meta', 'events', 'trips', 'has_faces', 'when', 'year', 'ym', 'm0', 'nmonths', 'cols', 'event_of', 'trip_of', 'fav', 'shot', 'ars', 'oldest', 'newest', 'single', 'multi', 'trip_list', 'people_ix', 'who', 'where', 'cities', 'common_place', 'word_re', 'visible', 'best', 'rank', 'stack_of', 'stack_n', 'dup', 'score')
    shas, N, index, M, meta, events, trips, has_faces, when, year, ym, m0, nmonths, cols, event_of, trip_of, fav, shot, ars, oldest, newest, single, multi, trip_list, people_ix, who, where, cities, common_place, word_re, visible, best, rank, stack_of, stack_n, dup, score, info = (None,) * 38

    loaded = {}   # what the installed state was read from: the next reload starts from it

    def install(st: dict) -> None:
        loaded["raw"] = st["_raw"]
        nonlocal shas, N, index, M, meta, events, trips, has_faces, when, year, ym, m0, nmonths, cols, event_of, trip_of, fav, shot, ars, oldest, newest, single, multi, trip_list, people_ix, who, where, cities, common_place, word_re, visible, best, rank, stack_of, stack_n, dup, score, info
        shas, N, index, M, meta, events, trips, has_faces, when, year, ym, m0, nmonths, cols, event_of, trip_of, fav, shot, ars, oldest, newest, single, multi, trip_list, people_ix, who, where, cities, common_place, word_re, visible, best, rank, stack_of, stack_n, dup, score = (st[k] for k in STATE)
        info = {"total": int(visible.sum()), "model": a.model, "m0": m0, "months": nmonths, "trips": trip_list,
                "demo": bool(a.demo), "faces": has_faces, "best": best is not None, **facet_counts({})}

    class Gate:
        """Requests share it; install() takes it alone. Writers wait first, so a stream of requests can't starve a reload."""
        def __init__(self):
            self.cv, self.readers, self.writing, self.waiting = threading.Condition(), 0, False, 0

        def __enter__(self):
            with self.cv:
                while self.writing or self.waiting:
                    self.cv.wait()
                self.readers += 1

        def __exit__(self, *exc):
            with self.cv:
                self.readers -= 1
                self.cv.notify_all()

        def swap(self, fn):
            with self.cv:
                self.waiting += 1
                while self.readers or self.writing:
                    self.cv.wait()
                self.waiting -= 1
                self.writing = True
            try:
                fn()
            finally:
                with self.cv:
                    self.writing = False
                    self.cv.notify_all()

    gate = Gate()
    try:
        install(load_state())
    except RuntimeError as e:
        sys.exit(str(e))
    print(f"{N} images loaded in {time.time() - t0:.1f}s ({len(events)} events, {len(trips)} trips)", flush=True)


    # The People page (/people): who each face is, and the questions worth asking. It's faces.py's,
    # mounted here so naming works wherever the app does. Loads in the background, like the encoder.
    people = {"state": "loading" if has_faces else "none", "p": None}   # none (no faces found yet) | loading | ready

    def load_people():      # again after a reload: the one that is ready keeps answering until the new one is
        from faces import People
        p = People(a.db, a.store, [cache] if cache == seed else [cache, seed], log=lambda msg: print(msg, flush=True))
        people.update(p=p, state="ready")

    if has_faces:
        threading.Thread(target=load_people, daemon=True).start()

    # Importing PyTorch and loading the text encoder is most of the startup time, and only typed
    # queries need it, so it loads while the page, filters and thumbnails are already being served.
    encoder, encoder_ready, model_lock = {}, threading.Event(), threading.Lock()

    def load_encoder():
        t = time.time()
        import torch
        from transformers import AutoImageProcessor, AutoModel, AutoTokenizer
        torch.set_num_threads(a.threads)
        encoder.update(torch=torch, model=AutoModel.from_pretrained(a.model, dtype=torch.float32).eval(),
                       tok=AutoTokenizer.from_pretrained(a.model))
        if inbox:   # the image side, for photos arriving from the phone
            encoder["images"] = AutoImageProcessor.from_pretrained(a.model)
        encoder_ready.set()
        text_vec("warm-up")  # the first forward pass is ~1 s slower; take it here, not on a user's query
        print(f"text encoder ready in {time.time() - t:.1f}s", flush=True)

    threading.Thread(target=load_encoder, daemon=True).start()

    # Devices that keep their own copy of the catalog (the iPhone app) follow it through a change feed
    # (sync.py). The feed's versions live beside the catalog, never in it.
    feed = Feed(Path(a.db).resolve().parent / "sync-feed.db")
    feed_dups = None   # set once feed_update has run

    def feed_extra(i: int) -> dict:
        """What a device needs to order and stack a photo as the app does, and cannot work out without the
        vectors: `dup` for a shot behind another in its stack, `stack_of` naming the stack (its earliest
        photo), and `best`, what "Best first" sorts by (larger is better). The score, not the photo's
        place in the order: a place changes for every photo whenever one arrives, a score does not."""
        out = {}
        if dup[i]:
            out["dup"] = True
        if stack_of[i] >= 0:
            out["stack_of"] = shas[stack_of[i]]
        if score is not None:
            out["best"] = round(float(score[i]), 3)
        return out

    def feed_update():
        """Tell the feed what the catalog now holds. A row is the photo's search result, plus feed_extra."""
        nonlocal feed_dups
        t, S, mt, vis = time.time(), shas, meta, visible
        dups = {S[i] for i in np.flatnonzero(dup & vis).tolist()}
        n = feed.update({s: dict(mt[s], **feed_extra(i)) for i, (s, v) in enumerate(zip(S, vis)) if v})
        feed_dups = dups
        print(f"sync feed: version {feed.version}, {n} rows changed ({time.time() - t:.1f}s)", flush=True)

    threading.Thread(target=feed_update, daemon=True).start()
    import pages   # the pages, with the shared stylesheet put in
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

    # Names and places in a typed search ("ben and matthew on a beach", "ben san diego") are filters, not
    # words for the image model: it doesn't know who anyone is, and it can only guess a place from how it
    # looks (a sunny Florida street passes for San Diego). A person's full name always counts, and a
    # first name when one person has it. A place counts if the library has photos there: a city, region
    # or country. A name or place that's also an everyday word ("sunny", "celebration", "golden") counts
    # only when capitalized ("Sunny's" too). People: photos with all of them; places: any of them.
    joiners = {"\0": {"and", "&", "+", "with", ","}, "\1": {"in", "at", "from", "near", "around", "to", "and", "or", ","}}
    filler = {"photo", "photos", "picture", "pictures", "pic", "pics", "of", "the", "all", "show", "me", "find",
              "trip", "trips", "visit"}

    def read_words(text: str):
        """'ben and matthew in san diego' -> (['Ben Lam', 'Matthew Lam'], ['San Diego'], '')."""
        names, places, parts, at = [], [], [], 0
        for m in word_re.finditer(text):
            word = m.group("who") or m.group("where")
            key, capital = word.lower(), word[0].isupper() or m.group(3)
            if m.group("who"):
                if key in COMMON_WORDS and not capital:
                    continue
                mark, got, val = "\0", names, who[key]
            else:
                if key in common_place and not capital:
                    continue
                mark, got, val = "\1", places, where[key]
            parts += [text[at:m.start()], mark]
            at = m.end()
            if val not in got:
                got.append(val)
        if not names and not places:
            return [], [], text
        toks = re.findall(r"[\0\1]|,|[^\s,\0\1]+", " ".join(parts + [text[at:]]))
        keep = [t for i, t in enumerate(toks) if t not in "\0\1" and not any(
            t.lower() in joiners[mk] for mk in toks[max(0, i - 1):i] + toks[i + 1:i + 2] if mk in joiners)]
        rest = " ".join(keep).strip(" ,")
        everything = filler | joiners["\0"] | joiners["\1"]
        return names, places, "" if all(t.lower() in everything for t in rest.split()) else rest

    def place_mask(place: str):
        def make():
            rx = re.compile(r"(?<!\w)" + re.escape(place.lower()) + r"(?!\w)")
            return np.fromiter((bool(rx.search(x)) for x in cols["place"]), bool, N)
        return remember(("place", place), make)

    def person_mask(name: str):
        m = np.zeros(N, bool)
        m[multi["person"]["rows"][multi["person"]["codes"] == people_ix[name]]] = True
        return m

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
        if (qs.get("hidden") or ["0"])[0] == "1":
            masks["hidden"] = ~visible
        member = index.get((qs.get("stack") or [""])[0])
        if member is not None:   # the photos of one stack, named by any of them
            masks["stack"] = stack_of == stack_of[member] if stack_of[member] >= 0 else np.arange(N) == member
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
        names, places, rest = read_words(text)
        if (names or places) and (qs.get("names") or ["1"])[0] != "0":
            if names:
                masks["names"] = np.logical_and.reduce([person_mask(n) for n in names])
            if places:
                masks["places"] = np.logical_or.reduce([place_mask(p) for p in places])
            text = rest
        return masks, text, applied, scope or typed_scope, names, places

    def view_masks(qs: dict, masks: dict, text: str, similar: bool) -> bool:
        """The view options, for browsing and filtering (no words to rank by, not "More like this"), as the
        iPhone app has them: screenshots out (`shots=0`), and stacks shown as their top photo (`dedupe=1`) by
        the library-wide flag the iPhone uses too, so a count is the count of what shows, and a cursor is a
        position in that list. True when browsing: take() then skips nothing page by page.
        Stacks collapse in every list, a ranked one too, except the list that is one stack's photos."""
        if (qs.get("dedupe") or ["1"])[0] == "1" and "stack" not in masks:
            masks["dup"] = ~dup
        if text or similar:
            return False
        if (qs.get("shots") or ["1"])[0] == "0":
            masks["shots"] = ~shot
        return True

    # What an agent's tools are made of (estate_mcp.py). An agent says exactly what it wants, so nothing
    # is read out of its words (no names or places taken from the description) and no view option
    # applies: it finds near-identical shots and screenshots too, and every match, not a screenful.
    def agent_find(args: dict):
        """Photos matching the filters, best first: ([ids], [scores]), or ([ids], None) newest first
        when there is nothing to rank by. Ranks by `query` (words) or `like` (a photo's id)."""
        mask = ~visible if args.get("hidden_only") else visible.copy()
        for key in ("place", "album", "camera"):
            if args.get(key):
                v = str(args[key]).lower()
                mask &= np.fromiter((v in x for x in cols[key]), bool, N)
        for name in args.get("people") or []:
            if name not in people_ix:
                raise ValueError(f"nobody in the library is named {name!r}; library_overview lists the names")
            mask &= person_mask(name)
        if args.get("named_people") in ("none", "some"):
            has = np.fromiter((bool(x) for x in cols["person"]), bool, N)
            mask &= has if args["named_people"] == "some" else ~has
        lo, hi = month_index(str(args.get("date_from") or "")), month_index(str(args.get("date_to") or ""), end=True)
        if lo is not None or hi is not None:
            mask &= (ym >= max(lo or 0, 0)) & (ym <= (hi if hi is not None else nmonths))
        if args.get("event") is not None:
            mask &= event_of == int(args["event"])
        if args.get("trip") is not None:
            mask &= trip_of == int(args["trip"])
        if args.get("favorite"):
            mask &= fav
        if args.get("screenshots") is False:
            mask &= ~shot
        if args.get("distinct"):         # the top photo of each stack of near-identical shots, not the others
            mask &= ~dup
        by_best = args.get("order") == "best"
        if by_best and rank is None:
            raise ValueError("this library has no quality scores, so nothing can be ordered best first")
        quality = lambda ids: [round(float(score[i]), 3) for i in ids]
        like, text = args.get("like"), str(args.get("query") or "").strip()
        if like:
            if like not in index:
                raise ValueError("no photo has that id")
            sims = M @ M[index[like]]
            mask[index[like]] = False
        elif text:
            sims = M @ remember(("text", text), lambda: text_vec(text))
        elif by_best:
            ids = best[mask[best]]
            return [shas[i] for i in ids], quality(ids)
        else:
            return [shas[i] for i in newest[mask[newest]]], None
        sims = np.where(mask, sims, -np.inf)
        if args.get("min_score") is not None:
            sims[sims < float(args["min_score"])] = -np.inf
        elif not like and mask.any():   # as a typed search in the app: near the best match, above the floor
            sims[sims < max(a.match_floor, sims.max() - a.match_window)] = -np.inf
        if by_best:                      # what matches, the best of it first
            found = np.flatnonzero(np.isfinite(sims))
            ids = found[np.argsort(rank[found], kind="stable")]
            return [shas[i] for i in ids], quality(ids)
        order = np.argsort(-sims)[: int(np.isfinite(sims).sum())]
        return [shas[i] for i in order], [round(float(sims[i]), 4) for i in order]

    def agent_row(sha: str):
        m = meta.get(sha)
        if m is None:
            return None
        row = {"id": sha, "date": m.get("local") or m["taken"], "place": m.get("place"), "people": m["people"],
               "albums": m["albums"], "event": m.get("event_title"), "event_id": m.get("event"), "trip": m.get("trip_title"),
               "camera": m.get("camera"), "favorite": m["fav"] or None, "screenshot": is_screenshot(m) or None,
               "source_path": m["path"], "hidden": (not visible[index[sha]]) or None,
               "quality": round(float(score[index[sha]]), 3) if score is not None else None,
               "shots_alike": int(stack_n[index[sha]]) if stack_n[index[sha]] > 1 else None}
        return {k: v for k, v in row.items() if v not in (None, [], "")}

    def agent_faces(ids) -> dict:
        """{id: (faces found, faces named)} for the photos in which faces were looked for and found."""
        if not has_faces or not ids:
            return {}
        con = sqlite3.connect(a.db, timeout=30)
        try:
            return {sha: (n, named or 0) for c in chunks(ids) for sha, n, named in con.execute(
                "SELECT sha256, count(*), sum(person_id > 0) FROM faces WHERE sha256 IN (%s) GROUP BY 1" % ",".join("?" * len(c)), c)}
        except sqlite3.OperationalError:
            return {}
        finally:
            con.close()

    def agent_overview() -> dict:
        top = lambda pairs, n=40: [{"name": k, "photos": v} for k, v in pairs[:n]]
        f = info["facets"]
        month = lambda i: f"{(m0 + i) // 12}-{(m0 + i) % 12 + 1:02d}"
        dated = [i for i, n in enumerate(info["months"]) if n]
        return {"photos": info["total"], "hidden": int(N - visible.sum()),
                "first_month": month(dated[0]) if dated else None, "last_month": month(dated[-1]) if dated else None,
                "by_year": dict(sorted(Counter(str(y) if y else "undated" for y in year[visible].tolist()).items())),
                "people": top(f["person"], 200), "countries": top(f["country"]), "cities": top(f["city"]),
                "albums": top(f["album"]), "cameras": top(f["camera"]), "trips": len(trip_list),
                "faces_found": has_faces,
                "search": {"model": a.model, "match_floor": a.match_floor, "match_window": a.match_window}}

    def periods(order) -> dict:
        """The photos in `order` (by time), as a card a year and a card a month, as the iPhone's Library
        has them: each with its key ("2019", "2019-07"), how many, where it starts in `order`, the photo on
        the front and its places. The front is a favourite if there is one, a landscape one if not; among
        those, the best photo (scores.py), or the one nearest the middle in a catalog with no scorers."""
        out = {}
        city = single["city"]
        for name, key_of, top in (("years", lambda m: m // 12, 3), ("months", lambda m: m, 2)):
            k = np.where(ym[order] >= 0, key_of(ym[order] + m0), -1)
            cuts = np.flatnonzero(np.diff(k)) + 1
            starts, ends = np.concatenate([[0], cuts]), np.concatenate([cuts, [len(order)]])
            cards = []
            for a0, b0 in zip(starts.tolist(), ends.tolist()):
                if not len(order) or k[a0] < 0:   # undated photos have no year or month
                    continue
                run = order[a0:b0]
                score = fav[run] * 4_000_000 + ((ars[run] >= 1.2) & (ars[run] <= 1.9)) * 2_000_000 \
                    - (rank[run] if rank is not None else np.abs(np.arange(len(run)) - len(run) // 2))
                cover = int(run[int(np.argmax(score))])
                codes = city["codes"][run]
                codes = codes[codes >= 0]
                names = {}
                for c, n in zip(*np.unique(codes, return_counts=True)):
                    nm = city["labels"][c].split(",")[0].strip()
                    names[nm] = names.get(nm, 0) + int(n)
                v = int(k[a0])
                cards.append({"key": str(v) if name == "years" else f"{v // 12}-{v % 12 + 1:02d}", "n": b0 - a0, "at": a0,
                              "cover": shas[cover], "ar": round(float(ars[cover]), 4),
                              "places": [x for x, _ in sorted(names.items(), key=lambda kv: (-kv[1], kv[0]))[:top]]})
            out[name] = cards
        return out

    def browse_order(mask, sort: str):
        """The photos `mask` selects with nothing to rank them by: by time, or the best first."""
        by = best if sort == "best" else oldest if sort == "old" else newest
        ids = by[mask[by]]
        if sort == "best-month":   # the Library: months newest first as ever, the best first in each
            runs = np.cumsum(np.concatenate([[0], np.diff(ym[ids]) != 0]))
            ids = ids[np.lexsort((rank[ids], runs))]
        return ids

    def shapes(order) -> bytes:
        """Every photo's shape, a byte each, in the order a list shows them: what the page needs to lay a
        month out before it has been told which photos are in it. The byte is the aspect ratio between
        0.5 and 2.6 (the grid shows nothing narrower or wider) in 255 steps; search.html reads it back."""
        return np.round((np.clip(ars[order], 0.5, 2.6) - 0.5) * (255 / 2.1)).astype(np.uint8).tobytes()

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
        rows = [dict(sha=shas[i], **meta[shas[i]], **({"stack": int(stack_n[i]), "stack_of": shas[stack_of[i]]} if stack_n[i] > 1 else {})) for i in kept]
        return rows, hidden, pos if pos < len(order) else None

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

    # A photo that just arrived from the phone is prepared now, while the file is still in the page
    # cache, decoding it once for both (three workers: decoding a HEIC is most of the time and runs in
    # parallel; the model takes one photo at a time):
    #   - its grid thumbnail, so the first look at new photos never waits on decoding a 12 MP HEIC;
    #   - its embedding, with the model this process already holds for text search. Starting
    #     `embed.py run` would cost ~7 s of loading PyTorch and the model for a fraction of a second of
    #     work; with the rows already there it finds nothing to do and never loads them;
    #   - for a HEIC, the size, EXIF date and dHash `estate.py ingest` would otherwise decode it again for
    #     (~0.45 s a photo), left in <inbox>.partial/facts for `ingest --facts`.
    # Both are keyed by the photo's hash, the key the ingest will give it. The embedding row is the one
    # thing the app writes to the catalog: a value computed from the file's bytes as `embed.py run`
    # computes it (byte for byte at --batch 1; batching itself moves cosine by ~1e-4), so which of the
    # two writes it does not matter.
    import queue
    arrivals = queue.Queue()

    def prepare_arrivals():
        con = None
        while True:
            sha, path = arrivals.get()
            try:
                dest = cache / "thumbs" / f"{sha}.jpg"
                facts = staging_dir(inbox) / "facts" / f"{sha}.json"
                con = con or connect(a.db)
                embedded = con.execute("SELECT 1 FROM embeddings WHERE sha256=? AND model=?", (sha, a.model)).fetchone()
                if dest.exists() and embedded:
                    continue
                encoder_ready.wait()
                side = encoder["images"].size["height"]
                with Image.open(path) as raw:
                    raw.draft("RGB", (side * 2, side * 2))   # as _decode does, so the vector is the same
                    im = ImageOps.exif_transpose(raw).convert("RGB")
                    if raw.format == "HEIF":   # draft() does nothing for HEIC, so these are image_facts' own
                        facts.parent.mkdir(parents=True, exist_ok=True)
                        tmp = facts.with_suffix(f".{threading.get_ident()}.tmp")
                        tmp.write_text(json.dumps(estate_facts(raw, im)))
                        os.replace(tmp, facts)
                    if not embedded:
                        small = im.resize((side, side), Image.Resampling.BICUBIC)
                        torch, model = encoder["torch"], encoder["model"]
                        with model_lock, torch.no_grad():
                            px = encoder["images"](images=[small], return_tensors="pt")["pixel_values"]
                            v = torch.nn.functional.normalize(
                                _features(torch, model.get_image_features(pixel_values=px)), dim=-1)[0]
                        vec = v.numpy().astype(np.float16)
                        con.execute("INSERT OR REPLACE INTO embeddings VALUES (?,?,?,?,?,?)",
                                    (sha, a.model, vec.shape[0], vec.tobytes(), None, now()))
                        con.commit()
                    if not dest.exists():
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        im.thumbnail((360, 360))
                        tmp = dest.with_suffix(f".{threading.get_ident()}.tmp")
                        im.save(tmp, "JPEG", quality=82)
                        os.replace(tmp, dest)
            except Exception as e:     # `embed.py run` and the page cope without it
                print(f"upload: could not prepare {path.name}: {type(e).__name__}: {e}", flush=True)
            finally:
                arrivals.task_done()

    def arrivals_prepared(timeout: float = 120) -> None:
        """Wait (a while) for the worker to catch up, so the ingest finds every embedding already made."""
        with arrivals.all_tasks_done:
            arrivals.all_tasks_done.wait_for(lambda: not arrivals.unfinished_tasks, timeout)

    for _ in range(3 if inbox else 0):
        threading.Thread(target=prepare_arrivals, daemon=True).start()

    # Every search, page of results, and photo opened or shared from one, one JSON object per line.
    # This is the raw material for choosing evaluation queries from real use; nothing reads it back.
    qlog = Path(a.query_log or Path(a.db).resolve().parent / "query-log.jsonl")
    qlog_lock = threading.Lock()
    unlogged = {"cursor", "limit", "dedupe", "shots", "ev", "sha", "pos"}

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

    # /stats: what the library is and what it costs (stats.py). The library numbers come from memory and
    # never change until a restart. The disk numbers walk the cache folders and read the catalog, which
    # takes a couple of minutes on a hard disk, so they are measured once, saved next to the catalog
    # (stats-cache.json) and only measured again when you press Recompute. They change when photos are
    # imported, which today means running the scripts.
    started = time.time()
    st = {"lib": None, "disk": None, "at": 0.0, "running": False, "error": None}
    st_lock = threading.Lock()
    st_file = Path(a.db).resolve().parent / "stats-cache.json"
    try:
        saved = json.loads(st_file.read_text())
        st["disk"], st["at"] = saved["disk"], saved["at"]
    except (OSError, ValueError, KeyError):
        pass

    def stats_job():
        try:
            import stats
            if st["lib"] is None:
                st["lib"] = stats.library_stats(meta, list(when), people_ix, trips, events)
            disk = stats.disk_stats(a.db, a.store, [cache] if cache == seed else [cache, seed])
            st["disk"], st["at"], st["error"] = disk, time.time(), None
            try:
                tmp = st_file.with_suffix(".tmp")
                tmp.write_text(json.dumps({"at": st["at"], "disk": disk}))
                os.replace(tmp, st_file)
            except OSError:
                pass
        except Exception as e:  # the page says so; the rest of the app is unaffected
            st["error"] = f"{type(e).__name__}: {e}"
        finally:
            st["running"] = False

    def assets_now():
        try:
            c = sqlite3.connect(a.db, timeout=10)
            try:
                return c.execute("SELECT count(*) FROM assets").fetchone()[0]
            finally:
                c.close()
        except sqlite3.Error:
            return None

    def stats_payload(refresh: bool):
        with st_lock:
            if st["lib"] is None:
                import stats
                st["lib"] = stats.library_stats(meta, list(when), people_ix, trips, events)
            if not st["running"] and (refresh or st["disk"] is None):
                st["running"] = True
                threading.Thread(target=stats_job, daemon=True).start()
        import stats
        qlines = qsize = None
        try:
            qsize = qlog.stat().st_size
            if qsize < 64 << 20:
                with qlog.open("rb") as f:
                    qlines = sum(1 for _ in f)
        except OSError:
            pass
        return {"state": "working" if st["running"] else "error" if st["error"] and not st["disk"] else "ready",
                "error": st["error"], "computed_at": st["at"] or None, "library": st["lib"], "disk": st["disk"],
                "catalog_assets_now": assets_now(),
                "app": {"uptime_s": round(time.time() - started), "rss_bytes": stats.rss_bytes(), "model": a.model,
                        "demo": bool(a.demo), "python": sys.version.split()[0], "faces_ready": people["state"] == "ready",
                        "query_log_bytes": qsize, "query_log_lines": qlines, "cache_dir": str(cache),
                        "store": str(store), "db": str(a.db)}}

    under_launchd = os.environ.get("XPC_SERVICE_NAME", "0") not in ("", "0")   # the job's label, only when launchd started us

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
            text = ctype.startswith(("application/json", "text/", "application/octet-stream"))   # (the shapes: mostly four values)
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

        def people_page(self, method: str, u, qs: dict, body) -> bool:
            """Answer a People page request; False if the path isn't one."""
            if not has_faces or not u.path.startswith(("/people", "/face/", "/api/people")):
                return False
            if people["state"] != "ready":
                if method == "GET" and u.path.rstrip("/") == "/people":   # the page itself: wait, and ask again
                    self.send(503, pages.waiting("Finding everyone", "Faces load after the library does. This takes under a minute; the page opens by itself."), "text/html; charset=utf-8")
                else:
                    self.send(503, b"Faces are still loading; try again in a moment.", "text/plain")
                return True
            got = people["p"].handle(method, u.path, qs, body)
            self.send(*(got or (404, b"not found", "text/plain", "no-store")))
            return True

        def changes_route(self, method: str, u, qs: dict, body) -> bool:
            """Change sets (changes.py): the review page and its API. Proposing, deciding and applying
            change what everyone sees, so they take the same permission as uploading (--upload-user), or
            come from this computer itself (not through `tailscale serve`): that is where the agent runs."""
            if not u.path.startswith(("/changes", "/api/changes", "/api/albums", "/api/tasks")):
                return False
            from intake.server import is_local
            if method == "POST" and not (self.allowed() or is_local(self)):
                self.send(403, b'{"error":"not allowed"}', "application/json")
                return True
            got = (tasks if u.path.startswith("/api/tasks") else changes).handle(method, u.path, qs, body, self.who())
            self.send(*(got or (404, b"not found", "text/plain", "no-store")))
            return True

        def mcp_route(self, method: str, body):
            """The library as tools for an agent (estate_mcp.py). Anyone who can open the app can search
            with it; proposing a change set takes what the Changes page takes."""
            from intake.server import is_local
            local = is_local(self)
            self.send(*mcp.handle(method, self.headers, body, self.allowed() or local, self.who(), local))

        def do_POST(self):
            u = urlparse(self.path)
            if u.path in ("/api/ingest", "/api/restart"):
                return self.admin(u.path)
            if u.path.startswith("/api/upload/"):   # the system uploader POSTs
                return self.upload()
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except ValueError:
                body = None
            try:
                if u.path == "/mcp":
                    return self.mcp_route("POST", body)
                if u.path == "/api/favorite":
                    return self.favorite(body)
                if intake and u.path.startswith("/api/intake"):
                    return self.intake_route("POST", u, parse_qs(u.query), body)
                if self.changes_route("POST", u, parse_qs(u.query), body):
                    return
                if not self.people_page("POST", u, parse_qs(u.query), body):
                    self.send(404, b"not found", "text/plain")
            except (BrokenPipeError, ConnectionResetError):
                pass

        def favorite(self, body):
            """POST /api/favorite {sha, fav}: a photo is, or is no longer, a favourite. It changes what
            everyone sees (a month's cover is a favourite if it has one), so it takes what uploading takes."""
            from intake.server import is_local
            if not (self.allowed() or is_local(self)):
                return self.send(403, b'{"error":"not allowed"}', "application/json")
            sha, on = (body.get("sha"), body.get("fav")) if isinstance(body, dict) else (None, None)
            if not isinstance(sha, str) or sha not in index or not isinstance(on, bool):
                return self.json({"error": "say which photo (sha) and fav: true or false"}, 400)
            self.json({"sha": sha, "fav": on, "favourites": set_favorite(sha, on, self.who())})

        def intake_route(self, method: str, u, qs: dict, body):
            """The setup page and its API (intake/api.py). Starting a job reads any folder on this computer,
            so it takes the same permission as uploading (--upload-user)."""
            from intake.server import is_local
            if method == "POST" and not self.allowed():
                return self.send(403, b'{"error":"not allowed"}', "application/json")
            got = intake.handle(method, u.path, qs, body, is_local(self))
            self.send(*(got or (404, b"not found", "text/plain")))

        def who(self) -> str:
            # `tailscale serve` adds the caller's identity; the app itself is bound to 127.0.0.1
            return self.headers.get("Tailscale-User-Login", "")

        def allowed(self) -> bool:
            return not a.upload_user or self.who() in a.upload_user

        def admin(self, path: str):
            """POST /api/ingest runs ingest-iphone.sh; POST /api/restart relaunches the app (launchd only)."""
            if ingest is None:
                return self.send(404, b"not found", "text/plain")
            if not self.allowed():
                return self.send(403, b"not allowed", "text/plain")
            if path == "/api/ingest":
                started = ingest.start()
                return self.json({"started": started}, 202 if started else 409)
            if not under_launchd:   # run by hand, nothing would bring it back
                return self.json({"error": "not running under launchd"}, 409)
            print(f"restart requested by {self.who() or 'local'}", flush=True)
            self.json({"restarting": True}, 202)
            threading.Timer(0.5, os._exit, (0,)).start()   # KeepAlive starts it again

        def do_PUT(self):
            self.upload()

        def do_OPTIONS(self):
            # The phone's background uploader asks first whether the server can resume interrupted uploads.
            # It can't: 501 means "send each file in one request".
            if urlparse(self.path).path.startswith("/api/upload"):
                self.extra_headers = {"Allow": "OPTIONS, PUT, POST"}
                return self.send(501, b"", "text/plain")
            self.send(404, b"not found", "text/plain")

        def upload(self):
            """PUT or POST /api/upload/<YYYY/MM/DD/name.ext>: one file from the phone into the inbox (upload.py)."""
            u = urlparse(self.path)
            prefix = "/api/upload/"
            self.close_connection = True   # every early answer below leaves the body unread
            if not u.path.startswith(prefix) or inbox is None:
                return self.send(404, b"not found", "text/plain")
            who = self.who()
            if not self.allowed():
                return self.send(403, b"not allowed", "text/plain")
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                return self.send(411, b"Content-Length required", "text/plain")
            try:
                h = self.headers
                meta = {"taken": h.get("X-Taken"), "lat": h.get("X-Latitude"), "lon": h.get("X-Longitude"),
                        "fav": h.get("X-Favorite")}
                stored = {}
                code, msg = save_upload(inbox, unquote(u.path[len(prefix):]), self.rfile, length,
                                        h.get("X-Content-SHA256"), meta, stored)
                if stored and stored["path"].suffix.lower() in IMAGE_EXT:
                    arrivals.put((stored["sha256"], stored["path"]))
                self.close_connection = code >= 400   # a refused body may still be on the wire
            except OSError as e:
                code, msg = 500, f"could not write: {e.strerror}"
            if code == 201:
                print(f"upload: {self.command} {u.path[len(prefix):]} ({length / 1e6:.1f} MB) from {who or 'local'}", flush=True)
            if ingest and code in (200, 201):
                ingest.poke(self.headers.get("X-Batch"), u.path[len(prefix):], new=code == 201)
            self.json({"status": msg}, code)

        def download(self, sid: int):
            """GET /api/albums/<id>/download: the album's original files as one zip, written as it is read
            (nothing is held in memory or on disk). Names are the files' own, numbered when two are alike."""
            import zipfile
            with gate:                   # only to say which files; the copying must not hold up a reload
                got = changes.shas(sid)
                files = [] if not got else [(store / s[:2] / s[2:4] / f"{s}{meta[s]['ext']}",
                                             (meta[s]["path"] or "").rsplit("/", 1)[-1] or f"{s[:12]}{meta[s]['ext']}")
                                            for s in got[1] if s in meta]
            if not got:
                return self.send(404, b'{"error":"no such album"}', "application/json")
            name = re.sub(r"[^\w .-]+", " ", got[0]).strip() or f"album-{sid}"
            self.close_connection = True         # its length is not known beforehand: the end of the connection ends it
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(name)}.zip")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            used = Counter()
            with zipfile.ZipFile(self.wfile, "w", zipfile.ZIP_STORED) as z:
                for src, base in files:
                    used[base.lower()] += 1
                    if used[base.lower()] > 1:
                        stem, dot, ext = base.rpartition(".")
                        base = f"{stem} ({used[base.lower()]}).{ext}" if dot else f"{base} ({used[base.lower()]})"
                    try:
                        z.write(src, base)
                    except OSError:              # a file that is not on the disk: the rest still come
                        continue

        def do_GET(self):
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            one = lambda k, d="": (qs.get(k) or [d])[0]
            try:
                if m := re.fullmatch(r"/api/albums/(\d+)/download", u.path):
                    return self.download(int(m[1]))
                if intake and u.path.startswith(("/setup", "/api/intake")):
                    return self.intake_route("GET", u, qs, None)
                if self.people_page("GET", u, qs, None) or self.changes_route("GET", u, qs, None):
                    return
                if u.path == "/mcp":
                    return self.mcp_route("GET", None)
                if u.path in ("/", "/index.html"):  # revalidated each load, so edits show up at once
                    body = pages.page("search.html")
                    return self.send(200, body, "text/html; charset=utf-8", "no-cache",
                                     etag=f'"{hashlib.md5(body).hexdigest()}"')
                if u.path in ("/api/search", "/api/similar"):
                    t = time.time()
                    limit = max(1, min(int(one("limit", "120")), 500))
                    start = max(0, int(one("cursor", "0")))
                    masks, text, applied, scope, names, places = select(qs)
                    deduped = view_masks(qs, masks, text, u.path == "/api/similar")
                    mask = combine(masks)
                    sha = one("sha")
                    if u.path == "/api/similar" and sha not in index:
                        return self.json({"error": "unknown photo"}, 404)
                    by_default = "best" if "stack" in masks else "old" if scope else "new"   # a stack: its top photo first
                    sort = one("sort") or by_default
                    if sort in ("best", "best-month") and best is None:   # a catalog with no scorers: as if nothing was asked
                        sort = "old" if scope or "stack" in masks else "new"
                    if u.path == "/api/similar":
                        sort = "match"            # by likeness: there is no other order to choose
                    elif text and sort != "best":
                        sort = "match"            # words to rank by: the closest match first, unless the best was asked for
                    key = (u.path, sha, text, sort, tuple(sorted((k, tuple(v)) for k, v in qs.items()
                                                                 if k not in ("cursor", "limit"))))

                    def ranked():
                        if u.path == "/api/similar":
                            vec = M[index[sha]]
                        elif text:
                            vec = remember(("text", text), lambda: text_vec(text))
                        else:  # nothing to rank by: what the filters select, by time
                            return browse_order(mask, sort)
                        sims = np.where(mask, M @ vec, -np.inf)
                        if u.path == "/api/similar":
                            sims[index[sha]] = -np.inf
                        else:  # only what matches the words: near the best match, and above the floor
                            sims[sims < max(a.match_floor, sims.max() - a.match_window)] = -np.inf
                            if sort == "best":    # the closest matches only (see BEST_WINDOW), the best of them first
                                sims[sims < sims.max() - a.best_window] = -np.inf
                                found = np.flatnonzero(np.isfinite(sims))
                                return found[np.argsort(rank[found], kind="stable")]
                        return np.argsort(-sims)[: int(np.isfinite(sims).sum())]

                    order = remember(key, ranked)
                    results, hidden, nxt = take(order, start, limit, one("dedupe", "1") == "1" and not deduped)
                    record(self, "similar" if u.path == "/api/similar" else "search", qs, sha=one("sha") or None, text=text,
                           filters=applied, people=names, places=places, cursor=start, matching=int(len(order)),
                           ms=round((time.time() - t) * 1000),
                           top=[r["sha"] for r in results[:20]] if start == 0 else None)
                    return self.json({"results": results, "next": nxt, "hidden": hidden, "total": info["total"],
                                      "matching": int(len(order)), "text": text, "filters": applied, "people": names, "places": places,
                                      "scope": scope, "sort": sort, "ms": round((time.time() - t) * 1000)})
                if u.path == "/api/log":  # the page reports photos opened or shared from a result list
                    if one("ev") in ("open", "share") and sha_re.match(one("sha")):
                        pos = one("pos")
                        record(self, one("ev"), qs, sha=one("sha"), pos=int(pos) if pos.isdigit() else None)
                    return self.send(204, b"", "text/plain")
                if u.path == "/api/facets":
                    masks, text = select(qs)[:2]
                    view_masks(qs, masks, text, bool(one("sha")))
                    return self.json(facet_counts(masks))
                if u.path == "/api/periods":   # the Library's Years and Months
                    masks, text = select(qs)[:2]
                    view_masks(qs, masks, "", False)
                    mask, sort = combine(masks), one("sort") or "new"
                    key = ("periods", sort, tuple(sorted((k, tuple(v)) for k, v in qs.items())))

                    def make():
                        by_time = oldest if sort == "old" else newest
                        return {**periods(by_time[mask[by_time]]), "matching": int(mask.sum()), "total": info["total"]}
                    return self.json(remember(key, make))
                if u.path == "/api/layout":    # the shapes of the Library's photos, in /api/search's order
                    masks, text = select(qs)[:2]
                    view_masks(qs, masks, "", False)
                    sort = one("sort") or "new"
                    if sort in ("best", "best-month") and best is None:
                        sort = "new"
                    key = ("layout", sort, tuple(sorted((k, tuple(v)) for k, v in qs.items())))
                    body = remember(key, lambda: shapes(browse_order(combine(masks), sort)))
                    return self.send(200, body, "application/octet-stream", "no-cache",
                                     etag=f'"{hashlib.md5(body).hexdigest()}"')
                if u.path in ("/stats", "/stats.html"):
                    body = pages.page("stats.html")
                    return self.send(200, body, "text/html; charset=utf-8", "no-cache",
                                     etag=f'"{hashlib.md5(body).hexdigest()}"')
                if u.path == "/api/ingest":
                    return self.json(ingest.status() | {"can_restart": under_launchd} if ingest else {"enabled": False})
                if u.path == "/api/stats":
                    return self.json(stats_payload(one("refresh") == "1"))
                if u.path == "/api/info":
                    return self.json(info)
                if u.path == "/api/sync":   # what changed since a device last asked (sync.py)
                    if not feed.ready:
                        self.extra_headers = {"Retry-After": "5"}
                        return self.json({"error": "starting"}, 503)
                    since = one("since", "0")
                    ids, deleted, (ver, after), more = feed.page(one("epoch"), int(since) if since.isdigit() else 0,
                                                                 one("after"), max(1, min(int(one("limit", "2000")), 10000)))
                    rows = [dict(sha=s, **meta[s], **feed_extra(index[s])) for s in ids if s in meta]
                    return self.json({"epoch": feed.epoch, "photos": rows, "deleted": deleted, "since": ver, "after": after,
                                      "more": more, "total": info["total"], "trips": [] if more else trip_list})
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

    def gated(fn):
        def run(self):
            if self.path.startswith(("/api/upload/", "/api/intake", "/setup")) or self.path.endswith("/download"):   # touch none of the above (a download: only briefly, itself); can be slow
                return fn(self)
            with gate:
                return fn(self)
        return run

    for _name in ("do_GET", "do_POST"):
        setattr(Handler, _name, gated(getattr(Handler, _name)))

    # Favourites set in the app. The catalog's own flag (assets.favorited) is what the photo's source said,
    # and `estate.py link` writes it again on every ingest, so what a person says here is kept beside it
    # (the `favorites` table) and wins. It shows at once: the photo's row, the count and every cached
    # order and cover are brought up to date here rather than by re-reading the catalog (load_state reads
    # the table too, so a reload agrees). `fav_log` is every one set since the start, so a reload that
    # read the table a moment too early can be put right as it is swapped in.
    fav_lock, fav_log = threading.Lock(), []

    def show_favorites(pairs) -> None:
        for sha, on in pairs:
            i = index.get(sha)
            if i is not None and bool(fav[i]) != on:
                meta[sha] = dict(meta[sha], fav=on)
                fav[i] = on
        info["fav"] = int((combine({}) & fav).sum())

    def set_favorites(shas, on: bool, who: str) -> int:
        """Record them, show them, tell the phones. Returns how many photos it changed."""
        shas = [s for s in dict.fromkeys(shas) if s in index]
        with fav_lock:
            changed = [s for s in shas if bool(fav[index[s]]) != on]
            con = connect(a.db)
            try:
                con.execute("CREATE TABLE IF NOT EXISTS favorites(sha256 TEXT PRIMARY KEY, fav INT NOT NULL,"
                            " set_at TEXT, set_by TEXT)")
                t = now()
                con.executemany("INSERT OR REPLACE INTO favorites VALUES (?,?,?,?)", ((s, int(on), t, who or None) for s in shas))
                con.commit()
            finally:
                con.close()
            fav_log.extend((s, on) for s in shas)
            show_favorites((s, on) for s in shas)
            with lru_lock:
                lru.clear()      # covers and "favourites only" lists were worked out without them
            with st_lock:
                st["lib"] = None
        if feed.ready and changed:
            feed.touch({s: dict(meta[s], **feed_extra(index[s])) for s in changed if visible[index[s]]})
        return len(changed)

    def set_favorite(sha: str, on: bool, who: str) -> int:
        """One photo. Returns how many favourites there now are."""
        set_favorites([sha], on, who)
        return info["fav"]

    # Re-reading the catalog is in one of three states: idle, running, or rerun (asked again while
    # running: when this run ends, another starts, so nothing that was asked for is missed).
    reload_state = {"state": "idle", "at": None, "seconds": None, "error": None}
    reload_lock = threading.Lock()

    def reload_differences(got: dict, want: dict) -> str:
        """What an incremental load got wrong, compared with a full one ('' if nothing)."""
        def same(x, y):
            if isinstance(x, np.ndarray) or isinstance(y, np.ndarray):
                return isinstance(x, np.ndarray) and isinstance(y, np.ndarray) and x.shape == y.shape and \
                    bool(np.all((x == y) | ((x != x) & (y != y))))
            if isinstance(x, dict) and isinstance(y, dict):
                return x.keys() == y.keys() and all(same(x[k], y[k]) for k in x)
            if isinstance(x, (list, tuple)) and isinstance(y, (list, tuple)):
                return len(x) == len(y) and all(same(p, q) for p, q in zip(x, y))
            if isinstance(x, re.Pattern):
                return x.pattern == y.pattern
            return x == y
        bad = [k for k in STATE if not same(got[k], want[k])]
        if "meta" in bad:
            bad += [f"meta[{s[:8]}]" for s in want["meta"] if not same(got["meta"].get(s), want["meta"][s])][:5]
        return ", ".join(bad)

    def reload_catalog() -> None:
        """Re-read the catalog and swap it in, with no restart. The new state is built on the side while the
        old one keeps answering; only the swap waits for requests in flight. Asked again mid-run, it runs again."""
        with reload_lock:
            if reload_state["state"] != "idle":
                reload_state["state"] = "rerun"
                return
            reload_state["state"] = "running"
        try:
            while True:
                t, mark = time.time(), len(fav_log)
                try:
                    fresh = load_state(loaded.get("raw"))
                    if os.environ.get("PHOTO_ESTATE_CHECK_RELOAD"):   # tools/bench_ingest.py --check
                        print(f"reload check: {reload_differences(fresh, load_state()) or 'same as a full load'}", flush=True)

                    def swap_in():
                        install(fresh)
                        show_favorites(fav_log[mark:])   # set while this load was reading
                    gate.swap(swap_in)
                    with lru_lock:
                        lru.clear()      # cached masks and rankings are the old catalog's
                    with st_lock:
                        st["lib"] = None
                    reload_state.update(at=time.time(), seconds=time.time() - t, error=None)
                    print(f"catalog reloaded: {N} images in {time.time() - t:.1f}s", flush=True)
                    feed_update()
                    if not a.no_pregenerate:
                        threading.Thread(target=pregenerate, daemon=True).start()
                except Exception as e:   # keep serving the old catalog
                    reload_state["error"] = f"{type(e).__name__}: {e}"
                    print(f"catalog reload failed: {reload_state['error']}", flush=True)
                with reload_lock:
                    if reload_state["state"] != "rerun":
                        reload_state["state"] = "idle"
                        break
                    reload_state["state"] = "running"
        except BaseException:
            with reload_lock:
                reload_state["state"] = "idle"
            raise
        if ingest and not reload_state["error"]:
            ingest.fresh = False

    # Change sets (changes.py). When one hides or shows photos, the catalog is re-read as after an ingest:
    # every view, count and cover is rebuilt without them, phones are told through the feed, and the
    # People page loads again without their faces. `settling` covers the moment before the reload starts.
    settling, settling_lock = {"n": 0}, threading.Lock()

    def hidden_changed():
        try:
            reload_catalog()
            if has_faces:
                load_people()
        finally:
            with settling_lock:
                settling["n"] -= 1

    def on_hidden_change():
        with settling_lock:
            settling["n"] += 1
        threading.Thread(target=hidden_changed, daemon=True).start()

    changes = Changes(a.db, facts=lambda sha: meta.get(sha),
                      shown=lambda sha: {"library": sha in index and bool(visible[index[sha]]), "sync": sha in feed.rows},
                      busy=lambda: settling["n"] > 0 or reload_state["state"] != "idle", on_change=on_hidden_change,
                      find=lambda query: agent_find(query), favorite=set_favorites)

    # Tasks (tasks.py): a request in words, worked on by an agent on this computer that uses the MCP
    # endpoint below like any other client, and ends in a change set.
    from types import SimpleNamespace
    from tasks import Tasks
    tasks = Tasks(a.db, f"http://127.0.0.1:{a.port or (8767 if a.demo else 8768 if a.sandbox else 8766)}/mcp",
                  command=a.agent_cmd, env_file=a.agent_env, model=a.agent_model, log=lambda msg: print(msg, flush=True))
    mcp = MCPServer(SimpleNamespace(find=agent_find, row=agent_row, faces=agent_faces, overview=agent_overview,
                                    changes=changes, tasks=tasks))

    if ingest:
        ingest.on_added = reload_catalog
        ingest.before_run = arrivals_prepared
        ingest.on_faces = lambda: has_faces and load_people()   # the run's faces are in: rebuild the People page
        threading.Thread(target=ingest.sweep, daemon=True).start()   # uploads left from before this start

    # Bringing in more photos from the app (/setup): the engine first-run setup uses (intake/), here
    # reloading the catalog when a job's photos are searchable, and the People page when its faces are in.
    intake = None
    if not a.demo:
        from intake.api import IntakeAPI, Library
        from intake.engine import Engine as IntakeEngine

        def make_intake(lib):
            e = IntakeEngine(lib.root, lib.db, lib.store, Path(__file__).resolve().parent, practice=bool(a.sandbox),
                             on_ready=lambda jid: reload_catalog(),
                             on_faces=lambda jid: (reload_catalog(), has_faces and load_people()))
            e.start()
            return e
        intake = IntakeAPI(Library(Path(a.root or Path(a.db).resolve().parent), a.db, store, fixed=True), make_intake,
                           setup_mode=False, practice=bool(a.sandbox))

    # --backup-dir: a copy of the catalog (and the face-label journal) every --backup-every hours, kept
    # --backup-keep deep. Done here, not by a separate job, because this process already has macOS's
    # permission to write the estate disk; a new launchd job would wait on an "Allow" click. The copy is
    # taken with SQLite's backup API, so it is consistent while the app and an ingest keep writing.
    def backup_once(dest: Path) -> None:
        import shutil
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        t = time.time()
        for left in dest.glob(".estate.db.*.tmp"):    # from a copy that was cut short
            left.unlink(missing_ok=True)
        tmp = dest / f".estate.db.{stamp}.tmp"
        src, out = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True, timeout=60), sqlite3.connect(tmp)
        try:
            src.backup(out)        # one step: one read transaction, so writers carry on (WAL)
            out.execute("PRAGMA journal_mode=DELETE")   # one self-contained file, openable read-only
        finally:
            out.close()
            src.close()
        os.replace(tmp, dest / f"estate.db.{stamp}")
        labels = Path(a.db).resolve().parent / "face-labels.jsonl"
        if labels.exists():
            shutil.copy2(labels, dest / f"face-labels.jsonl.{stamp}")
        for pattern in ("estate.db.*", "face-labels.jsonl.*"):
            for old in sorted(dest.glob(pattern))[:-a.backup_keep]:
                old.unlink(missing_ok=True)
        print(f"backup: catalog copied to {dest} in {time.time() - t:.0f}s", flush=True)

    def backups() -> None:
        dest, every = Path(a.backup_dir), a.backup_every * 3600
        while True:
            last = max((p.stat().st_mtime for p in dest.glob("estate.db.*")), default=None) if dest.is_dir() else None
            due = last + every if last else t0 + min(300, every)   # the first shortly after startup, not during it
            if time.time() < due:
                time.sleep(min(due - time.time(), 3600))
                continue
            try:
                if not dest.parent.is_dir():
                    raise OSError(f"{dest.parent} is not there (is the disk mounted?)")
                dest.mkdir(exist_ok=True)
                backup_once(dest)
            except Exception as e:  # try again in an hour
                print(f"backup failed: {type(e).__name__}: {e}", flush=True)
                time.sleep(3600)

    if a.backup_dir and not a.demo:
        threading.Thread(target=backups, daemon=True).start()

    pregen_lock = threading.Lock()

    def pregenerate():  # every thumbnail once, so scrolling results never waits on HEIC decoding
        if not pregen_lock.acquire(blocking=False):
            return
        try:
            _pregenerate()
        finally:
            pregen_lock.release()

    def _pregenerate():
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
        if len(todo) <= 64:   # a few new photos: not worth starting worker processes
            ok = sum(_thumb(job) for job in todo)
            print(f"thumbnails: {ok} made in {time.time() - t:.0f}s", flush=True)
            return
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

    port = a.port or (8767 if a.demo else 8768 if a.sandbox else 8766)
    srv = Server(("127.0.0.1", port), Handler)
    print(f"Open http://127.0.0.1:{port}/  (Ctrl-C to stop)", flush=True)
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
    s.add_argument("--after-rowid", type=int, help="only assets added after this rowid (incremental runs: a few index lookups, not a scan)")
    s.add_argument("--limit", type=int, default=0)
    s.set_defaults(fn=cmd_run)
    s = sub.add_parser("search", help="semantic search")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=15)
    s.set_defaults(fn=cmd_search)
    sub.add_parser("stats").set_defaults(fn=cmd_stats)
    s = sub.add_parser("serve", help="local search page")
    s.add_argument("--root", help="the library's folder: estate.db and store/ inside it (default: the one chosen on"
                   " the setup page; with no library yet, the app opens the setup page)")
    s.add_argument("--store", help="the photo store (default: <root>/store)")
    s.add_argument("--sandbox", action="store_true",
                   help="practise setting up: a scratch library and practice sources made from the demo photos (port 8768)")
    s.add_argument("--demo", action="store_true",
                   help="serve the ~100 public-domain photos in demo/ (--db, --store, --cache are ignored; nothing is logged)")
    s.add_argument("--port", type=int, help="default 8766, or 8767 with --demo so it can run beside the real one")
    s.add_argument("--cache", help="thumbnail cache dir (default: <db dir>/review, shared with estate.py review)")
    s.add_argument("--fast-cache", help="cache dir on a faster disk (e.g. the internal SSD), filled from --cache")
    s.add_argument("--no-pregenerate", action="store_true", help="don't build missing thumbnails in the background")
    s.add_argument("--agent-cmd", help="the Claude Code program that works on tasks (tasks.py); default: `claude` on PATH or in ~/.local/bin")
    s.add_argument("--agent-env", help="a file of KEY=VALUE lines put in the agent's environment, for its sign-in token; "
                                       "default ~/.config/photo-estate/agent.env")
    s.add_argument("--agent-model", help="the model the agent uses; default: Claude Code's own")
    s.add_argument("--match-floor", type=float, default=MATCH_FLOOR,
                   help="typed searches show no photo below this similarity (-1 = no floor)")
    s.add_argument("--match-window", type=float, default=MATCH_WINDOW,
                   help="...nor more than this below the best match (2 = show every photo, ranked)")
    s.add_argument("--best-window", type=float, default=BEST_WINDOW,
                   help="a described search shown best first keeps only matches within this of the best match")
    s.add_argument("--inbox", help="where phone uploads (PUT /api/upload/...) are saved; default <db dir>/iphone (none with --demo)")
    s.add_argument("--ingest-script", help="script the Library info page runs to catalogue uploads (default: ingest-iphone.sh)")
    s.add_argument("--ingest-quiet", type=float, default=60.0,
                   help="seconds without an upload before the ingest runs by itself (0 = only when asked)")
    s.add_argument("--ingest-max-wait", type=float, default=600.0,
                   help="an ingest starts at least this long after the first upload of a burst, however many follow")
    s.add_argument("--upload-user", action="append", default=[],
                   help="Tailscale login allowed to upload (repeatable); default: anyone who can reach the app")
    s.add_argument("--backup-dir", help="copy the catalog and face-labels.jsonl here every --backup-every hours"
                   " (e.g. a folder on another disk than the catalog)")
    s.add_argument("--backup-every", type=float, default=24.0, help="hours between catalog backups")
    s.add_argument("--backup-keep", type=int, default=7, help="catalog backups to keep")
    s.add_argument("--query-log", help="where to record searches and photos opened (default: <db dir>/query-log.jsonl)")
    s.set_defaults(fn=cmd_serve)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
