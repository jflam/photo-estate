# 11. The photo server

`embed.py serve` is the product: a local web server that holds your whole library in memory and
answers every search, filter and thumbnail request in milliseconds. `PhotoApp` in `photo_app.py` sets
it up and sends each request on (`cmd_serve` in `embed.py` builds one and serves it); the work is done
by a handful of modules that all answer from one in-memory catalog of NumPy arrays. This chapter takes
it apart: how the code is laid out, what it loads, how it is laid out in memory, how a request becomes
a response, and the engineering that makes it feel instant even when the photos live on a USB hard
disk.

```bash
uv run embed.py --db estate.db serve --store STORE      # http://127.0.0.1:8766/
uv run embed.py serve --demo                            # http://127.0.0.1:8767/
```

## How the code is laid out

`cmd_serve` began as one function holding the whole app as closures over the arrays it loaded, and
grew to about 1,640 lines that way. It is now split the way the later features (change sets, the
People page, tasks, `/mcp`, `/setup`) were added from the start: **a module per area, each with an
object whose `handle(method, path, qs, body, ...)` answers its own routes**, returning `(status, body,
content type, Cache-Control[, headers])` or `None` when the path is not its own. `PhotoApp.handle` in
[photo_app.py](../../photo-estate/photo_app.py) tries them in turn and returns what comes back as an
`http_util.Response`, which the HTTP handler writes out.

| Module | Object | Answers |
|---|---|---|
| [catalog.py](../../photo-estate/catalog.py) | `load()` → `Catalog`; `Live`, `Gate` | No routes: the library in memory (everything below), and the catalog being served |
| [browse.py](../../photo-estate/browse.py) | `Browse` | `/api/search`, `/api/similar`, `/api/facets`, `/api/periods`, `/api/layout`, `/api/log`; and an agent's searches (`find`, `row`, `faces`, `overview`) for `/mcp` |
| [images.py](../../photo-estate/images.py) | `Images` | `/thumb/`, `/preview/`, `/large/`, `/original/`; thumbnail pre-generation |
| [admin.py](../../photo-estate/admin.py) | `Admin` | `/api/upload/...`, `/api/ingest`, `/api/restart`, `/stats`, `/api/stats`; preparing photos that arrive from the phone; backups |
| [encoder.py](../../photo-estate/encoder.py) | `Encoder` | No routes: SigLIP's text side (and image side, for uploads), loaded in the background |
| [faces.py](../../photo-estate/faces.py) | `People` | `/people`, `/face/`, `/api/people*` ([chapter 14](14-faces.md)) |
| [changes.py](../../photo-estate/changes.py), [tasks.py](../../photo-estate/tasks.py) | `Changes`, `Tasks` | `/changes`, `/api/changes*`, `/api/albums*`, `/api/tasks*` |
| [estate_mcp.py](../../photo-estate/estate_mcp.py) | `Server` | `/mcp` |
| [intake/api.py](../../photo-estate/intake/api.py) | `IntakeAPI` | `/setup`, `/api/intake*` |
| [compare.py](../../photo-estate/compare.py) | `Compare` | `/compare`, `/api/compare*`, only with `--compare-vectors`: a second model's ranking beside the app's own ([chapter 10](10-embeddings.md)) |

What ties the modules together is `PhotoApp` (photo_app.py, about 500 lines): setting up the demo, the
page and the home-screen app files, `/api/info`, `/api/sync`, favourites, album downloads, who may
write, and re-reading the catalog after an ingest or a change set. `cmd_serve` in embed.py is now
four lines: find the library (or serve the setup page), build a `PhotoApp`, `start()` it, `serve()` it.

**The app is an object you can call.** Building a `PhotoApp` reads the catalog and starts no thread;
`start()` begins the background work (the search model, faces, the sync feed, thumbnails, the inbox
sweep, backups). `PhotoApp.handle(Request) -> Response` answers one request in process, so a test
builds a `PhotoApp` on the demo library and calls it directly, with no subprocess and no PyTorch
([tests/test_photo_app.py](../../photo-estate/tests/test_photo_app.py)); `make_server(host, 0)` puts the
real HTTP layer in front of it on a free port. The HTTP layer (`make_handler`) only reads the request,
with a bounded body, and writes the `Response`: the 304 for a matching `ETag`, gzip, the headers. Two
routes stream and stay in it: phone uploads (into the inbox as they arrive) and album downloads (a
zip written as it is read). A `Response` is a frozen dataclass (`status, body, content_type, cache,
headers`); the extra headers it carries (`Retry-After`, `Content-Disposition`, `ETag`) replaced the
handler's old mutable `extra_headers`.

