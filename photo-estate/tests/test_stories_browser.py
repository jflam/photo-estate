"""Questions and stories on the pages (multiplayer phase 7), in a real browser, as the demo's members: Ada the
sysadmin, Ben and Cleo, each a browser of their own (the X-Demo-Member header). The app is the demo, in process and
served on a free port (its own scratch catalog, never demo/), with a stand-in for Claude Code as the estate's agent,
and no background threads: nothing is written out (the test writes a transcript itself, as transcribe.py would) and
the listeners run when the test says. In order:

- a question to Ben is in Ben's inbox only; he answers it typed, and the inbox is empty again;
- Cleo answers a family question spoken: the browser's own MediaRecorder records from a stand-in microphone (a
  WebAudio stream of a WAV this test writes: headless Chromium here gets no microphone, not even its fake one),
  she listens, redoes, sends; the page says it is being written down, and shows the transcript once it is;
- the story is in the viewer's details for Ada, and gone for Ben once Ada withdraws the photo; Cleo tells a story
  from the viewer, typed; Ben says "I don't know" to a question;
- Ben makes a series from a search; new photos arrive for another series of his, the inbox offers them, and he
  adds one of two;
- Ben asks the agent for the family on /changes (the task acts for no one), and /members shows the agent as what it is.

Slow tier (`uv run pytest -m slow`). The machines and the words are unit-tested without a browser
(app/tests/story-flow.test.js, ask-flow.test.js, albums-flow.test.js); the server's half is tests/test_stories.py,
tests/test_listeners.py and tests/test_agent.py.

  uv run pytest tests/test_stories_browser.py
"""
from __future__ import annotations

import argparse
import io
import sqlite3
import stat
import sys
import threading
import wave
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import embed  # noqa: E402
import estate  # noqa: E402
import stories  # noqa: E402
from photo_app import PhotoApp  # noqa: E402
from test_multiplayer import ADA, BEN, CLEO, call  # noqa: E402
from test_pages_browser import browser, free_port  # noqa: E402,F401  (browser: the fixture)

pytestmark = [pytest.mark.slow, pytest.mark.server]

# A stand-in for Claude Code: it changes nothing and ends.
AGENT = r'''#!/usr/bin/env python3
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "total_cost_usd": 0,
                  "result": "Looked, and changed nothing."}), flush=True)
'''

# The browser's microphone, stood in for: a WebAudio stream that plays a WAV. MediaRecorder records it as it would a
# microphone; only getUserMedia is replaced.
MIC = """
navigator.mediaDevices.getUserMedia = async () => {
  window.micAsked = (window.micAsked || 0) + 1;
  const ctx = new AudioContext();
  const buf = await ctx.decodeAudioData(await (await fetch('/__mic.wav')).arrayBuffer());
  const src = ctx.createBufferSource(); src.buffer = buf; src.loop = true;
  const dst = ctx.createMediaStreamDestination(); src.connect(dst); src.start();
  return dst.stream;
};"""


