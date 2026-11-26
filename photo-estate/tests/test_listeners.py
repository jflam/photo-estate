"""The journal's listeners (multiplayer phase 7: listeners.py). First the rules on their own, with a stand-in for
the app (listeners.Env): a cursor moves past what is handled and stops at a WAIT; a listener run twice over the
same entries makes nothing twice. Then end to end on the demo library, in process and served on a free port, with
a stand-in for Claude Code (as tests/test_tasks.py has) as the estate's agent: new photos ingested, a family series
kept up to date by the agent and a member's own series waiting for its owner in the inbox; and an answer to a
question the agent asked, which wakes a new run with the answer.

  uv run pytest tests/test_listeners.py
"""
from __future__ import annotations

import argparse
import io
import json
import socket
import sqlite3
import stat
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import embed  # noqa: E402
import estate  # noqa: E402
import journal  # noqa: E402
import listeners  # noqa: E402
import members  # noqa: E402
import stories  # noqa: E402
from core import schema  # noqa: E402
from photo_app import PhotoApp  # noqa: E402
from test_multiplayer import ADA, BEN, CLEO, call, mid  # noqa: E402

MODEL = embed.DEFAULT_MODEL


# -- the rules, with a stand-in for the app ------------------------------------------------------------

@pytest.fixture
def scratch(tmp_path):
    con = sqlite3.connect(tmp_path / "estate.db")
    schema.ensure(con, *schema.CATALOG_AREAS)
    ada, ben, _ = members.demo(con)
    agent = members.agent(con)
    shas = [f"{i:064x}" for i in range(1, 7)]
    con.executemany("INSERT INTO assets(sha256, ext, kind) VALUES (?, '.jpg', 'image')", ((s,) for s in shas))
    t = journal.now()
    # Ben's own series (grows from a search), a family series the archivist keeps, and a live album (no series)
    for sid, owner, grows, kind in ((1, ben.id, '{"like": "x"}', "list"), (2, agent.id, '{"like": "y"}', "list"),
                                    (3, ben.id, None, "live")):
        con.execute("INSERT INTO change_sets(id, title, op, created_at, kind, owner, grows_from, query) VALUES"
                    " (?, ?, 'view', ?, ?, ?, ?, ?)", (sid, f"series {sid}", t, kind, owner, grows,
                                                       '{"like": "z"}' if kind == "live" else None))
    con.execute("INSERT INTO lenses(owner, title, kind, set_id, published, maintained_by) VALUES (?, 's2', 'series', 2, 1, ?)",
                (agent.id, agent.id))
    con.execute("INSERT INTO change_items(set_id, sha256) VALUES (1, ?)", (shas[3],))   # already in Ben's series
    con.commit()
    listeners.begin(con)           # the members made above are history: each listener starts after them
    yield con, ada, ben, agent, shas
    con.close()


class Stand:
    """A stand-in for the app: what the series' searches find, what the catalog served was read after, and the runs
    the listeners start (recorded as agent_tasks rows, as tasks.Tasks.submit makes them)."""

    def __init__(self, con, found, loaded=10**9):
        self.con, self.found, self.at, self.runs = con, found, loaded, []

    def env(self) -> listeners.Env:
        def submit(prompt, acting_for, by):
            self.runs.append((prompt, acting_for, by))
            self.con.execute("INSERT INTO agent_tasks(prompt, status, created_at, acting_for, started_by) VALUES (?, 'queued', '', ?, ?)",
                             (prompt, acting_for, by))
            self.con.commit()
            return 1
        return listeners.Env(find=lambda search, m: self.found.get(search.get("like"), []), loaded=lambda: self.at,
                             in_catalog=lambda sha: True, refused=lambda sha, m: False, submit=submit)


def ingested(con, rows) -> int:
    with con:
        return journal.append(con, None, "ingest.finished", "source:iphone", {"new": 3, "assets": rows})


