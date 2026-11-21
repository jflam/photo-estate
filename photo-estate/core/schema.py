"""Every SQLite table Photo Estate makes, in one place, and the additive migrations (standard library only).

  ensure(con, *areas)   create what is missing for these areas, then add any column an older database lacks
  AREAS                 area -> its DDL, as each owner has always created it (CREATE ... IF NOT EXISTS)
  ADDED                 area -> {table: ((column, type), ...)}: columns newer than some existing databases
  CATALOG_AREAS         the areas that live in the catalog (estate.db), for tests and tools
  EXIF_APP_INDEX        the index catalog.load adds to exif for the app's startup read

Areas, and who opens them (each owner calls ensure when it opens the database, never per request):

  estate.db, the catalog
    catalog      estate.connect: sources, assets, occurrences, sidecars, albums, xmp, flickr, exif,
                 places, events, trips, tags, and the full-text `search` table
    embeddings   embed.connect: one SigLIP 2 vector per (photo, model)
    video        estate.connect and embed.connect: live_pairs (a Live Photo's clip and its still) and
                 video_frames (the frames a video is searched by)
    faces        faces.connect: faces, face_scans, persons, face_not, face_later
    changes      changes.Changes (and tasks.Tasks, which reads change_sets): change_sets, change_items, hidden
    tasks        tasks.Tasks: agent_tasks, agent_events
    scores       scores.save: scorers ("Best first")
    favorites    photo_app.PhotoApp, when it opens the catalog: favourites set in the app
  sync-feed.db   sync         sync.Feed: kv, rows (the phones' change feed, beside the catalog)
  intake.db      intake       intake.engine.Store: jobs, events (the setup page's jobs)

Rules for a database that already exists (the Mac mini's catalog was made by older code):
  - additive only: CREATE TABLE/INDEX IF NOT EXISTS, and ADD COLUMN only when PRAGMA table_info lacks it;
    nothing is dropped, renamed or rewritten (places.py alone rebuilds places/events/trips, which it derives);
  - idempotent and cheap: no table scans, so opening a 100k-photo catalog costs what it did;
  - the DDL text is exactly what the owners ran before, so a fresh database is identical to one made by
    older code (tests/test_core.py compares sqlite_master with tests/fixtures/schema_baseline.json).
A reader that opens a database it did not create (read-only, or the demo, or one from before a table
existed) still checks for the table itself.
"""
from __future__ import annotations

import sqlite3

