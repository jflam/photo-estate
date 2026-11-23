"""
photo_app.py — the photo app (`embed.py serve`) as an object: PhotoApp holds the server's state and
wires the route modules together; make_handler() is the thin HTTP layer in front of it.

  PhotoApp(a)                 set the app up from `embed.py serve`'s options (embed.parser()); reads the
                              catalog, starts no thread (with --demo, a scratch copy of demo/estate.db)
  .start()                    the background work: faces, the search model, the sync feed, thumbnails,
                              the inbox sweep, backups, the release timer, the listeners and the transcription job
  .handle(Request) -> Response   one request, in process: every route but the album download
  .request(method, target, headers=, body=, local=) -> Response   the same, for tests and tools
  .make_server(host, port) -> Server   a ThreadingHTTPServer for it (port 0: any free port)
  .serve()                    make_server on 127.0.0.1:--port and serve until Ctrl-C
  .reload_catalog()           re-read the catalog and swap it in (after an ingest, or a change set)
  .set_favorites(shas, on, who, actor=, subject=) -> int   record favourites (and journal it), show them, tell the phones
  .who(headers, local) -> Member | None  who a request is for (members.py)
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
favourites, album downloads (streamed by the handler), re-reading the catalog after an ingest, and the
members (members.py):

  GET  /request-access, POST /api/request-access {name?}   a login nobody has let in asks to be
  GET  /api/me                        the member a request is for: name, role, status, upload policy
  GET  /members, /api/members         (sysadmin) every member with its logins; the people to link to
  POST /api/members/<id> {name?, role?, status?, upload_policy?, person_id?}   (sysadmin) approve, revoke...
  GET  /members?be=<login>            --demo only: be that demo member in this browser (a cookie)

the member's inbox (member_inbox.py): what waits for them to decide, each item opening the screen that decides it

  GET  /inbox, /api/inbox             the page; the member's open items, newest first, and how many
  POST /api/inbox/<id>/dismiss        put one of their own away (404 for anyone else's)

the phone gate's opt-outs (private.py: tombstones), the member's own: an upload of an opted-out photo's bytes from
them is refused (admin.py)

  GET  /api/tombstones                the hashes they opted out of, not taken back
  POST /api/tombstones {add?: [sha256], lift?: [sha256]}   opt out, or take an opt-out back

a member's own agents (members.py: agent_bindings; multiplayer phase 8): /mcp with a member's login is their agent on
the device it calls from (PhotoApp.device: the tailnet node `tailscale serve` forwarded it from, named by `tailscale
whois`), pending until they allow it here, from another device

  GET  /agents, /api/agents           Connect your agent: how to (the address of /mcp), and their agents with each state
  POST /api/agents/<id> {allow}       allow one (from another device than its own: 403 otherwise), or turn it off

questions and stories (stories.py; multiplayer phase 7): the member's questions, dealt by their inbox within a
weekly allowance, answered typed or spoken; stories told about photos; a recording as its own body (an audio
Content-Type, up to 8 MiB), kept in the store with no assets row and written out later by transcribe.py's job

  GET  /api/questions                 the member's open questions, as their inbox items, each photo a search row
  POST /api/questions/<id>/answer     {text}, or the recording as the body (?seconds=: its length, as measured): a story (201)
  POST /api/questions/<id>/dismiss    "I don't know": put away for good
  POST /api/stories {photos, text}    a story with no question (a recording: the body, ?photos=<sha>,<sha>)
  GET  /api/photos/<sha>/stories      a photo's stories the member may read; /api/albums/<id>/stories a series'
  GET  /api/stories/<id>/recording    the recording, with Range, to a member who may read the story

the listeners (listeners.py), one thread start() runs, and the transcription job (transcribe.Job); and what each
member sees through (lenses.py), outside the gate like a favourite, so a change is in every view before the answer:

  GET  /api/lenses                    the member's lenses, the estate defaults, and those others publish
  POST /api/lenses {title}            a new filter lens of the member's
  POST /api/lenses/<id>/subscribe {mode: live | snapshot, title?}   (an estate default: back in)
  POST /api/lenses/<id>/unsubscribe   (an estate default: opt out)
  POST /api/lenses/<id>/publish {on}, /propose, /decide {accept} (a sysadmin)
  POST /api/albums/<id>/publish {on}  an album of the member's as a series
  POST /api/photos/<sha>/show {on}    "show it anyway"
  GET  /api/photos/<sha>/why          why the member does not see a photo

Every route answers for the member the request is for: what they see is catalog.Live.visible_for(member)
(their filters; their favourites; their stacks, trips and counts), never one mask for everyone. /api/sync
is the member's own feed (sync-feed.db for the first sysadmin, sync-feed-<id>.db for anyone else, made the
first time one of their phones asks).

Who may do what (http_util.py has the rule; ROUTE_AUTH below): only active members may do anything at all
(Auth.MEMBER), and a request from this computer itself is the sysadmin. Letting people in, bringing photos
in from a folder (/setup, /api/intake) and restarting take a sysadmin (Auth.SYSADMIN). Open to anyone who
can reach the app (Auth.PUBLIC): the request-access page, the home-screen app files, and /api/info, which
tells a non-member only that the app is up. A browser refused a page is shown the request-access page; any
other refused request is 403 {"error": "not allowed"}. With --demo the Tailscale header is not read: the
X-Demo-Member header or the demo_member cookie says which demo member a request is, and with neither it is
the demo's sysadmin. On the first start of a catalog with no members, the --upload-user logins become
active members, the first (or --sysadmin) the sysadmin; after that members are kept on /members.

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
from dataclasses import replace
from datetime import datetime, timezone
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import catalog
import journal
import lenses
import listeners
import member_inbox
import members
import pages   # the pages, with the shared stylesheet put in
import private
import stories
from admin import Admin, backups
from browse import Browse
from changes import Changes
from core import config, schema
from core.lifecycle import Lifecycle, Phase, Unavailable
from embed import connect, now
from encoder import Encoder
from estate_mcp import TOKEN_HEADER, AppFacade, Server as MCPServer
from http_util import (FORBIDDEN, MAX_BODY, NOT_FOUND, Auth, HTTPError, Request, Response, byte_range, int_param,
                       json_response, parse_json, read_body)
from images import Images
from intake.server import is_local
from members import Member
from sync import Feed
from tasks import Tasks
from transcribe import Job as TranscribeJob
from upload import IngestJob

HERE = Path(__file__).resolve().parent

# Who may make each request: the first entry whose method and path prefix match. Anything not listed
# takes Auth.MEMBER: an active member, or this computer. Upload bodies stream and are checked in admin.py
# with that rule (Auth.MEMBER); the album download and /video/ stream too, and the handler asks
# PhotoApp.refusal before it sends them.
ROUTE_AUTH: tuple[tuple[str, tuple[str, ...], Auth], ...] = (
    ("GET", ("/request-access", "/manifest.webmanifest", "/sw.js", "/icon-"), Auth.PUBLIC),  # asking, and the home-screen app
    ("POST", ("/api/request-access",), Auth.PUBLIC),
    ("GET", ("/api/info",), Auth.PUBLIC),                   # a non-member is told only that the app is up
    ("GET", ("/members", "/api/members"), Auth.SYSADMIN),   # letting people in
    ("POST", ("/api/members",), Auth.SYSADMIN),
    ("GET", ("/setup", "/api/intake"), Auth.SYSADMIN),      # bringing photos in: a job reads any folder here
    ("POST", ("/api/intake", "/api/restart"), Auth.SYSADMIN),
    # Looking after the machine: what is on its disk and in its inbox counts every member's photos, their private
    # ones too (the inbox's run log names files).
    ("GET", ("/stats", "/api/stats", "/api/ingest"), Auth.SYSADMIN),
)
DEMO_HEADER, DEMO_COOKIE = "X-Demo-Member", "demo_member"   # --demo only: which demo member a request is
DEMO_DEVICE = "X-Demo-Device"     # --demo only: the device a request says it comes from (what whois would name)
WHOIS_FOR = 600.0                 # seconds a device's name (tailscale whois) is kept; a failure, a tenth of that
TAILSCALE = ("/Applications/Tailscale.app/Contents/MacOS/Tailscale",)   # the macOS app's CLI, when none is on PATH
# Requests that don't hold the catalog gate: they read no catalog, or only briefly, and can be slow.
UNGATED = ("/api/upload/", "/api/intake", "/setup")
FAVORITE = re.compile(r"/api/favorite|/api/albums/\d+/favorite")   # (POST) the gate's write side, in set_favorites
# (POST) what a member sees through, changed (lenses.py): the gate's write side, in relook
LENS_WRITE = re.compile(r"/api/lenses(?:/(\d+)/(subscribe|unsubscribe|publish|propose|decide))?"
                        r"|/api/albums/(\d+)/publish|/api/photos/([0-9a-f]{64})/show")
PRIVATE_WRITE = "/api/private"   # (POST) photos made private or released (private.py): the gate's write side, in relook
APP_TYPES = {".webmanifest": "application/manifest+json", ".js": "text/javascript; charset=utf-8",
             ".png": "image/png"}


def tailscale_whois(addr: str) -> tuple[str, str] | None:
    """(node, name) of the tailnet node at this address, from `tailscale whois --json` (its Node.StableID, and its
    ComputedName, else the first label of its MagicDNS Name), or None when it cannot be asked or does not say."""
    import shutil
    import subprocess
    if not re.fullmatch(r"[0-9a-fA-F.:]+", addr):
        return None
    node: dict = {}
    # Each CLI in turn until one answers: the one on PATH may be a wrapper that cannot be run (the mini's is a
    # script with no usable first line, which a shell runs and exec does not).
    for exe in dict.fromkeys(x for x in (shutil.which("tailscale"), *TAILSCALE) if x and os.path.exists(x)):
        try:
            got = subprocess.run([exe, "whois", "--json", addr], capture_output=True, text=True, timeout=4)
            node = (json.loads(got.stdout) if got.returncode == 0 else {}).get("Node") or {}
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
        if node.get("StableID"):
            break
    name = node.get("ComputedName") or (node.get("Name") or "").split(".")[0]
    return (f"node:{node['StableID']}", name or addr) if node.get("StableID") else None


def policy(method: str, path: str) -> Auth:
    for m, prefixes, rule in ROUTE_AUTH:
        if method == m and path.startswith(prefixes):
            return rule
    return Auth.MEMBER


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
        # Recordings of stories (stories.py) go into the store as originals do; the demo's store is checked in and
        # never written to, so its recordings go beside its scratch catalog.
        self.recordings = Path(a.db).resolve().parent / "recordings" if a.demo else store
        # Listeners (listeners.py): one thread reads the journal and acts (start(): listen), every listen_every
        # seconds, and at once when woken (a reload, a story told or written out).
        self.listen_every, self.listen_wake, self.listen_stop = 30.0, threading.Event(), threading.Event()
        con = connect(a.db)               # the tables this app writes itself (core/schema.py), made when it opens
        schema.ensure(con, "catalog", "favorites", "members", "lenses", "stories")   # (catalog: sharers and private, in a demo's scratch copy)
        # Members (members.py). The demo makes its own in its scratch catalog; a catalog with none yet takes
        # the --upload-user logins, as they could write before.
        made = members.demo(con) if a.demo else members.first_start(con, a.upload_user, a.sysadmin)
        # The estate's agent, before the looks are read: as itself (the family archivist) it is shown only what is
        # private to nobody (private.Private.agents)
        self.agent = members.agent(con).id
        con.close()
        if made:
            print(f"members: {', '.join(f'{m.login} ({m.role})' for m in made)} let in", flush=True)
        self.load_members()
        if not self.roster and not a.demo:
            print("members: none yet, so only this computer can use the app (name one with --sysadmin LOGIN)", flush=True)
        try:
            mark = self.journal_end()
            self.live = live = catalog.Live(self.load_catalog(), self.read_looks())
            self.loaded_through = mark       # the newest journal entry the catalog being served was read after
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
        # (sync.py), one per member, of what that member sees: a photo one member's filters hide leaves only
        # that member's phones. The feeds' versions live beside the catalog, never in it. The first sysadmin's
        # is sync-feed.db, as before members (the owner's phone does not fetch the library again); anyone
        # else's is sync-feed-<member id>.db, made the first time one of their phones asks (feed_of), and
        # kept up to date from then on (feed_update), so the cost of a feed is paid only for members with one.
        self.feed_dir = Path(a.db).resolve().parent
        self.feed = Feed(self.feed_dir / "sync-feed.db")
        self.feed_member = self.sysadmin.id if self.sysadmin else None   # whose view sync-feed.db is
        self.feeds: dict[int | None, Feed] = {self.feed_member: self.feed}
        for f in sorted(self.feed_dir.glob("sync-feed-*.db")):
            if (mid := f.stem.removeprefix("sync-feed-")).isdigit() and int(mid) != self.feed_member:
                self.feeds[int(mid)] = Feed(f)
        self.feeds_lock = threading.Lock()
        # Home-screen app files (manifest, icons, service worker), served from the site root: a service
        # worker only controls pages at or below its own path.
        app_dir = HERE / "app"
        # The pages' script modules are in app/ too; those are served under /app/ (pages.module), not here.
        self.app_files = ({f"/{p.name}": p for p in app_dir.iterdir()
                           if p.suffix in APP_TYPES and (p.suffix != ".js" or p.name in pages.NOT_MODULES)}
                          if app_dir.is_dir() else {})

        self.qlog = qlog = Path(a.query_log or Path(a.db).resolve().parent / "query-log.jsonl")
        self.browse = browse = Browse(live, self.encoder, a.db, a.model, qlog, a.match_floor, a.match_window, a.best_window)
        self.images = Images(live, store, cache, seed, may_see=self.may_see)
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
        # (the `favorites` table) and wins. It shows at once: the member's view (its rows, the count) and every
        # cached order and cover are brought up to date here, and the looks (lenses.State, which every view is
        # worked out from) are told, so a view worked out again agrees. fav_write puts one set_favorites, or
        # one re-reading of the looks (relook), at a time. Lock order: fav_write, then live.gate (catalog.Gate).
        self.fav_write = threading.Lock()
        # Re-reading the catalog is in one of three states: idle, running, or rerun (asked again while
        # running: when this run ends, another starts, so nothing that was asked for is missed).
        self.reload_state = {"state": "idle", "at": None, "seconds": None, "error": None}
        self.reload_lock = threading.Lock()
        # Photos private "until Dec 26" are released by a timer (release_timer, started in start()): it asks every
        # `release_every` seconds whether a day has come, by `today` (the computer's own date: the family's).
        self.release_every, self.release_stop = 60.0, threading.Event()
        self.today = lambda: datetime.now().date().isoformat()
        self.clock = lambda: datetime.now(timezone.utc)      # what the questions' weekly allowance counts by (stories.deal)
        # Change sets (changes.py). When one hides or shows photos, the catalog is re-read as after an ingest:
        # every view, count and cover is rebuilt without them, phones are told through the feed, and the
        # People page loads again without their faces. `settling` counts the moments before the reload starts.
        self.settling, self.settling_lock = 0, threading.Lock()
        self.changes = Changes(a.db, facts=lambda sha: self.live.cat.meta.get(sha), shown=self.shown,
                               busy=lambda: self.settling > 0 or self.reload_state["state"] != "idle",
                               on_change=self.on_hidden_change, find=browse.find, favorite=self.set_favorites,
                               visible_for=self.live.visible_for, reading=self.reading, refused=self.refused)
        # Tasks (tasks.py): a request in words, worked on by an agent on this computer that uses the MCP
        # endpoint below like any other client, and ends in a change set.
        self.tasks = Tasks(a.db, config.mcp_url(self.port), command=a.agent_cmd, env_file=a.agent_env,
                           model=a.agent_model, log=lambda msg: print(msg, flush=True))
        if a.demo:   # its worker stops before the scratch catalog it reads is removed at exit (atexit: last in, first out)
            import atexit
            atexit.register(self.tasks.stop)
        self.mcp = MCPServer(AppFacade(find=browse.find, row=browse.row, faces=browse.faces,
                                       overview=browse.overview, changes=self.changes, tasks=self.tasks,
                                       ask=self.ask, stories=self.told, family=self.family, journal=self.journal_for))
        # A member's own agent (/mcp with their login) is bound to the device it calls from (device): its tailnet
        # node, as `tailscale whois` names the address `tailscale serve` forwarded the call from. whois is asked once
        # in a while per address (WHOIS_FOR); a test gives its own.
        self.whois, self.whois_seen = tailscale_whois, {}
        # A story's recording is written out on this computer by a queued job (transcribe.py, a uv script with its
        # own environment: Parakeet on MLX), when one is uploaded and at start; then the listeners hear of it.
        self.transcribe = TranscribeJob(["uv", "run", "--quiet", "--script", str(HERE / "transcribe.py"), "--db", a.db,
                                         "--store", str(self.recordings), "run"],
                                        waiting=self.recordings_waiting, then=self.listen_wake.set,
                                        failed=self.recordings_failed, log=lambda msg: print(msg, flush=True))
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

    def read_looks(self) -> lenses.State:
        """What the tables say about who sees what (lenses.py): every member's view is worked out from it."""
        con = connect(self.a.db)
        try:
            return lenses.read(con)
        finally:
            con.close()

    def relook(self) -> bool:
        """What someone hides, or holds private, has changed: read the looks again, and every member's view is
        worked out anew. Never from inside the gate (a request): it takes the write side. True if what People is
        built without changed (what the estate defaults hide, and the photos private to anyone: faces.py)."""
        with self.fav_write:     # not while a favourite is being shown: it would be lost from the looks
            before = self.live.looks.people_out() if isinstance(self.live.looks, lenses.State) else None
            looks = self.read_looks()
            self.live.relook(looks)
            self.browse.forget()
            self.admin.forget_library()
        return before != looks.people_out()

    def refused(self, sha: str, member: int | None) -> bool:
        """`member` may not be shown this photo, whether or not it is in the catalog being served (private.py)."""
        looks = self.live.looks
        return isinstance(looks, lenses.State) and looks.private.refused(sha, member)

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
        threading.Thread(target=self.release_timer, daemon=True, name="release timer").start()
        con = connect(a.db)
        try:
            listeners.begin(con)      # a listener new to this catalog starts at the journal's end
        finally:
            con.close()
        threading.Thread(target=self.listen, daemon=True, name="listeners").start()
        self.transcribe.start()

    def release_due(self) -> dict[int, int]:
        """Release every private photo whose day (`until`) has come, by `today` (private.release_due); then every
        view anew, the phones' feeds and People. {member: photos released}."""
        con = connect(self.a.db)
        try:
            got = private.release_due(con, self.today())
        finally:
            con.close()
        if got:
            print(f"private: released {sum(got.values())} photos whose day has come", flush=True)
            self.on_hidden_change(self.relook())
        return got

    def release_timer(self) -> None:
        """The background thread start() runs: release_due every `release_every` seconds until release_stop."""
        while not self.release_stop.is_set():
            try:
                self.release_due()
            except Exception as e:       # (a locked catalog, a disk gone): try again next time
                print(f"private: release timer: {type(e).__name__}: {e}", flush=True)
            self.release_stop.wait(self.release_every)

    # -- the journal's listeners, questions and stories (listeners.py, stories.py) -----------------------------

    def journal_end(self) -> int:
        con = connect(self.a.db)
        try:
            return con.execute("SELECT coalesce(max(id), 0) FROM journal").fetchone()[0]
        finally:
            con.close()

    def listen_env(self) -> listeners.Env:
        """What the listeners are given: the app's search as a member sees the library, what the catalog being
        served was read after, and a run of the estate's agent (tasks.py), all outside any request."""
        def find(search, member):
            return self.reading(lambda: self.browse.find(search, member))[0]

        def submit(prompt, acting_for, by):
            got, code = self.tasks.submit(prompt, "", acting_for, started_by=by)
            if code >= 400:
                print(f"listeners: no run for {by}: {got.get('error')}", flush=True)
                return None
            return got["id"]
        return listeners.Env(find=find, loaded=lambda: self.loaded_through, in_catalog=lambda sha: sha in self.live.cat.index,
                             refused=lambda sha, m: self.reading(lambda: self.unseen(sha, m)), submit=submit)

    def listen_once(self) -> dict[str, int]:
        con = connect(self.a.db)
        try:
            return listeners.run(con, self.listen_env())
        finally:
            con.close()

    def listen(self) -> None:
        """The background thread start() runs: every listener, every listen_every seconds or when woken. A search
        model still loading (Unavailable) waits for the next pass; any other failure is printed and tried again."""
        while not self.listen_stop.is_set():
            try:
                self.listen_once()
            except Unavailable:
                pass
            except Exception as e:
                print(f"listeners: {type(e).__name__}: {e}", flush=True)
            self.listen_wake.wait(self.listen_every)
            self.listen_wake.clear()

    def unseen(self, sha: str, member: int | None) -> bool:
        """stories.Refused: `member` may not be shown this photo (not in the catalog served, or private to someone
        else; for the estate's agent as itself and NOBODY, anything private to anyone). Inside the gate."""
        return not self.may_see(sha, member)

    def ask(self, *args) -> dict:
        """The MCP tool ask_member (stories.ask): ValueError for what it refuses, for the agent to read."""
        con = connect(self.a.db)
        try:
            return stories.ask(con, *args, refused=self.unseen)
        except stories.StoryError as e:
            raise ValueError(str(e)) from e
        finally:
            con.close()

    def told(self, shas: list[str], member: int | None) -> dict:
        con = connect(self.a.db)
        try:
            return stories.about(con, shas, member, self.unseen)
        finally:
            con.close()

    def family(self) -> list[dict]:
        """The family's members, for ask_member's `to`: name, id, and who they are in People."""
        con = connect(self.a.db)
        try:
            names = dict(con.execute("SELECT id, name FROM persons")) if members._has(con, "persons") else {}
            return [{"id": m.id, "name": m.name, **({"person": names[m.person_id]} if m.person_id in names else {})}
                    for m in members.family(con)]
        finally:
            con.close()

    def recordings_waiting(self) -> int:
        con = connect(self.a.db)
        try:
            return len(stories.pending(con))
        finally:
            con.close()

    def recordings_failed(self, why: str) -> None:
        """The transcription job could not run at all: every recording waiting says why (it is tried again)."""
        con = connect(self.a.db)
        try:
            for sid, _, _ in stories.pending(con):
                stories.transcribed(con, sid, error=why)
        finally:
            con.close()

    def feed_of(self, member: int | None, make: bool = False) -> Feed | None:
        """`member`'s feed (None: the first sysadmin's, sync-feed.db); with `make`, made if they have none yet."""
        key = self.feed_member if member is None else member
        with self.feeds_lock:
            feed = self.feeds.get(key)
            if feed is None and make:
                feed = self.feeds[key] = Feed(self.feed_dir / f"sync-feed-{key}.db")
            return feed

    @staticmethod
    def feed_rows(v: catalog.View) -> dict:
        """A feed's rows: every photo the member sees, as its search result plus View.feed_extra."""
        return {s: v.feed_row(s) for s, ok in zip(v.cat.shas, v.visible) if ok}

    def feed_update(self) -> None:
        """Tell every member's feed what they now see (only members who have a feed: see feed_of)."""
        for member, feed in list(self.feeds.items()):
            t = time.time()
            with self.live.gate:
                rows = self.feed_rows(self.live.visible_for(member))
            n = feed.update(rows)
            print(f"sync feed{'' if feed is self.feed else f' of member {member}'}: version {feed.version}, {n} rows changed"
                  f" ({time.time() - t:.1f}s)", flush=True)

    # -- favourites, reloads, change sets ----------------------------------------------------------

    def set_favorites(self, shas, on: bool, who: str, actor: int | None = None, subject: str | None = None,
                      member: int | None = None) -> int:
        """Record them as `member`'s favourites (by default the actor's), show them, tell the phones. Returns how
        many photos it changed. Called outside the gate (handle() lets the favourite routes through without it):
        showing them takes its write side. One favorite.set journal entry by `actor` (members.id) about
        `subject` (the photo, or album:<id>)."""
        member = actor if member is None else member
        feed = self.feed_of(member)
        with self.fav_write:   # one at a time, so the table and the catalog end up with the same last word
            shas = [s for s in dict.fromkeys(shas) if s in self.live.cat.index]
            con = connect(self.a.db)   # the table first, outside the gate: requests aren't held up by SQLite
            try:
                lenses.set_favorites(con, member, shas, on)   # member_favorites: `favorites` is no longer written
                if shas:
                    journal.append(con, actor, "favorite.set", subject or (shas[0] if len(shas) == 1 else "photos"),
                                   {"fav": on, "photos": len(shas), **({"member": member} if member != actor else {})})
                con.commit()
            finally:
                con.close()

            def show(c: catalog.Catalog):   # the catalog being served now, no request reading it
                v = self.live.visible_for(member)
                changed = [s for s in shas if s in c.index and bool(v.fav[c.index[s]]) != on]
                self.live.looks.set_favorites(member, shas, on)     # (lenses.State) a view worked out again agrees
                for key, w in list(self.live.views.items()):        # this member's view (None: the first sysadmin's)
                    if self.live.looks.who(key) == self.live.looks.who(member):
                        w.show_favorites((s, on) for s in shas)
                self.browse.forget()            # covers and "favourites only" lists were worked out without them
                self.admin.forget_library()
                return changed, {s: v.feed_row(s) for s in changed if v.visible[c.index[s]]} if feed and feed.ready else {}
            changed, rows = self.live.update(show)
        if rows and feed:
            feed.touch(rows)
        return len(changed)

    def with_private(self, looks):
        """In the swap that installs a reloaded catalog (Live.install), with no request reading: the looks as they
        are, with the photos private to a sharer read again from the table. Read after the catalog was, so every
        photo it holds has its row here: ingest records a phone upload's `new` row in the transaction that adds
        the photo (private.py), and no member is shown a new upload before it has been checked. Read inside the
        swap, so a photo withdrawn meanwhile (relook) is not undone; the rest of the looks is kept as it is (a
        favourite shown in place is not lost)."""
        if not isinstance(looks, lenses.State):
            return looks
        con = connect(self.a.db)
        try:
            return replace(looks, private=private.read(con, looks.sysadmin))
        finally:
            con.close()

    def caught_up(self, cat: catalog.Catalog) -> None:
        """In the swap that installs a reloaded catalog: what was worked out from the old one is let go."""
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
                try:
                    mark = self.journal_end()
                    fresh = self.load_catalog(self.live.cat.raw)
                    if os.environ.get("PHOTO_ESTATE_CHECK_RELOAD"):   # tools/bench_ingest.py --check
                        print(f"reload check: {fresh.differences(self.load_catalog()) or 'same as a full load'}", flush=True)
                    self.live.install(fresh, then=self.caught_up, looks=self.with_private)
                    self.loaded_through = mark
                    self.listen_wake.set()      # an ingest's new photos are searchable now: the series listener
                    rs.update(at=time.time(), seconds=time.time() - t, error=None)
                    print(f"catalog reloaded: {fresh.N} images in {time.time() - t:.1f}s", flush=True)
                    self.feed_update()
                    if not self.a.no_pregenerate:
                        threading.Thread(target=self.images.pregenerate, daemon=True).start()
                except Exception as e:   # keep serving the old catalog
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

    def hidden_changed(self, people: bool | None = None) -> None:
        """After a change to what someone hides: every view anew (relook, unless done already: then `people`
        says whether the estate defaults' hides changed), every member's feed, and People if they did."""
        try:
            if people is None:
                people = self.relook()
            self.feed_update()
            if people and self.live.cat.has_faces:
                self.load_people()
        finally:
            with self.settling_lock:
                self.settling -= 1

    def on_hidden_change(self, people: bool | None = None) -> None:
        """Changes.on_change (a change set applied or reverted) and the lens routes: hidden_changed, in the
        background; `busy` (the review page's check) is true until it is done."""
        with self.settling_lock:
            self.settling += 1
        threading.Thread(target=self.hidden_changed, args=(people,), daemon=True).start()

    def reading(self, fn):
        """fn(), as a request reads the catalog: inside the gate."""
        with self.live.gate:
            return fn()

    def may_see(self, sha: str, member: int | None) -> bool:
        """Whether `member` may be shown this photo at all (Live.shared_for): in the catalog, and not private to
        sharers they are not one of. For the pixel routes (images.py) and every route that takes a sha."""
        i = self.live.cat.index.get(sha)
        return i is not None and bool(self.live.shared_for(member)[i])

    def shown(self, sha: str, viewer: int | None = None) -> dict:
        """Where the running app shows a photo to `viewer`: in their library, and in their phones' feed (a
        member with no feed yet: as in the library)."""
        v = self.live.visible_for(viewer)
        library = sha in v.cat.index and bool(v.visible[v.cat.index[sha]])
        feed = self.feed_of(viewer)
        return {"library": library, "sync": sha in feed.rows if feed else library}

    # -- who a request is for -----------------------------------------------------------------------

    def load_members(self) -> None:
        """Members change only through this app, so they are kept in memory and read again after a change."""
        con = connect(self.a.db)
        try:
            self.roster: dict[str, Member] = members.by_login(con)
            self.sysadmin: Member | None = members.sysadmin(con)
        finally:
            con.close()

    def login(self, headers) -> str:
        """The login a request comes with: what `tailscale serve` adds (the app itself is bound to 127.0.0.1),
        or with --demo, and only then, the demo's header or cookie instead."""
        if not self.a.demo:
            return members.norm(headers.get("Tailscale-User-Login"))
        got = headers.get(DEMO_HEADER)
        if not got:
            jar = SimpleCookie()
            try:
                jar.load(headers.get("Cookie") or "")
            except CookieError:
                pass
            got = unquote(jar[DEMO_COOKIE].value) if DEMO_COOKIE in jar else ""
        return members.norm(got)

    def who(self, headers, local: bool = False) -> Member | None:
        """The member a request is for, or None (a login nobody has let in, or no login). With --demo, the
        demo member its header or cookie names, and with neither the demo's sysadmin. Otherwise a request
        from this computer is the sysadmin, and one through `tailscale serve` the member its login is."""
        login = self.login(headers)
        if self.a.demo and not login:
            return self.sysadmin
        if local and not self.a.demo:
            if self.sysadmin:
                return replace(self.sysadmin, login="", local=True)
            return Member(None, "this computer", role="sysadmin", upload_policy="everything", local=True)
        return self.roster.get(login)

    def folder_of(self, m: Member | None) -> tuple[str, bool]:
        """Where in the inbox a member's uploads land, and whether the folder before member folders counts as
        theirs too: m<member id>/ (private.member_of reads who shared it back from there), and the inbox itself
        for the first sysadmin's uploads from before (an upload sent again is found where it was). With no
        member (no members yet), the inbox itself, as before."""
        if m is None or m.id is None:
            return "", True
        return f"m{m.id}", bool(self.sysadmin and self.sysadmin.id == m.id)

    def refusal(self, req: Request, m: Member | None = None) -> Response | None:
        """None if ROUTE_AUTH lets `m` (by default, whoever the request is for) make the request. Otherwise
        its answer: for a browser opening a page, a page that says why (the request-access page); for
        anything else 403 {"error": "not allowed"}."""
        m = m or self.who(req.headers, req.local)
        if policy(req.method, urlparse(req.target).path).allows(m):
            return None
        return self.door(req, m, 403) if req.method == "GET" and "text/html" in (req.headers.get("Accept") or "") else FORBIDDEN

    # -- requests -----------------------------------------------------------------------------------

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
                    or method == "POST" and (FAVORITE.fullmatch(path) or LENS_WRITE.fullmatch(path)) is not None
                    or method == "POST" and path == PRIVATE_WRITE)

    def route(self, req: Request) -> Response:
        method, headers, local = req.method, req.headers, req.local
        u = urlparse(req.target)
        path, qs = u.path, parse_qs(u.query)
        if method in ("PUT", "POST") and path.startswith("/api/upload/"):
            # Here from memory; the handler streams it. admin.py checks ROUTE_AUTH's rule itself (MEMBER),
            # with its own 403, since the handler's streamed uploads never come through route().
            m = self.who(headers, local)
            got, _ = self.admin.upload(method, path, {**headers, "Content-Length": str(len(req.body))},
                                       io.BytesIO(req.body), m.login if m else "", Auth.MEMBER.allows(m), self.folder_of(m),
                                       m.id if m else None)
            return Response.of(got)
        if self.a.demo and method == "GET" and path == "/members" and re.search(r"(?:^|&)be=", u.query):   # (be= alone: no cookie)
            return self.be(qs)
        m = self.who(headers, local)
        if (no := self.refusal(req, m)) is not None:
            return no
        if method == "OPTIONS":
            return Response.of(self.admin.handle("OPTIONS", path, {}, None))
        if method == "POST":
            return self.post(path, qs, req, m)
        if method == "GET":
            return self.get(path, qs, req, m)
        return NOT_FOUND

    def post(self, path: str, qs: dict, req: Request, m: Member | None) -> Response:
        who = m.login if m else ""
        if path == "/api/request-access":   # a form's body, or JSON
            return self.ask_in(req, m)
        if path in ("/api/ingest", "/api/restart"):   # (the body, if any, is not looked at)
            return Response.of(self.admin.handle("POST", path, {}, None, who, Auth.MEMBER.allows(m)))
        if path == "/mcp":     # a body that isn't JSON gets JSON-RPC's own parse error (estate_mcp.py)
            try:
                body = parse_json(req.body)
            except HTTPError:
                body = None
            return self.mcp_route("POST", req, body, m)
        if (got := self.stories_route("POST", path, qs, req, m)) is not None:   # (a recording is its own body, not JSON)
            return got
        body = parse_json(req.body)
        if (mid := re.fullmatch(r"/api/members/(\d+)", path)) is not None:
            return self.change_member(int(mid[1]), body, m)
        if path == "/api/favorite":
            return self.favorite(body, who, m.id if m else None)
        if (lw := LENS_WRITE.fullmatch(path)) is not None:
            return self.lens_write(lw, body, m)
        if path == PRIVATE_WRITE:
            return self.private_write(body, m)
        if (ii := re.fullmatch(r"/api/inbox/(\d+)/dismiss", path)) is not None:
            return self.inbox_route(m, int(ii[1]))
        if path == "/api/tombstones":
            return self.tombstones_route(m, body, write=True)
        if (ag := re.fullmatch(r"/api/agents/(\d+)", path)) is not None:
            return self.agents_route(req, m, int(ag[1]), body)
        if self.compare and path.startswith("/api/compare"):
            got = self.compare.handle("POST", path, qs, body, who)   # a vote, or a run started or finished
            return Response.of(got) if got else NOT_FOUND
        if self.intake and path.startswith("/api/intake"):
            return Response.of(self.intake.handle("POST", path, qs, body, req.local))
        if (got := self.changes_route("POST", path, qs, body, who, m.id if m else None)) is not None:
            return got
        if (got := self.people_page("POST", path, qs, body, m.id if m else None)) is not None:
            return got
        return NOT_FOUND

    def get(self, path: str, qs: dict, req: Request, m: Member | None) -> Response:
        one = lambda k, d="": (qs.get(k) or [d])[0]
        if path == "/request-access":
            return self.door(req, m)
        if path == "/api/info" and not Auth.MEMBER.allows(m):   # open: the app is up, and nothing of the library
            return json_response({"member": False})
        if path == "/api/me":
            return json_response(m.public() if m else None)
        if path in ("/members", "/members.html"):
            return _etagged(pages.page("members.html"), "text/html; charset=utf-8")
        if path == "/api/members":
            return self.members_listing(m)
        if path in ("/inbox", "/inbox.html"):
            return _etagged(pages.page("inbox.html"), "text/html; charset=utf-8")
        if path == "/api/inbox":
            return self.inbox_route(m)
        if path == "/api/tombstones":
            return self.tombstones_route(m)
        if path in ("/agents", "/agents.html"):
            return _etagged(pages.page("agents.html"), "text/html; charset=utf-8")
        if path == "/api/agents":
            return self.agents_route(req, m)
        if re.fullmatch(r"/api/albums/(\d+)/download", path):
            return Response(501, b"the album download is streamed: ask the HTTP server", "text/plain")
        if path.startswith("/video/"):    # so a test, or a caller with no socket, can still ask whether it is there
            got = self.images.video_file(path[len("/video/"):], one("original") == "1", m.id if m else None)
            return Response(501, b"a video is streamed: ask the HTTP server", got[1]) if got else NOT_FOUND
        if self.intake and path.startswith(("/setup", "/api/intake")):
            return Response.of(self.intake.handle("GET", path, qs, None, req.local))
        who = m.login if m else ""
        if (got := self.stories_route("GET", path, qs, req, m)) is not None:
            return got
        for got in (self.people_page("GET", path, qs, None, viewer=m.id if m else None),
                    self.changes_route("GET", path, qs, None, who, m.id if m else None),
                    self.lens_read(path, m)):
            if got is not None:
                return got
        if path == "/mcp":
            return self.mcp_route("GET", req, None, m)
        if path in ("/", "/index.html"):  # revalidated each load, so edits show up at once
            return _etagged(pages.page("search.html"), "text/html; charset=utf-8")
        if self.compare and path in ("/compare", "/compare.html"):
            return _etagged(pages.page("compare.html"), "text/html; charset=utf-8")
        if self.compare and path.startswith("/api/compare"):
            got = self.compare.handle("GET", path, qs, None, member=m.id if m else None)
            return Response.of(got) if got else NOT_FOUND
        viewer = m.id if m else None
        got = (self.browse.handle("GET", path, qs, None, req.headers, viewer)
               or self.admin.handle("GET", path, qs, None, who, Auth.MEMBER.allows(m), viewer)
               or self.images.handle("GET", path, qs, None, viewer))
        if got:
            return Response.of(got)
        v = self.live.visible_for(viewer)
        if path == "/api/info":   # (compare: the experiment is on, so the Library's menu offers it)
            return json_response({**v.info, "compare": True} if self.compare else v.info)
        if path == "/api/sync":   # what changed since a device last asked (sync.py), in the member's own feed
            feed = self.feed_of(viewer)
            if feed is None:      # this member's first phone: their feed is made now, from what they see
                limit = int_param(qs, "limit", 2000, min=1, max=10000)   # (a bad number is a 400 first)
                feed = self.feed_of(viewer, make=True)
                assert feed is not None
                feed.update(self.feed_rows(v))
            if not feed.ready:
                return json_response({"error": "starting"}, 503, {"Retry-After": "5"})
            since, limit = one("since", "0"), int_param(qs, "limit", 2000, min=1, max=10000)
            ids, deleted, (ver, after), more = feed.page(one("epoch"), int(since) if since.isdigit() else 0,
                                                         one("after"), limit)
            rows = [dict(sha=s, **v.feed_row(s)) for s in ids if s in v.cat.meta]
            return json_response({"epoch": feed.epoch, "photos": rows, "deleted": deleted, "since": ver,
                                  "after": after, "more": more, "total": v.info["total"],
                                  "trips": [] if more else v.trip_list})
        if path in self.app_files:
            f = self.app_files[path]
            return _etagged(f.read_bytes(), APP_TYPES[f.suffix])
        if (mod := pages.module(path)) is not None:   # a script module a page imports; revalidated like the page
            return _etagged(mod, pages.MODULE_TYPE)
        return NOT_FOUND

    def people_page(self, method: str, path: str, qs: dict, body, actor: int | None = None,
                    viewer: int | None = None) -> Response | None:
        """A People page request (faces.py), or None if the path isn't one. Writes were authorized in route();
        `actor` is the member they are journaled as. People is built once for everyone, without the photos the
        estate defaults hide (faces.face_rows); `viewer`'s own filters apply as a person's faces are listed:
        a face in a photo they do not see is left out (People.handle's `keep`), as is a cover of one."""
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
        keep = see = None
        if method == "GET":
            v = self.live.visible_for(viewer)
            keep = lambda sha: (i := v.cat.index.get(sha)) is not None and bool(v.visible[i])   # noqa: E731
            see = lambda sha: not self.refused(sha, viewer)                                       # noqa: E731
        return Response.of(s.value.handle(method, path, qs, body, actor, keep=keep, see=see))

    def changes_route(self, method: str, path: str, qs: dict, body, who: str, acting_for: int | None = None) -> Response | None:
        """Change sets (changes.py) and tasks (tasks.py): the review page and their API. Proposing, deciding
        and applying change what everyone sees: MEMBER, checked in route(). `acting_for` is the member the
        request is for (members.id): a task records it, and the journal names it as the actor of each write."""
        if not path.startswith(("/changes", "/api/changes", "/api/albums", "/api/tasks")):
            return None
        if path.startswith("/api/tasks"):
            return Response.of(self.tasks.handle(method, path, qs, body, who, acting_for))
        return Response.of(self.changes.handle(method, path, qs, body, who, acting_for))

    def mcp_route(self, method: str, req: Request, body, m: Member | None) -> Response:
        """The library as tools for an agent (estate_mcp.py), on the same rule as every route. A call from this
        computer with no login is the sysadmin, and a run of the estate's agent carries its token. A call with a
        member's login is that member's own agent (multiplayer phase 8), bound to the device it came from
        (members.bind: pending the first time, and an inbox item for them)."""
        agent = None
        if m is not None and m.login and not m.local and m.kind == "person" and m.active and m.id is not None \
                and req.headers.get(TOKEN_HEADER) is None and method == "POST":
            node, device = self.device(req.headers)
            con = connect(self.a.db)
            try:
                agent = {**members.bind(con, m.id, node, device), "name": m.name}
            finally:
                con.close()
        return Response.of(self.mcp.handle(method, req.headers, body, Auth.MEMBER.allows(m), m.login if m else "",
                                           req.local, m.id if m else None, agent))

    def device(self, headers) -> tuple[str, str]:
        """(node, name): the device a request comes from, as far as it can be known. Through `tailscale serve` the
        request carries X-Forwarded-For, which serve sets to the tailnet address of the node that sent it (it replaces
        whatever the client sent); `tailscale whois` names that node (its StableID, and its name). Without whois, the
        address is the node. With --demo, X-Demo-Device says it first (the demo's stand-in, like X-Demo-Member), and
        a request with no forwarded address is from this computer. Otherwise, "unknown"."""
        if self.a.demo and (named := " ".join((headers.get(DEMO_DEVICE) or "").split())[:60]):
            return f"demo:{named.lower()}", named
        addr = (headers.get("X-Forwarded-For") or "").split(",")[-1].strip()
        if not addr:
            return ("local", "this computer") if self.a.demo else ("unknown", "unknown")
        t = time.time()
        seen = self.whois_seen.get(addr)
        if seen is None or t - seen[0] > (WHOIS_FOR if seen[1] else WHOIS_FOR / 10):
            seen = self.whois_seen[addr] = (t, self.whois(addr))
        return seen[1] or (f"addr:{addr}", addr)

    def journal_for(self, member: int | None, cursor: int, limit: int) -> dict:
        """The MCP tool journal_since (journal.for_member): inside the gate, like every tool."""
        con = connect(self.a.db)
        try:
            return journal.for_member(con, member, cursor, limit, refused=self.unseen, loaded=self.loaded_through,
                                      readable=lambda sid: stories.story(con, sid, member, self.unseen))
        finally:
            con.close()

    # -- a member's own agents: Connect your agent (/agents) -------------------------------------------------

    def agents_route(self, req: Request, m: Member | None, bid: int | None = None, body=None) -> Response:
        """GET /api/agents: the member's own agents, one per device, and how to connect one (the address of /mcp as
        this request reached the app); POST /api/agents/<id> {allow: true | false}: allow one (from another device
        than its own) or turn it off. A member's own only (404 for anyone else's)."""
        viewer = m.id if m and not m.local else None
        node, device = self.device(req.headers)
        con = connect(self.a.db)
        try:
            if bid is not None:
                if not isinstance(body, dict) or not isinstance(body.get("allow"), bool):
                    return json_response({"error": "say allow: true or false"}, 400)
                got = members.decide_binding(con, viewer, bid, body["allow"], node)
                print(f"agents: {m.name if m else '?'}'s agent on {got['device']} is now {got['status']}", flush=True)
                return json_response({"binding": got})
            h = req.headers
            host = (h.get("X-Forwarded-Host") or h.get("Host") or f"127.0.0.1:{self.port}").split(",")[0].strip()
            scheme = "https" if (h.get("X-Forwarded-Proto") or "").lower() == "https" else "http"
            return json_response({"bindings": members.bindings(con, viewer) if viewer is not None else [],
                                  "url": f"{scheme}://{host}/mcp", "here": {"node": node, "device": device},
                                  "me": m.name if m else None, "demo": bool(self.a.demo),
                                  "login": m.login if self.a.demo and m else None})
        except members.BindingError as e:
            return json_response({"error": str(e)}, e.status)
        finally:
            con.close()

    # -- questions and stories (stories.py) ----------------------------------------------------------------

    STORY_ROUTE = re.compile(r"/api/questions(?:/(\d+)/(answer|dismiss))?|/api/stories(?:/(\d+)/recording)?"
                             r"|/api/photos/([0-9a-f]{64})/stories|/api/albums/(\d+)/stories")

    def stories_route(self, method: str, path: str, qs: dict, req: Request, m: Member | None) -> Response | None:
        """The member's questions, answering one (typed: JSON {text}; spoken: the recording as the body, with its
        audio Content-Type), telling a story about photos, and reading a photo's or a series' stories, or a story's
        recording (with Range). None if the path is none of these. Inside the gate: every photo is asked may_see."""
        r = self.STORY_ROUTE.fullmatch(path)
        if r is None:
            return None
        qid, what, sid, sha, album = int(r[1]) if r[1] else None, r[2], int(r[3]) if r[3] else None, r[4], r[5]
        viewer, recording = m.id if m else None, None
        con = connect(self.a.db)
        try:
            if method == "GET" and path == "/api/questions":
                stories.deal(con, viewer, self.clock(), self.unseen)
                v = self.live.visible_for(viewer)
                items = stories.waiting(con, viewer, self.unseen)
                for it in items:
                    for q in it["questions"]:
                        q["photos"] = [v.row(x) for x in q["photos"]]
                return json_response({"items": items, "questions": sum(len(it["questions"]) for it in items)})
            if method == "GET" and sha is not None:
                if not self.may_see(sha, viewer):
                    return json_response({"error": "no such photo"}, 404)
                return json_response({"sha": sha, "stories": stories.about(con, [sha], viewer, self.unseen).get(sha, [])})
            if method == "GET" and album is not None:
                got = self.changes.shas(int(album), viewer)
                if got is None:
                    return json_response({"error": "no such album"}, 404)
                return json_response({"album": int(album), "stories": stories.album_stories(con, got[1], viewer, self.unseen)})
            if method == "GET" and sid is not None:
                return self.recording(con, sid, viewer, req)
            if method != "POST" or not (qid is not None or path == "/api/stories"):
                return NOT_FOUND
            if qid is not None and what == "dismiss":
                return json_response(stories.dismiss(con, viewer, qid))
            ctype = (req.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            spoken = ctype.startswith("audio/")
            body = {} if spoken else parse_json(req.body)
            if not isinstance(body, dict):
                return json_response({"error": "expected a JSON object, or a recording"}, 400)
            try:      # (spoken: how long, as the teller's page measured it, ?seconds=)
                seconds = float((qs.get("seconds") or [""])[0]) if spoken and qs.get("seconds") else None
            except ValueError:
                return json_response({"error": "seconds is a number"}, 400)
            recording = stories.save_recording(con, self.recordings, req.body, ctype) if spoken else None
            if qid is not None:
                got = stories.answer(con, viewer, qid, self.unseen, text=body.get("text"), recording=recording, seconds=seconds)
            else:
                photos = body.get("photos") if not spoken else [x for x in (qs.get("photos") or [""])[0].split(",") if x]
                got = stories.tell(con, viewer, photos, self.unseen, text=body.get("text"), recording=recording, seconds=seconds)
        except stories.StoryError as e:
            return json_response({"error": str(e)}, e.status)
        finally:
            con.close()
        if recording:
            self.transcribe.wake.set()
        self.listen_wake.set()
        return json_response(got, 201)

    def recording(self, con, sid: int, viewer: int | None, req: Request) -> Response:
        """GET /api/stories/<id>/recording: the audio, to a member who may read the story (404 otherwise), with Range."""
        got = stories.recording_of(con, sid, viewer, self.unseen)
        f = stories.recording_path(self.recordings, *got) if got else None
        if f is None or not f.exists():
            return json_response({"error": "no such recording"}, 404)
        data, ext = f.read_bytes(), got[1] if got else ""
        etag = f'"{f.stem[:16]}"'
        asked = req.headers.get("Range")
        if asked and req.headers.get("If-Range") not in (None, etag):
            asked = None
        try:
            span = byte_range(asked, len(data))
        except ValueError:
            return Response(416, b"", "text/plain", "no-store", {"Content-Range": f"bytes */{len(data)}"})
        head = {"Accept-Ranges": "bytes", "Content-Disposition": f"inline; filename=story-{sid}{ext}"}
        if span is None:
            return Response(200, data, stories.TYPES.get(ext, "audio/mp4"), "private, max-age=86400", {**head, "ETag": etag})
        a, b = span
        return Response(206, data[a:b + 1], stories.TYPES.get(ext, "audio/mp4"), "private, max-age=86400",
                        {**head, "Content-Range": f"bytes {a}-{b}/{len(data)}"})

    # -- lenses (lenses.py) -----------------------------------------------------------------------

    def lens_read(self, path: str, m: Member | None) -> Response | None:
        """GET /api/lenses (the member's lenses, the estate defaults, those others publish) and
        GET /api/photos/<sha>/why (why the member does not see a photo), or None if the path is neither."""
        viewer = m.id if m else None
        why = re.fullmatch(r"/api/photos/([0-9a-f]{64})/why", path)
        if path != "/api/lenses" and not why:
            return None
        con = connect(self.a.db)
        try:
            looks = lenses.read(con)     # the tables as they are now (the views follow them a moment later)
            if why is None:
                return json_response(lenses.listing(con, looks, viewer))
            sha = why[1]
            if not self.may_see(sha, viewer):   # (a photo they may not be shown is not there, as for /thumb/)
                return json_response({"error": "no such photo"}, 404)
            v = self.live.visible_for(viewer)
            return json_response({**lenses.why(con, looks, viewer, sha), "visible": bool(v.visible[v.cat.index[sha]]),
                                  "private": private.about(con, looks.private, viewer, sha)})
        finally:
            con.close()

    def lens_write(self, lw: re.Match, body, m: Member | None) -> Response:
        """The POST routes of lenses.py (LENS_WRITE): a lens made, subscribed to or left, published, proposed
        or decided; an album published as a series; a photo shown anyway. Outside the gate, like a favourite:
        a change to what a member sees through is in every view (relook) before this answers; the feeds and
        People follow in the background (on_hidden_change)."""
        if not isinstance(body, dict):
            return json_response({"error": "expected a JSON object"}, 400)
        viewer = m.id if m else None
        lid, what, album, sha = int(lw[1]) if lw[1] else None, lw[2], int(lw[3]) if lw[3] else None, lw[4]
        flag = lambda k, d=True: body.get(k, d)   # noqa: E731
        for k in ("on", "accept"):
            if k in body and not isinstance(body[k], bool):
                return json_response({"error": f"{k} is true or false"}, 400)
        relook = True
        con = connect(self.a.db)
        try:
            if sha is not None:
                with self.live.gate:
                    if not self.may_see(sha, viewer):
                        return json_response({"error": "no such photo"}, 404)
                got = lenses.show(con, viewer, sha, flag("on"))
            elif album is not None:
                got, relook = lenses.publish_album(con, viewer, album, flag("on")), False
            elif lid is None:
                got, relook = {"lens": lenses.create(con, viewer, body.get("title")), "changed": True}, False
            else:
                lens = lenses.get(con, lid)
                relook = lens is not None and lens.kind == "filter"
                if what == "subscribe":
                    shas = None
                    if lens is not None and lens.kind == "series" and body.get("mode") == "snapshot" and lens.set_id is not None:
                        with self.live.gate:    # a series' photos, as this member sees them, are what is copied
                            shas = (self.changes.shas(lens.set_id, viewer) or ("", []))[1]
                    got = lenses.subscribe(con, viewer, lid, body.get("mode") or "live", shas, body.get("title"))
                elif what == "unsubscribe":
                    got = lenses.unsubscribe(con, viewer, lid)
                elif what == "publish":
                    got = lenses.publish(con, viewer, lid, flag("on"))
                elif what == "propose":      # (it publishes the lens: a subscription to it may come on)
                    got = lenses.propose(con, viewer, lid)
                else:
                    got = lenses.decide(con, viewer, lid, flag("accept"))
        except lenses.LensError as e:
            return json_response({"error": str(e)}, e.status)
        finally:
            con.close()
        if got.get("changed") and relook:
            self.on_hidden_change(self.relook())
        return json_response(got)

    # -- private photos (private.py) ----------------------------------------------------------------

    def private_write(self, body, m: Member | None) -> Response:
        """POST /api/private {shas | which: held | private, private: true | false, until?: YYYY-MM-DD | null}: the
        member makes photos they shared private (withdrawn), sets the day they are released on, or releases them.
        A photo they may not be shown is a 404, as one that does not exist; one they did not share, a 403. Outside
        the gate, like a lens: the change is in every view (relook) before this answers; the feeds and People follow
        in the background."""
        if not isinstance(body, dict) or not isinstance(body.get("private"), bool):
            return json_response({"error": "say private: true or false"}, 400)
        viewer = m.id if m else None
        if viewer is None:
            return json_response({"error": "only a member's photos are private"}, 403)
        con = connect(self.a.db)
        try:
            if body.get("which") in ("held", "private"):
                shas = private.held_by(con, viewer, body["which"])
            elif isinstance(body.get("shas"), list) and 0 < len(body["shas"]) <= 20000 and \
                    all(isinstance(x, str) and re.fullmatch(r"[0-9a-f]{64}", x) for x in body["shas"]):
                shas = body["shas"]
                with self.live.gate:
                    if not all(self.may_see(sha, viewer) for sha in shas):
                        return json_response({"error": "no such photo"}, 404)
            else:
                return json_response({"error": "say which photos: shas, or which: held | private"}, 400)
            got = private.change(con, viewer, shas, body["private"], body["until"] if "until" in body else private.KEEP)
            if len(shas) == 1 and "shas" in body:   # (the viewer's line: the photo as it is now)
                got["private"] = private.about(con, private.read(con, self.sysadmin.id if self.sysadmin else None), viewer, shas[0])
        except ValueError as e:
            return json_response({"error": str(e)}, 400)
        except private.PrivateError as e:
            return json_response({"error": str(e)}, e.status)
        finally:
            con.close()
        if got["changed"]:
            self.on_hidden_change(self.relook())
        return json_response({**got, "photos": len(shas)})

    # -- the inbox (member_inbox.py) -----------------------------------------------------------------

    def inbox_route(self, m: Member | None, dismiss: int | None = None) -> Response:
        """GET /api/inbox: the member's open items; POST /api/inbox/<id>/dismiss: one of theirs put away. A
        member only ever sees and changes their own (this computer, with no members yet: the items of none)."""
        con = connect(self.a.db)
        viewer = m.id if m else None
        try:
            if dismiss is None:     # (the questions it has room for are dealt now: stories.deal)
                stories.deal(con, viewer, self.clock(), self.unseen)
                return json_response(member_inbox.mine(con, viewer, stories.keep_item(con, viewer, self.unseen)))
            return json_response(member_inbox.dismiss(con, viewer, dismiss))
        except member_inbox.InboxError as e:
            return json_response({"error": str(e)}, e.status)
        finally:
            con.close()

    # -- tombstones: the phone gate's opt-outs (private.py) ---------------------------------------------

    def tombstones_route(self, m: Member | None, body=None, write: bool = False) -> Response:
        """GET /api/tombstones: the member's own opt-outs, not lifted, as hashes (a new phone reads them before its
        first upload); POST /api/tombstones {add?: [sha256], lift?: [sha256]}: opt out of hashes, or take opt-outs
        back. A member's own only; this computer, with no member to be, has none."""
        viewer = m.id if m else None
        if not write:
            con = connect(self.a.db)
            try:
                return json_response({"tombstones": private.tombstones(con, viewer) if viewer else []})
            finally:
                con.close()
        if not isinstance(body, dict):
            return json_response({"error": "expected a JSON object"}, 400)
        con = connect(self.a.db)
        try:
            return json_response(private.change_tombstones(con, viewer or 0, body.get("add") or [], body.get("lift") or []))
        except ValueError as e:
            return json_response({"error": str(e)}, 400)
        except private.PrivateError as e:
            return json_response({"error": str(e)}, e.status)
        finally:
            con.close()

    # -- members (members.py) ---------------------------------------------------------------------

    def door(self, req: Request, m: Member | None, status: int = 200) -> Response:
        """GET /request-access, and the page a browser is shown when it may not open the one it asked for:
        ask to be let in (a login that is no member's), wait (asked), or not let in (revoked). A member
        refused a sysadmin's page is told so."""
        login = m.login if m else self.login(req.headers)
        ask = html.escape(self.sysadmin.name) if self.sysadmin else "whoever looks after this library"
        you, more = f"<b>{html.escape(login)}</b>", ""
        if m is not None and m.active and status == 403:
            title, why = f"Only {ask} can open this", "This page is for whoever looks after the library."
            more = '<a class="btn" href="/">Back to the library</a>'
        elif m is not None and m.active:
            title, why = "You are in", f"You are {html.escape(m.name)}, a member of this library."
            more = '<a class="btn pri" href="/">Open the library</a>'
        elif m is not None and m.status == "pending":
            told = f" {ask} has been told: it is in their inbox." if self.sysadmin else ""
            title, why = f"{ask} will let you in", f"You have asked, as {you}.{told} Once {ask} has said yes, the library opens here."
            more = '<a class="btn" href="/">Try again</a>'
        elif m is not None:
            title, why = "You are not let in", f"{ask} has turned off {you}&rsquo;s way in to this library."
        elif not login:
            title, why = "Who is asking?", ("This library is reached through Tailscale, which says who is asking. Nothing"
                                            " came with this request to say who you are.")
        else:
            title = f"Ask {ask} to let you in"
            why = (f"This is a family&rsquo;s photo library. You are signed in to the tailnet as {you}, who is not one"
                   f" of its members yet. Asking tells {ask} your login and the name you give; nothing else.")
            name = html.escape(req.headers.get("Tailscale-User-Name") or "", quote=True)
            more = (f'<form method="post" action="/api/request-access"><input type="text" name="name" value="{name}"'
                    ' placeholder="Your name" maxlength="80" aria-label="Your name" required>'
                    '<button class="btn pri">Ask to be let in</button></form>')
        if self.a.demo:   # so a person trying the demo can be each of its members, and come back
            more += ('<p class="demo">The demo: be ' + ", ".join(f'<a class="link" href="/members?be={quote(x.login)}">'
                     f'{html.escape(x.name)}</a>' for x in sorted(self.roster.values(), key=lambda x: x.id or 0) if x.active) + "</p>")
        return Response(status, pages.plain(title, why, more), "text/html; charset=utf-8")

    def ask_in(self, req: Request, m: Member | None) -> Response:
        """POST /api/request-access {name?}: a login nobody has let in becomes a pending member. A form's post is
        answered with the request-access page again (303); JSON with {status}."""
        form = (req.headers.get("Content-Type") or "").startswith("application/x-www-form-urlencoded")
        login = self.login(req.headers)
        if m is None:
            if not login:
                return json_response({"error": "no login came with this request"}, 400)
            if form:
                name = (parse_qs(req.body.decode("utf-8", "replace")).get("name") or [""])[0]
            else:
                body = parse_json(req.body)
                name = body.get("name") if isinstance(body, dict) else None
            con = connect(self.a.db)
            try:
                m = members.request(con, login, str(name or req.headers.get("Tailscale-User-Name") or ""))
            finally:
                con.close()
            self.load_members()
            print(f"members: {login} asks to be let in, as {m.name}", flush=True)
        if form:
            return Response(303, b"", "text/plain", "no-store", {"Location": "/request-access"})
        return json_response({"status": m.status})

    def members_listing(self, m: Member | None) -> Response:
        """GET /api/members (sysadmin): every member with its logins, the people a member can be linked to."""
        con = connect(self.a.db)
        try:
            return json_response({"members": members.everyone(con), "persons": members.persons(con),
                                  "me": m.id if m else None, "demo": bool(self.a.demo),
                                  "roles": members.ROLES, "policies": members.POLICIES})
        finally:
            con.close()

    def change_member(self, mid: int, body, m: Member | None) -> Response:
        """POST /api/members/<id> (sysadmin): approve (status active), revoke (revoked), a role, an upload
        policy, a person, a name. 400 for a value members.change refuses, 404 for no such member."""
        if not isinstance(body, dict):
            return json_response({"error": "expected a JSON object"}, 400)
        con = connect(self.a.db)
        try:
            members.change(con, mid, m.id if m else None, **body)
            row = next(x for x in members.everyone(con) if x["id"] == mid)
        except ValueError as e:
            return json_response({"error": str(e)}, 404 if str(e) == "no such member" else 400)
        finally:
            con.close()
        self.load_members()
        self.on_hidden_change()    # (who the first sysadmin is decides whose an owner-less album, lens or favourite is)
        print(f"members: {row['name']} is now {row['status']}, {row['role']} (by {m.name if m else 'this computer'})", flush=True)
        return json_response({"member": row})

    def be(self, qs: dict) -> Response:
        """--demo only: GET /members?be=<login> makes this browser that demo member (the demo_member cookie;
        empty: the demo's sysadmin again), and goes where they may go."""
        login = members.norm((qs.get("be") or [""])[0])
        m = self.roster.get(login) if login else self.sysadmin
        cookie = f"{DEMO_COOKIE}={quote(login, safe='@.')}; Path=/; SameSite=Lax" + ("" if login else "; Max-Age=0")
        return Response(303, b"", "text/plain", "no-store",
                        {"Location": "/members" if m and m.is_sysadmin else "/", "Set-Cookie": cookie})

    def favorite(self, body, who: str, actor: int | None = None) -> Response:
        """POST /api/favorite {sha, fav}: a photo is, or is no longer, a favourite. It changes what everyone
        sees (a month's cover is a favourite if it has one): MEMBER, checked in route()."""
        sha, on = (body.get("sha"), body.get("fav")) if isinstance(body, dict) else (None, None)
        with self.live.gate:              # (here without it: handle() leaves it to set_favorites)
            known = isinstance(sha, str) and self.may_see(sha, actor)   # (one they may not be shown: as one that is not there)
        if not known or not isinstance(on, bool):
            return json_response({"error": "say which photo (sha) and fav: true or false"}, 400)
        self.set_favorites([sha], on, who, actor, sha)
        with self.live.gate:
            n = self.live.visible_for(actor).info["fav"]
        return json_response({"sha": sha, "fav": on, "favourites": n})

    def album_files(self, sid: int, viewer: int | None = None):
        """The album's title and its (original file, name in the zip) pairs, as `viewer` sees the album (its
        photos they see: Changes.shas), or None if there is no such album for them."""
        with self.live.gate:              # only to say which files; the copying must not hold up a reload
            got, meta = self.changes.shas(sid, viewer), self.live.cat.meta
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
            streamed = method == "GET" and (re.fullmatch(r"/api/albums/\d+/download", path) or path.startswith("/video/"))
            if streamed and (no := app.refusal(Request(method, self.path, self.headers, b"", is_local(self)))) is not None:
                return self.send(no)
            viewer = (getattr(app.who(self.headers, is_local(self)), "id", None)) if streamed else None
            if method == "GET" and (m := re.fullmatch(r"/api/albums/(\d+)/download", path)):
                return self.download(int(m[1]), viewer)
            if method == "GET" and path.startswith("/video/"):
                return self.video(path[len("/video/"):], parse_qs(urlparse(self.path).query).get("original") == ["1"], viewer)
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
            m = app.who(self.headers, is_local(self))
            got, unread = app.admin.upload(self.command, urlparse(self.path).path, self.headers, self.rfile,
                                           m.login if m else "", Auth.MEMBER.allows(m), app.folder_of(m), m.id if m else None)
            self.close_connection = client_closes or unread
            self.send(Response.of(got))

        def video(self, sha: str, original: bool = False, viewer: int | None = None):
            """GET /video/<sha>: a video, or a Live Photo's clip, as stored, read from the disk as it is sent
            (a video can be gigabytes). Answers a Range request with 206 and just those bytes: a player asks
            for the start, then the index at the end, then wherever it is dragged to, and Safari plays nothing
            from a server that cannot. An unsatisfiable range is a 416."""
            with app.live.gate:              # which file: one look at the catalog, then the gate is let go
                got = app.images.video_file(sha, original, viewer)
            if got is None:
                return self.send(NOT_FOUND)
            src, ctype, name = got
            size = src.stat().st_size
            # Which file this is, so a browser that holds part of the stored file never joins it to part of
            # the copy made since (or the other way round): a Range sent with another file's If-Range gets
            # the whole of this one.
            etag = f'"{sha[:16]}-{"c" if src.parent.name == "streams" else "o"}"'
            asked = self.headers.get("Range")
            if asked and self.headers.get("If-Range") not in (None, etag):
                asked = None
            try:
                span = byte_range(asked, size)
            except ValueError:
                return self.send(Response(416, b"", "text/plain", "no-store", {"Content-Range": f"bytes */{size}"}))
            start, end = span or (0, size - 1)
            self.send_response(206 if span else 200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("ETag", etag)
            if span:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Cache-Control", "private, max-age=2592000")   # named by its hash; not immutable: a copy to play from may come
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

        def download(self, sid: int, viewer: int | None = None):
            """GET /api/albums/<id>/download: the album's original files as one zip, written as it is read
            (nothing is held in memory or on disk). Names are the files' own, numbered when two are alike."""
            import zipfile
            got = app.album_files(sid, viewer)
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
