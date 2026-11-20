"""
catalog.py — what the photo app (`embed.py serve`) holds in memory: every photo's vector, facts and
facets, read from the catalog into NumPy arrays. The route modules (browse.py, images.py, admin.py)
and embed.py's own routes all answer from one `Catalog`.

  load(con, ...)      read the catalog (all of it, or only what changed since `prev`) -> Catalog
  Catalog             the arrays and per-photo dicts (see STATE), with the questions every route asks
                      of them: combine, facet_counts, take, periods, browse_order, cover_of, ...
  Live                the Catalog being served, swapped for a new one after an ingest (install)
  Gate                requests share it; a swap takes it alone

A Catalog is never changed after it is built, with one exception: a favourite set in the app is shown
in the installed one at once (embed.py's set_favorites), rather than waiting for a reload.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from changes import hidden_shas
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


def is_screenshot(m: dict) -> bool:
    if m.get("camera") or m.get("lens"):
        return False
    path = (m.get("path") or "").lower()
    if "screenshot" in path or "screen shot" in path:
        return True
    w, h = m.get("w") or 0, m.get("h") or 0
    a, b = min(w, h), max(w, h)
    return (a, b) in SCREEN_SIZES and ((m.get("ext") or "").lower() == ".png" or (a * 4 != b * 3 and a * 16 != b * 9))


def jpeg_size(path: str):
    """(width, height) from a JPEG's header, or None. Never raises."""
    try:
        with open(path, "rb") as f:
            d = f.read(8192)
        i = 2
        while i < len(d) - 9:
            if d[i] != 0xFF or d[i + 1] == 0xFF:
                i += 1
                continue
            if d[i + 1] in (0xC0, 0xC1, 0xC2):
                return int.from_bytes(d[i + 7:i + 9], "big"), int.from_bytes(d[i + 5:i + 7], "big")
            i += 2 + int.from_bytes(d[i + 2:i + 4], "big")
    except OSError:
        pass
    return None


def chunks(xs, n=500):
    xs = list(xs)
    return (xs[i:i + n] for i in range(0, len(xs), n))


