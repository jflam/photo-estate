"""
images.py — the photos themselves, as the photo app (`embed.py serve`) sends them.

  GET /thumb/<sha>      360 px WebP, for the grid (a JPEG if it was made before 2026-11-20 and not yet redone)
  GET /preview/<sha>    1600 px JPEG, for the viewer
  GET /large/<sha>      4096 px JPEG, for big high-density displays, where a 1600 px preview is visibly soft
  GET /original/<sha>   the file as stored, for Save to Photos / share

  Images.handle(method, path, qs, body) -> (status, body, content type, Cache-Control[, headers]) or None
  Images.image(sha, kind, side)         (a cached image, its type), made on first use
  Images.pregenerate()                  every missing thumbnail, then every missing preview, in the background

Made images are cached under <cache>/<kind>/<sha>.webp for thumbnails, <sha>.jpg for the rest. On the Mac mini the estate disk is a USB hard
disk: a cold thumbnail costs a ~9 ms seek, so a screenful of 150 takes most of a second. With
--fast-cache, `cache` is on the internal SSD and is filled from the disk's cache (`seed`) on first use,
decoding the original only if neither has it. Every image is named by the original's hash, so a
browser never needs to ask for it again.
"""
from __future__ import annotations

import os
import re
import shutil
import threading
import time
from pathlib import Path
from urllib.parse import quote

import pillow_heif
from PIL import Image, ImageOps

from catalog import Live
from core.media import ORIGINAL_TYPES

pillow_heif.register_heif_opener()
Image.MAX_IMAGE_PIXELS = None

sha_re = re.compile(r"^[0-9a-f]{64}$")
original_types = ORIGINAL_TYPES   # .jpg .jpeg .png .heic .heif .gif .webp .tif .tiff .avif (core/media.py)
SIZES = (("/thumb/", "thumbs", 360), ("/preview/", "previews", 1600), ("/large/", "large", 4096))
FOREVER = "public, max-age=2592000, immutable"
# Grid thumbnails are WebP since 2026-11-20 (#104). Measured on 300 of the library's photos (tools/thumb_formats.py):
# WebP 78 is the lowest quality at least as close to the uncompressed thumbnail as the JPEG at 82 was, by SSIM
# (0.9697 to 0.9686) and by its worst 1% (0.834 to 0.829), in 70% of the bytes (16.3 KB to 24.5 KB, median).
# AVIF needed quality 70 to match, at 76% of the bytes, and was 2-3x slower to make. A thumbnail made before is
# a JPEG: it is served until the background job has made the WebP.
THUMB_EXT, THUMB_QUALITY = ".webp", 78
TYPES = {".webp": "image/webp", ".jpg": "image/jpeg"}