def test_the_series_listener_waits_for_the_reload_then_asks_the_right_one(scratch):
    con, ada, ben, agent, shas = scratch
    e = ingested(con, [3, 6])                       # rows 4-6 are new: shas[3:6]
    stand = Stand(con, {"x": [shas[0], shas[3], shas[4]], "y": [shas[5], shas[1]], "z": shas})
    stand.at = e - 1                                # the catalog served was read before the ingest ended
    start = listeners.cursor(con, "series")
    assert listeners.run(con, stand.env()) == {"series": 0, "stories": 1}
    assert listeners.cursor(con, "series") == start and listeners.cursor(con, "stories") == e
    stand.at = e
    assert listeners.run(con, stand.env()) == {"series": 1, "stories": 1}   # (stories: the request it wrote)
    # Ben's own series: the new photo it finds and does not hold, in the inbox, and the entry names it
    req = [x for x in journal.since(con) if x["kind"] == "series.update_requested"]
    assert [(x["actor"], x["acting_for"], x["subject"], x["data"]) for x in req] == \
        [(None, ben.id, "album:1", {"ingest": e, "photos": [shas[4]]})]
    assert con.execute("SELECT member_id, kind, ref, title FROM inbox_items").fetchall() == \
        [(ben.id, "series_update", "album:1", "1 new photo may belong in your series “series 1”")]
    # the family series: a run of the estate's agent acting for no one, naming the new photo only
    assert [(af, by) for _, af, by in stand.runs] == [(None, f"journal:{e}/album:2")]
    assert shas[5] in stand.runs[0][0] and shas[1] not in stand.runs[0][0]
    # at least once: the same entries again make nothing new
    con.execute("UPDATE listener_cursors SET journal_id=0")
    con.commit()
    listeners.run(con, stand.env())
    assert len([x for x in journal.since(con) if x["kind"] == "series.update_requested"]) == 1
    assert con.execute("SELECT count(*) FROM inbox_items").fetchone()[0] == 1 and len(stand.runs) == 1
    # a second ingest adds to the open item; an entry from before (no `assets`) is passed over
    e2 = ingested(con, [5, 6])
    stand.found["x"] = [shas[5]]
    with con:
        journal.append(con, None, "ingest.finished", "source:old", {"new": 2})
    stand.at = e2 + 1                               # reloaded since
    listeners.run(con, stand.env())
    assert con.execute("SELECT title FROM inbox_items").fetchone()[0] == "2 new photos may belong in your series “series 1”"
    assert listeners.cursor(con, "series") == e2 + 1
    listeners.begin(con)                            # (a cursor that exists is left as it is)
    assert listeners.cursor(con, "series") == e2 + 1


def test_the_stories_listener_wakes_a_run_only_for_an_answer_the_agent_wanted(scratch):
    con, ada, ben, agent, shas = scratch
    no = lambda s, m: False      # noqa: E731
    wanted = stories.ask(con, agent.id, ben.id, [shas[0]], "Who is this?", "for Ben's album", to=ben.id, follow_up=True,
                         refused=no)["question"]
    unwanted = stories.ask(con, agent.id, ben.id, [shas[1]], "And this?", to=ben.id, refused=no)["question"]
    by_ada = stories.ask(con, ada.id, None, [shas[2]], "Where?", to=ben.id, follow_up=True, refused=no)["question"]
    stories.deal(con, ben.id, refused=no)
    told = [stories.answer(con, ben.id, q, no, text=f"answer {q}") for q in (wanted, unwanted, by_ada)]
    spoken = stories.tell(con, ben.id, [shas[0]], no, recording=("ab" * 32, ".m4a"), question=wanted)
    stand = Stand(con, {})
    listeners.run(con, stand.env())
    assert [(af, by.split("/")[1]) for _, af, by in stand.runs] == [(ben.id, f"story:{told[0]['id']}")], \
        "only the answer to the agent's question that wanted one, and a recording waits for its transcript"
    prompt = stand.runs[0][0]
    assert "Ben has answered" in prompt and "“answer 1”" in prompt and "for Ben's album" in prompt and shas[0] in prompt
    stories.transcribed(con, spoken["id"], text="it was grandma")
    listeners.run(con, stand.env())
    assert [by.split("/")[1] for _, _, by in stand.runs] == [f"story:{told[0]['id']}", f"story:{spoken['id']}"]
    assert "(spoken, as transcribed:) it was grandma" in stand.runs[1][0]
    con.execute("UPDATE listener_cursors SET journal_id=0")
    con.commit()
    listeners.run(con, stand.env())
    assert len(stand.runs) == 2, "once per story"


# -- end to end, with a stand-in for Claude Code ----------------------------------------------------------