### Who may write

`--upload-user` names the Tailscale logins that may change the library; with none named, everyone who
can reach the app may (the demo, and a library only you use). The app listens on `127.0.0.1` only, so
a request reaches it either through `tailscale serve`, which adds `Tailscale-User-Login`, or from a
program on the Mac itself: the browser there, the headless agent that works on tasks (tasks.py) and
an MCP client such as Claude Code. A request is **local** when it comes from `127.0.0.1` or `::1`
with none of a proxy's headers (`Tailscale-User-Login`, `X-Forwarded-For`, `Forwarded`: `is_local` in
intake/server.py).

Every request that changes the library takes one rule, `Auth.LOCAL_OR_WRITER`: a `--upload-user` login
or a local request. That covers naming, merging and relabelling people (`POST /api/people/label`,
`label-many`, `person`), favourites, albums and change sets, tasks, bringing photos in (`POST
/api/intake/...`), the ingest, restarting, and uploads; and a vote on `/compare` (it writes only a
file beside the second model's vectors, but it is a record kept of what you said). Reading is open to anyone who can reach the
app (`Auth.PUBLIC`), and so is `/mcp`, whose write tools take the same rule. The rules are one table,
`ROUTE_AUTH` in photo_app.py, checked before a request reaches its module; a refused request is `403
{"error": "not allowed"}` (an upload's is admin.py's own `403 not allowed`). Until 2026-11-19 the People
page's writes took no permission at all, and intake, the ingest, restart and uploads took a
`--upload-user` login but refused this Mac's own browser and agent; the other writes already let
local requests in. Local requests are let in because whoever runs a program on the Mac can already
change the catalog's files.

### Malformed requests

A request the app can't read is answered, not raised (`http_util.py`): a query parameter that should
be a number and isn't (`limit` and `cursor` on `/api/search`, `limit` on `/api/sync`, `job` and
`after` on `/api/intake`) is `400 {"error": "limit must be a whole number"}`; a POST body that is not
JSON is `400 {"error": "the body is not JSON"}` (on `/mcp`, JSON-RPC's own parse error, also a 400); a
`Content-Length` that is not a number is a 400; and a JSON body over 8 MB (`http_util.MAX_BODY`) is a
413, answered without reading it, and the connection is closed. An empty body is still `{}`. A month
filter (`from=`, `to=`) outside 1-12 is ignored, like any other unreadable date.

**One state object, not closures.** The modules share the in-memory catalog through one object,
`catalog.Live`, whose `cat` is the `Catalog` being served. A `Catalog` is built once by `load()` and
then only read (a favourite set in the app is the one exception: it is shown in place at once). A
route reads `live.cat` once at the start and uses that catalog to the end; the handler holds `Live`'s
`Gate` for every request except slow ones (uploads, setup, album downloads), and a reload swaps the
new catalog in through the same gate (`Live.install`), so a search never sees half an old catalog and
half a new one. The questions every route asks of the arrays are `Catalog` methods (`combine`,
`facet_counts`, `take`, `periods`, `browse_order`, `cover_of`, `feed_row`, ...).

```mermaid
flowchart LR
    H["PhotoApp.handle"] --> BR["Browse"]
    H --> IM["Images"]
    H --> AD["Admin"]
    H --> OTH["People, Changes, Tasks,<br/>MCP Server, IntakeAPI"]
    BR & IM & AD --> LIVE["Live.cat: the Catalog"]
    OTH -.->|"callbacks: find, cover,<br/>facts, favourite"| LIVE
    BR --> ENC["Encoder"]
    AD --> ENC
    RL["PhotoApp.reload_catalog"] -->|"catalog.load(prev) → Live.install"| LIVE
```

## The shape of it

```mermaid
flowchart TB
    BR["Browser or phone"] -->|"HTTP/1.1 keep-alive"| H["Handler (one thread per connection)"]
    subgraph proc["One Python process"]
      H --> API["/api/search /api/similar<br/>/api/facets /api/info /api/log"]
      H --> IMG["/thumb /preview /large /original"]
      H --> PEO["/people /face /api/people*<br/>delegated to faces.People"]
      H --> PG["/ and PWA files"]
      subgraph mem["In memory, built at startup"]
        M["M: N x 768 float32<br/>image vectors"]
        COL["columns: when, year, month,<br/>fav, event, trip"]
        FAC["facet codes:<br/>country region city camera<br/>lens trip album person"]
        META["meta: per-photo dicts"]
        LRU["LRU of 24 rankings"]
      end
      API --> mem
      TE["text encoder (PyTorch)<br/>loaded in a background thread"] --> API
    end
    IMG --> CACHE[("thumbnail cache on disk")]
    IMG --> STORE[("store: originals")]
    API -.->|"append"| QL["query-log.jsonl"]
    PEO --> DB[("estate.db")]
```

There is no framework. The handler is a `BaseHTTPRequestHandler` subclass with `do_GET` and
`do_POST`, the server a `ThreadingHTTPServer`. A library for one household does not need more, and a
standard-library server is one less dependency. (PERFORMANCE.md records the one place this costs:
about 80-110 ms of a warm 150-thumbnail burst is the Python server itself; a faster static server for
`/thumb/` is an open task.)

## Startup, step by step

Startup does all the expensive work once, so that requests can be cheap. The steps, in order:

```mermaid
flowchart TD
    S0["0\. --demo: copy the committed demo catalog to a temp dir<br/>so the repo is never written to"] --> S1
    S1["1\. embeddings -> sorted by sha -> shas[], index{}, M (N x 768 float32)"] --> S2
    S2["2\. assets + occurrences -> meta[sha]<br/>(first path, albums, people, favourite, size)"] --> S3
    S3["3\. optional tables: exif, places, events, trips, faces<br/>-> more meta fields"] --> S4
    S4["4\. real thumbnail shapes: thumb-dims.json,<br/>else read JPEG headers (16 threads)"] --> S5
    S5["5\. NumPy columns: when, year, month index, lowercase text, fav,<br/>time orders (oldest, newest)"] --> S6
    S6["6\. facets: integer-code every categorical field"] --> S7
    S7["7\. trips list with a cover photo; the /api/info document"] --> S8
    S8["8\. background threads: People (faces), text encoder, thumbnail pre-generation"] --> S9
    S9["9\. build the name and place vocabulary for search; start serving"]
```

### 1. The vectors

```python
rows = sorted(con.execute("SELECT sha256, vec FROM embeddings WHERE model=? AND vec IS NOT NULL", ...))
shas  = [r[0] for r in rows]
index = {s: i for i, s in enumerate(shas)}
M = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float16).reshape(N, -1).astype(np.float32)
```

Every photo gets a row number `i` in this order. **That row number is its identity for the lifetime
of the process**: every array below is indexed by it, and `index` maps a sha back to it. The float16
blobs are concatenated into one buffer and converted to float32 once, so that `M @ q` runs in float32
(NumPy has no fast float16 matmul). For 100,000 photos `M` is about 300 MB.

The query is `SELECT` without an `ORDER BY`, and the sort happens in Python. The comment in the code
explains why: on a spinning disk, walking an index to produce sorted output turns each row into a
seek. Reading the table in storage order and sorting in memory was 2.7 s → 0.15 s.

### 2-3. The metadata

`meta[sha]` is a dictionary per photo, filled from several tables:

| Source | Fields added |
|---|---|
| `assets` (images only) | `ext`, `taken`, `lat`, `lon`, `desc`, `people` (split), `fav`, `w`, `h` |
| `occurrences`, in id order | `path` (the first path seen), `albums` (de-duplicated) |
| `exif` (via covering index) | `camera`, `lens`, `focal`, `focal35`, `fnumber`, `exposure`, `iso` |
| `places` + `events` + `trips` | `local`, `tz`, `place`, `country`, `region`, `city`, `county`, `place_inferred`, `event`, `event_title`, `trip`, `trip_title` |
| `faces` + `persons` | confirmed face names are **added to `people`**, next to Google's tags |

Three small decisions in there:

- The labels the facets use are built by joining the non-empty parts: `city` = "Paris, Île-de-France,
  France", `region` = "Île-de-France, France", `country` = "France". The UI shows the first part and
  greys the rest.
- `optional()` wraps reads of tables that may not exist yet, so the app starts after *just*
  ingest and embed.
- `CREATE INDEX IF NOT EXISTS exif_app ON exif(sha256, camera, lens, focal, ...)` is run on startup
  (once effective). It makes the 8 columns the app needs readable from a 19 MB index instead of the
  705 MB table.

### 4. Real thumbnail shapes

The grid needs each photo's true aspect ratio *as displayed*, but the catalog's `width`/`height` are
not always upright (a portrait phone photo may be stored as landscape with an EXIF rotation). The
thumbnails are always upright, so their real shape is read from the JPEG header by `jpeg_size`,
which scans the first 8 KB for a start-of-frame marker (`FFC0`/`C1`/`C2`) without decoding. Results
are cached in `thumb-dims.json`, so only new photos are read next time. This is where the layout
engine's `ar` (aspect ratio) per photo comes from.

### 5. Columns

The photo metadata is then turned into **columns**: NumPy arrays indexed by photo number, so that a
filter is one vectorised operation over all photos instead of a Python loop.

| Array | Contents | Used for |
|---|---|---|
| `when` | local time, else `taken`, else `""` | Sorting, grouping |
| `year` | integer year | `year:` filter |
| `ym` | month number since the library's first month (`m0`) | Timeline histogram, date range |
| `cols["place" / "camera" / "album" / "person"]` | lower-cased text per photo | Typed `key:value` substring filters |
| `event_of`, `trip_of` | the id or `None` | Scoping to an event or trip |
| `fav` | boolean | Favourites |
| `oldest`, `newest` | photo numbers sorted by time, **undated last either way** | Unranked results |

### 6. Facets

A **facet** is a field you can filter by and count: Where (country, region, city), People, Albums,
Trips, Camera, Lens. Counting how many photos have each value, under the current filters, needs to
be fast, so values are turned into integers:

```mermaid
flowchart LR
    subgraph single["single-valued: one value per photo"]
      A["country, region, city, camera, lens, trip"]
      A --> C1["labels: sorted unique list<br/>ix: label -> int<br/>codes: int32 array of length N<br/>(-1 = none)"]
    end
    subgraph multi["multi-valued: a photo has several"]
      B["album, person"]
      B --> C2["labels, ix<br/>rows: int64 array, one entry per (photo, value)<br/>codes: int64 array, same length"]
    end
```

- **Single** facets are one `int32` code per photo. "Photos in France or Spain" is
  `np.isin(codes, [ix["France"], ix["Spain"]])`.
- **Multi** facets are stored as a **sparse list of pairs**: `rows[k]` is a photo number and
  `codes[k]` the value it has. "Photos in album X" sets `mask[rows[codes == ix["X"]]] = True`.
  Counting is `np.bincount(codes[base[rows]])`.

`select(qs)` turns a query string into **one boolean mask per kind of filter**: a dictionary like
`{"country": mask, "person": mask, "date": mask, "typed": mask, "names": mask, ...}`. Keeping them
*separate* rather than ANDing immediately is what makes live counts possible.

### Live counts: "each facet ignores its own selection"

When you choose Denmark in the Where filter, the list of countries should still show Sweden and
Norway with counts, so you can add them. So each facet's counts are computed under *every filter
except its own*:

```python
def facet_counts(masks):
    for k, f in single.items():
        base = combine(masks, skip=k)                       # everything else applies
        counts[k] = np.bincount(f["codes"][base & (f["codes"] >= 0)], minlength=len(f["labels"]))
    for k, f in multi.items():
        base = combine(masks, skip=None if k == "person" else k)
        counts[k] = np.bincount(f["codes"][base[f["rows"]]], minlength=len(f["labels"]))
    months = np.bincount(ym[combine(masks, skip="date") & (ym >= 0)], minlength=nmonths)
```

Semantics differ deliberately, and the difference is what you feel when using the filters:

| Facet | Choosing several means | Counts computed |
|---|---|---|
| Country, region, city, camera, lens, trip | **Any of them** (OR) | ignoring this facet's own selection |
| Albums | **Any of them** (OR) | ignoring this facet's own selection |
| **People** | **All of them** (AND): photos where they appear together | **including** this facet's selection, so as you add Ben, the list shrinks to the people who appear with Ben |

The timeline's month counts ignore the date selection (so the whole histogram stays visible while you
brush a range), and the Favourites count ignores the favourites toggle.

