"""Every SQLite table Photo Estate makes, in one place, and the additive migrations (standard library only).

  ensure(con, *areas)   create what is missing for these areas, then add any column an older database lacks
  AREAS                 area -> its DDL, as each owner has always created it (CREATE ... IF NOT EXISTS)
  ADDED                 area -> {table: ((column, type), ...)}: columns newer than some existing databases
  CATALOG_AREAS         the areas that live in the catalog (estate.db), for tests and tools
  APPEND_ONLY           sharers, tombstones and the protected-table triggers (part of the catalog area)
  PRIVATE               private: photos private to a sharer (part of the catalog area; private.py)
  EXIF_APP_INDEX        the index catalog.load adds to exif for the app's startup read

Areas, and who opens them (each owner calls ensure when it opens the database, never per request):

  estate.db, the catalog
    catalog      estate.connect: sources, assets, occurrences, sidecars, albums, xmp, flickr, exif,
                 places, events, trips, tags, and the full-text `search` table; APPEND_ONLY: sharers,
                 tombstones, and the triggers that protect assets, occurrences, sharers and tombstones; and
                 PRIVATE: `private`, the photos private to a sharer (ingest writes it for a phone upload)
    embeddings   embed.connect: one SigLIP 2 vector per (photo, model)
    video        estate.connect and embed.connect: live_pairs (a Live Photo's clip and its still) and
                 video_frames (the frames a video is searched by)
    faces        faces.connect: faces, face_scans, persons, face_not, face_later
    changes      changes.Changes (and tasks.Tasks, which reads change_sets): change_sets, change_items, hidden
    tasks        tasks.Tasks: agent_tasks, agent_events
    scores       scores.save: scorers ("Best first")
    favorites    photo_app.PhotoApp, when it opens the catalog: favourites set in the app
    members      members, member_logins and the journal (journal.py), with the triggers that keep them
                 append-only, and inbox_items (member_inbox.py: each member's inbox, which the code that makes
                 or decides an item writes in its own transaction): photo_app.PhotoApp when it opens the
                 catalog, and everything that appends to the journal (estate.connect, faces.connect,
                 changes.Changes, tasks.Tasks)
    lenses       lenses.py: lenses, subscriptions, shown ("show it anyway") and member_favorites (each member's
                 favourites): changes.Changes and photo_app.PhotoApp when they open the catalog, and
                 `estate.py migrate`
    stories      stories.py and listeners.py: questions, asked (a question dealt to a member), stories and
                 story_photos (append-only: only a story's transcript is written later, once), listener_cursors:
                 photo_app.PhotoApp, tasks.Tasks and transcribe.py when they open the catalog

Protected tables (the multiplayer plan's Append-only): a DELETE from journal, assets, occurrences, sharers,
tombstones, members or member_logins raises, as does any UPDATE of journal and an UPDATE that changes a key
(assets.sha256; occurrences.id, sha256; sharers.sha256, member_id; members.id; member_logins.login,
member_id; anything of tombstones but lifted_at, and lifted_at of a purge's, member 0). An INSERT whose
key is already there never replaces the row (an INSERT OR REPLACE would otherwise delete it without any
delete trigger firing): it raises for journal, members and member_logins, which the code only plain-INSERTs,
and is skipped (RAISE(IGNORE)) for assets, occurrences (the id, or the same source and path), sharers and
tombstones, so INSERT OR IGNORE works as before and a REPLACE changes nothing. The triggers do not stop
DROP TABLE or DROP TRIGGER: they are a guard against accidents, not against intent. The stories area's
stories and story_photos are protected the same way (no DELETE, no replacing INSERT, no UPDATE but a story's
transcript, written once): its STORIES DDL.
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
# Append-only (docs/notes/MULTIPLAYER-PLAN.md): who shared each photo, the photos refused for good, and the
# triggers that keep a photo and its history from being deleted or re-keyed. Other columns of assets and
# occurrences stay updatable (link, exif and embed correct dates, places, sizes and sidecar links). Only
# `estate.py purge` deletes from these tables: it drops the *_no_delete triggers inside its own transaction.
APPEND_ONLY = """
CREATE TABLE IF NOT EXISTS sharers(      -- the members who shared a photo (a copy two members sent has two)
  sha256 TEXT, member_id INT, shared_at TEXT, PRIMARY KEY(sha256, member_id));
