"""The pages and the script modules they share (issue #89): app/*.js are native ES modules that every
page imports by address (/app/dom.js); nothing is bundled. Checked here, without a browser:

- pages.module() serves the modules and nothing else in app/ (not the tests, not package.json, not the
  service worker, nothing outside the folder);
- the photo app and the setup server send them as JavaScript (a browser refuses a module sent as
  anything else), the photo app revalidated like a page;
- every page imports only modules that exist, and only names those modules export;
- no page has its own copy of a shared helper again (esc, $, api, toast, machine);
- the secondary pages ask the server only through api.js: no fetch() or setInterval() of their own (issue #80);
- the photo app's machines are strict, and their state is in the flow modules, not beside them (issue #81).

The modules' own unit tests are in app/tests/ (`npm test` in photo-estate/app).

  uv run pytest tests/test_pages.py
"""
import re
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import embed  # noqa: E402
import pages  # noqa: E402
from intake.server import make_handler  # noqa: E402
from photo_app import PhotoApp  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PAGES = sorted(p.name for p in ROOT.glob("*.html"))
SHARED = ("dom.js", "api.js", "fsm.js", "toast.js")
IMPORT = re.compile(r"""^import\s*\{([^}]*)\}\s*from\s*["']([^"']+)["'];?""", re.M)
EXPORT = re.compile(r"^export\s+(?:async\s+)?(?:const|let|function\*?|class)\s+([A-Za-z_$][\w$]*)", re.M)
EXPORT_MORE = re.compile(r"^export const [^=]+=[^;]*?,\s*([A-Z_]+) = ", re.M)   # export const A = 1, B = 2;


def script(page: str) -> str:
    """The page's one script."""
    html = (ROOT / page).read_text()
    scripts = re.findall(r"<script([^>]*)>(.*?)</script>", html, re.S)
    assert len(scripts) == 1, f"{page}: {len(scripts)} scripts"
    attrs, body = scripts[0]
    assert attrs.strip() == 'type="module"', f"{page}: its script is not a module ({attrs!r})"
    return body


def exports(module: str) -> set[str]:
    src = (ROOT / "app" / module).read_text()
    return set(EXPORT.findall(src)) | set(EXPORT_MORE.findall(src))


def imports(src: str) -> list[tuple[str, list[str]]]:
    """(address, names) for every import; `a as b` is the name a."""
    return [(addr, [n.split(" as ")[0].strip() for n in names.split(",") if n.strip()]) for names, addr in IMPORT.findall(src)]


def test_the_pages_are_the_ones_we_know():
    assert PAGES == ["changes.html", "compare.html", "inbox.html", "members.html", "people.html", "review.html", "search.html",
                     "setup.html", "stats.html"]


def test_module_serves_the_modules_and_nothing_else():
    for name in SHARED:
        assert pages.module(f"/app/{name}") == (ROOT / "app" / name).read_bytes()
    assert set(SHARED) <= {m.removeprefix("/app/") for m in pages.modules()}
    for path in ("/app/sw.js",                      # the service worker is served from the site root
                 "/app/tests/dom.test.js", "/app/package.json", "/app/tsconfig.json", "/app/estate.css",
                 "/app/node_modules/typescript/lib/tsc.js", "/app/nope.js", "/app/", "/app",
                 "/app/../pages.py", "/app/..%2Fpages.py", "/app/%2e%2e/embed.py", "/app//dom.js", "/app/DOM.js",
                 "/dom.js", "/app/dom.js/", "/app/dom.js?x", "app/dom.js"):
        assert pages.module(path) is None, path
    assert "/app/sw.js" not in pages.modules()


@pytest.mark.parametrize("page", PAGES)
def test_a_page_imports_only_what_exists(page):
    found = imports(script(page))
    assert found, f"{page} imports no module"
    for addr, names in found:
        assert re.fullmatch(r"\.?/app/[a-z-]+\.js", addr), f"{page}: {addr} is not a module's address"
        if page != "review.html":                  # estate.py review's page asks for everything relative to itself
            assert addr.startswith("/app/"), f"{page}: {addr}"
        assert pages.module(addr.lstrip(".")) is not None, f"{page} imports {addr}, which is not served"
        missing = set(names) - exports(addr.rsplit("/", 1)[1])
        assert not missing, f"{page} imports {sorted(missing)} from {addr}, which does not export them"


def test_modules_import_only_each_other():
    for addr in pages.modules():
        for dep, names in imports((ROOT / addr.lstrip("/")).read_text()):
            assert re.fullmatch(r"\./[a-z-]+\.js", dep), f"{addr} imports {dep}: modules import each other as ./name.js"
            assert pages.module("/app/" + dep[2:]) is not None, f"{addr} imports {dep}, which does not exist"
            assert not set(names) - exports(dep[2:]), f"{addr}: {names} from {dep}"


@pytest.mark.parametrize("page", PAGES)
def test_no_page_has_its_own_copy_of_a_shared_helper(page):
    src = script(page)
    for what, pattern in (("esc", r"(?:const|let|var|function)\s+esc\b\s*[=(]"),
                          ("an escaping replace", r"""replace\(/\[&<>"'?\]/g"""),
                          ("$", r"(?:const|let|var|function)\s+\$\$?\s*[=(]"),
                          ("toast", r"(?:const|let|var|function)\s+toast\b"),
                          ("machine", r"function\s+machine\b"),
                          ("a fetch wrapper", r"(?:const|let|var)\s+(?:api|getJSON|postJSON)\s*=\s*async"),
                          ("MARK", r"(?:const|let|var)\s+MARK\b")):
        assert not re.search(pattern, src), f"{page} defines {what} itself: import it from app/"
    assert '"use strict"' not in src, f"{page}: a module is strict already"


