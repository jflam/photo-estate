"""Lenses on the pages (multiplayer phase 3), in a real browser, as the demo's three members: Ada the sysadmin,
Ben and Cleo, each a browser of their own (the X-Demo-Member header). In order, on one demo app:

- Ben hides two photos through a change set (the review page says they are hidden from him alone, and which
  lens); they leave Ben's library and not Ada's; Ada is told plainly that his set is not there for her;
- Ben's "show hidden" (Albums, Filters, See them) lists them; the viewer says why one is hidden, and "Show it
  anyway" shows it to him, and "Hide it again" takes that back;
- Ben publishes his filter; Cleo subscribes (Ben's photos leave her library), unsubscribes, keeps a copy
  (they leave it again, and a later hide of Ben's does not reach her);
- Ada publishes an album as a series; Ben opens it (without the photo he hid), subscribes, and it is in his
  Albums;
- Ada hides a photo from everyone (the review page's choice); Ben proposes his filter, Ada makes it an estate
  default, and Cleo opts out of it;
- the Library's menu names the member, and offers Members to a sysadmin only.

Slow tier (`uv run pytest -m slow`): the app is `uv run embed.py serve --demo` on a free port (its own scratch
catalog, never demo/). The browser and the page checks are tests/test_pages_browser.py's. The words and the
machines the pages use are unit-tested without a browser (app/tests/lens-flow.test.js).

  uv run pytest tests/test_lenses_browser.py
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_pages_browser import browser, free_port, serve, stop  # noqa: E402,F401  (browser: the fixture)

pytestmark = [pytest.mark.slow, pytest.mark.server]


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    port = free_port()
    proc, base = serve(["embed.py", "serve", "--demo", "--port", str(port), "--no-pregenerate"], port, "/api/info",
                       tmp_path_factory.mktemp("lenses") / "server.log")
    yield base
    stop(proc)


def call(base: str, who: str, path: str, body=None):
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "X-Demo-Member": who})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def seen(base: str, who: str) -> set[str]:
    return {r["sha"] for r in call(base, who, "/api/search?limit=500&dedupe=0")["results"]}


def until(check, what: str, within: float = 20.0) -> None:
    """Wait for `check()`: a hiding set's apply is taken in by the app in the background."""
    end = time.monotonic() + within
    while not check():
        assert time.monotonic() < end, what
        time.sleep(0.1)


def lens_id(base: str, who: str, **want) -> int:
    return next(x["id"] for x in call(base, who, "/api/lenses")["lenses"] if all(x.get(k) == v for k, v in want.items()))


@pytest.fixture
def be(browser):  # noqa: F811  (the fixture imported above)
    """be("ben") -> a page in a browser that is Ben. Every page's uncaught errors and console errors fail the test."""
    made = []

    def open_as(who: str):
        ctx = browser.new_context(viewport={"width": 1280, "height": 800}, locale="en-US", service_workers="block",
                                  extra_http_headers={"X-Demo-Member": who})
        p = ctx.new_page()
        p.errors, p.said = [], []
        p.on("pageerror", lambda e: p.errors.append(str(e)))
        p.on("console", lambda m: p.said.append(m.text) if m.type == "error" else None)
        made.append((ctx, p))
        return p
    yield open_as
    for ctx, p in made:
        ctx.close()
        assert not p.errors, f"uncaught errors in the page: {p.errors}"
        own = [t for t in p.said if "Failed to load resource" not in t]     # the browser's own line for a 404
        assert not own, f"console errors: {own}"


def albums(p, base):
    p.goto(base + "/?albums")
    p.locator("#albList .bt h2", has_text="Filters").wait_for()


def row(p, lid: int):
    return p.locator(f'#albList .lens[data-lens="{lid}"]')


def offer(p, lid: int, label: str) -> None:
    """Press a lens row's button, and wait for the row to be drawn again from the server's answer."""
    with p.expect_response(lambda r: "/api/lenses" in r.url and r.request.method == "GET"):
        row(p, lid).get_by_role("button", name=label, exact=True).click()
    p.wait_for_function("() => !document.querySelector('#albList .lens.busy')")


SHAS: dict[str, str] = {}      # the photos the story uses, picked once


def pick(base: str) -> None:
    if not SHAS:
        rows = call(base, "ada", "/api/search?limit=500&dedupe=0")["results"]
        for k, r in zip(("a", "b", "c", "d", "e", "f"), rows[3:]):
            SHAS[k] = r["sha"]


# ── Ben hides, and only Ben stops seeing them ───────────────────────────────────────────────────

