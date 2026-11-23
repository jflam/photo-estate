"""
catalog.py — what the photo app (`embed.py serve`) holds in memory: every photo's vector, facts and
facets, read from the catalog into NumPy arrays. The route modules (browse.py, images.py, admin.py)
and embed.py's own routes all answer from one `Catalog`.

  load(con, ...)      read the catalog (all of it, or only what changed since `prev`) -> Catalog
  Catalog             a dataclass: the arrays and per-photo dicts, every field typed, the same for every
                      member: browse_order, shapes, sims, periods' keys ...
  View                what one member sees of a Catalog (Catalog.view): the mask, and what is worked out from
                      it for that member (which photo of a stack is on top and how many it holds, the trip
                      cards and their covers, the counts in `info`, the favourites), with the questions every
                      route asks of them: combine, facet_counts, take, periods, feed_row, row ...
  PhotoMeta           one photo's facts (Catalog.meta[sha]): a TypedDict, so a plain dict on the wire. It has
                      no favourite: whose favourite it is depends on who asks (View.fav fills `fav` in a row)
  Catalog.scored()    (best, rank, score) when the catalog has scorers; ValueError when it has none
  STATE               the names of the fields load() works out (differences() compares them)
  Live                the Catalog being served, swapped for a new one after an ingest (install); a member's
                      View of it (visible_for), worked out once and kept until the catalog, or what anyone
                      hides, changes (relook); changed in place for a favourite (update); `generation`
                      counts all three
  Gate                requests share it; a swap takes it alone (not re-entrant: swap() inside it raises)

Every route asks Live.visible_for(member) what a member sees, and Live.shared_for(member) what a member may
ever be shown (everything until private photos, multiplayer phase 5): nothing reads a mask of its own.
A Catalog is never changed after it is built; a View only to show a favourite at once (View.show_favorites,
through Live.update), rather than waiting for the next one.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Mapping, TypedDict

import numpy as np
from numpy.typing import NDArray

from core import schema
from scores import best as best_score, best_first, load as load_scorers
from stacks import group as stack_group, seconds as stack_seconds, tops as stack_tops

# First names that are also everyday words: read as a person only when capitalized ("Sunny") or "Sunny's"
COMMON_WORDS = set("""sunny lily rose rosie daisy iris violet ivy holly heather hazel jasmine poppy ruby amber pearl
    jade crystal coral olive berry autumn summer winter april may june august dawn eve faith hope joy grace
    honey candy ginger pepper cookie buddy rocky max jack bill mark will pat sue bob rob rod guy ray art
    frank earl duke king prince queen bishop hunter chase page brook river sky storm rain snow forest
    glen dale cliff rocco sandy dusty misty penny chip bud bunny kitty angel star sterling destiny""".split())
# Countries and states that are also everyday words: read as a place only when capitalized
COMMON_PLACES = {"china", "turkey", "chad", "jordan", "georgia", "guinea", "japan", "chile", "cuba", "wales", "jersey"}

# Screenshots, told from what the catalog already holds, as the iPhone app does (ios/.../Screenshots.swift): a
# camera always writes its make and model and a screenshot never has one, so a screenshot is a photo with no
# camera that is exactly a phone or tablet screen's size (either way round), or whose name says it is one.
# Camera photos that lost their EXIF come back at 4:3 or 16:9, which are also iPad and old Android sizes; a
# real screenshot of those is a PNG, so for JPEG and HEIC those two shapes don't count.
SCREEN_SIZES = {
    (320, 480), (640, 960), (640, 1136), (750, 1334), (828, 1792), (1080, 1920), (1242, 2208), (1125, 2436),
    (1242, 2688), (1080, 2340), (1170, 2532), (1284, 2778), (1179, 2556), (1290, 2796), (1206, 2622),
    (1320, 2868), (1260, 2736),
    (768, 1024), (1536, 2048), (1668, 2224), (1668, 2388), (2048, 2732), (1620, 2160), (1640, 2360),
    (1488, 2266), (2064, 2752), (1668, 2420),
    (720, 1280), (1080, 2160), (1080, 2220), (1080, 2280), (1080, 2316), (1080, 2400), (1440, 2560),
    (1440, 2960), (1440, 3040), (1440, 3120), (1440, 3200),
}


def is_screenshot(m: Mapping[str, Any]) -> bool:
    if m.get("camera") or m.get("lens"):
        return False
    path = (m.get("path") or "").lower()
    if "screenshot" in path or "screen shot" in path:
        return True
    w, h = m.get("w") or 0, m.get("h") or 0
    a, b = min(w, h), max(w, h)
    return (a, b) in SCREEN_SIZES and ((m.get("ext") or "").lower() == ".png" or (a * 4 != b * 3 and a * 16 != b * 9))


def image_size(path: str):
    """(width, height) from a JPEG's or a WebP's header, or None. Never raises."""
    try:
        with open(path, "rb") as f:
            d = f.read(8192)
    except OSError:
        return None
    if d[:4] == b"RIFF" and d[8:12] == b"WEBP" and len(d) >= 30:
        kind = d[12:16]
        if kind == b"VP8 ":       # lossy: 14-bit sizes after the frame's start code
            return int.from_bytes(d[26:28], "little") & 0x3FFF, int.from_bytes(d[28:30], "little") & 0x3FFF
        if kind == b"VP8L":       # lossless: 14 bits each, less one, after a signature byte
            b = int.from_bytes(d[21:25], "little")
            return (b & 0x3FFF) + 1, ((b >> 14) & 0x3FFF) + 1
        if kind == b"VP8X":       # extended: 24 bits each, less one
            return int.from_bytes(d[24:27], "little") + 1, int.from_bytes(d[27:30], "little") + 1
        return None
    i = 2
    while i < len(d) - 9:
        if d[i] != 0xFF or d[i + 1] == 0xFF:
            i += 1
            continue
        if d[i + 1] in (0xC0, 0xC1, 0xC2):
            return int.from_bytes(d[i + 7:i + 9], "big"), int.from_bytes(d[i + 5:i + 7], "big")
        i += 2 + int.from_bytes(d[i + 2:i + 4], "big")
    return None


def chunks(xs, n=500):
    xs = list(xs)
    return (xs[i:i + n] for i in range(0, len(xs), n))


class PhotoFacts(TypedDict):
    """The keys every photo's dict has: read from assets and occurrences."""
    ext: str
    taken: str | None                    # ISO 8601, as the source said
    lat: float | None
    lon: float | None
    desc: str | None
    people: list[str]                    # tagged by the source, then the faces named on the People page
    albums: list[str]
    w: int | None
    h: int | None
    path: str | None                     # where it was first seen: dir/filename