CATALOG = """
CREATE TABLE IF NOT EXISTS sources(
  name TEXT PRIMARY KEY, entries INT, media INT, new_assets INT, bytes_seen INT, bytes_new INT,
  finished_at TEXT);
CREATE TABLE IF NOT EXISTS assets(
  sha256 TEXT PRIMARY KEY, ext TEXT, kind TEXT, size INT, width INT, height INT,
  dhash INT,                 -- 64-bit perceptual hash stored signed
  exif_taken_at TEXT,        -- camera local time, no tz
  taken_at TEXT,             -- merged: sidecar UTC, else EXIF
  lat REAL, lon REAL, description TEXT, people TEXT, favorited INT DEFAULT 0, first_seen TEXT);
CREATE TABLE IF NOT EXISTS occurrences(
  id INTEGER PRIMARY KEY, sha256 TEXT NOT NULL, source TEXT, path TEXT, dir TEXT, filename TEXT,
  album TEXT, sidecar_id INT, UNIQUE(source, path));
CREATE INDEX IF NOT EXISTS occ_sha ON occurrences(sha256);
CREATE TABLE IF NOT EXISTS sidecars(
  id INTEGER PRIMARY KEY, dir TEXT, filename TEXT, title TEXT, data TEXT, UNIQUE(dir, filename));
CREATE TABLE IF NOT EXISTS albums(dir TEXT PRIMARY KEY, title TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS xmp(       -- icloudpd --xmp-sidecar files: X.HEIC.xmp next to X.HEIC
  dir TEXT, filename TEXT, data TEXT, PRIMARY KEY(dir, filename));
CREATE TABLE IF NOT EXISTS flickr(    -- Flickr account-data photo_<id>.json, keyed by photo id
  id TEXT PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS exif(      -- exiftool -n -G1 dump (MakerNotes and binary blobs left out)
  sha256 TEXT PRIMARY KEY, camera TEXT, make TEXT, model TEXT, lens TEXT,
  focal REAL, focal35 REAL, fnumber REAL, exposure REAL, iso INT, software TEXT,
  duration REAL, fps REAL, data TEXT, error TEXT);
CREATE INDEX IF NOT EXISTS exif_camera ON exif(camera);
CREATE TABLE IF NOT EXISTS places(    -- places.py: where and when (rebuilt on every run)
  sha256 TEXT PRIMARY KEY, city TEXT,
  locality TEXT,             -- the nearer suburb/neighbourhood when a big city was chosen as `city`
  admin2 TEXT, admin1 TEXT, country TEXT, cc TEXT,
  place_source TEXT,         -- gps | event (inferred from the photo's event)
  tz TEXT, tz_source TEXT,   -- IANA name or fixed offset, from gps | exif | event | home
  local_time TEXT,           -- wall-clock time where the photo was taken, no offset
  utc_time TEXT, event_id INT);
CREATE INDEX IF NOT EXISTS places_event ON places(event_id);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY,    -- start time as UTC epoch seconds: stable across re-runs
  start_local TEXT, end_local TEXT, n INT, title TEXT, place TEXT, lat REAL, lon REAL,
  km_from_home REAL, trip_id INT);
CREATE TABLE IF NOT EXISTS trips(
  id INTEGER PRIMARY KEY, start_local TEXT, end_local TEXT, n INT, n_events INT, title TEXT, place TEXT);
CREATE TABLE IF NOT EXISTS tags(
  sha256 TEXT, model TEXT, caption TEXT, tags TEXT, data TEXT, seconds REAL, created_at TEXT,
  PRIMARY KEY(sha256, model));
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(sha256 UNINDEXED, text);
"""
EMBEDDINGS = """
CREATE TABLE IF NOT EXISTS embeddings(
  sha256 TEXT, model TEXT, dim INT, vec BLOB,   -- float16, L2-normalised; NULL when undecodable
  error TEXT, created_at TEXT, PRIMARY KEY(sha256, model));
"""
VIDEO = """
CREATE TABLE IF NOT EXISTS live_pairs(   -- a Live Photo: the short clip taken with a still (estate.py pair_live)
  clip TEXT PRIMARY KEY,                 -- the clip's sha256: a paired clip is never shown on its own
  still TEXT NOT NULL,                   -- the still's sha256
  how TEXT,                              -- 'id' (Apple's content identifier) | 'mvimg' (a Google motion photo)
  paired_at TEXT);
CREATE INDEX IF NOT EXISTS live_pairs_still ON live_pairs(still);
CREATE TABLE IF NOT EXISTS video_frames(  -- embed.py: the frames a video is searched by (its mean is in embeddings)
  sha256 TEXT, model TEXT, t REAL,        -- t: seconds into the video
  vec BLOB,                               -- float16, L2-normalised, as embeddings.vec
  PRIMARY KEY(sha256, model, t));
"""
FACES = """
CREATE TABLE IF NOT EXISTS faces(
  id INTEGER PRIMARY KEY, sha256 TEXT, idx INT, model TEXT,
  x REAL, y REAL, w REAL, h REAL,   -- the box, as fractions of the upright image
  score REAL,                       -- detector confidence, 0-1
  px INT,                           -- the box's longer side in pixels, at the size it was read
  vec BLOB,                         -- float16 x 512, L2-normalised
  person_id INT,                    -- persons.id; 0 = a face you chose not to name; NULL = unknown
  source TEXT,                      -- who says so: 'you', 'google' or 'auto'
  labeled_at TEXT);
CREATE INDEX IF NOT EXISTS faces_sha ON faces(sha256);
CREATE TABLE IF NOT EXISTS face_scans(   -- every image looked at, including those with no faces
  sha256 TEXT, model TEXT, n INT, error TEXT, created_at TEXT, PRIMARY KEY(sha256, model));
CREATE TABLE IF NOT EXISTS persons(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE, born TEXT, created_at TEXT);   -- born: 'YYYY' or 'YYYY-MM'
CREATE TABLE IF NOT EXISTS face_not(     -- "this face is not that person", so it's never suggested again
  face_id INT, person_id INT, PRIMARY KEY(face_id, person_id));
CREATE TABLE IF NOT EXISTS face_later(   -- "is this ...?" skipped for now: still unnamed, asked again later
  face_id INT, person_id INT, created_at TEXT, PRIMARY KEY(face_id, person_id));
"""
CHANGES = """
CREATE TABLE IF NOT EXISTS change_sets(
  id INTEGER PRIMARY KEY, title TEXT, prompt TEXT, op TEXT DEFAULT 'hide',
  status TEXT DEFAULT 'open',        -- open | reverted | done
  created_by TEXT, created_at TEXT, updated_at TEXT, applied_at TEXT, reverted_at TEXT, done_at TEXT);
CREATE TABLE IF NOT EXISTS change_items(
  set_id INT, sha256 TEXT,
  decision TEXT DEFAULT 'pending',   -- pending (nobody has said: as proposed) | accept | reject (keep it)
  applied INT DEFAULT 0,             -- 1 while this set is hiding the photo
  score REAL, reason TEXT, decided_at TEXT, PRIMARY KEY(set_id, sha256));
CREATE TABLE IF NOT EXISTS hidden(   -- a photo is hidden while any row names it
  sha256 TEXT, set_id INT, hidden_at TEXT, PRIMARY KEY(sha256, set_id));
"""
TASKS = """
CREATE TABLE IF NOT EXISTS agent_tasks(
  id INTEGER PRIMARY KEY, prompt TEXT,
  status TEXT DEFAULT 'queued',      -- queued | running | done | failed | cancelled
  created_by TEXT, created_at TEXT, started_at TEXT, finished_at TEXT,
  result TEXT, error TEXT, turns INT, cost REAL);
CREATE TABLE IF NOT EXISTS agent_events(
  id INTEGER PRIMARY KEY, task_id INT, at TEXT,
  kind TEXT,                         -- say (the agent's words) | tool (a call it made) | error | note
  text TEXT);
CREATE INDEX IF NOT EXISTS agent_events_task ON agent_events(task_id, id);
"""
SCORES = """
CREATE TABLE IF NOT EXISTS scorers(
  name TEXT, model TEXT,            -- quality | taste; the embedding model the weights are for
  w BLOB, b REAL,                   -- float32 weights, one per embedding dimension; score = w . vec + b
  info TEXT, updated_at TEXT, PRIMARY KEY(name, model));
"""
FAVORITES = """
CREATE TABLE IF NOT EXISTS favorites(sha256 TEXT PRIMARY KEY, fav INT NOT NULL, set_at TEXT, set_by TEXT);
"""
SYNC = """
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS rows (sha TEXT PRIMARY KEY, digest BLOB, ver INTEGER, gone INTEGER DEFAULT 0);
"""
INTAKE = """
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY, source TEXT NOT NULL, config TEXT NOT NULL,
  state TEXT NOT NULL,          -- queued | running | waiting | done | failed | cancelled
  stage TEXT NOT NULL,          -- the stage it is in, or resumes from
  done INT, total INT, detail TEXT, error TEXT, warnings TEXT DEFAULT '[]',
  paths TEXT,                   -- what acquire handed to ingest (JSON list)
  prompt TEXT,                  -- what it is waiting for (JSON, never a secret)
  media_before INT, media_after INT,
  created_at REAL, updated_at REAL, ready_at REAL, finished_at REAL);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, job_id INT, at REAL, text TEXT);
CREATE INDEX IF NOT EXISTS events_job ON events(job_id, id);
"""

