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
    app = make("--compare-vectors", str(vectors(tmp / "vectors")), "--compare-votes", str(tmp / "votes.jsonl"))
    app.loads = []                                   # the second model's loads: begun (so "loading"), never ended
    app.encoder_b.start = lambda: (app.loads.append(1), app.encoder_b.state.begin())
    return app


def test_routes_only_with_vectors(tmp_path):
    for extra in ([], ["--compare-vectors", str(tmp_path / "missing")]):   # the app still starts without them
        bare = make(*extra)
        assert bare.compare is None
        for path in ("/compare", "/api/compare/info", "/api/compare?q=dog"):
            assert bare.request("GET", path).status == 404, (extra, path)
        assert bare.request("POST", "/api/compare/vote", body={"q": "dog", "winner": "a"}).status == 404


def test_second_model_is_loaded_by_the_first_use_not_at_start(app):
    """It was loaded at every start and held all day: 3 GB or more of the Mac mini's 16 GB for an experiment.
    Opening /compare (its page asks /api/compare/info) is what loads it now, once."""
    assert app.encoder_b.idle == 15 * 60 and app.loads == []
    info = app.request("GET", "/api/compare/info").json()
    assert info["b"]["ready"] is False and app.loads == [1]
    app.request("GET", "/api/compare/info")
    assert app.request("GET", "/api/compare?q=a%20dog").status == 503 and app.loads == [1]


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
