"""Which files are photos and videos (standard library only).

  MIME          extension -> MIME type, for every file `estate.py ingest` takes: the one list
  RAW_EXT       camera RAW files: catalogued, but Pillow cannot decode them
  IMAGE_EXT     every still ingest takes (RAW included)
  VIDEO_EXT     every video ingest takes
  MEDIA_EXT     IMAGE_EXT | VIDEO_EXT: what a folder or an export is counted and ingested by
  DECODABLE_EXT the stills Pillow (with pillow-heif) decodes: IMAGE_EXT - RAW_EXT
  UPLOAD_EXT    what the phone sends (upload.py): JPEG, PNG, HEIC/HEIF, GIF, WebP, TIFF, DNG, AVIF and
                its own video formats; not RAW from other cameras, BMP or older video (issue #10)
  ORIGINAL_TYPES  extension -> MIME type for `GET /original/<sha>` (images.py): the stills a phone can save
  EMBED_SKIP_EXT  stills `embed.py run` leaves out (RAW: undecodable)
  FACES_SKIP_EXT  stills `faces.py run` leaves out (RAW, and GIF: see below)
  kind_of(name)   "image", "video" or None, by extension

Every set is lower-case with the dot. The rules for a screenshot are in catalog.py, with a fixture both
apps can test against (tests/fixtures/screenshot_rules.json).
"""
from __future__ import annotations

from pathlib import PurePosixPath

_STILLS = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".heic": "image/heic",
    ".heif": "image/heif", ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".tif": "image/tiff", ".tiff": "image/tiff", ".avif": "image/avif",
}
_RAW = {
    ".dng": "image/x-adobe-dng", ".cr2": "image/x-canon-cr2", ".cr3": "image/x-canon-cr3",
    ".nef": "image/x-nikon-nef", ".arw": "image/x-sony-arw", ".orf": "image/x-olympus-orf",
    ".rw2": "image/x-panasonic-rw2", ".raf": "image/x-fuji-raf",
}
_VIDEOS = {
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".m4v": "video/x-m4v", ".3gp": "video/3gpp",
    ".avi": "video/x-msvideo", ".mkv": "video/x-matroska", ".mts": "video/mp2t", ".m2ts": "video/mp2t",
    ".webm": "video/webm", ".mpg": "video/mpeg", ".wmv": "video/x-ms-wmv",
}
MIME: dict[str, str] = {**_STILLS, **_RAW, **_VIDEOS}

RAW_EXT = frozenset(_RAW)
IMAGE_EXT = frozenset(_STILLS) | RAW_EXT
VIDEO_EXT = frozenset(_VIDEOS)
MEDIA_EXT = IMAGE_EXT | VIDEO_EXT
DECODABLE_EXT = IMAGE_EXT - RAW_EXT

UPLOAD_EXT = frozenset({".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".webp", ".tif", ".tiff", ".dng", ".avif",
                        ".mp4", ".mov", ".m4v", ".3gp"})

# /original serves the stills a phone can take back (Save to Photos, share): those it can send, less RAW.
# Before #85 this left out .heif, .tif, .tiff and .avif, so a catalogued photo in one of those got a 404.
# BMP is left out as before (no phone sends or saves one).
ORIGINAL_TYPES: dict[str, str] = {e: MIME[e] for e in sorted(UPLOAD_EXT & DECODABLE_EXT)}

EMBED_SKIP_EXT = RAW_EXT
# faces.py also leaves out GIF, on purpose (docs/book/14-faces.md, "Honest limits"): a GIF is an animation or a
# graphic, its first frame is all the decoder reads, and it is palette-reduced, which makes weak face embeddings.
# embed.py does embed GIFs: what one shows is still worth finding by a typed search.
FACES_SKIP_EXT = RAW_EXT | {".gif"}

for _s in (IMAGE_EXT, VIDEO_EXT, UPLOAD_EXT):
    assert _s <= MIME.keys()
assert not IMAGE_EXT & VIDEO_EXT


def kind_of(name: str) -> str | None:
    """The kind of file a name is, by its extension (any case): "image", "video" or None."""
    ext = PurePosixPath(name).suffix.lower()
    return "image" if ext in IMAGE_EXT else "video" if ext in VIDEO_EXT else None
