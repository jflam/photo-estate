"""A question and whom it was dealt to (stories.py, member_inbox.dismiss, listeners.py's stories listener), on a scratch
catalog: the way the model of them (specs/Question.tla, checked by TLC) found a question put away could stay waiting
in an inbox item nobody sees, replayed on the real code, beside the orders that must keep working.

It needs no bad timing: the inbox page's Dismiss, on a question's card. No writer here reads what it decides by before
it holds the write lock but stories.answer and stories.dismiss, whose reads (the asked row's item, the question's
photos and recipient) are never written after; the one race tried here lands a member's "I don't know" in answer's
gap on a `Gap` connection, as tests/test_inbox_states.py does, and reaches only what the same two requests one after
the other reach.

  uv run pytest tests/test_question_states.py
"""
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import estate  # noqa: E402
import journal  # noqa: E402
import listeners  # noqa: E402
import member_inbox  # noqa: E402
import members  # noqa: E402
import stories  # noqa: E402
from core import schema  # noqa: E402
from core.states import QUESTION_MOVES, IllegalMove, QuestionStatus, check_move  # noqa: E402

ADA, CLEO = 1, 2                          # Ada is the sysadmin; the estate's agent asks
WRITES = re.compile(r"\s*(BEGIN|INSERT|UPDATE|DELETE|REPLACE)\b", re.I)
PHOTO = "1" * 64


def no(s, m) -> bool:
    """stories.Refused: nobody is refused a photo here."""
    return False


def weeks(n: int) -> datetime:
    """The clock deal is given, `n` weeks from now."""
    return datetime.now(timezone.utc) + stories.WEEK * n


class Gap(sqlite3.Connection):
    """A request's own connection, on which `during()` runs once: just before the first statement that writes or
    takes the write lock (by execute or executemany)."""
    during = None

    def _gap(self, sql: str) -> None:
        if self.during is not None and WRITES.match(sql):
            fn, self.during = self.during, None
            fn()

    def execute(self, sql, *args):        # type: ignore[override]
        self._gap(sql)
        return super().execute(sql, *args)

    def executemany(self, sql, *args):    # type: ignore[override]
        self._gap(sql)
        return super().executemany(sql, *args)


def gap(db: str, *steps) -> Gap:
    """A Gap connection whose gap runs each of `steps` (fn(con), each as another request of the app's, on a
    connection of its own), in order."""
    con = sqlite3.connect(db, factory=Gap, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")

    def others():
        for fn in steps:
            c = estate.connect(db)
            try:
                fn(c)
            finally:
                c.close()
    con.during = others
    return con


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "estate.db")
    con = estate.connect(path)
    schema.ensure(con, *schema.CATALOG_AREAS)
    members.add(con, "Ada", "ada", role="sysadmin", status="active")
    members.add(con, "Cleo", "cleo", role="member", status="active")
    members.agent(con)
    con.commit()
    con.close()
    return path


@pytest.fixture
def con(db):
    con = estate.connect(db)
    yield con
    con.close()


def agent(con) -> int:
    return members.agent(con).id


def ask(con, to: int | None = CLEO, key: str | None = None, text: str = "Who is this?", follow_up: bool = False) -> int:
    """A question the estate's agent asks (for no one) about one photo: to Cleo alone, or (None) to the family."""
    return stories.ask(con, agent(con), None, [PHOTO], text, to=to, group_key=key, follow_up=follow_up, refused=no)["question"]


def status(con, qid: int) -> str:
    return con.execute("SELECT status FROM questions WHERE id=?", (qid,)).fetchone()[0]


def row(con, qid: int, member: int) -> tuple | None:
    """The asked row of `qid` for `member`: (item, state of the item, answered, put away), or None (not dealt)."""
    got = con.execute("SELECT a.item_id, i.state, a.answered_at IS NOT NULL, a.dismissed_at IS NOT NULL FROM asked a"
                      " JOIN inbox_items i ON i.id=a.item_id WHERE a.question_id=? AND a.member_id=?", (qid, member)).fetchone()
    return tuple(got) if got else None


def items(con, member: int) -> list[tuple[int, str, str]]:
    """(id, ref, state) of the member's question items, oldest first."""
    return con.execute("SELECT id, ref, state FROM inbox_items WHERE member_id=? AND kind='question' ORDER BY id",
                       (member,)).fetchall()