def wav(seconds=3.0, rate=48000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        t = np.arange(int(seconds * rate)) / rate
        w.writeframes((np.sin(2 * np.pi * 330 * t) * 6000).astype("<i2").tobytes())
    return buf.getvalue()


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("stories")
    agent = tmp / "claude"
    agent.write_text(AGENT.replace("#!/usr/bin/env python3", f"#!{sys.executable}"))
    agent.chmod(agent.stat().st_mode | stat.S_IXUSR)
    port = free_port()
    a = embed.parser().parse_args(["serve", "--demo", "--no-pregenerate", "--port", str(port), "--agent-cmd", str(agent)])
    app = PhotoApp(a)                 # start() is not called: no model, no threads (no transcription, no listeners)
    srv = app.make_server("127.0.0.1", port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    app.base = f"http://127.0.0.1:{port}"
    app.tmp = tmp
    yield app
    app.tasks.stop()
    srv.shutdown()


@pytest.fixture
def be(browser, app):  # noqa: F811  (the fixture imported above)
    """be("ben") -> a page in a browser that is Ben, with the stand-in microphone. Uncaught errors and console errors
    fail the test."""
    made = []
    sound = wav()

    def open_as(who: str, phone: bool = False):
        ctx = browser.new_context(viewport={"width": 390, "height": 844} if phone else {"width": 1280, "height": 800},
                                  locale="en-US", service_workers="block", extra_http_headers={"X-Demo-Member": who})
        ctx.add_init_script(MIC)
        ctx.route("**/__mic.wav", lambda r: r.fulfill(body=sound, content_type="audio/wav"))
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


def ask(app, shas, text, to=None, why=None, key=None) -> int:
    """A question filed as ask_member files it: by the estate's agent, for no one (the archivist)."""
    con = sqlite3.connect(app.a.db)
    try:
        return stories.ask(con, app.agent, None, shas, text, why, to, key, None, False, None, refused=app.unseen)["question"]
    finally:
        con.close()


def pick(app, k: int) -> str:
    return app.live.cat.shas[k]


def inbox(p, base) -> list[str]:
    p.goto(base + "/inbox")
    p.wait_for_function("() => document.querySelector('#main h1.display') || [...document.querySelectorAll('#main .blank h3')].some(h => h.textContent !== 'Looking')")
    return p.locator("#main .item h3").all_inner_texts()


def open_photo(p, app, sha: str) -> None:
    """Open the viewer on a photo: the day it was taken (its event, or its camera), and its tile there."""
    row = app.live.visible_for(None).row(sha)
    p.goto(f"{app.base}/?event={row['event']}" if row.get("event") is not None else f"{app.base}/?camera={row['camera']}")
    tile = p.locator(f'#sGrid .t[data-i]:has(img[src*="{sha}"])')
    tile.first.wait_for()
    tile.first.click()
    p.locator("#v.on").wait_for()


# ── answering ────────────────────────────────────────────────────────────────────────────────────

def test_a_question_to_ben_is_his_alone_and_he_answers_it_typed(app, be):
    x = pick(app, 30)
    ask(app, [x], "Where was this taken?", to="Ben", why="Nobody has said where it is.")
    assert inbox(be("ada"), app.base) == [] and inbox(be("cleo"), app.base) == []
    b = be("ben")
    assert inbox(b, app.base) == ["Where was this taken?"]
    card = b.locator("#main .item", has_text="Where was this taken?")
    assert card.locator(".pill").inner_text() == "A question for you"
    card.get_by_role("link", name="Answer").click()
    b.wait_for_url("**/inbox?ask=q*")
    q = b.locator(".q").first
    q.locator("h3", has_text="Where was this taken?").wait_for()
    assert b.locator("#askTitle").inner_text() == "A question for you"
    assert q.locator(".by").inner_text().startswith("The estate’s agent asks you, for the family")
    assert q.locator(".why").inner_text() == "Nobody has said where it is."
    q.locator(f'[data-photo="{x}"]').click()                       # a photo, bigger
    b.locator("#look[open] img").wait_for()
    b.locator("#look").click()
    assert b.evaluate("window.micAsked || 0") == 0, "the microphone is never asked for on load"
    q.locator("textarea").fill("At the lake, the summer we got the canoe.")
    q.get_by_role("button", name="Send answer").click()
    q.locator(".yours .story .said", has_text="At the lake, the summer we got the canoe.").wait_for()
    assert q.locator(".yours .story .told").inner_text().startswith("Ben · ")
    assert "That is all of them" in b.locator("#after").inner_text()
    assert inbox(b, app.base) == []
    got = call(app, "GET", f"/api/photos/{x}/stories", ADA)["stories"]
    assert [(s["told_by"]["name"], s["text"], s["question"]["text"]) for s in got] == [
        ("Ben", "At the lake, the summer we got the canoe.", "Where was this taken?")]


def test_cleo_answers_a_family_question_spoken_and_the_transcript_comes(app, be):
    y = pick(app, 31)
    qid = ask(app, [y], "Who took this one?")
    c = be("cleo")
    assert inbox(c, app.base) == ["Who took this one?"]
    c.goto(f"{app.base}/inbox?ask=q{qid}")
    q = c.locator(".q").first
    q.get_by_role("button", name="Record").click()
    q.locator(".state.rec", has_text="Recording").wait_for()
    assert c.evaluate("window.micAsked") == 1
    c.wait_for_timeout(1300)
    q.get_by_role("button", name="Stop").click()
    q.locator(".clip audio").wait_for()
    assert q.locator(".clip .clock").inner_text() in ("0:01", "0:02")
    q.get_by_role("button", name="Redo").click()                     # listened, and recorded again
    q.locator(".state.rec").wait_for()
    c.wait_for_timeout(2200)
    q.get_by_role("button", name="Stop").click()
    q.locator(".clip audio").wait_for()
    with c.expect_request(lambda r: f"/api/questions/{qid}/answer?seconds=" in r.url) as sent:   # (its length, as measured)
        q.get_by_role("button", name="Send answer").click()
    kind = sent.value.headers["content-type"]
    assert kind.split(";")[0] in ("audio/mp4", "audio/webm"), kind     # Chromium: AAC in MP4 when it can
    q.locator(".yours .story .said.pending", has_text="Being written down…").wait_for()
    assert "spoken" in q.locator(".yours .story .told").inner_text()
    story = call(app, "GET", f"/api/photos/{y}/stories", CLEO)["stories"][0]
    assert story["recording"]["type"] == "audio/mp4" if kind.startswith("audio/mp4") else "audio/webm"
    assert 1.5 < story["recording"]["seconds"] < 4, "the length the page measured, kept beside the recording"
    assert q.locator(".yours .story .clip .clock").inner_text() in ("0:02", "0:03")
    r = app.request("GET", story["recording"]["url"], headers=ADA, body=b"")
    assert r.status == 200 and len(r.body) > 1000, "the family may play it"
    con = sqlite3.connect(app.a.db)                                   # written out, as transcribe.py would on the mini
    stories.transcribed(con, story["id"], text="My brother took it, from the balloon.")
    con.close()
    q.locator(".yours .story .said:not(.pending)", has_text="My brother took it, from the balloon.").wait_for(timeout=20000)
    assert inbox(c, app.base) == []


def test_the_story_is_in_the_viewer_for_those_who_may_see_the_photo(app, be):
    y = pick(app, 31)
    a = be("ada")
    open_photo(a, app, y)
    a.locator("#vStories .story", has_text="My brother took it").wait_for()
    assert a.locator("#vStories h4").inner_text().lower() == "stories"
    assert a.locator("#vStories .story .told").inner_text().startswith("Cleo · ")
    assert a.locator("#vStories audio").count() == 1
    assert a.locator("#vStories .clip .clock").inner_text() in ("0:02", "0:03"), "its length, before it is played"
    # Ada withdraws it (she shared it): Ben may not be shown it, nor read its story
    con = sqlite3.connect(app.a.db)
    con.execute("INSERT OR IGNORE INTO sharers VALUES (?, ?, '2026-01-01')", (y, app.roster["ada"].id))
    con.commit()
    con.close()
    call(app, "POST", "/api/private", ADA, {"shas": [y], "private": True})
    app.relook()
    call(app, "GET", f"/api/photos/{y}/stories", BEN, expect=404)
    assert y not in {s for st in call(app, "GET", f"/api/photos/{pick(app, 30)}/stories", BEN)["stories"] for s in st["photos"]}
    call(app, "POST", "/api/private", ADA, {"shas": [y], "private": False})
    app.relook()


def test_cleo_tells_a_story_from_the_viewer_and_ben_does_not_know(app, be):
    z = pick(app, 32)
    c = be("cleo")
    open_photo(c, app, z)
    row = c.locator("#vStories [data-tell-open]")
    row.wait_for()
    assert c.locator("#vStories .story").count() == 0, "no story: one quiet row"
    assert row.inner_text().startswith("Tell a story about this photo")
    row.click()
    c.locator("#vStories textarea").fill("Grandma’s garden, the year of the big frost.")
    c.keyboard.press("ArrowRight")                                    # typing: the viewer's keys are the field's
    c.locator("#vStories").get_by_role("button", name="Tell it").click()
    c.locator("#vStories .story .said", has_text="Grandma’s garden").wait_for()
    assert c.locator("#vStories [data-tell-open]").inner_text().startswith("Tell another story")
    got = call(app, "GET", f"/api/photos/{z}/stories", BEN)["stories"]
    assert [(s["told_by"]["name"], s["question"]) for s in got] == [("Cleo", None)]
    # Ben: "I don't know"
    qid = ask(app, [z], "Whose garden is this?", to="Ben")
    b = be("ben")
    b.goto(f"{app.base}/inbox?ask=q{qid}")
    b.locator(".q h3", has_text="Whose garden is this?").wait_for()
    b.get_by_role("button", name="I don’t know").click()
    b.locator("#after", has_text="Nothing left to answer here").wait_for()
    assert b.locator(".q").count() == 0
    assert "Whose garden is this?" not in inbox(b, app.base), "put away for good (the family's question waits still)"


# ── series ───────────────────────────────────────────────────────────────────────────────────────

def jpeg(seed: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), ((seed * 70) % 256, (seed * 30) % 256, (seed * 110) % 256)).save(buf, "JPEG", quality=90)
    return buf.getvalue()


def test_ben_makes_a_series_and_adds_new_photos_from_his_inbox(app, be):
    b = be("ben")
    camera = app.live.cat.meta[pick(app, 0)].get("camera") or "Canon EOS 5D Mark III"
    b.goto(f"{app.base}/?camera={camera}")
    b.locator("#sGrid .t[data-i]").first.wait_for()
    b.locator("#sAlbum").click()
    b.locator(".menu [data-series]").check()
    b.locator(".menu .mk input[type=text]").fill("Canon pictures")
    b.locator(".menu .mk button.pri").click()
    b.wait_for_url("**/?album=*")
    b.locator("#albHead p", has_text="A series: new photos its search finds are offered to you").wait_for()
    made = [s for s in call(app, "GET", "/api/albums", BEN)["sets"] if s["title"] == "Canon pictures"][0]
    assert made["grows_from"] == {"camera": camera}
    # another series of his, and new photos that match it
    seed = pick(app, 12)
    lake = call(app, "POST", "/api/albums", BEN, {"title": "The lake", "grows_from": {"like": seed, "min_score": 0.99}}, expect=201)
    drop, store = app.tmp / "drop", app.tmp / "store"
    drop.mkdir()
    for k in range(3):
        (drop / f"IMG_{k}.JPG").write_bytes(jpeg(40 + k))
    estate.cmd_ingest(argparse.Namespace(db=app.a.db, store=str(store), sources=[str(drop)], force=True, facts=None,
                                         delete_after=False, progress=False, uploads=False))
    con = sqlite3.connect(app.a.db)
    new = [s for s, in con.execute("SELECT sha256 FROM assets ORDER BY rowid DESC LIMIT 3")][::-1]
    vec = app.live.cat.M[app.live.cat.index[seed]].astype(np.float32)
    other = app.live.cat.M[app.live.cat.index[pick(app, 90)]].astype(np.float32)
    for s, v in zip(new, (vec, vec, other)):
        v = (v / np.linalg.norm(v)).astype(np.float16)
        con.execute("INSERT OR REPLACE INTO embeddings VALUES (?,?,?,?,NULL,'2026-11-22')", (s, embed.DEFAULT_MODEL, v.shape[0], v.tobytes()))
    con.commit()
    con.close()
    app.reload_catalog()
    app.listen_once()
    assert "2 new photos may belong in your series “The lake”" in inbox(b, app.base)
    card = b.locator("#main .item", has_text="The lake")
    assert card.locator(".pill").inner_text() == "New for a series"
    card.get_by_role("link", name="Look at them").click()
    b.wait_for_url(f"**/?album={lake['id']}*")
    panel = b.locator("#albCands .cands")
    panel.wait_for()
    assert panel.locator("h2").inner_text() == "2 new photos for this series"
    assert panel.get_by_role("button", name="Add all 2").is_visible()
    panel.locator(f'[data-cand="{new[1]}"]').click()                  # this one does not belong
    with b.expect_response(lambda r: r.url.endswith(f"/api/albums/{lake['id']}/candidates") and r.request.method == "POST"):
        panel.get_by_role("button", name="Add 1 photo").click()
    b.locator("#toast", has_text="Added 1 photo to the series").wait_for()
    assert b.locator("#albCands .cands").count() == 0
    held = {r["sha"] for r in call(app, "GET", f"/api/albums/{lake['id']}/photos?limit=200", BEN)["results"]}
    assert new[0] in held and new[1] not in held
    assert "2 new photos may belong in your series “The lake”" not in inbox(b, app.base)


# ── asking the archivist; the agent on /members ────────────────────────────────────────────────────

def test_ben_asks_for_the_family_and_members_shows_the_agent(app, be):
    b = be("ben")
    b.goto(app.base + "/changes")
    b.locator("#ask").wait_for()
    assert b.locator(".for label", has_text="For me").locator("input").is_checked()
    assert "only what everyone can see" in b.locator(".for label", has_text="For the family").inner_text()
    b.locator(".for label", has_text="For the family").click()
    b.locator("#ask").fill("Make a series of every sunset over water")
    with b.expect_response(lambda r: r.url.endswith("/api/tasks") and r.request.method == "POST") as got:
        b.get_by_role("button", name="Ask").click()
    task = got.value.json()
    assert task["acting_for"] is None and task["started_by"] == f"member:{app.roster['ben'].id}"
    b.locator(".task .pill.family", has_text="For the family").wait_for()
    a = be("ada")
    a.goto(app.base + "/members")
    agent = a.locator(".who.agent")
    agent.wait_for()
    assert agent.locator("h3").inner_text() == "The estate's agent"
    assert agent.locator("select, input, button").count() == 0, "nothing to change"
    assert a.locator("#sum").inner_text() == "3 members"