CREATE TABLE IF NOT EXISTS tombstones(   -- a photo refused: opted out by one member, or purged (member 0: everyone)
  sha256 TEXT, member_id INT,
  why TEXT,                              -- opted_out | purged
  at TEXT,
  lifted_at TEXT,                        -- an opt-out taken back (a purge never is)
  PRIMARY KEY(sha256, member_id));
CREATE TRIGGER IF NOT EXISTS assets_no_delete BEFORE DELETE ON assets
  BEGIN SELECT RAISE(ABORT, 'assets are append-only: a photo is removed only by estate.py purge'); END;
CREATE TRIGGER IF NOT EXISTS assets_keep_key BEFORE UPDATE OF sha256 ON assets WHEN NEW.sha256 IS NOT OLD.sha256
  BEGIN SELECT RAISE(ABORT, 'assets: sha256 never changes'); END;
CREATE TRIGGER IF NOT EXISTS assets_no_replace BEFORE INSERT ON assets
  WHEN EXISTS (SELECT 1 FROM assets WHERE sha256 = NEW.sha256)
  BEGIN SELECT RAISE(IGNORE); END;
CREATE TRIGGER IF NOT EXISTS occurrences_no_delete BEFORE DELETE ON occurrences
  BEGIN SELECT RAISE(ABORT, 'occurrences are append-only: removed only by estate.py purge'); END;
CREATE TRIGGER IF NOT EXISTS occurrences_keep_key BEFORE UPDATE OF id, sha256 ON occurrences
  WHEN NEW.id IS NOT OLD.id OR NEW.sha256 IS NOT OLD.sha256
  BEGIN SELECT RAISE(ABORT, 'occurrences: id and sha256 never change'); END;
CREATE TRIGGER IF NOT EXISTS occurrences_no_replace BEFORE INSERT ON occurrences
  WHEN EXISTS (SELECT 1 FROM occurrences WHERE id = NEW.id OR (source = NEW.source AND path = NEW.path))
  BEGIN SELECT RAISE(IGNORE); END;
CREATE TRIGGER IF NOT EXISTS sharers_no_delete BEFORE DELETE ON sharers
  BEGIN SELECT RAISE(ABORT, 'sharers are append-only: removed only by estate.py purge'); END;
CREATE TRIGGER IF NOT EXISTS sharers_keep_key BEFORE UPDATE OF sha256, member_id ON sharers
  WHEN NEW.sha256 IS NOT OLD.sha256 OR NEW.member_id IS NOT OLD.member_id
  BEGIN SELECT RAISE(ABORT, 'sharers: sha256 and member_id never change'); END;
CREATE TRIGGER IF NOT EXISTS sharers_no_replace BEFORE INSERT ON sharers
  WHEN EXISTS (SELECT 1 FROM sharers WHERE sha256 = NEW.sha256 AND member_id = NEW.member_id)
  BEGIN SELECT RAISE(IGNORE); END;
CREATE TRIGGER IF NOT EXISTS tombstones_no_delete BEFORE DELETE ON tombstones
  BEGIN SELECT RAISE(ABORT, 'tombstones are never deleted: an opt-out is lifted (lifted_at)'); END;
CREATE TRIGGER IF NOT EXISTS tombstones_only_lift BEFORE UPDATE ON tombstones
  WHEN NEW.sha256 IS NOT OLD.sha256 OR NEW.member_id IS NOT OLD.member_id OR NEW.why IS NOT OLD.why OR NEW.at IS NOT OLD.at
    OR (OLD.member_id = 0 AND NEW.lifted_at IS NOT OLD.lifted_at)
  BEGIN SELECT RAISE(ABORT, 'tombstones: only lifted_at changes, and a purge is never lifted'); END;
CREATE TRIGGER IF NOT EXISTS tombstones_no_replace BEFORE INSERT ON tombstones
  WHEN EXISTS (SELECT 1 FROM tombstones WHERE sha256 = NEW.sha256 AND member_id = NEW.member_id)
  BEGIN SELECT RAISE(IGNORE); END;
