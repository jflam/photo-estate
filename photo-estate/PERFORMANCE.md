# Photo app performance

How fast the photo app (`embed.py serve` + `search.html`) is, why, what was changed on 2026-11-11,
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

## Open tasks

Each is independent. "Done" means a before/after number on the Mac mini and no change to API answers.

1. **Find the floor for a thumbnail burst.** Of the ~150 ms for a warm screenful, roughly
   80–110 ms is the Python server itself (it takes that long even locally with no proxy). Serve
   `/thumb/` from a minimal async or Rust static-file server on a side port, behind `tailscale serve`
   the same way, and compare with `tools/thumb_burst.py`. Only worth pursuing if it saves well over
   50 ms. Don't change the tailnet policy or publish new ports without asking the owner.
2. **Cache the embedding matrix on the SSD.** Reading the `embeddings` table off the HDD is ~2.7 s
   of startup. Save the float32 matrix and sha list as `.npy` in `--fast-cache`, keyed by
   something cheap that changes when `embed.py run` adds rows (e.g. `count(*)` and `max(rowid)`
   for the model), and fall back to the table when stale.
3. **Pregenerate HEIC previews onto the SSD.** Opening a HEIC photo in the viewer the first time
   takes 0.1–0.5 s. About 46k HEICs × ~0.3 s, ~14 GB at 1600 px. Run it as a low-priority
   background job, not at server startup.
4. **Reduce the server's memory.** ~1.7 GB RSS on a 16 GB machine that's swapping. The per-photo
   `meta` dicts (~30 keys × 101k) are the obvious target: columns in numpy arrays, or tuples, with
   the dicts built only for the photos a response returns.
5. **HTTP/2 to the backend.** Only matters if task 1 shows the proxy's backend connections cost
   something. `tailscale serve` forwards as HTTP/1.1; check whether it can speak h2c to a backend
   before rewriting the server for HTTP/2 (e.g. Hypercorn/ASGI).
