"""The typed contracts of issue #90: Catalog is a dataclass with every field declared (no attribute comes
from anywhere else), a catalog read from the demo library has the types and the None-ness those fields
promise, with and without scorers, and the state enums (core/states.py) are the strings the catalog,
intake.db and the JSON API already hold, so nothing on disk or on the wire changes. Last, pyright itself:
the configured modules type-check with no errors.

  uv run pytest tests/test_types.py
"""
from __future__ import annotations

import dataclasses
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import typing
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import catalog  # noqa: E402
import scores  # noqa: E402
from core import states  # noqa: E402

MODEL = "google/siglip2-base-patch16-256"     # the demo library's embeddings (embed.py's DEFAULT_MODEL)
BOOKKEEPING = ("raw", "model", "demo", "info")


def demo_copy(tmp_path: Path) -> Path:
    db = tmp_path / "estate.db"
    shutil.copyfile(ROOT / "demo" / "estate.db", db)    # never write to demo/
    return db


def load(db: Path, tmp_path: Path, prev=None) -> catalog.Catalog:
    cache = tmp_path / "review"
    return catalog.load(sqlite3.connect(db), MODEL, cache, cache, prev, demo=True)


@pytest.fixture(scope="module")
def demo(tmp_path_factory) -> catalog.Catalog:
    tmp = tmp_path_factory.mktemp("demo")
    return load(demo_copy(tmp), tmp)


def test_catalog_is_a_slotted_dataclass_with_every_field():
    assert dataclasses.is_dataclass(catalog.Catalog)
    names = [f.name for f in dataclasses.fields(catalog.Catalog)]
    assert "__slots__" in vars(catalog.Catalog) and "__dict__" not in vars(catalog.Catalog)
    assert catalog.STATE == tuple(n for n in names if n not in BOOKKEEPING)
    # what the routes read, the nullable three among them; no field is left without a type
    for n in ("shas", "N", "index", "M", "meta", "events", "trips", "has_faces", "when", "ym", "m0", "nmonths",
              "single", "multi", "trip_list", "people_ix", "word_re", "visible", "best", "rank", "score",
              "stack_of", "stack_n", "dup", "fav", "ars", "oldest", "newest", "info"):
        assert n in names, n
    hints = typing.get_type_hints(catalog.Catalog)
    assert set(hints) == set(names)
    for n in ("best", "rank", "score"):
        assert type(None) in typing.get_args(hints[n]), f"{n} must be Optional"
    for n in ("M", "visible", "ars", "stack_of"):
        assert type(None) not in typing.get_args(hints[n]), f"{n} is never None"
    # info is worked out from the rest, not passed in
    assert not next(f for f in dataclasses.fields(catalog.Catalog) if f.name == "info").init


def test_photo_meta_keys():
    req, opt = catalog.PhotoMeta.__required_keys__, catalog.PhotoMeta.__optional_keys__
    assert req == {"ext", "taken", "lat", "lon", "desc", "people", "albums", "fav", "w", "h", "path"}
    assert {"ar", "camera", "lens", "local", "tz", "place", "country", "region", "city", "county", "event",
            "event_title", "trip", "trip_title", "place_inferred"} <= opt


def test_demo_catalog_types(demo: catalog.Catalog):
    c = demo
    assert c.N == len(c.shas) == len(c.index) == c.M.shape[0] > 0
    assert c.M.dtype == np.float32 and c.ars.dtype == np.float32
    for name in ("when", "year", "ym", "event_of", "trip_of", "fav", "shot", "ars", "oldest", "newest",
                 "visible", "stack_of", "stack_n", "dup"):
        a = getattr(c, name)
        assert isinstance(a, np.ndarray) and len(a) == c.N, name
    assert c.visible.dtype == bool and c.dup.dtype == bool and c.stack_of.dtype == np.int64
    assert isinstance(c.word_re, re.Pattern) and isinstance(c.cities, set) and isinstance(c.m0, int)
    # the demo library has no scorers and no faces: the nullable fields are None, the flags false
    assert c.best is None and c.rank is None and c.score is None
    assert c.has_faces is False and c.info["best"] is False and c.info["faces"] is False
    with pytest.raises(ValueError):
        c.scored()
    with pytest.raises(ValueError):
        c.browse_order(c.combine({}), "best")
    assert len(c.browse_order(c.combine({}), "new")) == int(c.visible.sum())
    # every photo's dict has the keys PhotoMeta requires, and `ar` once the catalog is built
    for sha in c.shas:
        m = c.meta[sha]
        assert catalog.PhotoMeta.__required_keys__ <= m.keys() and "ar" in m, sha
        assert set(m) <= catalog.PhotoMeta.__required_keys__ | catalog.PhotoMeta.__optional_keys__, set(m)
    assert c.events and all(set(e) == set(catalog.Event.__annotations__) for e in c.events.values())
    assert c.trips and all(set(t) == set(catalog.Trip.__annotations__) for t in c.trips.values())
    assert all(set(t) == set(catalog.TripCard.__annotations__) for t in c.trip_list)
    json.dumps(c.info)                              # what /api/info sends
    json.dumps([c.feed_row(s) for s in c.shas])     # what the sync feed sends
    with pytest.raises(AttributeError):             # slots: no attribute appears from nowhere
        c.not_a_field = 1                           # type: ignore[attr-defined]


