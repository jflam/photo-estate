"""The background jobs' lifecycles (issue #86): upload.IngestJob, tasks.Tasks, intake.engine.Engine and the
sync feed's deletion cursor. Each test pins one race or failure down without timing: hooks, Events, a
connection factory that runs code at the exact statement a race needs, and small child processes (one that
ignores SIGTERM, one whose grandchild holds stderr open until it is told to finish).

  IngestJob   a step not in MOVES raises; start() is guarded by the state and cancels an armed timer;
              status() copying the log while _follow appends a line is safe
  Tasks       a cancel between the worker's SELECT and its claim, or between the claim and the agent's
              start, means the agent never runs; a cancel as a run completes stays cancelled; stderr is
              read to the end; a timeout escalates SIGTERM to SIGKILL; update() takes only known columns
  Engine      a job cancelled while queued is never claimed; JobState moves are checked; a running job's
              cancel kills a stage that ignores SIGTERM
  Feed        each deletion reaches a device once: none for one that is up to date, once over many pages,
              all of them for one that is behind
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tasks  # noqa: E402
from core.lifecycle import IllegalMove  # noqa: E402
from core.states import IngestState, JobState, TaskStatus  # noqa: E402
from intake import sources  # noqa: E402,F401  (registers every source)
from intake.engine import STAGE_IDS, CatalogLock, Engine  # noqa: E402
from sync import Feed  # noqa: E402
from upload import IngestJob  # noqa: E402

BOUND = 30      # seconds: only how long a broken build may hang before the test says so


@pytest.fixture
def term_ignored():
    """Children started inside this fixture begin with SIGTERM ignored (an ignored signal survives exec),
    so there is no moment in which a SIGTERM could still kill them."""
    old = signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, old)


def kill_pid_in(path: Path) -> None:
    try:
        os.kill(int(path.read_text()), signal.SIGKILL)
    except (OSError, ValueError):
        pass


# ----------------------------------------------------------------------------------------- IngestJob

def ingest_job(tmp_path: Path, script: str = "echo hi", **kw) -> IngestJob:
    sh = tmp_path / "ingest.sh"
    sh.write_text(f"#!/bin/sh\n{script}\n")
    sh.chmod(0o755)
    return IngestJob(sh, tmp_path, str(tmp_path / "estate.db"), tmp_path / "store", tmp_path / "inbox", **kw)


def test_ingest_illegal_move_raises(tmp_path):
    job = ingest_job(tmp_path)
    with job.lock:
        with pytest.raises(IllegalMove):
            job._to(IngestState.STARTING)            # idle -> starting: only the timer running out does that
        assert job.state == IngestState.IDLE, "a refused step leaves the state as it was"
        job._to(IngestState.QUIET)
        job._to(IngestState.STARTING)
        with pytest.raises(IllegalMove):
            job._to(IngestState.BACKOFF)


def test_ingest_start_is_guarded_by_state(tmp_path):
    job = ingest_job(tmp_path, f"touch {tmp_path / 'ran'}")
    with job.lock:
        job.state = IngestState.RUNNING              # a run whose output is still being followed (proc may be done)
    assert job.start() is False and job.proc is None and not (tmp_path / "ran").exists()


def test_ingest_start_cancels_an_armed_timer(tmp_path):
    job = ingest_job(tmp_path, "echo '== 1 new assets and sidecars: link'", quiet=30, max_wait=60)
    ended = threading.Event()
    job.on_faces = ended.set                          # called once _follow has finished with the run
    job.poke()
    armed = job.timer
    assert job.state == IngestState.QUIET and armed is not None and armed.is_alive()
    try:
        assert job.start() is True
        assert job.timer is None and armed.finished.is_set(), "the run picks up the uploads; the timer is cancelled"
        assert ended.wait(BOUND)
        assert job.state == IngestState.IDLE and job.timer is None and job.status()["exit_code"] == 0
    finally:
        armed.cancel()
    with job.lock:
        job.state = IngestState.QUIET
    job._fire()                                       # called from a thread that is not the armed timer
    assert job.state == IngestState.QUIET, "a timer that is no longer the armed one does nothing"


class WatchedLock:
    """A lock that says when someone has to wait for it."""
    def __init__(self, contended: threading.Event):
        self.lock, self.contended = threading.Lock(), contended

    def __enter__(self):
        if not self.lock.acquire(blocking=False):
            self.contended.set()
            self.lock.acquire()
        return self

    def __exit__(self, *exc):
        self.lock.release()


def test_ingest_log_append_and_status_are_safe(tmp_path):
    """status() is part-way through copying the log when _follow appends a line. The copy pauses after its
    first line until the append has either happened (no lock: the copy then fails, "deque mutated during
    iteration") or is waiting for the lock (fixed: the copy finishes, then the line goes in)."""
    import collections
    job = ingest_job(tmp_path)
    copying, moved = threading.Event(), threading.Event()

    class PausingLog(collections.deque):
        def __iter__(self):
            for i, line in enumerate(super().__iter__()):
                if i == 0:
                    copying.set()
                    assert moved.wait(BOUND)
                yield line

        def append(self, x):
            super().append(x)
            moved.set()

    job.log = PausingLog(["first"], maxlen=300)
    job.lock = WatchedLock(moved)  # type: ignore[assignment]
    with job.lock:
        job.state = IngestState.RUNNING
    feed: list[str] = []
    line_ready = threading.Event()

    class Proc:
        @property
        def stdout(self):
            line_ready.wait(BOUND)
            yield from feed

        def wait(self):
            return 0

    follower = threading.Thread(target=job._follow, args=(Proc(),), daemon=True)
    follower.start()
    got: dict = {}

    def status():
        try:
            got["status"] = job.status()
        except Exception as e:
            got["error"] = e

    reader = threading.Thread(target=status, daemon=True)
    reader.start()
    assert copying.wait(BOUND)
    feed.append("second\n")
    line_ready.set()
    reader.join(BOUND)
    follower.join(BOUND)
    assert "error" not in got, got.get("error")
    assert got["status"]["log"] == ["first"] and list(job.log) == ["first", "second"]
    assert job.state == IngestState.IDLE


# ----------------------------------------------------------------------------------------- Tasks

FAKE = r'''#!{python}
import json, os, signal, subprocess, sys, time
argv = sys.argv[1:]
prompt = argv[argv.index("-p") + 1]
out = lambda o: print(json.dumps(o), flush=True)
result = lambda: out({{"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "result": "finished"}})
kind, _, arg = prompt.partition(":")
if kind == "marker":                       # the agent ran: leaves a mark, finishes
    open(arg, "w").write("ran")
    result()
elif kind == "done":
    result()
elif kind == "term-success":               # stopped, it still ends as if it had finished
    signal.signal(signal.SIGTERM, lambda *_: (result(), sys.exit(0)))
    out({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": "ready"}}]}}}})
    while True:
        time.sleep(60)
elif kind == "ignore":                     # started with SIGTERM ignored: only SIGKILL stops it
    open(arg, "w").write(str(os.getpid()))
    out({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": "ready"}}]}}}})
    while True:
        time.sleep(60)
elif kind == "stderr":                     # exits at once; a grandchild holds stderr open until released
    subprocess.Popen([sys.executable, "-c", "import sys; open(sys.argv[1]).read(); "
                      "sys.stderr.write('the real reason\\n')", arg], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL)
    sys.stderr.write("first line\n")
    sys.exit(1)
'''


def make_tasks(tmp_path: Path, monkeypatch, worker: bool = False, **kw) -> tasks.Tasks:
    fake = tmp_path / "claude"
    fake.write_text(FAKE.format(python=sys.executable))
    fake.chmod(0o755)
    if not worker:
        monkeypatch.setattr(tasks.Tasks, "worker", lambda self: None)
    return tasks.Tasks(str(tmp_path / "estate.db"), "http://127.0.0.1:9/mcp", command=str(fake),
                       env_file=str(tmp_path / "none.env"), **kw)


def hook_connections(monkeypatch, before=None, after=None):
    """Every connection Tasks opens runs `before(sql, params)` ahead of each statement and may have its
    result replaced by `after(sql, params, cursor)`."""
    class Con(sqlite3.Connection):
        def execute(self, sql, params=(), /):  # type: ignore[override]
            if before:
                before(sql, params)
            cur = super().execute(sql, params)
            return (after(sql, params, cur) if after else None) or cur

    def con(self):
        c = sqlite3.connect(self.db, timeout=60, factory=Con)
        c.execute("PRAGMA journal_mode=WAL")
        return c
    monkeypatch.setattr(tasks.Tasks, "con", con)


def claim(t: tasks.Tasks, tid: int) -> None:
    """What the worker's claim does, written directly (the tests call run() themselves)."""
    con = sqlite3.connect(t.db)
    con.execute("UPDATE agent_tasks SET status='running', started_at='now' WHERE id=? AND status='queued'", (tid,))
    con.commit()
    con.close()


def submit(t: tasks.Tasks, prompt: str) -> int:
    got, code = t.submit(prompt)
    assert code == 201, got
    return got["id"]


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


def test_task_cancelled_between_select_and_claim_never_runs(tmp_path, monkeypatch):
    marker = tmp_path / "ran"
    looped, fired = threading.Event(), []
    box: dict = {}
    pick = "SELECT id, prompt FROM agent_tasks WHERE status='queued'"

    def after(sql, params, cur):
        if not sql.startswith(pick):
            return None
        rows = cur.fetchall()
        if rows and not fired:                    # the worker has chosen this task: a cancel lands now
            fired.append(rows[0][0])
            box["t"].cancel(rows[0][0])
        elif fired:
            looped.set()                          # the worker has come round again
        return Rows(rows)

    hook_connections(monkeypatch, after=after)
    t = box["t"] = make_tasks(tmp_path, monkeypatch, worker=True)
    tid = submit(t, f"marker:{marker}")
    assert looped.wait(BOUND)
    got = t.get(tid, None)
    assert fired == [tid] and got is not None
    assert got["status"] == TaskStatus.CANCELLED and not got["started_at"], got
    assert not marker.exists(), "the agent never started"


def test_task_cancelled_after_claim_before_start_never_runs(tmp_path, monkeypatch):
    marker = tmp_path / "ran"
    t = make_tasks(tmp_path, monkeypatch)
    tid = submit(t, f"marker:{marker}")
    real = tasks.read_env
    monkeypatch.setattr(tasks, "read_env", lambda p: (t.cancel(tid), real(p))[1])   # just before the agent starts
    claim(t, tid)
    t.run(tid, f"marker:{marker}")
    assert (t.get(tid, None) or {})["status"] == TaskStatus.CANCELLED and not marker.exists()


def test_task_cancel_as_it_completes_stays_cancelled(tmp_path, monkeypatch):
    """The agent has finished; the cancel lands just before the run writes `done`."""
    box: dict = {}

    def before(sql, params):
        if sql.startswith("UPDATE agent_tasks SET status") and TaskStatus.DONE in params and not box.get("fired"):
            box["fired"] = True
            box["t"].cancel(box["tid"])

    hook_connections(monkeypatch, before=before)
    t = box["t"] = make_tasks(tmp_path, monkeypatch)
    tid = box["tid"] = submit(t, "done")
    claim(t, tid)
    t.run(tid, "done")
    got = t.get(tid, None) or {}
    assert box.get("fired") and got["status"] == TaskStatus.CANCELLED and got["result"] is None, got


def test_task_cancel_while_running_stops_it_and_stays_cancelled(tmp_path, monkeypatch):
    """Stopped, the agent still prints a successful result and exits 0: the task stays cancelled."""
    t = make_tasks(tmp_path, monkeypatch)
    t.grace = BOUND
    tid = submit(t, "term-success")
    real = t.record
    monkeypatch.setattr(t, "record", lambda i, msg: (t.cancel(i) if msg.get("type") == "assistant" else None, real(i, msg))[1])
    claim(t, tid)
    t.run(tid, "term-success")
    got = t.get(tid, None) or {}
    assert got["status"] == TaskStatus.CANCELLED and got["result"] is None, got


def test_task_stderr_is_read_to_the_end(tmp_path, monkeypatch):
    """The agent exits at once, but its stderr stays open (a grandchild holds it) until the run joins its
    reader: only a run that waits for the reader has the last line, which says why it stopped."""
    fifo = tmp_path / "release"
    os.mkfifo(fifo)
    released = []

    def release():
        if not released:
            released.append(1)
            with open(fifo, "w"):
                pass

    class Joined(threading.Thread):
        def join(self, timeout=None):
            release()
            super().join(timeout)

    t = make_tasks(tmp_path, monkeypatch)
    t.grace = BOUND
    tid = submit(t, f"stderr:{fifo}")
    claim(t, tid)
    monkeypatch.setattr(threading, "Thread", Joined)
    try:
        t.run(tid, f"stderr:{fifo}")
    finally:
        monkeypatch.undo()
        threading.Thread(target=release, daemon=True).start()   # a broken build never released it
    got = t.get(tid, None) or {}
    assert got["status"] == TaskStatus.FAILED and got["error"] == "first line\nthe real reason", got


def test_task_timeout_kills_an_agent_that_ignores_sigterm(tmp_path, monkeypatch, term_ignored):
    pid = tmp_path / "pid"
    t = make_tasks(tmp_path, monkeypatch, timeout=0.2)
    t.grace = 0.2
    tid = submit(t, f"ignore:{pid}")
    claim(t, tid)
    runner = threading.Thread(target=t.run, args=(tid, f"ignore:{pid}"), daemon=True)
    runner.start()
    runner.join(BOUND)
    if runner.is_alive():
        kill_pid_in(pid)
        pytest.fail("SIGTERM was ignored and nothing followed it")
    got = t.get(tid, None) or {}
    assert got["status"] == TaskStatus.FAILED and f"exit -{int(signal.SIGKILL)}" in got["error"], got


def test_stop_escalates_to_kill(term_ignored):
    from core.procs import stop
    p = subprocess.Popen([sys.executable, "-c", "import time; print('up', flush=True); time.sleep(600)"],
                         stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout is not None and p.stdout.readline() == "up\n"
        assert stop(p, grace=0.2) == -signal.SIGKILL
        assert stop(p, grace=0.2) == -signal.SIGKILL, "stopping one that has gone is a no-op"
    finally:
        p.kill()
        p.wait()


def test_task_update_rejects_unknown_columns(tmp_path, monkeypatch):
    t = make_tasks(tmp_path, monkeypatch)
    tid = submit(t, "done")
    for bad in ({"bogus": 1}, {"status": "done"}, {"error=error, prompt": "x"}):
        with pytest.raises(ValueError):
            t.update(tid, **bad)
    t.update(tid, error="kept")
    assert (t.get(tid, None) or {})["error"] == "kept"


def test_task_moves_follow_the_table(tmp_path, monkeypatch):
    t = make_tasks(tmp_path, monkeypatch)
    tid = submit(t, "done")
    with pytest.raises(IllegalMove):
        t.move(tid, TaskStatus.DONE, TaskStatus.QUEUED)           # queued -> done skips running
    assert t.move(tid, TaskStatus.RUNNING, TaskStatus.QUEUED) is True
    assert t.move(tid, TaskStatus.RUNNING, TaskStatus.QUEUED) is False, "a second claim loses"
    assert t.move(tid, TaskStatus.CANCELLED, frozenset({TaskStatus.QUEUED, TaskStatus.RUNNING})) is True
    assert t.move(tid, TaskStatus.DONE, TaskStatus.RUNNING) is False, "the ending does not overwrite a cancel"


# ----------------------------------------------------------------------------------------- intake Engine

def engine(tmp_path: Path, **kw) -> Engine:
    (tmp_path / "store").mkdir(exist_ok=True)
    return Engine(tmp_path, str(tmp_path / "estate.db"), tmp_path / "store", ROOT,
                  lock=CatalogLock(tmp_path / "lock"), **kw)


def test_intake_cancelled_while_queued_never_runs(tmp_path):
    e = engine(tmp_path, skip=set(STAGE_IDS))
    jid = e.jobs.new("folder", {"path": str(tmp_path)})
    assert e.cancel(jid)                      # _loop has just picked it (its SELECT); the cancel lands first
    e._run(jid)                               # and then _run starts on it
    j = e.jobs.job(jid)
    assert j["state"] == JobState.CANCELLED and j["ready_at"] is None, j
    assert not any(ev["text"] == "done" for ev in e.jobs.events(jid))
    assert e.current is None


def test_intake_job_moves_are_checked(tmp_path):
    e = engine(tmp_path)
    jid = e.jobs.new("folder", {"path": str(tmp_path)})
    with pytest.raises(IllegalMove):
        e.jobs.move(jid, JobState.DONE)                           # queued -> done skips running
    with pytest.raises(ValueError):
        e.jobs.set(jid, state=JobState.DONE)                      # set() never writes the state
    assert e.jobs.move(jid, JobState.RUNNING, {JobState.QUEUED}) is True
    assert e.jobs.move(jid, JobState.RUNNING, {JobState.QUEUED}) is False, "a second claim loses"
    assert e.jobs.move(jid, JobState.DONE) is True
    with pytest.raises(IllegalMove):
        e.jobs.move(jid, JobState.QUEUED)                         # a finished job is not tried again
    assert e.jobs.job(jid)["state"] == JobState.DONE
    assert not e.retry(jid)


def test_intake_cancel_kills_a_stage_that_ignores_sigterm(tmp_path, term_ignored):
    e = engine(tmp_path, skip=set(STAGE_IDS) - {"ingest"})
    e.grace = 0.2
    e.command = lambda stage, paths: [sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(600)"]  # type: ignore[method-assign]
    jid = e.jobs.new("folder", {"path": str(tmp_path)})
    ready = threading.Event()
    real = e.jobs.event
    e.jobs.event = lambda j, text: (real(j, text), ready.set() if text == "ready" else None)[0]  # type: ignore[method-assign]
    runner = threading.Thread(target=e._run, args=(jid,), daemon=True)
    runner.start()
    assert ready.wait(BOUND)
    proc = e.proc
    assert proc is not None and e.cancel(jid)
    runner.join(BOUND)
    if runner.is_alive():
        proc.kill()
        runner.join(BOUND)
        pytest.fail("the stage ignored SIGTERM and nothing followed it")
    assert proc.returncode == -signal.SIGKILL
    assert e.jobs.job(jid)["state"] == JobState.CANCELLED


# ----------------------------------------------------------------------------------------- sync Feed

def sync_all(feed: Feed, device: dict, limit: int) -> list[list[str]]:
    """Page until up to date; the deletions each page carried."""
    pages = []
    while True:
        ids, deleted, (since, after), more = feed.page(device["epoch"], device["since"], device["after"], limit)
        pages.append(deleted)
        for sha in deleted:
            device["rows"].pop(sha, None)
        for sha in ids:
            device["rows"][sha] = True
        device.update(epoch=feed.epoch, since=since, after=after)
        if not more:
            return pages


def sha(i: int) -> str:
    return f"{i:064x}"


@pytest.fixture
def feed(tmp_path):
    f = Feed(tmp_path / "sync-feed.db")
    f.update({sha(i): {"n": i} for i in range(40)})
    return f


def test_sync_up_to_date_poll_has_no_deletions(feed):
    phone = {"epoch": "", "since": 0, "after": "", "rows": {}}
    sync_all(feed, phone, 100)
    feed.update({sha(i): {"n": i} for i in range(5, 40)})      # five deleted
    assert sync_all(feed, phone, 100) == [[sha(i) for i in range(5)]]
    assert set(phone["rows"]) == {sha(i) for i in range(5, 40)}
    for _ in range(3):                                         # polling again and again: nothing new, nothing repeated
        assert feed.page(phone["epoch"], phone["since"], phone["after"], 100)[1] == []


def test_sync_many_pages_deliver_each_deletion_once(feed):
    phone = {"epoch": "", "since": 0, "after": "", "rows": {}}
    sync_all(feed, phone, 100)
    catalog = {sha(i): {"n": i, "v": 2} for i in range(40) if i % 4}                # ten deleted, the rest changed
    feed.update(catalog)
    pages = sync_all(feed, phone, 3)
    sent = [s for p in pages for s in p]
    assert len(pages) > 3 and sorted(sent) == sorted(sha(i) for i in range(0, 40, 4)) and len(sent) == len(set(sent))
    assert set(phone["rows"]) == set(catalog)
    # a deletion made in the middle of a sync, of a row the phone already has, still reaches it once
    feed.update({s: dict(r, v=3) for s, r in catalog.items()})
    ids, deleted, (since, after), more = feed.page(phone["epoch"], phone["since"], phone["after"], 3)
    assert more and deleted == []
    for s in ids:
        phone["rows"][s] = True
    phone.update(since=since, after=after)
    gone = ids[0]
    feed.update({s: dict(r, v=3) for s, r in catalog.items() if s != gone})
    rest = [s for p in sync_all(feed, phone, 3) for s in p]
    assert rest == [gone] and gone not in phone["rows"] and set(phone["rows"]) == set(catalog) - {gone}


def test_sync_stale_client_gets_every_deletion(feed, tmp_path):
    behind = {"epoch": "", "since": 0, "after": "", "rows": {}}
    sync_all(feed, behind, 100)
    feed.update({sha(i): {"n": i} for i in range(3, 40)})       # version 2: three deleted
    feed.update({sha(i): {"n": i} for i in range(6, 40)})       # version 3: three more
    pages = sync_all(feed, behind, 4)
    assert sorted(s for p in pages for s in p) == [sha(i) for i in range(6)]
    assert set(behind["rows"]) == {sha(i) for i in range(6, 40)}
    # a device at an older cursor (or another epoch) is sent all of them too, after a restart of the feed
    again = Feed(tmp_path / "sync-feed.db")
    assert sorted(again.page(again.epoch, 1, "~", 100)[1]) == [sha(i) for i in range(6)]
    assert sorted(again.page("other", 3, "~", 100)[1]) == [sha(i) for i in range(6)]
    assert again.page(again.epoch, 3, "~", 100)[1] == []
    json.dumps(again.page(again.epoch, 0, "", 100))            # still what the API serializes
