#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow>=10.0", "pillow-heif>=0.18"]
# ///
"""
estate.py — proof of concept for repatriating a Google Photos library.

  ingest  Takeout zips/dirs -> content-addressed store + SQLite catalog (exact de-dupe)
  link    match Takeout JSON sidecars to media across all zips; merge date/GPS/people/albums
          (also iCloud XMP sidecars and Flickr account-data photo_<id>.json)
  exif    full EXIF/XMP/QuickTime metadata of every asset via exiftool; camera, lens and exposure
          promoted to columns for filtering
  dupes   report exact duplicates and near-duplicates (perceptual dHash)
  review  local web page to choose which near-duplicates to keep (records choices only)
  tag     caption + tag images with a local vision model (any OpenAI-compatible server)
  search  full-text search over filenames, albums, people, descriptions, captions, tags
  stats   summary of the estate

Originals are never modified or deleted (except the source zip with --delete-after).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from PIL import Image, ImageOps

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:  # HEIC support is optional
    pass

Image.MAX_IMAGE_PIXELS = None  # panoramas are legitimate

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".webp", ".bmp", ".tif", ".tiff",
             ".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2", ".raf", ".avif"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".3gp", ".avi", ".mkv", ".mts", ".m2ts", ".webm", ".mpg", ".wmv"}
YEAR_DIR = re.compile(r"^(Photos from|Fotos von|Photos de|Fotos de|Foto dal|Foto's uit) \d{4}$")
EDITED = re.compile(r"-(edited|bearbeitet|modifié|editado|modificato|bewerkt|redigeret|muokattu)$", re.I)
DUPN = re.compile(r"\(\d+\)$")
DATE_DIR = re.compile(r"^\d{4}(/\d{2}){0,2}$")  # icloudpd's YYYY/MM/DD folders are dates, not albums
# Flickr data-download originals: "<title>_<id>_o.jpg" or "<id>_<secret>_o.jpg"
FLICKR_FILE = re.compile(r"(\d+)(?:_[0-9a-f]{10})?_o\.[A-Za-z0-9]+$")
# Titles Flickr copied from the filename at upload (DSCF0721.jpg, dsc_5300) say nothing
FILENAME_TITLE = re.compile(r"^[A-Za-z]{0,6}[_-]?\d+(\.[A-Za-z0-9]{3,4})?$")
U64 = (1 << 64) - 1

SCHEMA = """
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


# ----------------------------------------------------------------------------- helpers

def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db: str) -> sqlite3.Connection:
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    return con


def kind_of(name: str) -> str | None:
    ext = PurePosixPath(name).suffix.lower()
    return "image" if ext in IMAGE_EXT else "video" if ext in VIDEO_EXT else None


def blob_path(store: Path, sha: str, ext: str) -> Path:
    return store / sha[:2] / sha[2:4] / f"{sha}{ext}"


def split_dupn(s: str) -> tuple[str, str]:
    m = DUPN.search(s)
    return (s[:m.start()], m.group(0)) if m else (s, "")


def default_album(d: str) -> str | None:
    if DATE_DIR.match(d):
        return None
    name = PurePosixPath(d).name
    if YEAR_DIR.match(name) or name in ("", ".", "Takeout", "Google Photos", "Google Fotos"):
        return None
    return name


def iter_entries(src: Path):
    """Yield (posix_path, opener) for every file in a zip or directory tree."""
    if src.is_dir():
        for p in sorted(src.rglob("*")):
            if p.is_file():
                yield p.relative_to(src).as_posix(), (lambda p=p: open(p, "rb"))
    else:
        with zipfile.ZipFile(src) as zf:
            for info in zf.infolist():
                if not info.is_dir():
                    yield info.filename, (lambda info=info: zf.open(info))


# ----------------------------------------------------------------------------- ingest

def store_blob(con, store: Path, fp, ext: str) -> tuple[str, int, Path | None]:
    """Stream fp into the store while hashing. Returns (sha, size, new_path or None if dup)."""
    tmp = store / ".tmp" / f"{os.getpid()}.{time.time_ns()}"
    h, n = hashlib.sha256(), 0
    with open(tmp, "wb") as out:
        while chunk := fp.read(1 << 20):
            h.update(chunk)
            out.write(chunk)
            n += len(chunk)
    sha = h.hexdigest()
    if con.execute("SELECT 1 FROM assets WHERE sha256=?", (sha,)).fetchone():
        tmp.unlink()
        return sha, n, None
    dest = blob_path(store, sha, ext)
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(tmp, dest)
    return sha, n, dest


def image_facts(path: Path) -> dict:
    """Dimensions, EXIF DateTimeOriginal and 64-bit dHash. Never raises."""
    facts = {"width": None, "height": None, "exif_taken_at": None, "dhash": None}
    try:
        with Image.open(path) as im:
            facts["width"], facts["height"] = im.size
            exif = im.getexif()
            raw = exif.get_ifd(0x8769).get(36867) or exif.get(306)
            if raw:
                try:
                    dt = datetime.strptime(str(raw).strip("\x00 ")[:19], "%Y:%m:%d %H:%M:%S")
                    facts["exif_taken_at"] = dt.isoformat()
                except ValueError:
                    pass
            im.draft("L", (128, 128))  # fast JPEG decode at reduced size
            g = ImageOps.exif_transpose(im).convert("L").resize((9, 8), Image.Resampling.LANCZOS)
            px = g.tobytes()
            bits = 0
            for r in range(8):
                for c in range(8):
                    bits = (bits << 1) | (px[r * 9 + c] > px[r * 9 + c + 1])
            facts["dhash"] = bits - (1 << 64) if bits >= (1 << 63) else bits  # fit SQLite INT64
    except Exception as e:  # RAW formats, corrupt files, etc.
        facts["error"] = str(e)
    return facts


