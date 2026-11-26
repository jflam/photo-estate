"""The review of held photos (/held: changes.html in its held mode; multiplayer phase 5), in a real browser, as the
demo's members Ben (whose photos are held) and Cleo (someone else). In order, on one demo app:

- Ben's inbox item says how many wait and opens /held: every held photo of his, in sections, likeliest mistakes
  first, the one he hides last and said to be; each says why it was held;
- he marks two (a click, and R on another), zooms one, and presses U a few photos on: the two are released (Cleo sees
  them), the rest up to there stay private and leave the review, and what comes after still waits;
- he marks one more and reloads: the mark is still there, and so is everything not yet looked over;
- the search held view (/?q=is:held) says it shows fewer than are held, leads to the review, and has no "release all";
- "Done: keep the rest private" (A) releases the one he marked and keeps the rest: nothing waits, the inbox item is
  done, and what he kept is still private to him;
- Cleo has nothing to look over.

Light and dark screenshots of the review, two marked, a zoomed photo, the search's bar and the review when done go to
PHOTO_ESTATE_SHOTS (a folder; default: the test's own temporary folder).

Slow tier (`uv run pytest -m slow`). The app is built in process on the demo (its own scratch catalog, never demo/)
and served on a free port, so the test can write what a hold needs into that catalog first: Ben's sharers, a person's
name on one photo, vectors made to score just over the line, clearly, and very high (test_private.aim), a phone's
screenshot by its size, a photo he hides, and the hold itself. The words and the machine are unit-tested without a
browser (app/tests/held-flow.test.js).

  uv run pytest tests/test_held_browser.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import embed  # noqa: E402
from photo_app import PhotoApp  # noqa: E402
from test_lenses_browser import be  # noqa: E402,F401  (be: the fixture)
from test_multiplayer import BEN, CLEO, call, hide, mid, seen  # noqa: E402
from test_pages_browser import browser  # noqa: E402,F401  (browser: the fixture)
from test_private import aim, hold, share  # noqa: E402

pytestmark = [pytest.mark.slow, pytest.mark.server]

SECTIONS = ["Someone is named in them", "Just over the line", "Receipts and documents", "Screenshots, and the surest",
            "Already hidden from you"]
STORY: dict = {}


@pytest.fixture(scope="module")
def served():
    """The demo app in process, with twelve photos held for Ben across every section, served on a free port."""
    a = embed.parser().parse_args(["serve", "--demo", "--no-pregenerate"])
    app = PhotoApp(a)
    app.feed_update()
    c = app.live.cat
    named = [c.shas[k] for k in (3, 4)]
    close = [c.shas[k] for k in (5, 6, 7)]
    docs = [c.shas[k] for k in (8, 9, 17, 21)]
    sure, shot, hid = c.shas[18], c.shas[19], c.shas[20]
    con = sqlite3.connect(app.a.db)
    con.executemany("UPDATE assets SET people='Zed Quux' WHERE sha256=?", ((x,) for x in named))
    con.execute("UPDATE assets SET ext='.png', width=1170, height=2532 WHERE sha256=?", (shot,))
    con.execute("DELETE FROM exif WHERE sha256=?", (shot,))
    con.commit()
    con.close()
    for x, k, score in ((named[0], 0, 0.15), (named[1], 2, 0.11), (close[0], 0, 0.097), (close[1], 2, 0.099),
                        (close[2], 1, 0.101), (docs[0], 0, 0.11), (docs[1], 2, 0.115), (docs[2], 0, 0.125), (docs[3], 2, 0.118),
                        (sure, 2, 0.16), (hid, 0, 0.12)):
        aim(app, x, k, score)
    app.live.install(app.load_catalog(), then=app.caught_up, looks=app.with_private)
    held = [*named, *close, *docs, sure, shot, hid]
    for x in held:
        share(app, x, BEN)
    hide(app, BEN, [hid])
    hold(app, BEN, *held[:9])
    hold(app, BEN, *held[9:])                        # a later batch: the same inbox item
    srv = app.make_server("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    STORY.update(held=held, hid=hid, shot=shot)
    yield app, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture(scope="module")
def shots(tmp_path_factory) -> Path:
    d = Path(os.environ.get("PHOTO_ESTATE_SHOTS") or tmp_path_factory.mktemp("shots"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def both_schemes(p, path: Path) -> None:
    for scheme in ("light", "dark"):
        p.emulate_media(color_scheme=scheme)
        p.wait_for_timeout(300)
        p.screenshot(path=str(path.with_name(f"{path.stem}-{scheme}.png")))
    p.emulate_media(color_scheme="light")


def tiles(p) -> list[str]:
    return p.eval_on_selector_all("#grid .t", "ts => ts.map(t => t.dataset.sha)")


def marked(p) -> list[str]:
    return p.eval_on_selector_all("#grid .t.keep", "ts => ts.map(t => t.dataset.sha)")


def test_ben_looks_his_held_photos_over_a_stretch_at_a_time(served, be, shots):   # noqa: F811  (be: the fixture imported above)
    app, base = served
    held = STORY["held"]
    p = be("ben")
    p.goto(f"{base}/inbox")
    item = p.locator(".item", has_text="waiting for you to look them over").first
    item.wait_for()
    assert "12 photos kept private to you, waiting for you to look them over" in item.inner_text()
    item.get_by_role("link", name="Look them over").click()
    p.wait_for_url(f"{base}/held")
    p.locator("#grid .t").first.wait_for()
    # every one, in sections, the one he hides last
    listed = tiles(p)
    assert len(listed) == 12 == call(app, "GET", "/api/held?count=1", BEN)["waiting"] and set(listed) == set(held)
    assert p.eval_on_selector_all("#grid .sec b", "bs => bs.map(b => b.textContent)") == SECTIONS
    assert listed[-1] == STORY["hid"] and p.locator(f"#t{STORY['hid']} .hid").inner_text() == "Hidden"
    assert set(listed[:2]) == set(held[:2]), "a named person first"
    assert "Looks like" in (p.locator("#grid .t").nth(2).get_attribute("title") or "")
    assert p.locator(f"#t{STORY['shot']}").get_attribute("title") == "A screenshot by its size, with no camera"
    assert p.locator("#sum").inner_text().startswith("12 to look over")
    both_schemes(p, shots / "held-review")
    # two marked (a click; R on another), and up to here a few photos on
    p.locator("#grid .t").nth(1).click()
    p.keyboard.press("ArrowRight")
    p.keyboard.press("ArrowRight")
    p.keyboard.press("r")
    p.keyboard.press("ArrowRight")
    assert marked(p) == [listed[1], listed[3]]
    assert p.locator("#dock .say").inner_text() == "Up to here: release 2, keep 3 private · 7 after it"
    both_schemes(p, shots / "held-marked")
    p.keyboard.press(" ")
    p.locator(".lb .t2").wait_for()
    assert "5 of 12" in p.locator(".lb .t2").inner_text() and p.locator(".lb .state").inner_text().startswith("Stays private")
    p.wait_for_timeout(400)
    both_schemes(p, shots / "held-zoom")
    p.keyboard.press("Escape")
    p.keyboard.press("u")
    p.wait_for_function("() => document.querySelectorAll('#grid .t').length === 7")
    assert tiles(p) == listed[5:], "what it covered left the review; the rest waits, in order"
    cleo = set(seen(app, CLEO))
    assert {listed[1], listed[3]} <= cleo and not ({listed[0], listed[2], listed[4], *listed[5:]} & cleo)
    assert call(app, "GET", "/api/held?count=1", BEN) == {"held": 10, "waiting": 7}
    assert listed[0] in seen(app, BEN, "&q=is:held"), "what he kept is still private to him, and held"
    # a mark is kept in this browser: a reload has it, and everything not looked over
    p.locator("#grid .t").nth(1).click()
    p.reload()
    p.locator("#grid .t").first.wait_for()
    assert tiles(p) == listed[5:] and marked(p) == [listed[6]]
    # the search's held view: fewer than are held, a way to the review, and no "release all"
    p.goto(f"{base}/?q=is:held")
    bar = p.locator("#sHeld")
    p.wait_for_function("() => document.querySelector('#sHeld span')?.textContent.includes('wait for you')")   # (the counts)
    text = bar.locator("span").inner_text()
    assert text.startswith("This search shows 8 of the 10 photos held for you") and "7 wait for you to look them over" in text, text
    assert bar.locator("a.btn").inner_text() == "Look over all 7" and bar.locator("a.btn").get_attribute("href") == "/held"
    assert not bar.get_by_role("button", name="Release all").count() and "Release all" not in bar.inner_text()
    both_schemes(p, shots / "held-search-bar")
    bar.locator("a.btn").click()
    p.wait_for_url(f"{base}/held")
    p.locator("#grid .t").first.wait_for()
    # Done: the one marked released, the rest kept; nothing waits, the inbox item is done
    p.once("dialog", lambda d: d.accept())
    p.keyboard.press("a")
    p.locator("#grid .blank h3", has_text="Nothing to look over").wait_for()
    assert listed[6] in set(seen(app, CLEO)) and not (set(listed[5:]) - {listed[6]}) & set(seen(app, CLEO))
    assert call(app, "GET", "/api/held?count=1", BEN) == {"held": 9, "waiting": 0}
    both_schemes(p, shots / "held-done")
    p.goto(f"{base}/inbox")
    p.wait_for_function("() => !document.body.innerText.includes('waiting for you to look')")
    assert mid(app, BEN)


def test_cleo_has_nothing_to_look_over(served, be):   # noqa: F811  (be: the fixture imported above)
    app, base = served
    p = be("cleo")
    p.goto(f"{base}/held")
    p.locator("#grid .blank h3", has_text="Nothing to look over").wait_for()
    assert tiles(p) == [] and call(app, "GET", "/api/held", CLEO)["items"] == []