FAKE = r'''#!/usr/bin/env python3
import json, re, sys, urllib.request
argv = sys.argv[1:]
arg = lambda flag: argv[argv.index(flag) + 1]
out = lambda o: print(json.dumps(o), flush=True)
prompt, conf = arg("-p"), json.loads(arg("--mcp-config"))["mcpServers"]["photo-estate"]
if arg("--tools") != "" or "estate's agent" not in arg("--system-prompt"):
    sys.exit("not started as a listener's run is")
def tool(name, args):
    req = urllib.request.Request(conf["url"], json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                 "params": {"name": name, "arguments": args}}).encode(), {"Content-Type": "application/json", **conf["headers"]})
    got = json.load(urllib.request.urlopen(req, timeout=60))["result"]
    out({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "mcp__photo-estate__" + name, "input": args}]}})
    return got.get("structuredContent")
out({"type": "system", "subtype": "init", "mcp_servers": [{"name": "photo-estate", "status": "connected"}]})
if "family series" in prompt:          # it looks, and adds the first of the new photos only
    album = int(re.search(r"\(album (\d+)\)", prompt)[1])
    new = prompt.split("The new photos: ")[1].split(", ")
    tool("get_photos", {"ids": new})
    tool("amend_album", {"album": album, "add_ids": new[:1], "reason": "the same lake"})
    said = f"added 1 of {len(new)}"
elif "has answered a question" in prompt:
    said = "read the answer: " + prompt.split("in their words: ")[1].split("\n")[0]
else:
    said = "nothing to do"
out({"type": "result", "subtype": "success", "is_error": False, "num_turns": 2, "total_cost_usd": 0, "result": said})
'''


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def served(tmp_path):
    agent = tmp_path / "claude"
    agent.write_text(FAKE.replace("#!/usr/bin/env python3", f"#!{sys.executable}"))
    agent.chmod(agent.stat().st_mode | stat.S_IXUSR)
    port = free_port()
    a = embed.parser().parse_args(["serve", "--demo", "--no-pregenerate", "--port", str(port), "--agent-cmd", str(agent)])
    app = PhotoApp(a)
    srv = app.make_server("127.0.0.1", port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield app
    app.tasks.stop()
    srv.shutdown()


def jpeg(seed: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), ((seed * 70) % 256, (seed * 30) % 256, (seed * 110) % 256)).save(buf, "JPEG", quality=90)
    return buf.getvalue()


def wait(app, by_prefix: str) -> dict:
    con = sqlite3.connect(app.a.db)
    try:
        for _ in range(300):
            row = con.execute("SELECT id, status, result, error FROM agent_tasks WHERE started_by LIKE ? ORDER BY id DESC",
                              (by_prefix + "%",)).fetchone()
            if row and row[1] in ("done", "failed", "cancelled"):
                return dict(zip(("id", "status", "result", "error"), row))
            time.sleep(0.1)
    finally:
        con.close()
    raise AssertionError(f"no run started by {by_prefix} ended")