def handle_json(con, d: str, fn: str, opener, st: dict) -> None:
    try:
        with opener() as fp:
            data = json.load(fp)
    except Exception:
        return
    if not isinstance(data, dict):
        return
    if "photopage" in data and "date_taken" in data and data.get("id"):  # Flickr photo_<id>.json
        con.execute("INSERT OR REPLACE INTO flickr(id, data) VALUES (?,?)", (str(data["id"]), json.dumps(data)))
        st["sidecars"] += 1
    elif "photoTakenTime" in data:
        con.execute("INSERT OR IGNORE INTO sidecars(dir, filename, title, data) VALUES (?,?,?,?)",
                    (d, fn, data.get("title"), json.dumps(data)))
        st["sidecars"] += 1
    elif isinstance(data.get("title"), str):  # album metadata.json (name is localized)
        con.execute("INSERT OR REPLACE INTO albums(dir, title, data) VALUES (?,?,?)",
                    (d, data["title"], json.dumps(data)))


XMP_FIELDS = {"taken": "photoshop:DateCreated", "lat": "exif:GPSLatitude", "lon": "exif:GPSLongitude",
              "rating": "xmp:Rating"}


def parse_xmp(text: str) -> dict:
    """The few fields icloudpd writes that matter here. Its XMP is flat, so a regex per tag is enough."""
    out = {}
    for key, tag in XMP_FIELDS.items():
        m = re.search(rf"<{tag}>([^<]+)</{tag}>", text)
        if m:
            out[key] = m.group(1).strip()
    out["keywords"] = re.findall(r"<rdf:li>([^<]+)</rdf:li>", text)
    if "taken" in out:  # 2024-06-13T21:06:35-0700 -> UTC, comparable with Google's sidecar times
        try:
            out["taken"] = datetime.strptime(out["taken"], "%Y-%m-%dT%H:%M:%S%z").astimezone(
                timezone.utc).isoformat()
        except ValueError:
            del out["taken"]
    return out


def handle_xmp(con, d: str, fn: str, opener, st: dict) -> None:
    try:
        with opener() as fp:
            data = parse_xmp(fp.read().decode("utf-8", "replace"))
    except Exception:
        return
    con.execute("INSERT OR REPLACE INTO xmp(dir, filename, data) VALUES (?,?,?)", (d, fn, json.dumps(data)))
    st["sidecars"] += 1


def cmd_ingest(a) -> None:
    store = Path(a.store)
    (store / ".tmp").mkdir(parents=True, exist_ok=True)
    con = connect(a.db)
    for src in map(Path, a.sources):
        name = src.name
        row = con.execute("SELECT finished_at FROM sources WHERE name=?", (name,)).fetchone()
        if row and row[0] and not a.force:
            print(f"skip {name}: already ingested at {row[0]}")
            continue
        t0 = time.time()
        st = dict(entries=0, media=0, new=0, dup=0, sidecars=0, seen=0, new_bytes=0, unreadable=0)
        for i, (path, opener) in enumerate(iter_entries(src), 1):
            st["entries"] += 1
            pp = PurePosixPath(path)
            d, fn = str(pp.parent), pp.name
            if fn.lower().endswith(".json"):
                handle_json(con, d, fn, opener, st)
                continue
            if fn.lower().endswith(".xmp"):
                handle_xmp(con, d, fn, opener, st)
                continue
            kind = kind_of(fn)
            if not kind:
                continue
            st["media"] += 1
            if con.execute("SELECT 1 FROM occurrences WHERE source=? AND path=?",
                           (name, path)).fetchone():
                continue  # resuming an interrupted run
            ext = pp.suffix.lower()
            with opener() as fp:
                sha, size, dest = store_blob(con, store, fp, ext)
            st["seen"] += size
            if dest:
                facts = image_facts(dest) if kind == "image" else {}
                st["unreadable"] += "error" in facts
                con.execute(
                    "INSERT INTO assets(sha256, ext, kind, size, width, height, dhash, exif_taken_at,"
                    " taken_at, first_seen) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (sha, ext, kind, size, facts.get("width"), facts.get("height"), facts.get("dhash"),
                     facts.get("exif_taken_at"), facts.get("exif_taken_at"), now()))
                st["new"] += 1
                st["new_bytes"] += size
            else:
                st["dup"] += 1
            con.execute("INSERT OR IGNORE INTO occurrences(sha256, source, path, dir, filename, album)"
                        " VALUES (?,?,?,?,?,?)", (sha, name, path, d, fn, default_album(d)))
            if i % 200 == 0:
                con.commit()
        con.execute("INSERT OR REPLACE INTO sources VALUES (?,?,?,?,?,?,?)",
                    (name, st["entries"], st["media"], st["new"], st["seen"], st["new_bytes"], now()))
        con.commit()
        dt = max(time.time() - t0, 1e-6)
        print(f"{name}: {st['entries']} entries, {st['media']} media -> {st['new']} new, "
              f"{st['dup']} exact dups, {st['sidecars']} sidecars, "
              f"{st['new_bytes'] / 1e6:.1f} MB written ({st['seen'] / 1e6 / dt:.1f} MB/s)"
              + (f", {st['unreadable']} not decodable (kept, no dHash)" if st["unreadable"] else ""))
        if a.delete_after and src.is_file():
            src.unlink()
            print(f"  deleted {src}")
    print("next: run `link` to attach sidecar metadata")


# ----------------------------------------------------------------------------- link

class SidecarIndex:
    """Sidecars of one directory, indexed so each lookup is O(name length), not O(#sidecars)."""
    def __init__(self):
        self.raw: dict[str, int] = {}                  # filename without .json -> id
        self.by_stem: dict[tuple[str, str], int] = {}  # (stem, "(n)") -> id
        self.by_title: dict[str, int] = {}             # title of sidecars without (n) -> id

    def add(self, sid: int, s: str, sn: str, title: str | None) -> None:
        self.raw.setdefault(s + sn, sid)
        self.by_stem.setdefault((s, sn), sid)
        if not sn and title:
            self.by_title.setdefault(title, sid)


EMPTY_INDEX = SidecarIndex()


def _lookup(stem: str, ext: str, n: str, idx: SidecarIndex) -> int | None:
    stem = EDITED.sub("", stem)         # IMG-edited  -> IMG
    base = stem + ext
    full = base + ".supplemental-metadata"
    # exact, old-style without extension, or truncated (~51 char limit on the json name);
    # the longest matching sidecar name wins
    keys = {base, full, stem} | {full[:L] for L in range(30, len(full) + 1)}
    best = max((k for k in keys if (k, n) in idx.by_stem), key=len, default=None)
    if best is not None:
        return idx.by_stem[(best, n)]
    if not n:
        return idx.by_title.get(base)
    return None


