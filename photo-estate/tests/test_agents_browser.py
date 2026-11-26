"""Connect your agent (multiplayer phase 8: /agents, app/agents-flow.js), in a real browser, as the demo's Ben and
Cleo, light and dark, desktop and phone widths. On one demo app:

- Ben's agent connects from his laptop (X-Demo-Device, the demo's stand-in for what `tailscale whois` names); his
  Library menu has Connect your agent, his inbox the request, which opens /agents; he allows it there (this
  browser is another device) and the agent writes; he turns it off and it is refused;
- Cleo's agent connects from this computer, the same device as her browser: the page says so and does not offer to
  allow it from here; a second agent of hers connects from her laptop while the page is open, and shows up by the
  page's own gentle refresh, without a reload; she allows it.

Slow tier: the app is `uv run embed.py serve --demo` on a free port (its own scratch catalog). Screenshots go to
$PHOTO_ESTATE_SHOTS (else a temp folder). The machine and the words are unit-tested in app/tests/agents-flow.test.js;
the server's half is tests/test_personal_agents.py.

  uv run pytest tests/test_agents_browser.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_pages_browser import browser, free_port, serve, stop  # noqa: E402,F401  (browser: the fixture)

pytestmark = [pytest.mark.slow, pytest.mark.server]
SIZES = {"desktop": {"width": 1280, "height": 860}, "phone": {"width": 390, "height": 844}}


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    port = free_port()
    proc, base = serve(["embed.py", "serve", "--demo", "--port", str(port), "--no-pregenerate"], port, "/api/info",
                       tmp_path_factory.mktemp("agents") / "server.log")
    yield base
    stop(proc)


@pytest.fixture(scope="module")
def shots(tmp_path_factory) -> Path:
    d = Path(os.environ.get("PHOTO_ESTATE_SHOTS") or tmp_path_factory.mktemp("shots"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def mcp(base: str, who: str, name: str | None = None, args=None, device: str | None = None):
    """A call to /mcp as `who`'s own agent, from `device` (None: this computer): (status, answer)."""
    body = {"jsonrpc": "2.0", "id": 1, **({"method": "tools/call", "params": {"name": name, "arguments": args or {}}}
                                          if name else {"method": "tools/list"})}
    h = {"Content-Type": "application/json", "X-Demo-Member": who, **({"X-Demo-Device": device} if device else {})}
    try:
        with urllib.request.urlopen(urllib.request.Request(base + "/mcp", json.dumps(body).encode(), h), timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@pytest.fixture
def be(browser):  # noqa: F811  (the fixture imported above)
    """be("ben", size, scheme) -> a page in a browser that is Ben. Uncaught errors and console errors fail the test."""
    made = []

    def open_as(who: str, size: str = "desktop", scheme: str = "light"):
        ctx = browser.new_context(viewport=SIZES[size], locale="en-US", service_workers="block", color_scheme=scheme,
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


def agents(p, base):
    p.goto(base + "/agents")
    p.locator("#main h1.display").wait_for()


def shot(p, path: Path) -> None:
    p.evaluate("scrollTo(0, 0)")                     # (a whole page, from its top: the bar stays where it belongs)
    p.wait_for_timeout(150)
    p.screenshot(path=str(path), full_page=True)


def card(p, device: str):
    return p.locator("#main .agent", has=p.locator("h3", has_text=device))


def test_ben_allows_his_agent_from_the_inbox_and_turns_it_off(app, be, shots):
    status, got = mcp(app, "ben", device="ben-laptop")
    assert status == 200 and "journal_since" in [t["name"] for t in got["result"]["tools"]]
    status, got = mcp(app, "ben", "create_album", {"title": "Ben's agent's album"}, device="ben-laptop")
    assert got["result"]["isError"] and got["result"]["content"][0]["text"].startswith("Not yet: Ben has not allowed")
    p = be("ben")
    p.goto(app + "/")
    p.locator("#libMore").click()
    p.locator(".mi.ln", has_text="Connect your agent").wait_for()
    p.goto(app + "/inbox")
    item = p.locator("#main .item", has_text="An agent connected as you from ben-laptop. Allow it?")
    item.wait_for()
    assert item.locator(".pill").inner_text() == "Your agent"
    p.screenshot(path=str(shots / "inbox-binding-ben-desktop-light.png"))
    item.get_by_role("link", name="Allow or turn off").click()
    p.wait_for_url("**/agents")
    c = card(p, "ben-laptop")
    c.wait_for()
    assert c.locator(".pill").inner_text() == "Waiting for you" and "Connected as you at " in c.inner_text()
    cmd = p.locator("#cmd").inner_text()
    assert cmd == f"claude mcp add --transport http --header 'X-Demo-Member: ben' --header 'X-Demo-Device: ben-laptop' photo-estate {app}/mcp"
    assert "Anthropic’s servers, which are not on the tailnet" in p.locator(".where").inner_text()
    shot(p, shots / "agents-ben-pending-desktop-light.png")
    c.get_by_role("button", name="Allow it").click()
    c.locator(".pill", has_text="Allowed").wait_for()
    status, got = mcp(app, "ben", "create_album", {"title": "Ben's agent's album"}, device="ben-laptop")
    assert status == 200 and not got["result"]["isError"]
    p.goto(app + "/inbox")
    p.locator("#main .blank h3, #main .item").first.wait_for()
    assert p.locator("#main .item", has_text="An agent connected as you").count() == 0
    agents(p, app)
    shot(p, shots / "agents-ben-allowed-desktop-light.png")
    c = card(p, "ben-laptop")
    c.get_by_role("button", name="Turn it off").click()
    c.locator(".pill", has_text="Turned off").wait_for()
    status, got = mcp(app, "ben", device="ben-laptop")
    assert status == 403 and "turned off their agent on ben-laptop" in got["error"]["message"]
    # and as it looks: dark, and on a phone
    for size, scheme in (("desktop", "dark"), ("phone", "light"), ("phone", "dark")):
        q = be("ben", size, scheme)
        agents(q, app)
        card(q, "ben-laptop").wait_for()
        shot(q, shots / f"agents-ben-{size}-{scheme}.png")
    q = be("ben", "phone", "dark")
    mcp(app, "ben", device="ben-desktop")              # one more waiting, for the inbox on a phone
    q.goto(app + "/inbox")
    q.locator("#main .item", has_text="from ben-desktop").wait_for()
    q.screenshot(path=str(shots / "inbox-binding-ben-phone-dark.png"))


def test_cleo_cannot_allow_from_the_agents_own_device_and_sees_a_new_one_arrive(app, be, shots):
    mcp(app, "cleo")                                    # her agent, on this computer: the same device as her browser
    p = be("cleo", "phone", "dark")
    agents(p, app)
    c = card(p, "this computer")
    c.wait_for()
    assert "this device" in c.locator("h3").inner_text()
    assert c.get_by_role("button", name="Allow it").is_disabled()
    assert "Allow it from another device" in c.inner_text()
    shot(p, shots / "agents-cleo-same-device-phone-dark.png")
    # a second agent connects while the page is open: the page's own refresh shows it, with no reload
    p.evaluate("window.__stayed = true")
    mcp(app, "cleo", device="cleo-laptop")
    fresh = card(p, "cleo-laptop")
    fresh.wait_for(timeout=15000)
    assert p.evaluate("window.__stayed") is True, "no reload"
    assert p.locator("#sum").inner_text() == "2 waiting for you"
    fresh.get_by_role("button", name="Allow it").click()
    fresh.locator(".pill", has_text="Allowed").wait_for()
    status, got = mcp(app, "cleo", "create_album", {"title": "Cleo's agent's album"}, device="cleo-laptop")
    assert status == 200 and not got["result"]["isError"]
    status, got = mcp(app, "cleo", "create_album", {"title": "x"})
    assert got["result"]["isError"], "the one on this computer is still waiting"
    shot(p, shots / "agents-cleo-allowed-phone-dark.png")
    for size, scheme in (("desktop", "light"), ("desktop", "dark"), ("phone", "light")):
        q = be("cleo", size, scheme)
        agents(q, app)
        card(q, "cleo-laptop").wait_for()
        shot(q, shots / f"agents-cleo-{size}-{scheme}.png")