@pytest.mark.parametrize("page", [p for p in PAGES if p != "search.html"])
def test_the_secondary_pages_ask_the_server_only_through_api_js(page):
    """Issue #80: fetch() by hand is how a page came to draw an error as if it were data (no `r.ok`),
    and a timer that calls itself is how it came to ask for ever. api.js checks every answer; loop.js
    and fsm.js's poll() are the bounded, cancellable ways to ask again. (search.html: issue #81.)"""
    src = script(page)
    assert not re.search(r"\bfetch\(", src), f"{page} calls fetch() itself: use api() from app/api.js"
    assert not re.search(r"\bsetInterval\(", src), f"{page} polls with setInterval: use loop() or poll()"
    assert "/app/api.js" in src


def test_the_photo_apps_machines_are_strict_and_own_their_state():
    """Issue #81: in search.html a step the table does not have throws, so nothing depends on a refused
    step's `false`; and what a state knows is owned by the state (app/*-flow.js), not kept in variables
    beside the machine, where it outlived the state it belonged to."""
    src = script("search.html")
    for call in re.findall(r"\bmachine\((?:[^()]|\([^()]*\))*\)", src):
        assert "strict: true" in call, f"search.html: a machine that is not strict: {call}"
    assert not re.search(r"(?:if|while)\s*\(\s*!?\s*[\w.]+\.to\(", src), "search.html branches on the result of a step: ask can() or is() first"
    assert not re.search(r"\b(?:&&|\|\|)\s*[\w.]+\.to\(", src)
    for gone in (r"\blet [^;\n]*\b(?:vCur|vPick|vStack|VL|sheetUp|shareFile|settle|vNote)\b", r"\bnavWaiting\b", r"\bL\.(?:asked|loading|retry)\b",
                 r"\bM\.m\b", r"\bAL\.", r"\.ends\b", r"\.gen\b"):
        assert not re.search(gone, src), f"search.html keeps state beside its machines again: {gone}"
    for module in ("viewer-flow.js", "grid-flow.js", "library-flow.js", "albums-flow.js", "favourite.js"):
        assert f"/app/{module}" in src, f"search.html does not use {module}"


def test_esc_escapes_both_quotes():
    src = (ROOT / "app" / "dom.js").read_text()
    assert """replace(/[&<>"']/g""" in src and '"\'": "&#39;"' in src


@pytest.fixture(scope="module")
def app():
    return PhotoApp(embed.parser().parse_args(["serve", "--demo", "--no-pregenerate"]))


def test_photo_app_serves_modules_as_javascript(app):
    for addr in pages.modules():
        r = app.request("GET", addr)
        assert r.status == 200 and r.body == (ROOT / addr.lstrip("/")).read_bytes(), addr
        assert r.content_type == "text/javascript; charset=utf-8", addr
        assert r.cache == "no-cache" and r.etag, f"{addr}: revalidated on each load, like the page that imports it"
    for viewer in ({}, {"Tailscale-User-Login": "anyone@example.com"}):      # reading is public, as for a page
        assert app.request("GET", "/app/dom.js", viewer).status == 200
    assert app.request("POST", "/app/dom.js", local=True).status == 404
    for path in ("/app/sw.js", "/dom.js", "/api.js", "/app/tests/dom.test.js", "/app/package.json", "/app/../pages.py"):
        assert app.request("GET", path).status == 404, path
    sw = app.request("GET", "/sw.js")              # the service worker stays at the site root
    assert sw.status == 200 and sw.content_type.startswith("text/javascript")


def test_photo_app_pages_name_modules_it_serves(app):
    for path, page in (("/", "search.html"), ("/changes", "changes.html"), ("/stats", "stats.html"), ("/members", "members.html"),
                       ("/inbox", "inbox.html")):
        r = app.request("GET", path)
        assert r.status == 200 and b'<script type="module">' in r.body, path
        for addr, _ in imports(script(page)):
            assert app.request("GET", addr).status == 200, f"{path} imports {addr}"


def test_setup_server_serves_modules():
    class Api:
        def handle(self, method, path, qs, body, local):
            return None

    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Api()))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        for addr, _ in imports(script("setup.html")):
            with urllib.request.urlopen(base + addr, timeout=10) as r:
                assert r.status == 200 and r.headers["Content-Type"] == "text/javascript; charset=utf-8", addr
                assert r.read() == (ROOT / addr.lstrip("/")).read_bytes()
        for path in ("/app/sw.js", "/app/package.json", "/app/tests/api.test.js"):
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + path, timeout=10)
            assert e.value.code == 404, path
    finally:
        srv.shutdown()
        srv.server_close()


def test_every_server_that_sends_a_page_sends_its_modules():
    """faces.py and estate.py serve their page from handlers made inside a command, so this reads them."""
    for server, page in (("faces.py", "people.html"), ("estate.py", "review.html"), ("intake/server.py", "setup.html"),
                         ("photo_app.py", "search.html")):
        assert "pages.module(" in (ROOT / server).read_text(), f"{server} sends {page} but not the modules it imports"
