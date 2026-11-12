#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright", "pillow"]
# ///
"""
screenshots.py — retake the README screenshots (docs/screenshots/) from the demo library.

  uv run embed.py serve --demo &         # the demo on http://127.0.0.1:8767/
  uv run tools/screenshots.py            # then this

Uses the system's Chromium or Chrome if there is one, else Playwright's own
(`uv run --with playwright playwright install chromium` once).
"""
from __future__ import annotations

import argparse
import io
import shutil
import sqlite3
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
        page.locator("#grid img").first.click(); page.wait_for_timeout(1500)
        if not page.evaluate("document.querySelector('#v').classList.contains('info')"):
            page.evaluate("toggleInfo()")  # the details panel remembers whether it was open
        settle(page, 1500)
        save(page, "viewer-details", 1600)

        page.goto(f"{a.url}/"); settle(page)
        page.locator("#pills .pill").first.click(); settle(page, 800)  # the Where filter, open
        save(page, "library-filters", 1600)

        phone = br.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=3, is_mobile=True,
                               has_touch=True, color_scheme="dark").new_page()
        phone.goto(f"{a.url}/?q=kitten"); settle(phone)
        save(phone, "phone-kitten", 780)
        br.close()


if __name__ == "__main__":
    main()
