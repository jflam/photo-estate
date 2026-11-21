"""The secondary pages in a real browser (issue #80): the regressions that only show when a request fails
or never ends, provoked with request interception (page.route) against the real servers.

- review.html: a choice made while a save is failing is the one the retry sends, and the one saved;
- people.html: a failed save leaves the Save button working, and the next save goes through; a grouping
  that failed is said so and not asked about again;
- changes.html: a library that never settles is given up on, visibly, with a way to check again; a mark
  the server refuses is put back;
- stats.html: a 500 is shown as an error, not drawn as figures; the page never has two of its polls in
  flight, whatever is pressed;
- search.html (issue #81): a question edited while its answer is on its way shows only the new
  question's photos; an album being made from a search is not made once the person has moved on, and
  left to finish takes its name from the moment it was asked for; a double click on the heart ends as
  last asked, on the page and on the server, and a save that fails is put back; the viewer opened and
  closed quickly, with Back and Forward, leaves the page usable and the address right; a session of
  every view switched faster than the server answers throws nothing.

Slow tier (`uv run pytest -m slow`): each test starts a real server (`uv run embed.py serve --demo`,
`estate.py review`, `faces.py serve`) on the demo library or on a scratch copy of its catalog, never on
demo/ itself. Needs Playwright (in the dev group) and a Chromium it can run: its own
(`uv run playwright install chromium-headless-shell`), or any already in Playwright's cache. Without
either, every test here is skipped. The logic these pages share is unit-tested without a browser in
app/tests/ (`npm test`).

  uv run pytest tests/test_pages_browser.py
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
pytestmark = [pytest.mark.slow, pytest.mark.server]
JSON = "application/json"


# ── a browser, or a skip ────────────────────────────────────────────────────────────────────────

def cached_chromium() -> str | None:
    """A Chromium that Playwright downloaded at some time, whatever build this Playwright wants."""
    home = Path.home()
    caches = [Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"])] if os.environ.get("PLAYWRIGHT_BROWSERS_PATH") else []
    caches += [home / "Library/Caches/ms-playwright", home / ".cache/ms-playwright", home / "AppData/Local/ms-playwright"]
    names = ("chrome-headless-shell", "headless_shell", "chrome", "Chromium", "chrome.exe")
    for cache in caches:
        for folder in sorted(cache.glob("chromium*"), reverse=True):
            for f in folder.rglob("*"):
                if f.name in names and f.is_file() and os.access(f, os.X_OK):
                    return str(f)
    return None


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    with sync_api.sync_playwright() as pw:
        try:
            br = pw.chromium.launch()
        except Exception:
            exe = cached_chromium()
            if not exe:
                pytest.skip("no Chromium for Playwright: `uv run playwright install chromium-headless-shell`")
            try:
                br = pw.chromium.launch(executable_path=exe)
            except Exception as e:
                pytest.skip(f"the cached Chromium does not start: {e}")
        yield br
        br.close()


@pytest.fixture
def page(browser):
    """A page whose uncaught errors fail the test, and whose console errors are kept (page.said). No service
    worker: a request that passes through one (the photo app's, once it has claimed the page) is not seen by
    page.route, and whether it has yet is a matter of timing."""
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, locale="en-US", service_workers="block")
    p = ctx.new_page()
    p.errors, p.said = [], []
    p.on("pageerror", lambda e: p.errors.append(str(e)))
    p.on("console", lambda m: p.said.append(m.text) if m.type == "error" else None)
    yield p
    ctx.close()
    assert not p.errors, f"uncaught errors in the page: {p.errors}"
    own = [t for t in p.said if "Failed to load resource" not in t]       # the browser's own line for a failed request
    assert not own, f"console errors: {own}"


# ── the servers ─────────────────────────────────────────────────────────────────────────────────

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(argv: list[str], port: int, ready: str, log: Path):
    """Start a server with `uv run` (its own PEP 723 environment, as a person starts it) and wait for it."""
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    env.setdefault("HF_HUB_OFFLINE", "1")              # never download the model from a test
    env.pop("VIRTUAL_ENV", None)
    out = log.open("w")
    proc = subprocess.Popen(["uv", "run", "--quiet", *argv], cwd=ROOT, env=env, stdout=out, stderr=subprocess.STDOUT,
                            start_new_session=True)
    base = f"http://127.0.0.1:{port}"
    for _ in range(600):
        if proc.poll() is not None:
            break
        try:
            with urllib.request.urlopen(base + ready, timeout=2):
                return proc, base
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)
    stop(proc)
    raise AssertionError(f"{' '.join(argv)} did not start:\n{log.read_text()[-2000:]}")


def stop(proc) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        proc.kill()


def call(base: str, path: str, body=None):
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": JSON})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


@pytest.fixture(scope="module")
def demo_app(tmp_path_factory):
    """The photo app on the demo library (it serves a scratch copy of the catalog)."""
    port = free_port()
    proc, base = serve(["embed.py", "serve", "--demo", "--port", str(port), "--no-pregenerate"], port, "/api/info",
                       tmp_path_factory.mktemp("demo") / "server.log")
    yield base
    stop(proc)


@pytest.fixture(scope="module")
def review_app(tmp_path_factory, demo_dir):
    """`estate.py review` on a scratch copy of the demo catalog; choices go to a scratch folder."""
    tmp = tmp_path_factory.mktemp("review")
    shutil.copyfile(demo_dir / "estate.db", tmp / "estate.db")
    port = free_port()
    proc, base = serve(["estate.py", "--db", str(tmp / "estate.db"), "review", "--store", str(demo_dir / "store"),
                        "--port", str(port), "--threshold", "26", "--out", str(tmp / "out")], port, "/groups.json", tmp / "server.log")
    yield base
    stop(proc)


def synthetic_faces(db: Path) -> None:
    """The demo has no faces: give a scratch copy of its catalog one made-up face per photo (two people,
    half of each named, and a stranger who turns up often), so the People page has something to ask."""
    np = pytest.importorskip("numpy")
    import faces
    from core import schema
    con = faces.connect(str(db))
    schema.ensure(con, "faces")
    shas = [r[0] for r in con.execute("SELECT sha256 FROM assets WHERE kind='image' ORDER BY taken_at")]
    rng = np.random.default_rng(80)
    unit = lambda v: v / np.linalg.norm(v)
    centres = [unit(rng.standard_normal(512)) for _ in range(3)]
    con.executemany("INSERT INTO persons(id, name, created_at) VALUES (?,?,'')", [(1, "Ada Lake"), (2, "Ben Quick")])
    for i, sha in enumerate(shas):
        who = i % 3
        named = who < 2 and i % 4 < 2
        v = unit(centres[who] + (0.03 if named or who == 2 else 0.07 if i % 8 < 4 else 0.085) * rng.standard_normal(512))
        con.execute("INSERT INTO faces(id, sha256, idx, model, x, y, w, h, score, px, vec, person_id, source)"
                    " VALUES (?,?,0,?,?,?,0.3,0.3,0.9,120,?,?,?)",
                    (100 + i, sha, faces.MODEL, 0.2 + 0.05 * (i % 5), 0.2, v.astype(np.float16).tobytes(),
                     who + 1 if named else None, "you" if named else None))
    con.commit()
    con.close()


@pytest.fixture(scope="module")
def people_app(tmp_path_factory, demo_dir):
    """`faces.py serve` on a scratch copy of the demo catalog with synthetic faces."""
    tmp = tmp_path_factory.mktemp("people")
    shutil.copyfile(demo_dir / "estate.db", tmp / "estate.db")
    synthetic_faces(tmp / "estate.db")
    port = free_port()
    proc, base = serve(["faces.py", "--db", str(tmp / "estate.db"), "serve", "--store", str(demo_dir / "store"),
                        "--port", str(port)], port, "/api/people", tmp / "server.log")
    yield base
    stop(proc)


# ── what the tests share ────────────────────────────────────────────────────────────────────────

def refuse(status: int = 500, error: str = "database is locked"):
    return lambda route: route.fulfill(status=status, content_type=JSON, body=json.dumps({"error": error}))


def advance(page, ms: int, times: int, until=None) -> bool:
    """Move the page's clock (page.clock.install() first) forward `times` steps of `ms`, letting requests
    land between steps; stop early when `until()` is true."""
    for _ in range(times):
        page.clock.run_for(ms)
        page.wait_for_timeout(10)
        if until and until():
            return True
    return False


# ── review.html ─────────────────────────────────────────────────────────────────────────────────

def test_review_a_newer_edit_survives_a_failed_save(page, review_app):
    page.goto(review_app + "/")
    group = page.locator("section.group").first
    group.locator("figure").first.wait_for()
    gid = call(review_app, "/groups.json")["groups"][0]["id"]
    refused = []
    page.route("**/decisions", lambda r: (refused.append(r.request.post_data_json), refuse(500, "disk full")(r)))
    group.locator("figure").nth(1).click()                      # drop the second photo: the save fails
    page.locator("#retry").wait_for(timeout=10000)
    assert "Couldn’t save" in page.locator("#status").inner_text()
    group.locator("figure").nth(2).click()                      # then drop the third too, while it is failing
    shown = sorted(page.evaluate("""() => [...document.querySelector('section.group').querySelectorAll('figure.keep img')]
                                           .map(i => i.getAttribute('src').split('/').pop())"""))
    assert len(refused[0][str(gid)]["keep"]) == len(shown) + 1, "the save that failed carried the older choice"
    page.unroute("**/decisions")
    with page.expect_request("**/decisions") as sent:
        page.locator("#retry").click()
    assert sorted(sent.value.post_data_json[str(gid)]["keep"]) == shown, "the retry sends the newer choice"
    page.wait_for_function("document.querySelector('#status').textContent === 'All changes saved'", timeout=10000)
    assert page.locator("#retry").is_hidden()
    assert sorted(call(review_app, "/decisions.json")[str(gid)]["keep"]) == shown, "and the newer choice is what is saved"


# ── people.html ─────────────────────────────────────────────────────────────────────────────────

def test_people_recovers_after_an_api_error(page, people_app):
    page.goto(people_app + "/people#/review/1")
    save = page.locator("#save")
    save.wait_for()
    before = call(people_app, "/api/people")["named"]
    page.route("**/api/people/label-many", refuse(500, "database is locked"))
    save.click()
    page.wait_for_function("document.querySelector('#toast').textContent.includes('Could not save: database is locked')")
    assert save.is_enabled(), "the Save button works again after the failure"
    assert call(people_app, "/api/people")["named"] == before, "nothing was saved"
    faces_shown = page.locator("#grid .f").count()
    page.unroute("**/api/people/label-many")
    save.click()                                                # the same button, the same choices
    page.wait_for_function("document.querySelector('#toast').textContent.startsWith('Saved ')")
    assert call(people_app, "/api/people")["named"] == before + faces_shown, "the next save went through"


def test_people_a_batch_that_fails_midway_keeps_the_page_in_step(page, people_app):
    page.goto(people_app + "/people#/odd/2")
    tiles = page.locator("#grid .f")
    tiles.first.wait_for()
    n, named = tiles.count(), call(people_app, "/api/people")["named"]
    tiles.nth(0).locator(".more").click()                       # this one is someone else: a request of its own
    page.locator(".menu input").fill("Ada")
    page.keyboard.press("Enter")
    tiles.nth(1).click()                                        # these two are just not Ben: one request for both
    tiles.nth(2).click()
    sent = []

    def second_fails(route):
        sent.append(route.request.post_data_json)
        route.fallback() if len(sent) == 1 else refuse()(route)
    page.route("**/api/people/label", second_fails)
    page.locator("#next").click()
    page.wait_for_function("document.querySelector('#toast').textContent.includes('1 taken off Ben, 2 not saved')")
    assert tiles.count() == n - 1, "the face that was moved is gone from the page, as it is on the server"
    assert page.locator("#grid .f.no").count() == 2, "the two that were not saved keep their marks"
    assert page.locator("#next").is_enabled() and "Take 2 off Ben" in page.locator("#next").inner_text()
    assert call(people_app, "/api/people")["named"] == named, "one face moved from Ben to Ada: still named"
    page.unroute("**/api/people/label")
    with page.expect_request("**/api/people/label") as again:
        page.locator("#next").click()
    assert len(again.value.post_data_json["faces"]) == 2, "pressing again sends only what was left"
    page.wait_for_function("document.querySelector('#toast').textContent.includes('3 taken off Ben')")
    assert call(people_app, "/api/people")["named"] == named - 2


def test_people_stops_asking_when_the_grouping_failed(page, people_app):
    asked = []
    page.route("**/api/people/unknown*", lambda r: (asked.append(1), r.fulfill(
        status=200, content_type=JSON, body=json.dumps({"state": "failed", "groups": [], "error": "MemoryError: 12 GiB"}))))
    page.goto(people_app + "/people")
    page.wait_for_function("document.querySelector('#unknown')?.textContent.includes('could not be grouped: MemoryError: 12 GiB')")
    page.wait_for_timeout(4000)                                 # the old page asked again every 1.5 s
    assert len(asked) == 1, "failed is final: not asked again"


def test_people_stops_asking_when_the_view_changes_and_gives_up_in_the_end(page, people_app):
    asked = []
    page.clock.install()
    page.route("**/api/people/unknown*", lambda r: (asked.append(1), r.fulfill(
        status=200, content_type=JSON, body=json.dumps({"state": "working", "groups": []}))))
    page.goto(people_app + "/people")
    page.wait_for_function("document.querySelector('#unknown')?.textContent.includes('Grouping the unnamed faces')")
    advance(page, 2500, 3)
    assert len(asked) >= 2, "it asks again while the server is working"
    page.evaluate("location.hash = '#/person/1'")               # another view: the asking belongs to the one left
    page.locator(".who-head").wait_for()
    n = len(asked)
    advance(page, 10000, 6)
    assert len(asked) <= n + 1, "no asking for a view that is gone"
    page.evaluate("location.hash = '#/'")
    page.locator("#unknown").wait_for()
    assert advance(page, 10000, 80, until=lambda: page.locator("#unknownAgain").count() > 0), "ten minutes, then it gives up"
    assert "Still grouping the unnamed faces after ten minutes" in page.locator("#unknown").inner_text()
    n = len(asked)
    advance(page, 10000, 6)
    assert len(asked) == n, "and asks no more until Check again is pressed"


# ── changes.html ────────────────────────────────────────────────────────────────────────────────

def a_change_set(base: str, title: str) -> int:
    shas = [r["sha"] for r in call(base, "/api/search?limit=6&dedupe=0&shots=1")["results"]]
    return call(base, "/api/changes", {"title": title, "prompt": "a test of the page", "op": "hide",
                                       "items": [{"sha": s, "score": .5, "reason": "test"} for s in shas]})["id"]


def test_changes_settle_gives_up_visibly_instead_of_spinning_for_ever(page, demo_app):
    sid = a_change_set(demo_app, "Never settles")
    page.clock.install()
    page.goto(f"{demo_app}/changes/{sid}")
    page.locator("#grid .t").first.wait_for()
    asked = []
    page.route(f"**/api/changes/{sid}/check", lambda r: (asked.append(1), r.fulfill(status=200, content_type=JSON, body=json.dumps(
        {"id": sid, "busy": True, "ok": False, "wrong": [], "hidden": 0, "visible": 0}))))
    page.locator("#apply").click()
    page.wait_for_function("document.querySelector('#why').textContent === 'Updating the library…'")
    assert advance(page, 700, 220, until=lambda: page.locator("#recheck").count() > 0), "two minutes, then it gives up"
    assert "Still updating" in page.locator("#why").inner_text()
    n = len(asked)
    assert 100 < n <= 172, f"asked {n} times"
    advance(page, 700, 20)
    assert len(asked) == n, "it has stopped asking"
    page.unroute(f"**/api/changes/{sid}/check")
    page.locator("#recheck").click()                            # the way out: check again, now that the app answers
    assert advance(page, 700, 60, until=lambda: page.locator("#why .ok").count() > 0)
    assert "Checked." in page.locator("#why").inner_text()
    page.once("dialog", lambda d: d.accept())
    page.locator("#undo").click()                               # leave the demo's scratch catalog as it was
    assert advance(page, 700, 60, until=lambda: page.locator("#why .ok").count() > 0)


def test_changes_a_refused_mark_is_put_back_from_the_server(page, demo_app):
    sid = a_change_set(demo_app, "Marks")
    page.goto(f"{demo_app}/changes/{sid}")
    tiles = page.locator("#grid .t")
    tiles.first.wait_for()
    tiles.nth(0).click()                                        # saved
    page.wait_for_function("document.querySelectorAll('#grid .t.keep').length === 1")
    page.route(f"**/api/changes/{sid}/decide", refuse())
    tiles.nth(1).click()                                        # refused
    page.wait_for_function("document.querySelector('#toast').textContent.includes('Could not save that: database is locked')")
    kept = [it["sha"] for it in call(demo_app, f"/api/changes/{sid}")["items"] if it["decision"] == "reject"]
    assert page.evaluate("[...document.querySelectorAll('#grid .t.keep')].map(t => t.dataset.sha)") == kept, \
        "the page shows what the server has: the first mark, not the refused one"
    assert "1 kept" in page.locator("#sum").inner_text()
    page.unroute(f"**/api/changes/{sid}/decide")
    tiles.nth(1).click()                                        # and the same mark can be made again
    page.wait_for_function("document.querySelectorAll('#grid .t.keep').length === 2")
    page.wait_for_timeout(300)
    assert len([it for it in call(demo_app, f"/api/changes/{sid}")["items"] if it["decision"] == "reject"]) == 2


# ── stats.html ──────────────────────────────────────────────────────────────────────────────────

def test_stats_shows_an_error_on_a_500(page, demo_app):
    page.route("**/api/stats*", refuse(500, "The catalog is locked"))
    page.goto(demo_app + "/stats")
    page.locator("#again").wait_for()
    assert page.locator("#out h3").inner_text() == "Couldn’t take stock"
    assert page.locator("#out p").inner_text() == "The catalog is locked"
    assert page.locator(".cards").count() == 0, "an error is not drawn as figures"
    page.unroute("**/api/stats*")
    page.locator("#again").click()                              # and trying again works, without a reload
    page.locator(".cards").wait_for()


def test_stats_has_one_poll_loop(page, demo_app):
    """The server is still measuring and a catalogue run is going, so the old page had two timers asking;
    the buttons that ask again are pressed on top. Never two of the page's polls in flight at once."""
    flying, most, asked = set(), [0], []

    def began(req):
        if "/api/stats" in req.url or "/api/ingest" in req.url:
            flying.add(req); asked.append(urllib.parse.urlparse(req.url).path); most[0] = max(most[0], len(flying))
    page.on("request", began)
    page.on("requestfinished", lambda req: flying.discard(req))
    page.on("requestfailed", lambda req: flying.discard(req))
    figures = call(demo_app, "/api/stats")
    for _ in range(100):
        if figures["state"] != "working":
            break
        time.sleep(0.2)
        figures = call(demo_app, "/api/stats")
    page.clock.install()
    page.route("**/api/stats*", lambda r: r.fulfill(status=200, content_type=JSON, body=json.dumps({**figures, "state": "working"})))
    page.route("**/api/ingest", lambda r: r.fulfill(status=200, content_type=JSON, body=json.dumps(
        {"enabled": True, "running": True, "waiting": {"files": 3, "bytes": 3000}, "log": ["reading"], "exit_code": None,
         "finished_at": None, "restart_needed": False, "can_restart": False})))
    page.goto(demo_app + "/stats")
    page.locator("#refresh").wait_for(state="attached")
    page.locator("#up").wait_for()
    for _ in range(8):                                          # 12 s of the page's time, pressing as it goes
        advance(page, 500, 3)
        page.evaluate("document.querySelector('#refresh')?.click(); document.querySelector('#upMore')?.click()")
    assert most[0] == 1, f"{most[0]} polls in flight at once"
    stats = [p for p in asked if p == "/api/stats"]
    assert 8 <= len(stats) <= 12 + 9, f"{len(stats)} asks for the figures in 12 s: one loop, every 1.5 s or at a press"
    assert all(a != b for a, b in zip(asked, asked[1:])), "the two questions take turns in the one loop"


# ── search.html (issue #81) ─────────────────────────────────────────────────────────────────────

class Hold:
    """Requests kept waiting (page.route), let go when the test says so: how an answer is made to arrive
    after the person has moved on."""

    def __init__(self, page, pattern, when=lambda request: True):
        self.page, self.pattern, self.held, self.seen = page, pattern, [], []
        self.handler = lambda route: (self.seen.append(route.request), self.held.append(route)) if when(route.request) else route.fallback()
        page.route(pattern, self.handler)

    def wait(self, n=1):
        for _ in range(200):
            if len(self.held) >= n:
                return
            self.page.wait_for_timeout(25)
        raise AssertionError(f"{len(self.held)} of {n} requests for {self.pattern} were made")

    def release(self):
        """Let everything held, and everything after it, through."""
        self.page.unroute(self.pattern, self.handler)
        for route in self.held:
            try:
                route.continue_()
            except Exception:                              # the page gave the request up: there is nothing to answer
                pass
        self.held = []


def watch_search(page):
    """Console warnings (a refused step of a machine is one) and requests the page gave up, as they happen."""
    page.warned, page.dropped = [], []
    page.on("console", lambda m: page.warned.append(m.text) if m.type == "warning" and "blocked by Playwright" not in m.text else None)
    page.on("requestfailed", lambda r: page.dropped.append(r.url))


def tiles(page, grid="#sGrid"):
    return page.evaluate(f"""() => [...document.querySelectorAll('{grid} .t img')].map(i => (i.dataset.src || i.src).split('/').pop())""")


def heading(page):
    return page.locator("#sHead h1").inner_text()


def test_search_a_query_edited_mid_fetch_shows_only_the_new_query(page, demo_app):
    watch_search(page)
    want = [r["sha"] for r in call(demo_app, "/api/search?q=waterfall&dedupe=1&shots=0&limit=150")["results"]]
    kittens = {r["sha"] for r in call(demo_app, "/api/search?q=kitten&dedupe=1&shots=0&limit=150")["results"]}
    assert want and kittens and set(want) != kittens
    page.goto(demo_app + "/?search")
    page.locator("#browse .idea").first.wait_for()
    slow = Hold(page, "**/api/search?*", lambda r: "q=kitten" in r.url)
    page.locator("#q").fill("kitten")
    page.keyboard.press("Enter")
    slow.wait()                                                 # the first question is on its way, and stays there
    page.locator("#q").fill("waterfall")
    page.keyboard.press("Enter")
    page.wait_for_function("document.querySelector('#sHead h1').textContent === '“waterfall”'")
    page.wait_for_function(f"document.querySelectorAll('#sGrid .t').length === {len(want)}")
    slow.release()                                              # now the first answer may come
    page.wait_for_timeout(600)
    assert heading(page) == "“waterfall”"
    assert tiles(page) == want, "the grid shows the new question's photos, and only those"
    assert any("q=kitten" in u and "cursor=0" in u for u in page.dropped), "the first question's request was aborted"
    assert page.url.endswith("?q=waterfall")
    assert not page.warned


def albums_made(base):
    return [s["title"] for s in call(base, "/api/albums")["sets"]]


@pytest.mark.parametrize("move", ["navigation", "a query edit"])
def test_search_an_album_from_a_search_is_not_made_after_moving_on(page, demo_app, move):
    watch_search(page)
    before = albums_made(demo_app)
    posted = []
    page.on("request", lambda r: posted.append(r.post_data_json) if r.method == "POST" and r.url.endswith("/api/albums") else None)
    page.goto(demo_app + "/?q=kitten")
    page.locator("#sGrid .t").first.wait_for()
    slow = Hold(page, "**/api/search?*", lambda r: "limit=500" in r.url)     # the pages the album is collected from
    page.locator("#sAlbum").click()
    page.locator(".menu form input").fill("Kittens, never made")
    page.locator(".menu form button").click()
    slow.wait()
    if move == "navigation":
        page.locator('#tabbar [data-tab="library"]').click()
        page.locator("#libGrid .t").first.wait_for()
    else:
        page.locator("#q").fill("waterfall")
        page.keyboard.press("Enter")
        page.wait_for_function("document.querySelector('#sHead h1').textContent === '“waterfall”'")
    slow.release()
    page.wait_for_timeout(700)
    assert posted == [], "no album was asked for"
    assert len(slow.seen) == 1, "no further page of the search was asked for"
    assert any("limit=500" in u for u in page.dropped), "the page being read was aborted"
    assert albums_made(demo_app) == before
    assert page.locator(".menu").count() == 0 and page.locator("#toast.on").count() == 0, "and nothing says one was"
    assert ("?q=waterfall" in page.url) if move != "navigation" else page.url.rstrip("/") == demo_app
    assert not page.warned


def test_search_an_album_from_a_search_takes_its_name_from_the_start(page, demo_app):
    """Left to finish, the album is of the question, and under the note, of the moment it was asked for
    (the demo app serves a scratch copy of the catalog: the album is made there)."""
    posted = []
    page.on("request", lambda r: posted.append(r.post_data_json) if r.method == "POST" and r.url.endswith("/api/albums") else None)
    kittens = [r["sha"] for r in call(demo_app, "/api/search?q=kitten&dedupe=1&shots=0&limit=500")["results"]]
    page.goto(demo_app + "/?q=kitten")
    page.locator("#sGrid .t").first.wait_for()
    slow = Hold(page, "**/api/search?*", lambda r: "limit=500" in r.url)
    page.locator("#sAlbum").click()
    page.locator(".menu form input").fill("Kittens of #81")
    page.locator(".menu form button").click()
    slow.wait()
    page.evaluate("document.querySelector('#sHead h1').textContent = 'Something else'")    # the page changes under it
    slow.release()
    page.wait_for_function("document.querySelector('#albHead h1').textContent === 'Kittens of #81'")
    assert [(a["title"], a["prompt"], [i["sha"] for i in a["items"]]) for a in posted] == [("Kittens of #81", "From the search “kitten”", kittens)]
    assert "?album=" in page.url
    assert tiles(page, "#albGrid") == kittens


def test_search_a_double_click_on_the_heart_ends_as_last_asked(page, demo_app):
    watch_search(page)
    sent = []
    page.on("request", lambda r: sent.append(r.post_data_json) if r.url.endswith("/api/favorite") else None)
    page.goto(demo_app + "/?q=kitten")
    page.locator("#sGrid .t").nth(2).click()
    heart = page.locator("#vFavBtn")
    heart.wait_for()
    sha = tiles(page)[2]
    truth = lambda: next(r for r in call(demo_app, "/api/search?q=kitten&dedupe=1&shots=0&limit=150")["results"] if r["sha"] == sha)["fav"]
    assert heart.get_attribute("aria-pressed") == "false" and not truth()
    slow = Hold(page, "**/api/favorite")
    heart.dblclick()                                            # on, and off again before the first is answered
    slow.wait()
    assert heart.get_attribute("aria-pressed") == "false", "the page shows what was last asked for"
    assert len(sent) == 1, "one save at a time"
    slow.release()
    page.wait_for_function("document.querySelectorAll('#sGrid .t')[2].querySelector('.fv') === null")
    for _ in range(100):
        if len(sent) == 2:
            break
        page.wait_for_timeout(20)
    page.wait_for_timeout(300)
    assert [(s["sha"], s["fav"]) for s in sent] == [(sha, True), (sha, False)], "the last request is the last thing asked for"
    assert heart.get_attribute("aria-pressed") == "false" and not truth(), "and the page and the server agree"

    heart.click()                                               # once: on, and it stays on
    page.wait_for_function("document.querySelectorAll('#sGrid .t')[2].querySelector('.fv') !== null")
    page.wait_for_timeout(300)
    assert heart.get_attribute("aria-pressed") == "true" and truth() and len(sent) == 3

    page.route("**/api/favorite", refuse(500, "database is locked"))        # a save that fails is put back, and said
    heart.click()
    page.wait_for_function("document.querySelector('#vNote').textContent === 'Couldn’t save the favourite'")
    assert heart.get_attribute("aria-pressed") == "true" and truth()
    assert page.locator("#sGrid .t").nth(2).locator(".fv").count() == 1
    page.unroute("**/api/favorite")
    heart.click()                                               # and it can be tried again
    page.wait_for_function("document.querySelectorAll('#sGrid .t')[2].querySelector('.fv') === null")
    page.wait_for_timeout(300)
    assert not truth() and not page.warned


def test_search_the_viewer_opened_and_closed_quickly_with_back_and_forward(page, demo_app):
    watch_search(page)
    page.goto(demo_app + "/?q=kitten")
    grid = page.locator("#sGrid .t")
    grid.first.wait_for()
    home = page.url
    state = lambda: page.evaluate("[document.querySelector('#v').classList.contains('on'), !!history.state.v, history.state.d, location.search]")
    closed = lambda: page.wait_for_function("!document.querySelector('#v').classList.contains('on') && !history.state.v")
    for i in range(3):                                          # the button (Escape), pressed while the photo is still flying in
        grid.nth(i).click()
        page.evaluate(f"""() => {{ document.dispatchEvent(new KeyboardEvent('keydown', {{ key: 'Escape' }}));
                                  document.querySelectorAll('#sGrid .t')[{i + 1}].click(); }}""")    # at once: the last one is still leaving
        closed()
    assert state() == [False, False, 0, "?q=kitten"]
    for i in range(3):                                          # Back, pressed at once
        grid.nth(i).click()
        page.go_back()
        closed()
    assert state() == [False, False, 0, "?q=kitten"]
    page.go_forward()                                           # into the entry a photo left: stepped over, back to the search
    page.wait_for_timeout(400)
    assert state() == [False, False, 0, "?q=kitten"]
    grid.nth(1).click()                                         # open, on to the next two, close: the tile it goes back into
    page.keyboard.press("ArrowRight"); page.keyboard.press("ArrowRight")
    page.wait_for_function("document.querySelector('#vPrev').disabled === false")
    page.keyboard.press("Escape")
    page.locator('#tabbar [data-tab="albums"]').click()         # a step asked for while the viewer is leaving waits its turn
    page.wait_for_function("location.search === '?albums' && !document.querySelector('#v').classList.contains('on')")
    assert page.evaluate("history.state.d") == 1
    page.go_back()
    page.wait_for_function("location.search === '?q=kitten'")
    grid.first.wait_for()
    grid.nth(4).click()                                         # and the page is still usable
    page.wait_for_function("document.querySelector('#v').classList.contains('on') && history.state.v === 1")
    page.locator("#vClose").click()
    closed()
    assert page.url == home and not page.warned


def test_search_a_rapid_navigation_session_throws_nothing(page, demo_app):
    """Every view, switched faster than the server answers (each request is kept back until a few
    actions later): no uncaught error (a TransitionError is one; the `page` fixture fails the test on
    any), no console warning, and the page ends where the last action left it."""
    watch_search(page)
    made = call(demo_app, "/api/albums", {"title": "For the session", "prompt": "", "items": [
        {"sha": r["sha"]} for r in call(demo_app, "/api/search?q=waterfall&limit=5")["results"]]})["id"]
    page.goto(demo_app + "/")
    page.locator("#libGrid .t").first.wait_for()
    slow = Hold(page, "**/api/**", lambda r: "/api/log" not in r.url)
    js = lambda code: page.evaluate(f"() => {{ {code} }}")
    click = lambda sel: js(f"document.querySelector('{sel}')?.click()")
    ask = lambda q: js(f"const q = document.querySelector('#q'); q.focus(); q.value = '{q}'; q.dispatchEvent(new Event('input'));"
                       "q.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))")
    steps = [
        lambda: click('#scale [data-s="years"]'), lambda: click('#scale [data-s="months"]'), lambda: click("#libPeriods .card"),
        lambda: click("#monNav button"), lambda: click("#monGrid .t"), lambda: page.keyboard.press("ArrowRight"), lambda: page.keyboard.press("Escape"),
        lambda: click('#tabbar [data-tab="albums"]'), lambda: click(f'[data-album="{made}"]'), lambda: click("#albGrid .t"), lambda: page.keyboard.press("f"),
        lambda: page.go_back(), lambda: click('#tabbar [data-tab="search"]'), lambda: ask("kit"), lambda: ask("kitten"), lambda: click("#sGrid .t"),
        lambda: page.keyboard.press("f"), lambda: page.keyboard.press("f"), lambda: page.go_back(), lambda: click("#sAlbum"),
        lambda: click(".menu form button"), lambda: ask("waterfall"), lambda: click("#chips .chip"), lambda: page.keyboard.press("Escape"),
        lambda: page.go_back(), lambda: page.go_back(), lambda: page.go_forward(), lambda: click('#tabbar [data-tab="library"]'),
        lambda: click('#scale [data-s="all"]'), lambda: js("scrollTo(0, 4000)"), lambda: click("#libGrid .t"), lambda: page.keyboard.press("Escape"),
        lambda: click('#tabbar [data-tab="search"]'), lambda: ask("dog in the snow"),
    ]
    for round_ in range(3):
        for n, step in enumerate(steps):
            try:
                step()
            except Exception as e:                              # a navigation Playwright was still waiting on: the page's errors are what count
                assert "Timeout" in str(e) or "navigation" in str(e).lower(), e
            page.wait_for_timeout(15)
            if (n + round_) % 5 == 4:                           # the server catches up, a few actions late
                slow.release()
                page.wait_for_timeout(30)
                slow = Hold(page, "**/api/**", lambda r: "/api/log" not in r.url)
    slow.release()
    page.wait_for_function("document.querySelector('#sHead h1').textContent === '“dog in the snow”' && document.querySelectorAll('#sGrid .t img').length > 0")
    want = [r["sha"] for r in call(demo_app, "/api/search?q=dog+in+the+snow&dedupe=1&shots=0&limit=150")["results"]]
    assert tiles(page) == want, "after all that, the grid is the last question's"
    assert page.url.endswith("?q=dog+in+the+snow")
    page.locator("#sGrid .t").first.click()                     # and the page still works
    page.wait_for_function("document.querySelector('#v').classList.contains('on')")
    page.keyboard.press("Escape")
    page.wait_for_function("!document.querySelector('#v').classList.contains('on') && !history.state.v")
    assert not page.warned, page.warned
    assert not [t for t in page.said if "TransitionError" in t or "no step from" in t]


def test_no_test_wrote_to_the_demo_library(demo_dir):
    assert (demo_dir / "estate.db").is_file()
    changed = subprocess.run(["git", "status", "--porcelain", "--", "demo"], cwd=ROOT, capture_output=True, text=True).stdout
    assert not changed.strip(), f"demo/ changed:\n{changed}"