def waiting_unseen(con) -> list[tuple[int, int, str]]:
    """(question, member, item state) of every dealt question that waits on its member in an item that is not open:
    shown nowhere (stories._open), and never dealt again (stories._for)."""
    return con.execute("SELECT a.question_id, a.member_id, i.state FROM asked a JOIN inbox_items i ON i.id=a.item_id"
                       " WHERE a.answered_at IS NULL AND a.dismissed_at IS NULL AND i.state != 'open'").fetchall()


def shown(con, member: int) -> list[int]:
    """The questions GET /api/questions lists for the member."""
    return [q["id"] for it in stories.waiting(con, member, no) for q in it["questions"]]


# -- the normal path ---------------------------------------------------------------------------------------------

def test_a_question_lives_as_its_rows_say(db, con):
    """What must not change: a question to Cleo and one to the family, in one group, dealt into one item of hers; the
    family's dealt to Ada too. Cleo answers hers and says "I don't know" to the family's: her item is done, hers is
    answered, the family's still asked; Ada answers it: answered. Another to Cleo, put away: closed, never dealt again.
    Answering it after all (another tab still showing it) tells the story, and it is answered."""
    q1, q2 = ask(con, CLEO, "Trip"), ask(con, None, "Trip")
    assert (status(con, q1), status(con, q2)) == ("queued", "queued")
    assert stories.deal(con, CLEO) == 1 and stories.deal(con, ADA) == 1
    (iid, ref, _), = items(con, CLEO)
    assert ref == "ask:Trip" and row(con, q1, CLEO)[0] == row(con, q2, CLEO)[0] == iid
    assert (status(con, q1), status(con, q2)) == ("asked", "asked") and sorted(shown(con, CLEO)) == [q1, q2]
    stories.answer(con, CLEO, q1, no, text="Grandpa, in 1971")
    assert stories.dismiss(con, CLEO, q2) == {"question": q2, "dismissed": True}
    assert items(con, CLEO)[0][2] == "done" and shown(con, CLEO) == []
    assert (status(con, q1), status(con, q2)) == ("answered", "asked"), "a family question is not closed by one member"
    stories.answer(con, ADA, q2, no, text="The lake, 1980")
    assert status(con, q2) == "answered" and items(con, ADA)[0][2] == "done"

    q3 = ask(con, CLEO, text="And this?")
    stories.deal(con, CLEO, weeks(2))
    assert stories.dismiss(con, CLEO, q3)["dismissed"] and status(con, q3) == "closed"
    assert stories.deal(con, CLEO, weeks(4)) == 0 and shown(con, CLEO) == []
    told = stories.answer(con, CLEO, q3, no, text="Our first car")
    assert status(con, q3) == "answered" and row(con, q3, CLEO)[2:] == (1, 1), "the story stands, beside the put away"
    assert con.execute("SELECT text FROM stories WHERE id=?", (told["id"],)).fetchone()[0] == "Our first car"
    assert waiting_unseen(con) == []


def test_an_answer_told_while_the_question_is_put_away_stands(db, con):
    """answer reads the asked row before its transaction: Cleo answers in one tab while the other says "I don't know"
    in that gap. What the gap holds (the row's item, the photos) never changes, so it ends where the two one after the
    other do: the story is kept, the question answered, and nothing waits."""
    q1 = ask(con)
    stories.deal(con, CLEO)
    stories.answer(gap(db, lambda c: stories.dismiss(c, CLEO, q1)), CLEO, q1, no, text="Grandpa")
    assert status(con, q1) == "answered" and row(con, q1, CLEO)[1:] == ("done", 1, 1)
    assert con.execute("SELECT count(*) FROM stories WHERE question_id=?", (q1,)).fetchone()[0] == 1
    assert waiting_unseen(con) == []


