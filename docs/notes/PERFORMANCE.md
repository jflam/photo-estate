# Photo app performance

How fast the photo app (`embed.py serve` + `search.html`) is, why, what was changed (from 2026-11-11 on),
and what is left to try. The open tasks at the end are written so you can hand one to an agent as-is.

## Where the time went

Measured before changing anything, on a full-size catalog (101,602 embedded photos):

- **Server work is small.** API calls take 2–12 ms, a cached thumbnail 1.5 ms, a text query ~85 ms
  on the Mac mini (SigLIP 2 text encoder). With one or two users, the GIL and thread-per-connection
  are not what limits it.
- **The estate disk is a USB hard disk.** A thumbnail that isn't in memory costs a ~9 ms seek, and
  seeks don't overlap: 150 cold thumbnails took 0.65–0.92 s to read on the Mac at 1, 16 or 64
  threads. That was the ~1 s wait for the first screenful.
- **The Mac mini is short on memory** (16 GB, swap nearly full), so the OS page cache doesn't keep
  thumbnails or the catalog warm between uses or restarts.
- **Startup was seek-bound SQLite reads**, not Python: the `exif` scan took 8.7 s (the table is
  705 MB, mostly exiftool's JSON in `data`), a per-photo subquery into `occurrences` 5.8 s, and
  reading embeddings `ORDER BY sha256` through the primary-key index 2.7 s (0.15 s unordered).
- **The browser already uses HTTP/2.** `tailscale serve` handles TLS and HTTP/2, and forwards
  each request to `127.0.0.1:8766` as HTTP/1.1. The Python server was HTTP/1.0, so every forwarded
  request opened a new connection and thread.

A Rust rewrite was considered and not done: it doesn't make disk seeks faster, server work is a
few milliseconds per request, and what's left of startup is sequential disk reads.

## What changed

| Area | Change |
|---|---|
| Cache, HTTP, startup | `--fast-cache DIR`: thumbnails, previews and large views on the internal SSD, filled from `--cache` (the disk's `review/`), newest first at startup or on first request. HTTP/1.1 keep-alive with TCP_NODELAY and a 60 s idle timeout. gzip for JSON and the page. The page gets an ETag and `no-cache`, so browsers revalidate it; images get `immutable`. PyTorch and the text encoder load in a background thread, so the page and filters answer first. |
| Catalog reads | Startup reads tables in order and sorts in memory. A covering index `exif_app` (19 MB, created on first start) holds the 8 EXIF columns the app reads. |
| First query | One dummy text query warms up the encoder, so the first real query isn't ~1 s slower. |

API answers are byte-identical before and after on a set of info, facet, search, text, typed
filter, album and similar requests.

## Before and after

Mac mini (M1, 16 GB, catalog on a USB HDD), measured from a laptop over Tailscale:

| | Before | After |
|---|---|---|
| 150 random cold thumbnails, one HTTP/2 connection (wall time) | ~1.0 s | 174–186 ms |
| 150 warm thumbnails | 136–212 ms | 140–179 ms |
| Restart to first answer | ~21–26 s | 9–17 s (depends on what's still in the page cache) |
| Restart to text search ready | ~26 s | 14–22 s |
| First text query after a restart | 1.36 s | 0.21 s |
| `/api/search`, 150 results on the wire | 120 KB | 14 KB |
| `/api/info` on the wire | 67 KB | 22 KB |

Laptop (M2 Max, NVMe), direct to `127.0.0.1`:

| | Before | After |
|---|---|---|
| Restart to first answer | 8.1–9.8 s | 2.4–2.8 s |
| `/api/facets`, no filter | 8.6 ms | 4.3 ms |
| `/api/search`, filters only | 4.4 ms | 2.9 ms |
| 150 thumbnails, new connection each (wall time) | 86–115 ms | 94–106 ms |
| Memory (RSS) | ~1.66 GB | ~1.7 GB (~1 GB catalog and vectors, ~0.5 GB PyTorch) |

Unchanged: an uncached HEIC preview takes 0.1–0.5 s to decode (pillow-heif); cached ones now come
from the SSD.

## Opening a month in the web app (2026-11-18)

Measured from a laptop whose Tailscale connection to the Mac mini was relayed (DERP, no direct
connection), on the 108k-photo library. `/api/search` answers in 7 ms on the server.

| | Time |
|---|---|
| Click a month card → its photos' data in the page | 0.13–0.30 s (the page showed a heading and nothing else) |
| A month's 150 thumbnails (3.6 MB), all asked for at once, not yet in memory on the server | half done at 5.0 s, all at 6.3 s |
| The same 150, warm | 2.5–2.7 s |
| 30 thumbnails (a screenful) asked for alone | 0.6–0.95 s |

What changed:

- `/api/layout` gives every Library photo's aspect ratio, a byte each, in the order `/api/search` pages
  through (84 KB for the whole library before gzip). The page fetches it with `/api/periods`, so a month
  is laid out the moment its card is clicked: every tile the shape its photo will be, and the photos
  then arrive in their places with nothing moving.
- Grid thumbnails are asked for by the page (`Thumbs` in `search.html`), rows on screen first, rows ahead
  of the screen only while fewer than 6 pictures are on their way. With `loading="lazy"` the browser
  started every thumbnail within ~1250–2500 px of the screen together, and `tailscale serve` shares the
  link between them equally, so the screenful finished with the rest.

Not done: the thumbnails are 360 px JPEGs at quality 82, 24 KB each; a format half that size would halve
what is left. Month and year cards load a 1600 px preview each on top of the thumbnail.

## Reproducing

Work on a local copy, not the live disk (on macOS, a new process reading `/Volumes/...` can trigger
a permission dialog that someone has to click at the Mac). Over SSH, reads don't prompt:

```bash
mkdir -p ~/estate-local && cd ~/estate-local
rsync -a MAC:/Volumes/estate/estate.db MAC:/Volumes/estate/review .      # ~3.8 GB
# a few thousand originals for decode tests; missing ones just 404 for /preview and /large
sqlite3 estate.db "SELECT substr(sha256,1,2)||'/'||substr(sha256,3,2)||'/'||sha256||ext FROM assets
  WHERE kind='image' ORDER BY random() LIMIT 3000" > files.txt
rsync -a --files-from=files.txt MAC:/Volumes/estate/store/ store/

cd photo-estate
uv run embed.py --db ~/estate-local/estate.db serve --store ~/estate-local/store \
  --fast-cache ~/estate-local/fast --no-pregenerate
uv run tools/bench_server.py http://127.0.0.1:8766                           # every endpoint, direct
uv run tools/bench_server.py https://HOST.TAILNET.ts.net:8766 --h2 --quick   # through tailscale serve
uv run tools/thumb_burst.py https://HOST.TAILNET.ts.net:8766 ~/estate-local/estate.db   # cold bursts
```

Local numbers are useful for before/after comparisons; re-measure on the real machine, because
the hard disk and the memory pressure only show up there. To reproduce the proxy locally, put an
HTTP/2 reverse proxy (Caddy, or `tailscale serve`, which needs `sudo tailscale set --operator=$USER`
once) in front of the local server.

## Phone upload to visible (2026-11-14)

`tools/bench_ingest.py` uploads 9 full-size HEICs to a real `embed.py serve` over a synthetic
121k-photo catalog, the way the phone does, and times how long until they are searchable. Laptop SSD;
exiftool not installed, so the exif stage is skipped in every row.

| Change | Visible after last upload | Notes |
|---|---|---|
| Before | 89 s | 60 s quiet period + 25 s `ingest-iphone.sh` + 4 s catalog reload; first thumbnails ~550 ms each |
| Phone sends `X-Batch: <id>/<count>`; the last file starts the run | 28 s | The quiet period is the fallback only |
| Reload at "catalog ready", before faces | 25 s | Faces is ~3 s for 9 photos here; more on the Mac mini |
| Thumbnail made at upload | 25 s | First thumbnails 550 ms → 2 ms each |
| Embedding made at upload, in the app | 19 s | `embed.py run` finds nothing and never loads PyTorch (11 s → <1 s) |
| Incremental catalog reload | 14.6 s | Reload 4.6 s → 2.8 s; identical to a full load (`--check`) |
| `places` writes only changed rows; geocoder loaded only when needed | | A run with nothing new 4.5 s → 2 s; identical output |
| Ingest reuses the HEIC facts the app worked out on upload | **9.7 s** | Ingest 5 s → under 1 s (no second 12 MP decode per photo) |

Where the remaining 9.7 s goes:

| Step | Time | Why |
|---|---|---|
| Upload workers catching up | 2 s | The benchmark sends all 9 at once; a real phone is slower than the workers |
| `places.py run` | 4 s | ~2 s is the work itself over all 121k photos; ~2 s is loading the geocoder and zone shapes for the new GPS photos |
| `ingest`, `link`, `embed` (empty), process starts | ~1 s | |
| Catalog reload | 2.8 s | Mostly Python rebuilding the derived arrays (orderings, facets, the name regex) over all 121k photos |

## Phone upload to visible on the Mac mini: internal SSD vs USB hard disk (2026-11-14)

`tools/bench_ingest.py` on the M1 Mac mini (16 GB; the real app running beside it, ~9.5 GB of swap in
use), the same 121k-photo synthetic estate on the internal SSD and on the 8 TB USB hard disk (APFS).
Catalog, store, inbox and thumbnail cache all on the disk under test; code, Python environments and
models on the SSD, as in production. 9 photos, median of 3 interleaved runs. exiftool installed. The
page cache is not flushed (no sudo), but each run reads a fresh APFS clone of the catalog, which
starts uncached.

| | SSD | USB HDD | HDD penalty |
|---|---|---|---|
| **Before** (e715736): visible after last upload | 89 s | 115 s | +26 s |
| ↳ ingest script | 24 s | 46 s | +22 s |
| ↳ catalog reload | 5.3 s | 8.7 s | +3.4 s |
| **After** 1–6 (laptop-tuned): visible | 13.4 s | 44.5 s | +31 s |
| ↳ embed step with nothing to do | <1 s | 22 s | the whole-library "what has no embedding?" query |
| **After** + `--after-rowid` scoping | 12.6 s | 20.5 s | +8 s |
| **After** + `LIMIT 1` in places | — | **16.0 s** | **+3.4 s** |
| First thumbnails of new photos | 640 ms → 2 ms | 650 ms → 2 ms | |
| App startup catalog load (not on the upload path) | 6.2 s | 14.2 s | +8 s |

What the hard disk punished: queries that touch every row or a whole index to answer a question about
a few photos. Cold on the USB disk, `count(*)`/`NOT EXISTS` over embeddings took ~10 s per pass,
`NOT IN (SELECT sha256 FROM exif)` 19 s, `count(*) FROM exif` 4.7 s; on the SSD each is well under a
second, which is why the laptop never showed them. exif, embed and faces now look up only assets after
the last finished run (`--after-rowid`, kept in `<inbox>.partial/catalogued-rowid`).

Where the hard disk's 16 s goes: ~3 s upload workers catching up (the benchmark sends all 9 at once),
~1-2 s ingest, link and exif, ~5 s places, ~5 s catalog reload, the rest process starts.

## People, the catalog's disk, and GPU memory (2026-11-20)

Three problems on the Mac mini, found together and fixed in PR 101 and PR 102. Measured on the real
library (113,634 photos, 110,020 faces).

### People took 50–68 s to load after every restart

Profiled with the catalog opened read-only:

| | Wall | CPU |
|---|---|---|
| The whole faces load | 41 s | 2.3 s |
| One query, `faces JOIN assets` | 38 s | 1.3 s |
| `faces` alone, with the vectors | 2.2 s | 0.5 s |
| `assets` alone | 1.3 s | 0.2 s |

The join looked up each face's photo by `sha256` somewhere else in `assets`, which meant 120,000 random
reads from the USB hard disk through SQLite's 2 MB page cache. CPU time was small, so almost all of the
wall time was waiting for the disk. Two fixes:

- `face_rows()` in `faces.py` reads `faces`, `assets` and `hidden` front to back and joins them in Python.
  A test checks that it returns the same rows as the join.
- A load writes only what changed. Before the fix, every start rewrote 20 face rows with the name they
  already had and a new `labeled_at`. The cause was `seed()`, which sent a mis-tagged lone face to
  "unknown" instead of to its real label, so `rescore()` treated it as a change. A test now loads an
  unchanged catalog twice and checks that the second load writes nothing.

### The catalog moved to the mini's internal SSD

`estate.db`, `sync-feed.db`, `intake.db` and the small files next to them are now in `~/estate/`. The
photos, uploads, thumbnails and backups stay on the USB disk. A hard disk reads big files front to back
well and small pieces from all over a file badly, and the catalog is read the second way.
[MAC-MINI.md](../../photo-estate/deploy/MAC-MINI.md#the-catalog-is-on-the-internal-ssd) explains how it
was moved and how to move it back. `ingest-iphone.sh` now takes `CACHE`, and the app passes its own
`--cache`, because with the catalog on another disk `faces.py run` would otherwise have started a second
thumbnail cache beside the catalog.

| Loading the faces | Old code | New code |
|---|---|---|
| Catalog on the USB disk | 50–68 s in the app, 41 s alone | 4.7 s the first time, 1.7 s after |
| Catalog on the SSD | 4.1 s | 2.4 s the first time, 1.7 s after; 3.8 s inside the app, where it runs while the search model loads |

At a restart, the page now answers in 7 s (was 22 s) and the catalog reads in 4.4 s (was 18.9 s). The
faces load now spends about 1.4 s of CPU building arrays, so the disk is no longer the limit. Grouping
the unnamed faces takes 3.3–3.9 s in its own thread and was left alone.

### The photo app used 7.8 GB of the mini's 16 GB

Once agentboss and openclaw were removed from the mini, the photo app was its biggest user of memory:
a 7.8 GB footprint, 5.6 GB of which was GPU memory. Since PR 94, both SigLIP models (the app's own and
`/compare`'s large one) had been loaded whole onto the GPU at every start and held there. It went
unnoticed because **GPU memory does not show in a process's RSS on a Mac**: `ps` said 0.5 GB, while
`footprint <pid>` shows the real figure under "IOAccelerator". Two fixes, both in `encoder.py`:

- `/compare`'s model is loaded on demand (`Encoder.ensure`) and let go when it is not needed
  (`Encoder.let_go`). Since PR 102, that means when a run starts and when it finishes, or after
  `--compare-idle` minutes without use (default 15).
- A model used only for text has its image half removed before it goes to the GPU (`drop_image_side`).
  Measured with the base model on an M2 Max laptop, it takes 1,464 MB whole and 1,112 MB without the image
  half, and the query vectors are identical bit for bit. The app's own model keeps its image half, because
  it embeds phone uploads as they arrive.

Measured on the mini after the deploy:

| | Before | After |
|---|---|---|
| App footprint, `/compare` not in use | 7.8 GB | 3.3 GB (3.4 GB after PR 102) |
| GPU memory | 5.6 GB | 1.5 GB |
| GPU memory with `/compare`'s model loaded | 5.6 GB | 4.6 GB |
| GPU memory 15 idle minutes after `/compare` was used | 5.6 GB | 1,465 MB: the model was let go |
| Loading `/compare`'s model when a run starts | at every start of the app | about 6 s |

Typed searches are not affected. The app's own model still loads at startup (about 10 s after a restart)
and stays loaded.

## Open tasks

Each is independent. "Done" means a before/after number on the Mac mini and no change to API answers.

1. **Find the floor for a thumbnail burst.** Of the ~150 ms for a warm screenful, roughly
   80–110 ms is the Python server itself (it takes that long even locally with no proxy). Serve
   `/thumb/` from a minimal async or Rust static-file server on a side port, behind `tailscale serve`
   the same way, and compare with `tools/thumb_burst.py`. Only worth pursuing if it saves well over
   50 ms. Don't change the tailnet policy or publish new ports without asking the owner.
2. **Cache the embedding matrix on the SSD.** *Mostly overtaken: the mini's catalog has been on its SSD
   since 2026-11-20, where the whole catalog reads in 4.4 s. Measure again before doing this.* Reading the
   `embeddings` table off the HDD was ~2.7 s of startup. Save the float32 matrix and sha list as `.npy` in `--fast-cache`, keyed by
   something cheap that changes when `embed.py run` adds rows (e.g. `count(*)` and `max(rowid)`
   for the model), and fall back to the table when stale.
3. **Pregenerate HEIC previews onto the SSD.** Opening a HEIC photo in the viewer the first time
   takes 0.1–0.5 s. About 46k HEICs × ~0.3 s, ~14 GB at 1600 px. Run it as a low-priority
   background job, not at server startup.
4. **Reduce the server's memory.** ~1.7 GB RSS on a 16 GB machine that's swapping. Measure with
   `footprint <pid>`, not `ps`: the 1.5 GB the app's model holds on the GPU is not in RSS (see the
   2026-11-20 section above). The per-photo
   `meta` dicts (~30 keys × 101k) are the obvious target: columns in numpy arrays, or tuples, with
   the dicts built only for the photos a response returns.
5. **HTTP/2 to the backend.** Only matters if task 1 shows the proxy's backend connections cost
   something. `tailscale serve` forwards as HTTP/1.1; check whether it can speak h2c to a backend
   before rewriting the server for HTTP/2 (e.g. Hypercorn/ASGI).
