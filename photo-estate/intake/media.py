"""Small shared helpers (standard library only)."""
from __future__ import annotations

from upload import MEDIA_EXT  # noqa: F401  (the photo and video extensions ingest stores)


def human_bytes(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"
