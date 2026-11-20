# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8.3", "numpy>=1.26", "pillow>=10.0", "pillow-heif>=0.18"]
# ///
"""The test runner itself (tests/conftest.py): every self-running script is collected as one item that
passes only when the script exits 0, the fast tier leaves the slow scripts out, and the modules the
lightweight entry points import do not pull in the ML stack.

  uv run pytest tests/test_harness.py      # or, like every file here: uv run tests/test_harness.py
"""
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from conftest import LEGACY  # noqa: E402


def test_every_script_is_classified():
    """A self-running script missing from LEGACY would be imported by pytest and run at import time."""
    scripts = {p.name for p in HERE.glob("test_*.py")}
    assert set(LEGACY) <= scripts, f"LEGACY lists scripts that are gone: {set(LEGACY) - scripts}"
    native = {n for n in scripts - set(LEGACY) if "import pytest" in (HERE / n).read_text()}
    assert scripts - set(LEGACY) == native, \
        f"add these self-running scripts to LEGACY in tests/conftest.py: {sorted(scripts - set(LEGACY) - native)}"
    for name, marks in LEGACY.items():
        assert set(marks) <= {"slow", "server", "model"}, name
        assert "slow" in marks or not {"server", "model"} & set(marks), f"{name}: app and model tests are slow"


def test_scripts_run_whole_and_failures_are_reported(pytester: pytest.Pytester):
    """In a scratch folder with this conftest: an exit 0 passes, a non-zero exit fails with its output,
    and `-m "not slow"` deselects a slow script without running it."""
    pytester.makeconftest((HERE / "conftest.py").read_text())
    (pytester.path / "test_scores.py").write_text("print('ok')\n")                       # a fast script that passes
    (pytester.path / "test_stacks.py").write_text("print('checked 3 things')\nraise SystemExit(3)\n")
    (pytester.path / "test_mcp.py").write_text("raise SystemExit('a slow script must not run')\n")
    (pytester.path / "test_native.py").write_text("def test_it():\n    assert 1 + 1 == 2\n")

    fast = pytester.runpytest("-m", "not slow", "-p", "no:cacheprovider")
    fast.assert_outcomes(passed=2, failed=1, deselected=1)
    fast.stdout.fnmatch_lines(["*test_stacks.py exited 3*", "*checked 3 things*"])

    everything = pytester.runpytest("-p", "no:cacheprovider")
    everything.assert_outcomes(passed=2, failed=2)
    everything.stdout.fnmatch_lines(["*test_mcp.py exited 1*", "*a slow script must not run*"])


LIGHT = ["admin", "browse", "catalog", "changes", "encoder", "estate_mcp", "http_util", "images", "pages",
         "photo_app", "scores", "stacks", "stats", "sync", "tasks", "upload", "intake", "intake.server",
         "intake.engine", "intake.api", "core", "core.media", "core.schema", "core.config", "core.lifecycle"]


def test_lightweight_modules_do_not_import_the_ml_stack(estate_root):
    """The setup server, the CLI tools and the catalog stay fast: importing them loads no torch,
    transformers or onnxruntime (those live behind embed.py and faces.py)."""
    code = ("import importlib, sys\n"
            f"for m in {LIGHT!r}: importlib.import_module(m)\n"
            "print(sorted(k for k in ('torch', 'transformers', 'onnxruntime') if k in sys.modules))")
    r = subprocess.run([sys.executable, "-c", code], cwd=estate_root, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "[]", r.stdout


def test_demo_library_is_readable(demo_dir):
    """The demo library the app tests start from is checked in and opens (read-only)."""
    con = sqlite3.connect(f"file:{demo_dir / 'estate.db'}?mode=ro", uri=True)
    try:
        assert con.execute("SELECT count(*) FROM assets").fetchone()[0] > 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
