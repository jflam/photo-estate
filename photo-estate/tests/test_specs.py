"""The TLA+ models in specs/, checked by TLC: every reachable state of each, against its invariants.

  specs/PrivatePhoto.tla   one `private` row and everything that writes it (private.py)
  specs/Lens.tla           a filter lens, a subscription to it and an album's series rows (lenses.py)
  specs/ChangeSet.tla      an album or change set, its photos, its series row and its inbox items (changes.py)
  specs/InboxItem.tla      the inbox items about one thing, and their writers (member_inbox.py and its callers)
  specs/Question.tla       a question, whom it was dealt to, and the run an answer starts (stories.py, listeners.py)
  specs/Member.tla         a member, their own agent's binding, and what a request is let through as (members.py,
                           photo_app.PhotoApp's roster)
  specs/Reload.tla         what the app holds in memory from the catalog, and how it is replaced while requests are
                           served (catalog.Live; PhotoApp.reload_catalog, relook, set_favorites, on_hidden_change)
  specs/ReloadLocks.tla    who waits for whom among those threads: the catalog gate, fav_write, SQLite's write lock
  specs/People.tla         who each face is, in the catalog and in the People page's memory, while People is loaded
                           again beside the one answering, and the groups of unnamed faces (faces.People;
                           PhotoApp.load_people, reload_catalog)
  specs/PhoneGate.tla      the iPhone's phone gate: a photo on a member's phone (GateState, the upload extension,
                           the app, the in-app uploader) and its opt-out on the server (ios/Shared/PhoneGate.swift;
                           ios/Tests/main.swift replays its traces)
  specs/PhoneReplica.tla   a member's change feed and their phone's copy of the catalog, with every writer of either
                           (sync.Feed; PhotoApp.feed_update, sync_route, set_favorites; the phone's SyncEngine)

For each model, <Name>.cfg is the code as it is and must pass; each <Name>.before.<invariant>.cfg is the code
before a fix, and must still find its violation (an invariant that can no longer fail checks nothing).

Skipped without Java or the TLA+ tools. They are one jar, tla2tools.jar, from
https://github.com/tlaplus/tlaplus/releases: put it at ~/.local/share/tlaplus/tla2tools.jar or name it in
TLA2TOOLS. Java is `java` on the PATH, $JAVA_HOME, or Homebrew's openjdk.
With REQUIRE_TLC set (CI sets it) they
fail instead of being skipped, so a run that checked no model cannot pass.

Each check is its own TLC process on one core. The ones a run asks for are all started when the run
starts (tests/conftest.py calls `start` once the tests are collected), as many at a time as the machine has
cores, so they are checked while the tests before this file run; each test here waits for its own.

  uv run pytest tests/test_specs.py
"""
import os
import shutil
import subprocess
import tempfile
import threading
from concurrent.futures import Future
from pathlib import Path

import pytest

SPECS = Path(__file__).resolve().parent.parent / "specs"
PASSES = sorted(p for p in SPECS.glob("*.cfg") if ".before." not in p.name)
FAILS = sorted(SPECS.glob("*.before.*.cfg"))
# Whole models large enough to benefit from multiple TLC worker threads, longest first.
HEAVY = ("PhoneReplica.cfg", "PhoneGate.cfg", "Reload.cfg", "Lens.cfg")


def _java() -> str | None:
    home = os.environ.get("JAVA_HOME")
    for exe in ((f"{home}/bin/java" if home else None), shutil.which("java"), "/opt/homebrew/opt/openjdk/bin/java"):
        if exe and Path(exe).exists():
            try:      # (macOS has a /usr/bin/java that only says there is no Java)
                if subprocess.run([exe, "-version"], capture_output=True, timeout=30).returncode == 0:
                    return exe
            except (OSError, subprocess.TimeoutExpired):
                pass
    return None


_started = False
_stop = threading.Event()
_runs: dict[Path, Future] = {}                 # a config -> its TLC run, started or waiting for cores
_procs: list[subprocess.Popen] = []
_procs_lock = threading.Lock()
_threads: list[threading.Thread] = []
_tmp: Path | None = None


