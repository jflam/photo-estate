"""Two things the model-checking pull requests of November 2026 changed, seen in a real browser (issue #148): the
Dismiss button on a question card in the inbox (it puts every question waiting in the item away: specs/Question.tla),
and a group of unnamed faces named on /people after People was loaded again (the server answers 409 and names nothing:
specs/People.tla; the page says so and keeps the group). Light and dark; PHOTO_ESTATE_SHOTS=<dir> keeps screenshots.

Slow tier (`uv run pytest -m slow tests/test_survey_browser.py`): the demo app in this process, on a free port, with
made-up faces on its photos (tests/test_people_states.py), and Playwright's Chromium.
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
import faces  # noqa: E402
import stories  # noqa: E402
from photo_app import PhotoApp  # noqa: E402
from test_pages_browser import browser, free_port  # noqa: E402,F401  (browser: the fixture)
from test_people_states import CLUSTERED, GROUP0, catalog, library, people  # noqa: E402

pytestmark = [pytest.mark.slow, pytest.mark.server]
SHOTS = os.environ.get("PHOTO_ESTATE_SHOTS")


@pytest.fixture
def app(monkeypatch):
    real = faces.People.cluster

    def cluster(pe):                       # (as test_people_states' fixture: grouped() waits on it)
        try:
            real(pe)
        finally:
            CLUSTERED.setdefault(id(pe), threading.Event()).set()
    monkeypatch.setattr(faces.People, "cluster", cluster)
    port = free_port()
    app = PhotoApp(embed.parser().parse_args(["serve", "--demo", "--no-pregenerate", "--port", str(port)]))
    srv = app.make_server("127.0.0.1", port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    app.base = f"http://127.0.0.1:{port}"
    yield app
    app.tasks.stop()
    srv.shutdown()


@pytest.fixture(params=["light", "dark"])
def be(request, browser):  # noqa: F811
    made = []

    def open_as(who: str):
        ctx = browser.new_context(viewport={"width": 1280, "height": 800}, locale="en-US", service_workers="block",
                                  color_scheme=request.param, extra_http_headers={"X-Demo-Member": who})
        p = ctx.new_page()
        p.errors, p.scheme = [], request.param
        p.on("pageerror", lambda e: p.errors.append(str(e)))
        made.append((ctx, p))
        return p
    yield open_as
    for ctx, p in made:
        ctx.close()
        assert not p.errors, f"uncaught errors in the page: {p.errors}"


def shot(p, name: str) -> None:
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        p.screenshot(path=str(Path(SHOTS) / f"{name}-{p.scheme}.png"))


def test_dismiss_on_a_question_card_puts_its_questions_away(app, be):
    con = sqlite3.connect(app.a.db)
    ben = con.execute("SELECT member_id FROM member_logins WHERE login='ben'").fetchone()[0]
    qs = [stories.ask(con, app.tasks.agent, None, [app.live.cat.shas[k]], text, None, ben, None, None, False, None,
                      refused=app.unseen)["question"] for k, text in ((3, "Where was this?"), (4, "Who took this one?"))]
    b = be("ben")
    b.goto(app.base + "/inbox")
    card = b.locator("#main .item").first
    card.wait_for()
    shot(b, "question-card")
    assert b.locator("#main .item").count() == 2           # (each question its own card: asked apart)
    card.get_by_role("button", name="Dismiss").click()
    b.locator("#main .item").nth(1).wait_for(state="detached")
    assert b.locator("#toast").inner_text() == "Put away" and b.locator("#main .item h3").all_inner_texts() == ["Where was this?"]
    shot(b, "question-one-dismissed")
    b.locator("#main .item").first.get_by_role("button", name="Dismiss").click()
    b.locator("#main .blank h3", has_text="Nothing to decide").wait_for()
    shot(b, "question-dismissed")
    assert {s for s, in con.execute(f"SELECT status FROM questions WHERE id IN ({','.join('?' * len(qs))})", qs)} == {"closed"}
    assert not con.execute("SELECT 1 FROM inbox_items WHERE member_id=? AND state='open'", (ben,)).fetchone()
    con.close()


def test_a_group_named_after_people_was_loaded_again_says_so_and_names_nothing(app, be):
    library(app)
    c = be("cleo")
    c.goto(app.base + "/people")
    group = c.locator("#unknown .group").first
    group.wait_for()
    shot(c, "people-groups")
    app.load_people()                      # an ingest's faces, a private change: the groups are worked out anew
    group.locator("input").fill("Quinn")
    group.get_by_role("button", name="Name", exact=True).click()
    c.locator("#toast", has_text="Could not save that").wait_for()
    assert "grouped again" in c.locator("#toast").inner_text()
    shot(c, "people-409")
    assert group.is_visible() and group.evaluate("g => !g.dataset.busy && g.style.opacity === ''")   # still there, to name again
    assert set(catalog(app, GROUP0).values()) == {(None, None)} and people(app) is not None
    c.reload()                             # looked at again: the groups of the People answering now, and the name lands
    group = c.locator("#unknown .group").first
    group.wait_for()
    group.locator("input").fill("Quinn")
    group.get_by_role("button", name="Name", exact=True).click()
    c.locator("#toast", has_text="Named Quinn").wait_for()
    assert any(src == "you" for _, src in catalog(app, GROUP0).values())
