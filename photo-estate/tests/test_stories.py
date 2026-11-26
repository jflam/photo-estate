"""Questions and stories (multiplayer phase 7: stories.py, transcribe.py), on the demo library in process, as its
three members (Ada the sysadmin, Ben and Cleo) and the estate's agent: that nothing private leaks through a
question (asked, dealt, listed, answered), a story or a recording; the weekly allowance, the ranking and the
grouping on a fake clock; answers typed and spoken (upload, the queued job with a stand-in transcriber, the
transcript written once); the append-only triggers of stories; and the purge of a photo that has stories. No
model, no MLX: the transcriber is a stand-in, and the audio is a WAV this test writes.

  uv run pytest tests/test_stories.py
"""
from __future__ import annotations

import io
import json
import sqlite3
import sys
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import embed  # noqa: E402
import stories  # noqa: E402
import transcribe  # noqa: E402
from core import schema  # noqa: E402
from photo_app import PhotoApp  # noqa: E402
from test_append_only import PHOTOS, estate_with_three, run, sha, unlock  # noqa: E402,F401,F811  (fixtures)
from test_multiplayer import ADA, BEN, CLEO, call, mid  # noqa: E402

T0 = datetime(2026, 12, 1, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def app():
    a = embed.parser().parse_args(["serve", "--demo", "--no-pregenerate"])
    app = PhotoApp(a)             # start() is not called: no model, no threads; its own scratch catalog
    app.clock = lambda: T0
    yield app
    app.tasks.stop()


def db(app) -> sqlite3.Connection:
    return sqlite3.connect(app.a.db)


def pick(app, k: int) -> str:
    return app.live.cat.shas[k]


def ask(app, photos, text="Who is this?", to=None, key=None, priority=None, asker=None, acting_for=None, **kw):
    """A question filed as the MCP tool files it (stories.ask with the app's rule), by the estate's agent unless said."""
    con = db(app)
    try:
        return stories.ask(con, app.agent if asker is None else asker, acting_for, photos, text, kw.get("why"), to, key,
                           priority, kw.get("follow_up", False), None, refused=app.unseen)["question"]
    finally:
        con.close()


def withdraw(app, who, shas):
    con = db(app)
    con.executemany("INSERT OR IGNORE INTO sharers VALUES (?,?,'2026-01-01')", ((s, mid(app, who)) for s in shas))
    con.commit()
    con.close()
    call(app, "POST", "/api/private", who, {"shas": list(shas), "private": True})
    app.relook()


def inbox(app, who) -> list[tuple[str, str]]:
    return [(i["kind"], i["title"]) for i in call(app, "GET", "/api/inbox", who)["items"]]


def wav(seconds=0.5, rate=16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        t = np.arange(int(seconds * rate)) / rate
        w.writeframes((np.sin(2 * np.pi * 440 * t) * 8000).astype("<i2").tobytes())
    return buf.getvalue()


# -- nothing private leaks ------------------------------------------------------------------------------

def test_a_question_names_only_photos_its_recipient_may_see(app):
    x, y = pick(app, 20), pick(app, 21)
    withdraw(app, BEN, [x])                       # x: Ben's alone now
    ben, cleo = mid(app, BEN), mid(app, CLEO)
    with pytest.raises(stories.StoryError, match="Cleo may not be shown"):
        ask(app, [x, y], to="Cleo", acting_for=ben)                       # asking Cleo about Ben's private photo
    with pytest.raises(stories.StoryError, match="not one the whole family can see"):
        ask(app, [y, x], acting_for=ben)                                  # a family question naming it
    with pytest.raises(stories.StoryError, match="no such photo"):
        ask(app, [x], to="Ben", acting_for=cleo)                          # an agent acting for Cleo does not see it
    with pytest.raises(stories.StoryError, match="no such photo"):
        ask(app, [x], to="Ben")                                           # nor does the archivist (for no one)
    assert ask(app, [x], to="Ben", acting_for=ben)                       # Ben may be asked about his own
    with pytest.raises(stories.StoryError, match="nobody in the family"):
        ask(app, [y], to="The estate's agent")
    con = db(app)
    assert con.execute("SELECT count(*) FROM questions").fetchone()[0] == 1, "refused, never trimmed: nothing filed"
    con.close()
    # dealt, listed, answered: a question to Cleo about y, which Ben then takes back from the family
    q = ask(app, [y], "Where is this?", to="Cleo")
    withdraw(app, BEN, [y])
    assert inbox(app, CLEO) == [] and call(app, "GET", "/api/questions", CLEO)["items"] == []
    call(app, "POST", f"/api/questions/{q}/answer", CLEO, {"text": "Elm Street"}, expect=404)
    call(app, "POST", "/api/private", BEN, {"shas": [y], "private": False})
    app.relook()
    assert [t for k, t in inbox(app, CLEO)] == ["Where is this?"], "dealt once Cleo may see it again"
    call(app, "POST", "/api/private", BEN, {"shas": [y], "private": True})
    app.relook()
    assert inbox(app, CLEO) == [], "and left out of her inbox, item and all, once she may not"


def test_a_story_and_its_recording_are_for_whoever_may_see_every_photo_of_it(app):
    x, y = pick(app, 22), pick(app, 23)
    withdraw(app, BEN, [x])
    told = call(app, "POST", "/api/stories", BEN, {"photos": [x, y], "text": "The receipt for Mum's present, by the tree"}, expect=201)
    assert told["told_by"]["name"] == "Ben" and told["photos"] == [x, y] and told["question"] is None
    spoken = app.request("POST", f"/api/stories?photos={x}", headers={**BEN, "Content-Type": "audio/wav"}, body=wav())
    assert spoken.status == 201, spoken.body
    sid = spoken.json()["id"]
    for who in (CLEO, ADA):
        call(app, "GET", f"/api/photos/{x}/stories", who, expect=404)
        assert call(app, "GET", f"/api/photos/{y}/stories", who)["stories"] == [], "a story naming x is Ben's alone"
        call(app, "GET", f"/api/stories/{sid}/recording", who, expect=404)
    assert [s["text"] for s in call(app, "GET", f"/api/photos/{y}/stories", BEN)["stories"]] == [told["text"]]
    r = app.request("GET", f"/api/stories/{sid}/recording", headers={**BEN, "Range": "bytes=0-9"})
    assert (r.status, len(r.body), r.headers["Content-Range"].endswith(f"/{len(wav())}")) == (206, 10, True)
    assert app.request("GET", f"/api/stories/{sid}/recording", headers=BEN).body == wav()
    # telling a story about a photo one may not see is as telling one about a photo there is not
    call(app, "POST", "/api/stories", CLEO, {"photos": [x], "text": "?"}, expect=404)
    # and the MCP tool says nothing of it to anyone else
    assert "stories" not in call(app, "POST", "/mcp", CLEO, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "get_photos", "arguments": {"ids": [y]}}})["result"]["structuredContent"]["photos"][0]


