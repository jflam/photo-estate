"""Tasks: a request in words ("hide the photos from Carolyn's school"), worked on by an agent that runs
on the computer that holds the library, ending in a change set for a person to review (changes.py).

The agent is Claude Code run headless, once per task, with every built-in tool switched off: it can
do nothing on this computer (no shell, no files, so it cannot open a photo) except call the library's
own MCP tools (estate_mcp.py), which answer with ids, scores and facts. The model itself runs in the
cloud; what reaches it is the request, and what those tools return. It can propose a change set and
cannot apply one. A task outlives the chat or page that started it, and what the agent did is kept as
a list of events, so the work can be watched while it runs and read afterwards. Tasks run one at a time.

  GET  /api/tasks                 whether an agent can run here, and the member's own tasks, newest first
  GET  /api/tasks/<id>?after=N    one task, with its events after event N, and the change set it made (a task
                                  acting for someone else: 404, as one that does not exist: what its agent saw
                                  is what that member sees, their private photos too)
  POST /api/tasks      {prompt}   start one (it records the member who asked: acting_for)
  POST /api/tasks/<id>/cancel

The same over MCP for a chat client: submit_task, get_task, cancel_task.

The agent signs in with a long-lived token from `claude setup-token`, kept in a file only this user
can read (--agent-env, by default ~/.config/photo-estate/agent.env: a line CLAUDE_CODE_OAUTH_TOKEN=...).
It is put in the agent's environment and nowhere else: not in the catalog, not in a log. Without the
file the agent runs as whoever is signed in to Claude Code here, which is how it is tried on a laptop.

A task's status moves only as core.states.TASK_MOVES allows (queued -> running | cancelled, running ->
done | failed | cancelled), and every move is one conditional UPDATE (`move`: ... WHERE id=? AND
status=?), so whichever of the worker and a cancel gets there first wins and the other sees it lost:
the worker runs a task only if its claim (queued -> running) took, and a run's ending (running -> done |
failed) never overwrites a cancel. A cancel stops the agent with SIGTERM, then SIGKILL after `grace`
seconds (core.procs.stop), as the timeout does.

The journal (journal.py) gets task.submitted (the actor is who asked) and task.finished when a task reaches
done, failed or cancelled, in the transaction of that move: the actor is who cancelled it, or NULL for a
run's own end (the agent is not a member yet), acting for whoever asked. In that transaction too, each
hiding set the task's agent made (with photos, hiding none yet) becomes a `review` item in the inbox of
whoever asked (member_inbox.py), opening its review page; reviewing it there closes the item.

The agent acts for whoever asked (acting_for), and every tool answers as that member sees the library
(catalog.Live.visible_for); what it makes is theirs (estate_mcp.py).

The estate's agent is a member (multiplayer phase 7: members.agent, kind agent). A run is given a capability, not
a task number: run() mints a random token for *this agent, acting for this member (or no one), for this task*
(`mint`), puts it in the agent's MCP config as the X-Estate-Agent header, and forgets it when the run ends
(`forget`, in run()'s finally). The tokens are held in memory only, in `runs`: never in the catalog, a log or a
file, so a restart of the app ends every token (and Tasks() marks the run that was going failed). /mcp honours a
token only from this computer (estate_mcp.py), and X-Estate-Task, the number a run carried before, is refused.
Its journal entries name the agent as the actor, acting for its member.

Who may start a run (open question 4): any member, for themselves, or with `family` for the family archivist (the
agent acting for no one, which sees only photos private to nobody and whose albums are published series it
keeps). The estate starts runs itself too: listeners.py, when new photos match a family series, or when someone
answers a question an agent asked and wanted the answer to. Every run is a row here with `started_by`: member:<id>
for a person, journal:<entry>/<what> for a listener (also how a listener knows it has started that run already).
A run a person asked for to hide photos is told BRIEF; any other (a family request, or a listener's) WORK_BRIEF.
Who may read a task: the member it acts for; one acting for no one, whoever started it and the sysadmins.

  POST /api/tasks {prompt, family?: true}    family: a run of the family archivist (acting for no one)
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import journal
import member_inbox
import members
from core import schema
from core.procs import stop
from core.states import TASK_ACTIVE, TASK_MOVES, TaskStatus, check_move

SCHEMA = schema.TASKS
DEFAULT_ENV = Path.home() / ".config" / "photo-estate" / "agent.env"
SERVER = "photo-estate"
ROUTE = re.compile(r"^/api/tasks(?:/(\d+)(?:/(cancel))?)?$")

BRIEF = """You work inside Photo Estate, one family's photo library, on the computer that holds it. A person has asked for a change to the library, in their own words. Find the photos they mean and propose them as one change set for that person to review. Your only tools are the library's; you cannot see a photo, only ids, scores, dates, places, the names of people in it, albums and file paths.

