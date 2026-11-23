"""stories.py — questions and stories (multiplayer phase 7; standard library only, so transcribe.py can use it).
The words are the multiplayer plan's (docs/notes/MULTIPLAYER-PLAN.md).

A **question** is what the estate's agent (or a member) asks a member, or the whole family, about photos:
`ask` files it and returns at once (the MCP tool ask_member). Nobody waits for the answer: the waiting is the
question's row. A question may name only photos its recipient may be shown (shared_for; for the family, only
photos private to nobody), and only photos its asker sees: anything else is refused, never trimmed.

The **inbox decides how many** a member sees, not the asker. A question is `queued` until `deal` puts it in a
member's inbox (an `asked` row, and one `question` inbox item per group); deal runs when the member's inbox is
read (GET /api/inbox, GET /api/questions), with the clock it is given:

  - a member is dealt at most ALLOWANCE new inbox items in any 7 days (WEEK), or their members.questions_per_week,
    and never while ALLOWANCE of them are open: one group is one item, whatever it holds;
  - a group is the questions with one `group_key` ("Thanksgiving 1998": "3 questions about Thanksgiving 1998");
    a question with none is a group of its own. A question that arrives while its group's item is open joins it
    and costs nothing;
  - groups are dealt in this order: those naming a photo nobody has told a story about first, then those naming
    a photo of a series (an album that grows from a search, or a published series), then the asker's priority
    (highest first), then the oldest;
  - a question naming a photo the member may not be shown now waits (and one already dealt is left out of their
    inbox, item and all, until they may again).
Saying "I don't know" (dismissing a question, or its whole inbox item) puts it away for good for that member. It
counts against the week it was dealt in, like any item, and against nothing after: the window is the last 7 days.

A **story** is what a member said about photos, attributed and dated: typed text, or a recording (a file in the
store, content-addressed, with no assets row, so it is in no photo view) and its transcript, written later by
transcribe.py. Stories are never edited: a second member's answer to a question is a second story. A story can
be told with no question (the viewer's "tell a story about this photo"). Who may read one: its teller, and
whoever may be shown every one of its photos (a story about two photos, one private to its teller, is theirs
alone; one whose photos were all purged is its teller's). A recording is served by the same rule.

  ALLOWANCE, WEEK, MAX_PHOTOS, AUDIO          the constants (AUDIO: the recording types taken, and their extension)
  StoryError                                  what a member may not do, with its HTTP status
  ask(con, asker, acting_for, photos, text, why=, to=, group_key=, priority=, follow_up=, task=, refused=)
  deal(con, member, now, refused) -> int      the inbox's half: questions dealt to the member's inbox now
  waiting(con, member, refused) -> [dict]     the member's open questions, grouped as their inbox items
  answer(con, member, qid, refused, text=, recording=) / dismiss(con, member, qid)
  tell(con, member, photos, refused, text=, recording=, question=) -> dict     a story (story.told)
  readable(con, sid, member, refused) / about(con, shas, member, refused) -> {sha: [story]}   reading
  save_recording(con, store, body, content_type) -> (sha, ext)    the upload's bytes into the store
  recording_path(store, sha, ext), pending(con), transcribed(con, sid, text=, error=)       transcribe.py's half
  keep_item(con, member, refused)             the inbox's filter: a question item with nothing left to show is not shown

`refused(sha, member)` is the app's word that a member may not be shown a photo (PhotoApp.refused_photo: not in
the catalog, or private to someone else); the estate's agent acting for no one is asked as itself, which is shown
only what is private to nobody (private.Private.agents), and a question to the family checks private.NOBODY.

Journal (journal.py): question.asked {to, photos, group_key} by the asker, acting for whom its run acted for;
question.dismissed {question} by the member; story.told {question, photos, recording} by the teller (subject
story:<id>); story.transcribed {chars} by no one (the app), acting for the teller. Inbox items (member_inbox.py):
`question`, ref ask:<group>, for the member, done once each question in it is answered or put away.
"""
from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import journal
import member_inbox
import members

ALLOWANCE = 3                 # question items a member is dealt a week (open question 2: default low; members.questions_per_week)
WEEK = timedelta(days=7)
MAX_PHOTOS = 20               # photos one question or story may name
MAX_RECORDING = 8 * 1024 * 1024   # bytes: http_util.MAX_BODY (an AAC recording of 15 minutes or more)
AUDIO = {"audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/m4a": ".m4a", "audio/aac": ".aac", "audio/mpeg": ".mp3",
         "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/wave": ".wav", "audio/webm": ".webm", "audio/ogg": ".ogg"}
