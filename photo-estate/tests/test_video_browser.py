"""Videos and Live Photos in the photo app (search.html), in a real browser that can play them.

On a library with two videos and two Live Photos in it (tools/video_demo.py's build(): the demo library
copied, a few clips made from its own photos, embedded so a search finds them):

- a video's tile says how long it is, a Live Photo's says LIVE;
- a video opens playing, in place of its picture; one found by a typed search starts at the moment
  the words match (the waterfall, 8-12 s into "Coast to mountains"); Space pauses and plays it;
- leaving it (the next photo, the one before, closing) stops it and lets its stream go;
- a video this browser cannot play shows its poster, says so, and offers the file;
- a Live Photo's LIVE control, held, plays its clip over the still (nothing of it is asked for before);
  let go, the still is back;
- the Videos chip (kind=video) shows only the videos;
- and all of it with nothing said in the console.

Slow tier (`uv run pytest -m slow`): builds the library in a scratch folder (about 30 s, needs the SigLIP 2
model in the Hugging Face cache) and starts the app on it, never on demo/. Playwright's own Chromium has no
H.264 decoder, so this needs a browser that has one: installed Google Chrome (Playwright's channel
"chrome"), else Playwright's WebKit. Without either every test here is skipped. The machine these steps
follow (app/video-flow.js) is unit-tested without a browser in app/tests/ (`npm test`).

  uv run pytest tests/test_video_browser.py
"""
from __future__ import annotations

import importlib.util
import os
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
pytestmark = [pytest.mark.slow, pytest.mark.server, pytest.mark.model]

# What the page has of the <video> elements in it: the viewer's video, and a Live Photo's clip (class "clip").
MEDIA = """() => [...document.querySelectorAll('video')].filter(v => v.isConnected).map(v => ({
  clip: v.classList.contains('clip'), src: v.getAttribute('src'), t: v.currentTime, ready: v.readyState,
  paused: v.paused, muted: v.muted }))"""
H264 = """() => document.createElement('video').canPlayType('video/mp4; codecs="avc1.42E01E"')"""


# ── a browser that can play H.264, or a skip ─────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    with sync_api.sync_playwright() as pw:
        tried = []
        for name, launch in (("Google Chrome", lambda: pw.chromium.launch(channel="chrome")),
                             ("WebKit", lambda: pw.webkit.launch())):
            try:
                br = launch()
            except Exception as e:
                tried.append(f"{name}: {str(e).splitlines()[0]}")
                continue
            page = br.new_page()
            can = page.evaluate(H264)
            page.close()
            if can:
                yield br
                br.close()
                return
            br.close()
            tried.append(f"{name}: no H.264")
        pytest.skip("no browser here plays H.264 (Playwright's Chromium cannot): " + "; ".join(tried))


