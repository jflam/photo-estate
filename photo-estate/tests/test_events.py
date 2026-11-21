"""The pipeline's events (issue #84, core/events.py): what estate.py, places.py, embed.py, faces.py and
ingest-iphone.sh tell the program that runs them, and what upload.IngestJob and intake.engine.Engine do
with it. Nothing here reads a human log line for control flow any more: a reworded line changes nothing,
and the old wording without the events does nothing.

  parse       a line round-trips; a newer 1.x is read, another major version, an unknown type and a
              malformed line are not, and none of them raises
  IngestJob   new_assets then catalog_ready reloads the catalog at that point (before the run ends);
              the old human lines alone do not; event lines stay out of the page's log
  Engine      a stage's progress events set done, total and detail; its human lines go in the job's log
  emitters    estate.py, places.py, embed.py, faces.py and ingest-iphone.sh print the events the app reads,
              and print nothing of the protocol when not asked (a person's terminal looks as before)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import events  # noqa: E402
from core.events import Event, Progress  # noqa: E402
from core.states import IngestState  # noqa: E402
from intake.engine import CatalogLock, Engine  # noqa: E402
from upload import IngestJob  # noqa: E402

BOUND = 30      # seconds: only how long a broken build may hang before the test says so


def protocol(out: str) -> list:
    return [ev for line in out.splitlines() if (ev := events.parse(line)) is not None]


# ----------------------------------------------------------------------------------------- the module

def test_emit_and_parse_round_trip(monkeypatch, capsys):
    monkeypatch.delenv(events.ENV, raising=False)
    events.emit_progress("embed", 3, 10)
    events.emit_event("catalog_ready")
    assert capsys.readouterr().out == "", "nothing is printed unless the parent asks"
    monkeypatch.setenv(events.ENV, "1")
    events.emit_progress("embed", 3, 10)
    events.emit_progress("link", 2000, 14327, "matching each photo with its details")
    events.emit_progress("ingest", 25)
    events.emit_event("new_assets", count=2)
    events.emit_event("catalog_ready")
    out = capsys.readouterr().out
    assert all(line.startswith(events.PREFIX) for line in out.splitlines())
    assert json.loads(out.splitlines()[0][len(events.PREFIX):])["v"] == events.VERSION == "1.0"
    assert protocol(out) == [Progress("embed", 3, 10), Progress("link", 2000, 14327, "matching each photo with its details"),
                             Progress("ingest", 25), Event("new_assets", {"count": 2}), Event("catalog_ready")]


def test_parse_takes_newer_minors_and_refuses_the_rest():
    ok = '@@pe {"v":"1.7","type":"event","name":"catalog_ready","since":"later"}'
    assert events.parse(ok) == Event("catalog_ready", {"since": "later"}), "a newer minor version is read"
    assert events.parse('@@pe {"v":"1.3","type":"progress","stage":"x","done":1,"eta":5}') == Progress("x", 1)
    for line in ['== 12:00:01 catalog ready', 'progress 25 of 100: linking', '',
                 '@@pe {"v":"2.0","type":"event","name":"catalog_ready"}',      # another major version
                 '@@pe {"type":"event","name":"catalog_ready"}',                # no version
                 '@@pe {"v":1,"type":"event","name":"catalog_ready"}',          # not "MAJOR.MINOR"
                 '@@pe {"v":"1.0","type":"heartbeat"}',                         # a type this reader does not know
                 '@@pe {"v":"1.0","type":"event"}', '@@pe {"v":"1.0","type":"event","name":""}',
                 '@@pe {"v":"1.0","type":"progress","stage":"x","done":"3"}',
                 '@@pe {"v":"1.0","type":"progress","stage":"x","done":-1}',
                 '@@pe {"v":"1.0","type":"progress","stage":"x","done":true}',
                 '@@pe {"v":"1.0","type":"progress","stage":"x","done":1,"total":1.5}',
                 '@@pe {"v":"1.0","type":"progress","done":1}',
                 '@@pe {"v":"1.0","type":"progress","stage":"x","done":1,"what":7}',
                 '@@pe {"v":"1.0",', '@@pe ', '@@pe []', '@@pe null', '@@pe "x"', '@@pe {"v":"1.0"} trailing',
                 ' @@pe {"v":"1.0","type":"event","name":"catalog_ready"}']:
        assert events.parse(line) is None, line


# ----------------------------------------------------------------------------------------- IngestJob._follow

class FakeProc:
    """A child whose stdout is `lines`, one at a time; a callable in it runs at that point (and yields nothing)."""
    def __init__(self, lines, code: int = 0):
        self.lines, self.code = lines, code

    @property
    def stdout(self):
        for x in self.lines:
            if callable(x):
                x()
            else:
                yield x + "\n"

    def wait(self):
        return self.code

    def poll(self):
        return self.code


def follow(tmp_path: Path, lines: list, code: int = 0) -> tuple[IngestJob, list[str]]:
    job = IngestJob(tmp_path / "ingest.sh", tmp_path, str(tmp_path / "estate.db"), tmp_path / "store",
                    tmp_path / "inbox", quiet=0)
    told: list[str] = []
    reloaded = threading.Event()
    job.on_added = lambda: (told.append("catalog"), reloaded.set())
    job.on_faces = lambda: told.append("faces")
    with job.lock:
        job.state = IngestState.RUNNING
    wait_reload = lambda: told.append("reloaded before the end" if reloaded.wait(BOUND) else "no reload")
    job._follow(FakeProc([wait_reload if x == "WAIT" else x for x in lines], code))
    return job, told


NEW2 = events.line("event", name="new_assets", count=2)
READY = events.line("event", name="catalog_ready")


def test_follow_reloads_at_catalog_ready(tmp_path):
    job, told = follow(tmp_path, ["== 12:00:00 ingest /x/iphone", NEW2, "== 12:00:01 2 new assets and sidecars: link",
                                  "== 12:00:09 catalog ready", READY, "WAIT", "== 12:00:10 faces run", "done"])
    assert told == ["catalog", "reloaded before the end", "faces"], told
    assert list(job.log) == ["== 12:00:00 ingest /x/iphone", "== 12:00:01 2 new assets and sidecars: link",
                             "== 12:00:09 catalog ready", "== 12:00:10 faces run", "done"], "event lines are not the page's"
    assert job.fresh and job.state == IngestState.IDLE and job.code == 0


def test_follow_ignores_the_wording(tmp_path):
    _, told = follow(tmp_path, ["== 12:00:01 2 photos came in", NEW2, "== 12:00:09 searchable now", READY, "WAIT", "x"])
    assert told == ["catalog", "reloaded before the end", "faces"], "a reworded log line still reloads at once"
    job, told = follow(tmp_path, ["== 12:00:01 2 new assets and sidecars: link", "== 12:00:09 catalog ready", "done"])
    assert told == [] and not job.fresh, "the old lines without the events say nothing"


def test_follow_needs_new_assets(tmp_path):
    job, told = follow(tmp_path, [events.line("event", name="new_assets", count=0), "nothing new"])
    assert told == [] and not job.fresh
    job, told = follow(tmp_path, [READY, events.line("event", name="what_next"), "@@pe {broken", "done"])
    assert told == [] and list(job.log) == ["done"], "catalog_ready alone, unknown and broken event lines: nothing"
    job, told = follow(tmp_path, [NEW2, "boom"], code=1)
    assert told == [] and not job.fresh and job.state == IngestState.IDLE, "a failed run without catalog_ready"
    job, told = follow(tmp_path, [NEW2, "done"])
    assert told == ["catalog", "faces"] and job.fresh, "a script without catalog_ready: told at the end"


# ----------------------------------------------------------------------------------------- Engine._pipeline

def test_engine_progress_comes_from_events(tmp_path):
    (tmp_path / "store").mkdir()
    e = Engine(tmp_path, str(tmp_path / "estate.db"), tmp_path / "store", ROOT, lock=CatalogLock(tmp_path / "lock"))
    child = ("import sys; sys.path.insert(0, %r); from core import events\n"   # emits only if the engine asked
             "print('siglip: 3 to do (0 RAW files skipped)', flush=True); events.emit_progress('embed', 0, 3)\n"
             "print('12:00:00 1/3 0.5 img/s, 0 undecodable', flush=True); events.emit_progress('embed', 2)\n"
             "print('progress 99 of 100: an old line', flush=True)\n"
             "events.emit_progress('embed', 2, what='writing vectors')\n" % str(ROOT))
    e.command = lambda stage, paths: [sys.executable, "-c", child]  # type: ignore[method-assign]
    jid = e.jobs.new("folder", {"path": str(tmp_path)})
    sets: list[dict] = []
    real = e.jobs.set
    e.jobs.set = lambda j, **kv: (sets.append(kv), real(j, **kv))[1]  # type: ignore[method-assign]
    e._pipeline(jid, "embed", [])
    j = e.jobs.job(jid)
    assert (j["done"], j["total"], j["detail"]) == (2, 3, "Writing vectors"), j
    assert {"done": 0, "total": 3} in sets and {"done": 2, "total": 3} in sets, "the total holds for later counts"
    assert not any(kv.get("done") in (1, 99) for kv in sets), "the human lines no longer move the bar"
    log = [ev["text"] for ev in e.jobs.events(jid)]
    assert log == ["siglip: 3 to do (0 RAW files skipped)", "12:00:00 1/3 0.5 img/s, 0 undecodable",
                   "progress 99 of 100: an old line"], log
    assert e.proc is None


# ----------------------------------------------------------------------------------------- the emitters

def run(args: list[str], env_on: bool, cwd: Path = ROOT, **env) -> str:
    e = {k: x for k, x in os.environ.items() if k != events.ENV} | env | ({events.ENV: "1"} if env_on else {})
    r = subprocess.run(args, cwd=cwd, env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=300)
    assert r.returncode == 0, r.stdout
    return r.stdout


def jpegs(folder: Path, n: int, first: int = 0) -> None:
    from PIL import Image
    folder.mkdir(parents=True)
    for i in range(first, first + n):
        Image.new("RGB", (8, 8), (i, 255 - i, 7)).save(folder / f"IMG_{i:04d}.JPG")


@pytest.fixture
def library(tmp_path):
    """A tiny catalog of 26 photos in tmp_path, ingested and linked with the events off."""
    jpegs(tmp_path / "phone", 26)
    db, store = str(tmp_path / "estate.db"), str(tmp_path / "store")
    run([sys.executable, "estate.py", "--db", db, "ingest", "--store", store, str(tmp_path / "phone")], False)
    return tmp_path, db, store


def test_estate_emits_progress(library):
    tmp, db, store = library
    jpegs(tmp / "more", 26, first=100)
    argv = [sys.executable, "estate.py", "--db", db, "ingest", "--store", store, "--progress", str(tmp / "more")]
    out = run(argv, True)
    assert protocol(out) == [Progress("ingest", 25)] and "progress " not in out.replace(events.PREFIX, ""), out
    out = run([sys.executable, "estate.py", "--db", db, "link"], True)
    got = protocol(out)
    assert Progress("link", 52, 52, "matching each photo with its details") in got, out
    assert any(p.stage == "search" and p.done == p.total and p.what == "updating search" for p in got), out
    out = run([sys.executable, "estate.py", "--db", db, "link"], False)
    assert events.PREFIX not in out and "progress 52 of 52: matching each photo" in out, "a person's terminal: as before"
    if shutil.which("exiftool"):
        out = run([sys.executable, "estate.py", "--db", db, "exif", "--store", store], True)
        assert any(p.stage == "exif" and p.done == p.total for p in protocol(out)), out


def test_places_emits_progress(library):
    _, db, _ = library          # no GPS: places.py needs none of its geocoding packages
    got = protocol(run([sys.executable, "places.py", "--db", db, "run"], True))
    assert Progress("places", 1, 5, "reading the library") in got, got
    assert all(p.stage in ("places", "search") for p in got if isinstance(p, Progress)), got


@pytest.mark.parametrize("script", ["embed.py", "faces.py"])
def test_models_emit_their_to_do(library, script):
    """The "N to do" line's event, with nothing to do (--after-rowid past the end), so no model is loaded."""
    _, db, store = library
    out = run([sys.executable, script, "--db", db, "run", "--store", store, "--after-rowid", "99999"], True)
    assert protocol(out) == [Progress(script[:-3], 0, 0)], out
    assert events.PREFIX not in run([sys.executable, script, "--db", db, "run", "--store", store,
                                     "--after-rowid", "99999"], False)


