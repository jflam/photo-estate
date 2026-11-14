"""
stats.py: the numbers behind the photo app's /stats page. Plain functions, no model or web code, so
`embed.py serve` imports it lazily and it can be run on its own against a catalog:

    uv run stats.py --db estate.db --store STORE      # prints the same JSON the page uses

Two kinds of number. The *library* ones (years, cameras, places, people, when in the day) come from
the photo app's in-memory metadata and cost nothing. The *disk* ones (database tables, cache folders,
free space) read the catalog and walk directories, which can take seconds on a hard disk, so the
server runs `disk_stats` in a background thread and caches the answer.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import time
from collections import Counter
from datetime import date, datetime
from pathlib import Path


def _top(counter: Counter, n: int) -> list:
    return [[k, v] for k, v in counter.most_common(n)]


def library_stats(meta: dict, when: list[str], people_ix: dict, trips: dict, events: dict) -> dict:
    """What the photos are: from the app's in-memory metadata."""
    photos = list(meta.values())
    cam, lens, country, city, focal = Counter(), Counter(), Counter(), Counter(), Counter()
    people, albums = Counter(), Counter()
    gps = favs = tagged = placed = 0
    for m in photos:
        if m.get("lat") or m.get("lon"):
            gps += 1
        favs += bool(m.get("fav"))
        tagged += bool(m["people"])
        placed += bool(m.get("place"))
        if m.get("camera"):
            cam[m["camera"]] += 1
        if m.get("lens"):
            lens[m["lens"]] += 1
        if m.get("country"):
            country[m["country"]] += 1
        if m.get("city"):
            city[m["city"].split(",")[0]] += 1
        if m.get("focal35") or m.get("focal"):
            focal[round(m.get("focal35") or m["focal"])] += 1
        people.update(m["people"])
        albums.update(m["albums"])
    years, hours, weekdays, days = Counter(), [0] * 24, [0] * 7, Counter()
    for w in when:
        if len(w) >= 4 and w[:4].isdigit():
            years[int(w[:4])] += 1
        if len(w) >= 10:
            days[w[:10]] += 1
            try:
                d = date.fromisoformat(w[:10])
                weekdays[d.weekday()] += 1
                if len(w) >= 13:
                    hours[int(w[11:13])] += 1
            except ValueError:
                pass
    streak = best = 0
    prev = None
    best_end = None
    for d in sorted(days):
        try:
            o = date.fromisoformat(d).toordinal()
        except ValueError:
            continue
        streak = streak + 1 if prev is not None and o == prev + 1 else 1
        if streak > best:
            best, best_end = streak, d
        prev = o
    dated = sorted(w for w in when if len(w) >= 10)
    busiest = days.most_common(1)
    trip_rows = sorted(trips.values(), key=lambda t: -(t["n"] or 0))[:5]
    return {
        "photos": len(photos), "with_gps": gps, "favourites": favs, "with_people_tagged": tagged,
        "with_place": placed, "people": len(people), "albums": len(albums), "countries": len(country),
        "cities": len(city), "events": len(events), "trips": len(trips),
        "first": dated[0] if dated else None, "last": dated[-1] if dated else None,
        "days_with_photos": len(days), "busiest_day": busiest[0] if busiest else None,
        "longest_streak": {"days": best, "ends": best_end},
        "years": sorted(years.items()), "hours": hours, "weekdays": weekdays,
        "cameras": _top(cam, 10), "lenses": _top(lens, 8), "focal_lengths": _top(focal, 8),
        "countries_top": _top(country, 10), "cities_top": _top(city, 10),
        "people_top": _top(people, 10), "albums_top": _top(albums, 8),
        "biggest_trips": [[t["title"], t["n"], t["start"], t["end"]] for t in trip_rows],
    }


