"""
admin.py — looking after the photo app (`embed.py serve`): photos arriving from the phone, the ingest
that catalogues them, restarting, the Library info page, and the catalog's backups.

  PUT|POST /api/upload/<YYYY/MM/DD/name.ext>   one file from the phone into the inbox (upload.py)
  OPTIONS  /api/upload/...                     501: send each file in one request (no resumable uploads)
  GET      /api/ingest                         the ingest's state (upload.IngestJob)
  POST     /api/ingest                         run ingest-iphone.sh now
  POST     /api/restart                        relaunch the app (only under launchd, which starts it again)
  GET      /stats                              the Library info page (stats.html)
  GET      /api/stats[?refresh=1]              what the library is and what it costs (stats.py)

  Admin.handle(method, path, qs, body, who, allowed) -> (status, body, content type, Cache-Control[, headers]) or None
  Admin.upload(method, path, headers, rfile, who, allowed) -> (that, whether to close the connection)
  Admin.arrivals_prepared()     wait for uploaded photos' thumbnails and embeddings (the ingest's before_run)
  Admin.forget_library()        the library numbers on /stats are out of date (a reload, a favourite)
  backups(...)                  --backup-dir: a copy of the catalog every --backup-every hours

Writing (uploading, ingesting, restarting) takes the app's write rule: a --upload-user login or a request from
this computer (photo_app.ROUTE_AUTH). `allowed` says whether the caller passes it.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import sqlite3
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

from PIL import Image, ImageOps

import pages
from core.media import DECODABLE_EXT
from catalog import Live
from estate import facts_of as estate_facts
from images import THUMB_EXT, write_image
from upload import save_upload, staging_dir



def js(o, code: int = 200):
    return code, json.dumps(o, separators=(",", ":")).encode(), "application/json", "no-store"


NOT_FOUND = (404, b"not found", "text/plain", "no-store")


class Admin:
    def __init__(self, live: Live, encoder, connect, now, *, db: str, model: str, store: Path, cache: Path, seed: Path,
                 inbox: Path | None, ingest, query_log: Path, demo: bool, faces_ready):
        self.live, self.encoder, self.connect, self.now = live, encoder, connect, now
        self.db, self.model, self.store, self.cache, self.seed = db, model, store, cache, seed
        self.inbox, self.ingest, self.qlog, self.demo, self.faces_ready = inbox, ingest, query_log, demo, faces_ready
        self.under_launchd = os.environ.get("XPC_SERVICE_NAME", "0") not in ("", "0")   # the job's label, only when launchd started us
        # /stats: the library numbers come from memory and change only with the catalog. The disk numbers
        # walk the cache folders and read the catalog, which takes a couple of minutes on a hard disk, so
        # they are measured once, saved next to the catalog (stats-cache.json) and only measured again
        # when you press Recompute.
        self.started = time.time()
        self.st = {"lib": None, "disk": None, "at": 0.0, "running": False, "error": None}
        self.st_lock, self.st_gen = threading.Lock(), 0   # st_gen: forget_library()s so far
        self.st_file = Path(db).resolve().parent / "stats-cache.json"
        try:
            saved = json.loads(self.st_file.read_text())
            self.st["disk"], self.st["at"] = saved["disk"], saved["at"]
        except (OSError, ValueError, KeyError):
            pass
        self.arrivals = queue.Queue()
        for _ in range(3 if inbox else 0):
            threading.Thread(target=self.prepare_arrivals, daemon=True).start()

    # -- http -----------------------------------------------------------------------------------

    def handle(self, method: str, path: str, qs: dict, body, who: str = "", allowed: bool = True):
        """One request -> (status, body bytes, content type, Cache-Control[, headers]), or None if it isn't ours."""
        if method == "OPTIONS" and path.startswith("/api/upload"):
            # The phone's background uploader asks first whether the server can resume interrupted uploads.
            # It can't: 501 means "send each file in one request".
            return 501, b"", "text/plain", "no-store", {"Allow": "OPTIONS, PUT, POST"}
        if method == "POST" and path in ("/api/ingest", "/api/restart"):
            return self.admin(path, who, allowed)
        if method != "GET":
            return None
        if path in ("/stats", "/stats.html"):
            page = pages.page("stats.html")
            return 200, page, "text/html; charset=utf-8", "no-cache", {"ETag": f'"{hashlib.md5(page).hexdigest()}"'}
        if path == "/api/ingest":
            return js(self.ingest.status() | {"can_restart": self.under_launchd} if self.ingest else {"enabled": False})
        if path == "/api/stats":
            return js(self.stats_payload((qs.get("refresh") or [""])[0] == "1"))
        return None

    def admin(self, path: str, who: str, allowed: bool):
        """POST /api/ingest runs ingest-iphone.sh; POST /api/restart relaunches the app (launchd only)."""
        if self.ingest is None:
            return NOT_FOUND
        if not allowed:
            return 403, b"not allowed", "text/plain", "no-store"
        if path == "/api/ingest":
            started = self.ingest.start()
            return js({"started": started}, 202 if started else 409)
        if not self.under_launchd:   # run by hand, nothing would bring it back
            return js({"error": "not running under launchd"}, 409)
        print(f"restart requested by {who or 'local'}", flush=True)
        threading.Timer(0.5, os._exit, (0,)).start()   # KeepAlive starts it again
        return js({"restarting": True}, 202)

    def upload(self, method: str, path: str, headers, rfile, who: str, allowed: bool):
        """PUT or POST /api/upload/<YYYY/MM/DD/name.ext>: one file from the phone into the inbox (upload.py).
        Returns the answer and whether to close the connection: every early answer leaves the body unread."""
        prefix = "/api/upload/"
        if not path.startswith(prefix) or self.inbox is None:
            return NOT_FOUND, True
        if not allowed:
            return (403, b"not allowed", "text/plain", "no-store"), True
        try:
            length = int(headers.get("Content-Length", ""))
        except ValueError:
            return (411, b"Content-Length required", "text/plain", "no-store"), True
        close = True
        try:
            h = headers
            meta = {"taken": h.get("X-Taken"), "lat": h.get("X-Latitude"), "lon": h.get("X-Longitude"),
                    "fav": h.get("X-Favorite")}
            stored = {}
            code, msg = save_upload(self.inbox, unquote(path[len(prefix):]), rfile, length,
                                    h.get("X-Content-SHA256"), meta, stored)
            if stored and stored["path"].suffix.lower() in DECODABLE_EXT:   # an upload's stills, less DNG
                self.arrivals.put((stored["sha256"], stored["path"]))
            close = code >= 400   # a refused body may still be on the wire
        except OSError as e:
            code, msg = 500, f"could not write: {e.strerror}"
        if code == 201:
            print(f"upload: {method} {path[len(prefix):]} ({length / 1e6:.1f} MB) from {who or 'local'}", flush=True)
        if self.ingest and code in (200, 201):
            self.ingest.poke(headers.get("X-Batch"), path[len(prefix):], new=code == 201)
        return js({"status": msg}, code), close

    # -- photos arriving from the phone ---------------------------------------------------------
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

    def prepare_arrivals(self) -> None:
        assert self.inbox is not None    # these threads run only with an inbox
        inbox, con = self.inbox, None
        while True:
            sha, path = self.arrivals.get()
            try:
                dest = self.cache / "thumbs" / f"{sha}{THUMB_EXT}"
                facts = staging_dir(inbox) / "facts" / f"{sha}.json"
                con = con or self.connect(self.db)
                embedded = con.execute("SELECT 1 FROM embeddings WHERE sha256=? AND model=?", (sha, self.model)).fetchone()
                if dest.exists() and embedded:
                    continue
                self.encoder.wait(None)   # this worker holds no gate: wait the load out (it ends: ready or failed)
                side = self.encoder.side
                with Image.open(path) as raw:
                    raw.draft("RGB", (side * 2, side * 2))   # as _decode does, so the vector is the same
                    im = ImageOps.exif_transpose(raw).convert("RGB")
                    if raw.format == "HEIF":   # draft() does nothing for HEIC, so these are image_facts' own
                        facts.parent.mkdir(parents=True, exist_ok=True)
                        tmp = facts.with_suffix(f".{threading.get_ident()}.tmp")
                        tmp.write_text(json.dumps(estate_facts(raw, im)))
                        os.replace(tmp, facts)
                    if not embedded:
                        vec = self.encoder.image(im.resize((side, side), Image.Resampling.BICUBIC))
                        con.execute("INSERT OR REPLACE INTO embeddings VALUES (?,?,?,?,?,?)",
                                    (sha, self.model, vec.shape[0], vec.tobytes(), None, self.now()))
                        con.commit()
                    if not dest.exists():
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        im.thumbnail((360, 360))
                        write_image(im, dest)
            except Exception as e:     # `embed.py run` and the page cope without it
                print(f"upload: could not prepare {path.name}: {type(e).__name__}: {e}", flush=True)
            finally:
                self.arrivals.task_done()

    def arrivals_prepared(self, timeout: float = 120) -> None:
        """Wait (a while) for the workers to catch up, so the ingest finds every embedding already made."""
        with self.arrivals.all_tasks_done:
            self.arrivals.all_tasks_done.wait_for(lambda: not self.arrivals.unfinished_tasks, timeout)

    # -- /stats ---------------------------------------------------------------------------------

    def forget_library(self) -> None:
        with self.st_lock:
            self.st["lib"], self.st_gen = None, self.st_gen + 1

    def library_stats(self):
        """The library numbers, from the catalog being served. The caller holds the catalog gate (a request
        does; stats_job takes it just for this)."""
        import stats
        c = self.live.cat
        return stats.library_stats(c.meta, list(c.when), c.people_ix, c.trips, c.events)

    def disk_stats(self):
        import stats
        return stats.disk_stats(self.db, str(self.store), [self.cache] if self.cache == self.seed else [self.cache, self.seed])

    def stats_job(self) -> None:
        """Measure the disk again (stats_payload started it, with `running` set). `st` is only touched under
        st_lock, and the catalog only read inside the gate; neither is held for the slow walk of the disk.
        The gate is never taken with st_lock held (a request holds the gate, then st_lock)."""
        st = self.st
        try:
            with self.st_lock:
                need_lib, gen = st["lib"] is None, self.st_gen
            if need_lib:
                with self.live.gate:
                    lib = self.library_stats()
                with self.st_lock:
                    if st["lib"] is None and self.st_gen == gen:   # not if the catalog changed meanwhile
                        st["lib"] = lib
            disk, at = self.disk_stats(), time.time()
            with self.st_lock:
                st["disk"], st["at"], st["error"] = disk, at, None
            try:
                tmp = self.st_file.with_suffix(".tmp")
                tmp.write_text(json.dumps({"at": at, "disk": disk}))
                os.replace(tmp, self.st_file)
            except OSError:
                pass
        except Exception as e:  # the page says so; the rest of the app is unaffected
            with self.st_lock:
                st["error"] = f"{type(e).__name__}: {e}"
        finally:
            with self.st_lock:
                st["running"] = False

    def assets_now(self):
        try:
            c = sqlite3.connect(self.db, timeout=10)
            try:
                return c.execute("SELECT count(*) FROM assets").fetchone()[0]
            finally:
                c.close()
        except sqlite3.Error:
            return None

    def stats_payload(self, refresh: bool):
        """GET /api/stats, from a request (which holds the catalog gate). One reading of `st`, under st_lock."""
        st = self.st
        with self.st_lock:
            if st["lib"] is None:
                st["lib"] = self.library_stats()
            if not st["running"] and (refresh or st["disk"] is None):
                st["running"] = True
                threading.Thread(target=self.stats_job, daemon=True).start()
            snap = dict(st)
        import stats
        qlines = qsize = None
        try:
            qsize = self.qlog.stat().st_size
            if qsize < 64 << 20:
                with self.qlog.open("rb") as f:
                    qlines = sum(1 for _ in f)
        except OSError:
            pass
        return {"state": "working" if snap["running"] else "error" if snap["error"] and not snap["disk"] else "ready",
                "error": snap["error"], "computed_at": snap["at"] or None, "library": snap["lib"], "disk": snap["disk"],
                "catalog_assets_now": self.assets_now(),
                "app": {"uptime_s": round(time.time() - self.started), "rss_bytes": stats.rss_bytes(), "model": self.model,
                        "demo": bool(self.demo), "python": sys.version.split()[0], "faces_ready": self.faces_ready(),
                        "query_log_bytes": qsize, "query_log_lines": qlines, "cache_dir": str(self.cache),
                        "store": str(self.store), "db": str(self.db)}}