def test_one_run_per_story_however_often_its_entries_are_handled(db, con):
    """OneRunPerStory: an answer to a question the agent asked with follow_up, typed and spoken (story.told with its
    text, then story.transcribed). The listener handles story.told and the app stops before its cursor moves: it is
    handled again, and then story.transcribed: one run, started by the first."""
    listeners.begin(con)
    q1 = ask(con, follow_up=True)
    stories.deal(con, CLEO)
    sid = stories.tell(con, CLEO, [PHOTO], no, text="Grandpa", recording=("ab" * 32, ".m4a"), question=q1)["id"]
    stories.transcribed(con, sid, text="it was grandpa")
    runs = []

    def submit(prompt, acting_for, by):
        runs.append(by)
        with con:
            return con.execute("INSERT INTO agent_tasks(prompt, status, created_at, acting_for, started_by) VALUES"
                               " (?, 'queued', '', ?, ?)", (prompt, acting_for, by)).lastrowid
    env = listeners.Env(find=lambda s, m: [], loaded=lambda: 10**9, in_catalog=lambda s: True, refused=no, submit=submit)
    told, spoken = [e for e in journal.since(con, listeners.cursor(con, "stories") or 0, 50)
                    if e["kind"] in ("story.told", "story.transcribed")]
    listeners.stories(con, told, env)             # handled; the app stops before listeners.run moves the cursor
    listeners.run(con, env)                       # it starts again at the cursor: story.told, then story.transcribed
    assert runs == [f"journal:{told['id']}/story:{sid}"], "once per story"
    assert listeners.stories(con, spoken, env) is None and len(runs) == 1


def test_a_questions_status_moves_only_as_QUESTION_MOVES_allows():
    """core.states.QUESTION_MOVES is the model's Moves (specs/Question.tla, whose property StatusMoves TLC checks: every
    change of questions.status the model makes is one of them), and check_move refuses any other."""
    text = (Path(__file__).resolve().parent.parent / "specs" / "Question.tla").read_text()
    block = text[text.index("Moves == {"):text.index("}", text.index("Moves == {"))]
    model = {(a, b) for a, b in re.findall(r'<<"(\w+)", "(\w+)">>', block) if a != "none"}
    assert model == {(a, b) for a, bs in QUESTION_MOVES.items() for b in bs}
    assert set(QUESTION_MOVES) == set(QuestionStatus)
    for a, b in (("answered", "closed"), ("closed", "asked"), ("queued", "answered"), ("answered", "asked")):
        with pytest.raises(IllegalMove):
            check_move(QUESTION_MOVES, a, b, "question")


# -- with nothing in between: an order the inbox page allows ----------------------------------------------------

@pytest.mark.parametrize("shape", ["hers", "the family's", "both in one item"])
def test_a_question_item_put_away_puts_its_questions_away(db, con, shape):
    """WaitsInAnOpenItem, PutAwayForGood, ClosedWhenPutAway: a question is dealt to Cleo (to her alone: TLC's trace; or
    to the family; or one of each, in one group). On the inbox page she presses Dismiss on its card (POST
    /api/inbox/<id>/dismiss, member_inbox.dismiss): the item is dismissed, and its questions are left waiting on her in
    it. Not shown again (_open asks for an open item), never dealt again (_for: she has a row), and hers never closed,
    where the module docstring says dismissing "its whole inbox item" puts each away for good."""
    qs = [ask(con, CLEO, "Trip")] if shape != "the family's" else []
    qs += [ask(con, None, "Trip")] if shape != "hers" else []
    stories.deal(con, CLEO)
    (iid, _, _), = items(con, CLEO)
    assert member_inbox.dismiss(con, CLEO, iid)["state"] == "dismissed"
    assert waiting_unseen(con) == [], "a question dealt to a member and still waiting on them is in an open item"
    assert all(row(con, q, CLEO)[3] for q in qs), "dismissing its whole inbox item puts each question away for good"
    assert [status(con, q) for q in qs] == [("closed" if q == qs[0] and shape != "the family's" else "asked") for q in qs], \
        "a question to her alone is closed"
    assert sorted(e["subject"] for e in journal.since(con, 0, 500) if e["kind"] == "question.dismissed") == \
        sorted(f"question:{q}" for q in qs)
    assert stories.deal(con, CLEO, weeks(2)) == 0 and shown(con, CLEO) == []
    if shape != "hers":                           # the family's is still Ada's to answer
        stories.deal(con, ADA)
        assert shown(con, ADA) == [qs[-1]]
