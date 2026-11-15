"""upload.py — receive photos from the phone into an inbox folder (standard library only).

The photo app's `PUT /api/upload/<path>` hands the request body to `save_upload`. The inbox is a
plain folder mirror in the same shape as the iCloud mirror, so `estate.py ingest --force <inbox>`
picks it up like any other source:

    <inbox>/2026/11/14/IMG_0042-A1B2C3D.HEIC        the original, byte for byte
    <inbox>/2026/11/14/IMG_0042-A1B2C3D.HEIC.xmp    its metadata (date with offset, GPS, favourite)

The server never writes the catalog: it only drops files here. That keeps `ingest` the single
writer of the database, and a half-finished upload can never be mistaken for a photo, because a file
is written beside the inbox and renamed into it only once every byte has arrived.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path, PurePosixPath

MEDIA_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".webp", ".tif", ".tiff", ".dng", ".avif",
             ".mp4", ".mov", ".m4v", ".3gp"}
ALLOWED_EXT = MEDIA_EXT | {".xmp"}
MAX_BYTES = 8 << 30          # a long 4K video is a few GB
PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _().+,'=-]*$")   # one path segment: no dots first, no slashes


def clean_path(rel: str) -> PurePosixPath | None:
    """The inbox-relative path a client asked for, or None if it could point anywhere else."""
    rel = rel.strip("/")
    parts = rel.split("/")
    if not 1 <= len(parts) <= 6 or not all(PART.match(p) and not p.endswith(".") for p in parts):
        return None
    p = PurePosixPath(rel)
    ext = p.suffix.lower()
    if ext not in ALLOWED_EXT:
        return None
    if ext == ".xmp" and PurePosixPath(p.stem).suffix.lower() not in MEDIA_EXT:
        return None          # a sidecar is X.HEIC.xmp: it names the file it describes
    return p


def staging_dir(inbox: Path) -> Path:
    """Where uploads are written before they are renamed in: next to the inbox, never inside it
    (ingest walks the whole inbox, hidden folders included)."""
    return inbox.with_name(inbox.name + ".partial")


TAKEN = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(Z|[+-]\d\d:?\d\d)$")


def xmp_for(meta: dict) -> str | None:
    """The sidecar `link` reads (flat tags, as icloudpd writes them) from what the phone sent about a
    photo: {"taken": ISO 8601 with offset, "lat", "lon", "fav"}. None if nothing usable came."""
    tags = []
    if TAKEN.match(meta.get("taken") or ""):
        t = meta["taken"]
        t = t[:-1] + "+0000" if t.endswith("Z") else t[:-3] + t[-2:] if t[-3] == ":" else t
        tags.append(f"<photoshop:DateCreated>{t}</photoshop:DateCreated>")
    try:
        lat, lon = float(meta["lat"]), float(meta["lon"])
        if -90 <= lat <= 90 and -180 <= lon <= 180 and (lat or lon):
            tags += [f"<exif:GPSLatitude>{lat:.6f}</exif:GPSLatitude>", f"<exif:GPSLongitude>{lon:.6f}</exif:GPSLongitude>"]
    except (KeyError, TypeError, ValueError):
        pass
    if str(meta.get("fav")).lower() in ("1", "true", "yes"):
        tags.append("<xmp:Rating>5</xmp:Rating>")
    if not tags:
        return None
    return ('<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
            '<rdf:Description xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/" '
            'xmlns:exif="http://ns.adobe.com/exif/1.0/" xmlns:xmp="http://ns.adobe.com/xap/1.0/">'
            + "".join(tags) + "</rdf:Description></rdf:RDF></x:xmpmeta>\n")


def save_upload(inbox: Path, rel: str, fp, length: int, sha256: str | None = None,
                meta: dict | None = None, stored: dict | None = None) -> tuple[int, str]:
    """Stream `length` bytes from fp to <inbox>/<rel>. Returns (http status, message).

    `meta` (taken/lat/lon/fav, from request headers) becomes the file's `.xmp` sidecar, written after
    the file, because background uploads from the phone send one resource per request.

    Idempotent: sending a file that is already there, with the same size, is a 200. The same path
    with a different size is a 409, because ingest keys on the path and must never see it change.
    `sha256`, when given, is checked against what arrived. On a 201, `stored` (if given) gets the file's
    "path" and "sha256", which is the key the catalog will give it, so the app can prepare it early."""
    p = clean_path(rel)
    if p is None:
        return 400, "bad path"
    if not 0 <= length <= MAX_BYTES:
        return 413 if length > MAX_BYTES else 400, "bad length"
    dest = inbox / p
    if dest.exists():
        return (200, "exists") if dest.stat().st_size == length else (409, "path exists with different content")
    stage = staging_dir(inbox)
    stage.mkdir(parents=True, exist_ok=True)
    tmp = stage / f"{os.getpid()}.{time.time_ns()}"
    h, got = hashlib.sha256(), 0
    try:
        with open(tmp, "wb") as out:
            while got < length:
                chunk = fp.read(min(1 << 20, length - got))
                if not chunk:
                    break
                h.update(chunk)
                out.write(chunk)
                got += len(chunk)
            out.flush()
            os.fsync(out.fileno())
        if got != length:
            return 400, f"connection ended after {got} of {length} bytes"
        if sha256 and h.hexdigest() != sha256.lower():
            return 400, "sha256 does not match the bytes received"
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, dest)
        if stored is not None:
            stored.update(path=dest, sha256=h.hexdigest())
        xmp = xmp_for(meta or {}) if dest.suffix.lower() in MEDIA_EXT else None
        if xmp:
            side = dest.with_name(dest.name + ".xmp")
            side_tmp = stage / f"{os.getpid()}.{time.time_ns()}.xmp"
            side_tmp.write_text(xmp)
            os.replace(side_tmp, side)
        return 201, "stored"
    finally:
        tmp.unlink(missing_ok=True)


# ----------------------------------------------------------------------------- running the ingest

class IngestJob:
    """Runs ingest-iphone.sh from the web page, one run at a time, and keeps its output to show.

    The script does the work (ingest, link, exif, places, embeddings, faces); this only starts it,
    follows its output and says what is waiting in the inbox. It is started with fixed arguments, so
    nothing a client sends reaches the command line."""

    def __init__(self, script: Path, root: Path, db: str, store: Path, inbox: Path,
                 quiet: float = 60.0, max_wait: float = 600.0):
        import collections
        import threading
        self.script, self.root, self.db, self.store, self.inbox = script, root, db, store, inbox
        self.quiet, self.max_wait = quiet, max_wait   # seconds; quiet = 0 turns the automatic runs off
        self.on_added = None      # called once a run that added photos has them searchable (the app reloads its catalog)
        self.on_faces = None      # called when that run has also finished faces (the app reloads its People page)
        self.timer = None
        self.first_poke = None    # when the current burst of uploads began
        self.again = None         # an upload arrived while a run was going: seconds to wait after it ends
        self.batches = {}         # batch id -> (photos the phone said it is sending, paths that arrived)
        self.failures = 0
        self.lock = threading.Lock()
        self.log = collections.deque(maxlen=300)
        self.proc = None
        self.started = self.finished = self.code = None
        self.fresh = False        # a run added photos, so the app has not loaded them yet

    def waiting(self) -> dict:
        """Photos and videos in the inbox that the catalog has not recorded yet."""
        import sqlite3
        have = set()
        try:
            c = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True, timeout=10)
            try:
                have = {r[0] for r in c.execute("SELECT path FROM occurrences WHERE source=?", (self.inbox.name,))}
            finally:
                c.close()
        except sqlite3.Error:
            pass                  # no catalog yet: everything is waiting
        files = size = 0
        if self.inbox.is_dir():
            for p in self.inbox.rglob("*"):
                if p.suffix.lower() in MEDIA_EXT and p.is_file() and p.relative_to(self.inbox).as_posix() not in have:
                    files += 1
                    size += p.stat().st_size
        return {"files": files, "bytes": size}

    def status(self) -> dict:
        waiting = self.waiting()       # walks the inbox: not under the lock
        with self.lock:
            running = self.proc is not None and self.proc.poll() is None
            return {"enabled": True, "running": running, "started_at": self.started, "finished_at": self.finished,
                    "exit_code": self.code, "log": list(self.log), "restart_needed": self.fresh,
                    "waiting": waiting}

    def start(self) -> bool:
        """Start a run; False if one is already going."""
        import subprocess
        import threading
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                return False
            env = dict(os.environ, DB=str(self.db), STORE=str(self.store), INBOX=str(self.inbox))
            env.pop("RESTART", None)   # the page asks for a restart separately: it would kill this run's output
            self.log.clear()
            self.started, self.finished, self.code = time.time(), None, None
            self.proc = subprocess.Popen([str(self.script), str(self.root)], stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, text=True, env=env, stdin=subprocess.DEVNULL)
            threading.Thread(target=self._follow, args=(self.proc,), daemon=True).start()
            return True

    # -- running by itself: an upload starts (or restarts) a quiet period, and when it ends the ingest runs

    def poke(self, batch: str | None = None, path: str | None = None, new: bool = True) -> None:
        """An upload arrived. Run the ingest once uploads have been quiet for `quiet` seconds, but no later
        than `max_wait` after the first of the burst, so a long upload still gets catalogued as it goes.

        `batch` is the phone's X-Batch header, "<id>/<count>": the phone says how many files it is sending
        together, so when the last one is in there is nothing to wait for and the run starts at once. Files
        that were already here (`new` False) count towards the batch but start nothing by themselves. The
        quiet period stays the fallback for a batch that never completes (a failed upload, an old app)."""
        if not self.quiet:
            return
        with self.lock:
            done = self._batch_done(batch, path)
            if not new and not (done and (self.first_poke is not None or self.again is not None)):
                return
            delay = 0.0 if done else None
            if self.proc is not None and self.proc.poll() is None:   # picked up when the running one finishes
                self.again = min(x for x in (self.again, delay, self.quiet) if x is not None)
                return
            now = time.time()
            if self.first_poke is None:
                self.first_poke = now
            self._arm(delay if done else min(self.quiet, max(0.0, self.first_poke + self.max_wait - now)))

    def _batch_done(self, batch: str | None, path: str | None) -> bool:      # lock held
        m = re.match(r"^([A-Za-z0-9-]{1,64})/(\d{1,6})$", batch or "")
        if not m or path is None:
            return False
        bid, n = m.group(1), int(m.group(2))
        if bid not in self.batches:
            while len(self.batches) >= 64:               # forget the oldest: a batch that never finished
                self.batches.pop(next(iter(self.batches)))
            self.batches[bid] = (n, set())
        want, got = self.batches[bid]
        got.add(path)
        if len(got) < want:
            return False
        del self.batches[bid]
        return True

    def sweep(self) -> None:
        """At startup: uploads that were waiting when the app last stopped."""
        if self.quiet and self.waiting()["files"]:
            print("ingest: uploads waiting from before the start", flush=True)
            self.poke()

    def _arm(self, delay: float) -> None:      # lock held
        import threading
        if self.timer:
            self.timer.cancel()
        self.timer = threading.Timer(delay, self._fire)
        self.timer.daemon = True
        self.timer.start()

    def _fire(self) -> None:
        with self.lock:
            self.timer, self.first_poke = None, None
        print("ingest: uploads have gone quiet, cataloguing", flush=True)
        if not self.start():            # one is already going (started by hand): look again when it ends
            with self.lock:
                self.again = 0.0

    def _follow(self, proc) -> None:
        import threading
        added = told = False
        for line in proc.stdout:
            line = line.rstrip()
            self.log.append(line)
            added = added or "new assets and sidecars" in line
            if added and not told and line.endswith(" catalog ready"):   # searchable now; faces still to come
                told = True
                with self.lock:
                    self.fresh = True
                if self.on_added:
                    threading.Thread(target=self._tell, args=(self.on_added,), daemon=True).start()
        code = proc.wait()
        added = added and (told or code == 0)
        with self.lock:
            self.code, self.finished = code, time.time()
            if added and not told:
                self.fresh = True
            self.failures = 0 if code == 0 else self.failures + 1
            if code != 0:
                print(f"ingest failed (exit {code}), failure {self.failures}", flush=True)
            if self.quiet:
                if code != 0 and self.failures < 3:
                    self._arm(600)             # try again later, a few times
                elif self.again is not None:
                    self._arm(self.again)
            self.again = None
        if added and not told and self.on_added:     # a script without the "catalog ready" line
            self._tell(self.on_added)
        if added and code == 0 and self.on_faces:
            self._tell(self.on_faces)

    @staticmethod
    def _tell(fn) -> None:
        try:
            fn()
        except Exception as e:                 # the run itself succeeded
            print(f"ingest: reload after run failed: {e}", flush=True)
