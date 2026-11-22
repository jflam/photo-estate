"""core/ (issue #85): one list of media types, one place for the SQLite tables, one set of ports, and the
screenshot rules as a fixture both apps can test against. Each test here fails without the change it names.

  uv run pytest tests/test_core.py
"""
import ast
import importlib.util
import json
import re
import shutil
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent          # photo-estate/
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT))
from core import config, media, schema  # noqa: E402

SHA = "ab" * 32


# -- media types ------------------------------------------------------------------------------------

# What each module accepted before #85, copied from the code it replaced. Only /original changes.
OLD_INGEST_IMAGES = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".webp", ".bmp", ".tif", ".tiff",
                     ".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2", ".raf", ".avif"}
OLD_INGEST_VIDEOS = {".mp4", ".mov", ".m4v", ".3gp", ".avi", ".mkv", ".mts", ".m2ts", ".webm", ".mpg", ".wmv"}
OLD_UPLOAD = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".webp", ".tif", ".tiff", ".dng", ".avif",
              ".mp4", ".mov", ".m4v", ".3gp"}
OLD_ADMIN = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".webp", ".tif", ".tiff", ".avif"}
OLD_RAW = {".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2", ".raf"}
OLD_ORIGINAL = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".heic": "image/heic", ".png": "image/png",
                ".gif": "image/gif", ".webp": "image/webp"}


def test_media_sets_keep_what_each_module_accepted():
    assert media.IMAGE_EXT == OLD_INGEST_IMAGES and media.VIDEO_EXT == OLD_INGEST_VIDEOS
    assert media.MEDIA_EXT == OLD_INGEST_IMAGES | OLD_INGEST_VIDEOS
    assert media.RAW_EXT == OLD_RAW and media.EMBED_SKIP_EXT == OLD_RAW
    assert media.FACES_SKIP_EXT == OLD_RAW | {".gif"}
    assert media.UPLOAD_EXT == OLD_UPLOAD
    # admin.py prepares uploaded stills it can decode: what it had, as uploads reach it (no .bmp is ever uploaded)
    assert media.UPLOAD_EXT & media.DECODABLE_EXT == OLD_ADMIN
    assert set(media.MIME) == media.MEDIA_EXT
    assert all(media.MIME[e].startswith("image/") for e in media.IMAGE_EXT)
    assert all(media.MIME[e].startswith("video/") for e in media.VIDEO_EXT)
    assert media.kind_of("a/B.HEIC") == "image" and media.kind_of("x.MTS") == "video" and media.kind_of("x.txt") is None


def test_original_types_fix_only_adds_the_missing_stills():
    assert {e: t for e, t in media.ORIGINAL_TYPES.items() if e in OLD_ORIGINAL} == OLD_ORIGINAL
    assert set(media.ORIGINAL_TYPES) - set(OLD_ORIGINAL) == {".heif", ".tif", ".tiff", ".avif"}


def test_modules_use_the_one_list():
    import admin
    import embed
    import estate
    import faces
    import images
    import upload
    from intake import media as intake_media
    assert estate.IMAGE_EXT is media.IMAGE_EXT and estate.VIDEO_EXT is media.VIDEO_EXT and estate.RAW_EXT is media.RAW_EXT
    assert intake_media.MEDIA_EXT is media.MEDIA_EXT and intake_media.IMAGE_EXT is media.IMAGE_EXT
    assert upload.MEDIA_EXT is media.UPLOAD_EXT
    assert admin.DECODABLE_EXT is media.DECODABLE_EXT
    assert images.original_types is media.ORIGINAL_TYPES
    assert embed.SKIP_EXT is media.EMBED_SKIP_EXT and faces.SKIP_EXT is media.FACES_SKIP_EXT
    spec = importlib.util.spec_from_file_location("sample_resize", ROOT / "tools" / "aesthetics" / "sample_resize.py")
    sample_resize = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sample_resize)
    assert sample_resize.SKIP_EXT is media.EMBED_SKIP_EXT
    for path in [*ROOT.glob("*.py"), *ROOT.glob("intake/**/*.py"), *ROOT.glob("tools/**/*.py")]:
        tree = ast.parse(path.read_text())
        literals = [t.id for n in ast.walk(tree) if isinstance(n, ast.Assign) and isinstance(n.value, (ast.Set, ast.Dict))
                    for t in n.targets if isinstance(t, ast.Name) and t.id.endswith("_EXT")]
        assert not literals, f"{path.name} defines its own {literals}: use core/media.py"


