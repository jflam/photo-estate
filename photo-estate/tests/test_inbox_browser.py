"""The inbox on the pages (multiplayer phase 4), in a real browser, as the demo's members: Ada the sysadmin, Ben
and Cleo, each a browser of their own (the X-Demo-Member header), and a stranger. In order, on one demo app:

- a stranger asks to be let in, and the request-access page says Ada has been told; Ada's Library menu shows
  a count, her inbox the request, which opens /members; letting the stranger in there closes it;
- Ben publishes his filter and Cleo subscribes; Ben hides more, twice, and Cleo's inbox says once that his
  lens changed; opening it takes her to Albums and puts it away; the next change she dismisses;
- Ben proposes his filter as an estate default: Ada's inbox shows it, opens Albums, and deciding there
  closes it;
- a task Ben submits ends with a change set (a stand-in for Claude Code, as tests/test_tasks.py has): it is
  in Ben's inbox only, opens its review page, and applying it there closes it;
- the Albums tab's badge on a hiding set's photos says from whom they are hidden.

Slow tier (`uv run pytest -m slow`): the app is `uv run embed.py serve --demo` on a free port (its own scratch
catalog, never demo/). The browser and the page checks are tests/test_pages_browser.py's. The machine and the
words the page uses are unit-tested without a browser (app/tests/inbox-flow.test.js); the server's half is
tests/test_member_inbox.py.

  uv run pytest tests/test_inbox_browser.py
"""
from __future__ import annotations

import json
import stat
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_pages_browser import browser, free_port, serve, stop  # noqa: E402,F401  (browser: the fixture)

pytestmark = [pytest.mark.slow, pytest.mark.server]

# A stand-in for Claude Code: it makes one change set of three photos through the library's tools, as the
# member the task acts for, and ends.
AGENT = r'''#!/usr/bin/env python3
import json, sys, urllib.request
argv = sys.argv[1:]
conf = json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]["photo-estate"]
def tool(name, args):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}
    req = urllib.request.Request(conf["url"], json.dumps(body).encode(), {"Content-Type": "application/json", **conf["headers"]})
    return json.load(urllib.request.urlopen(req, timeout=60))["result"]["structuredContent"]
ids = [p["id"] for p in tool("search_photos", {"limit": 3})["photos"]]
made = tool("create_change_set", {"title": "Blurry ones", "prompt": "hide the blurry ones"})
tool("amend_change_set", {"change_set": made["change_set"], "add_ids": ids, "reason": "blurry"})
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 3, "total_cost_usd": 0,
                  "result": "Proposed 3 photos."}), flush=True)
'''


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("inbox")
    agent = tmp / "claude"
    agent.write_text(AGENT)
    agent.chmod(agent.stat().st_mode | stat.S_IXUSR)
    port = free_port()
    proc, base = serve(["embed.py", "serve", "--demo", "--port", str(port), "--no-pregenerate", "--agent-cmd", str(agent)],
                       port, "/api/info", tmp / "server.log")
    yield base
    stop(proc)


def call(base: str, who: str, path: str, body=None):
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "X-Demo-Member": who})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def until(check, what: str, within: float = 30.0) -> None:
    end = time.monotonic() + within
    while not check():
        assert time.monotonic() < end, what
        time.sleep(0.1)


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
        own = [t for t in p.said if "Failed to load resource" not in t]
        assert not own, f"console errors: {own}"


def inbox(p, base) -> list[str]:
    """Open /inbox and wait for it to be drawn: the titles of the items, newest first."""
    p.goto(base + "/inbox")
    p.locator("#main h1.display, #main .blank h3:not(:text('Looking'))").first.wait_for()
    p.wait_for_function("() => !document.querySelector('#main .blank h3') || document.querySelector('#main .blank h3').textContent !== 'Looking'")
    return p.locator("#main .item h3").all_inner_texts()


def item(p, text: str):
    return p.locator("#main .item", has_text=text)


def albums(p, base):
    p.goto(base + "/?albums")
    p.locator("#albList .bt h2", has_text="Filters").wait_for()


def offer(p, lid: int, label: str) -> None:
    with p.expect_response(lambda r: "/api/lenses" in r.url and r.request.method == "GET"):
        p.locator(f'#albList .lens[data-lens="{lid}"]').get_by_role("button", name=label, exact=True).click()
    p.wait_for_function("() => !document.querySelector('#albList .lens.busy')")


def hide(base: str, who: str, shas, lens=None) -> int:
    sid = call(base, who, "/api/changes", {"title": "hide these", "items": [{"sha": s} for s in shas]})["id"]
    call(base, who, f"/api/changes/{sid}/apply", {} if lens is None else {"lens": lens})
    until(lambda: not set(shas) & {r["sha"] for r in call(base, who, "/api/search?limit=500&dedupe=0")["results"]},
          "the hide was not taken in")
    return sid


SHAS: list[str] = []


def pick(base: str) -> list[str]:
    if not SHAS:
        SHAS.extend(r["sha"] for r in call(base, "ada", "/api/search?limit=500&dedupe=0")["results"][10:16])
    return SHAS


# ── someone asks to be let in ────────────────────────────────────────────────────────────────────

