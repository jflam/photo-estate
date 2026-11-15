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


def waiting(title: str, why: str) -> bytes:
    """A page for a part of the app that is not ready yet (faces load after the library does). It asks again by itself."""
    css = (HERE / "app" / "estate.css").read_text()
    return f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta http-equiv="refresh" content="4"><title>{title} — Photo Estate</title>
<style>{css}
body {{ min-height: 100dvh; display: grid; place-items: center; }}
.mark .s {{ animation: rise 2.4s var(--ease) infinite alternate; }}
@keyframes rise {{ from {{ transform: translateY(34px); }} to {{ transform: none; }} }}
</style>
<div class="blank"><svg class="mark" viewBox="0 0 150 100" aria-hidden="true"><circle class="s" cx="102" cy="26" r="15"/><path fill="currentColor" d="M0 100 49.500 38 84 74 103.500 56 150 100Z"/></svg>
<h3>{title}</h3><p>{why}</p><a class="btn" href="/">Back to the library</a></div>""".encode()
