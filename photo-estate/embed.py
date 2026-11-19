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

from encoder import features as _features

DEFAULT_MODEL = "google/siglip2-base-patch16-256"
SCHEMA = """
CREATE TABLE IF NOT EXISTS embeddings(
  sha256 TEXT, model TEXT, dim INT, vec BLOB,   -- float16, L2-normalised; NULL when undecodable
  error TEXT, created_at TEXT, PRIMARY KEY(sha256, model));
"""
# A typed search shows only photos that match it, not the whole library in ranked order. Measured
# for siglip2-base-patch16-256 with the two-prompt average in encoder.py (cosine between query and
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


def load_model(mid: str, threads: int):
    import torch
    from transformers import AutoModel, AutoProcessor
    torch.set_num_threads(threads)
    return torch, AutoModel.from_pretrained(mid, dtype=torch.float32).eval(), AutoProcessor.from_pretrained(mid)


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
    """The photo app: describe a photo and/or narrow by place, person, album, trip, camera and date.

    This sets the app up and wires its parts together. Each part is a module with an object whose
    handle(method, path, qs, body, ...) answers its own routes (each module's docstring lists them):
      catalog.py     the library in memory (Catalog), and the one being served (Live) that the others read
      browse.py      search, similar, facets, Years and Months, layout, the query log; an agent's searches
      images.py      thumbnails, previews, large images and originals
      admin.py       uploads from the phone, the ingest, restarting, the Library info page, backups
      faces.py (the People page), changes.py (change sets and albums), tasks.py, estate_mcp.py (/mcp),
      intake/api.py (/setup)
    What stays here: the page and the home-screen app files, /api/info, /api/sync, favourites, album
    downloads, re-reading the catalog after an ingest, and the request handler that sends each request on."""
    if not a.demo and not _library_or_setup(a):
        return
    import gzip
    import hashlib
    import json
    import re
    import threading
    from collections import Counter
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from types import SimpleNamespace
    from urllib.parse import parse_qs, quote, urlparse

    import catalog
    import pages   # the pages, with the shared stylesheet put in
    from admin import Admin, backups
    from browse import Browse
    from changes import Changes
    from encoder import Encoder
    from estate_mcp import Server as MCPServer
    from images import Images
    from sync import Feed
    from tasks import Tasks
    from upload import IngestJob

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
    port = a.port or (8767 if a.demo else 8768 if a.sandbox else 8766)
    # Phone uploads land in a plain folder that `estate.py ingest` reads like any other source. The demo
    # takes none unless --inbox says where; otherwise the inbox sits beside the catalog.
    inbox = Path(a.inbox) if a.inbox else None if a.demo else Path(a.db).resolve().parent / "iphone"
    ingest = (IngestJob(Path(a.ingest_script or Path(__file__).with_name("ingest-iphone.sh")), Path(a.db).resolve().parent,
                        a.db, store, inbox, quiet=a.ingest_quiet, max_wait=a.ingest_max_wait) if inbox else None)
    seed = Path(a.cache or Path(a.db).resolve().parent / "review")
    # On the Mac mini the estate disk is a USB hard disk: --fast-cache puts the thumbnail cache on the
    # internal SSD, filled from the disk's cache (`seed`) on first use (images.py).
    cache = Path(a.fast_cache) if a.fast_cache else seed
    t0 = time.time()

    def load_catalog(prev: dict | None = None) -> catalog.Catalog:
        return catalog.load(connect(a.db), a.model, cache, seed, prev, demo=a.demo)

    try:
        live = catalog.Live(load_catalog())
    except RuntimeError as e:
        sys.exit(str(e))
    print(f"{live.cat.N} images loaded in {time.time() - t0:.1f}s ({len(live.cat.events)} events, {len(live.cat.trips)} trips)", flush=True)

    # The People page (/people): who each face is, and the questions worth asking. It's faces.py's,
    # mounted here so naming works wherever the app does. Loads in the background, like the encoder.
    people = {"state": "loading" if live.cat.has_faces else "none", "p": None}   # none (no faces found yet) | loading | ready

    def load_people():      # again after a reload: the one that is ready keeps answering until the new one is
        from faces import People
        p = People(a.db, a.store, [cache] if cache == seed else [cache, seed], log=lambda msg: print(msg, flush=True))
        people.update(p=p, state="ready")

    if live.cat.has_faces:
        threading.Thread(target=load_people, daemon=True).start()

    encoder = Encoder(a.model, a.threads, images=bool(inbox))   # the image side too, for photos arriving from the phone
    encoder.start()

    # Devices that keep their own copy of the catalog (the iPhone app) follow it through a change feed
    # (sync.py). The feed's versions live beside the catalog, never in it.
    feed = Feed(Path(a.db).resolve().parent / "sync-feed.db")

    def feed_update():
        """Tell the feed what the catalog now holds. A row is the photo's search result, plus Catalog.feed_extra."""
        t, c = time.time(), live.cat
        n = feed.update({s: c.feed_row(s) for s, v in zip(c.shas, c.visible) if v})
        print(f"sync feed: version {feed.version}, {n} rows changed ({time.time() - t:.1f}s)", flush=True)

    threading.Thread(target=feed_update, daemon=True).start()
    # Home-screen app files (manifest, icons, service worker), served from the site root: a service
    # worker only controls pages at or below its own path.
    app_dir = Path(__file__).with_name("app")
    app_types = {".webmanifest": "application/manifest+json", ".js": "text/javascript; charset=utf-8",
                 ".png": "image/png"}
    app_files = {f"/{p.name}": p for p in app_dir.iterdir() if p.suffix in app_types} if app_dir.is_dir() else {}

    qlog = Path(a.query_log or Path(a.db).resolve().parent / "query-log.jsonl")
    browse = Browse(live, encoder, a.db, a.model, qlog, a.match_floor, a.match_window, a.best_window)
    images = Images(live, store, cache, seed)
    admin = Admin(live, encoder, connect, now, db=a.db, model=a.model, store=store, cache=cache, seed=seed,
                  inbox=inbox, ingest=ingest, query_log=qlog, demo=a.demo, faces_ready=lambda: people["state"] == "ready")
    NOT_FOUND = (404, b"not found", "text/plain", "no-store")

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

        def reply(self, got):
            """Send what a module's handle() returned: (status, body, content type[, Cache-Control[, headers]]),
            where an ETag among the headers makes the answer revalidatable. None is a 404."""
            code, body, ctype, *rest = got or NOT_FOUND
            headers = dict(rest[1]) if len(rest) > 1 else {}
            etag = headers.pop("ETag", None)
            if headers:
                self.extra_headers = headers
            self.send(code, body, ctype, rest[0] if rest else "no-store", etag=etag)

        def people_page(self, method: str, u, qs: dict, body) -> bool:
            """Answer a People page request; False if the path isn't one."""
            if not live.cat.has_faces or not u.path.startswith(("/people", "/face/", "/api/people")):
                return False
            if people["state"] != "ready":
                if method == "GET" and u.path.rstrip("/") == "/people":   # the page itself: wait, and ask again
                    self.send(503, pages.waiting("Finding everyone", "Faces load after the library does. This takes under a minute; the page opens by itself."), "text/html; charset=utf-8")
                else:
                    self.send(503, b"Faces are still loading; try again in a moment.", "text/plain")
                return True
            self.reply(people["p"].handle(method, u.path, qs, body))
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
            self.reply((tasks if u.path.startswith("/api/tasks") else changes).handle(method, u.path, qs, body, self.who()))
            return True

        def mcp_route(self, method: str, body):
            """The library as tools for an agent (estate_mcp.py). Anyone who can open the app can search
            with it; proposing a change set takes what the Changes page takes."""
            from intake.server import is_local
            local = is_local(self)
            self.send(*mcp.handle(method, self.headers, body, self.allowed() or local, self.who(), local))

        def do_POST(self):
            u = urlparse(self.path)
            if u.path in ("/api/ingest", "/api/restart"):   # (the body, if any, is not read)
                return self.reply(admin.handle("POST", u.path, {}, None, self.who(), self.allowed()))
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
            if not isinstance(sha, str) or sha not in live.cat.index or not isinstance(on, bool):
                return self.json({"error": "say which photo (sha) and fav: true or false"}, 400)
            self.json({"sha": sha, "fav": on, "favourites": set_favorite(sha, on, self.who())})

        def intake_route(self, method: str, u, qs: dict, body):
            """The setup page and its API (intake/api.py). Starting a job reads any folder on this computer,
            so it takes the same permission as uploading (--upload-user)."""
            from intake.server import is_local
            if method == "POST" and not self.allowed():
                return self.send(403, b'{"error":"not allowed"}', "application/json")
            self.reply(intake.handle(method, u.path, qs, body, is_local(self)))

        def who(self) -> str:
            # `tailscale serve` adds the caller's identity; the app itself is bound to 127.0.0.1
            return self.headers.get("Tailscale-User-Login", "")

        def allowed(self) -> bool:
            return not a.upload_user or self.who() in a.upload_user

        def do_PUT(self):
            self.upload()

        def do_OPTIONS(self):
            self.reply(admin.handle("OPTIONS", urlparse(self.path).path, {}, None))

        def upload(self):
            """PUT or POST /api/upload/...: one file from the phone (admin.py)."""
            self.close_connection = True   # until admin says the body was read
            got, self.close_connection = admin.upload(self.command, urlparse(self.path).path, self.headers, self.rfile,
                                                      self.who(), self.allowed())
            self.reply(got)

        def download(self, sid: int):
            """GET /api/albums/<id>/download: the album's original files as one zip, written as it is read
            (nothing is held in memory or on disk). Names are the files' own, numbered when two are alike."""
            import zipfile
            with live.gate:              # only to say which files; the copying must not hold up a reload
                got, meta = changes.shas(sid), live.cat.meta
                files = [] if not got else [(images.original(s, meta[s]["ext"]),
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
                got = (browse.handle("GET", u.path, qs, None, self.headers)
                       or admin.handle("GET", u.path, qs, None, self.who(), self.allowed())
                       or images.handle("GET", u.path, qs, None))
                if got:
                    return self.reply(got)
                c = live.cat
                if u.path == "/api/info":
                    return self.json(c.info)
                if u.path == "/api/sync":   # what changed since a device last asked (sync.py)
                    if not feed.ready:
                        self.extra_headers = {"Retry-After": "5"}
                        return self.json({"error": "starting"}, 503)
                    since = one("since", "0")
                    ids, deleted, (ver, after), more = feed.page(one("epoch"), int(since) if since.isdigit() else 0,
                                                                 one("after"), max(1, min(int(one("limit", "2000")), 10000)))
                    rows = [dict(sha=s, **c.feed_row(s)) for s in ids if s in c.meta]
                    return self.json({"epoch": feed.epoch, "photos": rows, "deleted": deleted, "since": ver, "after": after,
                                      "more": more, "total": c.info["total"], "trips": [] if more else c.trip_list})
                if u.path in app_files:
                    body = app_files[u.path].read_bytes()
                    return self.send(200, body, app_types[app_files[u.path].suffix], "no-cache",
                                     etag=f'"{hashlib.md5(body).hexdigest()}"')
                self.send(404, b"not found", "text/plain")
            except (BrokenPipeError, ConnectionResetError):
                pass

    def gated(fn):
        def run(self):
            if self.path.startswith(("/api/upload/", "/api/intake", "/setup")) or self.path.endswith("/download"):   # touch none of the above (a download: only briefly, itself); can be slow
                return fn(self)
            with live.gate:
                return fn(self)
        return run

    for _name in ("do_GET", "do_POST"):
        setattr(Handler, _name, gated(getattr(Handler, _name)))

    # Favourites set in the app. The catalog's own flag (assets.favorited) is what the photo's source said,
    # and `estate.py link` writes it again on every ingest, so what a person says here is kept beside it
    # (the `favorites` table) and wins. It shows at once: the photo's row, the count and every cached
    # order and cover are brought up to date here rather than by re-reading the catalog (catalog.load reads
    # the table too, so a reload agrees). `fav_log` is every one set since the start, so a reload that
    # read the table a moment too early can be put right as it is swapped in.
    fav_lock, fav_log = threading.Lock(), []

    def set_favorites(shas, on: bool, who: str) -> int:
        """Record them, show them, tell the phones. Returns how many photos it changed."""
        c = live.cat
        shas = [s for s in dict.fromkeys(shas) if s in c.index]
        with fav_lock:
            changed = [s for s in shas if bool(c.fav[c.index[s]]) != on]
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
            c.show_favorites((s, on) for s in shas)
            browse.forget()            # covers and "favourites only" lists were worked out without them
            admin.forget_library()
        if feed.ready and changed:
            feed.touch({s: c.feed_row(s) for s in changed if c.visible[c.index[s]]})
        return len(changed)

    def set_favorite(sha: str, on: bool, who: str) -> int:
        """One photo. Returns how many favourites there now are."""
        set_favorites([sha], on, who)
        return live.cat.info["fav"]

    # Re-reading the catalog is in one of three states: idle, running, or rerun (asked again while
    # running: when this run ends, another starts, so nothing that was asked for is missed).
    reload_state = {"state": "idle", "at": None, "seconds": None, "error": None}
    reload_lock = threading.Lock()

    def reload_catalog() -> None:
        """Re-read the catalog and swap it in, with no restart. The new catalog is built on the side while the
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
                    fresh = load_catalog(live.cat.raw)
                    if os.environ.get("PHOTO_ESTATE_CHECK_RELOAD"):   # tools/bench_ingest.py --check
                        print(f"reload check: {fresh.differences(load_catalog()) or 'same as a full load'}", flush=True)
                    live.install(fresh, then=lambda cat: cat.show_favorites(fav_log[mark:]))   # set while this load was reading
                    browse.forget()      # cached masks and rankings are the old catalog's
                    admin.forget_library()
                    reload_state.update(at=time.time(), seconds=time.time() - t, error=None)
                    print(f"catalog reloaded: {fresh.N} images in {time.time() - t:.1f}s", flush=True)
                    feed_update()
                    if not a.no_pregenerate:
                        threading.Thread(target=images.pregenerate, daemon=True).start()
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
            if live.cat.has_faces:
                load_people()
        finally:
            with settling_lock:
                settling["n"] -= 1

    def on_hidden_change():
        with settling_lock:
            settling["n"] += 1
        threading.Thread(target=hidden_changed, daemon=True).start()

    def shown(sha: str) -> dict:
        c = live.cat
        return {"library": sha in c.index and bool(c.visible[c.index[sha]]), "sync": sha in feed.rows}

    changes = Changes(a.db, facts=lambda sha: live.cat.meta.get(sha), shown=shown,
                      busy=lambda: settling["n"] > 0 or reload_state["state"] != "idle", on_change=on_hidden_change,
                      find=browse.find, favorite=set_favorites, cover=lambda ids: live.cat.album_cover(ids))

    # Tasks (tasks.py): a request in words, worked on by an agent on this computer that uses the MCP
    # endpoint below like any other client, and ends in a change set.
    tasks = Tasks(a.db, f"http://127.0.0.1:{port}/mcp",
                  command=a.agent_cmd, env_file=a.agent_env, model=a.agent_model, log=lambda msg: print(msg, flush=True))
    mcp = MCPServer(SimpleNamespace(find=browse.find, row=browse.row, faces=browse.faces, overview=browse.overview,
                                    changes=changes, tasks=tasks))

    if ingest:
        ingest.on_added = reload_catalog
        ingest.before_run = admin.arrivals_prepared
        ingest.on_faces = lambda: live.cat.has_faces and load_people()   # the run's faces are in: rebuild the People page
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
                             on_faces=lambda jid: (reload_catalog(), live.cat.has_faces and load_people()))
            e.start()
            return e
        intake = IntakeAPI(Library(Path(a.root or Path(a.db).resolve().parent), a.db, store, fixed=True), make_intake,
                           setup_mode=False, practice=bool(a.sandbox))

    if a.backup_dir and not a.demo:
        threading.Thread(target=backups, args=(a.db, Path(a.backup_dir), a.backup_every, a.backup_keep, t0),
                         daemon=True).start()
    if not a.no_pregenerate:
        threading.Thread(target=images.pregenerate, daemon=True).start()

    class Server(ThreadingHTTPServer):
        # The default listen backlog is 5. Behind `tailscale serve`, every HTTP/2 stream becomes its own
        # connection here, so a screenful of thumbnails overflows it and the proxy answers 502.
        request_queue_size = 256
        daemon_threads = True

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
