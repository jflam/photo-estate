"""The intake API: what the setup page calls, and what an agent will call over MCP (phase 4). Both the
setup server (no library yet) and the full app (embed.py serve) mount it, so it is one object with a
`handle` method that returns (status, body, content type), like faces.People.

  GET  /setup                          the setup page
  GET  /api/intake                     the library, every source (with its guide and checks), every job
  GET  /api/intake/jobs/<id>?after=N   one job, with the lines it printed after event N
  POST /api/intake/library   {root}    where the library lives (setup only, before anything is in it)
  POST /api/intake/preview   {source, config}   what that would bring in; changes nothing
  POST /api/intake/jobs      {source, config}   start bringing it in
  POST /api/intake/jobs/<id>/answer  {value}    what a waiting job asked for (a password, a code, "ok")
  POST /api/intake/jobs/<id>/cancel | retry
  POST /api/intake/choose    {what: folder|file}  the Mac's own Open dialog (only from this Mac)

A query parameter that should be a number and isn't (`job`, `after`) is a 400 {"error"}.

Nothing here returns what a person typed to sign in: `answer` hands it to the running job and forgets it."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from http_util import HTTPError, int_param   # photo-estate/http_util.py, beside this package

from . import sources  # noqa: F401  (registers every source)
from .engine import Engine, media_count
from .media import human_bytes

CONFIG = Path(os.environ.get("PHOTO_ESTATE_CONFIG", Path.home() / ".config" / "photo-estate" / "library.json"))


def saved_root() -> Path | None:
    try:
        root = json.loads(CONFIG.read_text()).get("root")
        return Path(root) if root else None
    except (OSError, ValueError):
        return None


def save_root(root: Path) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps({"root": str(root)}, indent=2) + "\n")


def free_bytes(p: Path) -> int | None:
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return None


def suggestions() -> list[dict]:
    """Places a library could live: the Pictures folder, and each external disk."""
    out = [Path.home() / "Pictures" / "Photo Estate"]
    vols = Path("/Volumes")
    if vols.is_dir():
        for v in sorted(vols.iterdir()):
            if v.is_dir() and not v.is_symlink() and os.access(v, os.W_OK):   # the startup disk is a symlink
                out.append(v / "Photo Estate")
    return [{"root": str(p), "free": free_bytes(p), "free_label": human_bytes(free_bytes(p) or 0),
             "external": str(p).startswith("/Volumes/")} for p in out]


class Library:
    """Where the catalog and store are. In setup mode it may not be chosen yet."""

    def __init__(self, root: str | Path | None, db: str | None = None, store: str | Path | None = None, *,
                 fixed: bool = False, remember: bool = True):
        self.fixed, self.remember = fixed, remember   # fixed: given on the command line, not chosen on the page
        self.set(root, db, store)

    def set(self, root, db=None, store=None):
        self.root: Path | None = Path(root).expanduser().resolve() if root else None
        self.db: str | None = db or (str(self.root / "estate.db") if self.root else None)
        self.store: Path | None = Path(store) if store else (self.root / "store" if self.root else None)

    def chosen(self) -> tuple[Path, str, Path]:
        """(root, db, store) of a library that has been chosen; ValueError before then."""
        if self.root is None or self.db is None or self.store is None:
            raise ValueError("no library has been chosen yet")
        return self.root, self.db, self.store

    def describe(self) -> dict:
        if not self.root:
            return {"chosen": False, "suggestions": suggestions()}
        root, db, store = self.chosen()
        free = free_bytes(store)
        return {"chosen": True, "fixed": self.fixed, "root": str(root), "db": db, "store": str(store),
                "media": media_count(db) if Path(db).exists() else 0,
                "free": free, "free_label": human_bytes(free or 0)}


def consent_page(jid: int) -> bytes:
    """The practice drive's pretend consent page (only in --sandbox)."""
    import pages
    css = (pages.HERE / "app" / "estate.css").read_text()
    return f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Practice drive — sign in</title>