class PhotoMeta(PhotoFacts, total=False):
    """What the catalog knows of one photo (Catalog.meta[sha]): a plain dict, sent as it is in search
    results and the sync feed. PhotoFacts' keys are always there; these only when their table has a row
    for the photo (exif: `estate.py exif`; places: `places.py run`), and `ar` once load() has read the
    thumbnail's shape (always, by the time a Catalog is built). Two classes rather than Required[...],
    so the required and optional keys are right at run time too (__required_keys__)."""
    ar: float                            # the thumbnail's width / height (upright)
    # videos (absent on a photo, so a photo's row is what it was before there were videos)
    kind: str                            # "video"
    dur: float | None                    # seconds, as exiftool read it
    live: str                            # on a still: the sha of its Live Photo clip (GET /video/<that>)
    # exif
    camera: str | None
    lens: str | None
    focal: float | None
    focal35: float | None
    fnumber: float | None
    exposure: float | None
    iso: int | None
    # places
    local: str | None                    # local time where it was taken
    tz: str | None
    place: str | None
    country: str | None
    region: str | None
    city: str | None
    county: str
    place_inferred: bool                 # placed by its event, not by its own GPS
    event: int | None
    event_title: str | None
    trip: int | None
    trip_title: str | None


class Event(TypedDict):
    title: str | None
    start: str | None
    end: str | None
    n: int
    trip: int | None


class Trip(TypedDict):
    title: str | None
    start: str | None
    end: str | None
    n: int


class TripCard(TypedDict):
    """A trip as /api/info lists it: the photos a member sees only, and its cover."""
    id: int
    title: str | None
    start: str | None
    end: str | None
    n: int
    cover: str


class Facet(TypedDict):
    """A facet with one value per photo: `codes[i]` indexes `labels` (-1: none)."""
    labels: list[str]
    ix: dict[str, int]
    codes: NDArray[np.int32]


class MultiFacet(TypedDict):
    """A facet a photo can have several values of: pair k says photo `rows[k]` has `labels[codes[k]]`."""
    labels: list[str]
    ix: dict[str, int]
    rows: NDArray[np.int64]
    codes: NDArray[np.int64]


