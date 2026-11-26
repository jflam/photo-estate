"""specs/machines.toml, held to the code: the register of every piece of state that is in one of a few named
conditions, and what checks its moves (a TLA+ model, a table of legal moves, or nothing yet).

What fails here, and what to do then (specs/README.md is the long version):

  - a status-like column, or a StrEnum of core/, that no entry claims: you added state. Add its entry, and decide
    how its moves are checked. Several writers, and it decides what someone is shown or allowed: write its model
    in this pull request (the skill verify-state-machines is the procedure). One writer: a table of legal moves.
  - a module that writes a machine's tables and is not among its `writers`: you added a writer. If the machine
    has a model, the model does not know about your writer yet: add it there as an action, run TLC, then list
    the module. If it has a table of moves and one writer, it now has two, and a table checks one writer.
  - a modelled function whose fingerprint is not its `stamp`: you changed code a model stands for. Read the
    model's action for it, change the model if what it does changed, run TLC, then set the stamp printed here.
  - an `unmodelled` entry that is not in BACKLOG below: new machines are not added to the backlog.

  uv run pytest tests/test_machines.py
"""
import ast
import enum
import hashlib
import importlib
import re
import sqlite3
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core import schema  # noqa: E402

SPECS = ROOT / "specs"
MACHINES = tomllib.loads((SPECS / "machines.toml").read_text())["machine"]
STATE_COLUMNS = {"status", "state", "why", "mode", "decision", "phase", "stage"}   # names a state column goes by
ENUM_MODULES = ("core.states", "core.lifecycle")
# The machines the survey of November 2026 found with no model. This set only gets shorter, as each is modelled:
# a machine that is new is modelled, or given its table of moves, in the pull request that adds it.
BACKLOG = {"a member", "a member's own agent on a device", "an inbox item", "a question, and whom it was dealt to",
           "the catalog reload", "the clustering of unnamed faces"}
HOW = "see tests/test_machines.py's docstring and specs/README.md"


def modules() -> list[Path]:
    return sorted([*ROOT.glob("*.py"), *(ROOT / "intake").rglob("*.py"), *(ROOT / "core").glob("*.py")])


def writers(tables) -> list[str]:
    """The modules with SQL that writes any of `tables` (UPDATE, INSERT or DELETE, as the code spells them)."""
    pats = [re.compile(rf"\b(UPDATE\s+{t}\s+SET|INTO\s+{t}\b|DELETE\s+FROM\s+{t}\b)") for t in tables]
    return [str(p.relative_to(ROOT)) for p in modules() if any(pat.search(p.read_text()) for pat in pats)]


def fingerprint(code: dict[str, list[str]]) -> str:
    """A hash of the named functions as Python reads them: comments, docstrings and layout do not change it. A
    method is named Class.method."""
    h = hashlib.sha256()
    fns = (ast.FunctionDef, ast.AsyncFunctionDef)
    for path in sorted(code):
        tree = ast.parse((ROOT / path).read_text())
        defs = {n.name: n for n in tree.body if isinstance(n, fns)}
        defs |= {f"{c.name}.{n.name}": n for c in tree.body if isinstance(c, ast.ClassDef) for n in c.body if isinstance(n, fns)}
        for name in sorted(code[path]):
            assert name in defs, f"{path} has no function {name}, which specs/machines.toml says a model stands for ({HOW})"
            fn = defs[name]
            if fn.body and isinstance(fn.body[0], ast.Expr) and isinstance(getattr(fn.body[0], "value", None), ast.Constant) \
                    and isinstance(fn.body[0].value.value, str):
                fn.body = fn.body[1:] or [ast.Pass()]
            h.update(f"{path}:{name}\n{ast.dump(fn)}\n".encode())
    return h.hexdigest()[:16]


