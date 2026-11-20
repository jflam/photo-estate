#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["reverse_geocode>=1.6", "timezonefinder", "pycountry", "numpy", "pillow>=10.0", "pillow-heif>=0.18"]
# ///
"""
places.py — Tier 0: where and when, worked out from what the catalog already knows. No models, no
network: GeoNames cities (reverse_geocode) and timezone shapes (timezonefinder) ship offline.

  run     place names, local time, events and trips for every asset; rebuilds the search index
  events  list events (--trips: list trips)

  uv run places.py --db /Volumes/estate/estate.db run
  uv run places.py --db /Volumes/estate/estate.db events --trips

Everything is recomputed on each run (seconds), so new photos and better rules just mean re-running;
only the rows that come out different are written, so a run with a few new photos writes a few rows.

How it decides:
  place       GPS -> nearest GeoNames place; but a city of 100k+ within ~0.006*sqrt(population) km
              wins over a nearer suburb or neighbourhood (the Eiffel Tower is in Paris, not Vanves),
              and the nearer place is kept as `locality`. Photos without GPS get their event's most
              common place, marked place_source='event'.
  timezone    GPS -> IANA zone; else the EXIF/QuickTime UTC offset, or the difference between a
              sidecar's UTC time and the camera's wall-clock time; else the event's zone; else the zone
              of that year's home.
  local time  times that carry an offset (Google/iCloud sidecars are UTC) are converted to the zone;
              offset-less times (EXIF, Flickr) are already wall-clock time and are kept.
  home        per year, the ~10 km cell where photos were taken on the most distinct days (so a move
              from one city to another is followed). Years without GPS borrow the nearest year's.
  events      photos in time order, split wherever there is a gap of more than --gap hours. Titled by
              an album most of its photos share, else its place.
  trips       runs of events more than --away km from home with no home event in between and no gap
              over two days, spanning at least two dates. Titled "Trip to <place>".
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import estate  # noqa: E402  (same folder; schema, connect, reindex)

BIG_CITY = 100_000
OFFSET_KEYS = ('$."ExifIFD:OffsetTimeOriginal"', '$."ExifIFD:OffsetTime"', '$."IFD0:OffsetTime"')
QT_DATE_KEYS = ('$."Keys:CreationDate"', '$."UserData:DateTimeOriginal"')


# ----------------------------------------------------------------------------- helpers

def parse_offset(s) -> timezone | None:
    """'-07:00', '+0530', or the offset at the end of '2024:06:13 21:06:35-07:00'."""
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    tail = s[-6:] if len(s) >= 6 and s[-6] in "+-" and s[-3] == ":" else s[-5:] if len(s) >= 5 and s[-5] in "+-" else ""
    if not tail:
        return None
    try:
        sign = -1 if tail[0] == "-" else 1
        hh, mm = int(tail[1:3]), int(tail[-2:])
        if hh > 14 or mm > 59:
            return None
        return timezone(sign * timedelta(hours=hh, minutes=mm))
    except ValueError:
        return None


def offset_between(utc: datetime, wall: str) -> timezone | None:
    """The zone implied by a UTC instant and the camera's wall-clock time of the same shot, if it is a
    real one (a whole quarter hour within +-14 h)."""
    try:
        minutes = (datetime.fromisoformat(wall) - utc.astimezone(timezone.utc).replace(tzinfo=None)).total_seconds() / 60
    except ValueError:
        return None
    if abs(minutes) > 14 * 60 or minutes % 15:
        return None
    return timezone(timedelta(minutes=minutes))


def tz_label(tz) -> str:
    return tz.key if isinstance(tz, ZoneInfo) else str(tz).replace("UTC", "") or "+00:00"


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(min(1, math.sqrt(a)))


def country_name(cc: str) -> str:
    import pycountry   # pyright: ignore[reportMissingImports]  (the places group: not in the dev environment)
    c = pycountry.countries.get(alpha_2=cc)
    return getattr(c, "common_name", None) or (c.name if c else cc)


def place_label(city, admin1, country, home_cc) -> str:
    """'Springfield, Illinois' at home; 'Toronto, Canada' abroad."""
    parts = [city, admin1] if country == home_cc else [city, country_name(country)]
    return ", ".join(p for p in parts if p)


def dominant(values, share: float = 0.0):
    """Most common non-empty value, if it has at least `share` of the non-empty values."""
    c = Counter(v for v in values if v)
    if not c:
        return None
    v, n = c.most_common(1)[0]
    return v if n >= share * sum(c.values()) else None


# ----------------------------------------------------------------------------- run

PLACE_COLS = ("sha256", "city", "locality", "admin2", "admin1", "country", "cc", "place_source", "tz", "tz_source",
              "local_time", "utc_time", "event_id")
EVENT_COLS = ("id", "start_local", "end_local", "n", "title", "place", "lat", "lon", "km_from_home", "trip_id")
TRIP_COLS = ("id", "start_local", "end_local", "n", "n_events", "title", "place")


def tables_current(con) -> bool:
    """Whether places, events and trips exist with the columns estate.SCHEMA gives them now. If not,
    the run recreates them (they are fully derived), so a schema change needs no migration."""
    want = sqlite3.connect(":memory:")
    want.executescript(estate.SCHEMA)
    cols = lambda c, t: [r[1:3] for r in c.execute(f"PRAGMA table_info({t})")]
    return all(cols(con, t) == cols(want, t) for t in ("places", "events", "trips"))


def signatures(places: dict, events: dict, trips: dict) -> dict:
    """What `places` adds to each asset's search text (place names, event and trip titles), worked out
    from rows in memory: when it changes, so must the search row."""
    out = {}
    for sha, p in places.items():
        e = events.get(p[12])
        t = trips.get(e[9]) if e else None
        out[sha] = (*p[1:6], e[4] if e else None, t[5] if t else None)
    return out


def write_diff(con, table: str, key: str, cols: tuple, now: dict, before: dict) -> int:
    """Make `table` hold the rows in `now` ({key: row}) by writing only the rows that differ."""
    gone = [(k,) for k in before.keys() - now.keys()]
    changed = [r for k, r in now.items() if before.get(k) != r]
    con.executemany(f"DELETE FROM {table} WHERE {key}=?", gone)
    con.executemany(f"INSERT OR REPLACE INTO {table}({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", changed)
    return len(gone) + len(changed)


def cmd_run(a) -> None:
    t0 = time.time()
    con = estate.connect(a.db)
    con.execute("PRAGMA busy_timeout=120000")
    offs = ", ".join(f"json_extract(e.data, '{k}')" for k in OFFSET_KEYS + QT_DATE_KEYS)
    has_exif = con.execute("SELECT 1 FROM exif LIMIT 1").fetchone() is not None   # not count(*): a 5 s scan on a hard disk

    # What the last run decided. Place names and zones come from GPS (reverse geocoding and timezone
    # lookup are the slow parts), and EXIF offsets from exiftool's JSON (a big table): both are reused
    # for photos already in `places`, so a run costs about what the new photos cost. --full redoes all.
    current = tables_current(con)
    step = lambda k, what: estate.progress(k, 5, what, every=1)   # for the setup page: the steps, not photos
    step(1, "reading the library")

    def sel(t, cols):          # what the last run wrote, as far as it can still be read
        try:
            return {r[0]: r for r in con.execute(f"SELECT {', '.join(cols)} FROM {t}")}
        except sqlite3.OperationalError:
            return {}
    prior = {"places": sel("places", PLACE_COLS), "events": sel("events", EVENT_COLS), "trips": sel("trips", TRIP_COLS)}
    before = signatures(*prior.values())
    old = {} if a.full else {sha: (*r[1:5], r[6], r[7], r[8], r[9]) for sha, r in prior["places"].items()}
    albums: dict[str, str] = {}
    for sha, album in con.execute("SELECT sha256, album FROM occurrences WHERE album IS NOT NULL ORDER BY id"):
        albums.setdefault(sha, album)
    rows = [(*r, albums.get(r[0])) for r in con.execute(
        "SELECT sha256, taken_at, exif_taken_at, lat, lon FROM assets")]

    def gps_ok(lat, lon) -> bool:
        return lat is not None and lon is not None and (abs(lat) > 1e-6 or abs(lon) > 1e-6) \
            and -90 <= lat <= 90 and -180 <= lon <= 180

    def reusable(sha, lat, lon) -> bool:
        o = old.get(sha)
        if o is None:
            return False
        return not gps_ok(lat, lon) or (o[5] == "gps" and o[7] == "gps")   # a GPS photo needs its cached zone

    fresh = [r[0] for r in rows if r[1] and not reusable(r[0], r[3], r[4])]
    offsets: dict[str, str | None] = {}
    if has_exif and fresh:
        con.execute("DROP TABLE IF EXISTS temp.fresh")
        con.execute("CREATE TEMP TABLE fresh(sha256 TEXT PRIMARY KEY)")
        con.executemany("INSERT INTO fresh VALUES (?)", [(x,) for x in fresh])
        offsets = {sha: off for sha, off in con.execute(
            f"SELECT f.sha256, coalesce({offs}) FROM fresh f JOIN exif e ON e.sha256 = f.sha256")}
    print(f"{len(rows)} assets, {len(fresh)} to place from scratch", flush=True)

    # --- place names and zones for everything with GPS
    step(2, "naming places and time zones")
    A = []  # one dict per asset with a usable time
    gps = []
    for sha, taken, exif_taken, lat, lon, album in rows:
        if not taken:
            continue
        try:
            dt = datetime.fromisoformat(taken)
        except ValueError:
            continue
        ok_gps = gps_ok(lat, lon)
        cached = old.get(sha) if sha not in offsets and reusable(sha, lat, lon) else None
        off = parse_offset(offsets.get(sha))
        if off is None and dt.tzinfo is not None and exif_taken and cached is None:
            off = offset_between(dt, exif_taken)
        r = {"sha": sha, "dt": dt, "lat": lat if ok_gps else None, "lon": lon if ok_gps else None,
             "off": off, "album": album, "tz": None, "tz_src": None, "place": None, "locality": None,
             "cached": cached}
        if cached is not None and not ok_gps and cached[7] == "exif":
            r["off"] = parse_offset(cached[6])     # the offset it was placed with last time
        A.append(r)
        if ok_gps:
            gps.append(r)
    todo_geo = [r for r in gps if r["cached"] is None]
    if todo_geo:   # the geocoder and the zone shapes take a while to load: only when something needs them
        import reverse_geocode   # pyright: ignore[reportMissingImports]  (the places group)
        from timezonefinder import TimezoneFinder   # pyright: ignore[reportMissingImports]
        pts = [(r["lat"], r["lon"]) for r in todo_geo]
        near, big = reverse_geocode.search(pts), reverse_geocode.search(pts, min_population=BIG_CITY)
        tf, zones = TimezoneFinder(), {}
        for r, g, b in zip(todo_geo, near, big):
            if b.get("city") != g.get("city") and haversine_km(r["lat"], r["lon"], b["latitude"], b["longitude"]) \
                    <= min(25, 0.006 * math.sqrt(b.get("population") or 0)):
                r["locality"], g = g.get("city"), b
            r["place"] = (g.get("city"), g.get("county"), g.get("state"), g["country_code"])
            key = (round(r["lat"], 2), round(r["lon"], 2))
            if key not in zones:
                name = tf.timezone_at(lat=r["lat"], lng=r["lon"])
                zones[key] = ZoneInfo(name) if name else None
            if zones[key]:
                r["tz"], r["tz_src"] = zones[key], "gps"
    for r in gps:
        if (c := r["cached"]) is not None:       # c = city, locality, admin2, admin1, cc, place_source, tz, tz_source
            r["place"], r["locality"] = (c[0], c[2], c[3], c[4]), c[1]
            r["tz"], r["tz_src"] = ZoneInfo(c[6]), "gps"
    for r in A:
        if r["tz"] is None and r["off"] is not None:
            r["tz"], r["tz_src"] = r["off"], "exif"
    print(f"  {len(gps)} with GPS ({len(todo_geo)} geocoded, {time.time() - t0:.1f}s)", flush=True)

    step(3, "finding home, events and trips")
    # --- home per year: the ~10 km cell with photos on the most distinct days
    days: dict[int, dict[tuple, set]] = {}
    for r in gps:
        cell = (round(r["lat"], 1), round(r["lon"], 1))
        days.setdefault(r["dt"].year, {}).setdefault(cell, set()).add(r["dt"].date())
    home_of: dict[int, dict] = {}
    for y, cells in days.items():
        cell = max(cells, key=lambda c: len(cells[c]))
        near = min((r for r in gps if r["dt"].year == y and (round(r["lat"], 1), round(r["lon"], 1)) == cell),
                   key=lambda r: abs(r["lat"] - cell[0]) + abs(r["lon"] - cell[1]))
        home_of[y] = {"lat": near["lat"], "lon": near["lon"], "tz": near["tz"], "place": near["place"]}
    years = sorted(home_of)

    def home(y: int):
        if not years:
            return None
        return home_of[min(years, key=lambda k: (abs(k - y), -k))]

    last_home = home(max(years)) if years else None
    home_cc = last_home["place"][3] if last_home else None
    for y in years:
        h = home_of[y]
        print(f"  home {y}: {h['place'][0]}, {h['place'][2]} ({tz_label(h['tz']) if h['tz'] else '?'})")

    # --- a provisional UTC instant for ordering
    def to_utc(r):
        dt = r["dt"]
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc)
        tz = r["tz"] or ((home(dt.year) or {}).get("tz")) or timezone.utc
        return dt.replace(tzinfo=tz).astimezone(timezone.utc)

    for r in A:
        r["utc"] = to_utc(r)
    A.sort(key=lambda r: r["utc"])

    # --- events: split on gaps
    gap = timedelta(hours=a.gap)
    events: list[list[dict]] = []
    for r in A:
        if events and r["utc"] - events[-1][-1]["utc"] <= gap:
            events[-1].append(r)
        else:
            events.append([r])

    # --- fill zones and places from the event, then from home; compute local time
    for ev in events:
        ev_tz = dominant([tz_label(r["tz"]) for r in ev if r["tz_src"] == "gps"]) or \
            dominant([tz_label(r["tz"]) for r in ev if r["tz"] is not None])
        ev_tz_obj = next((r["tz"] for r in ev if r["tz"] is not None and tz_label(r["tz"]) == ev_tz), None)
        ev_place = dominant([r["place"] for r in ev])
        for r in ev:
            if r["tz"] is None:
                if ev_tz_obj is not None:
                    r["tz"], r["tz_src"] = ev_tz_obj, "event"
                elif (h := home(r["dt"].year)) and h["tz"]:
                    r["tz"], r["tz_src"] = h["tz"], "home"
            if r["dt"].tzinfo is not None and r["tz"] is not None:
                r["local"] = r["dt"].astimezone(r["tz"]).replace(tzinfo=None)
            else:
                r["local"] = r["dt"].replace(tzinfo=None)
            if r["dt"].tzinfo is None and r["tz"] is not None:
                r["utc"] = r["dt"].replace(tzinfo=r["tz"]).astimezone(timezone.utc)
            r["place_src"] = "gps" if r["place"] else None
            if r["place"] is None and ev_place:
                r["place"], r["place_src"] = ev_place, "event"

    # --- event rows
    ev_rows, used_ids = [], set()
    for ev in events:
        eid = int(min(r["utc"] for r in ev).timestamp())
        while eid in used_ids:
            eid += 1
        used_ids.add(eid)
        g = [r for r in ev if r["place_src"] == "gps"]
        lat = sorted(r["lat"] for r in g)[len(g) // 2] if g else None
        lon = sorted(r["lon"] for r in g)[len(g) // 2] if g else None
        p = dominant([r["place"] for r in ev])
        label = place_label(p[0], p[2], p[3], home_cc) if p else None
        album = dominant([r["album"] for r in ev], 0.5) if sum(bool(r["album"]) for r in ev) >= len(ev) / 2 else None
        start, end = min(r["local"] for r in ev), max(r["local"] for r in ev)
        h = home(start.year)
        km = haversine_km(lat, lon, h["lat"], h["lon"]) if g and h else None
        for r in ev:
            r["event"] = eid
        ev_rows.append({"id": eid, "start": start, "end": end, "n": len(ev), "title": album or label,
                        "place": label, "lat": lat, "lon": lon, "km": km, "trip": None, "members": ev,
                        "cc": p[3] if p else None})

    # --- trips: consecutive away events; events without GPS in between don't break a trip
    trips, cur = [], []

    def close():
        if cur and len({d for e in cur for d in (e["start"].date(), e["end"].date())}) >= 2:
            trips.append(list(cur))
        cur.clear()

    for e in ev_rows:
        if cur and e["start"] - cur[-1]["end"] > timedelta(days=2):
            close()
        if e["km"] is None:
            if cur:
                cur.append(e)
            continue
        if e["km"] > a.away:
            cur.append(e)
        else:
            close()
    close()
    trip_rows = []
    for t in trips:
        while t and t[-1]["km"] is None:   # trailing no-GPS events belong to the way home, not the trip
            t.pop()
        members = [r for e in t for r in e["members"] if r["place_src"] == "gps"]
        city = dominant([(r["place"][0], r["place"][2], r["place"][3]) for r in members], 0.6)
        regions = Counter((r["place"][2], r["place"][3]) for r in members if r["place"][2])
        by_cc = Counter(r["place"][3] for r in members)
        top = [rg for rg, n in regions.most_common(2) if n >= 0.15 * sum(regions.values())]
        top_cc = [cc for cc, n in by_cc.most_common(2) if n >= 0.15 * sum(by_cc.values())]
        if city:
            label = place_label(city[0], city[1], city[2], home_cc)
        elif len(top_cc) > 1 and home_cc not in top_cc:   # abroad in several countries: name them
            label = " and ".join(country_name(cc) for cc in top_cc)
        elif top and len(ccs := {rg[1] for rg in top}) == 1 and home_cc not in ccs:  # "Quebec and Ontario, Canada"
            label = " and ".join(rg[0] for rg in top) + f", {country_name(top[0][1])}"
        elif top:                                          # "Florida and Ontario, Canada"
            label = " and ".join(rg[0] if rg[1] == home_cc else f"{rg[0]}, {country_name(rg[1])}" for rg in top)
        else:
            label = "somewhere"
        tid = t[0]["id"]
        for e in t:
            e["trip"] = tid
        trip_rows.append((tid, t[0]["start"].isoformat(), t[-1]["end"].isoformat(), sum(e["n"] for e in t),
                          len(t), f"Trip to {label}", label))

    step(4, "saving places, events and trips")
    # --- write: only the rows that differ from what is there (most of them, on a run with a few new
    # photos), so the cost follows what changed; the tables are recreated when their columns changed
    now = {
        "places": {r["sha"]: (r["sha"], r["place"][0] if r["place"] else None,
                              r["locality"] if r["place_src"] == "gps" else None,
                              *(r["place"][1:3] if r["place"] else (None,) * 2),
                              country_name(r["place"][3]) if r["place"] else None, r["place"][3] if r["place"] else None,
                              r["place_src"], tz_label(r["tz"]) if r["tz"] is not None else None, r["tz_src"],
                              r["local"].isoformat(), r["utc"].isoformat(), r["event"]) for r in A},
        "events": {e["id"]: (e["id"], e["start"].isoformat(), e["end"].isoformat(), e["n"], e["title"], e["place"],
                             e["lat"], e["lon"], e["km"], e["trip"]) for e in ev_rows},
        "trips": {t[0]: t for t in trip_rows},
    }
    con.commit()                              # the temp table above opened an implicit transaction
    con.execute("BEGIN")
    if not current:
        for t in ("places", "events", "trips"):
            con.execute(f"DROP TABLE IF EXISTS {t}")
        for stmt in estate.SCHEMA.split(";"):
            if any(f" {t}(" in stmt or f" {t} " in stmt for t in ("places", "events", "trips", "places_event")):
                con.execute(stmt)
    wrote = sum(write_diff(con, t, k, cols, now[t], prior[t] if current else {})
                for t, k, cols in (("places", "sha256", PLACE_COLS), ("events", "id", EVENT_COLS), ("trips", "id", TRIP_COLS)))
    after = signatures(*now.values())
    changed = [sha for sha in after.keys() | before.keys() if after.get(sha) != before.get(sha)]
    print(f"  {len(ev_rows)} events, {len(trip_rows)} trips; {wrote} rows written;"
          f" updating search rows of {len(changed)} assets", flush=True)
    estate.reindex(con, changed, report=True)
    con.commit()
    src = Counter(r["tz_src"] for r in A)
    psrc = Counter(r["place_src"] for r in A)
    print(f"done in {time.time() - t0:.0f}s: {len(A)} assets with a time"
          f" | zone from gps {src['gps']}, exif {src['exif']}, event {src['event']}, home {src['home']},"
          f" none {src[None]} | place from gps {psrc['gps']}, event {psrc['event']}, none {psrc[None]}")


def cmd_events(a) -> None:
    con = estate.connect(a.db)
    if a.trips:
        q = "SELECT id, start_local, end_local, n, n_events, title FROM trips ORDER BY start_local DESC LIMIT ?"
        for tid, s, e, n, ne, title in con.execute(q, (a.limit,)):
            print(f"{s[:10]} → {e[:10]}  {n:5} photos {ne:3} events  {title}")
        return
    q = ("SELECT e.start_local, e.end_local, e.n, e.title, t.title FROM events e LEFT JOIN trips t ON t.id=e.trip_id"
         " WHERE e.n >= ? ORDER BY e.start_local DESC LIMIT ?")
    for s, e, n, title, trip in con.execute(q, (a.min, a.limit)):
        print(f"{s[:16]} → {e[11:16]}  {n:5}  {title or '?'}" + (f"   [{trip}]" if trip else ""))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default="estate.db")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("run", help="places, local time, events, trips (recomputed from scratch)")
    s.add_argument("--gap", type=float, default=3.0, help="hours without photos that end an event")
    s.add_argument("--away", type=float, default=100.0, help="km from home that makes an event part of a trip")
    s.add_argument("--full", action="store_true", help="geocode and look up zones for every photo again")
    s.set_defaults(fn=cmd_run)
    s = sub.add_parser("events", help="list events, newest first")
    s.add_argument("--trips", action="store_true")
    s.add_argument("--min", type=int, default=1, help="only events with at least this many photos")
    s.add_argument("--limit", type=int, default=40)
    s.set_defaults(fn=cmd_events)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