def load(con: sqlite3.Connection, model: str, cache: Path, seed: Path, prev: dict | None = None,
         demo: bool = False) -> Catalog:
    """Everything the app keeps in memory, built from the catalog (`con`, closed when read). Startup runs it
    once; a finished ingest runs it again in the background and swaps the result in (Live.install), so new
    photos appear without a restart. `cache` and `seed` are the thumbnail caches (images.py), where the
    photos' real shapes are read.

    With `prev` (what the last load read, its `raw`) it reads only what an ingest can have changed:
    embeddings, assets and occurrences added since (by rowid), the photos those touch, and the small
    or derived tables (places, events, trips, named faces), applying only rows that differ. Photos it
    does not touch keep their per-photo dict, so the cost follows the new photos, not the library.
    Dicts are never changed in place: the old catalog keeps answering requests until it is swapped out."""

    def optional(q, args=()):  # tables filled by `estate.py exif` / `places.py run`; the page works without them
        try:
            return con.execute(q, args).fetchall()
        except sqlite3.OperationalError:
            return []

    def where_in(q, shas):     # q's rows for these photos (q ends in "sha256 IN")
        return [r for c in chunks(shas) for r in con.execute(f"{q} ({','.join('?' * len(c))})", c)]

    def read_meta(want) -> tuple[dict[str, PhotoMeta], dict[str, list[str]], dict[str, bool]]:
        """Per-photo dicts from assets, occurrences and exif, the people the source tagged, and whether the
        source said the photo is a favourite. `want` None: every photo and video, by scanning the tables in
        order (startup: on a hard disk, walking an index turns each row into a seek, ~18 s on the Mac mini);
        else just those photos, by key."""
        got: dict[str, PhotoMeta] = {}
        tagged: dict[str, list[str]] = {}
        said: dict[str, bool] = {}
        q_assets = "SELECT sha256, ext, taken_at, lat, lon, description, people, favorited, width, height, kind FROM assets"
        q_occ = "SELECT sha256, dir || '/' || filename, album FROM occurrences"
        q_exif = "SELECT sha256, camera, lens, focal, focal35, fnumber, exposure, iso FROM exif"
        films = []
        for sha, ext, taken, lat, lon, desc, people, fav, w, h, kind in (
                con.execute(q_assets) if want is None else where_in(q_assets + " WHERE sha256 IN", want)):
            if sha in index_of:
                tagged[sha] = [p.strip() for p in (people or "").split(",") if p.strip()]
                said[sha] = bool(fav)
                got[sha] = {"ext": ext, "taken": taken, "lat": lat, "lon": lon, "desc": desc,
                            "people": list(tagged[sha]), "albums": [], "w": w, "h": h, "path": None}
                if kind == "video":
                    got[sha]["kind"] = "video"
                    got[sha]["dur"] = None
                    films.append(sha)
        try:   # by key: a few thousand videos, never a scan of exif
            for sha, dur in where_in("SELECT sha256, duration FROM exif WHERE sha256 IN", films):
                got[sha]["dur"] = round(dur, 1) if dur else None
        except sqlite3.OperationalError:
            pass
        occ = con.execute(q_occ + " ORDER BY id") if want is None else sorted(
            where_in("SELECT id, sha256, dir || '/' || filename, album FROM occurrences WHERE sha256 IN", got))
        for *_, sha, path, album in occ:
            if (m := got.get(sha)) is not None:
                if m["path"] is None:  # the first place the photo was seen
                    m["path"] = path
                if album and album not in m["albums"]:
                    m["albums"].append(album)
        # exif is mostly exiftool's JSON dump (~700 MB for 120k photos); this index holds just the columns
        # read here, so startup scans ~15 MB instead. Built once, then kept up to date by SQLite.
        optional(schema.EXIF_APP_INDEX)
        try:
            ex = optional(q_exif) if want is None else where_in(q_exif + " WHERE sha256 IN", got)
        except sqlite3.OperationalError:
            ex = []
        for sha, camera, lens, focal, focal35, fnum, expo, iso in ex:
            if sha in got:
                got[sha].update(camera=camera, lens=lens, focal=focal, focal35=focal35, fnumber=fnum,
                                exposure=expo, iso=iso)
        return got, tagged, said

    q_emb = "SELECT rowid, sha256, vec FROM embeddings WHERE model=? AND vec IS NOT NULL"
    marks = {k: con.execute(q).fetchone()[0] or 0 for k, q in (
        ("emb", "SELECT max(rowid) FROM embeddings"), ("asset", "SELECT max(rowid) FROM assets"),
        ("occ", "SELECT max(id) FROM occurrences"))}
    # A Live Photo's clip is never a photo of its own: its still carries it (meta[still]["live"]). When a still
    # has several (Takeout's MP4 and iCloud's MOV of one clip), the .mp4 first: every browser plays it.
    pairs = sorted(optional("SELECT l.still, a.ext != '.mp4', l.clip, a.ext FROM live_pairs l JOIN assets a ON a.sha256 = l.clip"))
    clips: dict[str, str] = {clip: ext for _, _, clip, ext in pairs}
    live: dict[str, str] = {}
    for still, _, clip, _ in pairs:
        live.setdefault(still, clip)
    if prev is not None and clips.keys() & prev["index"].keys():
        prev = None    # a video the app was showing has been found to be a clip (its still came later): read everything again
    if prev is None:
        images = {r[0] for r in con.execute("SELECT sha256 FROM assets")} - clips.keys()
        rows = sorted((sha, vec) for rid, sha, vec in con.execute(q_emb + " AND rowid <= ?", (model, marks["emb"])))
        orphans = {sha: vec for sha, vec in rows if sha not in images}   # embedded on upload, not ingested yet
        rows = [r for r in rows if r[0] in images]
        if not rows:
            raise RuntimeError(f"no embeddings for {model}; run `embed.py run` first")
        shas = [r[0] for r in rows]
        index_of = {s: i for i, s in enumerate(shas)}
        M = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float16).reshape(len(shas), -1).astype(np.float32)
        del rows
        meta, tagged, said = read_meta(None)
        fresh = set(meta)
    else:
        old = prev["index"]
        vecs = dict(prev["orphans"])            # embeddings made before their photo was ingested
        vecs.update((sha, vec) for _, sha, vec in con.execute(q_emb + " AND rowid > ? AND rowid <= ?",
                                                              (model, prev["marks"]["emb"], marks["emb"])))
        touched = {r[0] for r in con.execute("SELECT sha256 FROM assets WHERE rowid > ? AND rowid <= ?",
                                             (prev["marks"]["asset"], marks["asset"]))}
        touched |= {r[0] for r in con.execute("SELECT sha256 FROM occurrences WHERE id > ? AND id <= ?",
                                              (prev["marks"]["occ"], marks["occ"]))}
        images = {r[0] for r in where_in("SELECT sha256 FROM assets WHERE sha256 IN", vecs.keys() | touched)} - clips.keys()
        orphans = {sha: v for sha, v in vecs.items() if sha not in images and sha not in old}
        added = sorted(sha for sha in vecs if sha in images and sha not in old)
        replaced = [sha for sha in vecs if sha in old]
        if added or replaced:
            shas = sorted(prev["shas"] + added)
            index_of = {s: i for i, s in enumerate(shas)}
            new = np.frombuffer(b"".join(vecs[s] for s in added + replaced), dtype=np.float16)
            new = new.reshape(len(added) + len(replaced), -1).astype(np.float32)
            M = np.concatenate([prev["M"], new[:len(added)]])
            M[[old[s] for s in replaced]] = new[len(added):]
            pos = {s: len(old) + i for i, s in enumerate(added)}
            M = M[[old[s] if s in old else pos[s] for s in shas]] if added else M
        else:
            shas, index_of, M = prev["shas"], old, prev["M"]
        fresh = {sha for sha in touched | set(added) if sha in index_of}
        got, tagged_new, said_new = read_meta(fresh)
        meta: dict[str, PhotoMeta] = dict(prev["meta"])
        meta.update(got)
        tagged = dict(prev["tagged"])
        tagged.update(tagged_new)
        said = dict(prev["said"])
        said.update(said_new)
    N, index = len(shas), index_of

    events: dict[int, Event] = {eid: {"title": t, "start": s0, "end": s1, "n": n, "trip": tid}
              for eid, t, s0, s1, n, tid in optional("SELECT id, title, start_local, end_local, n, trip_id FROM events")}
    trips: dict[int, Trip] = {tid: {"title": t, "start": s0, "end": s1, "n": n}
             for tid, t, s0, s1, n in optional("SELECT id, title, start_local, end_local, n FROM trips")}

    def own(sha) -> PhotoMeta:   # this load's own copy of a photo's dict, to change
        if sha not in fresh:
            m = meta[sha] = meta[sha].copy()
            m["people"] = list(m["people"])
            fresh.add(sha)
        return meta[sha]

    join = lambda *xs: ", ".join(dict.fromkeys(x for x in xs if x)) or None
    place_key = {}
    old_keys = prev["place_key"] if prev else {}
    for sha, local, tz, city, locality, admin2, admin1, country, psrc, eid in optional(
            "SELECT sha256, local_time, tz, city, locality, admin2, admin1, country, place_source, event_id"
            " FROM places"):
        if sha in meta:
            ev = events.get(eid) or {}
            tid = ev.get("trip")
            trip_title = trips[tid]["title"] if tid in trips else None
            key = place_key[sha] = (local, tz, city, locality, admin2, admin1, country, psrc, eid,
                                    ev.get("title"), ev.get("trip"), trip_title)
            if sha in fresh or old_keys.get(sha) != key:
                own(sha).update(local=local, tz=tz, place=join(city, admin1, country),
                                country=country or None, region=join(admin1, country) if admin1 else None,
                                city=join(city, admin1, country) if city else None,
                                county=" ".join(x for x in (locality, admin2) if x), place_inferred=psrc == "event",
                                event=eid, event_title=ev.get("title"), trip=ev.get("trip"), trip_title=trip_title)
    for sha in old_keys.keys() - place_key.keys():   # no longer placed (never, in practice)
        for k in ("local", "tz", "place", "country", "region", "city", "county", "place_inferred", "event",
                  "event_title", "trip", "trip_title"):
            own(sha).pop(k, None)
    # Faces named on the People page (faces.py) count as people in the photo, alongside Google's tags.
    face_names = {}
    for sha, name in optional("SELECT DISTINCT f.sha256, p.name FROM faces f JOIN persons p ON p.id=f.person_id"):
        if sha in meta:
            face_names.setdefault(sha, []).append(name)
    old_names = prev["face_names"] if prev else {}
    for sha in (face_names.keys() | old_names.keys()) if prev else face_names:
        if sha in fresh or face_names.get(sha) != old_names.get(sha):
            own(sha)["people"] = list(dict.fromkeys(tagged[sha] + face_names.get(sha, [])))
    has_faces = bool(optional("SELECT 1 FROM faces LIMIT 1"))
    old_live = prev["live"] if prev else {}
    for sha in (live.keys() | old_live.keys()) if prev else live:
        if sha in meta and (sha in fresh or live.get(sha) != old_live.get(sha)):
            m = own(sha)
            if sha in live:
                m["live"] = live[sha]
            else:
                m.pop("live", None)
    # The frames videos are searched by (embed.py): one matrix, each row owned by a video. Read whole at
    # startup; afterwards only when the table has grown (a video is written once, all its frames together).
    frames_mark = (optional("SELECT max(rowid) FROM video_frames") or [(0,)])[0][0] or 0
    if prev is not None and prev["frames"][0] == frames_mark:
        _, frame_shas, frame_t, F = prev["frames"]
    else:
        rows = optional("SELECT sha256, t, vec FROM video_frames WHERE model=? ORDER BY sha256, t", (model,))
        rows = [r for r in rows if len(r[2]) == M.shape[1] * 2]
        frame_shas, frame_t = [r[0] for r in rows], np.array([r[1] for r in rows], np.float32)
        F = np.frombuffer(b"".join(r[2] for r in rows), dtype=np.float16).reshape(len(rows), -1).astype(np.float32) \
            if rows else np.zeros((0, M.shape[1]), np.float32)
        del rows
    # What a photo's source said (assets.favorited) is a favourite of the photo's sharers only (the
    # multiplayer plan): read who they are for those photos, by key. A photo with no sharer yet (a catalog
    # `estate.py migrate` has not been run on) counts as the first sysadmin's (lenses.py).
    said_shas = [s for s in shas if said.get(s)]
    try:
        fav_sharers: dict[str, tuple[int, ...]] = {}
        for sha, mid in where_in("SELECT sha256, member_id FROM sharers WHERE sha256 IN", said_shas):
            fav_sharers[sha] = fav_sharers.get(sha, ()) + (mid,)
    except sqlite3.OperationalError:     # a catalog from before the sharers table
        fav_sharers = {}
    scorers = load_scorers(con, model)   # what "Best first" goes by (scores.py); none in a catalog without them
    con.close()
    raw = {"shas": shas, "index": index, "M": M, "meta": meta, "tagged": tagged, "said": said, "orphans": orphans,
           "marks": marks, "place_key": place_key, "face_names": face_names, "live": live,
           "frames": (frames_mark, frame_shas, frame_t, F)}
    # A frame whose video is not in this catalog (embedded, then found to be a clip) belongs to nothing.
    owned = np.array([s in index for s in frame_shas], bool)
    frame_of = np.array([index[s] for s, ok in zip(frame_shas, owned) if ok], np.int64)
    if not owned.all():
        F, frame_t = F[owned], frame_t[owned]

    # Thumbnails are upright; the catalog's width/height are not always. The grid needs the real shape.
    # Worked out for the photos this load read or changed, and for those that had no thumbnail last time.
    need = [s for s in shas if "ar" not in meta[s] or s in (prev or {}).get("no_dims", ())]
    dims_file = cache / "thumb-dims.json"
    dims = {}
    if need:
        try:
            dims = json.loads((dims_file if dims_file.exists() else seed / "thumb-dims.json").read_text())
        except (OSError, ValueError):
            pass
    missing = [s for s in need if s not in dims]
    if missing:
        def size_of(s):   # the WebP thumbnail, or the JPEG one made before it
            return next((d for f in (cache, seed) for e in (".webp", ".jpg") if (d := image_size(str(f / "thumbs" / f"{s}{e}")))), None)
        with ThreadPoolExecutor(16) as ex:
            for s, d in zip(missing, ex.map(size_of, missing)):
                if d and d[0] and d[1]:
                    dims[s] = d
        try:
            cache.mkdir(parents=True, exist_ok=True)
            dims_file.write_text(json.dumps(dims, separators=(",", ":")))
        except OSError:
            pass
    for s in need:
        m = own(s)
        tw, th = dims.get(s) or (m["w"] or 4, m["h"] or 3)
        m["ar"] = round(tw / th, 4)
    raw["no_dims"] = {s for s in need if s not in dims}
    del dims

    col = lambda f: np.array([f(meta[s]) for s in shas], dtype=object)
    when = col(lambda m: m.get("local") or m["taken"] or "")
    year = np.array([int(w[:4] or 0) for w in when])

    def month_abs(w: str) -> int:
        try:
            y, mo = int(w[:4]), int(w[5:7])
        except ValueError:
            return -1
        return y * 12 + mo - 1 if y > 1900 and 1 <= mo <= 12 else -1

    ym = np.array([month_abs(w) for w in when])
    m0 = int(ym[ym >= 0].min()) if (ym >= 0).any() else 0     # first month with a photo, as year*12+month-1
    nmonths = int(ym.max()) - m0 + 1 if (ym >= 0).any() else 1
    ym = np.where(ym >= 0, ym - m0, -1)

    cols = {  # lower-cased text per photo, for the typed `key:value` substring filters
        "place": col(lambda m: f"{m.get('place') or ''} {m.get('county') or ''}".lower()),
        "camera": col(lambda m: f"{m.get('camera') or ''} {m.get('lens') or ''}".lower()),
        "album": col(lambda m: " | ".join(m["albums"]).lower()),
        "person": col(lambda m: " | ".join(m["people"]).lower()),
    }
    event_of = col(lambda m: m.get("event"))
    trip_of = col(lambda m: m.get("trip"))
    said_fav = np.array([said.get(s, False) for s in shas], bool)
    shot = np.array([is_screenshot(meta[s]) for s in shas], bool)
    video = np.array([meta[s].get("kind") == "video" for s in shas], bool)
    ars = np.array([meta[s]["ar"] for s in shas], np.float32)   # pyright: ignore[reportTypedDictNotRequiredAccess]  (set above)
    oldest = np.argsort(when, kind="stable")
    oldest = np.concatenate([oldest[when[oldest] != ""], oldest[when[oldest] == ""]])   # undated last, either way
    newest = np.concatenate([oldest[when[oldest] != ""][::-1], oldest[when[oldest] == ""]])
    score = best_score(M, scorers)  # what "Best first" sorts by, larger is better; None with no scorers
    best = best_first(M, scorers)   # every photo, best first; None when the catalog has no scorers
    rank = None                     # each photo's place in that order (0 is the best)
    if best is not None:
        rank = np.empty(len(shas), np.int64)
        rank[best] = np.arange(len(shas))
    # When each photo was taken, as the stacks go by it (View: stacks are of the photos a member sees).
    # By the instant it was taken (`taken`), not the local time a view sorts by: two shots a second apart
    # must not come out hours apart because only one of them has a place, and so a time zone.
    secs = np.array([stack_seconds(meta[x]["taken"]) for x in shas])

    # Facets. `single`: one value per photo, choosing several means any of them. `multi`: a photo can
    # have several (albums: any of the chosen; people: all of the chosen, i.e. photos of them together).
    def coded(values) -> Facet:
        labels = sorted({v for v in values if v})
        ix = {v: i for i, v in enumerate(labels)}
        return {"labels": labels, "ix": ix, "codes": np.array([ix.get(v, -1) for v in values], np.int32)}

    single = {k: coded([meta[s].get(k) for s in shas]) for k in ("country", "region", "city", "camera", "lens")}
    single["trip"] = coded([str(t) if t is not None else None for t in trip_of])
    multi: dict[str, MultiFacet] = {}
    for key, part in (("album", "albums"), ("person", "people")):
        labels = sorted({v for s in shas for v in meta[s][part]})
        ix = {v: i for i, v in enumerate(labels)}
        multi[key] = {"labels": labels, "ix": ix,
                      "rows": np.array([i for i, s in enumerate(shas) for _ in meta[s][part]], np.int64),
                      "codes": np.array([ix[v] for s in shas for v in meta[s][part]], np.int64)}

    # Words in a typed search that name a person or a place (browse.py's read_words)
    people_ix = multi["person"]["ix"]
    firsts = {}
    for n in people_ix:
        firsts.setdefault(n.split()[0].lower(), []).append(n)
    who = {f: ns[0] for f, ns in firsts.items() if len(ns) == 1}
    who.update({n.lower(): n for n in people_ix})
    where, cities = {}, set()
    for k in ("city", "region", "country"):
        for label in single[k]["labels"]:
            name = label.split(",")[0].strip()
            where.setdefault(name.lower(), name)
            if k == "city":
                cities.add(name.lower())
    for k in [k for k in where if k in who]:   # a person first
        del where[k]
    try:
        lower_words = {w for w in Path("/usr/share/dict/words").read_text().split() if w.islower()}
    except OSError:
        lower_words = set()
    common_place = {k for k in where if " " not in k and (k in COMMON_WORDS or k in COMMON_PLACES
                                                          or (k in cities and k in lower_words))}

    def alt(keys):
        return "|".join(re.escape(k) for k in sorted(keys, key=len, reverse=True))
    word_re = re.compile(r"(?<![\w'’])(?:(?P<who>" + (alt(who) or "(?!)") + r")|(?P<where>" + (alt(where) or "(?!)") +
                         r"))(['’]s)?(?![\w'’])", re.I)
    return Catalog(
        shas=shas, N=N, index=index, M=M, meta=meta, events=events, trips=trips, has_faces=has_faces, when=when,
        year=year, ym=ym, m0=m0, nmonths=nmonths, cols=cols, event_of=event_of, trip_of=trip_of, said_fav=said_fav,
        fav_sharers=fav_sharers, shot=shot, ars=ars, oldest=oldest, newest=newest, single=single, multi=multi,
        people_ix=people_ix, who=who, where=where, cities=cities, common_place=common_place, word_re=word_re,
        video=video, clips=clips, live=live, F=F, frame_of=frame_of, frame_t=frame_t, best=best,
        rank=rank, secs=secs, score=score, raw=raw, model=model, demo=demo)


