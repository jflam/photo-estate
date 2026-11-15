"""
pages.py: the web pages as they are sent. Every page (search.html, people.html, stats.html, review.html)
shares one stylesheet, app/estate.css; it is put into the page here, where the page says /*@estate.css*/,
so a page stays a single request and the three servers that send pages (embed.py, faces.py, estate.py)
cannot drift apart in how they look.
"""
from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).resolve().parent
MARK = b"/*@estate.css*/"


def page(name: str) -> bytes:
    """The page's bytes with the shared stylesheet in place. Read each time, so edits show up on reload."""
    return (HERE / name).read_bytes().replace(MARK, (HERE / "app" / "estate.css").read_bytes(), 1)