def test_a_stranger_asks_and_adas_inbox_has_it_until_she_lets_them_in(app, be):
    s = be("dana@example.com")
    s.goto(app + "/request-access")
    s.locator("input[name=name]").fill("Dana")
    s.get_by_role("button", name="Ask to be let in").click()
    s.locator(".blank h3", has_text="Ada will let you in").wait_for()
    assert "Ada has been told: it is in their inbox." in s.locator(".blank p").first.inner_text()
    a = be("ada")
    a.goto(app + "/")
    a.locator("#libCnt").wait_for()
    assert a.locator("#libCnt").inner_text() == "1"
    a.locator("#libMore").click()
    row = a.locator('.menu a[href="/inbox"]')
    assert row.inner_text().replace("\n", " ") == "Inbox 1"
    row.click()
    a.wait_for_url("**/inbox")
    a.locator("#main .item").first.wait_for()
    assert a.locator("#main .item h3").all_inner_texts() == ["Dana asks to be let in (dana@example.com)"]
    assert item(a, "Dana").locator(".pill").inner_text() == "Asking to be let in"
    assert a.locator("#sum").inner_text() == "1 thing to decide"
    assert inbox(be("ben"), app) == [] and inbox(be("cleo"), app) == []
    item(a, "Dana").get_by_role("link", name="Open Members").click()
    a.wait_for_url("**/members")
    card = a.locator(".who", has_text="dana@example.com")
    card.get_by_role("button", name="Let in").click()
    card.locator("button", has_text="Turn off").wait_for()
    assert inbox(a, app) == []
    assert a.locator("#main .blank h3").inner_text() == "Nothing to decide"
    a.goto(app + "/")
    a.locator("#libGrid .t").first.wait_for()
    assert a.locator("#libCnt").is_hidden()


# ── a lens Cleo follows changes ──────────────────────────────────────────────────────────────────

def test_cleo_is_told_once_that_bens_lens_changed(app, be):
    x, y, z, w = pick(app)[:4]
    hide(app, "ben", [x])
    mine = call(app, "ben", "/api/lenses")["mine"]
    call(app, "ben", f"/api/lenses/{mine}/publish", {"on": True})
    c = be("cleo")
    albums(c, app)
    offer(c, mine, "Subscribe")
    assert inbox(c, app) == []
    hide(app, "ben", [y])
    hide(app, "ben", [z])
    assert inbox(c, app) == ["Ben’s lens “Hidden photos” changed: it hides 3 photos now"], "once, however often"
    assert item(c, "Ben’s lens").locator(".pill").inner_text() == "A lens you follow"
    assert inbox(be("ben"), app) == [] and inbox(be("ada"), app) == []
    item(c, "Ben’s lens").get_by_role("link", name="See it in Albums").click()     # a notice: seeing it puts it away
    c.locator("#albList .bt h2", has_text="Filters").wait_for()
    assert inbox(c, app) == []
    hide(app, "ben", [w])
    assert inbox(c, app) == ["Ben’s lens “Hidden photos” changed: it hides 4 photos now"]
    item(c, "Ben’s lens").get_by_role("button", name="Dismiss").click()
    c.locator("#main .blank h3", has_text="Nothing to decide").wait_for()
    assert c.locator("#toast").inner_text() == "Put away"


# ── Ben proposes his filter ──────────────────────────────────────────────────────────────────────

def test_ben_proposes_his_filter_and_adas_inbox_has_it_until_she_decides(app, be):
    mine = call(app, "ben", "/api/lenses")["mine"]
    b = be("ben")
    albums(b, app)
    offer(b, mine, "Propose as an estate default")
    assert inbox(b, app) == []
    a = be("ada")
    assert inbox(a, app) == ["Ben proposes the filter “Hidden photos” as an estate default"]
    assert item(a, "Ben proposes").locator(".pill").inner_text() == "Proposed estate default"
    item(a, "Ben proposes").get_by_role("link", name="Decide in Albums").click()
    a.locator("#albList .lh", has_text="Waiting for you to decide").wait_for()
    offer(a, mine, "Make it an estate default")
    assert inbox(a, app) == []


# ── a change set a task made for Ben ─────────────────────────────────────────────────────────────

def test_a_change_set_a_task_made_for_ben_is_in_bens_inbox_only(app, be):
    tid = call(app, "ben", "/api/tasks", {"prompt": "hide the blurry ones"})["id"]
    until(lambda: call(app, "ben", f"/api/tasks/{tid}")["status"] == "done", "the task did not finish")
    sid = call(app, "ben", f"/api/tasks/{tid}")["change_sets"][0]["id"]
    b = be("ben")
    assert inbox(b, app) == ["“Blurry ones”: 3 photos the agent proposes to hide, for you to look over"]
    assert item(b, "Blurry ones").locator(".pill").inner_text() == "To look over"
    assert inbox(be("ada"), app) == [] and inbox(be("cleo"), app) == []
    item(b, "Blurry ones").get_by_role("link", name="Look it over").click()
    b.wait_for_url(f"**/changes/{sid}")
    b.locator("#dock #apply").wait_for()
    b.locator("#apply").click()
    b.wait_for_function("() => /Checked\\./.test(document.querySelector('#why').textContent)", timeout=30000)
    assert inbox(b, app) == []


# ── hidden from whom, on a hiding set's photos ──────────────────────────────────────────────────

@pytest.mark.parametrize("who, lens, words", [("ben", None, "Hidden from you"), ("ada", "estate", "Hidden from everyone")])
def test_the_albums_badge_says_from_whom_a_hiding_sets_photos_are_hidden(app, be, who, lens, words):
    sid = hide(app, who, pick(app)[4:5] if who == "ben" else pick(app)[5:6], lens)
    p = be(who)
    p.goto(f"{app}/?album={sid}")
    p.locator("#albGrid .t .hid", has_text=words).first.wait_for()     # (once the lenses are known: whose lens it is)
    assert p.locator("#albGrid .t .hid").all_text_contents() == [words]