# ── the library and the app on it ─────────────────────────────────────────────────────────────────

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def video_demo():
    """tools/video_demo.py, imported by its path (tools/ is not a package)."""
    spec = importlib.util.spec_from_file_location("video_demo", ROOT / "tools" / "video_demo.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    """The video library built in a scratch folder, and the app serving it: its address."""
    root = video_demo().build(tmp_path_factory.mktemp("videos") / "lib")
    port, log = free_port(), root / "serve.log"
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    env.setdefault("HF_HUB_OFFLINE", "1")              # never download the model from a test
    env.pop("VIRTUAL_ENV", None)
    with log.open("w") as out:
        proc = subprocess.Popen(["uv", "run", "--quiet", "embed.py", "--db", str(root / "estate.db"), "serve",
                                 "--store", str(root / "store"), "--port", str(port)],
                                cwd=ROOT, env=env, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(600):
            if proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(base + "/api/info", timeout=2):
                    break
            except (urllib.error.URLError, OSError):
                time.sleep(0.2)
        else:
            raise AssertionError("the app did not start")
        assert proc.poll() is None, f"the app did not start:\n{log.read_text()[-2000:]}"
        yield base
    finally:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=10)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            proc.kill()


@pytest.fixture
def page(browser):
    """A page whose uncaught errors and console errors fail the test. No service worker."""
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, locale="en-US", service_workers="block")
    p = ctx.new_page()
    p.errors, p.said = [], []
    p.on("pageerror", lambda e: p.errors.append(str(e)))
    p.on("console", lambda m: p.said.append(m.text) if m.type == "error" else None)
    yield p
    ctx.close()
    assert not p.errors, f"uncaught errors in the page: {p.errors}"
    assert not p.said, f"console errors: {p.said}"


def media(p) -> list[dict]:
    return p.evaluate(MEDIA)


def wait_media(p, check, what: str, within: float = 8.0) -> list[dict]:
    """Wait until check(media(p)) holds; fail saying `what` with the last state seen."""
    end = time.monotonic() + within
    while True:
        m = media(p)
        if check(m):
            return m
        if time.monotonic() > end:
            raise AssertionError(f"{what}: {m}")
        p.wait_for_timeout(100)


def playing(m: list[dict], clip: bool = False) -> bool:
    return len(m) == 1 and m[0]["clip"] == clip and not m[0]["paused"] and m[0]["ready"] >= 2


def open_tile(p, which: str) -> None:
    """Open the first tile in the Search grid that has `which` (".dur": a video, ".lv": a Live Photo)."""
    i = p.eval_on_selector_all("#sGrid .t", f"ts => ts.findIndex(t => t.querySelector('{which}'))")
    assert i >= 0, f"no tile with {which}"
    p.locator("#sGrid .t").nth(i).click()


# ── the tests ─────────────────────────────────────────────────────────────────────────────────────

def test_videos_chip_tiles_and_playing(app, page):
    """The Videos chip shows the videos, each tile with its length; one opens playing, Space pauses and plays
    it, and the next video takes its place with no second stream; closed, nothing plays."""
    p = page
    p.goto(app + "/?kind=video")
    p.wait_for_selector("#sGrid .t img.in")
    assert p.inner_text("#sHead h1") == "Videos"
    assert p.get_attribute('#chips .scope [data-scope="video"]', "aria-checked") == "true"
    badges = p.eval_on_selector_all("#sGrid .t", "ts => ts.map(t => t.querySelector('.dur')?.textContent)")
    assert sorted(badges) == ["0:05", "0:12"], badges

    p.locator("#sGrid .t").first.click()
    m = wait_media(p, playing, "the video did not start playing")
    assert m[0]["src"].startswith("/video/")
    t0 = m[0]["t"]
    wait_media(p, lambda m: playing(m) and m[0]["t"] > t0 + 0.3, "the video's time did not advance")
    assert p.locator("#vLive").is_hidden(), "a video has no LIVE control"
    assert "Video" in p.inner_text("#vInfo")

    p.keyboard.press(" ")
    wait_media(p, lambda m: len(m) == 1 and m[0]["paused"], "Space did not pause it")
    p.keyboard.press(" ")
    wait_media(p, playing, "Space did not play it again")

    first = media(p)[0]["src"]
    p.keyboard.press("ArrowRight")
    m = wait_media(p, lambda m: playing(m) and m[0]["src"] != first, "the next video did not play in its place")
    assert len(m) == 1

    p.keyboard.press("Escape")
    wait_media(p, lambda m: m == [], "a video is still in the page after closing")
    assert not p.evaluate("[...document.querySelectorAll('video')].some(v => !v.paused)")


def test_a_search_opens_the_video_where_the_words_match(app, page):
    """"waterfall" finds "Coast to mountains" at its third scene (8-12 s): it opens there; the photo next to
    it stops it, and coming back plays it again."""
    p = page
    p.goto(app + "/?q=waterfall")
    p.wait_for_selector("#sGrid .t img.in")
    open_tile(p, ".dur")
    m = wait_media(p, playing, "the video did not start playing")
    assert "#t=" in m[0]["src"], m
    assert 7.5 <= m[0]["t"] <= 12.1, f"it did not start at the waterfall: {m}"

    n = p.eval_on_selector_all("#sGrid .t", "ts => ts.length")
    i = p.eval_on_selector_all("#sGrid .t", "ts => ts.findIndex(t => t.querySelector('.dur'))")
    p.keyboard.press("ArrowRight" if i + 1 < n else "ArrowLeft")
    wait_media(p, lambda m: m == [], "the photo beside it left a video in the page")
    p.keyboard.press("ArrowLeft" if i + 1 < n else "ArrowRight")
    wait_media(p, playing, "back on the video, it did not play")


def test_a_video_the_browser_cannot_play(app, page):
    """The <video>'s error event: its poster, a line saying so, and a link to download the file."""
    p = page
    p.goto(app + "/?kind=video")
    p.wait_for_selector("#sGrid .t img.in")
    p.locator("#sGrid .t").first.click()
    m = wait_media(p, playing, "the video did not start playing")
    sha = m[0]["src"].split("/video/")[1].split("#")[0]
    p.evaluate("() => document.querySelector('.v-pic video').dispatchEvent(new Event('error'))")
    p.wait_for_selector(".v-cant")
    assert "can’t play this video" in p.inner_text(".v-cant")
    link = p.locator(".v-cant a")
    assert link.get_attribute("href") == f"/video/{sha}?original=1"      # the stored file, not a copy made to play from
    assert link.get_attribute("download")
    assert media(p) == [], "the stream was not let go"
    assert p.locator(".v-pic img.hi.in").count() >= 1, "no poster"
    p.keyboard.press("ArrowRight")
    p.wait_for_selector(".v-cant", state="detached")


def test_a_live_photo_plays_while_held(app, page):
    """LIVE, held (mouse, then Enter), plays the clip muted over the still; let go, the still is back. Nothing
    of the clip is asked for before the first press."""
    p = page
    asked = []
    p.on("request", lambda r: asked.append(r.url) if "/video/" in r.url else None)
    p.goto(app + "/?search")                            # the folder the Live Photos are in, from Search's first page
    p.wait_for_selector('#browse .th[data-v="Videos 2026"]')
    p.click('#browse .th[data-v="Videos 2026"]')
    p.wait_for_selector("#sGrid .t .lv")
    assert p.locator("#sGrid .t .lv").count() == 2
    open_tile(p, ".lv")
    p.wait_for_selector("#vLive:not([hidden])")
    p.wait_for_timeout(300)
    assert media(p) == [] and asked == [], f"the clip was asked for before a press: {asked}"
    assert "Live Photo" in p.inner_text("#vInfo")

    p.hover("#vLive")
    m = wait_media(p, lambda m: playing(m, clip=True), "pointing at LIVE did not play the clip")
    assert m[0]["muted"]
    assert asked, "no request for the clip"
    p.mouse.move(5, 400)
    wait_media(p, lambda m: m == [], "the clip did not stop when let go")

    p.focus("#vLive")
    p.keyboard.down("Enter")
    wait_media(p, lambda m: playing(m, clip=True), "Enter on LIVE did not play the clip")
    p.keyboard.up("Enter")
    wait_media(p, lambda m: m == [], "the clip did not stop when Enter was let go")
    assert p.locator("#v.on").count() == 1, "Enter on LIVE must not close or leave the photo"


def test_a_live_tile_plays_while_held_and_does_not_open(app, page):
    """In the grid: a Live Photo's tile held with the mouse plays its clip over the tile, and letting go is
    not a click (the photo does not open); pointing at its LIVE mark plays it too. A quick click still opens."""
    p = page
    p.goto(app + "/?search")
    p.wait_for_selector('#browse .th[data-v="Videos 2026"]')
    p.click('#browse .th[data-v="Videos 2026"]')
    p.wait_for_selector("#sGrid .t .lv")
    box = p.locator("#sGrid .t:has(.lv)").first.bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

    p.mouse.move(x, y)
    p.mouse.down()
    m = wait_media(p, lambda m: playing(m, clip=True), "holding a Live tile did not play its clip")
    assert m[0]["muted"] and p.locator("#sGrid .t video.clip").count() == 1
    p.mouse.up()
    wait_media(p, lambda m: m == [], "the clip did not stop when the tile was let go")
    p.wait_for_timeout(200)
    assert p.locator("#v.on").count() == 0, "letting go of a held tile opened the photo"

    p.hover("#sGrid .t .lv")
    wait_media(p, lambda m: playing(m, clip=True), "pointing at a tile's LIVE mark did not play its clip")
    p.mouse.move(5, 5)
    wait_media(p, lambda m: m == [], "the clip did not stop when the mouse left the mark")

    p.mouse.click(x, y)
    p.wait_for_selector("#v.on")


def test_the_scope_control_is_there_before_a_search_and_narrows_one(app, page):
    """All, Photos, Videos: offered on Search's first page, before anything is typed; Videos shows the videos,
    a search typed then looks only in them, Photos has no video in it, and All is everything again."""
    p = page
    p.goto(app + "/?search")
    p.wait_for_selector("#chips .scope")
    assert p.eval_on_selector_all("#chips .scope button", "bs => bs.map(b => [b.textContent, b.getAttribute('aria-checked')])") == [
        ["All", "true"], ["Photos", "false"], ["Videos", "false"]]
    assert p.locator("#chips .chip").count() == 0, "the filter chips wait for something to filter"
    p.click('#chips [data-scope="video"]')
    p.wait_for_selector("#sGrid .t .dur")
    assert "kind=video" in p.url and p.inner_text("#sHead h1") == "Videos"
    assert p.locator("#sGrid .t").count() == 2 and p.locator("#chips .chip").count() > 0
    p.fill("#q", "snow")
    p.keyboard.press("Enter")
    p.wait_for_function("() => document.querySelector('#sHead h1').textContent.includes('snow')")
    assert "kind=video" in p.url and p.get_attribute('#chips [data-scope="video"]', "aria-checked") == "true"
    assert p.locator("#sGrid .t").count() == p.locator("#sGrid .t .dur").count() >= 1
    p.click('#chips [data-scope="photo"]')
    p.wait_for_function("() => location.search.includes('kind=photo')")
    p.wait_for_function("() => document.querySelectorAll('#sGrid .t img.in').length > 0 && !document.querySelector('#sGrid .t .dur')")
    p.click('#chips [data-scope="all"]')
    p.wait_for_function("() => !location.search.includes('kind=')")
