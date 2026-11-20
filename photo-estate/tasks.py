"""Tasks: a request in words ("hide the photos from Carolyn's school"), worked on by an agent that runs
on the computer that holds the library, ending in a change set for a person to review (changes.py).

The agent is Claude Code run headless, once per task, with every built-in tool switched off: it can
do nothing on this computer (no shell, no files, so it cannot open a photo) except call the library's
own MCP tools (estate_mcp.py), which answer with ids, scores and facts. The model itself runs in the
cloud; what reaches it is the request, and what those tools return. It can propose a change set and
cannot apply one. A task outlives the chat or page that started it, and what the agent did is kept as
a list of events, so the work can be watched while it runs and read afterwards. Tasks run one at a time.

  GET  /api/tasks                 whether an agent can run here, and every task, newest first
  GET  /api/tasks/<id>?after=N    one task, with its events after event N, and the change set it made
  POST /api/tasks      {prompt}   start one
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
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
        with closing(self.con()) as con:
            schema.ensure(con, "tasks", "changes")   # changes: a task's change sets are read from change_sets
            # whatever was running when the app last stopped did not finish (running -> failed)
            con.execute("UPDATE agent_tasks SET status='failed', error='the app restarted while this was running', finished_at=?"
                        " WHERE status='running'", (now(),))
            con.commit()
        threading.Thread(target=self.worker, daemon=True).start()

    def con(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db, timeout=60)
        con.execute("PRAGMA journal_mode=WAL")
        return con

    def available(self) -> tuple[bool, str]:
        if not self.command:
            return False, "Claude Code is not installed on this computer, so no agent can run here"
        return True, ""

    # -- reading ---------------------------------------------------------------------------------

    COLS = ("id", "prompt", "status", "created_by", "created_at", "started_at", "finished_at", "result", "error", "turns", "cost")

    def task(self, con, tid: int, after: int | None = None) -> dict | None:
        row = con.execute(f"SELECT {', '.join(self.COLS)} FROM agent_tasks WHERE id=?", (tid,)).fetchone()
        if not row:
            return None
        t: dict[str, Any] = dict(zip(self.COLS, row))
        # the change set(s) the agent made, by the name it was given while it worked
        t["change_sets"] = [{"id": i, "title": title, "photos": n, "review": f"/changes/{i}"} for i, title, n in con.execute(
            "SELECT s.id, s.title, (SELECT count(*) FROM change_items WHERE set_id=s.id) FROM change_sets s"
            " WHERE s.created_by=? ORDER BY s.id", (f"task:{tid}",))]
        last = con.execute("SELECT id, kind, text FROM agent_events WHERE task_id=? ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
        t["last"] = {"id": last[0], "kind": last[1], "text": last[2]} if last else None
        if after is not None:
            t["events"] = [{"id": i, "at": at, "kind": kind, "text": text} for i, at, kind, text in con.execute(
                "SELECT id, at, kind, text FROM agent_events WHERE task_id=? AND id>? ORDER BY id", (tid, after))]
        return t

    def get(self, tid: int, after: int | None = 0) -> dict | None:
        con = self.con()
        try:
            return self.task(con, tid, after)
        finally:
            con.close()

    def listing(self) -> dict:
        con = self.con()
        try:
            ok, why = self.available()
            ids = [r[0] for r in con.execute("SELECT id FROM agent_tasks ORDER BY id DESC LIMIT 50")]
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

    def move(self, tid: int, to: TaskStatus, frm: TaskStatus | frozenset[TaskStatus], **fields) -> bool:
        """Move a task from `frm` (one status or a set) to `to`, with FIELDS beside it, in one conditional
        UPDATE. True if it moved; False if the task was not in `frm` by then (someone else got there first).
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
            return cur.rowcount == 1

    def submit(self, prompt, who: str = "") -> tuple[dict, int]:
        prompt = str(prompt or "").strip()
        if not prompt:
            return {"error": "say what you want done"}, 400
        ok, why = self.available()
        if not ok:
            return {"error": why}, 409
        con = self.con()
        try:
            tid = con.execute("INSERT INTO agent_tasks(prompt, created_by, created_at) VALUES (?,?,?)",
                              (prompt[:8000], who or None, now())).lastrowid
            con.commit()
            assert tid is not None
            out = self.task(con, tid)
            assert out is not None   # just written
        finally:
            con.close()
        self.wake.set()
        return out, 201

    def cancel(self, tid: int) -> tuple[dict, int]:
        t = self.get(tid, None)
        if not t:
            return {"error": "no such task"}, 404
        with self.lock:      # beside run()'s check before it starts the agent: one or the other goes first
            moved = self.move(tid, TaskStatus.CANCELLED, TASK_ACTIVE, finished_at=now())
            proc = self.procs.get(tid) if moved else None
        if proc:             # SIGTERM, then SIGKILL after the grace: not in this request's thread
            threading.Thread(target=stop, args=(proc, self.grace), daemon=True, name=f"stop task {tid}").start()
        return self.get(tid, None) or t, 200

    # -- running ---------------------------------------------------------------------------------

    def worker(self) -> None:
        while True:
            with closing(self.con()) as con:
                row = con.execute("SELECT id, prompt FROM agent_tasks WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if not row:
                self.wake.wait(30)
                self.wake.clear()
                continue
            # the claim: only if it is still queued (a cancel may have landed since the SELECT)
            if not self.move(row[0], TaskStatus.RUNNING, TaskStatus.QUEUED, started_at=now()):
                continue
            try:
                self.run(*row)
            except Exception as e:   # the next task still gets its turn
                self.move(row[0], TaskStatus.FAILED, TaskStatus.RUNNING, error=f"{type(e).__name__}: {e}", finished_at=now())
                self.log(f"task {row[0]} failed: {type(e).__name__}: {e}")

    def run(self, tid: int, prompt: str) -> None:
        """Run a task the worker has claimed (it is `running`) to its end."""
        if not self.command:     # submit() takes no task then (available())
            raise RuntimeError("Claude Code is not installed on this computer")
        config = {"mcpServers": {SERVER: {"type": "http", "url": self.mcp_url, "headers": {"X-Estate-Task": str(tid)}}}}
        cmd = [self.command, "-p", prompt, "--system-prompt", BRIEF, "--mcp-config", json.dumps(config), "--strict-mcp-config",
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

    def handle(self, method: str, path: str, qs: dict, body: dict | None, who: str = ""):
        """One request -> (status, body bytes, content type, Cache-Control), or None if it isn't ours."""
        js = lambda o, code=200: (code, json.dumps(o, separators=(",", ":")).encode(), "application/json", "no-store")
        m = ROUTE.match(path)
        if not m:
            return None
        tid, what = int(m[1]) if m[1] else None, m[2]
        if method == "GET" and not what:
            if tid is None:
                return js(self.listing())
            after = (qs.get("after") or ["0"])[0]
            got = self.get(tid, int(after) if after.isdigit() else 0)
            return js(got) if got else js({"error": "no such task"}, 404)
        if method == "POST" and isinstance(body, dict):
            if tid is None:
                return js(*self.submit(body.get("prompt"), who))
            if what == "cancel":
                return js(*self.cancel(tid))
        return js({"error": "not found"}, 404)