A whole `/api/facets` call is a few array operations: 4 ms on a laptop for the whole library, even for
a 100k-photo catalog.

### 7. Trips and `/api/info`

The trips list orders trips by start (newest first) and picks each a **cover**: the middle favourite
photo of the trip, or the middle photo if there are no favourites. `/api/info` is one JSON document:
total, model, the first month (`m0`, as `year*12 + month - 1`), trips, whether faces exist, whether this is
the demo, and the unfiltered facet counts including the per-month histogram (which also serve as the
page's first facet state; the number of months is the histogram's length).

### 8. Background threads

Four things start in threads *after* the arrays are built, so the page and filters answer first:

| Thread | Does | Why in the background |
|---|---|---|
| People | `from faces import People; People(...)` | Loading all faces and scoring them takes seconds; only `/people` needs it. Until it is ready, `/people` answers 503 "Faces are still loading" |
| Text encoder | `import torch`, load SigLIP, then one `"warm-up"` encode | Importing PyTorch is **most of startup time** and only typed queries need it. The warm-up takes the ~1 s first-forward-pass penalty away from the first real query |
| Thumbnail pre-generation | Make every missing 360 px thumbnail | So scrolling never waits for HEIC decoding |
| Sync feed | `Feed(...)`, then `feed_update()` | Compares the loaded catalog with the change feed the iPhone app syncs from (chapter 20); until it finishes, `/api/sync` answers 503 |