def match_sidecar(fn: str, idx: SidecarIndex) -> int | None:
    """Pick the Takeout sidecar for media filename fn among sidecars in the same directory."""
    for k in (fn, fn + ".supplemental-metadata"):     # exact names first
        if k in idx.raw:
            return idx.raw[k]
    stem, ext = os.path.splitext(fn)
    s, n = split_dupn(stem)             # IMG(1).jpg  -> IMG.jpg + (1)
    sid = _lookup(s, ext, n, idx)
    if sid is None and n:               # "(1)" was part of the real name: "WP_..._Smart (1).jpg"
        sid = _lookup(stem, ext, "", idx)
    return sid


def cmd_link(a) -> None:
    con = connect(a.db)
    side: dict[str, SidecarIndex] = {}
    for sid, d, fn, title in con.execute("SELECT id, dir, filename, title FROM sidecars ORDER BY id"):
        s, n = split_dupn(fn[:-5])
        side.setdefault(d, SidecarIndex()).add(sid, s, n, title)
    albums = dict(con.execute("SELECT dir, title FROM albums"))
    flickr = {fid: json.loads(raw) for fid, raw in con.execute("SELECT id, data FROM flickr")}
    matched = total = live = 0
    pending, by_stem, flickr_hits = [], {}, {}
    for oid, sha, d, fn in con.execute("SELECT id, sha256, dir, filename FROM occurrences").fetchall():
        sid = match_sidecar(fn, side.get(d, EMPTY_INDEX))
        album = albums.get(d) or default_album(d)
        fl = flickr_for(fn, flickr) if sid is None else None
        if fl:
            album = ", ".join(a["title"] for a in fl.get("albums") or [] if a.get("title")) or album
            flickr_hits[sha] = fl
        con.execute("UPDATE occurrences SET sidecar_id=?, album=? WHERE id=?", (sid, album, oid))
        total += 1
        if fl:
            matched += 1
        elif sid is None:
            pending.append((oid, d, fn))
        else:
            matched += 1
            if kind_of(fn) == "image":
                by_stem.setdefault((d, os.path.splitext(fn)[0].lower()), []).append(sid)
    # Live Photos / motion photos: Takeout exports IMG_1.HEIC + IMG_1.MP4 with a single sidecar
    # for the still. Give the motion clip the still's sidecar.
    # When two stills share a name (IMG_1904.JPG + IMG_1904.HEIC), Takeout calls the second clip
    # IMG_1904(1).MP4: plain names go first, then a (n) clip takes a still no other clip has taken.
    unmatched, used = [], set()
    pending.sort(key=lambda r: bool(split_dupn(os.path.splitext(r[2])[0])[1]))
    for oid, d, fn in pending:
        sid = None
        if kind_of(fn) == "video":
            stem = os.path.splitext(fn)[0]
            cands = by_stem.get((d, stem.lower())) or by_stem.get((d, split_dupn(stem)[0].lower())) or []
            sid = next((c for c in cands if c not in used), cands[0] if cands else None)
            if sid is not None:
                used.add(sid)
        if sid is None:
            unmatched.append(f"{d}/{fn}")
            continue
        con.execute("UPDATE occurrences SET sidecar_id=? WHERE id=?", (sid, oid))
        matched += 1
        live += 1

    merged: dict[str, dict] = {}
    for sha, raw in con.execute("SELECT o.sha256, s.data FROM occurrences o"
                                " JOIN sidecars s ON s.id = o.sidecar_id"):
        m, acc = json.loads(raw), merged.setdefault(sha, {"people": set(), "fav": 0})
        ts = (m.get("photoTakenTime") or {}).get("timestamp")
        if ts and "taken" not in acc:
            acc["taken"] = datetime.fromtimestamp(int(ts), timezone.utc).isoformat()
        for g in ("geoData", "geoDataExif"):
            geo = m.get(g) or {}
            if "lat" not in acc and (geo.get("latitude") or geo.get("longitude")):
                acc["lat"], acc["lon"] = geo["latitude"], geo["longitude"]
        if m.get("description") and "desc" not in acc:
            acc["desc"] = m["description"]
        acc["people"].update(p["name"] for p in m.get("people", []) if p.get("name"))
        acc["fav"] |= bool(m.get("favorited"))
    # iCloud XMP sidecars fill whatever no Google sidecar supplied. Their time carries the UTC
    # offset, so iCloud and Google copies of one photo get the same taken_at.
    for sha, raw in con.execute("SELECT o.sha256, x.data FROM occurrences o"
                                " JOIN xmp x ON x.dir = o.dir AND x.filename = o.filename || '.xmp'"):
        m, acc = json.loads(raw), merged.setdefault(sha, {"people": set(), "fav": 0})
        if m.get("taken") and "taken" not in acc:
            acc["taken"] = m["taken"]
        if "lat" not in acc and m.get("lat") and m.get("lon"):
            acc["lat"], acc["lon"] = float(m["lat"]), float(m["lon"])
        acc["fav"] |= m.get("rating") == "5"
    # Flickr fills what is still missing. Its date_taken is camera-local with no offset (like
    # EXIF), so it is only a fallback after EXIF. Title + description become the description;
    # Flickr tags are stored as tags of model "flickr" so they are searchable.
    con.execute("DELETE FROM tags WHERE model='flickr'")
    for sha, m in flickr_hits.items():
        acc = merged.setdefault(sha, {"people": set(), "fav": 0})
        if m.get("date_taken"):
            acc.setdefault("fallback_taken", m["date_taken"].replace(" ", "T"))
        for g in m.get("geo") or []:   # micro-degrees as strings
            if "lat" not in acc and g.get("latitude") and g.get("longitude"):
                acc["lat"], acc["lon"] = int(g["latitude"]) / 1e6, int(g["longitude"]) / 1e6
        if "desc" not in acc and (text := flickr_text(m)):
            acc["desc"] = text
        tags = sorted({t["tag"] for t in m.get("tags") or [] if t.get("tag")})
        con.execute("INSERT OR REPLACE INTO tags VALUES (?,?,?,?,?,?,?)",
                    (sha, "flickr", None, json.dumps(tags),
                     json.dumps({"id": m["id"], "photopage": m.get("photopage"), "privacy": m.get("privacy")}),
                     0, now()))
    for sha, acc in merged.items():
        con.execute("UPDATE assets SET taken_at=coalesce(?, exif_taken_at, ?), lat=?, lon=?,"
                    " description=?, people=?, favorited=? WHERE sha256=?",
                    (acc.get("taken"), acc.get("fallback_taken"), acc.get("lat"), acc.get("lon"),
                     acc.get("desc"), ", ".join(sorted(acc["people"])) or None, acc["fav"], sha))
    reindex(con)
    con.commit()
    print(f"linked {matched}/{total} occurrences to sidecars ({live} Live Photo clips via their still,"
          f" {len(flickr_hits)} Flickr photos); metadata merged onto {len(merged)} assets")
    if unmatched:
        print(f"{len(unmatched)} without a sidecar (may be in a zip not yet ingested):")
        for u in unmatched[:20]:
            print("  ", u)