@pytest.mark.parametrize("ext", [".heif", ".tif", ".tiff", ".avif", ".jpg", ".heic"])
def test_original_is_served_for_every_shareable_still(tmp_path, ext):
    """GET /original/<sha> answered 404 for a catalogued .heif, .tif, .tiff or .avif before #85."""
    from images import Images
    store = tmp_path / "store"
    blob = store / SHA[:2] / SHA[2:4] / f"{SHA}{ext}"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"the original bytes")
    live = SimpleNamespace(cat=SimpleNamespace(meta={SHA: {"ext": ext, "path": f"2024/05/IMG_0001{ext.upper()}"}}))
    status, body, ctype, cache, headers = Images(live, store, tmp_path / "cache", tmp_path / "cache").handle(
        "GET", f"/original/{SHA}", {}, None)
    assert (status, body, ctype) == (200, b"the original bytes", media.MIME[ext])
    assert ctype == {".heif": "image/heif", ".tif": "image/tiff", ".tiff": "image/tiff", ".avif": "image/avif",
                     ".jpg": "image/jpeg", ".heic": "image/heic"}[ext]
    assert headers["Content-Disposition"].endswith(f"IMG_0001{ext.upper()}") and "immutable" in cache


def test_original_still_refuses_raw_and_video(tmp_path):
    from images import Images
    for ext in (".dng", ".mov"):
        live = SimpleNamespace(cat=SimpleNamespace(meta={SHA: {"ext": ext, "path": "x"}}))
        blob = tmp_path / SHA[:2] / SHA[2:4] / f"{SHA}{ext}"
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(b"x")
        assert Images(live, tmp_path, tmp_path, tmp_path).handle("GET", f"/original/{SHA}", {}, None)[0] == 404


# -- schema -----------------------------------------------------------------------------------------

def master(path) -> list[list]:
    con = sqlite3.connect(path)
    try:
        return sorted(list(r) for r in con.execute("SELECT type, name, tbl_name, sql FROM sqlite_master"))
    finally:
        con.close()


def every_open_path(td: Path) -> dict:
    """Fresh databases, made the way each owner opens them."""
    import changes
    import embed
    import estate
    import faces
    import scores
    import sync
    import tasks
    from intake.engine import Store
    db = str(td / "estate.db")
    estate.connect(db).close()
    embed.connect(db).close()
    faces.connect(db).close()
    changes.Changes(db, facts=lambda s: None, shown=lambda s: {})
    tasks.Tasks(db, config.mcp_url(1), command="/nonexistent")
    con = sqlite3.connect(db)
    scores.save(con, "quality", "m", [1.0])          # scorers are made when a scorer is installed
    con.execute("DELETE FROM scorers")
    con.commit()
    schema.ensure(con, "favorites")                   # what PhotoApp runs when it opens the catalog
    con.close()
    sync.Feed(td / "sync-feed.db")
    Store(td / "intake.db")
    return {"catalog": master(db), "sync": master(td / "sync-feed.db"), "intake": master(td / "intake.db")}


def test_fresh_databases_match_what_the_old_code_made(tmp_path):
    """schema_baseline.json is sqlite_master as the code before #85 left it, captured from that code's own
    open paths (estate/embed/faces.connect, Changes, Tasks, scores.SCHEMA, set_favorites' CREATE, Feed, Store)."""
    baseline = json.loads((FIXTURES / "schema_baseline.json").read_text())
    # Tables newer than the baseline: only ever added (the video area: live_pairs and video_frames). They
    # must be there, and everything else must be as the old code made it.
    since = {"live_pairs", "video_frames"}
    old_only = lambda rows: [r for r in rows if r[2] not in since]
    got = every_open_path(tmp_path)
    assert since <= {r[1] for r in got["catalog"] if r[0] == "table"}
    assert {**got, "catalog": old_only(got["catalog"])} == baseline
    one = tmp_path / "one.db"
    con = sqlite3.connect(one)
    schema.ensure(con, *schema.CATALOG_AREAS)
    con.close()
    assert old_only(master(one)) == baseline["catalog"]