A typed search that arrives before the encoder is ready simply blocks on `Encoder.ready.wait()`.
Measured on the author's Mac mini with a 100k library: first answer in 9-17 s after restart (most of
it reading the catalog off a USB disk), typed search ready 14-22 s.

## How a request becomes a response

The core is `/api/search` (and `/api/similar`, which shares the code).

```mermaid
sequenceDiagram
    participant B as Browser
    participant H as Handler
    participant Sel as select(qs)
    participant R as ranked()
    participant C as LRU cache (24)
    participant T as take()
    participant Q as query log

    B->>H: GET /api/search?q=...&country=France&from=2019-06&limit=150
    H->>Sel: parse the query string
    Sel-->>H: masks (per filter), free text, applied typed filters, scope, names, places
    H->>H: mask = AND of all masks, sort = old if scoped to an event or trip, else new
    H->>C: key = (path, sha, text, sort, all params except cursor/limit/dedupe)
    alt cached
      C-->>H: the full ranked order
    else
      H->>R: compute
      Note over R: no text: filtered photos by time<br/>text: M @ text_vec, cut by floor and window<br/>similar: M @ M[sha], not itself
      R-->>C: store
    end
    H->>T: take(order, cursor, limit, dedupe)
    T-->>H: results, hidden count, next cursor
    H->>Q: append one JSON line
    H-->>B: {results, next, hidden, total, matching, text, filters, people, places, scope, sort, ms}
```

