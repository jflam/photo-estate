"""
photo_app.py — the photo app (`embed.py serve`) as an object: PhotoApp holds the server's state and
wires the route modules together; make_handler() is the thin HTTP layer in front of it.

  PhotoApp(a)                 set the app up from `embed.py serve`'s options (embed.parser()); reads the
                              catalog, starts no thread (with --demo, a scratch copy of demo/estate.db)
  .start()                    the background work: faces, the search model, the sync feed, thumbnails,
                              the inbox sweep and backups
  .handle(Request) -> Response   one request, in process: every route but the album download
  .request(method, target, headers=, body=, local=) -> Response   the same, for tests and tools
  .make_server(host, port) -> Server   a ThreadingHTTPServer for it (port 0: any free port)
  .serve()                    make_server on 127.0.0.1:--port and serve until Ctrl-C
  .reload_catalog()           re-read the catalog and swap it in (after an ingest, or a change set)
  .set_favorites(shas, on, who) -> int   record favourites, show them at once, tell the phones
  ROUTE_AUTH, policy(method, path) -> Auth   who may make each request (http_util.Auth)

Each route module has an object whose handle(method, path, qs, body, ...) answers its own routes (each
module's docstring lists them):
  catalog.py     the library in memory (Catalog), and the one being served (Live) that the others read
  browse.py      search, similar, facets, Years and Months, layout, the query log; an agent's searches
  images.py      thumbnails, previews, large images and originals
  admin.py       uploads from the phone, the ingest, restarting, the Library info page, backups
  faces.py (the People page), changes.py (change sets and albums), tasks.py, estate_mcp.py (/mcp),
  intake/api.py (/setup), and with --compare-vectors compare.py (/compare: an experiment, a second model's
  ranking beside the app's own)
What PhotoApp answers itself: the page and the home-screen app files, /api/info, /api/sync,
favourites, album downloads (streamed by the handler), and re-reading the catalog after an ingest.

Who may write (http_util.py has the rule): every request that changes the library (people, favourites,
albums and change sets, tasks, intake, ingest, restart, upload) is Auth.LOCAL_OR_WRITER: a
--upload-user login (anyone, when none is named), or a request from this computer itself. Everything
else is PUBLIC; /mcp is too, and its write tools take the same rule (estate_mcp.py's can_write).

Bad requests are answered, not raised: a query parameter that is not a number, a body that is not JSON
or a Content-Length that is not a number is a 400, and a JSON body over http_util.MAX_BODY a 413, each
as {"error": ...}.

Building a PhotoApp needs only numpy and Pillow: the search model (encoder.py) and the faces load in
start()'s threads, so tests can build one on the demo library and call handle() without PyTorch.
"""
from __future__ import annotations

import gzip
import hashlib
import html
import io
import json
import os
import re
import sys
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import catalog
import pages   # the pages, with the shared stylesheet put in
from admin import Admin, backups
from browse import Browse
from changes import Changes
from core import config, schema
from core.lifecycle import Lifecycle, Phase, Unavailable
from embed import connect, now
from encoder import Encoder
from estate_mcp import AppFacade, Server as MCPServer
from http_util import (FORBIDDEN, MAX_BODY, NOT_FOUND, Auth, HTTPError, Request, Response, byte_range, int_param,
                       json_response, parse_json, read_body)
from images import Images
from intake.server import is_local
from sync import Feed
from tasks import Tasks
from upload import IngestJob

HERE = Path(__file__).resolve().parent

# Who may make each request: the first entry whose method and path prefix match. Anything not listed
# is PUBLIC (a POST nobody answers is then a 404). Upload bodies stream and are checked in admin.py with
# the same rule (PhotoApp.can_write).
ROUTE_AUTH: tuple[tuple[str, tuple[str, ...], Auth], ...] = (
    ("POST", ("/api/people",), Auth.LOCAL_OR_WRITER),                    # naming, merging, relabelling
    ("POST", ("/changes", "/api/changes", "/api/albums", "/api/tasks"), Auth.LOCAL_OR_WRITER),
    ("POST", ("/api/favorite",), Auth.LOCAL_OR_WRITER),
    ("POST", ("/api/intake",), Auth.LOCAL_OR_WRITER),                    # a job reads any folder here
    ("POST", ("/api/ingest", "/api/restart"), Auth.LOCAL_OR_WRITER),
    ("POST", ("/api/upload/",), Auth.LOCAL_OR_WRITER),
    ("PUT", ("/api/upload/",), Auth.LOCAL_OR_WRITER),
    ("POST", ("/api/compare",), Auth.LOCAL_OR_WRITER),                   # a vote: a side file, not the library
)
# Requests that don't hold the catalog gate: they read no catalog, or only briefly, and can be slow.
UNGATED = ("/api/upload/", "/api/intake", "/setup")
FAVORITE = re.compile(r"/api/favorite|/api/albums/\d+/favorite")   # (POST) the gate's write side, in set_favorites
APP_TYPES = {".webmanifest": "application/manifest+json", ".js": "text/javascript; charset=utf-8",
             ".png": "image/png"}