PREVIEW_WORKERS = max(1, min(3, (os.cpu_count() or 4) // 3))   # a third of the cores: the app stays quick
PREVIEW_BATCH = 2000             # previews made between checks for free space and for a newer run
PREVIEW_ROOM = 20 << 30          # stop making previews while the cache's disk has less than this free


def _lower_priority() -> None:
    os.nice(10)


def ext_of(kind: str) -> str:
    """The file a cached image of this kind is kept in: WebP for the grid's thumbnails, JPEG for the rest."""
    return THUMB_EXT if kind == "thumbs" else ".jpg"


def write_image(im: Image.Image, dest: Path | str) -> None:
    """Save an already resized image to dest, in the format its name says, atomically (a temp file, then
    os.replace), so a reader never sees half a file."""
    dest = Path(dest)
    tmp = dest.with_name(f"{dest.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    if dest.suffix == ".webp":
        im.save(tmp, "WEBP", quality=THUMB_QUALITY, method=6)
    else:
        im.save(tmp, "JPEG", quality=90 if max(im.size) > 2000 else 82)
    os.replace(tmp, dest)


def _thumb(job: tuple[str, str, int]) -> bool:
    """Worker: write one thumbnail or preview, by its side and the name it is given. Never raises."""
    src, dest, side = job
    try:
        with Image.open(src) as im:
            im.draft("RGB", (side, side))
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((side, side))
            write_image(im, dest)
        return True
    except Exception:
        return False


class Images:
    def __init__(self, live: Live, store: Path, cache: Path, seed: Path):
        self.live, self.store, self.cache, self.seed = live, store, cache, seed
        self.pregen_lock = threading.Lock()       # guards the two flags below
        self.pregen_running = self.pregen_again = False

    def original(self, sha: str, ext: str) -> Path:
        return self.store / sha[:2] / sha[2:4] / f"{sha}{ext}"

    def image(self, sha: str, kind: str, side: int) -> tuple[bytes, str] | None:
        """(the image, its content type), made on first use, or None if the original cannot be read."""
        cache, seed = self.cache, self.seed
        name = f"{sha}{ext_of(kind)}"
        dest = cache / kind / name
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            if cache != seed and (seed / kind / name).exists():
                body = (seed / kind / name).read_bytes()
                tmp = dest.with_name(f"{name}.{threading.get_ident()}.tmp")
                tmp.write_bytes(body)
                os.replace(tmp, dest)
                return body, TYPES[dest.suffix]
            if kind == "thumbs":   # a JPEG thumbnail from before WebP: good enough until the job replaces it
                for old in (cache / kind / f"{sha}.jpg", seed / kind / f"{sha}.jpg"):
                    if old.exists():
                        return old.read_bytes(), "image/jpeg"
            m = self.live.cat.meta[sha]
            try:
                with Image.open(self.original(sha, m["ext"])) as im:
                    im.draft("RGB", (side, side))
                    im = ImageOps.exif_transpose(im).convert("RGB")
                    im.thumbnail((side, side))
                    write_image(im, dest)
            except Exception:
                return None
        return dest.read_bytes(), TYPES[dest.suffix]

    def handle(self, method: str, path: str, qs: dict, body):
        """One request -> (status, body bytes, content type, Cache-Control[, headers]), or None if it isn't ours."""
        if method != "GET":
            return None
        meta = self.live.cat.meta
        if path.startswith("/original/"):
            sha = path[len("/original/"):]
            m = meta.get(sha) if sha_re.match(sha) else None
            src = self.original(sha, m["ext"]) if m else None
            if not m or src is None or m["ext"] not in original_types or not src.exists():
                return 404, b"", "text/plain", "no-store"
            name = (m["path"] or "").rsplit("/", 1)[-1] or f"{sha[:12]}{m['ext']}"
            return (200, src.read_bytes(), original_types[m["ext"]], FOREVER,
                    {"Content-Disposition": f"inline; filename*=UTF-8''{quote(name)}"})
        for prefix, kind, side in SIZES:
            if path.startswith(prefix):
                sha = path[len(prefix):]
                if sha_re.match(sha) and sha in meta and (got := self.image(sha, kind, side)):
                    return 200, got[0], got[1], FOREVER
                return 404, b"", "text/plain", "no-store"
        return None

    def pregenerate(self) -> None:
        """Every missing thumbnail, then every missing preview, in the background. One run at a time: a call
        while one is under way makes it start again once it can (the preview pass gives way between batches,
        so a new photo's thumbnail never waits hours behind it)."""
        with self.pregen_lock:
            if self.pregen_running:
                self.pregen_again = True
                return
            self.pregen_running = True
        while True:
            try:
                self._thumbs()
                self._previews()
            except Exception as e:     # never takes the app down; the images are still made on first use
                print(f"pregenerate: {type(e).__name__}: {e}", flush=True)
            with self.pregen_lock:
                if not self.pregen_again:
                    self.pregen_running = False
                    return
                self.pregen_again = False

    def _thumbs(self) -> None:
        cache, seed, c = self.cache, self.seed, self.live.cat
        (cache / "thumbs").mkdir(parents=True, exist_ok=True)
        have = {p.name[:64] for p in (cache / "thumbs").iterdir() if p.suffix == THUMB_EXT}
        if cache != seed and (seed / "thumbs").is_dir():  # copy what the disk's cache has, newest first
            seeded = {p.name[:64] for p in (seed / "thumbs").iterdir() if p.suffix == THUMB_EXT} - have
            copy = [s for s in (c.shas[i] for i in c.newest) if s in seeded]
            if copy:
                print(f"copying {len(copy)} thumbnails to {cache}", flush=True)
                t = time.time()
                for s in copy:
                    self.image(s, "thumbs", 360)
                print(f"thumbnails: {len(copy)} copied in {time.time() - t:.0f}s", flush=True)
            have |= seeded
        todo = [(str(self.original(s, c.meta[s]["ext"])), str(cache / "thumbs" / f"{s}{THUMB_EXT}"), 360)
                for s in (c.shas[i] for i in c.newest) if s not in have]
        if not todo:
            return
        print(f"making {len(todo)} thumbnails in the background", flush=True)
        t = time.time()
        if len(todo) <= 64:   # a few new photos: not worth starting worker processes
            ok = sum(_thumb(job) for job in todo)
            print(f"thumbnails: {ok} made in {time.time() - t:.0f}s", flush=True)
            return
        import multiprocessing as mp  # forkserver: no fork() of a process that holds torch threads
        with mp.get_context("forkserver").Pool(max(1, (os.cpu_count() or 4) - 2), initializer=_lower_priority) as pool:
            ok = sum(pool.imap_unordered(_thumb, todo, chunksize=32))
        print(f"thumbnails: {ok} made in {(time.time() - t) / 60:.0f} min", flush=True)

    def _previews(self) -> None:
        """The viewer's 1600 px previews, newest photos first, so opening a photo never waits on decoding its
        original from the store (on the Mac mini, a USB hard disk). In a few low-priority worker processes,
        in batches; it stops while the cache's disk has less than PREVIEW_ROOM free, and gives way to a new
        run (pregen_again) between batches. /large/ is left to be made on first use."""
        cache, seed, c = self.cache, self.seed, self.live.cat
        (cache / "previews").mkdir(parents=True, exist_ok=True)
        have = {p.name[:64] for p in (cache / "previews").iterdir() if p.suffix == ".jpg"}
        if cache != seed and (seed / "previews").is_dir():   # copied on first use, a read and no decode
            have |= {p.name[:64] for p in (seed / "previews").iterdir() if p.suffix == ".jpg"}
        todo = [(str(self.original(s, c.meta[s]["ext"])), str(cache / "previews" / f"{s}.jpg"), 1600)
                for s in (c.shas[i] for i in c.newest) if s not in have]
        if not todo:
            return
        print(f"making {len(todo)} previews in the background", flush=True)
        t, ok, pool = time.time(), 0, None
        try:
            for i in range(0, len(todo), PREVIEW_BATCH):
                if self.pregen_again:
                    print(f"previews: {ok} made; stopping for a newer run", flush=True)
                    return
                if shutil.disk_usage(cache).free < PREVIEW_ROOM:
                    print(f"previews: {ok} made; stopped, under {PREVIEW_ROOM >> 30} GB free on {cache}", flush=True)
                    return
                batch = todo[i:i + PREVIEW_BATCH]
                if len(todo) <= 64:
                    ok += sum(_thumb(job) for job in batch)
                    continue
                if pool is None:
                    import multiprocessing as mp
                    pool = mp.get_context("forkserver").Pool(PREVIEW_WORKERS, initializer=_lower_priority)
                ok += sum(pool.imap_unordered(_thumb, batch, chunksize=8))
                if i + PREVIEW_BATCH < len(todo):
                    print(f"previews: {ok} of {len(todo)} made, {ok / (time.time() - t):.1f} a second", flush=True)
        finally:
            if pool is not None:
                pool.close()
                pool.join()
        print(f"previews: {ok} made in {(time.time() - t) / 60:.0f} min", flush=True)