# The names load() produces: a Catalog's attributes.
STATE = ('shas', 'N', 'index', 'M', 'meta', 'events', 'trips', 'has_faces', 'when', 'year', 'ym', 'm0', 'nmonths',
         'cols', 'event_of', 'trip_of', 'fav', 'shot', 'ars', 'oldest', 'newest', 'single', 'multi', 'trip_list',
         'people_ix', 'who', 'where', 'cities', 'common_place', 'word_re', 'visible', 'best', 'rank', 'stack_of',
         'stack_n', 'dup', 'score')


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

    def read_meta(want):
        """Per-photo dicts from assets, occurrences and exif. `want` None: every image, by scanning
        the tables in order (startup: on a hard disk, walking an index turns each row into a seek,
        ~18 s on the Mac mini); else just those photos, by key."""
        got, tagged = {}, {}
        q_assets = "SELECT sha256, ext, taken_at, lat, lon, description, people, favorited, width, height FROM assets"
        q_occ = "SELECT sha256, dir || '/' || filename, album FROM occurrences"
        q_exif = "SELECT sha256, camera, lens, focal, focal35, fnumber, exposure, iso FROM exif"
        for sha, ext, taken, lat, lon, desc, people, fav, w, h in (
                con.execute(q_assets + " WHERE kind='image'") if want is None else where_in(q_assets + " WHERE sha256 IN", want)):
            if sha in index_of:
                tagged[sha] = [p.strip() for p in (people or "").split(",") if p.strip()]
                got[sha] = {"ext": ext, "taken": taken, "lat": lat, "lon": lon, "desc": desc,
                            "people": list(tagged[sha]), "albums": [], "fav": bool(fav), "w": w, "h": h, "path": None}
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
        return got, tagged

    q_emb = "SELECT rowid, sha256, vec FROM embeddings WHERE model=? AND vec IS NOT NULL"
    marks = {k: con.execute(q).fetchone()[0] or 0 for k, q in (
        ("emb", "SELECT max(rowid) FROM embeddings"), ("asset", "SELECT max(rowid) FROM assets"),
        ("occ", "SELECT max(id) FROM occurrences"))}
    if prev is None:
        images = {r[0] for r in con.execute("SELECT sha256 FROM assets WHERE kind='image'")}
        rows = sorted((sha, vec) for rid, sha, vec in con.execute(q_emb + " AND rowid <= ?", (model, marks["emb"])))
        orphans = {sha: vec for sha, vec in rows if sha not in images}   # embedded on upload, not ingested yet
        rows = [r for r in rows if r[0] in images]
        if not rows:
            raise RuntimeError(f"no embeddings for {model}; run `embed.py run` first")
        shas = [r[0] for r in rows]
        index_of = {s: i for i, s in enumerate(shas)}
        M = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float16).reshape(len(shas), -1).astype(np.float32)
        del rows
        meta, tagged = read_meta(None)
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
        images = {r[0] for r in where_in("SELECT sha256 FROM assets WHERE kind='image' AND sha256 IN", vecs.keys() | touched)}
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
        got, tagged_new = read_meta(fresh)
        meta = dict(prev["meta"])
        meta.update(got)
        tagged = dict(prev["tagged"])
        tagged.update(tagged_new)
    N, index = len(shas), index_of

    events = {eid: {"title": t, "start": s0, "end": s1, "n": n, "trip": tid}
              for eid, t, s0, s1, n, tid in optional("SELECT id, title, start_local, end_local, n, trip_id FROM events")}
    trips = {tid: {"title": t, "start": s0, "end": s1, "n": n}
             for tid, t, s0, s1, n in optional("SELECT id, title, start_local, end_local, n FROM trips")}

    def own(sha):              # this load's own copy of a photo's dict, to change
        if sha not in fresh:
            meta[sha] = dict(meta[sha], people=list(meta[sha]["people"]))
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
            trip_title = (trips.get(ev.get("trip")) or {}).get("title")
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
    # A favourite set in the app (embed.py's set_favorite) takes the place of what the photo's source said.
    for sha, on in optional("SELECT sha256, fav FROM favorites"):
        if sha in meta and meta[sha]["fav"] != bool(on):
            own(sha)["fav"] = bool(on)
    hidden = hidden_shas(con)   # photos a change set hides (changes.py): in the catalog, in no view
    scorers = load_scorers(con, model)   # what "Best first" goes by (scores.py); none in a catalog without them
    con.close()
    raw = {"shas": shas, "index": index, "M": M, "meta": meta, "tagged": tagged, "orphans": orphans,
           "marks": marks, "place_key": place_key, "face_names": face_names}

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
        def size_of(s):
            return jpeg_size(str(cache / "thumbs" / f"{s}.jpg")) or jpeg_size(str(seed / "thumbs" / f"{s}.jpg"))
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
    fav = np.array([meta[s]["fav"] for s in shas])
    shot = np.array([is_screenshot(meta[s]) for s in shas], bool)
    visible = np.array([s not in hidden for s in shas], bool)
    ars = np.array([meta[s]["ar"] for s in shas], np.float32)
    oldest = np.argsort(when, kind="stable")
    oldest = np.concatenate([oldest[when[oldest] != ""], oldest[when[oldest] == ""]])   # undated last, either way
    newest = np.concatenate([oldest[when[oldest] != ""][::-1], oldest[when[oldest] == ""]])
    score = best_score(M, scorers)  # what "Best first" sorts by, larger is better; None with no scorers
    best = best_first(M, scorers)   # every photo, best first; None when the catalog has no scorers
    rank = None                     # each photo's place in that order (0 is the best)
    if best is not None:
        rank = np.empty(len(shas), np.int64)
        rank[best] = np.arange(len(shas))
    # Near-identical shots taken together are one stack (stacks.py): the best one shows, the rest are behind it.
    # By the instant it was taken (`taken`), not the local time a view sorts by: two shots a second apart
    # must not come out hours apart because only one of them has a place, and so a time zone.
    stack_of = stack_group(np.array([stack_seconds(meta[x]["taken"]) for x in shas]), M, visible)
    top, stack_n = stack_tops(stack_of, rank)
    dup = ~top                      # behind another photo of its stack

    # Facets. `single`: one value per photo, choosing several means any of them. `multi`: a photo can
    # have several (albums: any of the chosen; people: all of the chosen, i.e. photos of them together).
    def coded(values):
        labels = sorted({v for v in values if v})
        ix = {v: i for i, v in enumerate(labels)}
        return {"labels": labels, "ix": ix, "codes": np.array([ix.get(v, -1) for v in values], np.int32)}

    single = {k: coded([meta[s].get(k) for s in shas]) for k in ("country", "region", "city", "camera", "lens")}
    single["trip"] = coded([str(t) if t is not None else None for t in trip_of])
    multi = {}
    for key, field in (("album", "albums"), ("person", "people")):
        labels = sorted({v for s in shas for v in meta[s][field]})
        ix = {v: i for i, v in enumerate(labels)}
        multi[key] = {"labels": labels, "ix": ix,
                      "rows": np.array([i for i, s in enumerate(shas) for _ in meta[s][field]], np.int64),
                      "codes": np.array([ix[v] for s in shas for v in meta[s][field]], np.int64)}

    trip_list = []
    for tid, t in sorted(trips.items(), key=lambda kv: kv[1]["start"] or "", reverse=True):
        ids = np.flatnonzero((trip_of == tid) & visible)
        if len(ids):
            ids = ids[np.argsort(when[ids], kind="stable")]
            favs = ids[fav[ids]]
            pick = favs if len(favs) else ids   # a favourite if there is one: the best of them, or the middle one
            cover = int(pick[np.argmin(rank[pick])]) if rank is not None else int(pick[len(pick) // 2])
            trip_list.append({"id": tid, "title": t["title"], "start": t["start"], "end": t["end"],
                              "n": int(len(ids)), "cover": shas[cover]})
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
    return Catalog({k: v for k, v in locals().items() if k in STATE}, raw, model, demo)


class Catalog:
    """The library as the app holds it in memory. Its attributes are the names in STATE; `raw` is what
    the next incremental load() starts from, and `info` is what /api/info answers."""

    def __init__(self, state: dict, raw: dict, model: str, demo: bool):
        self.__dict__.update(state)
        self.raw = raw
        self.info = {"total": int(self.visible.sum()), "model": model, "m0": self.m0, "months": self.nmonths,
                     "trips": self.trip_list, "demo": bool(demo), "faces": self.has_faces, "best": self.best is not None,
                     **self.facet_counts({})}

    def combine(self, masks: dict, skip: str | None = None):
        # Hidden photos are in no view, unless the hidden ones are what was asked for (`hidden=1`).
        out = np.ones(self.N, bool) if "hidden" in masks else self.visible.copy()
        for k, m in masks.items():
            if k != skip:
                out &= m
        return out

    def facet_counts(self, masks: dict) -> dict:
        """Counts per facet value under the current filters; each facet ignores its own selection."""
        ranked = lambda labels, c: sorted(((labels[j], int(n)) for j, n in enumerate(c) if n),
                                          key=lambda x: (-x[1], x[0]))
        out = {}
        for k, f in self.single.items():
            base = self.combine(masks, skip=k)
            out[k] = ranked(f["labels"], np.bincount(f["codes"][base & (f["codes"] >= 0)],
                                                     minlength=len(f["labels"])))
        for k, f in self.multi.items():
            base = self.combine(masks, skip=None if k == "person" else k)
            out[k] = ranked(f["labels"], np.bincount(f["codes"][base[f["rows"]]], minlength=len(f["labels"])))
        base = self.combine(masks, skip="date")
        return {"facets": out, "months": np.bincount(self.ym[base & (self.ym >= 0)], minlength=self.nmonths).tolist(),
                "fav": int((self.combine(masks, skip="fav") & self.fav).sum()), "matching": int(self.combine(masks).sum())}

    def scope_of(self, kind: str, ident: int):
        got = (self.events if kind == "event" else self.trips).get(ident)
        return {"kind": kind, "id": ident, **got} if got else None

    def month_index(self, s: str, end: bool = False):
        """"2020" or "2020-07" -> a month as an index into the catalog's months (the first month of a year,
        or with `end` its last); None for anything else, a month outside 1-12 included."""
        m = re.fullmatch(r"(\d{4})(?:-(\d{1,2}))?", s or "")
        month = (int(m[2]) if m[2] else (12 if end else 1)) if m else 0
        if not 1 <= month <= 12:
            return None
        return int(m[1]) * 12 + month - 1 - self.m0

    def person_mask(self, name: str):
        m = np.zeros(self.N, bool)
        m[self.multi["person"]["rows"][self.multi["person"]["codes"] == self.people_ix[name]]] = True
        return m

    def feed_extra(self, i: int) -> dict:
        """What a device needs to order and stack a photo as the app does, and cannot work out without the
        vectors: `dup` for a shot behind another in its stack, `stack_of` naming the stack (its earliest
        photo), and `best`, what "Best first" sorts by (larger is better). The score, not the photo's
        place in the order: a place changes for every photo whenever one arrives, a score does not."""
        out = {}
        if self.dup[i]:
            out["dup"] = True
        if self.stack_of[i] >= 0:
            out["stack_of"] = self.shas[self.stack_of[i]]
        if self.score is not None:
            out["best"] = round(float(self.score[i]), 3)
        return out

    def feed_row(self, sha: str) -> dict:
        """A photo as the sync feed (sync.py) and /api/sync carry it: its search result plus feed_extra."""
        return dict(self.meta[sha], **self.feed_extra(self.index[sha]))

    def cover_of(self, run, fallback=None) -> int:
        """The photo to put on the front of a set of photos (`run`: their places in the catalog): a
        favourite if it has one, a landscape one if not; among those, the best photo (scores.py), or with
        no scorers the one `fallback` ranks first (default: nearest the middle)."""
        rank, fav, ars = self.rank, self.fav, self.ars
        order = rank[run] if rank is not None else fallback if fallback is not None else np.abs(np.arange(len(run)) - len(run) // 2)
        score = fav[run] * 4_000_000 + ((ars[run] >= 1.2) & (ars[run] <= 1.9)) * 2_000_000 - order
        return int(run[int(np.argmax(score))])

    def album_cover(self, ids):
        """An album's front: chosen as a month's is, and with no scorers the earliest in the album's order."""
        run = np.array([self.index[s] for s in ids if s in self.index], np.int64)
        return self.shas[self.cover_of(run, np.arange(len(run)))] if len(run) else None

    def periods(self, order) -> dict:
        """The photos in `order` (by time), as a card a year and a card a month, as the iPhone's Library
        has them: each with its key ("2019", "2019-07"), how many, where it starts in `order`, the photo on
        the front and its places. The front is a favourite if there is one, a landscape one if not; among
        those, the best photo (scores.py), or the one nearest the middle in a catalog with no scorers."""
        out = {}
        city, ym, m0 = self.single["city"], self.ym, self.m0
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
                for c, n in zip(*np.unique(codes, return_counts=True)):
                    nm = city["labels"][c].split(",")[0].strip()
                    names[nm] = names.get(nm, 0) + int(n)
                v = int(k[a0])
                cards.append({"key": str(v) if name == "years" else f"{v // 12}-{v % 12 + 1:02d}", "n": b0 - a0, "at": a0,
                              "cover": self.shas[cover], "ar": round(float(self.ars[cover]), 4),
                              "places": [x for x, _ in sorted(names.items(), key=lambda kv: (-kv[1], kv[0]))[:top]]})
            out[name] = cards
        return out

    def browse_order(self, mask, sort: str):
        """The photos `mask` selects with nothing to rank them by: by time, or the best first."""
        by = self.best if sort == "best" else self.oldest if sort == "old" else self.newest
        ids = by[mask[by]]
        if sort == "best-month":   # the Library: months newest first as ever, the best first in each
            runs = np.cumsum(np.concatenate([[0], np.diff(self.ym[ids]) != 0]))
            ids = ids[np.lexsort((self.rank[ids], runs))]
        return ids

    def shapes(self, order) -> bytes:
        """Every photo's shape, a byte each, in the order a list shows them: what the page needs to lay a
        month out before it has been told which photos are in it. The byte is the aspect ratio between
        0.5 and 2.6 (the grid shows nothing narrower or wider) in 255 steps; search.html reads it back."""
        return np.round((np.clip(self.ars[order], 0.5, 2.6) - 0.5) * (255 / 2.1)).astype(np.uint8).tobytes()

    def take(self, order, start: int, limit: int, dedupe: bool):
        """One page from a ranked list, skipping copies of a shot just shown (Google + iCloud, bursts)."""
        M, shas, meta, stack_n, stack_of = self.M, self.shas, self.meta, self.stack_n, self.stack_of
        kept, hidden, pos = [], 0, start
        while pos < len(order) and len(kept) < limit:
            i = int(order[pos])
            pos += 1
            if dedupe and kept and float(np.max(M[kept[-8:]] @ M[i])) > 0.97:
                hidden += 1
                continue
            kept.append(i)
        rows = [dict(sha=shas[i], **meta[shas[i]], **({"stack": int(stack_n[i]), "stack_of": shas[stack_of[i]]} if stack_n[i] > 1 else {})) for i in kept]
        return rows, hidden, pos if pos < len(order) else None

    def show_favorites(self, pairs) -> None:
        """Show favourites set since this catalog was read: in the photo's row, the mask and the count."""
        for sha, on in pairs:
            i = self.index.get(sha)
            if i is not None and bool(self.fav[i]) != on:
                self.meta[sha] = dict(self.meta[sha], fav=on)
                self.fav[i] = on
        self.info["fav"] = int((self.combine({}) & self.fav).sum())

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


class Gate:
    """Requests share it; a swap takes it alone. Writers wait first, so a stream of requests can't starve a reload."""
    def __init__(self):
        self.cv, self.readers, self.writing, self.waiting = threading.Condition(), 0, False, 0

    def __enter__(self):
        with self.cv:
            while self.writing or self.waiting:
                self.cv.wait()
            self.readers += 1

    def __exit__(self, *exc):
        with self.cv:
            self.readers -= 1
            self.cv.notify_all()

    def swap(self, fn):
        with self.cv:
            self.waiting += 1
            while self.readers or self.writing:
                self.cv.wait()
            self.waiting -= 1
            self.writing = True
        try:
            fn()
        finally:
            with self.cv:
                self.writing = False
                self.cv.notify_all()


class Live:
    """The catalog the app is serving (`cat`), shared by every route module. A request reads `cat` inside
    the gate (embed.py's Handler holds it for every request but slow ones), so it sees one catalog from
    start to finish; install() swaps a new one in only while no request is being served."""

    def __init__(self, cat: Catalog):
        self.cat, self.gate = cat, Gate()

    def install(self, cat: Catalog, then=None) -> None:
        def swap_in():
            self.cat = cat
            if then:
                then(cat)
        self.gate.swap(swap_in)
