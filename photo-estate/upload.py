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
