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

  uv run pytest tests/test_specs.py
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SPECS = Path(__file__).resolve().parent.parent / "specs"
PASSES = sorted(p for p in SPECS.glob("*.cfg") if ".before." not in p.name)
FAILS = sorted(SPECS.glob("*.before.*.cfg"))


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


@pytest.fixture(scope="module")
def tlc(tmp_path_factory):
    jar = Path(os.environ.get("TLA2TOOLS") or Path.home() / ".local/share/tlaplus/tla2tools.jar")
    java = _java()
    if java is None or not jar.exists():
        pytest.skip("TLC needs Java and tla2tools.jar (see this file's docstring)")

    def run(cfg: Path) -> subprocess.CompletedProcess:
        module = cfg.name.split(".")[0] + ".tla"
        return subprocess.run([java, "-cp", str(jar), "tlc2.TLC", "-metadir", str(tmp_path_factory.mktemp("tlc")),
                               "-config", cfg.name, module], cwd=SPECS, capture_output=True, text=True, timeout=600)
    return run


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