def _walk(d: Path) -> tuple[int, int]:
    """(files, bytes) under d, without following links."""
    n = size = 0
    stack = [str(d)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                    elif e.is_file(follow_symlinks=False):
                        n += 1
                        size += e.stat(follow_symlinks=False).st_size
        except OSError:
            pass
    return n, size


def disk_stats(db: str, store: str, caches: list) -> dict:
    """What the library costs: catalog contents, database size by table, cache folders, free space."""
    t0 = time.time()
    con = sqlite3.connect(db, timeout=60)
    one = lambda q, *a: con.execute(q, a).fetchone()

    def opt(q, *a):
        try:
            return con.execute(q, a).fetchall()
        except sqlite3.OperationalError:
            return []

    kinds = {k: {"n": n, "bytes": s} for k, n, s in con.execute(
        "SELECT kind, count(*), coalesce(sum(size), 0) FROM assets GROUP BY kind")}
    by_ext = [[e, k, n, s] for e, k, n, s in con.execute(
        "SELECT ext, kind, count(*), coalesce(sum(size), 0) FROM assets GROUP BY ext ORDER BY 4 DESC LIMIT 12")]
    occurrences, sources = one("SELECT count(*), count(DISTINCT source) FROM occurrences")
    seen = one("SELECT coalesce(sum(a.size), 0) FROM occurrences o JOIN assets a USING(sha256)")[0]
    stored = sum(v["bytes"] for v in kinds.values())
    mp = one("SELECT coalesce(sum(width * height), 0) / 1e6 FROM assets WHERE kind='image'")[0]
    biggest = [[s, e, p] for s, e, p in con.execute(
        "SELECT a.size, a.ext, (SELECT filename FROM occurrences o WHERE o.sha256 = a.sha256 LIMIT 1)"
        " FROM assets a ORDER BY a.size DESC LIMIT 5")]
    video = opt("SELECT coalesce(sum(duration), 0), count(*) FROM exif WHERE duration > 0")
    counts = {}
    for name, q in (("embeddings", "SELECT count(*) FROM embeddings WHERE vec IS NOT NULL"),
                    ("embedding_errors", "SELECT count(*) FROM embeddings WHERE vec IS NULL"),
                    ("exif", "SELECT count(*) FROM exif"), ("faces", "SELECT count(*) FROM faces"),
                    ("faces_named", "SELECT count(*) FROM faces WHERE person_id > 0"),
                    ("faces_by_you", "SELECT count(*) FROM faces WHERE source = 'you'"),
                    ("persons", "SELECT count(*) FROM persons"),
                    ("images_scanned_for_faces", "SELECT count(*) FROM face_scans"),
                    ("tags", "SELECT count(*) FROM tags"), ("sidecars", "SELECT count(*) FROM sidecars")):
        r = opt(q)
        counts[name] = r[0][0] if r else None
    tables = [[n, s] for n, s in opt("SELECT name, sum(pgsize) FROM dbstat GROUP BY name ORDER BY 2 DESC")
              if not n.startswith("sqlite_")][:14]   # dbstat is missing from some SQLite builds: then empty
    recent = [[n, e, m, na, b, f] for n, e, m, na, b, f in opt(
        "SELECT name, entries, media, new_assets, bytes_new, finished_at FROM sources"
        " ORDER BY finished_at DESC LIMIT 8")]
    con.close()

    db_p = Path(db)
    wal = sum(p.stat().st_size for p in (Path(f"{db}-wal"), Path(f"{db}-shm")) if p.exists())
    cache = {}
    for kind in ("thumbs", "previews", "large", "faces"):
        n = size = 0
        for c in dict.fromkeys(map(str, caches)):
            a, b = _walk(Path(c) / kind)
            n, size = n + a, size + b
        cache[kind] = {"files": n, "bytes": size}
    try:
        du = shutil.disk_usage(store)
        fs = {"total": du.total, "used": du.used, "free": du.free}
    except OSError:
        fs = None
    return {
        "kinds": kinds, "by_ext": by_ext, "occurrences": occurrences, "sources": sources,
        "bytes_seen": seen, "bytes_stored": stored, "bytes_saved_by_exact_dedupe": max(seen - stored, 0),
        "megapixels": mp, "biggest_files": biggest,
        "video_seconds": video[0][0] if video else None, "videos_with_duration": video[0][1] if video else None,
        "counts": counts, "database": {"bytes": db_p.stat().st_size, "wal_bytes": wal, "tables": tables},
        "cache": cache, "filesystem": fs, "recent_sources": recent, "took_s": round(time.time() - t0, 1),
    }


def rss_bytes() -> int | None:
    try:
        import resource
        r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return r if sys.platform == "darwin" else r * 1024   # bytes on macOS, KB on Linux
    except ImportError:
        return None


if __name__ == "__main__":
    import argparse
    import json
    p = argparse.ArgumentParser(description="print the disk and catalog statistics as JSON")
    p.add_argument("--db", default="estate.db")
    p.add_argument("--store", required=True)
    p.add_argument("--cache", help="thumbnail cache (default <db dir>/review)")
    a = p.parse_args()
    cache = a.cache or Path(a.db).resolve().parent / "review"
    print(json.dumps(disk_stats(a.db, a.store, [cache]), indent=1))
