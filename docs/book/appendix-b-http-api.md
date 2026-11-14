# Appendix B: HTTP API reference

Three servers share the same style (plain `BaseHTTPRequestHandler`, JSON, no authentication, loopback
only):

| Server | Started by | Default port | Purpose |
|---|---|---|---|
| **Photo app** | `embed.py serve` | 8766 (8767 with `--demo`) | The product; also hosts the People page |
| **People page alone** | `faces.py serve` | 8768 | Naming only |
| **Duplicate review** | `estate.py review` | 8765 | Near-duplicate choices |

## The photo app

### Pages and files

| Path | Returns |
|---|---|
| `GET /` , `/index.html` | `search.html`, with an `ETag` and `Cache-Control: no-cache` |
| `GET /manifest.webmanifest`, `/icon-180.png`, `/icon-192.png`, `/icon-512.png`, `/icon-maskable-512.png`, `/sw.js` | The files in `app/`, served from the site root |
| `GET /people`, `/people/` | `people.html`, once faces exist and `People` has loaded (else 503) |

### Search and browse

`GET /api/search` and `GET /api/similar` take the same parameters; `/api/similar` additionally
requires `sha` and ranks by that photo's vector (404 `{"error":"unknown photo"}` if unknown).

| Parameter | Repeatable | Meaning |
|---|---|---|
| `q` | | Free text, possibly with typed filters, names and places ([chapter 12](12-search-language.md)) |
| `names` | | `0` = do not read names and places in `q` |
| `sha` | | The photo for `/api/similar` |
| `country`, `region`, `city`, `camera`, `lens` | yes | Facet values; **any of** the repeated values |
| `album` | yes | **Any of** |
| `person` | yes | **All of** |
| `trip` | | A trip id (scope if single) |
| `event` | | An event id (filter and scope) |
| `from`, `to` | | `YYYY` or `YYYY-MM` month bounds, inclusive |
| `fav` | | `1` = favourites only |
| `sort` | | `old` or `new` (default `old` when scoped to an event or trip, else `new`); only for unranked results |
| `cursor` | | Position to continue from (the previous response's `next`) |
| `limit` | | 1-500, default 120 |
| `dedupe` | | `0` to show near-identical shots (default `1`: hide) |

Response:

```json
{
  "results": [ { "sha": "...", "ext": ".jpg", "taken": "...", "local": "2019-07-14T12:22:33",
                 "tz": "Europe/Paris", "lat": 48.85, "lon": 2.29, "desc": null,
                 "people": ["Ben"], "albums": ["Paris Trip"], "fav": true, "w": 4032, "h": 3024, "ar": 1.3333,
                 "path": "Takeout/.../IMG_0001.JPG", "camera": "Apple iPhone 15 Pro", "lens": "...",
                 "focal": 6.8, "focal35": 24.0, "fnumber": 1.78, "exposure": 0.004, "iso": 50,
                 "place": "Paris, Île-de-France, France", "city": "...", "region": "...", "country": "France",
                 "county": "...", "place_inferred": false, "event": 1563099753, "event_title": "Paris Trip",
                 "trip": 1563099753, "trip_title": "Trip to Paris, France" } ],
  "next": 150,                // cursor for the next page, or null
  "hidden": 3,                // near-identical shots skipped on this page
  "total": 104,               // photos in the library
  "matching": 8,              // photos matching (the length of the ranking)
  "text": "latte art",        // what was left for the image model
  "filters": ["place:denmark"],   // typed filters applied
  "people": ["Ben Lam"], "places": ["San Diego"],   // names and places read from q
  "scope": { "kind": "trip", "id": 1, "title": "...", "start": "...", "end": "...", "n": 120, "trip": null },
  "sort": "new",
  "ms": 91
}
```

(`results[*]` fields are present when known.)

### Facets and info

| Request | Returns |
|---|---|
| `GET /api/facets?...` (same filter parameters) | `{"facets": {country, region, city, camera, lens, trip, album, person: [[label, count], ...]}, "months": [counts], "fav": N, "matching": N}` (each facet's counts ignore its own selection; `person` counts include it) |
| `GET /api/info` | `{"total", "model", "m0", "trips": [{id, title, start, end, n, cover}], "demo", "faces", "facets", "months", "fav", "matching"}`: the unfiltered facet payload merged in. `months` is the list of per-month counts, so the number of months is its length and `m0` is the first month as `year*12 + month - 1` |
| `GET /stats` | The library info page ([stats.html](../../photo-estate/stats.html)) |
| `GET /api/stats[?refresh=1]` | `{state: working|ready|error, library, disk, app}`: library figures from memory, disk figures (catalog tables, cache folders, free space) measured once in a background thread and saved to `stats-cache.json` next to the catalog; `refresh=1` recomputes (the page's Recompute button); `catalog_assets_now` lets the page flag a stale measurement. Code in [stats.py](../../photo-estate/stats.py), which also runs standalone |
| `GET /api/log?ev=open|share&sha=...&pos=N` plus the search's parameters | Appends an event to the query log; `204` |

### Images

| Path | Returns |
|---|---|
| `GET /thumb/<sha>` | 360 px JPEG |
| `GET /preview/<sha>` | 1600 px JPEG |
| `GET /large/<sha>` | 4096 px JPEG |
| `GET /original/<sha>` | The stored file (`.jpg`, `.jpeg`, `.heic`, `.png`, `.gif`, `.webp` only), `Content-Disposition: inline; filename*=UTF-8''<original name>` |

All four validate `<sha>` as 64 lowercase hex characters of a known photo (else 404) and are served
`public, max-age=2592000, immutable`.

### The People API (also on `faces.py serve`)

| Request | Purpose |
|---|---|
| `GET /face/<id>` | The 160 px face crop (regenerated from the original if the cache lacks it); cached one day |
| `GET /api/people` | The overview: faces, photos, named, ignored, pending, people with year counts and cover, and the state of the unknown-groups job |
| `GET /api/people/review?person=ID[&year=Y][&limit=60][&skipped=1]` | Faces waiting for that person, most likely first |
| `GET /api/people/person?person=ID[&year=Y][&limit=12]` | A person's named faces: a few per year, or all of one year |
| `GET /api/people/odd?person=ID[&year=Y][&offset=0][&limit=60]` | A person's named faces, least like the rest first |
| `GET /api/people/unknown[?limit=12]` | Groups of unnamed faces |
| `GET /api/people/photo?face=ID` | One face's photo and every face in it |
| `POST /api/people/label` | Name, forget, rule out, put aside, undo, name a cluster ([chapter 15](15-human-corrections.md) for the body) |
| `POST /api/people/label-many` | `{"items": [label bodies]}`: apply in order, rescore once |
| `POST /api/people/person` | `{"id", "name"?, "born"?}`: rename (or merge) and set birth month |

`POST /api/people/label` body (any combination):

```json
{ "faces": [12, 13], "person": 5,          // person: id | "a name" | 0 (stranger) | null (forget)
  "pending": {"person": 5, "year": 2016},  // instead of "faces": everything waiting for that person
  "cluster": 3,                            // instead of "faces": the whole unknown group
  "not": [14], "not_person": 5,            // rule out
  "later": [15], "later_person": 5,        // put aside
  "undo": [12], "undo_person": 5 }         // take back
```

Returns `{"ok": true, "pending": N}`.

## The `faces.py serve` extras

| Request | Returns |
|---|---|
| `GET /` | 302 to `/people` |
| `GET /preview/<sha>` | A 1600 px preview (rendered on demand into `review/previews/`) |

## The duplicate review server (`estate.py review`)

| Request | Returns |
|---|---|
| `GET /` | `review.html` |
| `GET /groups.json` | `{"groups": [...], "categories": {key: {label, policy, short}}}` |
| `GET /decisions.json` | Choices saved so far |
| `GET /thumb/<sha>`, `/preview/<sha>` | 360 px and 1600 px JPEGs |
| `POST /decisions` | `{groupId: {keep: [shas], reviewed: bool, category}}`; merged into `decisions.json`; groups with an empty `keep` are ignored |

## Status codes

| Code | When |
|---|---|
| 200 | OK |
| 204 | `/api/log` |
| 304 | A conditional request matched the `ETag` |
| 400 | A malformed review decision |
| 404 | Unknown sha, unknown path, or `/api/similar` with an unknown photo |
| 503 | `/people` before the face data has loaded |