# -- the inbox decides how many --------------------------------------------------------------------------

def test_the_weekly_allowance_ranking_and_grouping(app):
    p = [pick(app, k) for k in range(30, 40)]
    call(app, "POST", "/api/stories", ADA, {"photos": [p[8]], "text": "Told already"}, expect=201)     # p8 is described
    call(app, "POST", "/api/albums", CLEO, {"title": "Lake days", "items": [{"sha": p[6]}]}, expect=201)
    con = db(app)
    con.execute("UPDATE change_sets SET grows_from=? WHERE title='Lake days'", (json.dumps({"like": p[6]}),))   # a series
    con.commit()
    con.close()
    q1 = ask(app, [p[8]], "Who took this?", to="Cleo", priority=1.0)         # described: last, whatever its priority
    tg = [ask(app, [p[k]], f"Who is at the table ({k})?", to="Cleo", key="Thanksgiving 1998") for k in (1, 2, 3)]
    q5 = ask(app, [p[4]], "Which year?", to="Cleo", priority=0.5)
    q6 = ask(app, [p[5]], "Whose dog?", to="Cleo", priority=0.9)
    ask(app, [p[6]], "Which lake?", to="Cleo")                              # in a series: ahead of the others
    assert stories.ALLOWANCE == 3
    assert inbox(app, CLEO) == [("question", t) for t in ("Which year?", "Whose dog?", "Which lake?")], \
        "three a week: a series' gap, then by priority; newest item first"
    assert inbox(app, CLEO) == [("question", t) for t in ("Which year?", "Whose dog?", "Which lake?")], "reading again deals nothing"
    assert inbox(app, BEN) == [] and inbox(app, ADA) == []
    # "I don't know": put away for good; it counted against this week only
    call(app, "POST", f"/api/questions/{q5}/dismiss", CLEO)
    assert [t for _, t in inbox(app, CLEO)] == ["Whose dog?", "Which lake?"]
    app.clock = lambda: T0 + timedelta(days=6)
    assert len(inbox(app, CLEO)) == 2, "nothing new within the week"
    app.clock = lambda: T0 + timedelta(days=8)
    assert [t for _, t in inbox(app, CLEO)] == ["3 questions about Thanksgiving 1998", "Whose dog?", "Which lake?"], \
        "a week on: one more item, and a group is one item"
    late = ask(app, [p[7]], "And who is carving?", to="Cleo", key="Thanksgiving 1998")
    assert [t for _, t in inbox(app, CLEO)][0] == "4 questions about Thanksgiving 1998", "joins its open item, for nothing"
    got = call(app, "GET", "/api/questions", CLEO)
    group = next(it for it in got["items"] if it["group_key"] == "Thanksgiving 1998")
    assert [q["id"] for q in group["questions"]] == [*tg, late]
    q = group["questions"][0]
    assert q["asked_by"] == {"id": app.agent, "name": "The estate's agent", "kind": "agent"} and q["to"] == "you"
    assert q["photos"][0]["sha"] == p[1], "each photo as /api/search gives it"
    for qid in [*tg, late]:
        call(app, "POST", f"/api/questions/{qid}/answer", CLEO, {"text": "Grandma Rose"}, expect=201)
    assert "4 questions about Thanksgiving 1998" not in [t for _, t in inbox(app, CLEO)], "all answered: done"
    app.clock = lambda: T0 + timedelta(days=30)
    titles = [t for _, t in inbox(app, CLEO)]
    assert "Which year?" not in titles and "Who took this?" in titles, "a dismissed question never comes back"
    con = db(app)
    assert con.execute("SELECT status FROM questions WHERE id=?", (q5,)).fetchone()[0] == "closed"
    assert con.execute("SELECT status FROM questions WHERE id=?", (q6,)).fetchone()[0] == "asked"
    # a member's own allowance (members.questions_per_week)
    con.execute("UPDATE members SET questions_per_week=1 WHERE id=?", (mid(app, BEN),))
    con.commit()
    con.close()
    ask(app, [p[1]], "One?", to="Ben")
    ask(app, [p[2]], "Two?", to="Ben")
    assert len(inbox(app, BEN)) == 1
    assert q1