@pytest.fixture
def fake_bin(tmp_path):
    """ingest-iphone.sh with its tools faked: `uv` prints the step, and its ingest makes `sqlite3`'s
    count go from 0 to 2 (or not, for NEW=0)."""
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "uv").write_text('#!/bin/sh\necho "step $*"\ncase "$*" in *" ingest "*) [ "${NEW:-2}" = 0 ] || touch "$FAKE/ingested";; esac\n')
    (bin_ / "sqlite3").write_text('#!/bin/sh\ncase "$2" in *count*) [ -e "$FAKE/ingested" ] && echo 2 || echo 0;; *) echo 0;; esac\n')
    for f in bin_.iterdir():
        f.chmod(0o755)
    root = tmp_path / "root"
    (root / "store").mkdir(parents=True)
    (root / "iphone").mkdir()
    script = tmp_path / "app" / "ingest-iphone.sh"   # its own folder: the steps it would run are the fake uv's
    script.parent.mkdir()
    shutil.copy(ROOT / "ingest-iphone.sh", script)
    env = {"PATH": f"{bin_}:{os.environ['PATH']}", "FAKE": str(tmp_path), "TMPDIR": str(tmp_path)}
    return script, root, env


def test_ingest_script_emits_its_events(fake_bin):
    script, root, env = fake_bin
    out = run([str(script), str(root)], True, **env)
    assert protocol(out) == [Event("new_assets", {"count": 2}), Event("catalog_ready")], out
    lines = out.splitlines()
    at = lines.index(READY)
    assert lines[at - 1].endswith(" catalog ready") and "faces.py" in "".join(lines[at:]), "in order: before faces"
    (Path(env["FAKE"]) / "ingested").unlink()
    off = run([str(script), str(root)], False, **env)
    assert events.PREFIX not in off and "2 new assets and sidecars" in off and "catalog ready" in off, off
    (Path(env["FAKE"]) / "ingested").unlink()
    nothing = run([str(script), str(root)], True, **env, NEW="0")
    assert protocol(nothing) == [Event("new_assets", {"count": 0})] and "nothing new" in nothing, nothing