Points worth knowing:

- **Cursor pagination over a cached ranking.** The first request computes the *entire* ranked order
  (a NumPy array of photo numbers) and caches it, keyed by everything that affects it *except* the
  cursor and limit. Later pages (`&cursor=150`) are slices of the same array, so scrolling does no
  recomputation. The cache is an `OrderedDict` LRU capped at 24 entries and also memoises text
  encodings and place masks.
- **`next` is a position, not an id.** `take` returns `pos` when more remain, else `None`; the page
  stops requesting when it is null.
- **`take` implements "hide near-identical shots".** It walks the order from `start`, skipping any
  candidate whose similarity to one of the last 8 kept exceeds 0.97, until `limit` photos are kept,
  and reports how many it hid. It is turned off by `dedupe=0`.
- **The response carries how the query was read** (`people`, `places`, `filters`, `scope`, `text`),
  which the page uses to write the title ("Ben & Matthew", "in San Diego") and offer "Search the words
  instead".
- **`ms`** is the server's own time for the request; the page shows it in the subtitle.
- **Ordering when there is no text.** With nothing to rank by, the results are "what the filters
  select, by time": `by_time[mask[by_time]]`. Newest first by default, oldest first inside an
  event or trip (so a trip reads in order), or whatever `sort=` says.

### The response for one photo