def test_series_are_kept_up_to_date_when_photos_arrive(served, tmp_path):
    app = served
    seed = app.live.cat.shas[12]
    grow = {"like": seed, "min_score": 0.99}
    # Ben's own series, and a family series the archivist made (through a run's token, acting for no one)
    bens = call(app, "POST", "/api/albums", BEN, {"title": "The lake", "grows_from": grow}, expect=201)
    assert bens["grows_from"] == grow
    con = sqlite3.connect(app.a.db)
    tid = con.execute("INSERT INTO agent_tasks(prompt, status, created_at, started_by) VALUES ('family', 'running', '', 'member:1')").lastrowid
    con.commit()
    h = {"X-Estate-Agent": app.tasks.mint(tid)}
    fam = app.request("POST", "/mcp", headers=h, local=True, body={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "create_album", "arguments": {"title": "The lake, for everyone", "search": grow, "grow": True}}}).json()
    fam = fam["result"]["structuredContent"]["album"]
    with app.tasks.lock:
        app.tasks.runs.clear()
    con.execute("UPDATE agent_tasks SET status='done' WHERE id=?", (tid,))
    con.commit()
    # two new photos, looking just like the seed, and one that does not; ingested from a folder, embedded, reloaded
    inbox, store = tmp_path / "drop", tmp_path / "store"
    inbox.mkdir()
    for k in range(3):
        (inbox / f"IMG_{k}.JPG").write_bytes(jpeg(40 + k))
    estate.cmd_ingest(argparse.Namespace(db=app.a.db, store=str(store), sources=[str(inbox)], force=True, facts=None,
                                         delete_after=False, progress=False, uploads=False))
    new = [s for s, in con.execute("SELECT sha256 FROM assets ORDER BY rowid DESC LIMIT 3")][::-1]
    vec = app.live.cat.M[app.live.cat.index[seed]].astype(np.float32)
    other = app.live.cat.M[app.live.cat.index[app.live.cat.shas[90]]].astype(np.float32)
    for s, v in zip(new, (vec, vec, other)):
        v = (v / np.linalg.norm(v)).astype(np.float16)
        con.execute("INSERT OR REPLACE INTO embeddings VALUES (?,?,?,?,NULL,'2026-11-22')", (s, MODEL, v.shape[0], v.tobytes()))
    con.commit()
    entry = con.execute("SELECT id, data FROM journal WHERE kind='ingest.finished' ORDER BY id DESC").fetchone()
    assert json.loads(entry[1])["assets"][1] - json.loads(entry[1])["assets"][0] == 3
    app.listen_once()
    assert listeners.cursor(con, "series") < entry[0], "not before the catalog that holds them is served"
    app.reload_catalog()
    assert app.loaded_through >= entry[0]
    app.listen_once()
    # Ben's: in his inbox, for him to add by hand
    items = call(app, "GET", "/api/inbox", BEN)["items"]
    assert [(i["kind"], i["ref"], i["opens"]) for i in items] == [("series_update", f"album:{bens['id']}",
                                                                   f"/?album={bens['id']}&candidates=1")]
    assert items[0]["title"] == "2 new photos may belong in your series “The lake”"
    got = call(app, "GET", f"/api/albums/{bens['id']}/candidates", BEN)
    assert [r["sha"] for r in got["results"]] and {r["sha"] for r in got["results"]} == set(new[:2])
    call(app, "GET", f"/api/albums/{bens['id']}/candidates", CLEO, expect=404)
    done = call(app, "POST", f"/api/albums/{bens['id']}/candidates", BEN, {"add": [new[0], new[2]]})
    assert (done["added"], done["not_candidates"]) == (1, 1)
    assert call(app, "GET", "/api/inbox", BEN)["items"] == [] and call(app, "GET", f"/api/albums/{bens['id']}/candidates", BEN)["results"] == []
    # the family series: the estate's agent was woken, looked, and added what belongs, directly
    run = wait(app, f"journal:{entry[0]}/album:{fam}")
    assert run["status"] == "done" and run["result"] == "added 1 of 2", run
    con2 = sqlite3.connect(app.a.db)
    held = {s for s, in con2.execute("SELECT sha256 FROM change_items WHERE set_id=?", (fam,))}
    assert len(held & set(new[:2])) == 1 and new[2] not in held, "the one it chose of the two that matched"
    assert con2.execute("SELECT actor, acting_for FROM journal WHERE subject=? AND kind='album.items' ORDER BY id DESC",
                        (f"album:{fam}",)).fetchone() == (app.agent, None)
    assert con2.execute("SELECT acting_for, started_by FROM agent_tasks WHERE id=?", (run["id"],)).fetchone() == \
        (None, f"journal:{entry[0]}/album:{fam}"), "the implicit run is recorded, with what started it"
    con2.close()
    con.close()
    # at least once: the listeners run again over everything, and nothing is made twice
    c = sqlite3.connect(app.a.db)
    c.execute("UPDATE listener_cursors SET journal_id=0") if c.execute("SELECT 1 FROM listener_cursors").fetchone() else None
    c.commit()
    app.listen_once()
    assert c.execute("SELECT count(*) FROM agent_tasks WHERE started_by LIKE 'journal:%'").fetchone()[0] == 1
    assert c.execute("SELECT count(*) FROM journal WHERE kind='series.update_requested'").fetchone()[0] == 1
    c.close()


def test_an_answer_the_agent_wanted_wakes_a_new_run_with_it(served):
    app = served
    x = app.live.cat.shas[14]
    con = sqlite3.connect(app.a.db)
    tid = con.execute("INSERT INTO agent_tasks(prompt, status, created_at, acting_for, started_by) VALUES"
                      " ('ask Cleo', 'running', '', ?, 'member:2')", (mid(app, BEN),)).lastrowid
    con.commit()
    h = {"X-Estate-Agent": app.tasks.mint(tid)}
    got = app.request("POST", "/mcp", headers=h, local=True, body={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "ask_member", "arguments": {"photos": [x], "text": "Who is at the window?", "to": "Cleo", "follow_up": True,
                                            "why": "to name the album's people"}}}).json()["result"]["structuredContent"]
    con.execute("UPDATE agent_tasks SET status='done' WHERE id=?", (tid,))
    con.commit()
    assert con.execute("SELECT asked_by, acting_for, task_id FROM questions").fetchone() == (app.agent, mid(app, BEN), tid)
    con.close()
    call(app, "GET", "/api/inbox", CLEO)
    call(app, "POST", f"/api/questions/{got['question']}/answer", CLEO, {"text": "Aunt May, in 1987"}, expect=201)
    app.listen_once()
    run = wait(app, "journal:")
    assert run["status"] == "done" and run["result"] == "read the answer: “Aunt May, in 1987”"
    con = sqlite3.connect(app.a.db)
    assert con.execute("SELECT acting_for FROM agent_tasks WHERE id=?", (run["id"],)).fetchone()[0] == mid(app, BEN)
    con.close()
    assert [i["kind"] for i in call(app, "GET", "/api/inbox", ADA)["items"]] == []
