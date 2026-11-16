"""Small shared helpers (standard library only)."""
from __future__ import annotations

# What `estate.py ingest` takes, copied here because estate.py needs Pillow and the setup page must start
# without it. tests/test_intake.py checks the two stay the same. Not upload.MEDIA_EXT: that is what a phone
# sends, and leaves out RAW and older video files a folder or an export holds (issue #10).
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".webp", ".bmp", ".tif", ".tiff",
             ".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2", ".raf", ".avif"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".3gp", ".avi", ".mkv", ".mts", ".m2ts", ".webm", ".mpg", ".wmv"}
MEDIA_EXT = IMAGE_EXT | VIDEO_EXT


def human_bytes(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"