def flickr_for(fn: str, flickr: dict) -> dict | None:
    """The Flickr photo JSON for a data-download original, found by the photo id in its name."""
    if not flickr or not (m := FLICKR_FILE.search(fn)):
        return None
    for cand in [m.group(1), *re.findall(r"\d+", fn)]:  # a title ending in digits can fool the regex
        if cand in flickr:
            return flickr[cand]
    return None


def flickr_text(m: dict) -> str:
    """Title and description of a Flickr photo as plain text (descriptions may hold HTML)."""
    title = (m.get("name") or "").strip()
    if FILENAME_TITLE.match(title):
        title = ""
    desc = html.unescape(re.sub(r"<[^>]+>", " ", m.get("description") or ""))
    desc = re.sub(r"\s+", " ", desc).strip()
    return " — ".join(x for x in (title, desc) if x)


# ----------------------------------------------------------------------------- exif

MAKERS = {"nikon": "Nikon", "canon": "Canon", "fujifilm": "Fujifilm", "sony": "Sony", "olympus": "Olympus",
          "panasonic": "Panasonic", "samsung": "Samsung", "lge": "LG", "lg": "LG", "eastman": "Kodak",
          "kodak": "Kodak", "motorola": "Motorola", "htc": "HTC", "google": "Google", "apple": "Apple",
          "microsoft": "Microsoft", "nokia": "Nokia", "ricoh": "Ricoh", "pentax": "Pentax", "leica": "Leica",
          "hasselblad": "Hasselblad", "dji": "DJI", "gopro": "GoPro", "huawei": "Huawei", "xiaomi": "Xiaomi",
          "oneplus": "OnePlus", "minolta": "Minolta", "konica": "Konica Minolta", "casio": "Casio"}


def camera_name(make: str | None, model: str | None) -> str | None:
    """One readable name per camera: ("NIKON CORPORATION", "NIKON D300") -> "Nikon D300"."""
    make, model = (str(make or "").strip(" \x00"), str(model or "").strip(" \x00"))
    first = make.split()[0].lower().strip(",.") if make else ""
    brand = (MAKERS.get(first) or (make.split()[0].title() if make.isupper() else make)) if make else ""
    if not model:
        return brand or None
    words = model.split()
    if brand and words and words[0].lower().strip(",.") in (first, brand.lower()):
        words = words[1:]           # "NIKON D300" -> "D300"; "Canon EOS R5" -> "EOS R5"
    name = " ".join(words) or model
    return f"{brand} {name}".strip() if brand else name


def _pick(d: dict, *names: str):
    """First non-empty value whose tag name (without its -G1 group) is in names, in names order."""
    for n in names:
        for k, v in d.items():
            if k.rsplit(":", 1)[-1] == n and v not in (None, "", 0, "0"):
                return v
    return None


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def exif_row(d: dict) -> dict:
    """Promote the fields worth filtering on from one exiftool record."""
    make, model = _pick(d, "Make"), _pick(d, "Model")
    iso = _num(_pick(d, "ISO", "ISOSpeedRatings", "RecommendedExposureIndex"))
    return {
        "camera": camera_name(make, model), "make": make and str(make).strip(), "model": model and str(model).strip(),
        "lens": (str(v).strip() or None) if (v := _pick(d, "LensModel", "Lens", "LensID", "LensType")) else None,
        "focal": _num(_pick(d, "FocalLength")), "focal35": _num(_pick(d, "FocalLengthIn35mmFormat", "FocalLength35efl")),
        "fnumber": _num(_pick(d, "FNumber", "ApertureValue")), "exposure": _num(_pick(d, "ExposureTime")),
        "iso": int(iso) if iso else None, "software": (str(v).strip() if (v := _pick(d, "Software")) else None),
        "duration": _num(_pick(d, "Duration")), "fps": _num(_pick(d, "VideoFrameRate")),
    }


DROP_GROUPS = ("System:", "ExifTool:")


def _exiftool_batch(paths: list[str]) -> list[dict]:
    """Run exiftool on a batch of files. Top-level so worker threads can share it."""
    args = ["exiftool", "-json", "-n", "-G1", "-a", "--MakerNotes:all", "-charset", "filename=utf8", "-@", "-"]
    r = subprocess.run(args, input="\n".join(paths), capture_output=True, text=True)
    try:
        return json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return []


