"""Private photos on the pages (multiplayer phase 5), in a real browser, as the demo's members Ben (their sharer)
and Cleo (someone else), each a browser of their own (the X-Demo-Member header). In order, on one demo app:

- Ben opens a photo he shared and withdraws it from the viewer's details; the private line says so, and Cleo's
  library no longer has it (nor its thumbnail);
- he sets the day it is released on (a menu over the viewer), and the line says it;
- he releases it from the line, and Cleo sees it again;
- the held view (Search for is:held): two photos held for Ben, marked in the grid; its bar says how many are held
  and wait, leads to the review (/held: tests/test_held_browser.py), and has no "release all"; he chooses one and
  releases it, then the other; Cleo is shown neither while held, both after, and his inbox item is done;
- Cleo's held view holds nothing of Ben's.

Light and dark screenshots of the viewer's private line and of the held view go to PHOTO_ESTATE_SHOTS (a folder;
default: the test's own temporary folder).

Slow tier (`uv run pytest -m slow`): the app is `uv run embed.py serve --demo` on a free port (its own scratch
catalog, never demo/). The demo has no member's photos and no hold, so the test writes Ben's as ingest and
`private.py check` would (sharers, and two held photos with their inbox item) into that scratch catalog. The
words and the machines are unit-tested without a browser (app/tests/private-flow.test.js).

  uv run pytest tests/test_private_browser.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import private  # noqa: E402
from test_lenses_browser import be, until  # noqa: E402,F401  (be: the fixture)
from test_pages_browser import browser, free_port, serve, stop  # noqa: E402,F401  (browser: the fixture)

pytestmark = [pytest.mark.slow, pytest.mark.server]


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    port = free_port()
    proc, base = serve(["embed.py", "serve", "--demo", "--port", str(port), "--no-pregenerate"], port, "/api/info",
                       tmp_path_factory.mktemp("private") / "server.log")
    yield base
    stop(proc)


@pytest.fixture(scope="module")
def shots(tmp_path_factory) -> Path:
    d = Path(os.environ.get("PHOTO_ESTATE_SHOTS") or tmp_path_factory.mktemp("shots"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def call(base: str, who: str, path: str, body=None):
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "X-Demo-Member": who})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def status(base: str, who: str, path: str) -> int:
    try:
        with urllib.request.urlopen(urllib.request.Request(base + path, headers={"X-Demo-Member": who}), timeout=60) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def seen(base: str, who: str) -> set[str]:
    return {r["sha"] for r in call(base, who, "/api/search?limit=500&dedupe=0")["results"]}


STORY: dict = {}


@pytest.fixture(scope="module")
def story(app):
    """Ben's photos, written into the app's scratch catalog as ingest and the check write them: four he shared,
    and two of them held (one photo.held entry, one held inbox item). The app takes them in at the next change
    of what anyone sees (Ben withdraws a fifth and releases it at once)."""
    if STORY:
        return STORY
    db = call(app, "ada", "/api/stats")["app"]["db"]
    found = call(app, "ada", "/api/search?limit=500&dedupe=0")["results"]
    one = [r for r in found if r.get("event") is not None][:5]       # a photo is opened by its event (/?event=)
    con = sqlite3.connect(db)
    ben = con.execute("SELECT member_id FROM member_logins WHERE login='ben'").fetchone()[0]
    con.executemany("INSERT OR IGNORE INTO sharers VALUES (?,?,'2026-11-22')", ((r["sha"], ben) for r in one))
    with con:
        private._hold(con, ben, [one[1]["sha"], one[2]["sha"]], "upload", private.now())
    con.close()
    nudge = one[4]["sha"]
    call(app, "ben", "/api/private", {"shas": [nudge], "private": True})
    call(app, "ben", "/api/private", {"shas": [nudge], "private": False})
    STORY.update(ben=ben, photo=one[0], held=[one[1]["sha"], one[2]["sha"]])
    return STORY


def open_photo(p, base, r) -> None:
    p.goto(f"{base}/?event={r['event']}")
    p.locator("#sGrid .t").first.wait_for()
    p.locator("#sGrid .t").first.click()
    p.locator("#v.on").wait_for()


def both_schemes(p, path: Path) -> None:
    for scheme in ("light", "dark"):
        p.emulate_media(color_scheme=scheme)
        p.wait_for_timeout(250)
        p.screenshot(path=str(path.with_name(f"{path.stem}-{scheme}.png")))
    p.emulate_media(color_scheme="light")


def test_ben_withdraws_a_photo_from_the_viewer_sets_its_day_and_releases_it(app, be, story, shots):   # noqa: F811  (be: the fixture imported above)
    r, x = story["photo"], story["photo"]["sha"]
    assert x in seen(app, "cleo")
    p = be("ben")
    open_photo(p, app, r)
    p.locator("#vInfoBtn").click()
    row = p.locator("#vPrivRow [data-withdraw]")
    row.wait_for()
    assert "Shared by you" in row.inner_text() and "Withdraw from the family" in row.inner_text()
    words, act = row.locator("span").bounding_box(), row.locator(".vact").bounding_box()
    assert words and act and act["y"] >= words["y"] + 20 and words["width"] > 250, "the words take the width, the action under them"
    row.scroll_into_view_if_needed()
    both_schemes(p, shots / "viewer-withdraw-row")
    row.click()
    line = p.locator("#vPriv")
    line.wait_for()
    assert line.locator("span").inner_text().startswith("Private to you since ") and "you withdrew it" in line.inner_text()
    assert p.locator("#vPrivRow").is_hidden()
    assert x not in seen(app, "cleo") and status(app, "cleo", f"/thumb/{x}") == 404 and status(app, "ben", f"/thumb/{x}") == 200
    # a day it is released on by itself: the menu over the viewer
    line.get_by_role("button", name="Release on a day…").click()
    form = p.locator(".menu form.mk")
    form.wait_for()
    form.locator("input[type=date]").fill("2026-12-26")
    both_schemes(p, shots / "viewer-private-day-menu")
    form.get_by_role("button", name="Keep it private until then").click()
    p.wait_for_function("() => document.querySelector('#vPriv span')?.textContent.includes('released by itself on Dec 26')")
    p.locator("#vInfoBtn").click()                   # the details away: the line as a person sees it
    both_schemes(p, shots / "viewer-private-line")
    line.get_by_role("button", name="Release", exact=True).click()
    p.wait_for_function("() => document.querySelector('#vPriv').hidden")
    until(lambda: x in seen(app, "cleo"), "Cleo sees it again once Ben released it")
    p.keyboard.press("Escape")


def test_the_held_view_marks_and_releases_those_chosen(app, be, story, shots):   # noqa: F811  (be: the fixture imported above)
    a, b = story["held"]
    assert not {a, b} & seen(app, "cleo")
    p = be("ben")
    p.goto(f"{app}/inbox")
    item = p.locator(".item", has_text="kept private to you").first
    item.wait_for()
    assert item.get_by_role("link", name="Look them over").get_attribute("href") == "/held"
    p.goto(f"{app}/?q=is:held")
    p.wait_for_function("() => document.querySelector('#sHeld span')?.textContent.includes('wait for you')")   # (the counts)
    assert p.locator("#sHead h1").inner_text() == "Held as private"
    assert p.locator("#sGrid .t").count() == 2 and p.locator("#sGrid .t .prv").count() == 2
    text = p.locator("#sHeld span").inner_text()
    assert text.startswith("2 photos held for you. 2 wait for you to look them over"), text
    assert p.locator("#sHeld a.btn").inner_text() == "Look over all 2" and p.locator("#sHeld a.btn").get_attribute("href") == "/held"
    assert "Release all" not in p.locator("#sHeld").inner_text()
    both_schemes(p, shots / "held-view")
    p.locator("#sHeld").get_by_role("button", name="Choose…").click()
    first = p.locator("#sGrid .t").first
    first.click()
    assert "chosen" in (first.get_attribute("class") or "")
    assert p.locator("#v.on").count() == 0, "choosing: a tile tapped is chosen, not opened"
    both_schemes(p, shots / "held-view-choosing")
    chosen = first.locator("img").get_attribute("data-src").rsplit("/", 1)[-1]
    p.locator("#sHeld").get_by_role("button", name="Release 1").click()
    p.wait_for_function("() => document.querySelectorAll('#sGrid .t[data-i]').length === 1")
    assert chosen in seen(app, "cleo") and ({a, b} - {chosen}).isdisjoint(seen(app, "cleo"))
    p.locator("#sHeld").get_by_role("button", name="Choose…").click()
    p.locator("#sGrid .t").first.click()
    p.locator("#sHeld").get_by_role("button", name="Release 1").click()
    p.wait_for_function("() => document.querySelector('#sHeld span')?.textContent.startsWith('Nothing is held')")
    assert {a, b} <= seen(app, "cleo")
    p.goto(f"{app}/inbox")
    p.wait_for_function("() => !document.body.innerText.includes('kept private to you')")


def test_cleo_holds_nothing_of_bens(app, be, story):   # noqa: F811  (be: the fixture imported above)
    p = be("cleo")
    p.goto(f"{app}/?q=is:held")
    p.locator("#sHeld span").wait_for()
    assert p.locator("#sHeld span").inner_text().startswith("Nothing is held")
    assert p.locator("#sGrid .t[data-i]").count() == 0
