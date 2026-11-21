"""/compare in process (photo_app.PhotoApp on the demo library, start() not called: no search model), so it
runs in CI's fast tier: the routes exist only with vectors, a vote takes the write rule (--upload-user or
this computer), bad numbers are a 400, and a search answers 503 while the second model is not loaded
instead of waiting with the catalog gate held. tests/test_compare.py runs the real thing with the model.
  uv run pytest tests/test_compare_app.py
"""
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import embed  # noqa: E402
from photo_app import PhotoApp  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MODEL = "google/siglip2-base-patch16-256"
WRITER, VIEWER = {"Tailscale-User-Login": "writer@example.com"}, {"Tailscale-User-Login": "viewer@example.com"}


def vectors(folder: Path) -> Path:
    """The demo catalog's own vectors, as tools/embed_squares.py writes a second model's."""
    folder.mkdir()
    con = sqlite3.connect(f"file:{ROOT / 'demo' / 'estate.db'}?mode=ro", uri=True)
    rows = sorted(con.execute("SELECT sha256, vec FROM embeddings WHERE model=? AND vec IS NOT NULL", (MODEL,)))
    con.close()
    np.save(folder / "shard-0000.npy", np.stack([np.frombuffer(v, np.float16) for _, v in rows]))
    (folder / "shard-0000.shas.txt").write_text("".join(f"{s}\n" for s, _ in rows))
    (folder / "meta.json").write_text(json.dumps({"model": MODEL}))
    return folder


def make(*extra) -> PhotoApp:
    return PhotoApp(embed.parser().parse_args(["serve", "--demo", "--no-pregenerate", *extra]))


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("compare")
    return stubbed(make("--compare-vectors", str(vectors(tmp / "vectors")), "--compare-votes", str(tmp / "votes.jsonl")))


def stubbed(app: PhotoApp) -> PhotoApp:
    """No model: the second model's loads are counted and never end (so it is "loading"), and so is each
    time it is let go."""
    app.loads, app.let_go = [], []
    app.encoder_b.start = lambda: (app.loads.append(1), app.encoder_b.state.begin())
    app.encoder_b.let_go = lambda *a: (app.let_go.append(1), True)[1]
    return app


def test_routes_only_with_vectors(tmp_path):
    for extra in ([], ["--compare-vectors", str(tmp_path / "missing")]):   # the app still starts without them
        bare = make(*extra)
        assert bare.compare is None
        for path in ("/compare", "/api/compare/info", "/api/compare?q=dog"):
            assert bare.request("GET", path).status == 404, (extra, path)
        assert bare.request("POST", "/api/compare/vote", body={"q": "dog", "winner": "a"}).status == 404


def test_second_model_is_loaded_by_a_run_not_at_start_nor_by_looking(tmp_path):
    """It was loaded at every start and held all day: 3 GB or more of the Mac mini's 16 GB for an experiment.
    Opening /compare only looks (its page asks /api/compare/info); starting a run is what loads it, once."""
    app = stubbed(make("--compare-vectors", str(vectors(tmp_path / "vectors"))))
    assert app.encoder_b.idle == 15 * 60 and app.loads == []
    info = app.request("GET", "/api/compare/info").json()
    assert info["b"]["ready"] is False and info["run"] is None and info["reports"] == [] and app.loads == []
    assert app.request("POST", "/api/compare/run", body={}).status == 200 and app.loads == [1]
    app.request("POST", "/api/compare/run", body={})
    assert app.request("GET", "/api/compare?q=a%20dog").status == 503 and app.loads == [1]


def test_the_library_menu_is_told_the_experiment_is_on(app):
    assert app.request("GET", "/api/info").json()["compare"] is True
    assert "compare" not in make().request("GET", "/api/info").json()


def test_terms_file(tmp_path):
    from compare import read_terms
    default = read_terms()
    assert len(default) >= 20 and len(set(default)) == len(default) and not any(t.startswith("#") for t in default)
    assert read_terms(tmp_path) == default                     # no terms.txt beside the vectors: the app's own list
    (tmp_path / "terms.txt").write_text("# mine\n\n a red kite \nsnow\na red kite\n")
    assert read_terms(tmp_path) == ["a red kite", "snow"]