@dataclass(slots=True, eq=False, repr=False)
class Catalog:
    """The library as the app holds it in memory, as load() builds it: the same for every member. A photo
    is a place `i` in the catalog (0..N-1): `shas[i]` names it, row `i` of `M` is its vector, and every
    array below has one entry per photo in that order. The fields up to `score` are what load() works out
    (STATE names them, and differences() compares them); `raw` is what the next incremental load() starts
    from. `best`, `rank` and `score` are None in a catalog with no scorers (scores.py); the dicts and sets
    are empty when their optional table is (events, trips, faces). What a member sees of it (which photos,
    their stacks, trips, counts and favourites) is a View: Catalog.view, through Live.visible_for."""
    shas: list[str]                      # every photo in the catalog, sorted
    N: int                               # len(shas)
    index: dict[str, int]                # sha -> its place
    M: NDArray[np.float32]               # N x d: the search model's unit vectors
    meta: dict[str, PhotoMeta]
    events: dict[int, Event]
    trips: dict[int, Trip]
    has_faces: bool                      # the faces table has a row (the People page can load)
    when: NDArray[np.object_]            # local time if placed, else taken; "" when undated
    year: NDArray[np.int_]               # 0 when undated
    ym: NDArray[np.int_]                 # month as an index from m0; -1 when undated
    m0: int                              # the first month with a photo, as year*12+month-1
    nmonths: int
    cols: dict[str, NDArray[np.object_]]   # lower-cased text for the `key:value` filters: place, camera, album, person
    event_of: NDArray[np.object_]        # event id or None
    trip_of: NDArray[np.object_]         # trip id or None
    said_fav: NDArray[np.bool_]          # the photo's source said it is a favourite (assets.favorited)
    fav_sharers: dict[str, tuple[int, ...]]   # a photo of said_fav -> its sharers (members.id), whose favourite that is
    shot: NDArray[np.bool_]              # a screenshot
    ars: NDArray[np.float32]             # aspect ratio of the thumbnail
    oldest: NDArray[np.intp]             # every photo, oldest first, undated last
    newest: NDArray[np.intp]             # newest first, undated last
    single: dict[str, Facet]             # country, region, city, camera, lens, trip
    multi: dict[str, MultiFacet]         # album, person
    people_ix: dict[str, int]            # person name -> its code in multi["person"]
    who: dict[str, str]                  # a word in a typed search -> the person it names
    where: dict[str, str]                # a word -> the place it names
    cities: set[str]
    common_place: set[str]               # places that are also everyday words: only when capitalized
    word_re: re.Pattern[str]             # finds `who` and `where` words in a search
    video: NDArray[np.bool_]             # a video (never a Live Photo's clip: those are in `clips`, not in the catalog)
    clips: dict[str, str]                # a Live Photo clip's sha -> its extension (images.py serves it at /video/)
    live: dict[str, str]                 # a still's sha -> the sha of its Live Photo clip
    F: NDArray[np.float32]               # the videos' frame vectors, one row per frame kept (embed.py video_rows)
    frame_of: NDArray[np.int64]          # the place in the catalog of the video each row of F is from
    frame_t: NDArray[np.float32]         # seconds into its video
    best: NDArray[np.intp] | None        # every photo, best first; None with no scorers
    rank: NDArray[np.int64] | None       # each photo's place in `best` (0 is the best); None with no scorers
    secs: NDArray[np.float64]            # when it was taken, in seconds (nan: undated), as stacks.py groups by
    score: NDArray[np.float64] | None    # what "Best first" sorts by, larger is better; None with no scorers
    raw: dict[str, Any]                  # for the next incremental load()
    model: str
    demo: bool

    def view(self, member: int | None, hide, fav, shared=None) -> View:
        """What `member` sees: every photo `shared` lets them be shown (all, by default) that `hide` (a bool
        per photo) does not leave out, with `fav` (a bool per photo) their favourites. Its stacks, trip
        cards and counts are worked out here, over just those photos. Live.visible_for keeps it."""
        shared = np.ones(self.N, bool) if shared is None else shared
        visible = shared & ~np.asarray(hide, bool)
        # Near-identical shots taken together are one stack (stacks.py): the best one shows, the rest are behind
        # it. Over the photos this member sees, so a stack whose best shot they hid shows them the next best.
        # Photos only: a video is not another shot of the photo taken beside it.
        stack_of = stack_group(self.secs, self.M, visible & ~self.video)
        top, stack_n = stack_tops(stack_of, self.rank)
        # A stack is a Live Photo if any of its shots is: the clip a held stack plays is its best such shot's.
        stack_live: dict[int, str] = {}
        held: dict[int, int] = {}
        for sha, its_clip in self.live.items():
            if (i := self.index.get(sha)) is not None and stack_of[i] >= 0:
                g, r = int(stack_of[i]), (int(self.rank[i]) if self.rank is not None else i)
                if g not in held or r < held[g]:
                    held[g], stack_live[g] = r, its_clip
        v = View(cat=self, member=member, shared=shared, visible=visible, fav=np.asarray(fav, bool).copy(),
                 stack_of=stack_of, stack_n=stack_n, dup=~top, stack_live=stack_live, trip_list=[])
        v.trip_list = v.trip_cards()
        v.info = {"total": int(visible.sum()), "videos": int((visible & self.video).sum()),
                  "model": self.model, "m0": self.m0, "months": self.nmonths,
                  "trips": v.trip_list, "demo": bool(self.demo), "faces": self.has_faces, "best": self.best is not None,
                  **v.facet_counts({})}
        return v

    def scored(self) -> tuple[NDArray[np.intp], NDArray[np.int64], NDArray[np.float64]]:
        """(best, rank, score), for a catalog that has scorers; ValueError for one that has none (callers
        check `best is not None` first, and answer as if no order had been asked for)."""
        if self.best is None or self.rank is None or self.score is None:
            raise ValueError("this catalog has no scorers: it has no best-first order")
        return self.best, self.rank, self.score

    def sims(self, vec) -> NDArray[np.float32]:
        """How well each photo matches a unit vector (a query's, or another photo's): its cosine, and for a
        video the best of its frames or its mean, whichever is closer. A video is found by one moment in it."""
        s = self.M @ vec
        if len(self.frame_of):
            np.maximum.at(s, self.frame_of, self.F @ vec)
        return s

    def moment(self, sha: str, vec) -> float | None:
        """Seconds into a video where it best matches `vec`: where to start playing it for a search."""
        rows = np.flatnonzero(self.frame_of == self.index[sha]) if sha in self.index else []
        return round(float(self.frame_t[rows[np.argmax(self.F[rows] @ vec)]]), 1) if len(rows) else None

    def scope_of(self, kind: str, ident: int):
        got = (self.events if kind == "event" else self.trips).get(ident)
        return {"kind": kind, "id": ident, **got} if got else None

    def month_index(self, s: str, end: bool = False):
        """"2020" or "2020-07" -> a month as an index into the catalog's months (the first month of a year,
        or with `end` its last); None for anything else, a month outside 1-12 included."""
        m = re.fullmatch(r"(\d{4})(?:-(\d{1,2}))?", s or "")
        month = (int(m[2]) if m[2] else (12 if end else 1)) if m else 0
        if not m or not 1 <= month <= 12:
            return None
        return int(m[1]) * 12 + month - 1 - self.m0

    def person_mask(self, name: str):
        m = np.zeros(self.N, bool)
        m[self.multi["person"]["rows"][self.multi["person"]["codes"] == self.people_ix[name]]] = True
        return m

    def browse_order(self, mask, sort: str):
        """The photos `mask` selects with nothing to rank them by: by time, or the best first."""
        by = self.scored()[0] if sort == "best" else self.oldest if sort == "old" else self.newest
        ids = by[mask[by]]
        if sort == "best-month":   # the Library: months newest first as ever, the best first in each
            runs = np.cumsum(np.concatenate([[0], np.diff(self.ym[ids]) != 0]))
            ids = ids[np.lexsort((self.scored()[1][ids], runs))]
        return ids

    def shapes(self, order) -> bytes:
        """Every photo's shape, a byte each, in the order a list shows them: what the page needs to lay a
        month out before it has been told which photos are in it. The byte is the aspect ratio between
        0.5 and 2.6 (the grid shows nothing narrower or wider) in 255 steps; search.html reads it back."""
        return np.round((np.clip(self.ars[order], 0.5, 2.6) - 0.5) * (255 / 2.1)).astype(np.uint8).tobytes()

    def differences(self, want: Catalog) -> str:
        """What an incremental load got wrong, compared with a full one ('' if nothing)."""
        def same(x, y):
            if isinstance(x, np.ndarray) or isinstance(y, np.ndarray):
                return isinstance(x, np.ndarray) and isinstance(y, np.ndarray) and x.shape == y.shape and \
                    bool(np.all((x == y) | ((x != x) & (y != y))))
            if isinstance(x, dict) and isinstance(y, dict):
                return x.keys() == y.keys() and all(same(x[k], y[k]) for k in x)
            if isinstance(x, (list, tuple)) and isinstance(y, (list, tuple)):
                return len(x) == len(y) and all(same(p, q) for p, q in zip(x, y))
            if isinstance(x, re.Pattern):
                return x.pattern == y.pattern
            return x == y
        bad = [k for k in STATE if not same(getattr(self, k), getattr(want, k))]
        if "meta" in bad:
            bad += [f"meta[{s[:8]}]" for s in want.meta if not same(self.meta.get(s), want.meta[s])][:5]
        return ", ".join(bad)


