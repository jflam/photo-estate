"""bench_views.py: what multiplayer's per-member views cost on a library of the real size. See
docs/notes/PERFORMANCE.md.

  uv run --group ml tools/bench_views.py --work ~/bench-estate          # the 121k-photo estate bench_ingest.py builds
  uv run --group ml tools/bench_views.py --work ~/bench-views --photos 100000

The estate is tools/bench_ingest.py's synthetic one (the demo catalog cloned to --photos rows, with places,
events and trips), built once in --work. In process, with no model and no thread: the app's start (reading
the catalog), its first /api/info and search (which work out the sysadmin's view), then with three members
(Ada the sysadmin, Ben, Cleo): an estate default hiding --default photos and a filter of Ben's hiding --mine,
the time to work out each member's view after a change (Live.visible_for), a search as each, and a sync feed
made for Ben (his phone's first ask) and brought up to date after Ben hides 100 more. Every number is the
wall time of one run, in seconds. The catalog in --work is copied first: the bench never writes to it.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tools"))


def timed(label, fn):
    t = time.perf_counter()
    out = fn()
    print(f"  {label:58} {time.perf_counter() - t:7.3f} s", flush=True)
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--work", required=True, type=Path)
    p.add_argument("--photos", type=int, default=121_000)
    p.add_argument("--default", type=int, default=5000, help="photos the estate default hides")
    p.add_argument("--mine", type=int, default=2000, help="photos Ben's own filter hides")
    a = p.parse_args()
    from bench_ingest import build
    build(a.work, a.photos)
    import embed
    import members
    from photo_app import PhotoApp
    tmp = Path(tempfile.mkdtemp(prefix="bench-views-"))
    try:
        shutil.copyfile(a.work / "estate.db", tmp / "estate.db")
        con = sqlite3.connect(tmp / "estate.db")
        from core import schema
        schema.ensure(con, *schema.CATALOG_AREAS)
        if not con.execute("SELECT 1 FROM members LIMIT 1").fetchone():
            members.demo(con)
        con.close()
        args = embed.parser().parse_args(["--db", str(tmp / "estate.db"), "serve", "--store", str(a.work / "store"),
                                          "--cache", str(a.work), "--no-pregenerate", "--query-log", "/dev/null",
                                          "--inbox", str(tmp / "inbox"), "--port", "1"])
        print(f"{a.photos} photos ({tmp})")
        app = timed("start: PhotoApp() (reads the catalog)", lambda: PhotoApp(args))
        N = app.live.cat.N
        timed("first /api/info (works out the sysadmin's view)", lambda: app.request("GET", "/api/info", local=True))
        timed("first /api/search?limit=120 (browse, newest first)", lambda: app.request("GET", "/api/search?limit=120", local=True))
        timed("again (cached)", lambda: app.request("GET", "/api/search?limit=120&cursor=120", local=True))
        timed("/api/periods", lambda: app.request("GET", "/api/periods", local=True))
        timed("feed_update: the sysadmin's feed, first time", app.feed_update)
        timed("feed_update: nothing changed", app.feed_update)
        if not hasattr(app.live, "visible_for"):
            return                                    # (the code from before multiplayer phase 3: no members to compare)
        ada, ben, cleo = (app.roster[x].id for x in ("ada", "ben", "cleo"))
        shas = app.live.cat.shas
        ADA, BEN = {"X-Demo-Member": "ada"}, {"X-Demo-Member": "ben"}
        args.demo = True                             # (the demo's member header, on a real-size catalog)

        def hide(who, picked, lens):
            sid = app.request("POST", "/api/changes", headers=who, body={"title": "bench", "items": picked}).json()["id"]
            r = app.request("POST", f"/api/changes/{sid}/apply", headers=who, body={"lens": lens})
            assert r.status == 200, r.body
            while app.settling:
                time.sleep(0.01)
        timed(f"Ada hides {a.default} for everyone (apply, relook, every feed)", lambda: hide(ADA, shas[: a.default], "estate"))
        timed(f"Ben hides {a.mine} for himself (apply, relook, every feed)",
              lambda: hide(BEN, shas[N // 2: N // 2 + a.mine], "mine"))
        for name, m in (("Ada", ada), ("Ben", ben), ("Cleo", cleo)):
            with app.live.gate:
                v = timed(f"visible_for({name}) after a change (stacks, trips, info)", lambda: app.live.visible_for(m))
            print(f"    {name} sees {v.info['total']} photos")
        timed("Ben: /api/search?limit=120", lambda: app.request("GET", "/api/search?limit=120", headers=BEN))
        timed("Ben: /api/info", lambda: app.request("GET", "/api/info", headers=BEN))
        timed("Ben's phone asks first: his feed made (/api/sync)", lambda: app.request("GET", "/api/sync?limit=2000", headers=BEN))
        timed("Ben hides 100 more (apply, relook, both feeds)", lambda: hide(BEN, shas[N // 3: N // 3 + 100], "mine"))
        timed("feed_update: both feeds, nothing changed", app.feed_update)
        with app.live.gate:
            looks = app.live.looks
            timed("lenses.State.masks for Ben (the hides as a mask)", lambda: looks(app.live.cat, ben))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
