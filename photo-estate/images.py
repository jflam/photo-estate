"""
images.py — the photos themselves, as the photo app (`embed.py serve`) sends them.

  GET /thumb/<sha>      360 px JPEG, for the grid
  GET /preview/<sha>    1600 px JPEG, for the viewer
  GET /large/<sha>      4096 px JPEG, for big high-density displays, where a 1600 px preview is visibly soft
  GET /original/<sha>   the file as stored, for Save to Photos / share

  Images.handle(method, path, qs, body) -> (status, body, content type, Cache-Control[, headers]) or None
  Images.image(sha, kind, side)         a cached JPEG, made on first use
  Images.pregenerate()                  every missing thumbnail, once, in the background

Made images are cached under <cache>/<kind>/<sha>.jpg. On the Mac mini the estate disk is a USB hard
disk: a cold thumbnail costs a ~9 ms seek, so a screenful of 150 takes most of a second. With
--fast-cache, `cache` is on the internal SSD and is filled from the disk's cache (`seed`) on first use,
decoding the original only if neither has it. Every image is named by the original's hash, so a
browser never needs to ask for it again.
"""
from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import quote

import pillow_heif
from PIL import Image, ImageOps

from catalog import Live

pillow_heif.register_heif_opener()
Image.MAX_IMAGE_PIXELS = None

sha_re = re.compile(r"^[0-9a-f]{64}$")
original_types = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".heic": "image/heic", ".png": "image/png",
                  ".gif": "image/gif", ".webp": "image/webp"}
SIZES = (("/thumb/", "thumbs", 360), ("/preview/", "previews", 1600), ("/large/", "large", 4096))
FOREVER = "public, max-age=2592000, immutable"


def _thumb(job: tuple[str, str, int]) -> bool:
    """Worker: write one JPEG thumbnail. Never raises."""
    src, dest, side = job
    try:
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


class Images:
    def __init__(self, live: Live, store: Path, cache: Path, seed: Path):
        self.live, self.store, self.cache, self.seed = live, store, cache, seed
        self.pregen_lock = threading.Lock()

    def original(self, sha: str, ext: str) -> Path:
        return self.store / sha[:2] / sha[2:4] / f"{sha}{ext}"

    def image(self, sha: str, kind: str, side: int) -> bytes | None:
        cache, seed = self.cache, self.seed
        dest = cache / kind / f"{sha}.jpg"
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            if cache != seed and (seed / kind / f"{sha}.jpg").exists():
                body = (seed / kind / f"{sha}.jpg").read_bytes()
                tmp = dest.with_suffix(f".{threading.get_ident()}.tmp")
                tmp.write_bytes(body)
                os.replace(tmp, dest)
                return body
            m = self.live.cat.meta[sha]
            try:
                with Image.open(self.original(sha, m["ext"])) as im:
                    im.draft("RGB", (side, side))
                    im = ImageOps.exif_transpose(im).convert("RGB")
                    im.thumbnail((side, side))
                    tmp = dest.with_suffix(f".{threading.get_ident()}.tmp")
                    im.save(tmp, "JPEG", quality=90 if side > 2000 else 82)
                    os.replace(tmp, dest)
            except Exception:
                return None
        return dest.read_bytes()

    def handle(self, method: str, path: str, qs: dict, body):
        """One request -> (status, body bytes, content type, Cache-Control[, headers]), or None if it isn't ours."""
        if method != "GET":
            return None
        meta = self.live.cat.meta
        if path.startswith("/original/"):
            sha = path[len("/original/"):]
            m = meta.get(sha) if sha_re.match(sha) else None
            src = m and self.original(sha, m["ext"])
            if not m or m["ext"] not in original_types or not src.exists():
                return 404, b"", "text/plain", "no-store"
            name = (m["path"] or "").rsplit("/", 1)[-1] or f"{sha[:12]}{m['ext']}"
            return (200, src.read_bytes(), original_types[m["ext"]], FOREVER,
                    {"Content-Disposition": f"inline; filename*=UTF-8''{quote(name)}"})
        for prefix, kind, side in SIZES:
            if path.startswith(prefix):
                sha = path[len(prefix):]
                if sha_re.match(sha) and sha in meta and (got := self.image(sha, kind, side)):
                    return 200, got, "image/jpeg", FOREVER
                return 404, b"", "text/plain", "no-store"
        return None

    def pregenerate(self) -> None:
        """Every thumbnail once, so scrolling results never waits on HEIC decoding. One run at a time."""
        if not self.pregen_lock.acquire(blocking=False):
            return
        try:
            self._pregenerate()
        finally:
            self.pregen_lock.release()

    def _pregenerate(self) -> None:
        cache, seed, c = self.cache, self.seed, self.live.cat
        (cache / "thumbs").mkdir(parents=True, exist_ok=True)
        have = {p.name[:64] for p in (cache / "thumbs").iterdir()}
        if cache != seed and (seed / "thumbs").is_dir():  # copy what the disk's cache has, newest first
            seeded = {p.name[:64] for p in (seed / "thumbs").iterdir()} - have
            copy = [s for s in (c.shas[i] for i in c.newest) if s in seeded]
            if copy:
                print(f"copying {len(copy)} thumbnails to {cache}", flush=True)
                t = time.time()
                for s in copy:
                    self.image(s, "thumbs", 360)
                print(f"thumbnails: {len(copy)} copied in {time.time() - t:.0f}s", flush=True)
            have |= seeded
        todo = [(str(self.original(s, c.meta[s]["ext"])), str(cache / "thumbs" / f"{s}.jpg"), 360)
                for s in c.shas if s not in have]
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