def cmd_exif(a) -> None:
    from concurrent.futures import ThreadPoolExecutor
    if not shutil.which("exiftool"):
        sys.exit("exiftool not found: install it (Arch: sudo pacman -S perl-image-exiftool)")
    con, store = connect(a.db), Path(a.store)
    q = "SELECT sha256, ext FROM assets" + ("" if a.force else " WHERE sha256 NOT IN (SELECT sha256 FROM exif)")
    todo = con.execute(q + " ORDER BY sha256").fetchall()[: a.limit or None]
    print(f"{len(todo)} assets to read with exiftool", flush=True)
    by_path = {str(blob_path(store, sha, ext)): sha for sha, ext in todo}
    batches = [list(by_path)[i:i + a.batch] for i in range(0, len(by_path), a.batch)]
    t0, n, cams = time.time(), 0, 0
    with ThreadPoolExecutor(a.workers) as ex:
        for paths, recs in zip(batches, ex.map(_exiftool_batch, batches)):
            got = {}
            for rec in recs:
                src = rec.pop("SourceFile", None)
                if src in by_path:
                    got[by_path[src]] = {k: v for k, v in rec.items() if not k.startswith(DROP_GROUPS)
                                         and not (isinstance(v, str) and v.startswith("(Binary data"))}
            for p in paths:
                sha, d = by_path[p], got.get(by_path[p])
                if d is None:
                    con.execute("INSERT OR REPLACE INTO exif(sha256, error) VALUES (?, ?)", (sha, "exiftool returned nothing"))
                    continue
                row = exif_row(d)
                cams += row["camera"] is not None
                con.execute(f"INSERT OR REPLACE INTO exif(sha256, {', '.join(row)}, data, error) VALUES"
                            f" (?, {', '.join('?' * len(row))}, ?, ?)",
                            (sha, *row.values(), json.dumps(d, ensure_ascii=False, default=str),
                             d.get("ExifTool:Error") or d.get("Error")))
            con.commit()
            n += len(paths)
            dt = time.time() - t0
            print(f"{time.strftime('%H:%M:%S')} {n}/{len(todo)} {n / dt:.0f} files/s,"
                  f" ~{(len(todo) - n) / max(n / dt, 1e-6) / 60:.0f} min left", flush=True)
    reindex(con)
    con.commit()
    print(f"done: {n} assets in {(time.time() - t0) / 60:.1f} min, {cams} with a camera name")
    for cam, c in con.execute("SELECT camera, count(*) c FROM exif WHERE camera IS NOT NULL"
                              " GROUP BY camera ORDER BY c DESC LIMIT 25"):
        print(f"  {c:7}  {cam}")


def reindex(con, shas: list[str] | None = None) -> None:
    """Rebuild full-text search rows. The FTS row id is the asset's rowid, so deleting one asset's
    row is a direct lookup (sha256 is UNINDEXED in FTS5; deleting by it scans the whole table)."""
    if shas:
        where, args = f"WHERE sha256 IN ({','.join('?' * len(shas))})", shas
    else:
        where, args = "", []
        con.execute("DELETE FROM search")
    for rid, sha, desc, people in con.execute(
            f"SELECT rowid, sha256, description, people FROM assets {where}", args).fetchall():
        parts = [desc or "", people or ""]
        for fn, album in con.execute("SELECT filename, album FROM occurrences WHERE sha256=?", (sha,)):
            parts += [fn, album or ""]
        for caption, tags in con.execute("SELECT caption, tags FROM tags WHERE sha256=?", (sha,)):
            parts += [caption or "", " ".join(json.loads(tags or "[]"))]
        for camera, lens in con.execute("SELECT camera, lens FROM exif WHERE sha256=?", (sha,)):
            parts += [camera or "", lens or ""]
        for row in con.execute("SELECT p.city, p.locality, p.admin2, p.admin1, p.country, e.title, t.title FROM places p"
                               " LEFT JOIN events e ON e.id = p.event_id LEFT JOIN trips t ON t.id = e.trip_id"
                               " WHERE p.sha256=?", (sha,)):
            parts += [x or "" for x in row]
        if shas:
            con.execute("DELETE FROM search WHERE rowid=?", (rid,))
        con.execute("INSERT INTO search(rowid, sha256, text) VALUES (?,?,?)",
                    (rid, sha, " ".join(p for p in parts if p)))


# ----------------------------------------------------------------------------- dupes

def near_dupe_clusters(items: list[tuple], t: int, strict: int = 2, crowd: int = 10,
                       flat: int = 2) -> list[list[int]]:
    """Group images whose 64-bit dHash is within Hamming distance t. items: (sha, dhash[, taken_at]).

    Candidate pairs come from pigeonhole banding: split the hash into t+1 bands; any pair within
    distance t agrees exactly on at least one band, so only same-bucket pairs are compared.
    Three guards against false matches:
      * near-blank images (almost all hash bits equal) are skipped: their hash says nothing;
      * in crowded regions (an image with >= `crowd` neighbours, e.g. screenshots of one app),
        a pair needs distance <= `strict` unless both were taken within 2 s of each other;
      * groups are stars, not chains: every member is a direct match of the group's centre, so
        A~B~C~...~Z can't merge unrelated photos into one giant group.
    """
    hs = [it[1] & U64 for it in items]
    tt = [_ts(it[2]) if len(it) > 2 else None for it in items]
    usable = [flat < h.bit_count() < 64 - flat for h in hs]
    edges: dict[tuple[int, int], int] = {}
    width = 64 // (t + 1)
    for b in range(t + 1):
        lo, hi = b * width, 64 if b == t else (b + 1) * width
        mask = ((1 << (hi - lo)) - 1) << lo
        buckets: dict[int, list[int]] = {}
        for i, h in enumerate(hs):
            if usable[i]:
                buckets.setdefault(h & mask, []).append(i)
        for idx in buckets.values():
            for x in range(len(idx)):
                for y in range(x + 1, len(idx)):
                    i, j = idx[x], idx[y]
                    d = (hs[i] ^ hs[j]).bit_count()
                    if d <= t:
                        edges[(i, j)] = d
    deg: dict[int, int] = {}
    for i, j in edges:
        deg[i] = deg.get(i, 0) + 1
        deg[j] = deg.get(j, 0) + 1
    adj: dict[int, set[int]] = {}
    for (i, j), d in edges.items():
        same_moment = tt[i] is not None and tt[j] is not None and abs(tt[i] - tt[j]) <= 2
        if d <= strict or same_moment or (deg[i] < crowd and deg[j] < crowd):
            adj.setdefault(i, set()).add(j)
            adj.setdefault(j, set()).add(i)
    done: set[int] = set()
    groups = []
    for c in sorted(adj, key=lambda n: (-len(adj[n]), n)):
        if c in done:
            continue
        g = [c] + sorted(n for n in adj[c] if n not in done)
        if len(g) > 1:
            groups.append(g)
            done.update(g)
    return groups


