"""The secondary pages in a real browser (issue #80): the regressions that only show when a request fails
or never ends, provoked with request interception (page.route) against the real servers.

- review.html: a choice made while a save is failing is the one the retry sends, and the one saved;
- people.html: a failed save leaves the Save button working, and the next save goes through; a grouping
  that failed is said so and not asked about again;
- changes.html: a library that never settles is given up on, visibly, with a way to check again; a mark
  the server refuses is put back;
- stats.html: a 500 is shown as an error, not drawn as figures; the page never has two of its polls in
  flight, whatever is pressed.

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
    """A page whose uncaught errors fail the test, and whose console errors are kept (page.said)."""
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, locale="en-US")
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


def test_no_test_wrote_to_the_demo_library(demo_dir):
    assert (demo_dir / "estate.db").is_file()
    changed = subprocess.run(["git", "status", "--porcelain", "--", "demo"], cwd=ROOT, capture_output=True, text=True).stdout
    assert not changed.strip(), f"demo/ changed:\n{changed}"