# --backup-dir: a copy of the catalog (and the face-label journal) every --backup-every hours, kept
# --backup-keep deep. Done by the app, not by a separate job, because this process already has macOS's
# permission to write the estate disk; a new launchd job would wait on an "Allow" click. The copy is
# taken with SQLite's backup API, so it is consistent while the app and an ingest keep writing.

def backup_once(db: str, dest: Path, keep: int) -> None:
    import shutil
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    t = time.time()
    for left in dest.glob(".estate.db.*.tmp"):    # from a copy that was cut short
        left.unlink(missing_ok=True)
    tmp = dest / f".estate.db.{stamp}.tmp"
    src, out = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60), sqlite3.connect(tmp)
    try:
        src.backup(out)        # one step: one read transaction, so writers carry on (WAL)
        out.execute("PRAGMA journal_mode=DELETE")   # one self-contained file, openable read-only
    finally:
        out.close()
        src.close()
    os.replace(tmp, dest / f"estate.db.{stamp}")
    labels = Path(db).resolve().parent / "face-labels.jsonl"
    if labels.exists():
        shutil.copy2(labels, dest / f"face-labels.jsonl.{stamp}")
    for pattern in ("estate.db.*", "face-labels.jsonl.*"):
        for old in sorted(dest.glob(pattern))[:-keep]:
            old.unlink(missing_ok=True)
    print(f"backup: catalog copied to {dest} in {time.time() - t:.0f}s", flush=True)


def backups(db: str, dest: Path, every_hours: float, keep: int, t0: float) -> None:
    """Forever: a backup whenever the newest one is --backup-every old (the first shortly after startup)."""
    every = every_hours * 3600
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
            backup_once(db, dest, keep)
        except Exception as e:  # try again in an hour
            print(f"backup failed: {type(e).__name__}: {e}", flush=True)
            time.sleep(3600)