def _ts(s: str | None) -> float | None:
    try:
        return datetime.fromisoformat(s).timestamp() if s else None
    except ValueError:
        return None


RAW_EXT = {".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2", ".raf"}
# category -> (label, default decision); "largest" keeps only the biggest copy, "all" keeps every one
DUPE_CATEGORIES = {
    "copies":    ("Same moment, different file (re-encoded, resized or re-uploaded copy)", "largest"),
    "edit":      ("Original + Google edit", "all"),
    "raw":       ("RAW + JPEG of the same shot", "all"),
    "burst":     ("Burst / quick retakes (within 2 min)", "all"),
    "sameday":   ("Same day, different times (often a timezone-shifted copy)", "all"),
    "screens":   ("Screenshots that look alike", "all"),
    "otherdays": ("Different days (re-uploads, scans, or look-alikes)", "all"),
}


def _resized_copies(members: list[dict]) -> bool:
    """Different resolutions, same aspect ratio: a downscaled copy (WhatsApp, export), not a retake."""
    dims = {(m["width"], m["height"]) for m in members if m["width"] and m["height"]}
    if len(dims) < 2:
        return False
    ratios = [max(w, h) / min(w, h) for w, h in dims]
    return max(ratios) - min(ratios) < 0.02


def near_dupe_report(con, t: int = 5) -> list[dict]:
    """Every near-duplicate group with members, category and default keep decision."""
    rows = con.execute(
        "SELECT a.sha256, a.dhash, a.taken_at, a.ext, a.width, a.height, a.size,"
        " (SELECT dir || '/' || filename FROM occurrences o WHERE o.sha256=a.sha256 ORDER BY id LIMIT 1)"
        " FROM assets a WHERE a.dhash IS NOT NULL").fetchall()
    report = []
    for g in near_dupe_clusters([(r[0], r[1], r[2]) for r in rows], t):
        h0 = rows[g[0]][1] & U64
        members = [{"sha256": r[0], "distance": ((r[1] & U64) ^ h0).bit_count(), "taken_at": r[2],
                    "ext": r[3], "width": r[4], "height": r[5], "size": r[6], "path": r[7]}
                   for r in (rows[i] for i in g)]
        members.sort(key=lambda m: (-(m["width"] or 0) * (m["height"] or 0), -m["size"]))  # largest first
        names = [PurePosixPath(m["path"]).name.lower() for m in members]
        exts = {m["ext"] for m in members}
        times = [x for x in (_ts(m["taken_at"]) for m in members) if x is not None]
        span = max(times) - min(times) if len(times) > 1 else 0
        if any("-edited" in n for n in names):
            cat = "edit"
        elif exts & RAW_EXT and exts - RAW_EXT:
            cat = "raw"
        elif span <= 2 or (span <= 86400 and _resized_copies(members)):
            cat = "copies"
        elif exts == {".png"}:
            cat = "screens"
        elif span <= 120:
            cat = "burst"
        elif span <= 86400:
            cat = "sameday"
        else:
            cat = "otherdays"
        keep = [members[0]["sha256"]] if DUPE_CATEGORIES[cat][1] == "largest" else [m["sha256"] for m in members]
        report.append({"id": members[0]["sha256"][:16], "category": cat, "members": members,
                       "default_keep": keep, "span_seconds": span})
    order = list(DUPE_CATEGORIES)
    report.sort(key=lambda r: (order.index(r["category"]), r["members"][0]["taken_at"] or ""))
    return report


def cmd_dupes(a) -> None:
    con = connect(a.db)
    occ, uniq, seen = con.execute("SELECT count(*), count(DISTINCT o.sha256), coalesce(sum(a.size),0)"
                                  " FROM occurrences o JOIN assets a USING(sha256)").fetchone()
    stored = con.execute("SELECT coalesce(sum(size),0) FROM assets").fetchone()[0]
    print(f"EXACT: {occ} files in Takeout -> {uniq} unique assets "
          f"({seen / 1e6:.1f} MB seen, {stored / 1e6:.1f} MB stored, {(seen - stored) / 1e6:.1f} MB saved)")
    for sha, n, paths in con.execute(
            "SELECT sha256, count(*) c, group_concat(dir || '/' || filename, ' | ') FROM occurrences"
            " GROUP BY sha256 HAVING c > 1 ORDER BY c DESC LIMIT ?", (a.limit,)):
        print(f"  {sha[:12]} x{n}: {paths}")

    report = near_dupe_report(con, a.threshold)
    print(f"\nNEAR (dHash distance <= {a.threshold}): {len(report)} groups covering "
          f"{sum(len(r['members']) for r in report)} images. Review these; nothing is deleted.")
    for cat, (label, policy) in DUPE_CATEGORIES.items():
        rs = [r for r in report if r["category"] == cat]
        if not rs:
            continue
        extra = sum(sum(m["size"] for m in r["members"]) - r["members"][0]["size"] for r in rs)
        print(f"  {len(rs):6} groups {sum(len(r['members']) for r in rs):7} images "
              f"{extra / 1e9:6.1f} GB beyond the largest copy   {label} [default: keep {policy}]")
    for r in sorted(report, key=lambda r: -len(r["members"]))[: a.limit]:
        print(f"  group {r['id']} ({r['category']}):")
        for m in r["members"]:
            print(f"    d={m['distance']:<2} {m['width']}x{m['height']} {m['size'] / 1e3:7.0f} KB  {m['path']}")
    if a.json:
        Path(a.json).write_text(json.dumps(report, indent=1))
        print(f"wrote {a.json}")


# ----------------------------------------------------------------------------- review

SHA_RE = re.compile(r"^[0-9a-f]{64}$")
SHORT = {"copies": "Copies", "edit": "Edits", "raw": "RAW + JPEG", "burst": "Bursts",
         "sameday": "Same day", "screens": "Screenshots", "otherdays": "Different days"}