@dataclass(slots=True, eq=False, repr=False)
class View:
    """What one member sees of a Catalog (Catalog.view builds it; Live.visible_for keeps one per member).
    Every array has one entry per photo of `cat`. `visible` is the mask every view starts from: `shared`
    (what the member may ever be shown) less what their filter lenses hide (lenses.py). The stacks, the
    trip cards and their covers, and the counts in `info` are worked out over `visible`; `fav` is the
    member's own favourites. A View is never changed, but by show_favorites (in the gate's write side)."""
    cat: Catalog
    member: int | None                   # members.id; None: nobody (a catalog with no members yet)
    shared: NDArray[np.bool_]            # what the member may ever be shown (lenses.shared_for)
    visible: NDArray[np.bool_]           # shared, less what the member's filters hide: what every view shows
    fav: NDArray[np.bool_]               # the member's favourites
    stack_of: NDArray[np.int64]          # the stack a photo is in (its earliest member's place); -1 on its own
    stack_n: NDArray[np.int64]           # how many photos its stack holds (1 on its own)
    dup: NDArray[np.bool_]               # behind another photo of its stack
    stack_live: dict[int, str]           # a stack with a Live Photo in it (named as stack_of does) -> the clip of its best such shot
    trip_list: list[TripCard]            # trips with a visible photo, newest first
    info: dict[str, Any] = field(default_factory=dict)   # what /api/info answers

    def trip_cards(self) -> list[TripCard]:
        c, visible, fav, rank = self.cat, self.visible, self.fav, self.cat.rank
        out: list[TripCard] = []
        for tid, t in sorted(c.trips.items(), key=lambda kv: kv[1]["start"] or "", reverse=True):
            ids = np.flatnonzero((c.trip_of == tid) & visible)
            if len(ids):
                ids = ids[np.argsort(c.when[ids], kind="stable")]
                favs = ids[fav[ids]]
                pick = favs if len(favs) else ids   # a favourite if there is one: the best of them, or the middle one
                cover = int(pick[np.argmin(rank[pick])]) if rank is not None else int(pick[len(pick) // 2])
                out.append({"id": tid, "title": t["title"], "start": t["start"], "end": t["end"],
                            "n": int(len(ids)), "cover": c.shas[cover]})
        return out

    def combine(self, masks: dict, skip: str | None = None):
        # Hidden photos are in no view, unless the hidden ones are what was asked for (`hidden=1`): then what
        # the member may be shown (`shared`), of which the mask picks those their filters hide.
        out = self.shared.copy() if "hidden" in masks else self.visible.copy()
        for k, m in masks.items():
            if k != skip:
                out &= m
        return out

    def facet_counts(self, masks: dict) -> dict:
        """Counts per facet value under the current filters; each facet ignores its own selection."""
        c = self.cat
        ranked = lambda labels, n: sorted(((labels[j], int(k)) for j, k in enumerate(n) if k),
                                          key=lambda x: (-x[1], x[0]))
        out = {}
        for k, f in c.single.items():
            base = self.combine(masks, skip=k)
            out[k] = ranked(f["labels"], np.bincount(f["codes"][base & (f["codes"] >= 0)],
                                                     minlength=len(f["labels"])))
        for k, f in c.multi.items():
            base = self.combine(masks, skip=None if k == "person" else k)
            out[k] = ranked(f["labels"], np.bincount(f["codes"][base[f["rows"]]], minlength=len(f["labels"])))
        base = self.combine(masks, skip="date")
        return {"facets": out, "months": np.bincount(c.ym[base & (c.ym >= 0)], minlength=c.nmonths).tolist(),
                "fav": int((self.combine(masks, skip="fav") & self.fav).sum()), "matching": int(self.combine(masks).sum())}

    def row(self, sha: str) -> dict:
        """A photo as a result list sends it: its facts, and `fav`, whether it is this member's favourite."""
        return dict(sha=sha, **self.cat.meta[sha], fav=bool(self.fav[self.cat.index[sha]]))

    def feed_extra(self, i: int) -> dict:
        """What a device needs to order and stack a photo as the app does, and cannot work out without the
        vectors: `dup` for a shot behind another in its stack, `stack_of` naming the stack (its earliest
        photo), and `best`, what "Best first" sorts by (larger is better). The score, not the photo's
        place in the order: a place changes for every photo whenever one arrives, a score does not."""
        out = {}
        if self.dup[i]:
            out["dup"] = True
        if self.stack_of[i] >= 0:
            out["stack_of"] = self.cat.shas[self.stack_of[i]]
        if self.cat.score is not None:
            out["best"] = round(float(self.cat.score[i]), 3)
        return out

    def feed_row(self, sha: str) -> dict:
        """A photo as the sync feed (sync.py) and /api/sync carry it: its search result plus feed_extra."""
        i = self.cat.index[sha]
        return dict(self.cat.meta[sha], fav=bool(self.fav[i]), **self.feed_extra(i))

    def cover_of(self, run, fallback=None) -> int:
        """The photo to put on the front of a set of photos (`run`: their places in the catalog): a
        favourite if it has one, a landscape one if not; among those, the best photo (scores.py), or with
        no scorers the one `fallback` ranks first (default: nearest the middle)."""
        rank, fav, ars = self.cat.rank, self.fav, self.cat.ars
        order = rank[run] if rank is not None else fallback if fallback is not None else np.abs(np.arange(len(run)) - len(run) // 2)
        score = fav[run] * 4_000_000 + ((ars[run] >= 1.2) & (ars[run] <= 1.9)) * 2_000_000 - order
        return int(run[int(np.argmax(score))])

    def album_cover(self, ids):
        """An album's front: chosen as a month's is, and with no scorers the earliest in the album's order."""
        run = np.array([self.cat.index[s] for s in ids if s in self.cat.index], np.int64)
        return self.cat.shas[self.cover_of(run, np.arange(len(run)))] if len(run) else None

    def periods(self, order) -> dict:
        """The photos in `order` (by time), as a card a year and a card a month, as the iPhone's Library
        has them: each with its key ("2019", "2019-07"), how many, where it starts in `order`, the photo on
        the front and its places. The front is a favourite if there is one, a landscape one if not; among
        those, the best photo (scores.py), or the one nearest the middle in a catalog with no scorers."""
        out = {}
        c = self.cat
        city, ym, m0 = c.single["city"], c.ym, c.m0
        for name, key_of, top in (("years", lambda m: m // 12, 3), ("months", lambda m: m, 2)):
            k = np.where(ym[order] >= 0, key_of(ym[order] + m0), -1)
            cuts = np.flatnonzero(np.diff(k)) + 1
            starts, ends = np.concatenate([[0], cuts]), np.concatenate([cuts, [len(order)]])
            cards = []
            for a0, b0 in zip(starts.tolist(), ends.tolist()):
                if not len(order) or k[a0] < 0:   # undated photos have no year or month
                    continue
                run = order[a0:b0]
                cover = self.cover_of(run)
                codes = city["codes"][run]
                codes = codes[codes >= 0]
                names = {}
                for code, n in zip(*np.unique(codes, return_counts=True)):
                    nm = city["labels"][code].split(",")[0].strip()
                    names[nm] = names.get(nm, 0) + int(n)
                v = int(k[a0])
                cards.append({"key": str(v) if name == "years" else f"{v // 12}-{v % 12 + 1:02d}", "n": b0 - a0, "at": a0,
                              "cover": c.shas[cover], "ar": round(float(c.ars[cover]), 4),
                              "places": [x for x, _ in sorted(names.items(), key=lambda kv: (-kv[1], kv[0]))[:top]]})
            out[name] = cards
        return out

    def take(self, order, start: int, limit: int, dedupe: bool):
        """One page from a ranked list, skipping copies of a shot just shown (Google + iCloud, bursts)."""
        c = self.cat
        M, shas, stack_n, stack_of = c.M, c.shas, self.stack_n, self.stack_of
        kept, hidden, pos = [], 0, start
        while pos < len(order) and len(kept) < limit:
            i = int(order[pos])
            pos += 1
            if dedupe and kept and not c.video[i] and float(np.max(M[kept[-8:]] @ M[i])) > 0.97:   # a video is no photo's copy
                hidden += 1
                continue
            kept.append(i)
        rows = [dict(self.row(shas[i]), **({"stack": int(stack_n[i]), "stack_of": shas[stack_of[i]]} if stack_n[i] > 1 else {}))
                for i in kept]
        for i, r in zip(kept, rows):        # a stack with a Live Photo in it: the clip holding it plays (its own, if it is one)
            if stack_n[i] > 1 and (clip := r.get("live") or self.stack_live.get(int(stack_of[i]))):
                r["stack_live"] = clip
        return rows, hidden, pos if pos < len(order) else None

    def show_favorites(self, pairs) -> None:
        """Show favourites set since this view was worked out: in the mask and the count."""
        for sha, on in pairs:
            i = self.cat.index.get(sha)
            if i is not None:
                self.fav[i] = on
        self.info["fav"] = int((self.combine({}) & self.fav).sum())


# What load() works out: a Catalog's fields but the bookkeeping (raw, model, demo).
STATE = tuple(f.name for f in fields(Catalog) if f.name not in ("raw", "model", "demo"))


class Gate:
    """Requests share it; a swap takes it alone. Writers wait first, so a stream of requests can't starve a reload.
    Not re-entrant: a thread inside it (as a reader or in a swap) that asks for a swap would wait for itself
    forever once a writer waits, so swap() raises instead. Lock order: the gate first, then any module's own
    lock (Browse.lru_lock, PhotoApp.fav_lock, Admin.st_lock, ...); never the gate while holding one."""
    def __init__(self):
        self.cv, self.readers, self.writing, self.waiting = threading.Condition(), 0, False, 0
        self.mine = threading.local()   # .n: how many times this thread is inside the gate

    def __enter__(self):
        with self.cv:
            while self.writing or self.waiting:
                self.cv.wait()
            self.readers += 1
        self.mine.n = getattr(self.mine, "n", 0) + 1

    def __exit__(self, *exc):
        self.mine.n -= 1
        with self.cv:
            self.readers -= 1
            self.cv.notify_all()

    def swap(self, fn):
        """fn() alone, with no request reading; returns what it returns."""
        if getattr(self.mine, "n", 0):
            raise RuntimeError("Gate.swap from inside the gate would wait for itself")
        with self.cv:
            self.waiting += 1
            while self.readers or self.writing:
                self.cv.wait()
            self.waiting -= 1
            self.writing = True
        self.mine.n = 1
        try:
            return fn()
        finally:
            self.mine.n = 0
            with self.cv:
                self.writing = False
                self.cv.notify_all()


# How a member sees the catalog: (shared, hide, fav) for them, a bool per photo each (lenses.Looks.masks).
Looks = Callable[[Catalog, "int | None"], "tuple[NDArray[np.bool_], NDArray[np.bool_], NDArray[np.bool_]]"]


def everything(cat: Catalog, member: int | None):
    """The Looks of a catalog nobody curates: every photo shared, none hidden, the source's favourites."""
    return np.ones(cat.N, bool), np.zeros(cat.N, bool), cat.said_fav


class Live:
    """The catalog the app is serving (`cat`), shared by every route module, and what each member sees of it.
    A request reads `cat` inside the gate (photo_app.py's handle() holds it for every request but slow ones), so
    it sees one catalog from start to finish; install() swaps a new one in, relook() takes in a change to what
    members hide (lenses.py), and update() changes a member's View in place (a favourite), each only while no
    request is being served. `generation` counts those changes: anything worked out from the catalog (Browse's
    cache) belongs to the generation it was made in, and is never used in a later one.

    visible_for(member) is the one way any route asks what a member sees: that member's View, worked out from
    `looks` the first time it is asked for and kept until the catalog or the looks change. shared_for(member)
    is what they may ever be shown (the pages that show hidden photos, and the pixel routes, go by it)."""

    def __init__(self, cat: Catalog, looks: Looks | None = None):
        self.cat, self.gate, self.generation = cat, Gate(), 0
        self.looks: Looks = looks or everything
        self.views: dict[int | None, View] = {}     # member -> their View of `cat`, from `looks`
        self.shares: dict[int | None, Any] = {}     # member -> what they may be shown, when no View is kept yet
        self.views_lock = threading.Lock()          # one View worked out at a time (taken inside the gate)

    def visible_for(self, member: int | None) -> View:
        """What `member` (members.id, or None when there are no members) sees: worked out once per catalog and
        looks. A View worked out while they were being replaced (by a caller outside the gate) is not kept."""
        with self.views_lock:
            v = self.views.get(member)
            if v is not None and v.cat is self.cat:
                return v
            cat, looks = self.cat, self.looks
        shared, hide, fav = looks(cat, member)
        v = cat.view(member, hide, fav, shared)
        with self.views_lock:
            if self.cat is cat and self.looks is looks:
                v = self.views.setdefault(member, v)
            return v

    def shared_for(self, member: int | None):
        """What `member` may ever be shown, a bool per photo: everything, until private photos (phase 5). Asked
        of the looks (lenses.State.shared) without working a whole View out, for the pixel routes."""
        with self.views_lock:
            if (v := self.views.get(member)) is not None and v.cat is self.cat:
                return v.shared
            got = self.shares.get(member)
            if got is not None and got[0] is self.cat and got[1] is self.looks:
                return got[2]
            cat, looks = self.cat, self.looks
        fn = getattr(looks, "shared", None)
        shared = fn(cat, member) if fn else looks(cat, member)[0]
        with self.views_lock:
            if self.cat is cat and self.looks is looks:
                self.shares[member] = (cat, looks, shared)
        return shared

    def forget_views(self) -> None:
        with self.views_lock:
            self.views.clear()
            self.shares.clear()

    def install(self, cat: Catalog, then=None) -> None:
        """Serve `cat` from now on; then(cat) runs in the same swap, before any request sees it."""
        def swap_in():
            with self.views_lock:
                self.cat = cat
            self.forget_views()
            self.generation += 1
            if then:
                then(cat)
        self.gate.swap(swap_in)

    def relook(self, looks: Looks | None = None) -> None:
        """What members hide has changed (lenses.py): every View is worked out again, from `looks` if given."""
        def swap_in():
            if looks is not None:
                with self.views_lock:
                    self.looks = looks
            self.forget_views()
            self.generation += 1
        self.gate.swap(swap_in)

    def update(self, fn):
        """fn(cat) on the catalog being served, with no request reading it (a favourite); returns its result."""
        def change():
            self.generation += 1
            return fn(self.cat)
        return self.gate.swap(change)