Every result is the photo's `meta` plus its `sha`: `ext`, `taken`, `local`, `tz`, `lat`, `lon`,
`desc`, `people`, `albums`, `fav`, `w`, `h`, `ar`, `path`, `camera`, `lens`, `focal`, `focal35`,
`fnumber`, `exposure`, `iso`, `place`, `city`, `region`, `country`, `county`, `place_inferred`,
`event`, `event_title`, `trip`, `trip_title`. The page needs all of it for the details panel without
another round trip, and the JSON is gzipped (150 results: 120 KB → 14 KB on the wire).

## Serving images

Three renditions of every photo, plus the original, all named by the photo's hash:

| URL | Side | JPEG quality | Purpose |
|---|---:|---:|---|
| `/thumb/<sha>` | 360 px | 82 | The grid |
| `/preview/<sha>` | 1600 px | 82 | The viewer |
| `/large/<sha>` | 4096 px | 90 | The viewer when zoomed or on a big dense display |
| `/original/<sha>` | as stored | n/a | Save / share (the file itself, with its metadata) |

```mermaid
flowchart TD
    REQ["GET /thumb/sha"] --> V{"valid 64-hex sha<br/>known photo?"}
    V -->|no| E404["404"]
    V -->|yes| F{"in the fast cache?"}
    F -->|yes| OUT["read and return"]
    F -->|no| S{"in the seed cache<br/>(the disk's review/)?"}
    S -->|yes| CP["copy to fast cache atomically, return"]
    S -->|no| DEC["decode the original:<br/>draft, upright, RGB, thumbnail(side)"]
    DEC --> W["write temp file, os.replace"]
    W --> OUT
```

- **Two cache tiers.** `--cache` (default `<db dir>/review/`) is the *seed*: it lives next to the
  catalog. `--fast-cache` puts the working cache on a faster disk (the internal SSD) and fills it
  from the seed, falling back to decoding the original only when neither has the file. On a USB hard
  disk a cold thumbnail costs a ~9 ms seek, and seeks do not overlap, so 150 of them took about a
  second however many requests ran at once; with the SSD cache the same screenful takes 174-186 ms
  (PERFORMANCE.md).
- **Immutable URLs.** Names include the hash of the original, which never changes, so responses
  are `Cache-Control: public, max-age=2592000, immutable` (30 days). The browser never asks again.