def test_a_family_question_is_each_members_and_every_answer_is_a_story(app):
    x = pick(app, 41)
    q = ask(app, [x], "What happened here?", follow_up=True)
    assert all(len(inbox(app, w)) == 1 for w in (ADA, BEN, CLEO))
    a = call(app, "POST", f"/api/questions/{q}/answer", ADA, {"text": "The flood of 1996"}, expect=201)
    b = app.request("POST", f"/api/questions/{q}/answer", headers={**BEN, "Content-Type": "audio/wav; codecs=1"}, body=wav())
    assert b.status == 201, b.body
    b = b.json()
    assert a["question"] == {"id": q, "text": "What happened here?"} and b["transcript_state"] == "waiting"
    told = call(app, "GET", f"/api/photos/{x}/stories", CLEO)["stories"]
    assert [(s["told_by"]["name"], s["text"], bool(s["recording"])) for s in told] == \
        [("Ada", "The flood of 1996", False), ("Ben", None, True)], "two stories, side by side"
    assert len(inbox(app, CLEO)) == 1 and inbox(app, ADA) == [] and inbox(app, BEN) == []
    con = db(app)
    rec = con.execute("SELECT recording, recording_ext FROM stories WHERE id=?", (b["id"],)).fetchone()
    assert rec[1] == ".wav" and not con.execute("SELECT 1 FROM assets WHERE sha256=?", (rec[0],)).fetchone(), \
        "the recording is in no photo view"
    assert stories.recording_path(app.recordings, *rec).read_bytes() == wav()
    assert not (ROOT / "demo" / "store" / rec[0][:2]).exists(), "never in the checked-in demo store"
    assert [(k, json.loads(d)) for k, d in con.execute("SELECT kind, data FROM journal WHERE kind LIKE 'story.%' ORDER BY id")] == [
        ("story.told", {"question": q, "photos": 1, "recording": False, "text": True}),
        ("story.told", {"question": q, "photos": 1, "recording": True, "text": False})]
    con.close()
    # bad recordings
    for ctype, body, code in (("audio/flac", wav(), 415), ("audio/wav", b"not a recording", 400), ("audio/wav", b"", 400)):
        r = app.request("POST", f"/api/stories?photos={x}", headers={**BEN, "Content-Type": ctype}, body=body)
        assert r.status == code, (ctype, r.status, r.body)


