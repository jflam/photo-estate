"""The server that runs before there is a library to show: the setup page and the intake API, nothing
else, so it starts in a moment (no PyTorch, no catalog to read).

`embed.py serve` runs this when there is no catalog with photos in it yet. When the first job's photos
are searchable it turns itself into the full app in place (exec, same port, same process id): the page,
which has been polling, sees the app answer and opens the library. The job carries on with faces in
the new process, which resumes it from intake.db."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .api import IntakeAPI, Library
from .engine import Engine

HERE = Path(__file__).resolve().parent.parent


def is_local(handler) -> bool:
    """A request from a browser on this computer, not one forwarded by a proxy (tailscale serve)."""
    h = handler.headers
    return handler.client_address[0] in ("127.0.0.1", "::1") and not (
        h.get("Tailscale-User-Login") or h.get("X-Forwarded-For") or h.get("Forwarded"))


def make_handler(api: IntakeAPI, extra=None):
    app_dir = HERE / "app"
    icons = {f"/{p.name}": p for p in app_dir.glob("icon-*.png")} if app_dir.is_dir() else {}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 60

        def log_message(self, *args):
            pass

        def send(self, code, body: bytes, ctype: str, headers=()):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def route(self, method):
            u = urlparse(self.path)
            body = None
            if method == "POST":
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                except ValueError:
                    return self.send(400, b'{"error":"bad JSON"}', "application/json")
            if method == "GET" and u.path in ("/", "/index.html", "/people", "/stats"):
                return self.send(302, b"", "text/plain", [("Location", "/setup")])
            if method == "GET" and u.path in icons:
                return self.send(200, icons[u.path].read_bytes(), "image/png")
            if method == "GET" and u.path == "/sw.js":   # an old service worker from the full app: retire it
                return self.send(200, b"self.addEventListener('install',()=>self.skipWaiting());"
                                      b"self.addEventListener('activate',()=>self.registration.unregister());",
                                 "text/javascript")
            got = api.handle(method, u.path, parse_qs(u.query), body, is_local(self))
            if got is None:
                return self.send(404, b"not found", "text/plain")
            self.send(*got)

        def do_GET(self):
            try:
                self.route("GET")
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            try:
                self.route("POST")
            except (BrokenPipeError, ConnectionResetError):
                pass

    return Handler


def run_setup(library: Library, port: int, app_argv, *, practice: bool = False, skip=None) -> None:
    """Serve the setup page until the first photos are searchable, then exec `app_argv(library)`: the full app."""
    srv_box = {}

    def become_app(jid: int) -> None:
        # Called on the engine's thread, between `embed` and `faces`, so nothing else is running.
        api.becoming_app = True
        argv = app_argv(library)
        print(f"first photos are in: starting the photo app ({' '.join(argv[1:])})", flush=True)
        time.sleep(2.5)                      # long enough for the page to see "starting"
        sys.stdout.flush()
        if srv := srv_box.get("srv"):
            srv.socket.close()
        os.execv(argv[0], argv)

    def make_engine(lib: Library) -> Engine:
        e = Engine(lib.root, lib.db, lib.store, HERE, on_ready=become_app, practice=practice, skip=skip)
        e.start()
        return e

    api = IntakeAPI(library, make_engine, setup_mode=True, practice=practice)

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        request_queue_size = 64

    srv = srv_box["srv"] = Server(("127.0.0.1", port), make_handler(api))
    where = f" in {library.root}" if library.root else ""
    print(f"No library{where} yet. Set it up at http://127.0.0.1:{port}/setup  (Ctrl-C to stop)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    except OSError:                          # the socket closed under it: become_app is exec'ing
        threading.Event().wait()