- **Original files** are served for the stills a phone can save (`.jpg`, `.jpeg`, `.png`, `.heic`,
  `.heif`, `.gif`, `.webp`, `.tif`, `.tiff`, `.avif`: `ORIGINAL_TYPES` in `core/media.py`, with each
  one's MIME type), with a `Content-Disposition` carrying the original file name, so "Save Image"
  yields the real name. RAW, BMP and video are shared as a large JPEG instead (chapter 13). Until
  issue #85 the list was kept by hand here and left out `.heif`, `.tif`, `.tiff` and `.avif`, which
  answered 404 for photos the catalog holds.
- **Hash validation.** Every image route first matches `^[0-9a-f]{64}$` and a known photo: there is
  no way to ask the server for an arbitrary path. The same applies to `/original/`.
- **Pre-generation.** At startup a background pool (`forkserver` start method, so it does not
  `fork()` a process that holds PyTorch threads, `cpu_count - 2` workers) renders every missing
  thumbnail, and first copies what the seed has, newest first. It says "making N thumbnails in the
  background". `--no-pregenerate` turns it off.

## HTTP engineering

All from measurements in [PERFORMANCE.md](../notes/PERFORMANCE.md):

| Setting | Why |
|---|---|
| `protocol_version = "HTTP/1.1"`, idle `timeout = 60` | `tailscale serve` forwards each request as HTTP/1.1; with HTTP/1.0, every request opened a new connection and thread |
| `disable_nagle_algorithm = True` | Headers and body go out as two writes; Nagle would hold the second |
| gzip for JSON and `text/*` over 1 KB, level 5 | JSON and the page shrink 4-6×; JPEGs do not |
| `ETag` and `no-cache` for `/` and PWA files | Browsers revalidate each load, so editing `search.html` shows at once; an unchanged page costs a 304 |
| `request_queue_size = 256`, `daemon_threads` | The default listen backlog is 5; behind an HTTP/2 proxy a screenful of thumbnails arrives as dozens of simultaneous connections and overflowed it (502s) |
| Listen on `127.0.0.1` only | The app has no sign-in of its own; `tailscale serve` in front says who is asking, and `--upload-user` who may write (above) |
| JSON bodies up to 8 MB, read only after `Content-Length` is checked | A body that is too large is refused (413) before it is read, and the connection closed so it is not taken for the next request |

### When a client goes away

A kept-alive connection has two ways to end badly, and both used to print a `ConnectionResetError`
traceback after some phone uploads (about one in ten):

- **The server ignored `Connection: close` on an upload.** The upload handler set `close_connection`
  from whether it had read the whole body, which overwrote what the client had asked for. A client
  that sends `Connection: close` (urllib does, always) then closed its end while the server sat waiting
  for a next request, and if the answer's body was still unread on the client's side, its close was a
  reset. Now a connection stays open after an upload only if the client wants it *and* the body was read:
  `close_connection = client_closes or unread`.
- **A client that leaves is not an error.** A phone that sleeps, a closed tab or a proxy may reset an
  idle connection at any moment, including while the server waits for the next request, inside
  `http.server` where no handler can catch it. So `Server.handle_error` says nothing about a
  `BrokenPipeError`, `ConnectionResetError`, `ConnectionAbortedError` or `TimeoutError`, and prints
  everything else as before. That one hook replaced the `try`/`except` around each handler.

[tests/test_connections.py](../../photo-estate/tests/test_connections.py) checks both with the real app,
including a reset sent on purpose (`SO_LINGER` 0).

## Concurrency

The server is multi-threaded but not multi-process. The relevant locks are small:

- `Encoder.lock` lets one thread at a time use the model.
- `Browse.lru_lock` protects the ranking cache.
- `Browse.qlog_lock` serialises query-log appends.
- `Live.gate` lets requests share the catalog and a reload swap it alone (writers first, so a stream
  of requests cannot starve a reload).
- `People` has its own `RLock`.

NumPy releases the GIL during large array operations, so concurrent searches overlap usefully, and
for one household a handful of simultaneous requests is the realistic load.

## The query log

Every search, every page of results, and every photo opened or shared from a result list is appended
as one JSON object per line to `query-log.jsonl` next to the catalog:

```json
{"ts":"2026-11-13T18:20:04+00:00","ev":"search","params":{"q":"latte art"},
 "text":"latte art","filters":[],"people":[],"places":[],"cursor":0,"matching":8,"ms":91,
 "top":["ab12...","..."],"who":null,"ua":"Mozilla/5.0 ..."}
{"ts":"...","ev":"open","params":{...},"sha":"ab12...","pos":0}
```

- **Purpose:** raw material for choosing evaluation queries from *real* use (roadmap item 2). Each
  search keeps its top 20 results and how it was read; each opened photo keeps the search it came
  from and its position in the results. **Nothing in the app reads it back.**
- **Privacy:** it stays on your machine. The `Tailscale-User-Login` header, if a Tailscale proxy
  sets it, is recorded as `who`, so a household can see whose search it was. `--query-log` moves
  it; `--demo` sends it to `/dev/null`.
- **Robustness:** a write failure prints a warning and never fails the search.
- **What is not logged:** pagination and technical params (`cursor`, `limit`, `dedupe`, `ev`, `sha`,
  `pos`) are stripped from `params`.

The page's `logEvent` calls `/api/log?ev=open|share&sha=...&pos=...` when you open or share a photo
from a result list, which is how "opened" events reach the log.

## Demo mode

`--demo` serves the committed demo library without touching the repository:

1. Copies `demo/estate.db` into a fresh temp directory (deleted at exit via `atexit`).
2. Points `store` at `demo/store` (read-only use).
3. Lets thumbnails be generated into the temp directory.
4. Sends the query log to `/dev/null` and uses port 8767, so it can run beside a real library on 8766.

## Resource use

On the author's Mac mini, serving about 100k photos, the process uses about **1.7 GB** resident
(roughly 1 GB catalog and vectors, 0.5 GB PyTorch). The per-photo `meta` dictionaries (~30 keys ×
100k) are identified as the obvious thing to shrink (PERFORMANCE.md open task 4).

## Serving the phone

Beyond what the web page uses, the server has four routes that exist for the iPhone app
([chapter 19](19-iphone-app.md)); each is a few lines in the handler because the real work is in
modules of their own:

| Route | Does | Chapter |
|---|---|---|
| `GET /api/sync` | A page of the catalog's change feed, from `sync.py` | [20](20-sync.md) |
| `PUT` or `POST /api/upload/...` | Saves a photo into the inbox (`upload.py`); each arrival can start the ingest | [24](24-uploading.md) |
| `GET /api/ingest`, `POST /api/ingest` | The state of, and a manual start of, `ingest-iphone.sh` | [16](16-operations.md) |
| `GET /original/<sha>` | The file as stored: what the web page and the phone share | [13](13-photo-app.md) |

Two more things exist only for phone uploads. **Hot reload** (`PhotoApp.reload_catalog`):
after an ingest the server re-reads what changed *in the background* (`catalog.load` with the last
load's `raw`) and swaps the new catalog in under the gate, clearing the rankings cache, so searches
never wait and nothing needs a restart; it ends by calling `feed_update()`. **Preparing arrivals**
(`Admin.prepare_arrivals`): a photo that has just been uploaded gets its grid thumbnail and its
embedding made in the server's own process, using the model it already holds, so the ingest that
follows has nothing left to do for it.

## Where to look in the code

| Topic | Code |
|---|---|
| Wiring, demo mode, reloads, favourites, who may write | `PhotoApp`, `ROUTE_AUTH` in [photo_app.py](../../photo-estate/photo_app.py); `cmd_serve` in [embed.py](../../photo-estate/embed.py) |
| Startup load | `load` in [catalog.py](../../photo-estate/catalog.py), from `q_emb = ...` to `return Catalog(...)` |
| Facets and live counts | `coded`, `single`, `multi` in `load`; `Catalog.combine`, `Catalog.facet_counts` |
| Mask building | `Browse.select`, `parse`, `read_words`, `view_masks`, `Browse.place_mask` in [browse.py](../../photo-estate/browse.py); `Catalog.person_mask` |
| Ranking and paging | `Browse.search` (its `ranked`), `Browse.remember`, `Catalog.take`, `Catalog.browse_order` |
| Text encoder | `Encoder` in [encoder.py](../../photo-estate/encoder.py) |
| Images and caches | `Images.image`, `Images.pregenerate`, `_thumb` in [images.py](../../photo-estate/images.py); `jpeg_size` in catalog.py |
| Uploads, ingest, restart, `/stats`, backups | `Admin` and `backups` in [admin.py](../../photo-estate/admin.py) |
| HTTP | `make_handler` (`Handler.send`, `answer`, `upload`, `download`), `Server` in photo_app.py; `Response`, `read_body`, `int_param`, `Auth` in [http_util.py](../../photo-estate/http_util.py) |
| Query log | `Browse.record`, `unlogged` in browse.py |
| Benchmarks | [tools/bench_server.py](../../photo-estate/tools/bench_server.py), [tools/thumb_burst.py](../../photo-estate/tools/thumb_burst.py), [tools/bench_ingest.py](../../photo-estate/tools/bench_ingest.py) |