def test_ingest_script_puts_face_thumbnails_in_the_cache_it_is_given(fake_bin):
    """With the catalog on another disk than the thumbnails (the Mac mini: catalog on the SSD, cache on
    the USB disk), faces.py must be told the cache: its default is beside the catalog, where the app
    does not look."""
    script, root, env = fake_bin
    faces = lambda out: next(line for line in out.splitlines() if line.startswith("step") and "faces.py" in line)
    assert "--cache" not in faces(run([str(script), str(root)], True, **env))
    (Path(env["FAKE"]) / "ingested").unlink()
    assert faces(run([str(script), str(root)], True, **env, CACHE="/disk/review")).endswith("--cache /disk/review")


def test_ingest_job_follows_the_real_script(fake_bin, monkeypatch):
    """The two ends together: IngestJob runs ingest-iphone.sh, and is told at its catalog_ready, even with
    the script's human lines reworded."""
    script, root, env = fake_bin
    text = script.read_text()
    assert "new assets and sidecars:" in text and 'catalog ready"' in text
    script.write_text(text.replace("new assets and sidecars:", "new files:").replace('catalog ready"', 'searchable now"'))
    for k, x in env.items():
        monkeypatch.setenv(k, x)
    monkeypatch.delenv(events.ENV, raising=False)       # IngestJob sets it for its run
    job = IngestJob(script, root, str(root / "estate.db"), root / "store", root / "iphone", quiet=0)
    told, ended = [], threading.Event()
    job.on_added = lambda: told.append("catalog")
    job.on_faces = lambda: (told.append("faces"), ended.set())
    assert job.start()
    assert ended.wait(BOUND), list(job.log)
    assert told == ["catalog", "faces"] and job.status()["restart_needed"], told
    assert not any(events.PREFIX in line for line in job.log) and any("searchable now" in line for line in job.log)
