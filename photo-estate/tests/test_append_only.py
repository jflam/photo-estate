"""The estate only grows (docs/notes/MULTIPLAYER-PLAN.md, Append-only): the protected tables refuse a DELETE
or a change of key and let every other column be corrected, the checked-in demo catalog works with the
triggers added, each kind of source that loses a file changes nothing in the catalog or the store, ingest
sets the immutable flag only in a store marked for it, `estate.py purge` removes one photo for good and no
source brings it back, and `estate.py migrate` gives every photo a sharer once. Scratch catalogs and
stores only; estate.py is run as the command it is.

  uv run pytest tests/test_append_only.py
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import estate  # noqa: E402
import journal  # noqa: E402
import members  # noqa: E402
from core import schema  # noqa: E402
from embed import DEFAULT_MODEL  # noqa: E402

FLAGS = hasattr(os, "chflags")


def run(*args, ok=True) -> subprocess.CompletedProcess:
    r = subprocess.run([sys.executable, str(ROOT / "estate.py"), *map(str, args)], cwd=ROOT, capture_output=True, text=True)
    assert (r.returncode == 0) == ok, r.stdout + r.stderr
    return r


def jpeg(seed: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), ((seed * 70) % 256, (seed * 30) % 256, (seed * 110) % 256)).save(buf, "JPEG", quality=90)
    return buf.getvalue()


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def state(db: Path, store: Path) -> dict:
    """What the estate holds: every row of the tables a source adds to, and every file of the store with its
    hash. (`sources` is left out: it is the record of each source's last run, rewritten by every run.)"""
    con = sqlite3.connect(db)
    try:
        rows = {t: sorted(con.execute(f"SELECT * FROM {t}").fetchall(), key=repr)
                for t in ("assets", "occurrences", "sidecars", "xmp", "flickr", "albums", "journal", "sharers", "tombstones")}
    finally:
        con.close()
    files = {str(p.relative_to(store)): sha(p.read_bytes()) for p in store.rglob("*") if p.is_file() and ".tmp" not in p.parts}
    return {"rows": rows, "files": files}


@pytest.fixture
def unlock(tmp_path):
    """Clear every immutable flag under tmp_path when the test ends, so pytest can delete its folders."""
    yield
    if FLAGS:
        for p in tmp_path.rglob("*"):
            if p.is_file() and p.lstat().st_flags & stat.UF_IMMUTABLE:
                estate.set_immutable(p, False)


# -- the protected tables -------------------------------------------------------------------------------

def scratch_catalog(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    schema.ensure(con, *schema.CATALOG_AREAS)
    return con


def test_protected_tables_refuse_delete_and_rekeying_and_allow_corrections(tmp_path):
    con = scratch_catalog(tmp_path / "estate.db")
    mid = members.add(con, "Ada", "ada@example.com", role="sysadmin", status="active").id
    with con:
        con.execute("INSERT INTO assets(sha256, ext, kind) VALUES ('a', '.jpg', 'image')")
        con.execute("INSERT INTO occurrences(sha256, source, path, dir, filename) VALUES ('a', 's', 'd/x.jpg', 'd', 'x.jpg')")
        con.execute("INSERT INTO sharers VALUES ('a', ?, 't')", (mid,))
        con.execute("INSERT INTO tombstones(sha256, member_id, why, at) VALUES ('b', ?, 'opted_out', 't')", (mid,))
    refused = {
        "journal": ["DELETE FROM journal", "UPDATE journal SET kind='x'"],
        "assets": ["DELETE FROM assets", "DELETE FROM assets WHERE sha256='a'", "UPDATE assets SET sha256='z'"],
        "occurrences": ["DELETE FROM occurrences", "UPDATE occurrences SET id=99", "UPDATE occurrences SET sha256='z'"],
        "sharers": ["DELETE FROM sharers", "UPDATE sharers SET member_id=99", "UPDATE sharers SET sha256='z'"],
        "tombstones": ["DELETE FROM tombstones", "UPDATE tombstones SET why='purged'", "UPDATE tombstones SET member_id=0",
                       "UPDATE tombstones SET sha256='z'", "UPDATE tombstones SET at='u'"],
        "members": ["DELETE FROM members", "UPDATE members SET id=99"],
        "member_logins": ["DELETE FROM member_logins", "UPDATE member_logins SET login='eve@example.com'",
                          "UPDATE member_logins SET member_id=99"],
    }
    for table, sqls in refused.items():
        for sql in sqls:
            with pytest.raises(sqlite3.IntegrityError):
                con.execute(sql)
            con.rollback()
    # what ingest, link, exif, embed and members.py correct today, and the one thing a tombstone may do
    with con:
        con.execute("UPDATE assets SET taken_at='2020-01-01', lat=1, lon=2, width=640, height=480, people='Ada', favorited=1,"
                    " description='d', kind='image', ext='.jpg', sha256='a' WHERE sha256='a'")      # the same key is no change
        con.execute("UPDATE occurrences SET sidecar_id=3, album='Trip' WHERE sha256='a'")
        con.execute("UPDATE sharers SET shared_at='u'")
        con.execute("UPDATE tombstones SET lifted_at='u'")
        con.execute("UPDATE member_logins SET added_at='u'")
    members.change(con, mid, None, name="Ada L.")
    assert con.execute("SELECT count(*) FROM assets").fetchone()[0] == 1
    assert con.execute("SELECT name FROM members").fetchone()[0] == "Ada L."
    con.close()


def test_an_insert_never_replaces_a_protected_row(tmp_path):
    """INSERT OR REPLACE deletes the row it collides with and fires no delete trigger. On a protected table it
    is refused (journal, members, member_logins) or skipped (the rest), and the row stays as it was."""
    con = scratch_catalog(tmp_path / "estate.db")
    mid = members.add(con, "Ada", "ada@example.com", role="sysadmin", status="active").id
    with con:
        journal.append(con, mid, "album.created", "album:1", {"title": "x"})
        con.execute("INSERT INTO assets(sha256, ext, taken_at) VALUES ('a', '.jpg', '2020')")
        con.execute("INSERT INTO occurrences(sha256, source, path) VALUES ('a', 's', 'p')")
        con.execute("INSERT INTO sharers VALUES ('a', ?, 't')", (mid,))
        con.execute("INSERT INTO tombstones(sha256, member_id, why, at) VALUES ('b', 0, 'purged', 't')")
    tables = ("journal", "members", "member_logins", "assets", "occurrences", "sharers", "tombstones")
    rows = lambda: {t: con.execute(f"SELECT rowid, * FROM {t} ORDER BY rowid").fetchall() for t in tables}
    before = rows()
    for verb in ("INSERT OR REPLACE", "REPLACE"):
        for sql, args in (("INTO journal(id, at, kind) VALUES (1, 't', 'rewritten')", ()),
                          ("INTO members(id, name) VALUES (?, 'Eve')", (mid,)),
                          ("INTO member_logins VALUES ('ada@example.com', 99, 't')", ())):
            with pytest.raises(sqlite3.IntegrityError, match="never replaced"):
                con.execute(f"{verb} {sql}", args)
            con.rollback()
        with con:                                                   # skipped: no error, and nothing changes
            con.execute(f"{verb} INTO assets(sha256, ext) VALUES ('a', '.png')")
            con.execute(f"{verb} INTO occurrences(sha256, source, path) VALUES ('z', 's', 'p')")      # same source, path
            con.execute(f"{verb} INTO occurrences(id, sha256, source, path) VALUES (1, 'z', 's', 'q')")  # same id
            con.execute(f"{verb} INTO sharers VALUES ('a', ?, 'later')", (mid,))
            con.execute(f"{verb} INTO tombstones VALUES ('b', 0, 'opted_out', 'u', 'lifted')")
        assert rows() == before, verb
    # INSERT OR IGNORE still adds what is new and skips what is there; plain inserts with new keys still work
    with con:
        assert con.execute("INSERT OR IGNORE INTO assets(sha256, ext) VALUES ('a', '.png')").rowcount == 0
        assert con.execute("INSERT OR IGNORE INTO assets(sha256, ext) VALUES ('c', '.png')").rowcount == 1
        con.execute("INSERT OR IGNORE INTO occurrences(sha256, source, path) VALUES ('c', 's', 'p')")
        con.execute("INSERT OR IGNORE INTO occurrences(sha256, source, path) VALUES ('c', 's', 'p2')")
        con.executemany("INSERT OR IGNORE INTO sharers VALUES (?, ?, 'n')", [("a", mid), ("c", mid)])
        con.executemany("INSERT OR IGNORE INTO tombstones(sha256, member_id, why, at) VALUES (?, 0, 'purged', 'n')",
                        [("b",), ("d",)])
        journal.append(con, mid, "album.items", "album:1")
    members.add(con, "Ben", "ben@example.com")
    assert con.execute("SELECT sha256, ext FROM assets ORDER BY 1").fetchall() == [("a", ".jpg"), ("c", ".png")]
    assert con.execute("SELECT id, sha256, path FROM occurrences ORDER BY id").fetchall() == [(1, "a", "p"), (2, "c", "p2")]
    assert con.execute("SELECT sha256, shared_at FROM sharers ORDER BY 1").fetchall() == [("a", "t"), ("c", "n")]
    assert con.execute("SELECT sha256, at FROM tombstones ORDER BY 1").fetchall() == [("b", "t"), ("d", "n")]
    assert [e["id"] for e in journal.since(con)][-1] == 4 and len(members.everyone(con)) == 2
    con.close()


def test_a_purges_tombstone_is_never_lifted_and_an_opt_out_is(tmp_path):
    con = scratch_catalog(tmp_path / "estate.db")
    with con:
        con.executemany("INSERT INTO tombstones(sha256, member_id, why, at) VALUES (?, ?, ?, 't')",
                        [("a", 0, "purged"), ("b", 3, "opted_out")])
    with pytest.raises(sqlite3.IntegrityError, match="a purge is never lifted"):
        con.execute("UPDATE tombstones SET lifted_at='now' WHERE member_id=0")
    con.rollback()
    with con:
        con.execute("UPDATE tombstones SET lifted_at='now' WHERE member_id=3")
    assert con.execute("SELECT sha256, lifted_at FROM tombstones ORDER BY 1").fetchall() == [("a", None), ("b", "now")]
    con.close()


def test_a_full_ingest_of_the_demo_photos_works_with_the_triggers(tmp_path, demo_dir):
    """The demo's photos as a folder source into a fresh catalog (every insert through the new triggers), linked,
    then again with --force: nothing more."""
    db, store = tmp_path / "estate.db", tmp_path / "store"
    n = sum(1 for p in (demo_dir / "store").rglob("*") if p.is_file() and p.suffix.lower() in estate.IMAGE_EXT | estate.VIDEO_EXT)
    r = run("--db", db, "ingest", "--store", store, demo_dir / "store")
    assert f"{n} new" in r.stdout, r.stdout
    run("--db", db, "link", "--source", "store")
    before = state(db, store)
    assert len(before["rows"]["assets"]) == len(before["rows"]["occurrences"]) == n
    assert "0 new" in run("--db", db, "ingest", "--store", store, "--force", demo_dir / "store").stdout
    assert state(db, store) == before


def test_the_demo_catalog_works_with_the_triggers_added_to_its_scratch_copy(tmp_path, demo_dir):
    """The checked-in demo is never written: a scratch copy gets the triggers, then link (which updates
    assets and occurrences) runs on it, and the app's catalog reads it as it reads the original."""
    import catalog
    before = sha((demo_dir / "estate.db").read_bytes())
    db = tmp_path / "estate.db"
    shutil.copyfile(demo_dir / "estate.db", db)
    con = scratch_catalog(db)
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert {"assets_no_delete", "occurrences_keep_key", "journal_no_update", "members_no_delete"} <= names
    n = con.execute("SELECT count(*) FROM assets").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("DELETE FROM assets")
    con.rollback()
    con.close()
    run("--db", db, "link")
    shutil.copyfile(demo_dir / "estate.db", tmp_path / "plain.db")          # (catalog.load adds an index: never the original)
    plain = catalog.load(sqlite3.connect(tmp_path / "plain.db"), DEFAULT_MODEL, tmp_path / "c1", tmp_path / "s1", demo=True)
    kept = catalog.load(sqlite3.connect(db), DEFAULT_MODEL, tmp_path / "c2", tmp_path / "s2", demo=True)
    assert kept.N == plain.N > 0 and sorted(kept.shas) == sorted(plain.shas)
    assert sqlite3.connect(db).execute("SELECT count(*) FROM assets").fetchone()[0] == n
    assert sha((demo_dir / "estate.db").read_bytes()) == before, "the demo catalog must not change"


# -- sources only add -----------------------------------------------------------------------------------

PHOTOS = {f"IMG_{i}.JPG": jpeg(i) for i in range(1, 4)}


def make_source(kind: str, root: Path, photos: dict) -> tuple[Path, list[str]]:
    """A source of each kind there is, holding `photos`. -> (its path, the ingest arguments that read it)."""
    if kind == "takeout zip":       # Google Takeout, or a Flickr download: zips, read in place
        src = root / "takeout-001.zip"
        with zipfile.ZipFile(src, "w") as z:
            for name, b in photos.items():
                z.writestr(f"Takeout/Google Photos/Photos from 2020/{name}", b)
                z.writestr(f"Takeout/Google Photos/Photos from 2020/{name}.supplemental-metadata.json",
                           json.dumps({"title": name, "photoTakenTime": {"timestamp": "1590000000"}}))
        return src, ["--force"]
    src = root / {"icloud folder": "icloud", "onedrive mirror": "onedrive", "phone inbox": "iphone"}[kind]
    for i, (name, b) in enumerate(photos.items()):
        d = src / ("2020/05/20" if kind != "onedrive mirror" else "Pictures/Camera Roll")
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(b)
        if kind in ("icloud folder", "phone inbox"):   # icloudpd and the phone both put an .xmp beside the photo
            (d / f"{name}.xmp").write_text(f"<photoshop:DateCreated>2020-05-20T10:0{i}:00-0700</photoshop:DateCreated>")
    if kind == "phone inbox":       # as ingest-iphone.sh reads it: every run, with --force and the upload facts
        return src, ["--force", "--facts", root / "facts"]
    return src, ["--force"]         # the setup page ingests a folder with --force too (intake/engine.py)


def lose(kind: str, src: Path, name: str) -> None:
    if kind == "takeout zip":       # a new download of the same zip, without that photo
        keep = {i.filename: zipfile.ZipFile(src).read(i) for i in zipfile.ZipFile(src).infolist() if name not in i.filename}
        with zipfile.ZipFile(src, "w") as z:
            for n, b in keep.items():
                z.writestr(n, b)
        return
    for f in src.rglob(f"{name}*"):
        f.unlink()


@pytest.mark.parametrize("kind", ["takeout zip", "icloud folder", "onedrive mirror", "phone inbox"])
def test_a_source_that_loses_a_file_changes_nothing(tmp_path, kind):
    db, store = tmp_path / "estate.db", tmp_path / "store"
    src, extra = make_source(kind, tmp_path, PHOTOS)
    run("--db", db, "ingest", "--store", store, *extra, src)
    run("--db", db, "link", "--source", src.name)
    before = state(db, store)
    assert len(before["rows"]["assets"]) == 3 and len(before["files"]) == 3
    lose(kind, src, "IMG_2.JPG")
    r = run("--db", db, "ingest", "--store", store, *extra, src)
    run("--db", db, "link", "--source", src.name)
    assert state(db, store) == before, r.stdout
    assert sum(e["kind"] == "ingest.finished" for e in journal.since(sqlite3.connect(db))) == 1, "the second run added nothing"


# -- the immutable flag ---------------------------------------------------------------------------------

@pytest.mark.skipif(not FLAGS, reason="no file flags (chflags) on this system")
def test_ingest_sets_the_immutable_flag_only_in_a_store_marked_for_it(tmp_path, unlock, monkeypatch):
    marked, plain = tmp_path / "marked", tmp_path / "plain"
    marked.mkdir()
    (marked / estate.IMMUTABLE).touch()
    src, _ = make_source("onedrive mirror", tmp_path, PHOTOS)
    run("--db", tmp_path / "a.db", "ingest", "--store", marked, src)
    run("--db", tmp_path / "b.db", "ingest", "--store", plain, src)
    flagged = lambda store: {p.name: bool(p.lstat().st_flags & stat.UF_IMMUTABLE)
                             for p in store.rglob("*.jpg") if ".tmp" not in p.parts}
    assert list(flagged(marked).values()) == [True] * 3 and list(flagged(plain).values()) == [False] * 3
    one = next(p for p in marked.rglob("*.jpg"))
    with pytest.raises(PermissionError):
        one.unlink()                                        # even rm -rf in the wrong folder fails
    with pytest.raises(PermissionError):
        shutil.rmtree(one.parent)
    # a catalog that has not recorded files already in the store (an ingest stopped before it committed)
    # keeps them: an immutable file is never replaced
    r = run("--db", tmp_path / "c.db", "ingest", "--store", marked, src)
    assert "3 new" in r.stdout and sqlite3.connect(tmp_path / "c.db").execute("SELECT count(*) FROM assets").fetchone()[0] == 3
    # never in the checked-in demo store, even if it were marked
    monkeypatch.setattr(estate, "HERE", tmp_path)
    (tmp_path / "demo" / "store").mkdir(parents=True)
    (tmp_path / "demo" / "store" / estate.IMMUTABLE).touch()
    assert estate.immutable(marked) and not estate.immutable(tmp_path / "demo" / "store") and not estate.immutable(plain)


def test_the_checked_in_demo_store_is_not_marked(demo_dir):
    assert not (demo_dir / "store" / estate.IMMUTABLE).exists() and not estate.immutable(demo_dir / "store")


# -- purge ----------------------------------------------------------------------------------------------

@pytest.fixture
def estate_with_three(tmp_path, unlock):
    """A scratch estate: three photos ingested from a folder into a store marked immutable, a sysadmin."""
    db, store = tmp_path / "estate.db", tmp_path / "store"
    store.mkdir()
    (store / estate.IMMUTABLE).touch()
    src, _ = make_source("onedrive mirror", tmp_path, PHOTOS)
    run("--db", db, "ingest", "--store", store, src)
    con = estate.connect(str(db))
    schema.ensure(con, *schema.CATALOG_AREAS)        # every table a real catalog has (vectors, faces ...)
    admin = members.first_start(con, ["owner@example.com"])[0]
    con.close()
    return db, store, src, admin


def test_purge_removes_a_photo_for_good_and_keeps_the_fact(tmp_path, estate_with_three):
    db, store, src, admin = estate_with_three
    gone = sha(PHOTOS["IMG_2.JPG"])
    cache = tmp_path / "cache"
    for f in (cache / "thumbs" / f"{gone}.webp", cache / "previews" / f"{gone}.jpg", cache / "faces" / gone[:2] / f"{gone}-0.jpg",
              cache / "thumbs" / f"{sha(PHOTOS['IMG_1.JPG'])}.webp"):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")
    con = sqlite3.connect(db)
    triggers = con.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger' ORDER BY name").fetchall()
    con.execute("INSERT INTO embeddings(sha256, model) VALUES (?, 'm')", (gone,))
    con.execute("INSERT INTO faces(id, sha256, idx) VALUES (5, ?, 0)", (gone,))
    con.execute("INSERT INTO face_not VALUES (5, 1)")
    con.execute("INSERT INTO live_pairs VALUES (?, ?, 'id', 't')", ("c" * 64, gone))
    con.execute("INSERT INTO sharers VALUES (?, ?, 't')", (gone, admin.id))
    con.commit()
    con.close()
    run("--db", db, "purge", gone, "--store", store, ok=False)                     # a reason is required
    r = run("--db", db, "purge", gone, "--reason", "  a photo of a receipt\n", "--store", store, "--cache", cache,
            "--backup-dir", tmp_path / "backups")
    assert "remains in the catalog backups" in r.stdout and "onedrive: Pictures/Camera Roll/IMG_2.JPG" in r.stdout

    con = sqlite3.connect(db)
    for t in ("assets", "occurrences", "sharers", "embeddings", "faces", "search"):
        assert con.execute(f"SELECT count(*) FROM {t} WHERE sha256=?", (gone,)).fetchone()[0] == 0, t
    assert con.execute("SELECT count(*) FROM face_not").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM live_pairs").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM assets").fetchone()[0] == 2
    assert con.execute("SELECT member_id, why, lifted_at FROM tombstones WHERE sha256=?", (gone,)).fetchall() == [(0, "purged", None)]
    entry = [e for e in journal.since(con) if e["kind"] == "photo.purged"]
    assert [(e["actor"], e["subject"], e["data"]["reason"], e["data"]["sources"]) for e in entry] == [
        (admin.id, gone, "a photo of a receipt", [["onedrive", "Pictures/Camera Roll/IMG_2.JPG"]])]
    assert con.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger' ORDER BY name").fetchall() == triggers
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("DELETE FROM assets")                                           # the protection is back
    con.close()
    assert not estate.blob_path(store, gone, ".jpg").exists()
    assert sorted(p.name for p in cache.rglob("*") if p.is_file()) == [f"{sha(PHOTOS['IMG_1.JPG'])}.webp"]
    backups = list((tmp_path / "backups").iterdir())
    assert len(backups) == 1 and backups[0].name.startswith(f"estate.db.before-purge-{gone[:12]}-")
    copy = sqlite3.connect(backups[0])
    assert copy.execute("SELECT count(*) FROM assets WHERE sha256=?", (gone,)).fetchone()[0] == 1   # the photo, before
    copy.close()

    # no source brings it back: the same folder again, the same bytes elsewhere, and a run after a restart
    before = state(db, store)
    again = tmp_path / "again"
    again.mkdir()
    (again / "copy-of-it.jpg").write_bytes(PHOTOS["IMG_2.JPG"])
    r = run("--db", db, "ingest", "--store", store, "--force", src, again)
    assert "1 refused (purged)" in r.stdout and state(db, store) == before
    assert "purged already" in run("--db", db, "purge", gone, "--reason", "again", "--store", store).stdout
    run("--db", db, "purge", "0" * 64, "--reason", "no such photo", "--store", store, ok=False)


# -- the migration --------------------------------------------------------------------------------------

def test_migrate_gives_every_photo_without_a_sharer_the_sysadmin_once(tmp_path):
    db = tmp_path / "estate.db"
    con = scratch_catalog(db)
    with con:
        con.executemany("INSERT INTO assets(sha256, ext, kind, first_seen) VALUES (?, '.jpg', 'image', ?)",
                        [("a", "2020-01-01T00:00:00+00:00"), ("b", None), ("c", "2021-01-01T00:00:00+00:00")])
    con.close()
    assert "no active sysadmin" in run("--db", db, "migrate", ok=False).stderr
    con = sqlite3.connect(db)
    owner, kid = members.first_start(con, ["owner@example.com", "kid@example.com"])
    with con:
        con.execute("INSERT INTO sharers VALUES ('c', ?, 'then')", (kid.id,))      # shared by someone already
    con.close()
    assert "2 photos given owner" in run("--db", db, "migrate").stdout
    assert "0 photos given" in run("--db", db, "migrate").stdout                  # safe to run twice
    con = sqlite3.connect(db)
    rows = con.execute("SELECT sha256, member_id, shared_at FROM sharers ORDER BY 1").fetchall()
    assert [(s, m) for s, m, _ in rows] == [("a", owner.id), ("b", owner.id), ("c", kid.id)]
    assert rows[0][2] == "2020-01-01T00:00:00+00:00" and rows[1][2]           # when it came in, or now
    assert [(e["actor"], e["data"]) for e in journal.since(con) if e["kind"] == "sharers.migrated"] == [
        (owner.id, {"member": owner.id, "photos": 2})]
    con.close()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