def test_sign_test_and_verdict():
    from compare import report, sign_test
    assert sign_test(0, 0) == 1.0 and sign_test(5, 5) == 1.0 and sign_test(3, 4) == 1.0
    assert abs(sign_test(9, 1) - 22 / 1024) < 1e-9 and sign_test(1, 9) == sign_test(9, 1)   # 2 * (C(10,9) + C(10,10)) / 2^10
    run = lambda **w: report({"id": "x", "started": "s", "a": "g/base", "b": "g/large", "k": 24, "terms": list(w),
                              "answers": {t: {"winner": v, "overlap": 12, "a_ms": 20.0, "b_ms": 60.0} for t, v in w.items() if v}})
    r = run(**{f"b{i}": "b" for i in range(9)}, a0="a", s0="same", k0="skip", n0=None)
    assert r["count"] == {"a": 1, "b": 9, "same": 1, "skip": 1} and r["unanswered"] == 1 and r["p"] == 0.0215
    assert r["verdict"].startswith("large was better on 9 of 11 searches, base on 1, and 1 were the same: a clear difference")
    assert (r["overlap"], r["a_ms"], r["b_ms"]) == (12.0, 20.0, 60.0) and [x["winner"] for x in r["rows"]][-1] is None
    assert run(x="a", y="b")["verdict"].startswith("No difference") and run(x="skip")["verdict"] == "No search was judged."
    assert "it leans that way" in run(x="a", y="a", z="b")["verdict"]


def test_a_run_from_start_to_report(tmp_path):
    """Every search of the list, once; an answer given again replaces the first; finishing needs them all
    (or early), writes the report into the run and beside it, and lets the second model go."""
    vec = vectors(tmp_path / "vectors")
    (vec / "terms.txt").write_text("a dog\na cat\nsnow\na boat\n")
    app = stubbed(make("--compare-vectors", str(vec)))
    post = lambda path, body: app.request("POST", path, body=body)
    run = post("/api/compare/run", {}).json()
    assert run["terms"] == ["a dog", "a cat", "snow", "a boat"] and run["k"] == 24 and run["answers"] == {}
    assert post("/api/compare/run", {}).json()["id"] == run["id"], "asked again: the same run"
    vote = lambda q, w, **more: post("/api/compare/vote", {"run": run["id"], "q": q, "winner": w, "left": "b", "k": 24,
                                                           "overlap": 10, "a_ms": 20.5, "b_ms": 61, **more})
    assert vote("a dog", "b").json() == {"ok": True, "votes": {"a": 0, "b": 1, "same": 0}, "done": 1, "of": 4}
    assert vote("a dog", "a").json()["votes"] == {"a": 1, "b": 0, "same": 0}, "answered again: counted once"
    assert vote("a cat", "b").status == 200 and vote("snow", "skip").json()["votes"] == {"a": 1, "b": 1, "same": 0}
    assert vote("a bird", "a").status == 400 and vote("a cat", "left").status == 400
    assert post("/api/compare/vote", {"run": "20200101-000000", "q": "a cat", "winner": "a"}).status == 404
    assert post("/api/compare/vote", {"q": "anything", "winner": "skip"}).status == 400     # skip is a run's answer only
    assert app.request("GET", "/api/compare/info").json()["run"]["answers"] == {"a dog": "a", "a cat": "b", "snow": "skip"}

    early = post("/api/compare/run/finish", {"run": run["id"]})
    assert early.status == 409 and early.json()["left"] == ["a boat"] and app.let_go == []
    assert post("/api/compare/run/finish", {"run": "nope"}).status == 404
    assert vote("a boat", "same").status == 200
    rep = post("/api/compare/run/finish", {"run": run["id"]}).json()
    assert rep["count"] == {"a": 1, "b": 1, "same": 1, "skip": 1} and rep["unanswered"] == 0 and rep["verdict"].startswith("No difference")
    assert [(x["term"], x["winner"], x["overlap"], x["a_ms"]) for x in rep["rows"]][0] == ("a dog", "a", 10, 20.5)
    assert app.let_go == [1], "the experiment is over: its model is let go"

    runs = vec / "runs"
    saved = json.loads((runs / f"run-{run['id']}.json").read_text())
    assert saved["finished"] and saved["report"]["count"] == rep["count"]
    md = (runs / f"{run['id']}.md").read_text()
    assert md.startswith(f"# Model comparison {run['id']}") and "| a dog | google/siglip2-base-patch16-256 |" in md
    info = app.request("GET", "/api/compare/info").json()
    assert info["run"] is None and [r["id"] for r in info["reports"]] == [run["id"]]
    assert app.request("GET", f"/api/compare/report?run={run['id']}").json() == rep
    assert app.request("GET", "/api/compare/report?run=../../etc/passwd").status == 404
    assert post("/api/compare/run/finish", {"run": run["id"]}).status == 404, "finished once"
    votes = [json.loads(line) for line in (vec / "votes.jsonl").read_text().splitlines()]
    assert [v["winner"] for v in votes] == ["b", "a", "b", "same"] and all(v["run"] == run["id"] for v in votes), "a skip is not a vote"