def test_ben_hides_through_a_change_set_and_only_ben_stops_seeing_them(app, be):
    pick(app)
    a, b = SHAS["a"], SHAS["b"]
    sid = call(app, "ben", "/api/changes", {"title": "Blurry ones", "items": [{"sha": a}, {"sha": b}]})["id"]
    p = be("ben")
    p.goto(f"{app}/changes/{sid}")
    p.locator("#dock #apply").wait_for()
    assert p.locator("#hint").inner_text() == "Everything here will be hidden from you, except what you mark to keep."
    assert p.locator("#forSeg").count() == 0, "a member hides from themselves only: nothing to choose"
    p.locator("#apply").click()
    p.wait_for_function("() => /Checked\\./.test(document.querySelector('#why').textContent)", timeout=30000)
    p.wait_for_function("() => document.querySelector('#for').textContent === 'Hidden from you through your lens Hidden photos'")
    assert a not in seen(app, "ben") and b not in seen(app, "ben")
    assert {a, b} <= seen(app, "ada") and {a, b} <= seen(app, "cleo"), "Ben's hides are Ben's"
    # Ada: his set is not there for her, and the page says so plainly
    q = be("ada")
    q.goto(f"{app}/changes/{sid}")
    q.locator(".blank h3").wait_for()
    assert q.locator(".blank h3").inner_text() == "This change set is not here"
    assert "someone else’s" in q.locator(".blank p").inner_text()
    q.goto(f"{app}/?album={sid}")
    q.locator("#albGrid .blank h3", has_text="This album is not here").wait_for()


def test_show_hidden_says_why_and_shows_it_anyway(app, be):
    a = SHAS["a"]
    p = be("ben")
    albums(p, app)
    mine = call(app, "ben", "/api/lenses")["mine"]
    assert "Hides 2 photos" in row(p, mine).inner_text() and "Yours: always on" in row(p, mine).inner_text()
    p.locator("#albList [data-hidden]").click()                       # "See them": what his filters hide
    p.locator("#sGrid .t").first.wait_for()
    assert p.locator("#sHead h1").inner_text() == "Hidden from you"
    assert p.locator("#sGrid .t").count() == 2
    assert "hidden=1" in p.url
    i = p.evaluate(f"() => [...document.querySelectorAll('#sGrid .t')].findIndex(t => t.querySelector('img').dataset.src.endsWith('{a}'))")
    p.locator("#sGrid .t").nth(i).click()
    why = p.locator("#vWhy")
    why.wait_for()
    assert why.locator("span").inner_text().startswith("Hidden by your lens Hidden photos on ")
    assert why.locator("i").inner_text() == "Hidden photos"
    why.get_by_role("button", name="Show it anyway").click()
    why.get_by_role("button", name="Hide it again").wait_for()
    assert why.locator("span").inner_text().startswith("Shown anyway. Hidden by your lens Hidden photos")
    assert a in seen(app, "ben")
    why.get_by_role("button", name="Hide it again").click()
    why.get_by_role("button", name="Show it anyway").wait_for()
    assert a not in seen(app, "ben")
    p.keyboard.press("Escape")
    p.wait_for_function("() => !document.querySelector('#v').classList.contains('on')")
    assert p.locator("#vWhy").is_hidden()


# ── a filter published, subscribed to, copied ───────────────────────────────────────────────────

def test_ben_publishes_his_filter_and_cleo_subscribes_keeps_a_copy_and_unsubscribes(app, be):
    a, b, c = SHAS["a"], SHAS["b"], SHAS["c"]
    mine = call(app, "ben", "/api/lenses")["mine"]
    p = be("ben")
    albums(p, app)
    offer(p, mine, "Publish")
    assert "Published" in row(p, mine).inner_text()
    q = be("cleo")
    albums(q, app)
    r = row(q, mine)
    assert q.locator("#albList .lh", has_text="Published by others").count() == 1
    assert "Ben’s · Hides 2 photos · Subscribing changes what you see everywhere." in r.inner_text()
    offer(q, mine, "Subscribe")
    assert "On for you" in row(q, mine).inner_text()
    assert q.locator("#toast").inner_text().startswith("Subscribed: “Hidden photos” hides 2 photos from you everywhere")
    assert a not in seen(app, "cleo") and b not in seen(app, "cleo")
    # a snapshot: a copy of her own, and she no longer follows Ben's lens
    offer(q, mine, "Keep a copy instead")
    assert "Off: you keep a copy of it" in row(q, mine).inner_text()
    copy = lens_id(app, "cleo", mine=True, kind="filter")
    assert row(q, copy).locator("b").inner_text() == "Hidden photos (a copy)"
    assert "Hides 2 photos" in row(q, copy).inner_text() and a not in seen(app, "cleo"), "her copy hides them"
    # Ben hides one more: his lens grows, Cleo's copy does not
    sid = call(app, "ben", "/api/changes", {"title": "One more", "items": [{"sha": c}]})["id"]
    call(app, "ben", f"/api/changes/{sid}/apply", {})
    until(lambda: c not in seen(app, "ben"), "Ben's third hide was not taken in")
    assert c in seen(app, "cleo")
    # live again, then unsubscribed: Ben's third photo goes, and comes back; her copy stays hers
    albums(q, app)
    offer(q, mine, "Subscribe")
    assert c not in seen(app, "cleo")
    offer(q, mine, "Unsubscribe")
    assert "Subscribing changes what you see everywhere." in row(q, mine).inner_text()
    assert c in seen(app, "cleo") and a not in seen(app, "cleo")


# ── a series ─────────────────────────────────────────────────────────────────────────────────────

