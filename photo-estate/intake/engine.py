"""The intake engine: one job per "bring in photos from X", run one at a time, through the same stages
for every source:

  acquire   the source fetches its files (signing in through the app if it has to)
  ingest    estate.py ingest: every photo into the store, once        } the catalogue pipeline,
  link      estate.py link: dates, places, albums from sidecars       } the same commands
  exif      estate.py exif: camera and lens (needs exiftool)          } ingest-iphone.sh and
  places    places.py run: places, local time, events, trips          } the book's chapter 16 run
  embed     embed.py run: what is in each photo, for search           } by hand
  faces     faces.py run: faces for the People page (optional: a failure here still leaves the photos in)

After `embed` the job is *ready*: its photos are searchable, and `on_ready` is called (the app reloads
its catalog, or in setup mode becomes the full app). Faces run on after that.

Jobs live in intake.db beside the catalog, never in it, so the catalog keeps a single writer of what it
holds (estate.py). Every stage can run again and does only new work, so a job stopped anywhere (a
restart, a crash, a cancel) resumes from the stage it was in. What a person types to sign in is held in
memory for the running job only: never written down, never returned by the API."""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from dataclasses import asdict
from pathlib import Path

from core import schema   # photo-estate/core, beside this package

from .base import SOURCES, Cancelled, Context, Prompt

STAGES = [
    ("acquire", "Fetching your photos"),
    ("ingest", "Copying them into your library"),
    ("link", "Matching dates, places and albums"),
    ("exif", "Reading camera details"),
    ("places", "Working out places and trips"),
    ("embed", "Learning what is in each photo"),
    ("faces", "Finding faces"),
]
STAGE_IDS = [s for s, _ in STAGES]
READY_AFTER = "embed"
OPTIONAL = {"faces"}            # a failure here finishes the job with a warning
LOCK = Path(os.environ.get("TMPDIR", "/tmp")) / "photo-estate-ingest-iphone.lock"   # shared with ingest-iphone.sh

SCHEMA = schema.INTAKE
ACTIVE = ("queued", "running", "waiting")