def test_a_run_left_is_carried_on_after_a_restart_and_can_be_given_up(tmp_path):
    vec = vectors(tmp_path / "vectors")
    (vec / "terms.txt").write_text("a dog\na cat\n")
    first = stubbed(make("--compare-vectors", str(vec)))
    run = first.request("POST", "/api/compare/run", body={}).json()
    first.request("POST", "/api/compare/vote", body={"run": run["id"], "q": "a dog", "winner": "b"})
    (vec / "terms.txt").write_text("a dog\na cat\nsnow\n")      # the list changes: a run keeps the list it began with

    again = stubbed(make("--compare-vectors", str(vec)))           # the app restarted
    assert again.request("GET", "/api/compare/info").json()["run"]["answers"] == {"a dog": "b"} and again.loads == []
    same = again.request("POST", "/api/compare/run", body={}).json()
    assert same["id"] == run["id"] and same["terms"] == ["a dog", "a cat"] and again.loads == [1]
    early = again.request("POST", "/api/compare/run/finish", body={"run": run["id"], "early": True}).json()
    assert early["count"]["b"] == 1 and early["unanswered"] == 1

    again.request("POST", "/api/compare/run", body={})             # (in the same second as the last: its own id all the same)
    fresh = again.request("POST", "/api/compare/run", body={"fresh": True}).json()
    assert fresh["id"] != same["id"] and fresh["terms"] == ["a dog", "a cat", "snow"] and fresh["answers"] == {}
    assert len(again.request("GET", "/api/compare/info").json()["reports"]) == 1, "a run given up has no report"


def test_info_and_page(app):
    info = app.request("GET", "/api/compare/info").json()
    assert info["b"]["photos"] == info["both"] == app.live.cat.N and info["b"]["ready"] is False
    page = app.request("GET", "/compare")
    assert page.status == 200 and b"Compare models" in page.body and page.etag


def test_search_while_loading_and_bad_input(app):
    r = app.request("GET", "/api/compare?q=a%20dog")
    assert r.status == 503 and "loading" in r.json()["error"]
    assert app.request("GET", "/api/compare?q=a%20dog&k=many").status == 400
    assert app.request("GET", "/api/compare?q=").status == 400            # no words to rank by


def test_vote_takes_the_write_rule(app, monkeypatch):
    monkeypatch.setattr(app.a, "upload_user", ["writer@example.com"])
    vote = {"q": "a dog", "winner": "b", "left": "a", "k": 48}
    assert app.request("POST", "/api/compare/vote", VIEWER, body=vote, local=False).status == 403
    r = app.request("POST", "/api/compare/vote", WRITER, body=vote, local=False)
    assert r.status == 200 and r.json()["votes"]["b"] == 1
    assert app.request("POST", "/api/compare/vote", WRITER, body={"q": "x", "winner": "left"}, local=False).status == 400
    assert app.request("POST", "/api/compare/vote", WRITER, body=b"{not json", local=False).status == 400
    rec = [json.loads(line) for line in Path(app.compare.votes).read_text().splitlines()]
    assert rec[-1]["winner"] == "b" and rec[-1]["who"] == "writer@example.com"