# -- spoken: the upload, the queue, a stand-in transcriber -----------------------------------------------

STAND_IN = r'''
import sqlite3, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import transcribe
con = sqlite3.connect(sys.argv[2])
transcribe.run(con, Path(sys.argv[3]), transcribe=lambda path: f"heard {len(transcribe.decode(path))} samples")
'''


def test_a_spoken_answer_is_uploaded_queued_and_written_out_once(app, tmp_path):
    x = pick(app, 42)
    q = ask(app, [x], "Who is singing?", to="Cleo")
    inbox(app, CLEO)
    sid = app.request("POST", f"/api/questions/{q}/answer", headers={**CLEO, "Content-Type": "audio/wav"},
                      body=wav(1.0, 22050)).json()["id"]
    assert app.recordings_waiting() == 1
    stand_in = tmp_path / "stand_in.py"
    stand_in.write_text(STAND_IN)
    woke = []
    app.transcribe.command = [sys.executable, str(stand_in), str(ROOT), app.a.db, str(app.recordings)]
    app.transcribe.then = lambda: woke.append(1)
    assert app.transcribe.once() == 0 and woke == [1]
    got = call(app, "GET", f"/api/photos/{x}/stories", CLEO)["stories"][0]
    heard = got["transcript"]
    assert got["transcript_state"] == "done" and abs(int(heard.split()[1]) - 16000) < 100, "decoded to 16 kHz mono"
    assert app.transcribe.once() is None, "nothing is waiting"
    con = db(app)
    assert [json.loads(d) for d, in con.execute("SELECT data FROM journal WHERE kind='story.transcribed'")] == [{"chars": len(heard)}]
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE stories SET transcript='something else' WHERE id=?", (sid,))
    stories.transcribed(con, sid, text="again")                     # (and stories.transcribed writes it once only)
    assert con.execute("SELECT transcript FROM stories WHERE id=?", (sid,)).fetchone()[0] == heard
    con.close()


def test_without_the_model_or_mlx_the_job_says_so_on_each_recording(app, tmp_path, monkeypatch):
    x = pick(app, 43)
    sid = app.request("POST", f"/api/stories?photos={x}", headers={**ADA, "Content-Type": "audio/wav"}, body=wav()).json()["id"]
    con = db(app)
    monkeypatch.setattr(transcribe, "model", lambda: (_ for _ in ()).throw(transcribe.ModelMissing(transcribe.MISSING)))
    assert transcribe.run(con, app.recordings, out=lambda *a: None) == {"done": 0, "failed": 1, "missing": True}
    con.close()
    got = call(app, "GET", f"/api/photos/{x}/stories", ADA)["stories"][0]
    assert got["transcript_state"] == "failed" and "transcribe.py fetch" in got["transcript_error"]
    # a job that cannot run at all (no uv, no MLX on this computer) marks them too, and they wait to be tried again
    app.transcribe.command = [sys.executable, "-c", "import sys; print('no MLX here'); sys.exit(1)"]
    assert app.transcribe.once() == 1
    con = db(app)
    assert con.execute("SELECT transcript_error FROM stories WHERE id=?", (sid,)).fetchone()[0] == \
        "the transcription job failed: no MLX here"
    assert [r[0] for r in stories.pending(con)] == [sid], "and it waits to be tried again"
    con.close()