<style>{css}
body {{ min-height: 100dvh; display: grid; place-items: center; padding: 16px; }}
.card {{ max-width: 420px; padding: 32px; text-align: center; }} .card p {{ margin: 10px 0 22px; color: var(--ink-2); }}
.who {{ font-size: 13px; color: var(--muted); margin-bottom: 18px; }}
</style>
<div class="card well"><div class="who label">Practice drive · not a real account</div>
<h1 class="title">Photo Estate wants to read your files</h1>
<p>It can see and download your photos. It cannot change or delete anything.</p>
<div id="b"><button class="btn" onclick="window.close()">Cancel</button>
<button class="btn pri" id="ok">Allow</button></div><p id="done" hidden>Done. You can close this tab and go back to Photo Estate.</p></div>
<script>
document.getElementById('ok').onclick = async () => {{
  await fetch('/api/intake/jobs/{jid}/answer', {{method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: '{{"value":"allow"}}'}});
  document.getElementById('b').hidden = true; document.getElementById('done').hidden = false;
  setTimeout(() => window.close(), 900);
}};
</script>""".encode()


def choose_dialog(what: str, purpose: str = "photos") -> str | None:
    """The Mac's Open dialog, on this Mac's screen. None if cancelled or not a Mac."""
    if sys.platform != "darwin" or not shutil.which("osascript"):
        return None
    kind = "file" if what == "file" else "folder"
    prompt = "Choose where to keep your library" if purpose == "library" else "Choose where your photos are"
    script = f'POSIX path of (choose {kind} with prompt "{prompt}")'
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return None
    return r.stdout.strip() or None if r.returncode == 0 else None


class IntakeAPI:
    def __init__(self, library: Library, make_engine, *, setup_mode: bool, practice: bool = False):
        self.library, self.make_engine = library, make_engine
        self.setup_mode, self.practice = setup_mode, practice
        self.engine: Engine | None = make_engine(library) if library.root else None
        self.becoming_app = False       # setup mode: the first photos are in and the full app is starting

    def state(self) -> dict:
        e = self.engine
        srcs = e.sources() if e else sources_list(self.practice)
        return {"mode": "starting" if self.becoming_app else ("setup" if self.setup_mode else "app"),
                "practice": self.practice, "library": self.library.describe(),
                "sources": [s.describe() for s in srcs], "jobs": e.list() if e else []}

    def handle(self, method: str, path: str, qs: dict, body, local: bool):
        """One request -> (status, body, content type), or None if it isn't ours. A query parameter that is
        not a number (`job`, `after`) is a 400."""
        try:
            return self.route(method, path, qs, body if isinstance(body, dict) else {}, local)
        except HTTPError as x:
            return self.err(x.status, x.message)

    def route(self, method: str, path: str, qs: dict, body: dict, local: bool):
        if method == "GET" and path in ("/setup", "/setup/"):
            import pages
            return 200, pages.page("setup.html"), "text/html; charset=utf-8"
        if method == "GET" and path == "/setup/practice-consent" and self.practice:
            return 200, consent_page(int_param(qs, "job", 0)), "text/html; charset=utf-8"
        if not path.startswith("/api/intake"):
            return None
        parts = path.rstrip("/").split("/")[3:]          # after /api/intake
        if method == "GET" and not parts:
            return self.ok(self.state())
        if method == "POST" and parts == ["choose"]:
            if not local:
                return self.err(403, "The folder picker only opens on the computer the library is on.")
            return self.ok({"path": choose_dialog(body.get("what", "folder"), body.get("for", "photos"))})
        if method == "POST" and parts == ["library"]:
            return self.choose_library(body)
        e = self.engine
        if e is None:
            return self.err(409, "Choose where your library lives first.")
        if method == "POST" and parts == ["preview"]:
            src = e.source(body.get("source", ""))
            if not src:
                return self.err(404, "No such source.")
            return self.ok(asdict(src.preview(body.get("config") or {})))
        if parts[:1] == ["jobs"]:
            if method == "POST" and len(parts) == 1:
                try:
                    return self.ok({"id": e.create(body.get("source", ""), body.get("config") or {})}, 201)
                except ValueError as x:
                    return self.err(400, str(x))
            try:
                jid = int(parts[1])
            except (IndexError, ValueError):
                return self.err(404, "No such job.")
            if method == "GET" and len(parts) == 2:
                v = e.view(jid, int_param(qs, "after", 0, min=0))
                return self.ok(v) if v else self.err(404, "No such job.")
            if method == "POST" and len(parts) == 3 and parts[2] in ("answer", "cancel", "retry"):
                if parts[2] == "answer":
                    done = e.answer(jid, str(body.get("value", "")))
                else:
                    done = getattr(e, parts[2])(jid)
                return self.ok({"ok": True}) if done else self.err(409, "That job isn't waiting for this.")
        return self.err(404, "not found")

    def choose_library(self, body: dict):
        if not self.setup_mode or self.library.fixed:
            return self.err(409, "The library's location is already set.")
        if self.engine and self.engine.jobs.all():
            return self.err(409, "Photos are already coming into this library.")
        raw = (body.get("root") or "").strip()
        if not raw:
            return self.err(400, "Choose a folder.")
        root = Path(os.path.expanduser(raw)).resolve()
        try:
            (root / "store").mkdir(parents=True, exist_ok=True)
        except OSError as x:
            return self.err(400, f"Can't use {root}: {x.strerror or x}.")
        self.library.set(root)
        if self.library.remember:
            save_root(root)
        self.engine = self.make_engine(self.library)
        return self.ok(self.library.describe())

    @staticmethod
    def ok(obj, code=200):
        return code, json.dumps(obj, separators=(",", ":")).encode(), "application/json"

    @staticmethod
    def err(code, msg):
        return code, json.dumps({"error": msg}).encode(), "application/json"


def sources_list(practice: bool):
    from .base import SOURCES
    return [s for s in SOURCES.values() if practice or not s.practice]