def test_incremental_load_matches_a_full_one(demo: catalog.Catalog, tmp_path):
    db = demo_copy(tmp_path)
    again = load(db, tmp_path, prev=demo.raw)
    assert again.differences(load(db, tmp_path)) == ""


def test_catalog_with_scorers(tmp_path):
    db = demo_copy(tmp_path)
    con = sqlite3.connect(db)
    dim = con.execute("SELECT dim FROM embeddings WHERE model=? LIMIT 1", (MODEL,)).fetchone()[0]
    rng = np.random.default_rng(0)
    scores.save(con, "quality", MODEL, rng.normal(size=dim), 5.0)
    scores.save(con, "taste", MODEL, rng.normal(size=dim), 0.0)
    con.commit()
    con.close()
    c = load(db, tmp_path)
    best, rank, score = c.scored()
    assert c.best is best and c.rank is rank and c.score is score and c.info["best"] is True
    assert sorted(best.tolist()) == list(range(c.N)) and (rank[best] == np.arange(c.N)).all()
    assert score.dtype == np.float64 and len(score) == c.N
    assert len(c.browse_order(c.combine({}), "best-month")) == int(c.visible.sum())


def test_enum_values_are_the_legacy_strings():
    legacy = {
        states.ChangeOp: ("hide", "view"),
        states.ChangeStatus: ("open", "reverted", "done", "discarded"),   # discarded: phase 2 of the multiplayer plan
        states.AlbumKind: ("list", "live"),
        states.SetState: ("empty", "album", "review", "applied", "reverted", "done"),
        states.Decision: ("pending", "accept", "reject"),
        states.TaskStatus: ("queued", "running", "done", "failed", "cancelled"),
        states.IngestState: ("idle", "quiet", "starting", "running", "backoff"),
        states.JobState: ("queued", "running", "waiting", "done", "failed", "cancelled"),
        states.StageState: ("done", "pending", "running", "waiting", "failed"),
        states.ClusterState: ("waiting", "working", "ready", "failed"),
    }
    for enum, values in legacy.items():
        assert tuple(m.value for m in enum) == values, enum
        for m in enum:
            assert m == m.value and isinstance(m, str) and f"{m}" == m.value and hash(m) == hash(m.value)
            assert json.dumps({m: m}) == json.dumps({m.value: m.value})
            assert enum(m.value) is m
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE t(x TEXT)")
    con.execute("INSERT INTO t VALUES (?)", (states.Decision.ACCEPT,))
    assert con.execute("SELECT x, typeof(x) FROM t").fetchone() == ("accept", "text")
    assert states.TASK_ACTIVE == {"queued", "running"}
    assert states.JOB_ACTIVE == {"queued", "running", "waiting"} and states.JOB_RETRYABLE == {"failed", "cancelled"}


def test_modules_keep_their_tuples():
    import changes
    import upload
    from intake import engine
    assert changes.OPS == ("hide", "view") and changes.DECISIONS == ("pending", "accept", "reject")
    assert engine.ACTIVE == {"queued", "running", "waiting"}
    moves = upload.IngestJob.MOVES
    assert set(moves) == set(states.IngestState) and all(set(v) <= set(states.IngestState) for v in moves.values())
    assert moves["idle"] == ("quiet", "running")    # str keys still find their enum members


def test_pyright_is_clean():
    if not shutil.which("pyright", path=str(Path(sys.executable).parent)):
        pytest.skip("pyright is not in this environment (uv sync installs the dev group)")
    r = subprocess.run([str(Path(sys.executable).parent / "pyright"), "--outputjson"], cwd=ROOT,
                       capture_output=True, text=True, timeout=600)
    out = json.loads(r.stdout)
    bad = [f"{d['file']}:{d['range']['start']['line'] + 1}: {d['message']}" for d in out["generalDiagnostics"]
           if d["severity"] == "error"]
    assert not bad and r.returncode == 0, "\n".join(bad[:20])
    assert out["summary"]["filesAnalyzed"] >= 30
