# Appendix B: HTTP API reference

Three servers share the same style (plain `BaseHTTPRequestHandler`, JSON, loopback only, no sign-in of
their own; the photo app decides who may write from `tailscale serve`'s `Tailscale-User-Login`, below):

| Server | Started by | Default port | Purpose |
|---|---|---|---|
| **Photo app** | `embed.py serve` | 8766 (8767 with `--demo`, 8769 with `--sandbox`) | The product; also hosts the People page |
| **People page alone** | `faces.py serve` | 8764 | Naming only |
| **Duplicate review** | `estate.py review` | 8765 | Near-duplicate choices |

The defaults are in [core/config.py](../../photo-estate/core/config.py). Port 8768 belongs to the cloud preview
on the Mac mini (`deploy/preview-branch.sh`, [chapter 27](27-built-with-claude.md)); before issue #73,
`--sandbox` and `faces.py serve` defaulted to it too.

## The photo app

`embed.py serve` builds a `PhotoApp` ([photo_app.py](../../photo-estate/photo_app.py)), whose `handle` sends
each request to the module that answers it ([chapter 11](11-server.md)):

| Routes | Answered by |
|---|---|
| `/`, the app files, `/api/info`, `/api/sync`, `/api/favorite`, `/api/albums/<id>/download` | `PhotoApp` in [photo_app.py](../../photo-estate/photo_app.py) |
| `/api/search`, `/api/similar`, `/api/facets`, `/api/periods`, `/api/layout`, `/api/log` | `Browse` in [browse.py](../../photo-estate/browse.py) |
| `/thumb/`, `/preview/`, `/large/`, `/original/` | `Images` in [images.py](../../photo-estate/images.py) |
| `/api/upload/...`, `/api/ingest`, `/api/restart`, `/stats`, `/api/stats` | `Admin` in [admin.py](../../photo-estate/admin.py) |
| `/people`, `/face/`, `/api/people*` | `People` in [faces.py](../../photo-estate/faces.py) |
| `/changes`, `/api/changes*`, `/api/albums*`, `/api/tasks*` | `Changes` in [changes.py](../../photo-estate/changes.py), `Tasks` in [tasks.py](../../photo-estate/tasks.py) |
| `/mcp` | `Server` in [estate_mcp.py](../../photo-estate/estate_mcp.py) |
| `/setup`, `/api/intake*` (not with `--demo`) | `IntakeAPI` in [intake/api.py](../../photo-estate/intake/api.py) |
| `/compare`, `/api/compare*` (only with `--compare-vectors`) | `Compare` in [compare.py](../../photo-estate/compare.py) |

All of them answer from the same in-memory catalog (`catalog.Live`, in [catalog.py](../../photo-estate/catalog.py)).

### Who may write

Reading is open to anyone who can reach the app. Every request that changes the library takes one rule
(`Auth.LOCAL_OR_WRITER`, `ROUTE_AUTH` in photo_app.py, [chapter 11](11-server.md)): the caller is a
`--upload-user` login (the `Tailscale-User-Login` header `tailscale serve` adds), or the request comes
from this computer itself (`127.0.0.1` or `::1`, with no `Tailscale-User-Login`, `X-Forwarded-For` or
`Forwarded` header): the browser there, the task agent, a local MCP client. With no `--upload-user`,
everyone may write. Otherwise the answer is `403 {"error": "not allowed"}` (uploads: `403 not allowed`,
text).

| Writes (the rule above) | Open to anyone |
|---|---|
| `POST /api/people/label`, `/api/people/label-many`, `/api/people/person` | every `GET` |
| `POST /api/favorite` | `POST /mcp` (its write tools take the rule: a caller who may not write gets an `isError` result) |
| `POST /changes...`, `/api/changes...`, `/api/albums...`, `/api/tasks...` | `OPTIONS /api/upload/...` |
| `POST /api/intake/...` | |
| `POST /api/ingest`, `/api/restart` | |
| `PUT` and `POST /api/upload/...` | |

Before 2026-11-19 the three People writes took no permission, and intake, the ingest, restart and
uploads took a `--upload-user` login only (this computer's own requests were refused there).

### Malformed requests

| Request | Answer |
|---|---|
| A number parameter that isn't one: `limit`, `cursor` on `/api/search` and `/api/similar`; `limit` on `/api/sync`; `job`, `after` on `/setup/practice-consent` and `/api/intake/jobs/<id>` | `400 {"error": "limit must be a whole number"}` (named for the parameter). Numbers out of range are clamped as before (`limit` 1-500 on search, 1-10000 on sync) |
| A POST body that isn't JSON | `400 {"error": "the body is not JSON"}`; on `/mcp`, JSON-RPC's `-32700` error, also a 400. An empty body is `{}` |
| A `Content-Length` that isn't a whole number | `400 {"error": "Content-Length must be a whole number"}` |
| A JSON body over 8 MB (`http_util.MAX_BODY`) | `413 {"error": "the body is over 8388608 bytes"}`, before reading it; the connection is closed |
| `from=` or `to=` with a month outside 1-12 (`2020-00`, `2020-99`) | Ignored, like any date that can't be read |

The setup server (`intake/server.py`) and `faces.py serve` read bodies the same way.

### Pages and files

| Path | Returns |
|---|---|
| `GET /` , `/index.html` | `search.html`, with an `ETag` and `Cache-Control: no-cache` |
| `GET /manifest.webmanifest`, `/icon-180.png`, `/icon-192.png`, `/icon-512.png`, `/icon-maskable-512.png`, `/sw.js` | The files in `app/`, served from the site root |
| `GET /people`, `/people/` | `people.html`, once faces exist and `People` has loaded (else 503) |

### Uploads from the phone

| Request | Returns |
|---|---|
| `PUT /api/upload/<YYYY/MM/DD/name.ext>` | Saves the body to the inbox (`--inbox`, default `<db dir>/iphone`; none with `--demo`). `Content-Length` is required; an optional `X-Content-SHA256` is checked against the bytes received. `201 {"status":"stored"}`; `200 "exists"` if the same path and size is already there (so retries are safe); `409` if the path exists with a different size; `400` for a bad path, short body or wrong checksum; `403` if `--upload-user` is set, `Tailscale-User-Login` is not one of them and the request is not from this computer; `411` without a length |

The extension must be a photo or video type, or `X.<media ext>.xmp` for a sidecar. The server never
writes the catalog: `ingest-iphone.sh` ingests the inbox ([upload.py](../../photo-estate/upload.py)).

`GET /api/ingest` reports the inbox and the last run; `POST /api/ingest` runs `ingest-iphone.sh`
(one at a time: `202`, or `409` if a run is going) and `POST /api/restart` relaunches the app, but
only when launchd started it (else `409`). The Library info page (`/stats`) has the buttons for
these. Both POSTs take the write rule, and their body, if any, is not looked at: the script and its
arguments are fixed.

`GET /api/ingest` returns `{enabled, running, started_at, finished_at, exit_code, log: [last 300
lines], restart_needed, waiting: {files, bytes}, can_restart}`; `{"enabled": false}` with `--demo`.

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
| `shots` | | `0` to hide screenshots, told from size, camera and name as the iPhone does (default `1`: show). Browsing and filtering only: a described search or More like this still finds them |

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
| `GET /api/periods?...` (same filter parameters, plus `dedupe`, `shots`, `sort`) | The Library's Years and Months: `{"years": [...], "months": [...], "matching": N, "total": N}`, each card `{"key": "2019" or "2019-07", "n", "at", "cover", "ar", "places": [...]}`; `at` is where the card's photos start in `/api/search` with the same parameters (a `cursor`) |
| `GET /api/layout?...` (same filter parameters, plus `sort`) | One byte per photo, in `/api/search`'s order with the same parameters: its aspect ratio between 0.5 and 2.6 in 255 steps, so the page can lay a month out before fetching it. `application/octet-stream` with an `ETag` |
| `GET /api/info` | `{"total", "model", "m0", "trips": [{id, title, start, end, n, cover}], "demo", "faces", "facets", "months", "fav", "matching"}`: the unfiltered facet payload merged in. `months` is the list of per-month counts, so the number of months is its length and `m0` is the first month as `year*12 + month - 1` |
| `GET /stats` | The library info page ([stats.html](../../photo-estate/stats.html)) |
| `GET /api/stats[?refresh=1]` | `{state: working\|ready\|error, library, disk, app}`: library figures from memory, disk figures (catalog tables, cache folders, free space) measured once in a background thread and saved to `stats-cache.json` next to the catalog; `refresh=1` recomputes (the page's Recompute button); `catalog_assets_now` lets the page flag a stale measurement. Code in [stats.py](../../photo-estate/stats.py), which also runs standalone |
| `GET /api/sync?epoch=E&since=V[&after=SHA][&limit=N]` | The catalog as a change feed, for a device that keeps its own copy (the iPhone app; [sync.py](../../photo-estate/sync.py)): `{epoch, photos: [rows as in /api/search, plus dup], deleted: [shas], since, after, more, total, trips}`. Rows come in (version, sha) order; send `since` and `after` back to continue, until `more` is false. An unknown `epoch` starts from the beginning. `503` with `Retry-After` while the feed is being built after a start |
| `GET /api/log?ev=open\|share&sha=...&pos=N` plus the search's parameters | Appends an event to the query log; `204` |
| `GET /api/layout?...` (same filter parameters, plus `sort`) | Every matching photo's shape, one byte each, in `/api/search`'s order: the aspect ratio between 0.5 and 2.6 in 255 steps (`application/octet-stream`, with an `ETag`). The page lays a month out from it before it knows which photos are in it |
| `POST /api/favorite {sha, fav}` | Makes a photo a favourite, or not: `{sha, fav, favourites}`. Takes the write rule; `403` otherwise |

### Albums and change sets

Answered by [changes.py](../../photo-estate/changes.py) (chapter 26). Every `/api/changes` address
also answers under `/api/albums`; `/api/changes` lists only the albums that are up for hiding.

| Request | Does |
|---|---|
| `GET /changes`, `/changes/<id>` | The review page ([changes.html](../../photo-estate/changes.html)) |
| `GET /api/albums` | Every album, newest first |
| `GET /api/albums/<id>/photos?cursor=&limit=` | Its photos as `/api/search` gives them, each with its `section` and `reason` |
| `GET /api/albums/<id>/download` | The album's original files as one zip, written as it is read |
| `POST /api/albums {title, prompt?, query?, items?}` | A new album; with `query`, a live one (whatever that search finds) |
| `POST /api/albums/<id>/items {items?: [{sha, section?, reason?, score?}], remove?: [sha]}` | Add photos, or take them out |
| `POST /api/albums/<id>/rename {title?, prompt?}` | Change its title or note |
| `POST /api/albums/<id>/query {query}` | What a live album searches for |
| `POST /api/albums/<id>/favorite {fav}` | Make every photo in it a favourite, or not |
| `POST /api/albums/<id>/hide`, `/view` | Put it up for hiding (it becomes a change set), or take it down again |
| `POST /api/albums/<id>/discard` | Forget it (it must hide nothing) |
| `GET /api/changes/<id>`, `/api/changes/<id>/check` | One set and its photos; whether every photo is where the set says it should be |
| `POST /api/changes {title, prompt?, op?, items}` | Propose a set |
| `POST /api/changes/<id>/decide {shas, decision}` | Mark photos `accept`, `reject` (keep) or `pending` |
| `POST /api/changes/<id>/apply {through?}` | Hide every photo not marked reject, and put back those that are; with `through`, only as far as that photo in the page's order |
| `POST /api/changes/<id>/revert`, `/commit`, `/reopen`, `/discard` | Undo everything the set did; mark it done; make a done set changeable again; forget a set that hides nothing |

### Agents: MCP and tasks

| Request | Does |
|---|---|
| `POST /mcp` | The [MCP](https://modelcontextprotocol.io/) endpoint ([estate_mcp.py](../../photo-estate/estate_mcp.py)): streamable HTTP in its simplest form, one JSON-RPC message (or a batch) per POST, answered with JSON, no stream and no session. Tools: `library_overview`, `search_photos`, `similar_photos`, `get_photos`, `list_albums`, `get_album`, `create_album`, `amend_album`, `propose_hiding`, `list_change_sets`, `get_change_set`, `create_change_set`, `amend_change_set`, `submit_task`, `get_task`, `cancel_task` |
| `GET /api/tasks` | Whether an agent can run on this computer, and every task, newest first ([tasks.py](../../photo-estate/tasks.py)) |
| `GET /api/tasks/<id>?after=N` | One task, its events after event N, and the change set it made |
| `POST /api/tasks {prompt}` | Start one: Claude Code, run headless with only the library's MCP tools |
| `POST /api/tasks/<id>/cancel` | Stop a task that is queued or running |

### Bringing photos in (the setup page)

Answered by [intake/api.py](../../photo-estate/intake/api.py), both by the full app and by the small
setup server that runs before there is a library. In the full app every POST takes the write rule (a
job reads any folder on this computer); the setup server, which runs before there is a library to
protect, has no rule of its own.

| Request | Does |
|---|---|
| `GET /setup` | The setup page ([setup.html](../../photo-estate/setup.html)) |
| `GET /api/intake` | The library, every source (with its guide and checks), every job |
| `GET /api/intake/jobs/<id>?after=N` | One job, with the lines it printed after event N |
| `POST /api/intake/library {root}` | Where the library lives (setup only, before anything is in it) |
| `POST /api/intake/preview {source, config}` | What that would bring in; changes nothing |
| `POST /api/intake/jobs {source, config}` | Start bringing it in |
| `POST /api/intake/jobs/<id>/answer {value}` | What a waiting job asked for (a password, a code, "ok") |
| `POST /api/intake/jobs/<id>/cancel`, `/retry` | Stop a job, or run it again |
| `POST /api/intake/choose {what: folder or file}` | The Mac's own Open dialog (only from this Mac) |

### Images

| Path | Returns |
|---|---|
| `GET /thumb/<sha>` | 360 px JPEG |
| `GET /preview/<sha>` | 1600 px JPEG |
| `GET /large/<sha>` | 4096 px JPEG |
| `GET /original/<sha>` | The stored file, for the stills a phone can save (`.jpg`, `.jpeg`, `.png`, `.heic`, `.heif`, `.gif`, `.webp`, `.tif`, `.tiff`, `.avif`; `ORIGINAL_TYPES` in [core/media.py](../../photo-estate/core/media.py)), with that type's `Content-Type` and `Content-Disposition: inline; filename*=UTF-8''<original name>`; 404 for RAW, BMP and video |

All four validate `<sha>` as 64 lowercase hex characters of a known photo (else 404) and are served
`public, max-age=2592000, immutable`.

### Comparing two models (an experiment, `serve --compare-vectors DIR`)

`DIR` holds a second model's vectors as `tools/embed_squares.py` writes them: `shard-NNNN.npy` (float16,
unit rows), `shard-NNNN.shas.txt` and `meta.json` (`model`). Without the option these routes are 404.

| Path | Returns |
|---|---|
| `GET /compare` | `compare.html`: a search, the two models' results in unlabelled columns in random order, and a vote |
| `GET /api/compare/info` | `{a: {model, photos, dim}, b: {model, photos, dim, ready, meta}, both, votes: {a, b, same}}`; `b.ready` is false while the second model's text side loads |
| `GET /api/compare?q=&k=48` | `{q, text, applied, candidates, a, b, overlap}`. `q` is read as `/api/search` reads it: names, places and `key:value` filters choose the photos, the words left over (`text`) are ranked. Both sides rank the same photos (those with a vector from both models), and each of `a` and `b` is `{model, text_ms, rank_ms, results: [{id, score}]}`, the top `k` (at most 200) with no match floor. 400 when there are no words to rank by; 503 while the second model loads |
| `POST /api/compare/vote {q, winner: a\|b\|same, left, k}` | Appends a line to the votes file (`--compare-votes`, default `DIR/votes.jsonl`) and returns `{ok, votes}`. Takes the write rule (a `--upload-user` login or this computer, else 403), though it writes only that file |

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

The three POSTs take the write rule in the photo app (`403` for a viewer); `faces.py serve`, run by
hand on this computer, has no rule.

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
| 201 | `PUT /api/upload/...` stored a new file |
| 400 | A malformed request (a number parameter, a body that isn't JSON, a bad `Content-Length`: above), a malformed review decision, or a refused upload |
| 403 | A write from a caller who may not write (above) |
| 404 | Unknown sha, unknown path, or `/api/similar` with an unknown photo |
| 413 | A JSON body over 8 MB |
| 503 | `/people` before the face data has loaded |