# catalog.load makes this index the first time the app reads a catalog (not estate.connect: on a big catalog it
# takes a while to build, and only the app reads by it). exif is mostly exiftool's JSON dump; this holds just
# the columns the app reads, so startup scans ~15 MB instead of ~700 MB.
EXIF_APP_INDEX = "CREATE INDEX IF NOT EXISTS exif_app ON exif(sha256, camera, lens, focal, focal35, fnumber, exposure, iso)"

AREAS = {"catalog": CATALOG, "embeddings": EMBEDDINGS, "video": VIDEO, "faces": FACES, "changes": CHANGES, "tasks": TASKS,
         "scores": SCORES, "favorites": FAVORITES, "sync": SYNC, "intake": INTAKE}
CATALOG_AREAS = ("catalog", "embeddings", "video", "faces", "changes", "tasks", "scores", "favorites")
ADDED = {
    "changes": {"change_sets": (("done_at", "TEXT"), ("kind", "TEXT DEFAULT 'list'"), ("query", "TEXT")),
                "change_items": (("section", "TEXT"), ("position", "INTEGER"))},
}


def ensure(con: sqlite3.Connection, *areas: str) -> None:
    """Create every table and index of these areas that is missing, and add the columns ADDED lists that a
    table made by older code lacks. Safe to call on every open; commits."""
    for area in areas:
        con.executescript(AREAS[area])
        for table, cols in ADDED.get(area, {}).items():
            have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
            for name, kind in cols:
                if name not in have:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
    con.commit()