def start(items) -> None:
    """Start TLC on every config these collected tests ask for. Nothing is started twice, and nothing without
    Java or the jar. conftest.py calls it when a run's tests are collected; `tlc` calls it too, for a run
    that did not come through that hook."""
    global _started, _tmp
    jar = Path(os.environ.get("TLA2TOOLS") or Path.home() / ".local/share/tlaplus/tla2tools.jar")
    here = Path(__file__).resolve()
    asked = {item.callspec.params["cfg"] for item in items
             if item.path == here and "cfg" in getattr(getattr(item, "callspec", None), "params", {})}
    if _started or not asked or not jar.exists() or (java := _java()) is None:
        return
    _started = True
    _stop.clear()
    _tmp = base = Path(tempfile.mkdtemp(prefix="tlc-"))

    cores = os.cpu_count() or 2
    heavy_asked = [c for c in asked if c.name in HEAVY]
    # Heavy whole models get multiple TLC worker threads without oversubscribing the machine: one alone gets
    # every core; several share them (up to 4 each, where TLC scales best here). Small whole models and `.before.`
    # checks stop in seconds and take one core each.
    heavy_workers = cores if len(heavy_asked) <= 1 else max(2, min(4, cores // min(len(heavy_asked), 3)))

    def want_cores(cfg: Path) -> int:
        return min(cores, heavy_workers) if cfg.name in HEAVY else 1

    def priority(cfg: Path) -> tuple[int, int, str]:
        if cfg.name in HEAVY:
            return (0, HEAVY.index(cfg.name), cfg.name)
        return (1 if ".before." not in cfg.name else 2, 0, cfg.name)

    ordered = sorted(asked, key=priority)
    for cfg in ordered:
        _runs[cfg] = Future()

    cond = threading.Condition()
    free = cores

    def run_one(cfg: Path, w: int) -> None:
        nonlocal free
        fut = _runs[cfg]
        try:
            if _stop.is_set():
                fut.cancel()
                return
            # Its own temporary folder: TLC unpacks Naturals.tla and the other standard modules there, and two
            # runs starting at once in one folder read each other's half-written copies ("Fatal errors while parsing").
            module, tmp = cfg.name.split(".")[0] + ".tla", base / cfg.name / "tmp"
            tmp.mkdir(parents=True)
            cmd = [java, f"-Djava.io.tmpdir={tmp}", "-cp", str(jar), "tlc2.TLC", "-workers", str(w),
                   "-metadir", str(base / cfg.name / "states"), "-config", cfg.name, module]
            proc = subprocess.Popen(cmd, cwd=SPECS, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            with _procs_lock:
                _procs.append(proc)
            try:
                out, err = proc.communicate(timeout=900)
            except subprocess.TimeoutExpired:
                proc.kill()
                raise
            fut.set_result(subprocess.CompletedProcess(cmd, proc.returncode, out, err))
        except BaseException as exc:
            if not fut.done():
                fut.set_exception(exc)
        finally:
            with cond:
                free += w
                cond.notify_all()

    def dispatch() -> None:
        nonlocal free
        for cfg in ordered:
            w = want_cores(cfg)
            with cond:
                while free < w and not _stop.is_set():
                    cond.wait(timeout=0.5)
                if _stop.is_set():
                    _runs[cfg].cancel()
                    continue
                free -= w
            t = threading.Thread(target=run_one, args=(cfg, w), daemon=True)
            _threads.append(t)
            t.start()

    dt = threading.Thread(target=dispatch, daemon=True)
    _threads.append(dt)
    dt.start()


def stop() -> None:
    """End every TLC still running (a run that stopped early leaves some) and forget them all."""
    global _started, _tmp
    _stop.set()
    with _procs_lock:
        for proc in _procs:
            if proc.poll() is None:
                proc.kill()
        _procs.clear()
    for t in _threads:
        t.join(timeout=2)
    _threads.clear()
    if _tmp is not None:
        shutil.rmtree(_tmp, ignore_errors=True)
    _started = False
    _tmp = None
    _runs.clear()


@pytest.fixture(scope="module")
def tlc(request):
    start(request.session.items)
    if not _started:
        why = "TLC needs Java and tla2tools.jar (see this file's docstring)"
        if os.environ.get("REQUIRE_TLC"):
            pytest.fail(f"REQUIRE_TLC is set, and {why}", pytrace=False)
        pytest.skip(why)
    yield lambda cfg: _runs[cfg].result()
    stop()


def test_there_are_models():
    assert PASSES and FAILS


@pytest.mark.parametrize("cfg", PASSES, ids=lambda p: p.name)
def test_the_code_as_modelled_keeps_every_invariant(tlc, cfg):
    got = tlc(cfg)
    assert got.returncode == 0 and "No error has been found" in got.stdout, got.stdout[-3000:]


@pytest.mark.parametrize("cfg", FAILS, ids=lambda p: p.name)
def test_the_code_before_the_fix_breaks_its_invariant(tlc, cfg):
    got = tlc(cfg)
    invariant = cfg.name.split(".")[2]
    assert got.returncode == 12 and f"Invariant {invariant} is violated" in got.stdout, got.stdout[-3000:]
