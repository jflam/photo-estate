#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Intake (the setup page's engine, intake/): every practice source through the real ingest, link and
places, signing in the ways the real sources will (none, a browser approval, a password and a code);
duplicates across sources collapsed; a wrong password and a wrong code asked again; cancel and retry; a
job stopped mid-pipeline resumed from its stage; the catalog lock shared with ingest-iphone.sh; the local
folder source's preview; and the API, which must never hand back or write down what was typed to sign in.

The models (embed, faces) and exiftool are left out: they are covered elsewhere and take minutes."""
import json, os, sqlite3, sys, tempfile, threading, time, zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
os.environ["PHOTO_ESTATE_PRACTICE_FAST"] = "1"
os.environ["PHOTO_ESTATE_CONFIG"] = str(Path(tempfile.mkdtemp()) / "library.json")
from intake.api import IntakeAPI, Library  # noqa: E402
from intake.engine import CatalogLock, Engine  # noqa: E402

SKIP = {"exif", "embed", "faces"}
SECRET = "correct horse battery staple"


def wait(e, jid, states=("done", "failed", "cancelled"), timeout=240):
    t = time.time()
    while (j := e.jobs.get(jid))["state"] not in states:
        assert time.time() - t < timeout, f"job {jid} stuck in {j['state']}/{j['stage']}"
        time.sleep(0.2)
    return e.view(jid)


def assets(db):
    c = sqlite3.connect(db)
    try:
        return c.execute("SELECT count(*), count(taken_at), count(lat) FROM assets").fetchone()
    finally:
        c.close()


def make(root, **kw):
    (root / "store").mkdir(parents=True, exist_ok=True)
    e = Engine(root, str(root / "estate.db"), root / "store", HERE.parent, practice=True, skip=SKIP,
               lock=CatalogLock(root / "lock"), **kw)
    e.start()
    return e


def main():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "lib"
        ready = []
        e = make(root, on_ready=ready.append)
        db = str(root / "estate.db")

        # 1. Takeout-shaped zip, no sign-in: 48 photos with Google's dates and places.
        j1 = e.create("practice_takeout", {})
        v = wait(e, j1)
        assert v["state"] == "done", v
        n, dated, placed = assets(db)
        assert (n, v["media_before"], v["media_after"]) == (48, 0, 48), (n, v)
        assert dated == 48 and placed >= 30, (dated, placed)
        assert ready == [j1], ready
        assert all(s["state"] == "done" for s in v["stages"]), v["stages"]
        print("ok  takeout: 48 photos, dated and placed, ready once")

        # 2. Password, then code. A wrong one of each is asked again; the password is never kept.
        j2 = e.create("practice_icloud", {})
        prompts = []

        def answer_when(kind, value):
            t = time.time()
            while True:
                j = e.jobs.get(j2)
                if j["state"] == "waiting" and j["prompt"] and j["prompt"]["kind"] == kind and len(prompts) < 9:
                    prompts.append(j["prompt"]["label"])
                    assert e.answer(j2, value)
                    while (j := e.jobs.get(j2))["state"] == "waiting" and (j["prompt"] or {}).get("label") == prompts[-1]:
                        time.sleep(0.05)
                    return
                assert time.time() - t < 30, j
                time.sleep(0.05)

        answer_when("password", "wrong")
        answer_when("password", SECRET)
        answer_when("code", "000000")
        answer_when("code", "271828")
        v = wait(e, j2)
        assert v["state"] == "done", v
        assert "Try again" in prompts[1] and "didn't match" in prompts[3], prompts
        assert assets(db)[0] == 88, assets(db)
        print("ok  icloud: password and code asked again when wrong; 40 photos with XMP dates")

        # 3. Browser approval. 16 of its 32 photos are already in from the other two.
        j3 = e.create("practice_drive", {})
        t = time.time()
        while e.jobs.get(j3)["state"] != "waiting":
            assert time.time() - t < 30
            time.sleep(0.05)
        p = e.jobs.get(j3)["prompt"]
        assert p["kind"] == "browser" and p["url"] == f"/setup/practice-consent?job={j3}", p
        assert not e.answer(j1, "allow"), "a job that isn't waiting takes no answer"
        assert e.answer(j3, "allow")
        v = wait(e, j3)
        assert v["state"] == "done" and assets(db)[0] == 104, (v["state"], assets(db))
        assert v["media_after"] - v["media_before"] == 16
        print("ok  drive: approved in the browser; 16 new, 16 duplicates collapsed")

        # 4. Nothing typed to sign in is written anywhere.
        for f in root.rglob("*"):
            if f.is_file() and f.stat().st_size < 50 << 20:
                assert SECRET.encode() not in f.read_bytes(), f"the password is in {f}"
        print("ok  the password is in no file")

        # 5. Cancel while it waits for you, then try again: it asks again.
        j4 = e.create("practice_icloud", {})
        wait(e, j4, ("waiting",))
        assert e.cancel(j4)
        assert wait(e, j4)["state"] == "cancelled"
        assert e.retry(j4)
        wait(e, j4, ("waiting",))
        assert e.jobs.get(j4)["prompt"]["kind"] == "password"
        e.cancel(j4)
        wait(e, j4)
        print("ok  cancel and retry")

        # 6. A failure keeps its stage; a stop mid-pipeline resumes from that stage after a restart.
        jf = e.create("folder", {"path": str(root / "store")})   # the store itself: it has photos
        e.jobs.set(jf, state="running", stage="link", paths=[str(root / "store")])
        e.cancel(jf)                                             # not current: just marked
        e.jobs.set(jf, state="running")                          # as a crash would leave it
        e2 = make(root)                                          # a second engine on the same library = a restart
        v = wait(e2, jf)
        assert v["state"] == "done", v
        assert not any("ingest" in ev["text"] and "entries" in ev["text"] for ev in v["events"]), "ingest ran again"
        assert any("resuming after a restart" in ev["text"] for ev in v["events"])
        print("ok  a job stopped in `link` resumes there after a restart")

        # 7. The phone's ingest holds the lock (a directory with no owner): the job waits for it.
        lock = root / "lock"
        lock.mkdir()
        z = Path(td) / "more"
        z.mkdir()
        with zipfile.ZipFile(z / "x.zip", "w") as zf:
            zf.write(next((root / "store").rglob("*.jpg")), "a/copy.jpg")
        j5 = e2.create("folder", {"path": str(z)})
        t = time.time()
        while "Waiting for the phone" not in (e2.jobs.get(j5)["detail"] or ""):
            assert time.time() - t < 60, e2.jobs.get(j5)
            time.sleep(0.1)
        lock.rmdir()
        assert wait(e2, j5)["state"] == "done"
        print("ok  waits for ingest-iphone.sh's lock")

        # 8. The folder source's preview.
        from intake.base import SOURCES
        folder = SOURCES["folder"]
        assert not folder.preview({"path": str(Path(td) / "nope")}).ok
        assert not folder.preview({"path": ""}).ok
        pv = folder.preview({"path": str(z)})
        assert pv.ok and pv.media == 1 and pv.items == [str(z.resolve() / "x.zip")], pv
        fl = SOURCES["flickr_files"].preview({"path": str(z)})
        assert fl.ok and any("account-data" in p for p in fl.problems), fl
        print("ok  previews")

        # 9. The API: state, a job's view, answers, and a library chosen once.
        lib = Library(None, remember=True)
        api = IntakeAPI(lib, lambda l: make(l.root), setup_mode=True, practice=True)

        def call(m, path, body=None, local=True):
            code, raw, _ = api.handle(m, path, {}, body, local)
            return code, json.loads(raw)
        code, st = call("GET", "/api/intake")
        assert code == 200 and st["mode"] == "setup" and not st["library"]["chosen"], st
        assert {"practice_takeout", "folder", "takeout_files", "flickr_files"} <= {s["id"] for s in st["sources"]}
        assert call("POST", "/api/intake/jobs", {"source": "practice_takeout"})[0] == 409   # no library yet
        assert call("POST", "/api/intake/choose", {}, local=False)[0] == 403
        code, got = call("POST", "/api/intake/library", {"root": str(Path(td) / "lib2")})
        assert code == 200 and got["chosen"], got
        assert json.loads(Path(os.environ["PHOTO_ESTATE_CONFIG"]).read_text())["root"] == str((Path(td) / "lib2").resolve())
        code, got = call("POST", "/api/intake/jobs", {"source": "practice_icloud"})
        assert code == 201, got
        jid = got["id"]
        wait(api.engine, jid, ("waiting",))
        assert call("POST", "/api/intake/library", {"root": str(Path(td) / "lib3")})[0] == 409
        assert call("POST", f"/api/intake/jobs/{jid}/answer", {"value": SECRET})[0] == 200
        time.sleep(0.3)
        code, view = call("GET", f"/api/intake/jobs/{jid}")
        assert code == 200 and SECRET not in json.dumps(view) and SECRET not in json.dumps(call("GET", "/api/intake")[1])
        assert call("POST", f"/api/intake/jobs/{jid}/cancel")[0] == 200
        assert call("POST", "/api/intake/jobs", {"source": "nope"})[0] == 400
        assert call("GET", "/api/intake/jobs/999")[0] == 404
        code, _, ctype = api.handle("GET", "/setup", {}, None, True)
        assert code == 200 and ctype.startswith("text/html")
        print("ok  API")
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