class Store:
    """intake.db: the jobs and what each one printed. Thread-safe."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.con.execute("PRAGMA journal_mode=WAL")
        schema.ensure(self.con, "intake")
        self.lock = threading.Lock()

    def q(self, sql, args=()):
        with self.lock:
            return self.con.execute(sql, args).fetchall()

    def new(self, source: str, config: dict) -> int:
        with self.lock:
            t = time.time()
            return self.con.execute("INSERT INTO jobs(source, config, state, stage, created_at, updated_at)"
                                    " VALUES (?,?,?,?,?,?)", (source, json.dumps(config), "queued", "acquire", t, t)).lastrowid

    def set(self, job_id: int, **kv) -> None:
        kv["updated_at"] = time.time()
        for k in ("paths", "prompt", "warnings"):
            if k in kv and not isinstance(kv[k], (str, type(None))):
                kv[k] = json.dumps(kv[k])
        with self.lock:
            self.con.execute(f"UPDATE jobs SET {', '.join(f'{k}=?' for k in kv)} WHERE id=?", (*kv.values(), job_id))

    def event(self, job_id: int, text: str) -> None:
        with self.lock:
            self.con.execute("INSERT INTO events(job_id, at, text) VALUES (?,?,?)", (job_id, time.time(), text))

    def get(self, job_id: int) -> dict | None:
        with self.lock:
            cur = self.con.execute("SELECT * FROM jobs WHERE id=?", (job_id,))
            row = cur.fetchone()
            names = [d[0] for d in cur.description]
        if not row:
            return None
        j = dict(zip(names, row))
        for k, empty in (("config", {}), ("paths", []), ("prompt", None), ("warnings", [])):
            j[k] = json.loads(j[k]) if j[k] else empty
        return j

    def all(self) -> list[dict]:
        return [self.get(r[0]) for r in self.q("SELECT id FROM jobs ORDER BY id DESC")]

    def events(self, job_id: int, after: int = 0, limit: int = 400) -> list[dict]:
        rows = self.q("SELECT id, at, text FROM (SELECT * FROM events WHERE job_id=? AND id>? ORDER BY id DESC LIMIT ?)"
                      " ORDER BY id", (job_id, after, limit))
        return [{"id": i, "at": at, "text": t} for i, at, t in rows]


def media_count(db: str) -> int:
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
        try:
            return c.execute("SELECT count(*) FROM assets").fetchone()[0]
        finally:
            c.close()
    except sqlite3.Error:
        return 0


class CatalogLock:
    """The lock ingest-iphone.sh takes (a directory), so a phone's upload run and an intake job never
    write the catalog at once. A lock this process took and then died holding is taken over."""

    def __init__(self, path: Path = LOCK):
        self.path = path
        self.held = False

    def acquire(self, cancelled, waiting) -> None:
        told = False
        while True:
            try:
                self.path.mkdir()
                (self.path / "owner").write_text(str(os.getpid()))
                self.held = True
                return
            except FileExistsError:
                if self._stale():
                    shutil.rmtree(self.path, ignore_errors=True)
                    continue
                if not told:
                    waiting()
                    told = True
                if cancelled():
                    raise Cancelled()
                time.sleep(2)

    def _stale(self) -> bool:
        try:
            pid = int((self.path / "owner").read_text())
        except (OSError, ValueError):
            return False                   # ingest-iphone.sh's: it removes its own on exit
        if pid == os.getpid():
            return not self.held           # ours from before a restart in place (exec keeps the pid)
        try:
            os.kill(pid, 0)
            return False
        except ProcessLookupError:
            return True
        except PermissionError:
            return False

    def release(self) -> None:
        if self.held:
            self.held = False
            shutil.rmtree(self.path, ignore_errors=True)


PROGRESS = [  # how each stage's output says how far it has got
    re.compile(r"^progress (?P<done>\d+)(?: of (?P<total>\d+))?(?:: (?P<what>.+))?$"),   # estate.py, places.py
    re.compile(r"(?P<done>\d+)/(?P<total>\d+) [\d.]+ (?:img|files)/s"),  # embed.py, faces.py, estate.py exif
]
TODO = re.compile(r"(?P<total>\d+) (new images )?to (do|scan)")


class Engine:
    def __init__(self, root: Path, db: str, store: Path, here: Path, *, on_ready=None, on_faces=None,
                 practice: bool = False, skip: set[str] | None = None, uv: str | None = None,
                 lock: CatalogLock | None = None):
        self.root, self.db, self.store, self.here = Path(root), db, Path(store), Path(here)
        self.on_ready, self.on_faces = on_ready, on_faces
        self.practice = practice
        self.skip = set(skip or ())         # stages to leave out (tests; --no-models)
        self.uv = uv or shutil.which("uv") or "uv"
        self.lock = lock or CatalogLock()
        self.jobs = Store(Path(db).resolve().parent / "intake.db")
        self.staging = Path(db).resolve().parent / "intake"
        self.wake = threading.Condition()
        self.asking: dict[int, dict] = {}   # job id -> {"prompt", "event", "value"}: answers live only here
        self.cancelled: set[int] = set()
        self.proc: subprocess.Popen | None = None
        self.current: int | None = None
        self.stage_started: dict[int, float] = {}   # job id -> when its current stage began (the page shows it)
        self.thread: threading.Thread | None = None

    # -- what the API calls

    def sources(self) -> list:
        return [s for s in SOURCES.values() if self.practice or not s.practice]

    def source(self, sid: str):
        s = SOURCES.get(sid)
        return s if s and (self.practice or not s.practice) else None

    def create(self, sid: str, config: dict) -> int:
        src = self.source(sid)
        if not src:
            raise ValueError(f"no source {sid!r}")
        config = {f.key: str(config.get(f.key, "")) for f in src.fields}
        p = src.preview(config)
        if not p.ok:
            raise ValueError(" ".join(p.problems) or "this source is not ready")
        jid = self.jobs.new(sid, config)
        self.jobs.event(jid, f"{src.name}: {p.summary}" if p.summary else src.name)
        self.jobs.set(jid, total=p.media, media_before=media_count(self.db))
        self.poke()
        return jid

    def answer(self, jid: int, value: str) -> bool:
        a = self.asking.get(jid)
        if not a:
            return False
        a["value"] = value
        a["event"].set()
        return True

    def cancel(self, jid: int) -> bool:
        j = self.jobs.get(jid)
        if not j or j["state"] not in ACTIVE:
            return False
        if self.current == jid:
            self.cancelled.add(jid)
            if a := self.asking.get(jid):
                a["event"].set()
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
        else:
            self.jobs.set(jid, state="cancelled", finished_at=time.time(), prompt=None)
        self.jobs.event(jid, "cancelled")
        return True

    def retry(self, jid: int) -> bool:
        j = self.jobs.get(jid)
        if not j or j["state"] not in ("failed", "cancelled"):
            return False
        self.jobs.set(jid, state="queued", error=None, finished_at=None)
        self.jobs.event(jid, f"trying again from “{dict(STAGES)[j['stage']]}”")
        self.poke()
        return True

    def view(self, jid: int, events_after: int = 0) -> dict | None:
        j = self.jobs.get(jid)
        if not j:
            return None
        src = SOURCES.get(j["source"])
        j["source_name"] = src.name if src else j["source"]
        j["stages"] = [{"id": s, "label": label, "skipped": s in self.skip, "optional": s in OPTIONAL,
                        "searchable_after": s == READY_AFTER,
                        "state": self._stage_state(j, s)} for s, label in STAGES]
        started = self.stage_started.get(jid) if j["state"] == "running" else None
        j["stage_seconds"] = round(time.time() - started) if started else None   # not a timestamp: clocks differ
        j["events"] = self.jobs.events(jid, events_after)
        j["ready"] = j["ready_at"] is not None
        return j

    def list(self) -> list[dict]:
        return [self.view(j["id"]) | {"events": []} for j in self.jobs.all()]

    @staticmethod
    def _stage_state(j: dict, s: str) -> str:
        at, here = STAGE_IDS.index(j["stage"]), STAGE_IDS.index(s)
        if j["state"] == "done" or here < at:
            return "done"
        if here > at:
            return "pending"
        return {"running": "running", "waiting": "waiting", "failed": "failed"}.get(j["state"], "pending")

    # -- running

    def start(self) -> None:
        for j in self.jobs.all():        # stopped mid-job last time (a restart): carry on where it was
            if j["state"] in ("running", "waiting"):
                self.jobs.set(j["id"], state="queued", prompt=None)
                self.jobs.event(j["id"], "resuming after a restart")
        self.thread = threading.Thread(target=self._loop, daemon=True, name="intake")
        self.thread.start()

    def poke(self) -> None:
        with self.wake:
            self.wake.notify_all()

    def _loop(self) -> None:
        while True:
            nxt = self.jobs.q("SELECT id FROM jobs WHERE state='queued' ORDER BY id LIMIT 1")
            if not nxt:
                with self.wake:
                    self.wake.wait(5)
                continue
            self._run(nxt[0][0])

    def _run(self, jid: int) -> None:
        j = self.jobs.get(jid)
        src = SOURCES.get(j["source"])
        self.current = jid
        self.jobs.set(jid, state="running", error=None)
        log = lambda text: self.jobs.event(jid, text)
        try:
            if src is None:
                raise RuntimeError(f"unknown source {j['source']!r}")
            for stage in STAGE_IDS[STAGE_IDS.index(j["stage"]):]:
                self._check(jid)
                self.jobs.set(jid, stage=stage, done=None, detail=dict(STAGES)[stage])
                self.stage_started[jid] = time.time()
                if stage in self.skip:
                    pass
                elif stage == "acquire":
                    paths = self._acquire(jid, src, j)
                    self.jobs.set(jid, paths=[str(p) for p in paths])
                    j["paths"] = [str(p) for p in paths]
                else:
                    try:
                        self._pipeline(jid, stage, j["paths"])
                    except (RuntimeError, OSError) as e:
                        if stage not in OPTIONAL:
                            raise
                        log(f"warning: {dict(STAGES)[stage].lower()} did not finish: {e}")
                        self.jobs.set(jid, warnings=self.jobs.get(jid)["warnings"] + [f"{stage}: {e}"])
                if stage == READY_AFTER:
                    nxt = STAGE_IDS[STAGE_IDS.index(stage) + 1]
                    self.jobs.set(jid, stage=nxt, ready_at=time.time(), media_after=media_count(self.db))
                    log("your photos are in the library and searchable")
                    self._tell(self.on_ready, jid)
            self.jobs.set(jid, state="done", finished_at=time.time(), detail="Done", prompt=None,
                          media_after=media_count(self.db))
            log("done")
            if "faces" not in self.skip:
                self._tell(self.on_faces, jid)
        except Cancelled:
            self.jobs.set(jid, state="cancelled", finished_at=time.time(), prompt=None, detail="Cancelled")
        except Exception as e:   # a source or a stage failed: say why, keep the stage so a retry resumes there
            msg = str(e) or type(e).__name__
            self.jobs.set(jid, state="failed", error=msg, finished_at=time.time(), prompt=None)
            log(f"failed: {msg}")
        finally:
            self.cancelled.discard(jid)
            self.asking.pop(jid, None)
            self.current = None

    def _check(self, jid):
        if jid in self.cancelled:
            raise Cancelled()

    def _acquire(self, jid: int, src, j: dict) -> list[Path]:
        staging = self.staging / f"job-{jid}"
        staging.mkdir(parents=True, exist_ok=True)

        def progress(done, total, detail):
            kv = {"detail": detail} if detail else {}
            if done is not None:
                kv["done"] = done
            if total is not None:
                kv["total"] = total
            self.jobs.set(jid, **kv)

        def ask(prompt: Prompt) -> str:
            ev = threading.Event()
            self.asking[jid] = {"prompt": prompt, "event": ev, "value": None}
            self.jobs.set(jid, state="waiting", prompt=asdict(prompt), detail=prompt.label)
            self.jobs.event(jid, f"waiting for you: {prompt.label}")
            ev.wait()
            value = self.asking.pop(jid, {}).get("value")
            self._check(jid)
            self.jobs.set(jid, state="running", prompt=None)
            return value or ""

        ctx = Context(jid, staging, lambda t: self.jobs.event(jid, t), progress, ask,
                      lambda: jid in self.cancelled, self.here)
        paths = src.acquire(j["config"], ctx)
        if not paths:
            raise RuntimeError("the source found nothing to bring in")
        return paths

    def command(self, stage: str, paths: list[str]) -> list[str] | None:
        """The command line for a pipeline stage (None: leave it out, with a warning already logged)."""
        def script(name, *args):
            return [self.uv, "run", "--quiet", "--script", str(self.here / name), "--db", str(self.db), *args]
        store = str(self.store)
        if stage == "ingest":
            # --force: a folder's name may have been seen before (a second "Photos" folder, a re-run); files
            # already recorded are skipped, so this costs a scan, never a second copy.
            return script("estate.py", "ingest", "--store", store, "--force", "--progress", *paths)
        if stage == "link":
            # Only what this job brought in (ingest names a source after the folder or zip): a whole-library
            # link takes most of an hour on a hard disk (issue #13).
            return script("estate.py", "link", *(x for p in paths for x in ("--source", Path(p).name)))
        if stage == "exif":
            if not shutil.which("exiftool"):
                return None
            return script("estate.py", "exif", "--store", store)
        if stage == "places":
            return script("places.py", "run")
        if stage == "embed":
            return script("embed.py", "run", "--store", store)
        if stage == "faces":
            return script("faces.py", "run", "--store", store)
        raise ValueError(stage)

    def _pipeline(self, jid: int, stage: str, paths: list[str]) -> None:
        argv = self.command(stage, paths)
        if argv is None:
            msg = "exiftool is not installed (brew install exiftool): camera and lens details were skipped"
            self.jobs.event(jid, f"warning: {msg}")
            self.jobs.set(jid, warnings=self.jobs.get(jid)["warnings"] + [msg])
            return
        self.lock.acquire(lambda: jid in self.cancelled,
                          lambda: self.jobs.set(jid, detail="Waiting for the phone's upload to finish cataloguing"))
        try:
            self.jobs.set(jid, detail=dict(STAGES)[stage], done=None, total=None if stage != "ingest" else self.jobs.get(jid)["total"])
            self.stage_started[jid] = time.time()
            env = dict(os.environ, PYTHONUNBUFFERED="1")
            self.proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                         stdin=subprocess.DEVNULL, env=env, cwd=self.here)
            tail = []
            total = self.jobs.get(jid)["total"] if stage == "ingest" else None
            for line in self.proc.stdout:
                line = line.rstrip()
                if not line:
                    continue
                if (m := TODO.search(line)) and stage != "ingest":
                    total = int(m["total"])
                    self.jobs.set(jid, total=total, done=0)
                hit = next((m for rx in PROGRESS if (m := rx.search(line))), None)
                if hit:
                    kv = {"done": int(hit["done"])}
                    if hit["total"]:
                        kv["total"] = int(hit["total"])
                    elif total is not None:
                        kv["total"] = total
                    what = hit.groupdict().get("what")
                    if what:
                        kv["detail"] = what[:1].upper() + what[1:]
                    self.jobs.set(jid, **kv)
                    if line.startswith("progress "):
                        continue           # progress, not news: the bar shows it
                self.jobs.event(jid, line)
                tail = (tail + [line])[-6:]
            code = self.proc.wait()
            self._check(jid)
            if code != 0:
                raise RuntimeError(f"{stage} stopped (exit {code}): " + (tail[-1] if tail else "no output"))
        finally:
            self.proc = None
            self.lock.release()

    @staticmethod
    def _tell(fn, jid) -> None:
        if fn:
            try:
                fn(jid)
            except Exception as e:
                print(f"intake: after job {jid}: {e}", flush=True)