"""
# Private photos (private.py; multiplayer phase 5): the current state, not protected (the journal is its history).
# A row stays when it is released (released_at), so a photo its sharer let go is never held again.
PRIVATE = """
CREATE TABLE IF NOT EXISTS private(      -- a photo private to one of its sharers (private.py says who may see it)
  sha256 TEXT, member_id INT,
  why TEXT,                              -- new (a phone upload not checked yet) | held | withdrawn
  since TEXT,
  until TEXT,                            -- a day (YYYY-MM-DD) it is released on by itself, or NULL
  released_at TEXT,                      -- NULL while it is private
  batch INT,                             -- held: the journal entry of the hold (its inbox item is held:<batch>)
  PRIMARY KEY(sha256, member_id));
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
LENSES = """
CREATE TABLE IF NOT EXISTS lenses(       -- a named view (lenses.py): a filter leaves photos out, a series is an album
  id INTEGER PRIMARY KEY,
  owner INT,                             -- members.id; NULL: the first sysadmin's
  title TEXT,
  kind TEXT DEFAULT 'filter',            -- filter | series
  set_id INT,                            -- a series: its album (change_sets.id); NULL for a filter, whose hiding sets
                                         --   name it (change_sets.lens_id)
  published INT DEFAULT 0,
  estate_default INT DEFAULT 0,          -- a published filter every member sees through unless they opt out
  proposed_at TEXT,                      -- its owner proposed it as an estate default: a sysadmin decides
  maintained_by INT,                     -- an agent member that keeps it up to date (multiplayer phase 7), or NULL
  created_at TEXT);
CREATE TABLE IF NOT EXISTS subscriptions(   -- a member and a lens of someone else's
  member_id INT, lens_id INT,
  mode TEXT,                             -- live | snapshot (copied into one of their own) | opted_out (an estate default)
  at TEXT, PRIMARY KEY(member_id, lens_id));
CREATE TABLE IF NOT EXISTS shown(        -- "show it anyway": a photo the member's filters hide, shown to them all the same
  sha256 TEXT, member_id INT, at TEXT, PRIMARY KEY(sha256, member_id));
CREATE TABLE IF NOT EXISTS member_favorites(   -- each member's own favourites (`favorites` is no longer written)
  sha256 TEXT, member_id INT, fav INT NOT NULL, set_at TEXT, PRIMARY KEY(sha256, member_id));
"""
MEMBERS = """
CREATE TABLE IF NOT EXISTS members(      -- who the estate knows: not persons (who is *in* photos, faces.py)
  id INTEGER PRIMARY KEY, name TEXT,
  kind TEXT DEFAULT 'person',            -- person | agent | device
  role TEXT DEFAULT 'member',            -- sysadmin | member | viewer
  person_id INT,                         -- persons.id: who this member is in photos, if known
  upload_policy TEXT DEFAULT 'review',   -- everything | review | review_flagged
  joined_at TEXT,                        -- when first let in
  status TEXT DEFAULT 'pending');        -- pending | active | revoked (never deleted)
CREATE TABLE IF NOT EXISTS member_logins(   -- a Tailscale login, and the member it is (a member may have several)
  login TEXT PRIMARY KEY, member_id INT NOT NULL, added_at TEXT);
CREATE TABLE IF NOT EXISTS journal(      -- what happened, appended to and never changed
  id INTEGER PRIMARY KEY, at TEXT,
  actor INT,                             -- members.id of who did it; NULL: the app itself
  acting_for INT,                        -- members.id when the actor is an agent
  kind TEXT,                             -- member.added, member.requested, member.changed, ...
  subject TEXT,                          -- what it is about: member:<id>, a sha, album:<id> ...
  data TEXT);                            -- JSON: whatever else the kind needs
CREATE TRIGGER IF NOT EXISTS journal_no_delete BEFORE DELETE ON journal
  BEGIN SELECT RAISE(ABORT, 'the journal is append-only'); END;
CREATE TRIGGER IF NOT EXISTS journal_no_update BEFORE UPDATE ON journal
  BEGIN SELECT RAISE(ABORT, 'the journal is append-only'); END;
CREATE TRIGGER IF NOT EXISTS journal_no_replace BEFORE INSERT ON journal
  WHEN EXISTS (SELECT 1 FROM journal WHERE id = NEW.id)
  BEGIN SELECT RAISE(ABORT, 'the journal is append-only: an entry is never replaced'); END;
CREATE TRIGGER IF NOT EXISTS members_no_delete BEFORE DELETE ON members
  BEGIN SELECT RAISE(ABORT, 'a member is never deleted: revoke it (status revoked)'); END;
CREATE TRIGGER IF NOT EXISTS members_keep_key BEFORE UPDATE OF id ON members WHEN NEW.id IS NOT OLD.id
  BEGIN SELECT RAISE(ABORT, 'members: id never changes'); END;
CREATE TRIGGER IF NOT EXISTS members_no_replace BEFORE INSERT ON members
  WHEN EXISTS (SELECT 1 FROM members WHERE id = NEW.id)
  BEGIN SELECT RAISE(ABORT, 'a member is never replaced'); END;
CREATE TRIGGER IF NOT EXISTS member_logins_no_delete BEFORE DELETE ON member_logins
  BEGIN SELECT RAISE(ABORT, 'a login is never deleted: revoke its member'); END;
CREATE TRIGGER IF NOT EXISTS member_logins_keep_key BEFORE UPDATE OF login, member_id ON member_logins
  WHEN NEW.login IS NOT OLD.login OR NEW.member_id IS NOT OLD.member_id
  BEGIN SELECT RAISE(ABORT, 'member_logins: login and member_id never change'); END;
CREATE TRIGGER IF NOT EXISTS member_logins_no_replace BEFORE INSERT ON member_logins
  WHEN EXISTS (SELECT 1 FROM member_logins WHERE login = NEW.login)
  BEGIN SELECT RAISE(ABORT, 'a login is never replaced: it stays its member''s'); END;
CREATE TABLE IF NOT EXISTS inbox_items(  -- what a member has to decide (member_inbox.py; not the upload folder)
  id INTEGER PRIMARY KEY, member_id INT,
  kind TEXT,                             -- member_request | review | lens_proposed | lens_changed
  ref TEXT,                              -- what it is about, as the journal names it: member:<id>, album:<id>, lens:<id>
  title TEXT, created_at TEXT, updated_at TEXT,
  state TEXT DEFAULT 'open',             -- open | done | dismissed
  done_at TEXT);
CREATE INDEX IF NOT EXISTS inbox_member ON inbox_items(member_id, state);
"""
# Questions and stories (stories.py; multiplayer phase 7), and where each listener has read the journal to
# (listeners.py). A story is what a member said: append-only like the journal, but for its transcript, written
# once when the recording has been transcribed (transcribe.py). Which photos a story is about is a row each in
# story_photos, so a photo's stories are an index lookup, and `estate.py purge` (which deletes a photo's rows from
# every table with a sha256 column, dropping the *_no_delete triggers in its transaction) takes the purged photo
# out of the stories about it and leaves the stories.
STORIES = """
CREATE TABLE IF NOT EXISTS questions(    -- what an agent (or a member) asks a member, or the whole family
  id INTEGER PRIMARY KEY,
  asked_by INT,                          -- members.id: the estate's agent, or a member
  acting_for INT,                        -- members.id the asking agent's run acted for; NULL: no one (the archivist)
  to_member INT,                         -- members.id; NULL: open to the family
  shas TEXT,                             -- JSON list of the photos it is about
  text TEXT, why TEXT,
  group_key TEXT,                        -- questions with the same key are one inbox item ("Thanksgiving 1998")
  priority REAL,                         -- the asker's, 0-1: the last of the inbox's ranking
  follow_up INT DEFAULT 0,               -- 1: an answer starts a new run of the estate's agent (listeners.py)
  task_id INT,                           -- agent_tasks.id of the run that asked
  status TEXT DEFAULT 'queued',          -- queued | asked | answered | closed
  created_at TEXT);
CREATE TABLE IF NOT EXISTS asked(        -- a question dealt to a member's inbox (stories.deal): the waiting is this row
  question_id INT, member_id INT,
  item_id INT,                           -- inbox_items.id it is shown in (one item per group)
  at TEXT, answered_at TEXT, dismissed_at TEXT,
  PRIMARY KEY(question_id, member_id));
CREATE TABLE IF NOT EXISTS stories(      -- what a member said about photos, attributed and dated, never edited
  id INTEGER PRIMARY KEY,
  member_id INT,                         -- who told it
  question_id INT,                       -- the question it answers, or NULL
  text TEXT,                             -- what they typed (NULL for a recording alone)
  recording TEXT, recording_ext TEXT,    -- the sha256 of the audio: a file in the store with no assets row
  told_at TEXT,
  transcript TEXT,                       -- the recording, written out (transcribe.py): set once
  transcribed_at TEXT, transcript_error TEXT);
CREATE TABLE IF NOT EXISTS story_photos(story_id INT, sha256 TEXT, PRIMARY KEY(story_id, sha256));
CREATE INDEX IF NOT EXISTS story_photos_sha ON story_photos(sha256);
CREATE TABLE IF NOT EXISTS listener_cursors(name TEXT PRIMARY KEY, journal_id INT);
CREATE TRIGGER IF NOT EXISTS stories_no_delete BEFORE DELETE ON stories
  BEGIN SELECT RAISE(ABORT, 'a story is never deleted'); END;
CREATE TRIGGER IF NOT EXISTS stories_only_transcript BEFORE UPDATE ON stories
  WHEN NEW.id IS NOT OLD.id OR NEW.member_id IS NOT OLD.member_id OR NEW.question_id IS NOT OLD.question_id
    OR NEW.text IS NOT OLD.text OR NEW.recording IS NOT OLD.recording OR NEW.recording_ext IS NOT OLD.recording_ext
    OR NEW.told_at IS NOT OLD.told_at OR (OLD.transcript IS NOT NULL AND (NEW.transcript IS NOT OLD.transcript
    OR NEW.transcribed_at IS NOT OLD.transcribed_at))
  BEGIN SELECT RAISE(ABORT, 'a story is never changed: only its transcript is written, once'); END;
CREATE TRIGGER IF NOT EXISTS stories_no_replace BEFORE INSERT ON stories
  WHEN EXISTS (SELECT 1 FROM stories WHERE id = NEW.id)
  BEGIN SELECT RAISE(ABORT, 'a story is never replaced'); END;
CREATE TRIGGER IF NOT EXISTS story_photos_no_delete BEFORE DELETE ON story_photos
  BEGIN SELECT RAISE(ABORT, 'a story keeps its photos: removed only by estate.py purge'); END;
CREATE TRIGGER IF NOT EXISTS story_photos_no_update BEFORE UPDATE ON story_photos
  BEGIN SELECT RAISE(ABORT, 'a story keeps its photos'); END;
CREATE TRIGGER IF NOT EXISTS story_photos_no_replace BEFORE INSERT ON story_photos
  WHEN EXISTS (SELECT 1 FROM story_photos WHERE story_id = NEW.story_id AND sha256 = NEW.sha256)
  BEGIN SELECT RAISE(IGNORE); END;
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

AREAS = {"catalog": CATALOG + APPEND_ONLY + PRIVATE, "embeddings": EMBEDDINGS, "video": VIDEO, "faces": FACES, "changes": CHANGES, "tasks": TASKS,
         "scores": SCORES, "favorites": FAVORITES, "members": MEMBERS, "lenses": LENSES, "stories": STORIES, "sync": SYNC, "intake": INTAKE}
CATALOG_AREAS = ("catalog", "embeddings", "video", "faces", "changes", "tasks", "scores", "favorites", "members", "lenses",
                 "stories")
ADDED = {
    # owner: the member whose album or set it is (NULL: the first sysadmin's); lens_id: the filter lens a hiding
    # set hides for (lenses.py; NULL: from before lenses, hiding for everyone until `estate.py migrate`)
    "changes": {"change_sets": (("done_at", "TEXT"), ("kind", "TEXT DEFAULT 'list'"), ("query", "TEXT"),
                                ("owner", "INT"), ("lens_id", "INT"),
                                # grows_from: a list album's search that new photos are matched against (a series,
                                # kept up to date by listeners.py): JSON as `query`; NULL for every other album
                                ("grows_from", "TEXT")),
                "change_items": (("section", "TEXT"), ("position", "INTEGER"))},
    # acting_for: members.id of whom the run works for; started_by: who or what started it (member:<id>, or for a
    # run a listener started journal:<entry id>/<what it is about>: listeners.py)
    "tasks": {"agent_tasks": (("acting_for", "INT"), ("started_by", "TEXT"))},
    # questions_per_week: this member's own allowance of questions (stories.py: NULL, the estate's ALLOWANCE)
    "members": {"members": (("questions_per_week", "INT"),)},
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