def test_old_change_tables_are_migrated_with_their_data(tmp_path):
    """A catalog from before albums: change_sets without done_at/kind/query, change_items without
    section/position. Opening it adds the columns, with their defaults, and keeps every row."""
    import changes
    db = tmp_path / "estate.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE change_sets(id INTEGER PRIMARY KEY, title TEXT, prompt TEXT, op TEXT DEFAULT 'hide',
          status TEXT DEFAULT 'open', created_by TEXT, created_at TEXT, updated_at TEXT, applied_at TEXT, reverted_at TEXT);
        CREATE TABLE change_items(set_id INT, sha256 TEXT, decision TEXT DEFAULT 'pending', applied INT DEFAULT 0,
          score REAL, reason TEXT, decided_at TEXT, PRIMARY KEY(set_id, sha256));
        INSERT INTO change_sets(id, title, status) VALUES (7, 'old set', 'done');
        INSERT INTO change_items(set_id, sha256, decision, applied, reason) VALUES (7, 'aa', 'accept', 1, 'why');
    """)
    con.close()
    changes.Changes(str(db), facts=lambda s: None, shown=lambda s: {})
    changes.Changes(str(db), facts=lambda s: None, shown=lambda s: {})     # twice: nothing more to do
    con = sqlite3.connect(db)
    cols = lambda t: [r[1] for r in con.execute(f"PRAGMA table_info({t})")]
    assert cols("change_sets")[-4:] == ["reverted_at", "done_at", "kind", "query"]
    assert cols("change_items")[-2:] == ["section", "position"]
    assert con.execute("SELECT id, title, status, done_at, kind, query FROM change_sets").fetchall() == \
        [(7, "old set", "done", None, "list", None)]
    assert con.execute("SELECT set_id, sha256, decision, applied, reason, section, position FROM change_items").fetchall() == \
        [(7, "aa", "accept", 1, "why", None, None)]
    assert con.execute("SELECT count(*) FROM sqlite_master WHERE name='hidden'").fetchone()[0] == 1
    con.close()


def test_ensure_on_an_existing_catalog_only_adds(tmp_path, demo_dir):
    """The demo catalog (a copy) stands in for one made by older code: every table it has keeps its
    definition and its rows; the areas it lacked are added."""
    db = tmp_path / "estate.db"
    shutil.copyfile(demo_dir / "estate.db", db)
    before = {r[1]: r for r in master(db)}
    con = sqlite3.connect(db)
    counts = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for (kind, t, *_) in before.values() if kind == "table"}
    schema.ensure(con, *schema.CATALOG_AREAS)
    schema.ensure(con, *schema.CATALOG_AREAS)
    after = {r[1]: r for r in master(db)}
    assert all(after[name] == row for name, row in before.items())
    assert {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in counts} == counts
    assert {"favorites", "change_sets", "agent_tasks", "faces", "scorers"} <= after.keys()
    con.close()


def test_no_ddl_outside_core():
    """Every CREATE TABLE/INDEX and ADD COLUMN lives in core/schema.py. Not TEMP tables (a connection's
    scratch), and places.py's rebuild of the tables it derives runs estate.SCHEMA's own statements."""
    for path in [*ROOT.glob("*.py"), *ROOT.glob("intake/**/*.py"), *ROOT.glob("tools/**/*.py")]:
        text = path.read_text()
        assert not re.search(r"CREATE (VIRTUAL )?(TABLE|INDEX)|ADD COLUMN", text), f"{path.relative_to(ROOT)} has its own DDL"


@pytest.fixture(scope="module")
def demo_app():
    import embed
    from photo_app import PhotoApp
    return PhotoApp(embed.parser().parse_args(["serve", "--demo", "--no-pregenerate"]))


def test_favorites_table_is_made_when_the_app_opens(demo_app):
    """Before #85 it was made inside set_favorites, on the first favourite (the demo catalog has none)."""
    con = sqlite3.connect(f"file:{demo_app.a.db}?mode=ro", uri=True)
    try:
        assert con.execute("SELECT count(*) FROM favorites").fetchone() == (0,)
    finally:
        con.close()


# -- ports ------------------------------------------------------------------------------------------

def test_ports_are_distinct_and_as_documented():
    assert len(set(config.PORTS.values())) == len(config.PORTS)
    assert (config.REAL, config.DEMO, config.PREVIEW, config.SANDBOX, config.REVIEW, config.FACES) == \
        (8766, 8767, 8768, 8769, 8765, 8764)
    assert config.app_port() == 8766 and config.app_port(demo=True) == 8767 and config.app_port(sandbox=True) == 8769
    assert config.app_port(9000, demo=True) == 9000


