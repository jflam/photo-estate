"""
pages.py: the web pages as they are sent. Every page (search.html, people.html, stats.html, review.html)
shares one stylesheet, app/estate.css; it is put into the page here, where the page says /*@estate.css*/,
so the servers that send pages (embed.py, faces.py, estate.py, intake/server.py) cannot drift apart in
how they look.

The pages also share script: native ES modules in app/*.js (dom.js, api.js, fsm.js, toast.js, ...), which a
page imports by address (`import { esc } from "/app/dom.js"`) and the browser fetches as they are: no
bundler, no build. module() is the one place that says which files those are, so every server that
sends a page sends the same modules:

  page(name) -> bytes                the page, stylesheet in place, icons versioned
  module(path) -> bytes | None       the module at /app/<name>.js, or None when the path is not one
  MODULE_TYPE                        its Content-Type
  waiting(title, why) -> bytes       a page for a part of the app that is not ready yet

The icons a page names (/icon-192.png, /icon-180.png) are given an address that changes when the icon does
(/icon-192.png?v=1a2b3c4d): browsers keep a site's icon by its address for a long time, whatever the server
says about caching, and would go on showing the old one.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
MARK = b"/*@estate.css*/"
MODULE = re.compile(r"/app/([a-z][a-z0-9-]*\.js)")
MODULE_TYPE = "text/javascript; charset=utf-8"     # browsers refuse a module sent as anything but JavaScript
NOT_MODULES = {"sw.js"}                            # the service worker: served from the site root, not imported


def page(name: str) -> bytes:
    """The page's bytes with the shared stylesheet in place. Read each time, so edits show up on reload."""
    body = (HERE / name).read_bytes().replace(MARK, (HERE / "app" / "estate.css").read_bytes(), 1)
    return re.sub(rb'href="/(icon-\d+\.png)"', _versioned, body)


def module(path: str) -> bytes | None:
    """The script module a page imports from `path` (/app/dom.js), or None if the path is not one. Only
    the modules themselves, app/*.js: not their tests, not package.json, nothing outside app/. Read each
    time, like a page."""
    m = MODULE.fullmatch(path)
    if not m or m[1] in NOT_MODULES:
        return None
    f = HERE / "app" / m[1]
    return f.read_bytes() if f.is_file() else None


def modules() -> list[str]:
    """The address of every module there is, sorted."""
    return sorted(f"/app/{f.name}" for f in (HERE / "app").glob("*.js") if module(f"/app/{f.name}") is not None)


def _versioned(m: re.Match) -> bytes:
    """An icon's link, with a few characters of the file's hash as its version."""
    icon = HERE / "app" / m[1].decode()
    v = hashlib.sha256(icon.read_bytes()).hexdigest()[:8] if icon.exists() else "0"
    return b'href="/%s?v=%s"' % (m[1], v.encode())


def waiting(title: str, why: str, refresh: bool = True) -> bytes:
    """A page for a part of the app that is not ready yet (faces load after the library does). It asks again by
    itself, unless refresh is False (a part that failed: asking again would only say so again)."""
    css = (HERE / "app" / "estate.css").read_text()
    return f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
{'<meta http-equiv="refresh" content="4">' if refresh else ""}<title>{title} — Photo Estate</title>
<style>{css}
body {{ min-height: 100dvh; display: grid; place-items: center; }}
.mark .s {{ animation: rise 2.4s var(--ease) infinite alternate; }}
@keyframes rise {{ from {{ transform: translateY(34px); }} to {{ transform: none; }} }}
</style>
<div class="blank"><svg class="mark" viewBox="0 0 150 100" aria-hidden="true"><circle class="s" cx="102" cy="26" r="15"/><path fill="currentColor" d="M0 100 49.500 38 84 74 103.500 56 150 100Z"/></svg>
<h3>{title}</h3><p>{why}</p><a class="btn" href="/">Back to the library</a></div>""".encode()