def test_ada_publishes_an_album_and_ben_opens_and_subscribes(app, be):
    a, d, e = SHAS["a"], SHAS["d"], SHAS["e"]
    aid = call(app, "ada", "/api/albums", {"title": "Ada's walk", "items": [{"sha": a}, {"sha": d}, {"sha": e}]})["id"]
    p = be("ada")
    p.goto(f"{app}/?album={aid}")
    p.locator("#albGrid .t").first.wait_for()
    p.locator("#albMore").click()
    p.locator(".menu button", has_text="Publish it as a series").click()
    p.wait_for_function("() => /Published: the family can subscribe/.test(document.querySelector('#albHead p').textContent)")
    assert p.locator("#albGrid .t").count() == 3
    lens = lens_id(app, "ben", kind="series", set_id=aid)
    q = be("ben")
    albums(q, app)
    assert q.locator(f'#albList .cards .card[data-album="{aid}"]').count() == 0, "not in his Albums until he subscribes"
    r = row(q, lens)
    assert "Ada’s series · Subscribing adds it to your Albums." in r.inner_text()
    r.locator(".what").click()                                          # opened, like an album
    q.locator("#albGrid .t").first.wait_for()
    assert q.locator("#albHead p.num").inner_text().split("·")[:2] == ["2 photos ", " Ada’s series"], "not the photo Ben hid"
    assert q.locator("#albGrid .t").count() == 2
    # back to the list, which is asked for again: its old rows show meanwhile, and take no offer until the lenses come
    with q.expect_response(lambda r: "/api/lenses" in r.url and r.request.method == "GET"):
        q.go_back()
    q.locator("#albList .bt h2", has_text="Filters").wait_for()
    offer(q, lens, "Subscribe")
    q.locator(f'#albList .cards .card[data-album="{aid}"]').wait_for()
    assert q.locator(f'#albList .cards .card[data-album="{aid}"] .badge').inner_text() == "Ada’s"
    assert "In your Albums" in row(q, lens).inner_text()


# ── hiding from everyone, and estate defaults ───────────────────────────────────────────────────

def test_ada_hides_from_everyone_with_the_review_pages_choice(app, be):
    f = SHAS["f"]
    sid = call(app, "ada", "/api/changes", {"title": "Not for the family", "items": [{"sha": f}]})["id"]
    p = be("ada")
    p.goto(f"{app}/changes/{sid}")
    p.locator("#forSeg").wait_for()
    assert [b.inner_text() for b in p.locator("#forSeg button").all()] == ["You", "Everyone"]
    assert p.locator("#forSeg button.on").inner_text() == "You", "her own lens, unless she chooses"
    p.locator("#forSeg button", has_text="Everyone").click()
    assert "Every member, unless they opt out" in p.locator("#for").inner_text()
    assert p.locator("#hint").inner_text() == "Everything here will be hidden from everyone, except what you mark to keep."
    p.locator("#apply").click()
    p.wait_for_function("() => /Checked\\./.test(document.querySelector('#why').textContent)", timeout=30000)
    p.wait_for_function("() => document.querySelector('#for').textContent"
                        " === 'Hidden from everyone through your lens Estate defaults (a member may opt out)'")
    assert all(f not in seen(app, who) for who in ("ada", "ben", "cleo"))


def test_ben_proposes_ada_accepts_cleo_opts_out(app, be):
    a = SHAS["a"]
    mine = call(app, "ben", "/api/lenses")["mine"]
    p = be("ben")
    albums(p, app)
    offer(p, mine, "Propose as an estate default")
    assert "Proposed as an estate default" in row(p, mine).inner_text()
    assert a in seen(app, "ada"), "proposing changes nothing for anyone else"
    q = be("ada")
    albums(q, app)
    assert q.locator("#albList .lh", has_text="Waiting for you to decide").count() == 1
    offer(q, mine, "Make it an estate default")
    assert a not in seen(app, "ada")
    assert "On for everyone, you too" in row(q, mine).inner_text()
    c = be("cleo")
    albums(c, app)
    assert "Ben’s · Hides 3 photos · On for everyone, you too" in row(c, mine).inner_text()
    offer(c, mine, "Opt out")
    assert "You opted out" in row(c, mine).inner_text()
    assert SHAS["c"] in seen(app, "cleo") and SHAS["c"] not in seen(app, "ada")
    # why, for Ada, now names Ben's lens
    why = call(app, "ada", f"/api/photos/{SHAS['c']}/why")
    assert why["text"].startswith("Hidden by Ben's lens “Hidden photos” on "), why


# ── who am I, and Members ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("who, name, members", [("ada", "Ada", True), ("ben", "Ben", False)])
def test_the_library_menu_names_the_member_and_offers_members_to_a_sysadmin(app, be, who, name, members):
    p = be(who)
    p.goto(app + "/")
    p.locator("#libMore").click()
    menu = p.locator(".menu")
    assert menu.locator("h5").first.inner_text() == f"You are {name}"
    assert menu.locator('a[href="/members"]').count() == (1 if members else 0)
    assert menu.locator('a[href="/request-access"]').inner_text() == "Be another member"
