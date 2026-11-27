"""pytest for photo-estate: runs the self-running test scripts in tests/ unchanged, plus native pytest tests.

Every `test_*.py` here began as a self-running script (`uv run tests/test_x.py` prints its checks and
exits 0, or raises). Imported as a module it would run at import time, so pytest does not import the
scripts listed in LEGACY: each one is collected as a single test item that runs the script whole in a
subprocess and passes when it exits 0. A failure shows the script's exit code and the end of its output.
A file not listed here is an ordinary pytest module (tests/test_harness.py checks that no script is left
out of the list).

  uv run pytest -m "not slow"         # the fast tier: what CI runs, in two kinds of job (a few minutes)
  uv run pytest                       # everything, including the app and model tests (several minutes)
  uv run pytest --legacy-runner=uv    # run each script with `uv run`, in its own PEP 723 environment

Markers (declared in pyproject.toml):
  legacy   one of the scripts below
  slow     left out of the fast tier
  server   starts the real app, `uv run embed.py serve --demo`: its environment has torch and
           transformers, gigabytes on a fresh Linux machine
  model    also needs the SigLIP 2 search model in the Hugging Face cache (HF_HUB_OFFLINE=1 is set)

By default a script runs with this environment's Python (sys.executable): the locked dev environment
from `uv sync`, so the fast tier uses exactly the versions in uv.lock. Scripts that start the app or
estate.py still start them with `uv run`, as before.

Fixtures:
  estate_root   the photo-estate/ folder (where the scripts run from)
  demo_dir      the checked-in demo library (photo-estate/demo); never write to it
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent      # photo-estate/
DEMO = ROOT / "demo"

# Every self-running script, with the markers that say what it needs. Times are on an M-series Mac
# with warm uv caches. The fast tier needs only the dev group (numpy, pillow, pillow-heif) and uv.
FAST: tuple[str, ...] = ()
LEGACY: dict[str, tuple[str, ...]] = {
    "test_aesthetics_judge.py": FAST,                  # ~13 s, numpy; local HTTP server in a thread
    "test_aesthetics_taste.py": FAST,                  # <1 s, numpy
    "test_intake.py": FAST,                            # ~10 s, the setup server, no models
    "test_poc.py": FAST,                               # ~6 s, `uv run --script estate.py` (exif checks skip without exiftool)
    "test_scores.py": FAST,                            # <1 s, numpy
    "test_stacks.py": FAST,                            # <1 s, numpy
    "test_sync.py": FAST,                              # ~1 s
    "test_upload.py": FAST,                            # ~12 s, `uv run --script estate.py`
    "test_changes.py": ("slow", "server"),             # ~3 s once embed.py's environment exists
    "test_favorite.py": ("slow", "server"),
    "test_layout.py": ("slow", "server"),
    "test_tasks.py": ("slow", "server"),
    "test_mcp.py": ("slow", "server"),                 # ~5 s with the model, ~2 min without (a search waits it out)
    "test_albums.py": ("slow", "server", "model"),     # text search ("latte art") needs the model
    "test_connections.py": ("slow", "server", "model"),
    "test_compare.py": ("slow", "server", "model"),   # /compare: the app's own model loaded twice  # uploads are embedded as they arrive
}
TIMEOUT = {"fast": 300, "slow": 1800}                  # seconds, per script


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--legacy-runner", choices=("python", "uv"), default="python",
                     help="run the self-running scripts with this environment's Python (default) "
                          "or with `uv run`, each in its own PEP 723 environment")


def _specs(session: pytest.Session):
    """tests/test_specs.py as this run imported it, or None when the run has none of its tests."""
    return next((item.module for item in session.items
                 if item.path.name == "test_specs.py" and hasattr(getattr(item, "module", None), "start")), None)


def pytest_collection_finish(session: pytest.Session) -> None:
    # The TLA+ models take minutes and each has a core to itself: they are started now, to be checked while
    # the tests before test_specs.py run (its docstring).
    if (specs := _specs(session)) and not session.config.option.collectonly:
        specs.start(session.items)


def pytest_sessionfinish(session: pytest.Session) -> None:
    if specs := _specs(session):
        specs.stop()


def pytest_pycollect_makemodule(module_path: Path, parent: pytest.Collector):
    if module_path.name in LEGACY and module_path.parent == Path(__file__).resolve().parent:
        return LegacyScript.from_parent(parent, path=module_path)
    return None


class LegacyScriptFailed(Exception):
    def __init__(self, rc: int | None, output: str, cmd: list[str]):
        super().__init__(rc)
        self.rc, self.output, self.cmd = rc, output, cmd


class LegacyScript(pytest.File):
    """One self-running script, collected as one item."""

    def collect(self):
        yield LegacyItem.from_parent(self, name=self.path.stem)


class LegacyItem(pytest.Item):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.add_marker("legacy")
        for m in LEGACY[self.path.name]:
            self.add_marker(m)

    def runtest(self) -> None:
        runner = self.config.getoption("--legacy-runner")
        cmd = (["uv", "run", "--quiet", str(self.path)] if runner == "uv"
               else [sys.executable, str(self.path)])
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        if runner == "uv":
            env["PHOTO_ESTATE_LEGACY_UV"] = "1"
        env.setdefault("HF_HUB_OFFLINE", "1")          # never download the model from a test
        slow = "slow" in LEGACY[self.path.name]
        try:
            r = subprocess.run(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, errors="replace", timeout=TIMEOUT["slow" if slow else "fast"])
        except subprocess.TimeoutExpired as e:
            out = e.stdout if isinstance(e.stdout, str) else (e.stdout or b"").decode(errors="replace")
            raise LegacyScriptFailed(None, out, cmd) from None
        if r.returncode != 0:
            raise LegacyScriptFailed(r.returncode, r.stdout, cmd)

    def repr_failure(self, excinfo, style=None):
        if isinstance(excinfo.value, LegacyScriptFailed):
            e = excinfo.value
            why = "timed out" if e.rc is None else f"exited {e.rc}"
            tail = "\n".join(e.output.splitlines()[-60:])
            return f"{self.path.name} {why}: {' '.join(e.cmd)}\n--- last lines of its output ---\n{tail}"
        return super().repr_failure(excinfo, style=style)

    def reportinfo(self):
        return self.path, None, f"legacy script {self.path.name}"


@pytest.fixture(scope="session")
def estate_root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def demo_dir() -> Path:
    assert (DEMO / "estate.db").is_file(), "the demo library is missing"
    return DEMO
