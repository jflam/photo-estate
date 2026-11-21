"""
images.py — the photos themselves, as the photo app (`embed.py serve`) sends them.

  GET /thumb/<sha>      360 px WebP, for the grid (a JPEG if it was made before 2026-11-20 and not yet redone)
  GET /preview/<sha>    1600 px JPEG, for the viewer
  GET /large/<sha>      4096 px JPEG, for big high-density displays, where a 1600 px preview is visibly soft
  GET /original/<sha>   the file as stored, for Save to Photos / share
  GET /video/<sha>      a video, or a Live Photo's clip, with Range: photo_app's handler streams it from
                        Images.video_file (this module only says which file, never reads it). A heavy video
                        (video.wants_copy) is played from a 1080p copy once the background job has made it,
                        <cache>/streams/<sha>.mp4; ?original=1 is always the stored file (to save or share)

A video's thumbnail and preview are made from its poster frame (video.poster); everything else is the same.

  Images.handle(method, path, qs, body) -> (status, body, content type, Cache-Control[, headers]) or None
  Images.image(sha, kind, side)         (a cached image, its type), made on first use
  Images.pregenerate()                  every missing thumbnail, then every missing preview, then the heavy videos'
                                        copies to play from, in the background
  Images.video_file(sha, original=False)  (the file to send, its content type, its name), or None if it is not a video
  upright(src, side)                    a photo, or a video's poster frame, as an upright RGB image for a `side` px copy

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
from core.media import ORIGINAL_TYPES, VIDEO_TYPES

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


WORKERS = max(1, (os.cpu_count() or 4) - 2)   # at nice 10: the app's own work comes first
BATCH = 2000                     # photos done between checks for free space and for a newer run
PREVIEW_ROOM = 20 << 30          # make no previews while the cache's disk has less than this free


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


def upright(src: Path | str, side: int) -> Image.Image:
    """The picture in `src` the right way up, in RGB, decoded no larger than a `side` px copy needs (a JPEG's
    draft mode). For a video: its poster frame. Raises what the decoder raises."""
    if Path(src).suffix.lower() in VIDEO_TYPES:
        import video
        im = video.poster(str(src))
        if im is None:
            raise ValueError("no frame could be decoded")
        return im.convert("RGB")
    with Image.open(src) as im:
        im.draft("RGB", (side, side))
        return ImageOps.exif_transpose(im).convert("RGB")


def _make(job: tuple[str, str | None, str | None]) -> bool:
    """Worker: (original, thumbnail or None, preview or None) -> both written from one decode of the original.
    Decoding is most of the cost (a 48 MP HEIC takes most of a second), so a photo that needs both is opened
    once. Never raises."""
    src, thumb, preview = job
    try:
        im = upright(src, 1600 if preview else 360)
        if preview:
            im.thumbnail((1600, 1600))
            write_image(im, preview)
        if thumb:
            im.thumbnail((360, 360))
            write_image(im, thumb)
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
                im = upright(self.original(sha, m["ext"]), side)
                im.thumbnail((side, side))
                write_image(im, dest)
            except Exception:
                return None
        return dest.read_bytes(), TYPES[dest.suffix]

    def video_file(self, sha: str, original: bool = False) -> tuple[Path, str, str] | None:
        """(the file to send, the type to send it as, its name) for a video in the catalog or a Live Photo's
        clip; None for anything else, or a file that is not on the disk. The file is the copy to play from
        if one has been made (_streams), unless `original`: then always the stored file."""
        if not sha_re.match(sha):
            return None
        cat = self.live.cat
        m = cat.meta.get(sha)
        ext = m["ext"] if m and m.get("kind") == "video" else cat.clips.get(sha)
        if not ext or ext not in VIDEO_TYPES:
            return None
        src = self.original(sha, ext)
        if not src.exists():
            return None
        name = ((m["path"] or "") if m else "").rsplit("/", 1)[-1] or f"{sha[:12]}{ext}"
        copy = self.cache / "streams" / f"{sha}.mp4"
        if not original and copy.exists():
            return copy, "video/mp4", name.rsplit(".", 1)[0] + ".mp4"
        return src, VIDEO_TYPES[ext], name

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
        """Every missing thumbnail and preview, in the background. One run at a time: a call while one is under
        way makes it start again once it can, and the run gives way between batches, so a new photo never waits
        hours behind it (the next run starts from the newest photos)."""
        with self.pregen_lock:
            if self.pregen_running:
                self.pregen_again = True
                return
            self.pregen_running = True
        while True:
            try:
                self._thumbs()
                self._previews()
                self._streams()
            except Exception as e:     # never takes the app down; the images are still made on first use
                print(f"pregenerate: {type(e).__name__}: {e}", flush=True)
            with self.pregen_lock:
                if not self.pregen_again:
                    self.pregen_running = False
                    return
                self.pregen_again = False

    def _have(self, kind: str) -> set[str]:
        """The photos with an image of this kind in the cache or the seed (which is copied on first use)."""
        ext, out = ext_of(kind), set()
        for d in {self.cache / kind, self.seed / kind}:
            if d.is_dir():
                out |= {p.name[:64] for p in d.iterdir() if p.suffix == ext}
        return out

    def _thumbs(self) -> None:
        """Every missing thumbnail, newest photos first, with its preview from the same decode when that is
        missing too. The seed's thumbnails are copied to a separate fast cache first."""
        cache, seed, c = self.cache, self.seed, self.live.cat
        (cache / "thumbs").mkdir(parents=True, exist_ok=True)
        (cache / "previews").mkdir(parents=True, exist_ok=True)
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
        previews = self._have("previews")
        self._make_all("thumbnails", [
            (str(self.original(s, c.meta[s]["ext"])), str(cache / "thumbs" / f"{s}{THUMB_EXT}"),
             None if s in previews else str(cache / "previews" / f"{s}.jpg"))
            for s in (c.shas[i] for i in c.newest) if s not in have])

    def _previews(self) -> None:
        """The viewer's 1600 px previews the thumbnail pass did not make (the photo had a thumbnail already),
        newest first, so opening a photo never waits on decoding its original from the store (on the Mac mini,
        a USB hard disk). /large/ is left to be made on first use."""
        c, previews = self.live.cat, self._have("previews")
        (self.cache / "previews").mkdir(parents=True, exist_ok=True)
        self._make_all("previews", [(str(self.original(s, c.meta[s]["ext"])), None, str(self.cache / "previews" / f"{s}.jpg"))
                                    for s in (c.shas[i] for i in c.newest) if s not in previews])

    def _streams(self) -> None:
        """A copy to play from for every video that wants one (video.wants_copy: too heavy to stream as it
        is, or of a kind no browser plays) and has none, newest first. One at a time, each in a process of
        its own at low priority: the Mac mini has one hardware encoder, and a file FFmpeg crashes on must
        not take the app with it. A video that cannot be copied is marked (<sha>.failed) and not tried again.
        Gives way to a newer run between videos, and stops while the cache's disk is short of room."""
        import video
        c, d = self.live.cat, self.cache / "streams"
        d.mkdir(parents=True, exist_ok=True)
        have = {p.name[:64] for p in d.iterdir() if p.suffix in (".mp4", ".failed")}
        todo = []
        for s in (c.shas[i] for i in c.newest):
            m = c.meta[s]
            if m.get("kind") != "video" or s in have:
                continue
            src = self.original(s, m["ext"])
            try:
                if video.wants_copy(src.stat().st_size, m.get("dur"), m["ext"]):
                    todo.append((s, src))
            except OSError:
                continue
        if not todo:
            return
        print(f"making {len(todo)} videos' copies to play from in the background", flush=True)
        import multiprocessing as mp
        from concurrent.futures import ProcessPoolExecutor
        from concurrent.futures.process import BrokenProcessPool
        t, ok, pool = time.time(), 0, None
        try:
            for k, (s, src) in enumerate(todo):
                if self.pregen_again:
                    print(f"copies: {ok} made; stopping for a newer run", flush=True)
                    return
                if shutil.disk_usage(self.cache).free < PREVIEW_ROOM:
                    print(f"copies: none while under {PREVIEW_ROOM >> 30} GB is free on {self.cache}", flush=True)
                    return
                if pool is None:
                    pool = ProcessPoolExecutor(1, mp_context=mp.get_context("forkserver"), initializer=_lower_priority)
                try:
                    pool.submit(video.stream_copy, str(src), str(d / f"{s}.mp4")).result()
                    ok += 1
                except Exception as e:
                    (d / f"{s}.failed").write_text(f"{type(e).__name__}: {e}\n")
                    if isinstance(e, BrokenProcessPool):
                        pool = None
                if (k + 1) % 25 == 0:
                    print(f"copies: {ok} of {len(todo)} made, {(time.time() - t) / 60:.0f} min", flush=True)
        finally:
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)
        print(f"copies: {ok} made in {(time.time() - t) / 60:.0f} min", flush=True)

    def _make_all(self, what: str, todo: list[tuple[str, str | None, str | None]]) -> None:
        """Run _make over todo in batches of BATCH, in WORKERS low-priority processes (forkserver: no fork() of
        a process that holds torch threads). Before each batch: give way to a newer run (pregen_again), and
        leave the previews out while the cache's disk has less than PREVIEW_ROOM free."""
        if not todo:
            return
        print(f"making {len(todo)} {what} in the background", flush=True)
        t, ok, pool, full = time.time(), 0, None, False
        try:
            for i in range(0, len(todo), BATCH):
                if self.pregen_again:
                    print(f"{what}: {ok} made; stopping for a newer run", flush=True)
                    return
                if shutil.disk_usage(self.cache).free < PREVIEW_ROOM:
                    if not full:
                        print(f"{what}: no previews while under {PREVIEW_ROOM >> 30} GB is free on {self.cache}", flush=True)
                    full = True
                    batch = [(src, th, None) for src, th, _ in todo[i:i + BATCH] if th]
                    if not batch:
                        return
                else:
                    batch = todo[i:i + BATCH]
                if len(todo) <= 64:    # a few new photos: not worth starting worker processes
                    ok += sum(_make(job) for job in batch)
                    continue
                if pool is None:
                    import multiprocessing as mp
                    pool = mp.get_context("forkserver").Pool(WORKERS, initializer=_lower_priority)
                ok += sum(pool.imap_unordered(_make, batch, chunksize=8))
                if i + BATCH < len(todo):
                    print(f"{what}: {ok} of {len(todo)} done, {ok / (time.time() - t):.1f} a second", flush=True)
        finally:
            if pool is not None:
                pool.close()
                pool.join()
        print(f"{what}: {ok} made in {(time.time() - t) / 60:.0f} min", flush=True)
