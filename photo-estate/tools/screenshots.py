#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright", "pillow"]
# ///
"""
screenshots.py — retake the README screenshots (docs/screenshots/) from the demo library.

  uv run embed.py serve --demo &         # the demo on http://127.0.0.1:8767/
  uv run tools/screenshots.py            # then this

The demo's catalog has no scorers (scores.py), so it has no "Best first". The two pictures that show it
(library-years, search-best-first) are taken only from a server whose catalog has them: a copy of the
demo's estate.db after `scores.py install`, served with `embed.py --db COPY serve --store demo/store`.
The change set in changes-review is proposed by this script, so run it against a scratch copy.

Uses the system's Chromium or Chrome if there is one, else Playwright's own
(`uv run --with playwright playwright install chromium` once).
"""
from __future__ import annotations

import argparse
import io
import json
import shutil
import sqlite3
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent.parent
OUT = HERE.parent / "docs" / "screenshots"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default="http://127.0.0.1:8767")
    p.add_argument("--out", default=str(OUT))
    a = p.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(f"file:{HERE / 'demo' / 'estate.db'}?mode=ro", uri=True)
    lighthouse = con.execute("SELECT sha256 FROM occurrences WHERE filename LIKE 'Lighthouse drone%'").fetchone()[0]

    def settle(page, ms=1800):
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(ms)  # thumbnails fade in

    def save(page, name: str, width: int):
        im = Image.open(io.BytesIO(page.screenshot())).convert("RGB")
        if im.width > width:
            im = im.resize((width, round(im.height * width / im.width)), Image.Resampling.LANCZOS)
        im.save(out / f"{name}.jpg", quality=84, optimize=True, progressive=True)
        print(f"{name}.jpg {im.size[0]}x{im.size[1]}, {(out / f'{name}.jpg').stat().st_size // 1024} KB")

    exe = next((shutil.which(b) for b in ("chromium", "chromium-browser", "google-chrome-stable", "google-chrome")
                if shutil.which(b)), None)
    with sync_playwright() as pw:
        br = pw.chromium.launch(executable_path=exe) if exe else pw.chromium.launch()
        page = br.new_context(viewport={"width": 1280, "height": 800}, device_scale_factor=2,
                              color_scheme="dark").new_page()
        page.goto(f"{a.url}/?q=latte+art"); settle(page)
        save(page, "search-latte-art", 1600)

        page.goto(f"{a.url}/?sha={lighthouse}"); settle(page)
        save(page, "more-like-this", 1600)

        page.goto(f"{a.url}/?q=hot+air+balloon"); settle(page)
        page.locator("#sGrid img").first.click(); page.wait_for_timeout(1500)
        if not page.evaluate("document.querySelector('#v').classList.contains('info')"):
            page.evaluate("toggleInfo()")  # the details panel remembers whether it was open
        settle(page, 1500)
        save(page, "viewer-details", 1600)

        page.goto(f"{a.url}/?q=sunset"); settle(page)
        page.locator("#chips .chip").first.click(); settle(page, 800)  # the first filter, open
        save(page, "search-filters", 1600)

        def api(path: str, body: dict | None = None) -> dict:
            req = urllib.request.Request(a.url + path, data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Content-Type": "application/json"})
            return json.load(urllib.request.urlopen(req))

        if api("/api/info").get("best"):   # a catalog with scorers
            page.goto(f"{a.url}/"); settle(page)
            page.get_by_role("tab", name="Years").click(); settle(page)   # each year's cover is its best photo
            save(page, "library-years", 1600)

            page.goto(f"{a.url}/?q=mountains"); settle(page)
            page.get_by_role("button", name="View options").click(); page.wait_for_timeout(400)
            page.locator('.mi[data-sort="best"]').click(); page.mouse.click(640, 700); settle(page)   # choose, then close the menu
            save(page, "search-best-first", 1600)

        # A change set as an agent proposes one: found by description, each photo with a score and a reason.
        found = api("/api/search?" + urllib.parse.urlencode({"q": "latte art"}))["results"]
        made = api("/api/changes", {"title": "Hide the coffee photos", "prompt": "hide the photos of coffee", "op": "hide",
                                    "items": [{"sha": r["sha"], "score": round(.9 - .04 * i, 2), "reason": "latte art in a cup"}
                                              for i, r in enumerate(found)]})
        page.goto(f"{a.url}/changes/{made['id']}"); settle(page)
        save(page, "changes-review", 1600)

        phone = br.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=3, is_mobile=True,
                               has_touch=True, color_scheme="dark").new_page()
        phone.goto(f"{a.url}/?q=kitten"); settle(phone)
        save(phone, "phone-kitten", 780)
        br.close()


if __name__ == "__main__":
    main()