def _render_jpeg(src: str, dest: str, side: int) -> str:
    """Write a JPEG preview of src at most `side` px. Top-level so worker processes can run it."""
    try:
        with Image.open(src) as im:
            im.draft("RGB", (side, side))
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((side, side))
            tmp = f"{dest}.{os.getpid()}.tmp"
            im.save(tmp, "JPEG", quality=82)
            os.replace(tmp, dest)
    except Exception as e:  # undecodable file: leave no image; the page shows the alt text
        return f"{src}: {e}"
    return ""


def cmd_review(a) -> None:
    """Serve a local page for reviewing near-duplicates. Choices go to <out>/decisions.json."""
    import threading
    from concurrent.futures import ProcessPoolExecutor
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from multiprocessing import get_context

    store = Path(a.store)
    out = Path(a.out or Path(a.db).resolve().parent / "review")
    (out / "thumbs").mkdir(parents=True, exist_ok=True)
    (out / "previews").mkdir(parents=True, exist_ok=True)
    con = connect(a.db)
    report = near_dupe_report(con, a.threshold)
    ext = {m["sha256"]: m["ext"] for g in report for m in g["members"]}
    payload = json.dumps({"groups": report, "categories": {
        k: {"label": label, "policy": pol, "short": SHORT[k]} for k, (label, pol) in DUPE_CATEGORIES.items()}})
    dec_path = out / "decisions.json"
    lock = threading.Lock()
    page = Path(__file__).with_name("review.html").read_bytes()

    def image(sha: str, kind: str, side: int) -> Path | None:
        dest = out / kind / f"{sha}.jpg"
        if not dest.exists():
            _render_jpeg(str(blob_path(store, sha, ext[sha])), str(dest), side)
        return dest if dest.exists() else None

    def pregenerate():  # thumbnails for every group, in page order, using all cores
        todo = [(str(blob_path(store, sha, ext[sha])), str(out / "thumbs" / f"{sha}.jpg"), 360)
                for g in report for m in g["members"] if not (out / "thumbs" / f"{(sha := m['sha256'])}.jpg").exists()]
        if not todo:
            return
        t0 = time.time()
        with ProcessPoolExecutor(max(1, (os.cpu_count() or 2) - 2), mp_context=get_context("fork")) as ex:
            errors = [e for e in ex.map(_render_jpeg, *zip(*todo), chunksize=16) if e]
        print(f"thumbnails: {len(todo) - len(errors)} made in {time.time() - t0:.0f}s"
              + (f", {len(errors)} could not be decoded" if errors else ""), flush=True)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, code: int, body: bytes, ctype: str, cache: bool = False):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                return self.send(200, page, "text/html; charset=utf-8")
            if path == "/groups.json":
                return self.send(200, payload.encode(), "application/json")
            if path == "/decisions.json":
                with lock:
                    body = dec_path.read_bytes() if dec_path.exists() else b"{}"
                return self.send(200, body, "application/json")
            for prefix, kind, side in (("/thumb/", "thumbs", 360), ("/preview/", "previews", 1600)):
                if path.startswith(prefix):
                    sha = path[len(prefix):]
                    if SHA_RE.match(sha) and sha in ext and (f := image(sha, kind, side)):
                        return self.send(200, f.read_bytes(), "image/jpeg", cache=True)
                    return self.send(404, b"", "text/plain")
            self.send(404, b"not found", "text/plain")

        def do_POST(self):
            if self.path != "/decisions":
                return self.send(404, b"", "text/plain")
            try:
                incoming = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                with lock:
                    current = json.loads(dec_path.read_text()) if dec_path.exists() else {}
                    for gid, d in incoming.items():
                        if isinstance(d, dict) and isinstance(d.get("keep"), list) and d["keep"]:
                            current[gid] = {"keep": d["keep"], "reviewed": bool(d.get("reviewed")),
                                            "category": d.get("category"), "at": now()}
                    tmp = dec_path.with_suffix(".tmp")
                    tmp.write_text(json.dumps(current, indent=1))
                    os.replace(tmp, dec_path)
                self.send(200, b"{}", "application/json")
            except Exception as e:
                self.send(400, str(e).encode(), "text/plain")

    con.close()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"{len(report)} groups, {sum(len(g['members']) for g in report)} photos. "
          f"Open http://127.0.0.1:{a.port}/  (Ctrl-C to stop). Choices are saved to {dec_path}", flush=True)
    threading.Thread(target=pregenerate, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


# ----------------------------------------------------------------------------- tag

PROMPT = """You are cataloguing a personal photo archive. Describe this photo for search.
Reply with ONLY a JSON object, no prose:
{"caption": "<one factual sentence>",
 "tags": ["<5-15 lowercase keywords: subjects, objects, animals, activity, setting, landmark, season, weather, event>"],
 "scene": "indoor" | "outdoor" | "unknown",
 "people_count": <integer>,
 "text": "<any clearly legible text in the image, or empty string>"}"""


def thumbnail_b64(path: Path, max_side: int) -> str:
    with Image.open(path) as im:
        im.draft("RGB", (max_side, max_side))
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


def parse_json_loose(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)  # tolerate ```json fences / chatter
        if m:
            return json.loads(m.group(0))
        raise ValueError(f"no JSON in model reply: {text[:120]!r}")


def ask_vlm(endpoint: str, model: str, b64: str, json_mode: bool, timeout: float) -> dict:
    body = {
        "model": model,
        "temperature": 0.1,
        "max_tokens": 400,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}],
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(endpoint.rstrip("/") + "/chat/completions",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.load(r)
    return parse_json_loose(resp["choices"][0]["message"]["content"])


def cmd_tag(a) -> None:
    con, store = connect(a.db), Path(a.store)
    q = ("FROM assets WHERE kind='image' AND dhash IS NOT NULL"
         " AND sha256 NOT IN (SELECT sha256 FROM tags WHERE model=?)")
    remaining = con.execute(f"SELECT count(*) {q}", (a.model,)).fetchone()[0]
    rows = con.execute(f"SELECT sha256, ext {q} ORDER BY favorited DESC, taken_at DESC LIMIT ?",
                       (a.model, a.limit or -1)).fetchall()
    print(f"{remaining} images untagged by {a.model}; tagging {len(rows)} via {a.endpoint}")
    times, failures = [], 0
    for i, (sha, ext) in enumerate(rows, 1):
        t0 = time.time()
        try:
            res = ask_vlm(a.endpoint, a.model, thumbnail_b64(blob_path(store, sha, ext), a.max_side),
                          not a.no_json_mode, a.timeout)
        except Exception as e:
            failures += 1
            print(f"  ! {sha[:12]}: {e}")
            continue
        dt = time.time() - t0
        times.append(dt)
        caption = str(res.get("caption", "")).strip()
        raw_tags = res.get("tags") or []
        tags = sorted({str(t).strip().lower() for t in raw_tags if str(t).strip()}) if isinstance(raw_tags, list) else []
        con.execute("INSERT OR REPLACE INTO tags VALUES (?,?,?,?,?,?,?)",
                    (sha, a.model, caption, json.dumps(tags), json.dumps(res), dt, now()))
        reindex(con, [sha])
        con.commit()
        print(f"[{i}/{len(rows)}] {sha[:12]} {dt:5.1f}s  {caption[:70]!r}  [{', '.join(tags[:8])}]")
    if times:
        avg = sum(times) / len(times)
        left = remaining - len(times)
        print(f"avg {avg:.2f}s/image -> ~{avg * left / 3600:.1f} h for the {left} still untagged")
    if failures:
        print(f"{failures} failures")
        sys.exit(1 if not times else 0)


# ----------------------------------------------------------------------------- search / stats

def cmd_search(a) -> None:
    con = connect(a.db)
    rows = con.execute(
        "SELECT s.sha256, a.ext, a.taken_at, a.kind,"
        " (SELECT dir || '/' || filename FROM occurrences o WHERE o.sha256 = s.sha256 LIMIT 1),"
        " (SELECT caption FROM tags t WHERE t.sha256 = s.sha256 ORDER BY created_at DESC LIMIT 1)"
        " FROM search s JOIN assets a ON a.sha256 = s.sha256 WHERE search MATCH ? ORDER BY rank LIMIT ?",
        (a.query, a.limit)).fetchall()
    for sha, ext, taken, kind, path, caption in rows:
        print(f"{(taken or '?')[:10]}  {kind:5}  {path}")
        if caption:
            print(f"            {caption}")
        if a.store:
            print(f"            {blob_path(Path(a.store), sha, ext)}")
    print(f"{len(rows)} result(s)")


def cmd_stats(a) -> None:
    con = connect(a.db)
    one = lambda q: con.execute(q).fetchone()
    print("sources ingested:", one("SELECT count(*) FROM sources WHERE finished_at IS NOT NULL")[0])
    for kind, n, size in con.execute("SELECT kind, count(*), sum(size) FROM assets GROUP BY kind"):
        print(f"  {kind:6} {n:7} assets {size / 1e9:8.2f} GB")
    occ, linked = one("SELECT count(*), count(sidecar_id) FROM occurrences")
    print(f"occurrences: {occ} ({linked} linked to sidecars)")
    print("assets with date:", one("SELECT count(*) FROM assets WHERE taken_at IS NOT NULL")[0],
          "| with GPS:", one("SELECT count(*) FROM assets WHERE lat IS NOT NULL")[0],
          "| albums:", one("SELECT count(DISTINCT album) FROM occurrences WHERE album IS NOT NULL")[0])
    for model, n, avg in con.execute("SELECT model, count(*), avg(seconds) FROM tags GROUP BY model"):
        print(f"tagged by {model}: {n} (avg {avg:.2f}s)")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default="estate.db", help="SQLite catalog (keep it next to or inside the store)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("ingest", help="ingest Takeout zips or extracted folders")
    s.add_argument("sources", nargs="+")
    s.add_argument("--store", required=True, help="content-addressed store root (external disk / NAS)")
    s.add_argument("--force", action="store_true", help="re-scan sources already ingested")
    s.add_argument("--delete-after", action="store_true", help="delete each zip after it is ingested")
    s.set_defaults(fn=cmd_ingest)

    s = sub.add_parser("link", help="match sidecars and merge metadata")
    s.set_defaults(fn=cmd_link)

    s = sub.add_parser("exif", help="read all EXIF/XMP/QuickTime metadata with exiftool (resumable)")
    s.add_argument("--store", required=True)
    s.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) // 2))
    s.add_argument("--batch", type=int, default=200, help="files per exiftool call")
    s.add_argument("--limit", type=int, default=0, help="0 = all")
    s.add_argument("--force", action="store_true", help="re-read assets already done")
    s.set_defaults(fn=cmd_exif)

    s = sub.add_parser("dupes", help="exact + near duplicate report")
    s.add_argument("--threshold", type=int, default=5, help="max dHash Hamming distance")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--json", help="write near-dupe clusters to this file")
    s.set_defaults(fn=cmd_dupes)

    s = sub.add_parser("review", help="local web page to review near-duplicates (nothing is deleted)")
    s.add_argument("--store", required=True)
    s.add_argument("--threshold", type=int, default=5)
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--out", help="where thumbnails and decisions.json go (default: <db dir>/review)")
    s.set_defaults(fn=cmd_review)

    s = sub.add_parser("tag", help="caption/tag images with a local VLM")
    s.add_argument("--store", required=True)
    s.add_argument("--endpoint", default="http://127.0.0.1:8080/v1",
                   help="OpenAI-compatible base URL (llama-server :8080, Ollama :11434/v1)")
    s.add_argument("--model", required=True, help="model name; tags are stored per model")
    s.add_argument("--limit", type=int, default=0, help="0 = all")
    s.add_argument("--max-side", type=int, default=768)
    s.add_argument("--timeout", type=float, default=300)
    s.add_argument("--no-json-mode", action="store_true", help="don't send response_format")
    s.set_defaults(fn=cmd_tag)

    s = sub.add_parser("search", help="full-text search (FTS5 syntax: paris OR beach, \"new york\")")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--store", help="also print blob paths")
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("stats")
    s.set_defaults(fn=cmd_stats)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