def test_every_entry_is_whole():
    names = [m["name"] for m in MACHINES]
    assert len(names) == len(set(names))
    for m in MACHINES:
        assert m["checked"] in ("model", "table", "not_needed", "unmodelled"), m["name"]
        if m["checked"] == "model":
            assert (SPECS / m["model"]).exists() and (SPECS / m["model"]).with_suffix(".cfg").exists(), m["name"]
            assert m.get("code") and m.get("stamp"), f"{m['name']}: a model names the functions it stands for"
        if m["checked"] == "table":
            mod, _, attr = m["table"].partition(":")
            obj = importlib.import_module(mod)
            for part in attr.split("."):
                obj = getattr(obj, part)
            assert isinstance(obj, dict) and obj, f"{m['name']}: {m['table']} is not a table of moves"
            assert len(m.get("writers", [])) <= 1, f"{m['name']}: a table of moves checks one writer; with more it needs a model ({HOW})"
        if m["checked"] == "not_needed":
            assert m.get("why"), m["name"]


def test_every_state_column_is_claimed():
    found = set()
    for area in schema.AREAS:                       # (an area to a connection: two of them have a table `events`)
        con = sqlite3.connect(":memory:")
        schema.ensure(con, area)
        for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            found |= {f"{t}.{r[1]}" for r in con.execute(f"PRAGMA table_info({t})") if r[1] in STATE_COLUMNS}
        con.close()
    claimed = {c for m in MACHINES for c in m.get("columns", [])}
    assert not found - claimed, f"state nobody has registered: {sorted(found - claimed)} ({HOW})"
    assert not claimed - found, f"specs/machines.toml names columns the schema does not have: {sorted(claimed - found)}"


def test_every_state_enum_is_claimed():
    found = {n for mod in ENUM_MODULES for n, v in vars(importlib.import_module(mod)).items()
             if isinstance(v, type) and issubclass(v, enum.StrEnum) and v.__module__ == mod}
    claimed = {e for m in MACHINES for e in m.get("enums", [])}
    assert not found - claimed, f"states nobody has registered: {sorted(found - claimed)} ({HOW})"
    assert not claimed - found, f"specs/machines.toml names enums core/ does not have: {sorted(claimed - found)}"


@pytest.mark.parametrize("m", [m for m in MACHINES if m.get("tables")], ids=lambda m: m["name"])
def test_a_machines_writers_are_the_ones_registered(m):
    got = writers(m["tables"])
    new = sorted(set(got) - set(m["writers"]))
    assert not new, (f"{', '.join(new)} now writes {', '.join(m['tables'])} ({m['name']}, checked: {m['checked']}"
                     f"{', ' + m['model'] if m.get('model') else ''}). {HOW}")
    assert got == sorted(m["writers"]), f"{m['name']}: its writers are {got}"


@pytest.mark.parametrize("m", [m for m in MACHINES if m["checked"] == "model"], ids=lambda m: m["name"])
def test_modelled_code_is_as_it_was_when_its_model_was_last_read(m):
    text = (SPECS / m["model"]).read_text()
    for path, names in m["code"].items():
        for name in names:
            assert re.search(rf"\b{re.escape(name)}\b", text), f"{m['model']} never names {path}'s {name}: which action is it?"
    now = fingerprint(m["code"])
    assert now == m["stamp"], (
        f"{m['name']}: code that specs/{m['model']} stands for has changed ({', '.join(f'{p}: {chr(44).join(n)}' for p, n in m['code'].items())})."
        f" Read the model's action for what you changed; if what it does is different now, change the model and run"
        f" `uv run pytest tests/test_specs.py`. Then, in specs/machines.toml, set stamp = \"{now}\".")


def test_the_backlog_only_gets_shorter():
    waiting = {m["name"] for m in MACHINES if m["checked"] == "unmodelled"}
    assert waiting <= BACKLOG, (f"{sorted(waiting - BACKLOG)}: a new machine is not put on the backlog. Model it, or give"
                                f" it a table of moves, in this pull request ({HOW})")
    assert BACKLOG <= waiting, f"modelled now, so take it out of BACKLOG in this file: {sorted(BACKLOG - waiting)}"