def policy(method: str, path: str) -> Auth:
    for m, prefixes, rule in ROUTE_AUTH:
        if method == m and path.startswith(prefixes):
            return rule
    return Auth.PUBLIC


def _etagged(body: bytes, ctype: str) -> Response:
    return Response(200, body, ctype, "no-cache", {"ETag": f'"{hashlib.md5(body).hexdigest()}"'})


class PhotoApp:
    def __init__(self, a):
        self.a = a
        if a.demo:  # the committed demo estate, served from a scratch copy so the repo is never written to
            import atexit
            import shutil
            import tempfile
            demo = HERE / "demo"
            if not (demo / "estate.db").exists():
                sys.exit(f"no demo estate in {demo}; build it with `uv run tools/make_demo.py`")
            scratch = Path(tempfile.mkdtemp(prefix="photo-estate-demo-"))
            atexit.register(shutil.rmtree, scratch, ignore_errors=True)
            shutil.copyfile(demo / "estate.db", scratch / "estate.db")
            a.db, a.store, a.cache, a.fast_cache = str(scratch / "estate.db"), str(demo / "store"), None, None
            a.query_log = a.query_log or os.devnull
        self.store = store = Path(a.store)
        self.port = config.app_port(a.port, demo=a.demo, sandbox=a.sandbox)
        # Phone uploads land in a plain folder that `estate.py ingest` reads like any other source. The demo
        # takes none unless --inbox says where; otherwise the inbox sits beside the catalog.
        self.inbox = inbox = Path(a.inbox) if a.inbox else None if a.demo else Path(a.db).resolve().parent / "iphone"
        self.seed = seed = Path(a.cache or Path(a.db).resolve().parent / "review")
        self.ingest = ingest = (IngestJob(Path(a.ingest_script or HERE / "ingest-iphone.sh"), Path(a.db).resolve().parent,
                                          a.db, store, inbox, quiet=a.ingest_quiet, max_wait=a.ingest_max_wait, cache=seed)
                                if inbox else None)
        # On the Mac mini the estate disk is a USB hard disk: --fast-cache puts the thumbnail cache on the
        # internal SSD, filled from the disk's cache (`seed`) on first use (images.py).
        self.cache = cache = Path(a.fast_cache) if a.fast_cache else seed
        self.t0 = time.time()
        con = connect(a.db)               # the tables this app writes itself (core/schema.py), made when it opens
        schema.ensure(con, "favorites")
        con.close()
        try:
            self.live = live = catalog.Live(self.load_catalog())
        except RuntimeError as e:
            sys.exit(str(e))
        print(f"{live.cat.N} images loaded in {time.time() - self.t0:.1f}s ({len(live.cat.events)} events,"
              f" {len(live.cat.trips)} trips)", flush=True)

        # The People page (/people): who each face is, and the questions worth asking. It's faces.py's,
        # mounted here so naming works wherever the app does. Loads in the background, like the encoder.
        # Its Lifecycle's value is the faces.People that answers; people_state() is what the page is told.
        self.people, self.people_loading = Lifecycle("the People page"), threading.Lock()   # one load at a time
        self.encoder = Encoder(a.model, a.threads, images=bool(inbox), device=a.device)   # the image side too, for photos from the phone
        # Devices that keep their own copy of the catalog (the iPhone app) follow it through a change feed
        # (sync.py). The feed's versions live beside the catalog, never in it.
        self.feed = Feed(Path(a.db).resolve().parent / "sync-feed.db")
        # Home-screen app files (manifest, icons, service worker), served from the site root: a service
        # worker only controls pages at or below its own path.
        app_dir = HERE / "app"
        # The pages' script modules are in app/ too; those are served under /app/ (pages.module), not here.
        self.app_files = ({f"/{p.name}": p for p in app_dir.iterdir()
                           if p.suffix in APP_TYPES and (p.suffix != ".js" or p.name in pages.NOT_MODULES)}
                          if app_dir.is_dir() else {})

        self.qlog = qlog = Path(a.query_log or Path(a.db).resolve().parent / "query-log.jsonl")
        self.browse = browse = Browse(live, self.encoder, a.db, a.model, qlog, a.match_floor, a.match_window, a.best_window)
        self.images = Images(live, store, cache, seed)
        self.admin = Admin(live, self.encoder, connect, now, db=a.db, model=a.model, store=store, cache=cache, seed=seed,
                           inbox=inbox, ingest=ingest, query_log=qlog, demo=a.demo,
                           faces_ready=lambda: self.people_state() == "ready")
        # An experiment: the same search ranked by a second model's vectors (compare.py). Without its
        # vectors the app runs as usual: on the mini it is a launchd service, and a stop would only repeat.
        self.compare = None
        if a.compare_vectors:
            from compare import Compare
            vec_dir = Path(a.compare_vectors).expanduser()
            try:
                # Loaded when /compare is first asked for and let go when it has not been used for a while:
                # the larger model was 3 GB or more of the mini's 16 GB, held all day for an experiment.
                self.encoder_b = Encoder(json.loads((vec_dir / "meta.json").read_text())["model"], a.threads,
                                         device=a.device, idle=a.compare_idle * 60 if a.compare_idle > 0 else None)
                self.compare = Compare(live, browse, vec_dir, self.encoder_b,
                                       Path(a.compare_votes).expanduser() if a.compare_votes else vec_dir / "votes.jsonl")
            except (OSError, ValueError, KeyError) as e:
                print(f"compare: no vectors in {vec_dir} ({type(e).__name__}: {e}); /compare is off", flush=True)

        # Favourites set in the app. The catalog's own flag (assets.favorited) is what the photo's source said,
        # and `estate.py link` writes it again on every ingest, so what a person says here is kept beside it
        # (the `favorites` table) and wins. It shows at once: the photo's row, the count and every cached
        # order and cover are brought up to date here rather than by re-reading the catalog (catalog.load reads
        # the table too, so a reload agrees). While a reload is reading the catalog, `fav_log` is every one
        # set since it began (None otherwise), so a reload that read the table a moment too early is put right
        # as it is swapped in. fav_lock guards fav_log; fav_write puts one set_favorites at a time.
        # Lock order: fav_write, then live.gate, then fav_lock (catalog.Gate).
        self.fav_lock, self.fav_write, self.fav_log = threading.Lock(), threading.Lock(), None
        # Re-reading the catalog is in one of three states: idle, running, or rerun (asked again while
        # running: when this run ends, another starts, so nothing that was asked for is missed).
        self.reload_state = {"state": "idle", "at": None, "seconds": None, "error": None}
        self.reload_lock = threading.Lock()
        # Change sets (changes.py). When one hides or shows photos, the catalog is re-read as after an ingest:
        # every view, count and cover is rebuilt without them, phones are told through the feed, and the
        # People page loads again without their faces. `settling` counts the moments before the reload starts.
        self.settling, self.settling_lock = 0, threading.Lock()
        self.changes = Changes(a.db, facts=lambda sha: self.live.cat.meta.get(sha), shown=self.shown,
                               busy=lambda: self.settling > 0 or self.reload_state["state"] != "idle",
                               on_change=self.on_hidden_change, find=browse.find, favorite=self.set_favorites,
                               cover=lambda ids: self.live.cat.album_cover(ids))
        # Tasks (tasks.py): a request in words, worked on by an agent on this computer that uses the MCP
        # endpoint below like any other client, and ends in a change set.
        self.tasks = Tasks(a.db, config.mcp_url(self.port), command=a.agent_cmd, env_file=a.agent_env,
                           model=a.agent_model, log=lambda msg: print(msg, flush=True))
        self.mcp = MCPServer(AppFacade(find=browse.find, row=browse.row, faces=browse.faces,
                                       overview=browse.overview, changes=self.changes, tasks=self.tasks))
        if ingest:
            ingest.on_added = self.reload_catalog
            ingest.before_run = self.admin.arrivals_prepared
            ingest.on_faces = lambda: self.live.cat.has_faces and self.load_people()   # the run's faces are in
        # Bringing in more photos from the app (/setup): the engine first-run setup uses (intake/), here
        # reloading the catalog when a job's photos are searchable, and the People page when its faces are in.
        self.intake = None
        if not a.demo:
            from intake.api import IntakeAPI, Library
            from intake.engine import Engine as IntakeEngine

            def make_intake(lib: Library) -> IntakeEngine:
                e = IntakeEngine(*lib.chosen(), HERE, practice=bool(a.sandbox),
                                 on_ready=lambda jid: self.reload_catalog(),
                                 on_faces=lambda jid: (self.reload_catalog(), self.live.cat.has_faces and self.load_people()))
                e.start()
                return e
            self.intake = IntakeAPI(Library(Path(a.root or Path(a.db).resolve().parent), a.db, store, fixed=True),
                                    make_intake, setup_mode=False, practice=bool(a.sandbox))

    # -- setting up ---------------------------------------------------------------------------------

    def load_catalog(self, prev: dict | None = None) -> catalog.Catalog:
        return catalog.load(connect(self.a.db), self.a.model, self.cache, self.seed, prev, demo=self.a.demo)

    def load_people(self) -> None:
        """Again after a reload: the one that is ready keeps answering until the new one is, and keeps
        answering if the new one fails. Loads run one at a time, so the newest faces are the last swapped in."""
        with self.people_loading:
            self.people.begin()
            try:
                from faces import People
                p = People(self.a.db, self.a.store, [self.cache] if self.cache == self.seed else [self.cache, self.seed],
                           log=lambda msg: print(msg, flush=True))
            except Exception as e:   # a missing or broken table, out of memory: /people says so, not "loading"
                self.people.fail(e)
                print(f"people: failed to load: {self.people.snapshot().error}", flush=True)
                return
            self.people.succeed(p)

    def people_state(self) -> str:
        """none (no faces) | loading | ready | failed (and nothing loaded before to keep answering)."""
        s = self.people.snapshot()
        if s.value is not None:
            return "ready"
        if s.phase is Phase.FAILED:
            return "failed"
        return "loading" if self.live.cat.has_faces else "none"

    def start(self) -> None:
        a = self.a
        if self.live.cat.has_faces:
            threading.Thread(target=self.load_people, daemon=True).start()
        self.encoder.start()   # (/compare's second model is not loaded here: Compare asks for it when it is used)
        threading.Thread(target=self.feed_update, daemon=True).start()
        if self.ingest:
            threading.Thread(target=self.ingest.sweep, daemon=True).start()   # uploads left from before this start
        if a.backup_dir and not a.demo:
            threading.Thread(target=backups, args=(a.db, Path(a.backup_dir), a.backup_every, a.backup_keep, self.t0),
                             daemon=True).start()
        if not a.no_pregenerate:
            threading.Thread(target=self.images.pregenerate, daemon=True).start()

    def feed_update(self) -> None:
        """Tell the feed what the catalog now holds. A row is the photo's search result, plus Catalog.feed_extra."""
        t, c = time.time(), self.live.cat
        n = self.feed.update({s: c.feed_row(s) for s, v in zip(c.shas, c.visible) if v})
        print(f"sync feed: version {self.feed.version}, {n} rows changed ({time.time() - t:.1f}s)", flush=True)

    # -- favourites, reloads, change sets ----------------------------------------------------------

    def set_favorites(self, shas, on: bool, who: str) -> int:
        """Record them, show them, tell the phones. Returns how many photos it changed. Called outside the
        gate (handle() lets the favourite routes through without it): showing them takes its write side."""
        with self.fav_write:   # one at a time, so the table and the catalog end up with the same last word
            shas = [s for s in dict.fromkeys(shas) if s in self.live.cat.index]
            con = connect(self.a.db)   # the table first, outside the gate: requests aren't held up by SQLite
            try:
                t = now()
                con.executemany("INSERT OR REPLACE INTO favorites VALUES (?,?,?,?)", ((s, int(on), t, who or None) for s in shas))
                con.commit()
            finally:
                con.close()

            def show(c: catalog.Catalog):   # the catalog being served now, no request reading it
                changed = [s for s in shas if s in c.index and bool(c.fav[c.index[s]]) != on]
                with self.fav_lock:
                    if self.fav_log is not None:   # a reload is reading the catalog: it may have read the table too early
                        self.fav_log.extend((s, on) for s in shas)
                c.show_favorites((s, on) for s in shas)
                self.browse.forget()            # covers and "favourites only" lists were worked out without them
                self.admin.forget_library()
                return changed, {s: c.feed_row(s) for s in changed if c.visible[c.index[s]]} if self.feed.ready else {}
            changed, rows = self.live.update(show)
        if rows:
            self.feed.touch(rows)
        return len(changed)

    def caught_up(self, cat: catalog.Catalog) -> None:
        """In the swap that installs a reloaded catalog: show it the favourites set while it was read, and
        drop the log (the table had the ones before, so it now reflects them all)."""
        with self.fav_lock:
            cat.show_favorites(self.fav_log or ())
            self.fav_log = None
        self.browse.forget()      # cached masks and rankings are the old catalog's
        self.admin.forget_library()

    def reload_catalog(self) -> None:
        """Re-read the catalog and swap it in, with no restart. The new catalog is built on the side while the
        old one keeps answering; only the swap waits for requests in flight. Asked again mid-run, it runs again."""
        rs = self.reload_state
        with self.reload_lock:
            if rs["state"] != "idle":
                rs["state"] = "rerun"
                return
            rs["state"] = "running"
        try:
            while True:
                t = time.time()
                with self.fav_lock:
                    self.fav_log = []         # favourites set from here on are shown to the new catalog too
                try:
                    fresh = self.load_catalog(self.live.cat.raw)
                    if os.environ.get("PHOTO_ESTATE_CHECK_RELOAD"):   # tools/bench_ingest.py --check
                        print(f"reload check: {fresh.differences(self.load_catalog()) or 'same as a full load'}", flush=True)
                    self.live.install(fresh, then=self.caught_up)
                    rs.update(at=time.time(), seconds=time.time() - t, error=None)
                    print(f"catalog reloaded: {fresh.N} images in {time.time() - t:.1f}s", flush=True)
                    self.feed_update()
                    if not self.a.no_pregenerate:
                        threading.Thread(target=self.images.pregenerate, daemon=True).start()
                except Exception as e:   # keep serving the old catalog
                    with self.fav_lock:
                        self.fav_log = None
                    rs["error"] = f"{type(e).__name__}: {e}"
                    print(f"catalog reload failed: {rs['error']}", flush=True)
                with self.reload_lock:
                    if rs["state"] != "rerun":
                        rs["state"] = "idle"
                        break
                    rs["state"] = "running"
        except BaseException:
            with self.reload_lock:
                rs["state"] = "idle"
            raise
        if self.ingest and not rs["error"]:
            self.ingest.fresh = False

    def hidden_changed(self) -> None:
        try:
            self.reload_catalog()
            if self.live.cat.has_faces:
                self.load_people()
        finally:
            with self.settling_lock:
                self.settling -= 1

    def on_hidden_change(self) -> None:
        with self.settling_lock:
            self.settling += 1
        threading.Thread(target=self.hidden_changed, daemon=True).start()

    def shown(self, sha: str) -> dict:
        c = self.live.cat
        return {"library": sha in c.index and bool(c.visible[c.index[sha]]), "sync": sha in self.feed.rows}

    # -- requests -----------------------------------------------------------------------------------

    @staticmethod
    def who(headers) -> str:
        # `tailscale serve` adds the caller's identity; the app itself is bound to 127.0.0.1
        return headers.get("Tailscale-User-Login", "") or ""

    def writer(self, headers) -> bool:
        """A --upload-user login, or anyone when none is named."""
        return not self.a.upload_user or self.who(headers) in self.a.upload_user

    def can_write(self, headers, local: bool) -> bool:
        return Auth.LOCAL_OR_WRITER.allows(writer=self.writer(headers), local=local)

    def request(self, method: str, target: str, headers=None, body: bytes | str | dict | list = b"",
                local: bool = False) -> Response:
        """handle(), for tests and tools: `body` may be bytes, text, or an object sent as JSON."""
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        return self.handle(Request(method, target, dict(headers or {}), body, local))

    def handle(self, req: Request) -> Response:
        """One request -> its Response. Holds the catalog gate (catalog.Live) for all but the slow ones, so
        a request sees one catalog from start to finish (gated()). A malformed request is a 400, never an exception;
        a search that needs the model while it loads is a 503, and after it failed to load a 500 (encoder.py)."""
        try:
            if not self.gated(req.method, urlparse(req.target).path):
                return self.route(req)
            with self.live.gate:
                return self.route(req)
        except HTTPError as e:
            return e.response
        except Unavailable as e:
            return json_response({"error": str(e)}, e.status, {"Retry-After": "5"} if e.status == 503 else None)

    @staticmethod
    def gated(method: str, path: str) -> bool:
        """Whether a request holds the catalog gate: not the slow ones (UNGATED, album downloads), nor
        setting favourites, which takes the gate's write side itself (set_favorites)."""
        return not (path.startswith(UNGATED) or path.endswith("/download")
                    or method == "POST" and FAVORITE.fullmatch(path) is not None)

    def route(self, req: Request) -> Response:
        method, headers, local = req.method, req.headers, req.local
        u = urlparse(req.target)
        path, qs = u.path, parse_qs(u.query)
        if method in ("PUT", "POST") and path.startswith("/api/upload/"):
            # Here from memory; the handler streams it. admin.py checks ROUTE_AUTH's rule itself (can_write),
            # with its own 403, since the handler's streamed uploads never come through route().
            got, _ = self.admin.upload(method, path, {**headers, "Content-Length": str(len(req.body))},
                                       io.BytesIO(req.body), self.who(headers), self.can_write(headers, local))
            return Response.of(got)
        if not policy(method, path).allows(writer=self.writer(headers), local=local):
            return FORBIDDEN
        if method == "OPTIONS":
            return Response.of(self.admin.handle("OPTIONS", path, {}, None))
        if method == "POST":
            return self.post(path, qs, req)
        if method == "GET":
            return self.get(path, qs, req)
        return NOT_FOUND

    def post(self, path: str, qs: dict, req: Request) -> Response:
        headers, local, who = req.headers, req.local, self.who(req.headers)
        if path in ("/api/ingest", "/api/restart"):   # (the body, if any, is not looked at)
            return Response.of(self.admin.handle("POST", path, {}, None, who, self.can_write(headers, local)))
        if path == "/mcp":     # a body that isn't JSON gets JSON-RPC's own parse error (estate_mcp.py)
            try:
                body = parse_json(req.body)
            except HTTPError:
                body = None
            return self.mcp_route("POST", req, body)
        body = parse_json(req.body)
        if path == "/api/favorite":
            return self.favorite(body, who)
        if self.compare and path.startswith("/api/compare"):
            got = self.compare.handle("POST", path, qs, body, who)   # a vote, or a run started or finished
            return Response.of(got) if got else NOT_FOUND
        if self.intake and path.startswith("/api/intake"):
            return Response.of(self.intake.handle("POST", path, qs, body, local))
        if (got := self.changes_route("POST", path, qs, body, who)) is not None:
            return got
        if (got := self.people_page("POST", path, qs, body)) is not None:
            return got
        return NOT_FOUND

    def get(self, path: str, qs: dict, req: Request) -> Response:
        one = lambda k, d="": (qs.get(k) or [d])[0]
        if re.fullmatch(r"/api/albums/(\d+)/download", path):
            return Response(501, b"the album download is streamed: ask the HTTP server", "text/plain")
        if path.startswith("/video/"):    # so a test, or a caller with no socket, can still ask whether it is there
            got = self.images.video_file(path[len("/video/"):])
            return Response(501, b"a video is streamed: ask the HTTP server", got[1]) if got else NOT_FOUND
        if self.intake and path.startswith(("/setup", "/api/intake")):
            return Response.of(self.intake.handle("GET", path, qs, None, req.local))
        who = self.who(req.headers)
        for got in (self.people_page("GET", path, qs, None), self.changes_route("GET", path, qs, None, who)):
            if got is not None:
                return got
        if path == "/mcp":
            return self.mcp_route("GET", req, None)
        if path in ("/", "/index.html"):  # revalidated each load, so edits show up at once
            return _etagged(pages.page("search.html"), "text/html; charset=utf-8")
        if self.compare and path in ("/compare", "/compare.html"):
            return _etagged(pages.page("compare.html"), "text/html; charset=utf-8")
        if self.compare and path.startswith("/api/compare"):
            got = self.compare.handle("GET", path, qs, None)
            return Response.of(got) if got else NOT_FOUND
        got = (self.browse.handle("GET", path, qs, None, req.headers)
               or self.admin.handle("GET", path, qs, None, who, self.can_write(req.headers, req.local))
               or self.images.handle("GET", path, qs, None))
        if got:
            return Response.of(got)
        c = self.live.cat
        if path == "/api/info":   # (compare: the experiment is on, so the Library's menu offers it)
            return json_response({**c.info, "compare": True} if self.compare else c.info)
        if path == "/api/sync":   # what changed since a device last asked (sync.py)
            if not self.feed.ready:
                return json_response({"error": "starting"}, 503, {"Retry-After": "5"})
            since, limit = one("since", "0"), int_param(qs, "limit", 2000, min=1, max=10000)
            ids, deleted, (ver, after), more = self.feed.page(one("epoch"), int(since) if since.isdigit() else 0,
                                                              one("after"), limit)
            rows = [dict(sha=s, **c.feed_row(s)) for s in ids if s in c.meta]
            return json_response({"epoch": self.feed.epoch, "photos": rows, "deleted": deleted, "since": ver,
                                  "after": after, "more": more, "total": c.info["total"],
                                  "trips": [] if more else c.trip_list})
        if path in self.app_files:
            f = self.app_files[path]
            return _etagged(f.read_bytes(), APP_TYPES[f.suffix])
        if (mod := pages.module(path)) is not None:   # a script module a page imports; revalidated like the page
            return _etagged(mod, pages.MODULE_TYPE)
        return NOT_FOUND

    def people_page(self, method: str, path: str, qs: dict, body) -> Response | None:
        """A People page request (faces.py), or None if the path isn't one. Writes were authorized in route()."""
        if not self.live.cat.has_faces or not path.startswith(("/people", "/face/", "/api/people")):
            return None
        s = self.people.snapshot()
        page = method == "GET" and path.rstrip("/") == "/people"
        if s.value is None and s.phase is Phase.FAILED:   # nothing loaded to keep answering: say why, don't spin
            if page:
                return Response(500, pages.waiting("Faces could not be loaded", f"{html.escape(s.error or '')}. The rest"
                                                   " of the library works; the faces load again after the next"
                                                   " ingest or restart.", refresh=False), "text/html; charset=utf-8")
            return Response(500, f"Faces could not be loaded: {s.error}".encode(), "text/plain")
        if s.value is None:
            if page:   # the page itself: wait, and ask again
                return Response(503, pages.waiting("Finding everyone", "Faces load after the library does. This takes"
                                                   " under a minute; the page opens by itself."), "text/html; charset=utf-8")
            return Response(503, b"Faces are still loading; try again in a moment.", "text/plain")
        if method == "POST" and not isinstance(body, dict):
            return json_response({"error": "expected a JSON object"}, 400)
        return Response.of(s.value.handle(method, path, qs, body))

    def changes_route(self, method: str, path: str, qs: dict, body, who: str) -> Response | None:
        """Change sets (changes.py) and tasks (tasks.py): the review page and their API. Proposing, deciding
        and applying change what everyone sees: LOCAL_OR_WRITER, checked in route()."""
        if not path.startswith(("/changes", "/api/changes", "/api/albums", "/api/tasks")):
            return None
        return Response.of((self.tasks if path.startswith("/api/tasks") else self.changes).handle(method, path, qs, body, who))

    def mcp_route(self, method: str, req: Request, body) -> Response:
        """The library as tools for an agent (estate_mcp.py). Anyone who can open the app can search with
        it; proposing a change set takes what the Changes page takes."""
        return Response.of(self.mcp.handle(method, req.headers, body, self.can_write(req.headers, req.local),
                                           self.who(req.headers), req.local))

    def favorite(self, body, who: str) -> Response:
        """POST /api/favorite {sha, fav}: a photo is, or is no longer, a favourite. It changes what everyone
        sees (a month's cover is a favourite if it has one): LOCAL_OR_WRITER, checked in route()."""
        sha, on = (body.get("sha"), body.get("fav")) if isinstance(body, dict) else (None, None)
        with self.live.gate:              # (here without it: handle() leaves it to set_favorites)
            known = isinstance(sha, str) and sha in self.live.cat.index
        if not known or not isinstance(on, bool):
            return json_response({"error": "say which photo (sha) and fav: true or false"}, 400)
        self.set_favorites([sha], on, who)
        with self.live.gate:
            n = self.live.cat.info["fav"]
        return json_response({"sha": sha, "fav": on, "favourites": n})

    def album_files(self, sid: int):
        """The album's title and its (original file, name in the zip) pairs, or None if there is no such album."""
        with self.live.gate:              # only to say which files; the copying must not hold up a reload
            got, meta = self.changes.shas(sid), self.live.cat.meta
            if not got:
                return None
            return got[0], [(self.images.original(s, meta[s]["ext"]),
                             (meta[s]["path"] or "").rsplit("/", 1)[-1] or f"{s[:12]}{meta[s]['ext']}")
                            for s in got[1] if s in meta]

    # -- serving ------------------------------------------------------------------------------------

    def make_server(self, host: str = "127.0.0.1", port: int | None = None) -> Server:
        return Server((host, self.port if port is None else port), make_handler(self))

    def serve(self) -> None:
        srv = self.make_server()
        print(f"Open http://127.0.0.1:{self.port}/  (Ctrl-C to stop)", flush=True)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass


class Server(ThreadingHTTPServer):
    # The default listen backlog is 5. Behind `tailscale serve`, every HTTP/2 stream becomes its own
    # connection here, so a screenful of thumbnails overflows it and the proxy answers 502.
    request_queue_size = 256
    daemon_threads = True

    def handle_error(self, request, client_address):
        # A client that goes away ends its connection: a phone that sleeps, a closed tab, a proxy that
        # drops an idle keep-alive connection, or one that resets it while the next request is awaited.
        # That is not an error, wherever in a request it happens, so it is not printed. Anything else is,
        # including a refused connection, which would be this server's own call to something else failing.
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def make_handler(app: PhotoApp):
    """The HTTP layer: read the request (a bounded body), ask app.handle, write the Response (a 304 for a
    matching ETag, gzip for large text). Phone uploads and album downloads stream here instead."""

    class Handler(BaseHTTPRequestHandler):
        # HTTP/1.1 keeps connections open: `tailscale serve` (and browsers on plain http) reuse them
        # instead of paying a new connection and thread per request. Idle ones close after `timeout`.
        protocol_version = "HTTP/1.1"
        timeout = 60
        disable_nagle_algorithm = True  # headers and body go out as separate writes; don't hold the second

        def log_message(self, *args):
            pass

        def send(self, r: Response):
            body, cache, etag = r.body, r.cache, r.etag
            if etag and self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", cache)
                return self.end_headers()
            text = r.content_type.startswith(("application/json", "text/", "application/octet-stream"))
            gzipped = text and len(body) > 1024 and "gzip" in self.headers.get("Accept-Encoding", "")
            if gzipped:  # JSON and the page shrink 4-6x; JPEGs don't
                body = gzip.compress(body, 5)
            self.send_response(r.status)
            self.send_header("Content-Type", r.content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            if etag:
                self.send_header("ETag", etag)
            if text:
                self.send_header("Vary", "Accept-Encoding")
            if gzipped:
                self.send_header("Content-Encoding", "gzip")
            for k, val in r.headers.items():
                if k != "ETag":
                    self.send_header(k, val)
            self.end_headers()
            self.wfile.write(body)

        def answer(self, method: str):
            path = urlparse(self.path).path
            if method in ("PUT", "POST") and path.startswith("/api/upload/"):   # the system uploader
                return self.upload()
            if method == "GET" and (m := re.fullmatch(r"/api/albums/(\d+)/download", path)):
                return self.download(int(m[1]))
            if method == "GET" and path.startswith("/video/"):
                return self.video(path[len("/video/"):])
            body = b""
            if method == "POST":
                try:
                    body = read_body(self.rfile, self.headers, MAX_BODY)
                except HTTPError as e:   # nothing was read: the body must not be taken for the next request
                    self.close_connection = True
                    return self.send(e.response)
            self.send(app.handle(Request(method, self.path, self.headers, body, is_local(self))))

        def do_GET(self):
            self.answer("GET")

        def do_POST(self):
            self.answer("POST")

        def do_PUT(self):
            self.answer("PUT")

        def do_OPTIONS(self):
            self.answer("OPTIONS")

        def upload(self):
            """PUT or POST /api/upload/...: one file from the phone (admin.py). The connection stays open
            only if the client wants it to (no `Connection: close`) *and* admin read the whole body: an
            early answer leaves the body on the wire, where it would be read as the next request."""
            client_closes = self.close_connection   # parse_request set it from `Connection: close` or HTTP/1.0
            self.close_connection = True            # until admin says the body was read
            got, unread = app.admin.upload(self.command, urlparse(self.path).path, self.headers, self.rfile,
                                           app.who(self.headers), app.can_write(self.headers, is_local(self)))
            self.close_connection = client_closes or unread
            self.send(Response.of(got))

        def video(self, sha: str):
            """GET /video/<sha>: a video, or a Live Photo's clip, as stored, read from the disk as it is sent
            (a video can be gigabytes). Answers a Range request with 206 and just those bytes: a player asks
            for the start, then the index at the end, then wherever it is dragged to, and Safari plays nothing
            from a server that cannot. An unsatisfiable range is a 416."""
            with app.live.gate:              # which file: one look at the catalog, then the gate is let go
                got = app.images.video_file(sha)
            if got is None:
                return self.send(NOT_FOUND)
            src, ctype, name = got
            size = src.stat().st_size
            try:
                span = byte_range(self.headers.get("Range"), size)
            except ValueError:
                return self.send(Response(416, b"", "text/plain", "no-store", {"Content-Range": f"bytes */{size}"}))
            start, end = span or (0, size - 1)
            self.send_response(206 if span else 200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            if span:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Cache-Control", "private, max-age=2592000, immutable")   # named by its hash, as the images are
            self.send_header("Content-Disposition", f"inline; filename*=UTF-8''{quote(name)}")
            self.end_headers()
            left = end - start + 1
            try:
                with open(src, "rb") as f:
                    f.seek(start)
                    while left > 0 and (chunk := f.read(min(left, 256 * 1024))):
                        self.wfile.write(chunk)
                        left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):   # the player has what it wanted, or moved on
                pass
            if left:
                self.close_connection = True

        def download(self, sid: int):
            """GET /api/albums/<id>/download: the album's original files as one zip, written as it is read
            (nothing is held in memory or on disk). Names are the files' own, numbered when two are alike."""
            import zipfile
            got = app.album_files(sid)
            if not got:
                return self.send(json_response({"error": "no such album"}, 404))
            title, files = got
            name = re.sub(r"[^\w .-]+", " ", title).strip() or f"album-{sid}"
            self.close_connection = True         # its length is not known beforehand: the end of the connection ends it
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(name)}.zip")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            used = Counter()
            with zipfile.ZipFile(self.wfile, "w", zipfile.ZIP_STORED) as z:
                for src, base in files:
                    used[base.lower()] += 1
                    if used[base.lower()] > 1:
                        stem, dot, ext = base.rpartition(".")
                        base = f"{stem} ({used[base.lower()]}).{ext}" if dot else f"{base} ({used[base.lower()]})"
                    try:
                        z.write(src, base)
                    except OSError:              # a file that is not on the disk: the rest still come
                        continue

    return Handler