How to work:
1. Start with library_overview: who is named, which years, places, albums.
2. Work out what the photos have in common: what they show, and the facts around them (who is or is not in them, where, when, which album or folder they came from).
3. Search with several plain descriptions of what such a photo shows. Read each summary. Page down the ranking with `offset` to where the matches thin out, and judge from dates, places, people, albums and file paths where they stop being the thing asked for. Choose a min_score for each description from that. Use filters (named_people, dates, place, album) to cut out what clearly does not belong.
4. The person will look over every photo you propose and can drop a wrong one with a click, but a photo you leave out they will never see. So at the margin, include. The exception is a photo with a named person in it: the family's own photos must not be swept up, so leave those out unless the request plainly covers them, and say how many you left out.
5. Create one change set. Give it a short title and, as its prompt, what was asked and how you chose. Add photos with amend_change_set, one add_search per description, each with a reason of a few words that a person can read at a glance ("children in a forest, nobody named"). Take out with remove_search what does not belong.
6. End with a short report in plain words: how many photos you proposed, the descriptions and thresholds you used, what you left out and why, what you are least sure of, and the review link.

Nothing you do hides a photo: the person applies the change set after looking. Nobody is there to answer a question, so do not ask one: make the reasonable choice and say what you assumed. If the request is not about choosing photos to hide, or cannot be done with these tools, say so and create nothing."""

WORK_BRIEF = """You are the estate's agent in Photo Estate, one family's photo library, on the computer that holds it. You were started either by a person asking for something for the whole family, or by the estate itself: a series has new photos that may belong in it, or someone has answered a question you asked. The request says which, with the ids you need. Your only tools are the library's; you cannot see a photo, only ids, scores, dates, places, the names of people in it, albums and the stories people have told.