def test_app_port_and_task_mcp_url(demo_app):
    assert demo_app.port == config.DEMO
    assert demo_app.tasks.mcp_url == config.mcp_url(config.DEMO) == "http://127.0.0.1:8767/mcp"


def test_faces_serve_and_scripts_agree():
    assert re.search(r'add_argument\("--port", type=int, default=config\.FACES\)', (ROOT / "faces.py").read_text())
    deploy, tools = ROOT / "deploy", ROOT / "tools"
    assert 'PORT="${PREVIEW_PORT:-8768}"' in (deploy / "preview-branch.sh").read_text()
    assert "PORT=8768" in (deploy / "preview-user-setup.sh").read_text()
    assert "PORT=8768" in (deploy / "preview-firewall-setup.sh").read_text()
    assert "127.0.0.1:8768" in (deploy / "com.jflam.photo-preview.plist").read_text()
    assert "127.0.0.1:8767" in (deploy / "com.jflam.photo-search-demo.plist").read_text()
    assert ":8768" in (tools / "cloud-preview.sh").read_text()
    estate_src = (ROOT / "estate.py").read_text()
    assert re.search(r'"--port", type=int, default=config\.REVIEW\)', estate_src)


# -- screenshots ------------------------------------------------------------------------------------

RULES = json.loads((FIXTURES / "screenshot_rules.json").read_text())


@pytest.mark.parametrize("case", RULES["cases"], ids=[c["name"] for c in RULES["cases"]])
def test_screenshot_rules(case):
    from catalog import is_screenshot
    m = {k: case.get(k) for k in ("w", "h", "ext", "path", "camera", "lens")}
    assert is_screenshot(m) is case["screenshot"], case


def test_screen_sizes_match_the_fixture_and_the_iphone_app():
    from catalog import SCREEN_SIZES
    assert {tuple(s) for s in RULES["screen_sizes"]} == SCREEN_SIZES
    swift = ROOT.parent / "ios" / "PhotoEstate" / "Catalog" / "Screenshots.swift"
    if not swift.exists():
        pytest.skip("no ios/ checkout")
    body = swift.read_text().split("static let sizes", 1)[1].split("= [", 1)[1].split("]", 1)[0]
    assert {(int(a), int(b)) for a, b in re.findall(r"\((\d+),\s*(\d+)\)", body)} == SCREEN_SIZES


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


# -- videos ------------------------------------------------------------------------------------------

def test_video_types_cover_every_video_and_send_quicktime_as_mp4():
    """GET /video/<sha> sends a phone's .mov (and .m4v) as video/mp4, which Chrome plays; the rest as they are."""
    assert set(media.VIDEO_TYPES) == media.VIDEO_EXT
    assert media.VIDEO_TYPES[".mov"] == media.VIDEO_TYPES[".m4v"] == media.VIDEO_TYPES[".mp4"] == "video/mp4"
    assert {e: t for e, t in media.VIDEO_TYPES.items() if e not in (".mov", ".m4v")} == \
        {e: media.MIME[e] for e in media.VIDEO_EXT - {".mov", ".m4v"}}
    assert not set(media.VIDEO_TYPES) & set(media.ORIGINAL_TYPES), "a video has no /original"


def test_the_video_area(tmp_path):
    """live_pairs and video_frames are made by estate.connect and embed.connect, and are in CATALOG_AREAS."""
    import embed
    import estate
    assert "video" in schema.CATALOG_AREAS
    for opener, name in ((estate.connect, "a.db"), (embed.connect, "b.db")):
        opener(str(tmp_path / name)).close()
        tables = {r[1] for r in master(tmp_path / name) if r[0] == "table"}
        assert {"live_pairs", "video_frames"} <= tables, (opener, tables)
    con = sqlite3.connect(tmp_path / "c.db")
    schema.ensure(con, "video")
    cols = {t: [r[1] for r in con.execute(f"PRAGMA table_info({t})")] for t in ("live_pairs", "video_frames")}
    assert cols == {"live_pairs": ["clip", "still", "how", "paired_at"], "video_frames": ["sha256", "model", "t", "vec"]}
    con.execute("INSERT INTO live_pairs VALUES ('c', 's', 'id', 'now')")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO live_pairs VALUES ('c', 's2', 'id', 'now')")     # a clip has one still
    con.close()
