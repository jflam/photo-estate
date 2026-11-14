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


def save_upload(inbox: Path, rel: str, fp, length: int, sha256: str | None = None) -> tuple[int, str]:
    """Stream `length` bytes from fp to <inbox>/<rel>. Returns (http status, message).

    Idempotent: sending a file that is already there, with the same size, is a 200. The same path
    with a different size is a 409, because ingest keys on the path and must never see it change.
    `sha256`, when given, is checked against what arrived."""
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
        return 201, "stored"
    finally:
        tmp.unlink(missing_ok=True)


# ----------------------------------------------------------------------------- running the ingest

class IngestJob:
    """Runs ingest-iphone.sh from the web page, one run at a time, and keeps its output to show.

    The script does the work (ingest, link, exif, places, embeddings, faces); this only starts it,
    follows its output and says what is waiting in the inbox. It is started with fixed arguments, so
    nothing a client sends reaches the command line."""

    def __init__(self, script: Path, root: Path, db: str, store: Path, inbox: Path):
        import collections
        import threading
        self.script, self.root, self.db, self.store, self.inbox = script, root, db, store, inbox
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

    def _follow(self, proc) -> None:
        for line in proc.stdout:
            self.log.append(line.rstrip())
        code = proc.wait()
        with self.lock:
            self.code, self.finished = code, time.time()
            if code == 0 and any("new assets and sidecars" in x for x in self.log):
                self.fresh = True