Who you act for is fixed for this run: a member (you see what they see, and what you make is theirs), or no one, as the family archivist (you see only what the whole family may see, and an album you make is published as a family series that you keep up to date). Do what the request asks with the tools:
- A series to keep up to date: look at each new photo (get_photos; similar_photos to the series' own photos, get_album), add those that belong with amend_album and a short reason, and leave out the rest.
- An answer to your question: it is a story, what a person said, attributed to them. Never change or retell it; use it (an album, a section, a better question) if the request says what for.
- A family request: make the albums or series it asks for (create_album; `grow` keeps a series growing as photos arrive).
Only people know some things: who is in a photo, where, why. When a story would help, ask one person, or the family, with ask_member: a few plain questions about photos they can see, grouped by event (group_key), never many. Nobody answers during this run: the answer comes later.
End with a short report in plain words: what you did, and what you are least sure of. You cannot hide photos for anyone, or apply anything."""


@dataclass(frozen=True)
class Run:
    """What a run's token stands for: this agent (members.id), acting for this member (None: no one), for this task."""
    task: int
    acting_for: int | None
    agent: int


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_env(path: Path) -> dict:
    """KEY=VALUE lines of a file such as agent.env; {} if there is none."""
    out = {}
    try:
        for line in path.read_text().splitlines():
            k, sep, v = line.strip().partition("=")
            if sep and k and not k.startswith("#"):
                out[k.removeprefix("export ").strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    return out


def find_command(given: str | None) -> str | None:
    for c in (given, shutil.which("claude"), str(Path.home() / ".local" / "bin" / "claude")):
        if c and (shutil.which(c) or Path(c).is_file()):
            return c
    return None


class Tasks:
    def __init__(self, db: str, mcp_url: str, command: str | None = None, env_file: str | None = None,
                 model: str | None = None, timeout: float = 3600, max_turns: int = 80, log=print, grace: float = 10.0):
        self.db, self.mcp_url, self.model, self.timeout, self.max_turns, self.log = db, mcp_url, model, timeout, max_turns, log
        self.grace = grace       # seconds between SIGTERM and SIGKILL when an agent is stopped
        self.command, self.env_file = find_command(command), Path(env_file) if env_file else DEFAULT_ENV
        self.lock, self.wake, self.procs = threading.Lock(), threading.Event(), {}
        self.runs: dict[str, Run] = {}     # token -> the run it is for: in memory only, while the run lasts
        with closing(self.con()) as con:
            schema.ensure(con, "tasks", "changes", "members")   # changes: a task's change sets; members: the journal
            agent = members.agent(con).id          # the estate's agent: whom the journal names for what a run does
            assert agent is not None
            self.agent: int = agent
            # whatever was running when the app last stopped did not finish (running -> failed)
            why = "the app restarted while this was running"
            for tid, af in con.execute("SELECT id, acting_for FROM agent_tasks WHERE status='running'").fetchall():
                con.execute("UPDATE agent_tasks SET status='failed', error=?, finished_at=? WHERE id=?", (why, now(), tid))
                journal.append(con, None, "task.finished", f"task:{tid}", {"status": TaskStatus.FAILED, "error": why}, af)
                self.to_review(con, tid, af)
            con.commit()
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.worker, daemon=True, name="tasks worker")
        self.thread.start()

    def stop(self, wait: float = 5.0) -> None:
        """Let the worker end, waiting at most `wait` seconds (a task it is running finishes first): before the
        catalog it reads goes away, as the demo's scratch copy does at exit."""
        self.stopping.set()
        self.wake.set()
        self.thread.join(wait)

    def con(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db, timeout=60)
        con.execute("PRAGMA journal_mode=WAL")
        return con

    def available(self) -> tuple[bool, str]:
        if not self.command:
            return False, "Claude Code is not installed on this computer, so no agent can run here"
        return True, ""

    # -- reading ---------------------------------------------------------------------------------

    COLS = ("id", "prompt", "status", "created_by", "created_at", "started_at", "finished_at", "result", "error", "turns", "cost",
            "acting_for", "started_by")

    def task(self, con, tid: int, after: int | None = None) -> dict | None:
        row = con.execute(f"SELECT {', '.join(self.COLS)} FROM agent_tasks WHERE id=?", (tid,)).fetchone()
        if not row:
            return None
        t: dict[str, Any] = dict(zip(self.COLS, row))
        # the change set(s) the agent made, by the name it was given while it worked
        t["change_sets"] = [{"id": i, "title": title, "photos": n, "review": f"/changes/{i}"} for i, title, n in con.execute(
            "SELECT s.id, s.title, (SELECT count(*) FROM change_items WHERE set_id=s.id) FROM change_sets s"
            " WHERE s.created_by=? AND s.status IS NOT 'discarded' ORDER BY s.id", (f"task:{tid}",))]
        last = con.execute("SELECT id, kind, text FROM agent_events WHERE task_id=? ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
        t["last"] = {"id": last[0], "kind": last[1], "text": last[2]} if last else None
        if after is not None:
            t["events"] = [{"id": i, "at": at, "kind": kind, "text": text} for i, at, kind, text in con.execute(
                "SELECT id, at, kind, text FROM agent_events WHERE task_id=? AND id>? ORDER BY id", (tid, after))]
        return t

    ANYONE = object()      # get()/listing() for the app itself (the /mcp header's lookup), not for a member

    @staticmethod
    def whose(con, acting_for) -> int | None:
        """Whose a task is: who it acts for; one from before members (no acting_for) is the first sysadmin's."""
        return acting_for if acting_for is not None else (member_inbox.sysadmins(con) or [None])[0]

    @staticmethod
    def readers(con, acting_for, started_by) -> set[int | None]:
        """Who may read a task: the member it acts for. One acting for no one (the archivist's, or one from before
        members) saw only what is private to nobody: whoever started it, and the sysadmins."""
        if acting_for is not None:
            return {acting_for}
        out: set[int | None] = set(member_inbox.sysadmins(con))
        if isinstance(started_by, str) and started_by.startswith("member:") and started_by[7:].isdigit():
            out.add(int(started_by[7:]))
        return out

    def get(self, tid: int, after: int | None = 0, viewer=ANYONE) -> dict | None:
        """One task, or None: also for a member who may not read it (`viewer`, members.id; None: no members yet;
        readers). A task's events and report hold what its agent saw, which is what that member sees (their private
        photos too): nobody else's to read."""
        con = self.con()
        try:
            t = self.task(con, tid, after)
            if t and viewer is not self.ANYONE and viewer is not None and viewer not in self.readers(
                    con, t["acting_for"], t["started_by"]):
                return None
            return t
        finally:
            con.close()

    def listing(self, viewer=ANYONE) -> dict:
        """Whether an agent can run here, and the newest tasks (for a member: only those they may read)."""
        con = self.con()
        try:
            ok, why = self.available()
            ids = [i for i, af, by in con.execute("SELECT id, acting_for, started_by FROM agent_tasks ORDER BY id DESC")
                   if viewer is self.ANYONE or viewer is None or viewer in self.readers(con, af, by)][:50]
            return {"available": ok, "why": why, "tasks": [self.task(con, i) for i in ids]}
        finally:
            con.close()

    # -- writing ---------------------------------------------------------------------------------

    def event(self, tid: int, kind: str, text: str) -> None:
        con = self.con()
        try:
            con.execute("INSERT INTO agent_events(task_id, at, kind, text) VALUES (?,?,?,?)", (tid, now(), kind, text[:4000]))
            con.commit()
        finally:
            con.close()

    FIELDS = frozenset({"started_at", "finished_at", "result", "error", "turns", "cost"})   # what update() may write

    def update(self, tid: int, **fields) -> None:
        """Write some of a task's FIELDS (not its status: that is move()). ValueError for any other column."""
        if bad := set(fields) - self.FIELDS:
            raise ValueError(f"not a task field: {', '.join(sorted(bad))}")
        if not fields:
            return
        with closing(self.con()) as con, con:
            con.execute(f"UPDATE agent_tasks SET {', '.join(k + '=?' for k in fields)} WHERE id=?", (*fields.values(), tid))

    def move(self, tid: int, to: TaskStatus, frm: TaskStatus | frozenset[TaskStatus], actor: int | None = None,
             **fields) -> bool:
        """Move a task from `frm` (one status or a set) to `to`, with FIELDS beside it, in one conditional
        UPDATE. True if it moved; False if the task was not in `frm` by then (someone else got there first).
        A move to done, failed or cancelled appends task.finished (by `actor`) in the same transaction.
        IllegalMove if TASK_MOVES does not allow the step, ValueError for an unknown field."""
        frms = frozenset({frm}) if isinstance(frm, str) else frm
        for f in frms:
            check_move(TASK_MOVES, f, to, f"task {tid}")
        if bad := set(fields) - self.FIELDS:
            raise ValueError(f"not a task field: {', '.join(sorted(bad))}")
        sets = ", ".join(["status=?", *(k + "=?" for k in fields)])
        with closing(self.con()) as con, con:
            cur = con.execute(f"UPDATE agent_tasks SET {sets} WHERE id=? AND status IN ({', '.join('?' * len(frms))})",
                              (to, *fields.values(), tid, *sorted(frms)))
            if cur.rowcount == 1 and to not in TASK_ACTIVE:
                af = con.execute("SELECT acting_for FROM agent_tasks WHERE id=?", (tid,)).fetchone()[0]
                journal.append(con, actor, "task.finished", f"task:{tid}",
                               {"status": to, **({"error": fields["error"]} if fields.get("error") else {})}, af)
                self.to_review(con, tid, af)
            return cur.rowcount == 1

    @staticmethod
    def to_review(con, tid: int, acting_for: int | None) -> None:
        """A task has ended: each hiding set its agent made that has photos and hides none yet is waiting for
        the member it acted for to look it over (member_inbox.py: a `review` item, opening /changes/<id>), in
        the transaction of the task's last move. A task from before members acted for the first sysadmin."""
        who = acting_for if acting_for is not None else (member_inbox.sysadmins(con) or [None])[0]
        for sid, title, n in con.execute(
                "SELECT s.id, s.title, (SELECT count(*) FROM change_items WHERE set_id=s.id) FROM change_sets s"
                " WHERE s.created_by=? AND s.op='hide' AND s.status IS NOT 'discarded'"
                " AND NOT EXISTS (SELECT 1 FROM change_items WHERE set_id=s.id AND applied=1) ORDER BY s.id",
                (f"task:{tid}",)).fetchall():
            if n:
                member_inbox.put(con, [who], "review", f"album:{sid}",
                                 f"“{title}”: {n} photo{'' if n == 1 else 's'} the agent proposes to hide, for you to look over")

    def submit(self, prompt, who: str = "", acting_for: int | None = None, family: bool = False,
               started_by: str | None = None) -> tuple[dict, int]:
        """A task for the agent. From a person: `acting_for` is the member (members.id) who asked for it, and the run
        acts for them; with `family`, for no one (the family archivist), started by them. From a listener:
        `started_by` journal:<entry>/<what>, acting for `acting_for` (None: no one)."""
        prompt = str(prompt or "").strip()
        if not prompt:
            return {"error": "say what you want done"}, 400
        ok, why = self.available()
        if not ok:
            return {"error": why}, 409
        asker = None if started_by else acting_for
        by = started_by or (f"member:{asker}" if asker is not None else None)
        runs_for = None if family else acting_for
        con = self.con()
        try:
            tid = con.execute("INSERT INTO agent_tasks(prompt, created_by, created_at, acting_for, started_by) VALUES (?,?,?,?,?)",
                              (prompt[:8000], who or None, now(), runs_for, by)).lastrowid
            assert tid is not None
            journal.append(con, asker, "task.submitted", f"task:{tid}",
                           {"prompt": prompt[:500], **({"started_by": by} if started_by or family else {})},
                           runs_for if runs_for != asker else None)
            con.commit()
            out = self.task(con, tid)
            assert out is not None   # just written
        finally:
            con.close()
        self.wake.set()
        return out, 201

    def cancel(self, tid: int, actor: int | None = None) -> tuple[dict, int]:
        """`actor`: the member who cancelled it (members.id), for the journal. Any member may stop a run (it spends
        the owner's subscription); what it saw is told only to whom it acts for (get)."""
        t = self.get(tid, None)
        if not t:
            return {"error": "no such task"}, 404
        with self.lock:      # beside run()'s check before it starts the agent: one or the other goes first
            moved = self.move(tid, TaskStatus.CANCELLED, TASK_ACTIVE, actor, finished_at=now())
            proc = self.procs.get(tid) if moved else None
        if proc:             # SIGTERM, then SIGKILL after the grace: not in this request's thread
            threading.Thread(target=stop, args=(proc, self.grace), daemon=True, name=f"stop task {tid}").start()
        got = self.get(tid, None) or t
        if actor is not None and self.get(tid, None, actor) is None:      # someone else's: only how it stands
            got = {k: got[k] for k in ("id", "status", "created_at", "finished_at") if k in got}
        return got, 200

    def cancel_for(self, member: int, actor: int | None = None) -> list[int]:
        """A member was revoked: every task acting for them that is queued or running is cancelled (a running one's
        agent is stopped), by `actor`. The ids cancelled. (Issue #131.)"""
        with closing(self.con()) as con:
            ids = [i for i, in con.execute(
                f"SELECT id FROM agent_tasks WHERE acting_for=? AND status IN ({', '.join('?' * len(TASK_ACTIVE))}) ORDER BY id",
                (member, *sorted(TASK_ACTIVE)))]
        return [i for i in ids if self.cancel(i, actor)[0].get("status") == TaskStatus.CANCELLED]

    def lets(self, acting_for: int | None) -> bool:
        """A run may act for this member: no one (the family archivist), or a member who is active now."""
        if acting_for is None:
            return True
        with closing(self.con()) as con:
            m = members.get(con, acting_for)
        return m is not None and m.active

    # -- running ---------------------------------------------------------------------------------

    def worker(self) -> None:
        while not self.stopping.is_set():
            with closing(self.con()) as con:
                row = con.execute("SELECT id, prompt, acting_for FROM agent_tasks WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if not row:
                self.wake.wait(30)
                self.wake.clear()
                continue
            *row, acting_for = row
            if not self.lets(acting_for):      # queued for a member revoked since (a listener's, or a revoke that lost a race)
                self.move(row[0], TaskStatus.CANCELLED, TaskStatus.QUEUED, finished_at=now())
                continue
            # the claim: only if it is still queued (a cancel may have landed since the SELECT)
            if not self.move(row[0], TaskStatus.RUNNING, TaskStatus.QUEUED, started_at=now()):
                continue
            try:
                self.run(*row)
            except Exception as e:   # the next task still gets its turn
                self.move(row[0], TaskStatus.FAILED, TaskStatus.RUNNING, error=f"{type(e).__name__}: {e}", finished_at=now())
                self.log(f"task {row[0]} failed: {type(e).__name__}: {e}")

    # -- a run's token -------------------------------------------------------------------------

    def mint(self, tid: int) -> str:
        """A new token for a run of task `tid`: this agent, acting for the task's member, for this task (Run)."""
        with closing(self.con()) as con:
            row = con.execute("SELECT acting_for FROM agent_tasks WHERE id=?", (tid,)).fetchone()
        if row is None:
            raise ValueError(f"no task {tid}")
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.runs[token] = Run(tid, row[0], self.agent)
        return token

    def forget(self, token: str) -> None:
        with self.lock:
            self.runs.pop(token, None)

    def run_of(self, token: str | None) -> Run | None:
        """The run a token is for, while it lasts; None for any other string."""
        if not token:
            return None
        with self.lock:
            return self.runs.get(token)

    # -- running, continued ----------------------------------------------------------------------

    def brief(self, tid: int) -> str:
        """BRIEF for a person's request to hide photos; WORK_BRIEF for a family request or a listener's run."""
        with closing(self.con()) as con:
            row = con.execute("SELECT acting_for, started_by FROM agent_tasks WHERE id=?", (tid,)).fetchone()
        af, by = row if row else (None, None)
        return WORK_BRIEF if (by or "").startswith("journal:") or (af is None and (by or "").startswith("member:")) else BRIEF

    def run(self, tid: int, prompt: str) -> None:
        """Run a task the worker has claimed (it is `running`) to its end, with a token that lasts as long."""
        if not self.command:     # submit() takes no task then (available())
            raise RuntimeError("Claude Code is not installed on this computer")
        token = self.mint(tid)
        try:
            self._run(tid, prompt, token)
        finally:
            self.forget(token)

    def _run(self, tid: int, prompt: str, token: str) -> None:
        assert self.command is not None
        config = {"mcpServers": {SERVER: {"type": "http", "url": self.mcp_url, "headers": {"X-Estate-Agent": token}}}}
        cmd = [self.command, "-p", prompt, "--system-prompt", self.brief(tid), "--mcp-config", json.dumps(config), "--strict-mcp-config",
               "--tools", "", "--allowedTools", f"mcp__{SERVER}", "--output-format", "stream-json", "--verbose",
               "--no-session-persistence", "--max-turns", str(self.max_turns)] + (["--model", self.model] if self.model else [])
        env = {**os.environ, **read_env(self.env_file)}
        env["PATH"] = os.pathsep.join(dict.fromkeys([str(Path(self.command).parent), *env.get("PATH", "").split(os.pathsep)]))
        work = tempfile.mkdtemp(prefix="photo-estate-task-")   # an empty folder: nothing of ours to read
        self.log(f"task {tid}: started")
        try:
            with self.lock:      # a cancel that came first means the agent never starts; one after finds it in procs
                if (self.get(tid, None) or {}).get("status") != TaskStatus.RUNNING:
                    return self.log(f"task {tid}: cancelled")
                proc = subprocess.Popen(cmd, cwd=work, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True)
                self.procs[tid] = proc
            assert proc.stdout is not None and proc.stderr is not None   # both PIPE
            stdout, stderr = proc.stdout, proc.stderr
            timer = threading.Timer(self.timeout, stop, (proc, self.grace))   # SIGTERM, then SIGKILL
            timer.daemon = True
            timer.start()
            errs: list[str] = []
            reader = threading.Thread(target=self._drain, args=(stderr, errs), daemon=True)
            reader.start()
            final = None
            for line in stdout:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                final = self.record(tid, msg) or final
            code = proc.wait()
            timer.cancel()
            reader.join(self.grace)  # all of stderr, to say why it stopped (bounded: a grandchild may hold it open)
        finally:
            with self.lock:
                self.procs.pop(tid, None)
            shutil.rmtree(work, ignore_errors=True)
        # the ending is one more move from running: a cancel that got there first stays
        if final and not final.get("is_error"):
            moved = self.move(tid, TaskStatus.DONE, TaskStatus.RUNNING, result=final.get("result"), turns=final.get("num_turns"),
                              cost=final.get("total_cost_usd"), finished_at=now())
        else:
            why = (final or {}).get("result") or "".join(errs)[-600:].strip() or f"the agent stopped (exit {code})"
            if final and final.get("subtype") == "error_max_turns":
                why = f"the agent did not finish in {self.max_turns} steps"
            moved = self.move(tid, TaskStatus.FAILED, TaskStatus.RUNNING, error=why, turns=(final or {}).get("num_turns"),
                              cost=(final or {}).get("total_cost_usd"), finished_at=now())
        self.log(f"task {tid}: {(self.get(tid, None) or {}).get('status') if moved else 'cancelled'}")

    @staticmethod
    def _drain(stream, into: list[str]) -> None:
        into.extend(stream)

    def record(self, tid: int, msg: dict) -> dict | None:
        """Keep what a line of the agent's output says it did. Returns the final result message, when this is it."""
        kind = msg.get("type")
        if kind == "system" and msg.get("subtype") == "init":
            down = [s.get("name") for s in msg.get("mcp_servers") or [] if s.get("status") != "connected"]
            if down:
                self.event(tid, "error", "The agent could not reach the library's tools")
        elif kind == "assistant":
            for block in (msg.get("message") or {}).get("content") or []:
                if block.get("type") == "text" and block.get("text", "").strip():
                    self.event(tid, "say", block["text"].strip())
                elif block.get("type") == "tool_use":
                    args = json.dumps(block.get("input") or {}, separators=(",", ":"))
                    self.event(tid, "tool", f"{str(block.get('name')).removeprefix(f'mcp__{SERVER}__')} {args[:600]}")
        elif kind == "user":
            for block in (msg.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                    content = block.get("content")
                    text = content if isinstance(content, str) else " ".join(
                        c.get("text", "") for c in content or [] if isinstance(c, dict))
                    self.event(tid, "error", text[:600])
        elif kind == "result":
            return msg
        return None

    # -- http ------------------------------------------------------------------------------------

    def handle(self, method: str, path: str, qs: dict, body: dict | None, who: str = "", acting_for: int | None = None):
        """One request -> (status, body bytes, content type, Cache-Control), or None if it isn't ours.
        `acting_for`: the member the request is for (members.id), who submits or cancels."""
        js = lambda o, code=200: (code, json.dumps(o, separators=(",", ":")).encode(), "application/json", "no-store")
        m = ROUTE.match(path)
        if not m:
            return None
        tid, what = int(m[1]) if m[1] else None, m[2]
        if method == "GET" and not what:
            if tid is None:
                return js(self.listing(acting_for))
            after = (qs.get("after") or ["0"])[0]
            got = self.get(tid, int(after) if after.isdigit() else 0, acting_for)
            return js(got) if got else js({"error": "no such task"}, 404)
        if method == "POST" and isinstance(body, dict):
            if tid is None:
                return js(*self.submit(body.get("prompt"), who, acting_for, family=body.get("family") is True))
            if what == "cancel":
                return js(*self.cancel(tid, acting_for))
        return js({"error": "not found"}, 404)
