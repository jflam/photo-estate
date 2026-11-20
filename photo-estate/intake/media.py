"""Small shared helpers (standard library only)."""
from __future__ import annotations

# What `estate.py ingest` takes: the same sets as estate.py, from core/media.py (standard library only, so the
# setup page starts without Pillow). Not upload.MEDIA_EXT: that is what a phone sends, and leaves out RAW and
# older video files a folder or an export holds (issue #10).
from core.media import IMAGE_EXT, MEDIA_EXT, VIDEO_EXT  # noqa: F401


def human_bytes(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"