def test_decode_reads_what_a_phone_and_a_browser_record(tmp_path):
    """PyAV, not a system ffmpeg: a WAV, and (where this PyAV can encode them) AAC in .m4a as an iPhone records it
    and Opus in .webm as Chrome does."""
    f = tmp_path / "a.wav"
    f.write_bytes(wav(0.5, 44100))
    assert len(transcribe.decode(f)) == 8000
    import av
    for name, codec, rate in (("a.m4a", "aac", 44100), ("a.webm", "libopus", 48000)):
        try:
            with av.open(str(tmp_path / name), "w") as c:
                s = c.add_stream(codec, rate=rate)
                s.layout = "mono"
                t = np.arange(rate // 2) / rate
                frame = av.AudioFrame.from_ndarray((np.sin(2 * np.pi * 440 * t) * 8000).astype(np.int16)[None, :],
                                                   format="s16", layout="mono")
                frame.rate = rate
                for packet in [*s.encode(frame), *s.encode(None)]:
                    c.mux(packet)
        except Exception as e:            # an encoder this PyAV build lacks: nothing to read back
            print(f"{name}: not made here ({type(e).__name__}: {e})")
            continue
        n = len(transcribe.decode(tmp_path / name))
        assert 7000 < n < 9500, (name, n)


def test_a_long_recording_is_cut_where_it_is_quiet_and_nothing_is_lost():
    """pieces() (the ONNX model takes a recording whole): no piece is longer than the chunk, the pieces are the
    recording again, and each cut is in a pause, not through the speech around it."""
    rate = 100
    rng = np.random.default_rng(0)
    said = rng.normal(0, 0.5, 35 * rate).astype(np.float32)
    for at in (8.0, 17.5, 26.0):                        # three pauses of one second
        said[int(at * rate):int((at + 1) * rate)] = 0
    got = transcribe.pieces(said, rate, chunk=10, look=4, quiet=0.3)
    assert all(len(p) <= 10 * rate for p in got) and len(got) == 4
    assert np.array_equal(np.concatenate(got), said)
    cuts = np.cumsum([len(p) for p in got])[:-1] / rate
    assert 8.0 < cuts[0] < 9.0 and 17.5 < cuts[1] < 18.5 and 26.0 < cuts[2] < 27.0, cuts
    short = said[:5 * rate]
    assert [len(p) for p in transcribe.pieces(short, rate, chunk=10, look=4)] == [len(short)]


# -- append-only ----------------------------------------------------------------------------------------

def test_stories_are_append_only_but_for_their_transcript(tmp_path):
    con = sqlite3.connect(tmp_path / "estate.db")
    schema.ensure(con, *schema.CATALOG_AREAS)
    con.execute("INSERT INTO stories(id, member_id, text, recording, recording_ext, told_at) VALUES (1, 2, 'hello', 'ab', '.m4a', 't')")
    con.execute("INSERT INTO story_photos VALUES (1, 'x')")
    con.commit()
    for q in ("DELETE FROM stories", "UPDATE stories SET text='changed'", "UPDATE stories SET member_id=3",
              "UPDATE stories SET told_at='later'", "UPDATE stories SET recording='cd'", "UPDATE stories SET question_id=4",
              "UPDATE stories SET id=9", "INSERT OR REPLACE INTO stories(id, member_id, text) VALUES (1, 3, 'replaced')",
              "DELETE FROM story_photos", "UPDATE story_photos SET sha256='y'"):
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(q)
    con.execute("UPDATE stories SET transcript_error='no model' WHERE id=1")        # why it waits: may change
    con.execute("UPDATE stories SET transcript='hello there', transcribed_at='t2', transcript_error=NULL WHERE id=1")
    for q in ("UPDATE stories SET transcript='again'", "UPDATE stories SET transcribed_at='t3'"):
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(q)
    con.execute("INSERT OR REPLACE INTO story_photos VALUES (1, 'x')")               # skipped, never a delete
    assert con.execute("SELECT * FROM story_photos").fetchall() == [(1, "x")]
    assert con.execute("SELECT text, transcript FROM stories").fetchall() == [("hello", "hello there")]


def test_purging_a_photo_takes_it_out_of_its_stories_and_keeps_them(tmp_path, estate_with_three):  # noqa: F811
    db_, store, _, admin = estate_with_three
    gone, kept = sha(PHOTOS["IMG_2.JPG"]), sha(PHOTOS["IMG_1.JPG"])
    con = sqlite3.connect(db_)
    no = lambda s, m: False      # noqa: E731
    both = stories.tell(con, admin.id, [gone, kept], no, text="the two of them")
    alone = stories.tell(con, admin.id, [gone], no, text="only the receipt")
    q = stories.ask(con, admin.id, None, [gone], "What is this?", to=admin.id, refused=no)["question"]
    con.close()
    run("--db", db_, "purge", gone, "--reason", "a receipt", "--store", store, "--backup-dir", tmp_path / "backups")
    con = sqlite3.connect(db_)
    assert con.execute("SELECT count(*) FROM story_photos WHERE sha256=?", (gone,)).fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM stories").fetchone()[0] == 2, "what was said stays"
    gone_now = lambda s, m: s == gone    # noqa: E731  (as the app: not in the catalog)
    assert stories.story(con, both["id"], admin.id + 1, gone_now)["photos"] == [kept]
    assert stories.story(con, alone["id"], admin.id + 1, gone_now) is None, "about no photo now: its teller's only"
    assert stories.story(con, alone["id"], admin.id, gone_now)["photos"] == []
    assert stories.deal(con, admin.id, T0, gone_now) == 0, "a question about a purged photo is never dealt"
    assert q
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("DELETE FROM story_photos")                                       # the protection is back
    con.close()
