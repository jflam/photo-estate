"""The background loaders of issue #82, made to fail and to race on purpose: core/lifecycle.py's state
machine, the search model (encoder.py) whose load raises or is still going, the People page's loader
(photo_app.PhotoApp.load_people), /api/stats's numbers measured in a thread (admin.py), and the grouping
of unnamed faces (faces.People.cluster). No model, no PyTorch: loaders are fakes, and the interleavings
are forced with threading.Event, never with sleeps. A join's timeout only stops a regression from hanging
the run.

  uv run pytest tests/test_lifecycle.py
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import embed  # noqa: E402
import stats  # noqa: E402
import faces  # noqa: E402
from admin import Admin  # noqa: E402
from catalog import Live  # noqa: E402
from core.lifecycle import MOVES, IllegalMove, Lifecycle, LoadFailed, Phase, StillLoading  # noqa: E402
from core.states import ClusterState  # noqa: E402
from encoder import Encoder, ModelFailed, ModelLoading, drop_image_side  # noqa: E402
from photo_app import PhotoApp  # noqa: E402

GUARD = 10   # seconds: only so that a hang fails the test instead of the run


def in_thread(fn, *args):
    """Run fn in a thread; returns (thread, box) where box gets "value" or "error"."""
    box: dict = {}

    def run():
        try:
            box["value"] = fn(*args)
        except BaseException as e:
            box["error"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, box


def bounded(fn, *args, **kw):
    """fn(*args, **kw), in a thread so that a call that would wait forever fails the test instead."""
    t, box = in_thread(lambda: fn(*args, **kw))
    t.join(GUARD)
    assert not t.is_alive(), f"{fn} is still waiting"
    if "error" in box:
        raise box["error"]
    return box["value"]


# -- the primitive ---------------------------------------------------------------------------------

def test_lifecycle_moves():
    s = Lifecycle("thing")
    assert s.snapshot().phase is Phase.IDLE and s.wait(None) is Phase.IDLE       # nothing in flight: no wait
    with pytest.raises(StillLoading, match="not started"):
        s.require(None)
    for bad in (s.succeed, lambda: s.fail(ValueError())):                         # idle -> ready|failed
        with pytest.raises(IllegalMove):
            bad()
    s.begin()
    with pytest.raises(IllegalMove):                                              # two loads at once
        s.begin()
    with pytest.raises(StillLoading, match="still loading") as e:
        s.require(0)
    assert e.value.status == 503 and s.wait(0) is Phase.LOADING
    s.succeed("v1")
    assert s.snapshot().phase is Phase.READY and s.require(0) == "v1" and s.done.is_set()
    with pytest.raises(IllegalMove):
        s.succeed("again")
    s.begin()                                         # a reload: the last good value is kept meanwhile
    assert not s.done.is_set() and s.snapshot().value == "v1"
    s.fail(OSError("table gone"))
    snap = s.snapshot()
    assert snap.phase is Phase.FAILED and snap.value == "v1" and snap.error == "OSError: table gone" and snap.at
    with pytest.raises(LoadFailed, match="thing failed to load: OSError: table gone") as e:
        s.require(None)                               # a failed load ends every wait at once
    assert e.value.status == 500
    with pytest.raises(IllegalMove):
        s.fail(OSError())
    s.begin()
    s.succeed("v2")
    assert s.snapshot().error is None and s.require(0) == "v2"
    s.release()                                       # let go: idle again, nothing to wait for, loadable again
    assert s.snapshot().phase is Phase.IDLE and s.snapshot().value is None and s.wait(None) is Phase.IDLE
    with pytest.raises(IllegalMove):
        s.release()
    s.begin()
    s.succeed("v3")
    assert s.require(0) == "v3"
    assert set(MOVES) == set(Phase) and all(MOVES[p] for p in Phase)


def test_a_waiter_is_released_by_a_failure():
    s = Lifecycle("thing")
    s.begin()
    t, box = in_thread(s.require, None)               # waits with no timeout
    s.fail(RuntimeError("boom"))
    t.join(GUARD)
    assert not t.is_alive() and isinstance(box["error"], LoadFailed)


# -- the search model ------------------------------------------------------------------------------

class FailingEncoder(Encoder):
    def _open(self):
        raise RuntimeError("no such model in the cache")


class HeldEncoder(Encoder):
    """A load that goes on until the test says so."""
    def __init__(self):
        super().__init__("fake/model", 1)
        self.release = threading.Event()

    def _open(self):
        self.release.wait()
        raise RuntimeError("released")


def test_encoder_whose_load_raises_fails_fast():
    enc = FailingEncoder("fake/model", 1)
    enc.start()
    for call in (lambda: enc.text("a dog"), lambda: enc.side, lambda: enc.image(None)):
        with pytest.raises(ModelFailed, match="no such model in the cache"):
            bounded(call)                    # not a request that waits forever for a model that failed to load
    assert enc.state.snapshot().phase is Phase.FAILED


def test_encoder_still_loading_is_a_bounded_wait():
    enc = HeldEncoder()
    enc.patience = 0
    enc.start()
    with pytest.raises(ModelLoading):
        bounded(enc.text, "a dog")
    with pytest.raises(ModelLoading):
        bounded(enc.wait, 0)
    enc.release.set()
    assert enc.state.wait(GUARD) is Phase.FAILED
    with pytest.raises(ModelFailed):
        bounded(enc.text, "a dog")


class KeptWhileUsed(Encoder):
    """An encoder with no model in it: _open counts, and text() only does what the real one does first."""
    def __init__(self, idle=None):
        super().__init__("fake/model", 1, idle=idle)
        self.opened = 0

    def _open(self):
        self.opened += 1
        return {"model": object(), "dev": "cpu"}

    def text(self, q):
        self.wait(self.patience)
        with self.lock:
            self._held()
        return q


def until(what, seconds=GUARD):
    end = time.time() + seconds
    while time.time() < end:
        if what():
            return True
        time.sleep(0.01)
    return False


def test_encoder_is_loaded_when_first_asked_for_and_once():
    enc = KeptWhileUsed()
    assert enc.state.snapshot().phase is Phase.IDLE and enc.opened == 0        # made, not loaded
    with pytest.raises(ModelLoading, match="not started"):
        bounded(enc.text, "a dog")                                              # and a use of it does not wait
    assert enc.ensure() is True and enc.ensure() is False                       # two requests: one load
    assert enc.state.wait(GUARD) is Phase.READY and enc.opened == 1
    assert enc.ensure() is False and bounded(enc.text, "a dog") == "a dog"
    failed = FailingEncoder("fake/model", 1)
    failed.start()
    assert failed.state.wait(GUARD) is Phase.FAILED and failed.ensure() is False   # a failure is not retried by asking


def test_encoder_is_let_go_when_unused_and_loads_again():
    enc = KeptWhileUsed(idle=0.3)
    enc.ensure()
    assert enc.state.wait(GUARD) is Phase.READY
    for _ in range(12):                                # in use for twice the idle time: kept
        assert bounded(enc.text, "a dog") == "a dog"
        time.sleep(0.05)
    assert enc.state.snapshot().phase is Phase.READY and enc.opened == 1
    assert until(lambda: enc.state.snapshot().phase is Phase.IDLE), "not let go after its idle time"
    assert enc.parts == {}
    with pytest.raises(ModelLoading):
        bounded(enc.text, "a dog")
    assert enc.ensure() is True and enc.state.wait(GUARD) is Phase.READY and enc.opened == 2
    assert bounded(enc.text, "a dog") == "a dog"
    kept = KeptWhileUsed()                             # no idle time: never let go by itself
    kept.ensure()
    assert kept.state.wait(GUARD) is Phase.READY
    time.sleep(0.4)
    assert kept.state.snapshot().phase is Phase.READY and kept.let_go(3600) is False and kept.let_go() is True


def test_a_use_that_meets_a_model_just_let_go_is_a_503_not_a_crash():
    enc = KeptWhileUsed()
    enc.ensure()
    assert enc.state.wait(GUARD) is Phase.READY
    with enc.lock:                                     # as let_go() leaves it, seen by a text() that had already waited
        enc.parts = {}
    with pytest.raises(ModelLoading, match="not loaded just now"):
        bounded(enc.text, "a dog")


def test_image_side_is_dropped_only_from_a_model_that_has_one():
    class Two:
        vision_model, text_model = object(), object()
    m = Two()
    assert drop_image_side(m) is True and m.vision_model is None and m.text_model is not None
    assert drop_image_side(m) is False and drop_image_side(object()) is False


@pytest.fixture(scope="module")
def app():
    a = embed.parser().parse_args(["serve", "--demo", "--no-pregenerate"])
    return PhotoApp(a)                 # start() is not called: no model, no faces, no threads


def test_search_answers_503_then_500(app, monkeypatch):
    held = HeldEncoder()
    held.patience = 0
    held.start()
    monkeypatch.setattr(app.browse, "encoder", held)
    r = bounded(app.request, "GET", "/api/search?q=a+dog+in+the+snow&limit=3")
    assert r.status == 503 and "still loading" in r.json()["error"] and r.headers["Retry-After"] == "5"
    failed = FailingEncoder("fake/model", 1)
    failed.start()
    monkeypatch.setattr(app.browse, "encoder", failed)
    app.browse.forget()
    r = bounded(app.request, "GET", "/api/search?q=a+dog+in+the+snow&limit=3")
    assert r.status == 500 and "no such model in the cache" in r.json()["error"]
    # an agent's search is told so as a tool error, not a broken connection
    got = bounded(app.request, "POST", "/mcp", body={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                    "params": {"name": "search_photos", "arguments": {"query": "a dog"}}})
    res = got.json()["result"]
    assert got.status == 200 and res["isError"] and "failed to load" in res["content"][0]["text"]
    held.release.set()
    assert app.request("GET", "/api/search?limit=3").status == 200     # browsing needs no model


# -- the People page's loader ----------------------------------------------------------------------

class StubPeople:
    def __init__(self, *a, **k):
        pass

    def handle(self, method, path, qs, body, actor=None, keep=None, see=None):   # actor, keep, see: as faces.People.handle takes them
        return 200, b'{"ok":true}', "application/json", "no-store"


class BrokenPeople:
    def __init__(self, *a, **k):
        raise MemoryError("out of memory reading faces")


def test_people_loader_failure_is_reported(app, monkeypatch):
    monkeypatch.setattr(app.live.cat, "has_faces", True)
    monkeypatch.setattr(app, "people", Lifecycle("the People page"))
    assert app.people_state() == "loading" and app.request("GET", "/api/people").status == 503
    monkeypatch.setattr(faces, "People", BrokenPeople)
    app.load_people()                                  # what start()'s thread runs: it must not raise
    assert app.people_state() == "failed"
    r = app.request("GET", "/api/people")
    assert r.status == 500 and b"MemoryError: out of memory reading faces" in r.body
    page = app.request("GET", "/people")
    assert page.status == 500 and b"Faces could not be loaded" in page.body and b'http-equiv="refresh"' not in page.body
    assert app.admin.faces_ready() is False                              # /api/stats's faces_ready
    monkeypatch.setattr(faces, "People", StubPeople)   # loaded again (after an ingest): ready
    app.load_people()
    assert app.people_state() == "ready" and app.request("GET", "/api/people").status == 200
    monkeypatch.setattr(faces, "People", BrokenPeople)  # a reload that fails: the loaded one keeps answering
    app.load_people()
    assert app.people_state() == "ready" and app.request("GET", "/api/people").status == 200
    assert app.people.snapshot().error == "MemoryError: out of memory reading faces"


# -- /api/stats ------------------------------------------------------------------------------------

class SpyLock:
    """A Lock that says when someone has to wait for it."""
    def __init__(self, factory=threading.Lock):
        self.inner, self.contended = factory(), threading.Event()

    def __enter__(self):
        if not self.inner.acquire(blocking=False):
            self.contended.set()
            self.inner.acquire()

    def __exit__(self, *exc):
        self.inner.release()


class PausingDict(dict):
    """st, pausing the writer between setting "disk" and "at"."""
    def __init__(self, d):
        super().__init__(d)
        self.mid, self.go = threading.Event(), threading.Event()

    def __setitem__(self, k, v):
        super().__setitem__(k, v)
        if k == "disk" and v == "NEW":
            self.mid.set()
            self.go.wait()


def make_admin(tmp_path, monkeypatch) -> Admin:
    adm = Admin(Live(object()), None, None, None, db=str(tmp_path / "estate.db"), model="m", store=tmp_path,  # type: ignore[arg-type]
                cache=tmp_path, seed=tmp_path, inbox=None, ingest=None, query_log=tmp_path / "q.jsonl", demo=True,
                faces_ready=lambda: False)
    monkeypatch.setattr(adm, "library_stats", lambda: {"photos": 1})
    monkeypatch.setattr(adm, "assets_now", lambda: None)
    return adm


def test_stats_snapshot_is_consistent(tmp_path, monkeypatch):
    adm = make_admin(tmp_path, monkeypatch)
    adm.st = st = PausingDict(dict(adm.st, disk="OLD", at=1.0))
    adm.st_lock = lock = SpyLock()
    finished = threading.Event()
    job = adm.stats_job
    monkeypatch.setattr(adm, "stats_job", lambda: (job(), finished.set()))
    monkeypatch.setattr(stats, "disk_stats", lambda *a: "NEW")
    assert adm.stats_payload(refresh=True)["state"] == "working"   # starts the job
    assert st.mid.wait(GUARD)                                      # the job is between "disk" and "at"
    moved = lock.contended = threading.Event()   # the reader has to wait for the lock (fixed), or has read st as it is
    reader, box = in_thread(lambda: (adm.stats_payload(False), moved.set())[0])
    assert moved.wait(GUARD)
    st.go.set()
    reader.join(GUARD)
    assert finished.wait(GUARD) and not reader.is_alive()
    got = box["value"]
    assert (got["disk"] == "NEW") == (got["computed_at"] not in (None, 1.0)), f"half-written: {got}"
    after = adm.stats_payload(False)
    assert after["state"] == "ready" and after["disk"] == "NEW" and after["error"] is None


def test_stats_job_failure_and_the_gate(tmp_path, monkeypatch):
    adm = make_admin(tmp_path, monkeypatch)
    readers = []

    def library_stats():
        readers.append(adm.live.gate.readers)    # the job reads the catalog inside the gate...
        adm.forget_library()                     # ...and a reload swaps a new one in meanwhile
        return {"photos": "stale"}
    monkeypatch.setattr(adm, "library_stats", library_stats)
    monkeypatch.setattr(stats, "disk_stats", lambda *a: 1 / 0)
    adm.st.update(running=True, lib=None)
    adm.stats_job()
    assert readers == [1], "library_stats read the catalog outside the gate"
    assert adm.st["lib"] is None, "a library count from the catalog before the reload was kept"
    assert adm.st["running"] is False and adm.st["error"] == "ZeroDivisionError: division by zero"


# -- grouping the unnamed faces --------------------------------------------------------------------

class RecordingLock:
    """People.lock (an RLock) that notes the clustering state each time it is taken and let go."""
    def __init__(self, people):
        self.inner, self.people, self.log = threading.RLock(), people, []

    def __enter__(self):
        self.inner.acquire()
        self.log.append(("in", self.people.clusters_state))

    def __exit__(self, *exc):
        self.log.append(("out", self.people.clusters_state))
        self.inner.release()


def bare_people(n=5) -> faces.People:
    p = faces.People.__new__(faces.People)       # no catalog: just what cluster() and unknown() read
    p.np, p.persons, p.n = np, {}, n
    p.pid, p.sug_score = np.full(n, -1, np.int32), np.zeros(n, np.float32)
    p.score, p.px, p.year = np.full(n, 0.9, np.float32), np.full(n, 100, np.int32), np.zeros(n, np.int32)
    p.src = np.zeros(n, np.int8)
    p.V = np.tile(np.array([[1, 0, 0, 0]], np.float32), (n, 1))     # five of one face: one group
    p.clusters, p.clusters_state, p.clusters_error = [], ClusterState.WAITING, None
    p.lock = RecordingLock(p)
    return p


def test_cluster_swaps_under_the_lock():
    p = bare_people()
    p.cluster()
    assert p.clusters_state == ClusterState.READY and [list(c) for c in p.clusters] == [[0, 1, 2, 3, 4]]
    log = p.lock.log
    assert log[0] == ("in", ClusterState.WAITING) and log[1] == ("out", ClusterState.WORKING), log
    i = next(i for i, e in enumerate(log) if e[1] == ClusterState.READY)
    assert log[i - 1:i + 1] == [("in", ClusterState.WORKING), ("out", ClusterState.READY)], log


def test_cluster_failure_is_reported():
    p = bare_people()
    p.px = None                                  # a bad index: the grouping raises
    p.cluster()                                  # in its own thread in the app: it must not raise
    assert p.clusters_state == ClusterState.FAILED and p.clusters_error.startswith("TypeError")
    got = p.unknown(14)
    assert got["state"] == "failed" and got["groups"] == [] and got["error"] == p.clusters_error