TYPES = {".m4a": "audio/mp4", ".aac": "audio/aac", ".mp3": "audio/mpeg", ".wav": "audio/wav", ".webm": "audio/webm",
         ".ogg": "audio/ogg"}
NOBODY = 0                    # private.NOBODY: who a question to the family is checked as

Refused = Callable[[str, int | None], bool]


class StoryError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def stamp(t: datetime | None = None) -> str:
    return (t or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _photos(photos) -> list[str]:
    if not isinstance(photos, list) or not photos or len(photos) > MAX_PHOTOS or not all(isinstance(s, str) for s in photos):
        raise StoryError(400, f"photos is a list of 1 to {MAX_PHOTOS} photo ids")
    return list(dict.fromkeys(photos))


def _names(con) -> dict[int, tuple[str, str]]:
    return {i: (n, k or "person") for i, n, k in con.execute("SELECT id, name, kind FROM members")}


# -- asking ------------------------------------------------------------------------------------------------

def recipient(con, to) -> members.Member | None:
    """The member a question is `to` (members.id, or a name as library_overview lists it), or None for the family.
    StoryError 400 for anyone who is not an active person."""
    if to is None or to == "":
        return None
    fam = members.family(con)
    got = [m for m in fam if (isinstance(to, int) and not isinstance(to, bool) and m.id == to)
           or (isinstance(to, str) and m.name.lower() == to.strip().lower())]
    if not got:
        raise StoryError(400, f"nobody in the family is {to!r}: `to` is a member's name or id, as library_overview lists them")
    return got[0]


def ask(con, asker: int | None, acting_for: int | None, photos, text, why=None, to=None, group_key=None, priority=None,
        follow_up=False, task: int | None = None, refused: Refused = lambda s, m: False) -> dict:
    """File a question; it is queued (deal puts it in an inbox). `asker`: who asks (the estate's agent, or a member);
    `acting_for`: whom the asking run works for (None: no one). StoryError 400 for a bad argument, 403 for a photo
    the recipient (or, for the family, everyone) may not be shown, 404 for one the asker does not see."""
    shas = _photos(photos)
    text = " ".join(str(text or "").split())
    if not text or len(text) > 500:
        raise StoryError(400, "text is the question, in up to 500 characters")
    who = recipient(con, to)
    viewer = acting_for if acting_for is not None else asker
    for s in shas:
        if refused(s, viewer):
            raise StoryError(404, f"no such photo: {s}")
    for s in shas:
        if who is None and refused(s, NOBODY):
            raise StoryError(403, f"photo {s} is not one the whole family can see: a question to the family names only those")
        if who is not None and refused(s, who.id):
            raise StoryError(403, f"{who.name} may not be shown photo {s}: ask them only about photos they can see")
    key = " ".join(str(group_key).split())[:120] if group_key else None
    pri = float(priority) if isinstance(priority, (int, float)) and not isinstance(priority, bool) else None
    t = stamp()
    with con:
        qid = con.execute("INSERT INTO questions(asked_by, acting_for, to_member, shas, text, why, group_key, priority,"
                          " follow_up, task_id, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,'queued',?)",
                          (asker, acting_for, who.id if who else None, json.dumps(shas), text,
                           (str(why)[:500] if why else None), key, pri, int(bool(follow_up)), task, t)).lastrowid
        journal.append(con, asker, "question.asked", f"question:{qid}",
                       {"to": who.id if who else "family", "photos": len(shas), "group_key": key}, acting_for)
    return {"question": qid, "status": "queued", "to": who.name if who else "the family", "photos": len(shas),
            "group_key": key, "follow_up": bool(follow_up)}


def _allowance(con, member: int) -> int:
    row = con.execute("SELECT questions_per_week FROM members WHERE id=?", (member,)).fetchone()
    return row[0] if row and isinstance(row[0], int) and row[0] >= 0 else ALLOWANCE


def _group(key: str | None, qid: int) -> str:
    return key or f"q{qid}"


def _title(n: int, key: str | None, text: str) -> str:
    if n == 1:
        return text
    return f"{n} questions about {key}" if key else f"{n} questions"


def _for(con, member: int) -> list[tuple]:
    """Questions for `member` that are not closed and not dealt to them yet: (id, shas, text, key, priority, at)."""
    if not con.execute(f"SELECT 1 FROM members WHERE id=? AND status='active' AND {members.PEOPLE}", (member,)).fetchone():
        return []
    return con.execute(
        "SELECT q.id, q.shas, q.text, q.group_key, q.priority, q.created_at FROM questions q WHERE q.status != 'closed'"
        " AND (q.to_member=? OR (q.to_member IS NULL AND q.asked_by IS NOT ?))"
        " AND NOT EXISTS (SELECT 1 FROM asked a WHERE a.question_id=q.id AND a.member_id=?) ORDER BY q.id",
        (member, member, member)).fetchall()


def deal(con, member: int | None, now: datetime | None = None, refused: Refused = lambda s, m: False) -> int:
    """Put questions in `member`'s inbox, within their allowance (the module docstring has the rules). Returns how
    many new inbox items it made. In its own transaction."""
    if member is None:
        return 0
    now = now or datetime.now(timezone.utc)
    t = stamp(now)
    rows = [r for r in _for(con, member) if not any(refused(s, member) for s in json.loads(r[1]))]
    if not rows:
        return 0
    made = 0
    with con:
        groups: dict[str, list[tuple]] = {}
        for r in rows:
            groups.setdefault(_group(r[3], r[0]), []).append(r)
        open_items = {ref.partition(":")[2]: iid for iid, ref in con.execute(
            "SELECT id, ref FROM inbox_items WHERE member_id=? AND kind='question' AND state='open'", (member,))}
        for key, qs in list(groups.items()):        # a question whose group is in the inbox already joins it, for nothing
            if key in open_items:
                _deal(con, member, open_items[key], qs, t)
                del groups[key]
        allowance = _allowance(con, member)
        week = con.execute("SELECT count(*) FROM (SELECT item_id, min(at) t FROM asked WHERE member_id=? GROUP BY item_id)"
                           " WHERE t > ?", (member, stamp(now - WEEK))).fetchone()[0]
        room = min(allowance - week, allowance - len(open_items))
        if room <= 0 or not groups:
            return 0
        shas = {s for qs in groups.values() for r in qs for s in json.loads(r[1])}
        told = _having(con, "SELECT DISTINCT sha256 FROM story_photos WHERE sha256 IN ({})", shas)
        series = _having(con, "SELECT DISTINCT i.sha256 FROM change_items i JOIN change_sets s ON s.id=i.set_id"
                              " WHERE s.status IS NOT 'discarded' AND s.op='view' AND (s.grows_from IS NOT NULL OR EXISTS"
                              " (SELECT 1 FROM lenses l WHERE l.kind='series' AND l.set_id=s.id AND l.published=1))"
                              " AND i.sha256 IN ({})", shas)

        def rank(qs):
            ss = {s for r in qs for s in json.loads(r[1])}
            return (not (ss - told), not (ss & series), -max((r[4] or 0) for r in qs), min(r[5] or "" for r in qs))
        for key, qs in sorted(groups.items(), key=lambda kv: rank(kv[1]))[:room]:
            title = _title(len(qs), qs[0][3], qs[0][2])
            iid = member_inbox.put(con, [member], "question", f"ask:{key}", title)[0]
            _deal(con, member, iid, qs, t)
            made += 1
    return made


def _having(con, q: str, shas) -> set[str]:
    out: set[str] = set()
    xs = list(shas)
    for i in range(0, len(xs), 500):
        part = xs[i:i + 500]
        out |= {s for s, in con.execute(q.format(",".join("?" * len(part))), part)}
    return out


def _deal(con, member: int, iid: int, qs, t: str) -> None:
    con.executemany("INSERT OR IGNORE INTO asked(question_id, member_id, item_id, at) VALUES (?,?,?,?)",
                    ((r[0], member, iid, t) for r in qs))
    con.executemany("UPDATE questions SET status='asked' WHERE id=? AND status='queued'", ((r[0],) for r in qs))
    n = con.execute("SELECT count(*) FROM asked WHERE item_id=?", (iid,)).fetchone()[0]
    if n > len(qs):        # joined an item: its title counts them all
        first = con.execute("SELECT q.group_key, q.text FROM asked a JOIN questions q ON q.id=a.question_id WHERE a.item_id=?"
                            " ORDER BY q.id LIMIT 1", (iid,)).fetchone()
        con.execute("UPDATE inbox_items SET title=? WHERE id=?", (_title(n, first[0], first[1])[:300], iid))


def _open(con, member: int) -> list[tuple]:
    """The member's dealt questions still waiting on them: (question, item, shas, text, why, key, asked_by, acting_for,
    to_member, created_at)."""
    return con.execute(
        "SELECT q.id, a.item_id, q.shas, q.text, q.why, q.group_key, q.asked_by, q.acting_for, q.to_member, q.created_at"
        " FROM asked a JOIN questions q ON q.id=a.question_id JOIN inbox_items i ON i.id=a.item_id"
        " WHERE a.member_id=? AND a.answered_at IS NULL AND a.dismissed_at IS NULL AND i.state='open' AND q.status != 'closed'"
        " ORDER BY a.item_id, q.id", (member,)).fetchall()


def waiting(con, member: int | None, refused: Refused = lambda s, m: False) -> list[dict]:
    """GET /api/questions: the member's open questions as their inbox items, newest item first, each question with
    who asked, why, and its photos (ids: the caller adds rows). A question naming a photo they may not be shown now
    is left out, and an item with none left."""
    if member is None:
        return []
    names = _names(con)
    items: dict[int, dict] = {}
    for qid, iid, shas, text, why, key, by, af, to, at in _open(con, member):
        photos = json.loads(shas)
        if any(refused(s, member) for s in photos):
            continue
        it = items.setdefault(iid, {"item": iid, "group_key": key, "questions": []})
        asker = names.get(by, ("Someone", "person")) if by is not None else ("Someone", "person")
        it["questions"].append({"id": qid, "text": text, "why": why, "photos": photos, "asked_at": at,
                                "asked_by": {"id": by, "name": asker[0], "kind": asker[1]},
                                "for": names[af][0] if af in names and asker[1] == "agent" else None,
                                "to": "you" if to == member else "the family"})
    return sorted(items.values(), key=lambda x: -x["item"])


def keep_item(con, member: int | None, refused: Refused):
    """member_inbox.mine's `keep`: a question item is shown only while one of its questions may be shown to them."""
    shown = {it["item"] for it in waiting(con, member, refused)}
    return lambda item: item["kind"] != "question" or item["id"] in shown


def _asked(con, member: int, qid: int) -> tuple:
    row = con.execute("SELECT a.item_id, q.shas, q.to_member, q.status FROM asked a JOIN questions q ON q.id=a.question_id"
                      " WHERE a.question_id=? AND a.member_id=?", (qid, member)).fetchone()
    if row is None:
        raise StoryError(404, "no such question")
    return row


def _settle(con, iid: int) -> None:
    """An item whose every question is answered or put away by its member is done."""
    if not con.execute("SELECT 1 FROM asked WHERE item_id=? AND answered_at IS NULL AND dismissed_at IS NULL LIMIT 1",
                       (iid,)).fetchone():
        con.execute("UPDATE inbox_items SET state='done', done_at=? WHERE id=? AND state='open'", (stamp(), iid))


def dismiss(con, member: int | None, qid: int) -> dict:
    """POST /api/questions/<id>/dismiss: "I don't know". Put away for this member for good (a question to them alone
    is closed). It counted against the week it was dealt in, and counts against nothing after (deal)."""
    if member is None:
        raise StoryError(404, "no such question")
    iid, _, to, _ = _asked(con, member, qid)
    with con:
        n = con.execute("UPDATE asked SET dismissed_at=? WHERE question_id=? AND member_id=? AND dismissed_at IS NULL"
                        " AND answered_at IS NULL", (stamp(), qid, member)).rowcount
        if n:
            if to == member:
                con.execute("UPDATE questions SET status='closed' WHERE id=? AND status != 'answered'", (qid,))
            journal.append(con, member, "question.dismissed", f"question:{qid}", {"question": qid})
            _settle(con, iid)
    return {"question": qid, "dismissed": bool(n)}


def answer(con, member: int | None, qid: int, refused: Refused, text=None, recording=None) -> dict:
    """POST /api/questions/<id>/answer: the member's answer, a story about the question's photos. Only a question
    dealt to them (404 otherwise, or for one naming a photo they may not be shown now). A second answer, theirs or
    anyone's, is another story."""
    if member is None:
        raise StoryError(404, "no such question")
    iid, shas, _, _ = _asked(con, member, qid)
    photos = json.loads(shas)
    if any(refused(s, member) for s in photos):
        raise StoryError(404, "no such question")
    story = tell(con, member, photos, refused, text=text, recording=recording, question=qid, _asked=iid)
    return story


def tell(con, member: int | None, photos, refused: Refused, text=None, recording=None, question=None, _asked=None) -> dict:
    """A story the member tells about photos they may be shown (404 for any other): `text`, or `recording` (sha, ext)
    from save_recording, or both. One story.told entry; answering closes their part of the question."""
    if member is None:
        raise StoryError(403, "only a member tells a story")
    shas = _photos(photos)
    for s in shas:
        if refused(s, member):
            raise StoryError(404, f"no such photo: {s}")
    text = str(text).strip()[:10000] if text else None
    if not text and not recording:
        raise StoryError(400, "a story is text, or a recording")
    t = stamp()
    with con:
        sid = con.execute("INSERT INTO stories(member_id, question_id, text, recording, recording_ext, told_at)"
                          " VALUES (?,?,?,?,?,?)", (member, question, text, recording[0] if recording else None,
                                                    recording[1] if recording else None, t)).lastrowid
        con.executemany("INSERT INTO story_photos(story_id, sha256) VALUES (?,?)", ((sid, s) for s in shas))
        if question is not None:
            con.execute("UPDATE asked SET answered_at=? WHERE question_id=? AND member_id=? AND answered_at IS NULL",
                        (t, question, member))
            con.execute("UPDATE questions SET status='answered' WHERE id=?", (question,))
            if _asked is not None:
                _settle(con, _asked)
        journal.append(con, member, "story.told", f"story:{sid}",
                       {"question": question, "photos": len(shas), "recording": bool(recording), "text": bool(text)})
    got = story(con, sid, member, refused)
    assert got is not None
    return got


# -- reading -------------------------------------------------------------------------------------------------

COLS = "s.id, s.member_id, s.question_id, s.text, s.recording, s.recording_ext, s.told_at, s.transcript, s.transcribed_at," \
       " s.transcript_error"


def _shas(con, sid: int) -> list[str]:
    return [s for s, in con.execute("SELECT sha256 FROM story_photos WHERE story_id=? ORDER BY rowid", (sid,))]


def may_read(teller: int, shas: list[str], member: int | None, refused: Refused) -> bool:
    """Its teller; anyone else who may be shown every one of its photos (none left: its teller only)."""
    return member == teller or bool(shas) and not any(refused(s, member) for s in shas)


def _brief(con, row, shas, member, names, refused) -> dict:
    sid, teller, qid, text, rec, ext, at, transcript, tat, err = row
    q = con.execute("SELECT text FROM questions WHERE id=?", (qid,)).fetchone() if qid is not None else None
    return {"id": sid, "told_by": {"id": teller, "name": names.get(teller, ("Someone",))[0]}, "told_at": at, "text": text,
            "question": {"id": qid, "text": q[0]} if q else None,
            "photos": [s for s in shas if not refused(s, member)],
            "recording": {"url": f"/api/stories/{sid}/recording", "type": TYPES.get(ext or "", "audio/mp4")} if rec else None,
            "transcript": transcript, "transcribed_at": tat,
            "transcript_state": None if not rec else "done" if transcript is not None else "failed" if err else "waiting",
            "transcript_error": err if rec and transcript is None else None}


def story(con, sid: int, member: int | None, refused: Refused) -> dict | None:
    """One story as `member` may read it, or None."""
    row = con.execute(f"SELECT {COLS} FROM stories s WHERE id=?", (sid,)).fetchone()
    if row is None:
        return None
    shas = _shas(con, sid)
    if not may_read(row[1], shas, member, refused):
        return None
    return _brief(con, row, shas, member, _names(con), refused)


def about(con, photos, member: int | None, refused: Refused) -> dict[str, list[dict]]:
    """{sha: [the stories about it `member` may read, oldest first]} for those of `photos` that have any."""
    want = [s for s in dict.fromkeys(photos) if not refused(s, member)]
    if not want:
        return {}
    ids: dict[int, set[str]] = {}
    for i in range(0, len(want), 500):
        part = want[i:i + 500]
        for sid, sha in con.execute(f"SELECT story_id, sha256 FROM story_photos WHERE sha256 IN ({','.join('?' * len(part))})",
                                    part):
            ids.setdefault(sid, set()).add(sha)
    names, out = _names(con), {}
    for sid in sorted(ids):
        row = con.execute(f"SELECT {COLS} FROM stories s WHERE id=?", (sid,)).fetchone()
        shas = _shas(con, sid)
        if row is None or not may_read(row[1], shas, member, refused):
            continue
        b = _brief(con, row, shas, member, names, refused)
        for sha in ids[sid]:
            out.setdefault(sha, []).append(b)
    return out


def recording_of(con, sid: int, member: int | None, refused: Refused) -> tuple[str, str] | None:
    """(sha, ext) of a story's recording, for a member who may read the story; None otherwise."""
    row = con.execute("SELECT member_id, recording, recording_ext FROM stories WHERE id=?", (sid,)).fetchone()
    if row is None or not row[1] or not may_read(row[0], _shas(con, sid), member, refused):
        return None
    return row[1], row[2] or ""


# -- recordings ---------------------------------------------------------------------------------------------

def recording_path(store: Path, sha: str, ext: str) -> Path:
    """Where a recording is in the store: as an original is (estate.blob_path)."""
    return store / sha[:2] / sha[2:4] / f"{sha}{ext}"


def audio_ext(content_type: str | None) -> str:
    ext = AUDIO.get((content_type or "").split(";")[0].strip().lower())
    if not ext:
        raise StoryError(415, "a recording is sent as its own body, Content-Type one of " + ", ".join(sorted(AUDIO)))
    return ext


def save_recording(con, store: Path, body: bytes, content_type: str | None) -> tuple[str, str]:
    """The upload's bytes into the store, content-addressed (estate.store_blob, with the immutable flag in a store
    marked for it), and no assets row: (sha, ext). StoryError 415 for a type not taken, 413 over MAX_RECORDING, 400
    for bytes that are no recording PyAV can read (it carries its own FFmpeg: the mini needs none)."""
    ext = audio_ext(content_type)
    if len(body) > MAX_RECORDING:
        raise StoryError(413, f"a recording is at most {MAX_RECORDING} bytes")
    if not body:
        raise StoryError(400, "the recording is empty")
    try:
        import av
        with av.open(io.BytesIO(body)) as c:
            if not c.streams.audio:
                raise ValueError("no sound in it")
    except ImportError:              # (the app's environment has PyAV: embed.py's header)
        pass
    except Exception as e:
        raise StoryError(400, f"this is not a recording the estate can read ({type(e).__name__})") from e
    import estate                    # (here: transcribe.py imports this module and has no Pillow)
    (store / ".tmp").mkdir(parents=True, exist_ok=True)
    sha, _, _ = estate.store_blob(con, store, io.BytesIO(body), ext, lock=estate.immutable(store))
    return sha, ext


def pending(con) -> list[tuple[int, str, str]]:
    """Stories whose recording has no transcript yet (a failed one is tried again): (id, sha, ext), oldest first."""
    return con.execute("SELECT id, recording, recording_ext FROM stories WHERE recording IS NOT NULL AND transcript IS NULL"
                       " ORDER BY id").fetchall()


def transcribed(con, sid: int, text: str | None = None, error: str | None = None) -> None:
    """transcribe.py's word on one recording: its transcript (written once: the stories trigger refuses a second),
    journaled story.transcribed by no one acting for the teller; or why it could not be (kept, tried again)."""
    with con:
        if text is not None:
            n = con.execute("UPDATE stories SET transcript=?, transcribed_at=?, transcript_error=NULL WHERE id=?"
                            " AND transcript IS NULL", (text, stamp(), sid)).rowcount
            if n:
                teller = con.execute("SELECT member_id FROM stories WHERE id=?", (sid,)).fetchone()[0]
                journal.append(con, None, "story.transcribed", f"story:{sid}", {"chars": len(text)}, teller)
        else:
            con.execute("UPDATE stories SET transcript_error=? WHERE id=? AND transcript IS NULL", ((error or "failed")[:500], sid))


def album_stories(con, shas: list[str], member: int | None, refused: Refused) -> list[dict]:
    """A series' stories: those told about any of its photos (`shas`, as the member sees the album) that the member
    may read, newest first, each once."""
    seen: dict[int, dict] = {}
    for lst in about(con, shas, member, refused).values():
        for b in lst:
            seen[b["id"]] = b
    return sorted(seen.values(), key=lambda b: -b["id"])
