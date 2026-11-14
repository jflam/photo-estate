# 18. Design decisions and what's next

The previous chapters described what exists. This one steps back: why it is shaped this way, what
was deliberately left out, where the edges are, and where it is going.

## The decisions that shaped it

Each row is a choice where a reasonable alternative existed, and the reason the project went the way
it did.

| Decision | The alternative | Why this way |
|---|---|---|
| **Plain files + one SQLite file** | A photo-manager app's own database; a server such as Postgres | The archive must outlive any application, including this one. SQLite is a single file, readable by any tool for decades, and fast enough once loaded into memory |
| **Content-addressed store** | Folders by date or album | De-duplication, integrity checks and merges for free; no file ever moves when metadata changes |
| **Append-only, never delete** | Auto-remove duplicates | A bug can never lose a photo; backups are `rsync`; trust is easy to explain |
| **Near-duplicates are reported, not merged** | Auto-merge by hash distance | "Same photo" is a judgement; the cost of a wrong merge is a lost photo |
| **Sidecars stored as data, linked later** | Link during ingest | A sidecar may be in a zip that has not arrived; making `link` idempotent removes ordering constraints |
| **Derived tables recomputed from scratch** (`places`, `events`, `trips`) | Incremental updates | Seconds to run; no stale rows possible; rules can change freely |
| **Expensive results stored per model** (`embeddings`, `tags`, `faces.model`) | Overwrite | A better model is an experiment, not a migration |
| **Google Takeout, not an API** | The Photos Library API | The API no longer returns your whole library (since 31 March 2025); Takeout is the only full export |
| **Local models on the CPU** | Cloud vision APIs; a rented GPU | Privacy ("nothing is sent to the cloud") and cost; a one-time overnight job. Measured, not assumed |
| **Brute-force vector search in NumPy** | A vector database; `sqlite-vec`; an ANN index | At 100k × 768 a full scan is a few milliseconds, exact, and has no index to build or keep consistent |
| **Whole library in RAM** | Query the database per request | Filters, facets and counts become vectorised array operations; requests take milliseconds. Costs about 1.7 GB |
| **Read the query before ranking it** | Send everything to the image model | The model cannot know your people or reliably place a photo |
| **A cut-off, not a sorted list** | Always return the top N | An empty result is the right answer to "kangaroo"; the floor and window were measured |
| **Single-file, build-free web pages** | A framework and a bundler | No toolchain to keep alive; a change is a reload; the whole front end can be read in one sitting |
| **Standard-library HTTP server** | FastAPI, Flask, a Rust server | One dependency fewer; load is one household. A Rust rewrite was explicitly considered and rejected as not addressing the real bottleneck (disk seeks) |
| **No authentication; bind `127.0.0.1`** | Accounts, tokens | The network is the boundary: Tailscale identity in front, nothing exposed |
| **`uv run` scripts with inline dependencies** | A package, a virtualenv, Docker | Zero setup; each script carries only what it needs |
| **Names and corrections in the catalog, plus a plain-text journal** | Only in the database | Your work should survive a model change |

## The tagging plan versus what was built

[PLAN.md](../../photo-estate/PLAN.md) and [TAGGING-RESEARCH.md](../../photo-estate/TAGGING-RESEARCH.md)
laid out six tiers. The result differs from the plan in instructive ways:

| Tier | Planned | Built |
|---|---|---|
| 0 Deterministic | Local time, season, sun position, holidays, events, trips, places, device facts | **Yes**: `places.py` and `exif` (time, place, events, trips, camera). Not built: season, sun position, holidays, DBSCAN "named places" |
| 1 SigLIP 2 | Embeddings **plus zero-shot tags** from a vocabulary; `sqlite-vec` | **Embeddings and search: yes**, with NumPy instead of `sqlite-vec` and the base model. Zero-shot tags: no (the embedding search made them unnecessary) |
| 2 RAM++ | A second opinion on tags | No |
| 3 OCR (PP-OCRv5) | Text in documents and screenshots | No (roadmap item 4) |
| 4 Faces (InsightFace) | Detect, cluster, seed from Google names | **Yes**, and far beyond the plan: a time-aware recogniser and a full human-in-the-loop page |
| 5 VLM | Captions for documents and one per event | The tool exists (`estate.py tag`); it is not part of the default pipeline (roadmap item 6) |
| Viewer | **Immich** as a read-only external library | Replaced by the project's own app. The plan hedged that Immich would be "one replaceable view"; the custom app made that unnecessary |

The pattern: specialist tagging was *replaced* by embedding search, and effort went instead into the
interface and into the one thing embeddings cannot do, which is know **your** people.

## Limits and rough edges

Stated plainly so nobody has to discover them.

**Coverage**
- **RAW files** (`.dng`, `.cr2`, `.nef`, ...) are stored and catalogued but not embedded, scanned for
  faces, or thumbnailed (Pillow cannot decode them).
- **Videos** are stored and de-duplicated but not embedded; Live Photo clips are linked to their still.
  Faces are not found in videos.
- **Photos Google or iCloud edited** are only as good as the export: Takeout "Storage saver" copies are
  compressed forever.
- **Sources:** Google, iCloud and Flickr only. Amazon Photos, OneDrive, Dropbox and a phone's camera
  roll would each be a new set of rules in `link`.

**Behaviour**
- **The app reads the catalog at startup.** New photos, new embeddings, and names newly confirmed on the
  People page appear in *search* after a restart. (The People page itself updates live.)
- **Near-duplicate decisions are recorded, not applied.** `decisions.json` has no consumer.
- **The face journal is write-only.** `face-labels.jsonl` is a durable record but has no replay tool,
  so surviving a rescan with a better model is a design intention, not yet a feature.
- **The query log is write-only** too, by design, until a scoring page exists.
- **`places` is all-or-nothing**: it recomputes everything and cannot be partially run. That is cheap
  (seconds), but it means events are renumbered if the rules change.
- **Home is estimated from photos.** A trip needs the "home" cell to be right; a year with few GPS photos
  borrows the nearest year's home.
- **Single process, single writer for corrections.** The People state is guarded by one lock.
  Concurrent corrections from two browsers are serialised, not merged.

**Rough spots in the repository**
- `pull-drive.sh`'s default `PATTERN` is the author's own export name, and it uses GNU `df` flags and
  bash 4 `mapfile`.
- `embed.py search` (command line) encodes the query with one prompt, while the app averages two; scores
  differ slightly.
- The README says the Check page lists faces "least certain first"; the code orders by descending
  score (most likely first).
- `cmd_serve` is a 660-line function; clear in intent, but the first thing to split if the server grows.
- The demo is the only automated check of ranking quality, and it is a few hundred searches by eye, not a
  test.

## Scale: where would it break?

An honest estimate, extrapolating from the measurements in PERFORMANCE.md (the author's library is
about 120k assets):

| Resource | At 100k photos | At 1M photos (estimate) |
|---|---|---|
| Image vectors, float32 in RAM | about 300 MB | about 3 GB |
| Brute-force `M @ q` | milliseconds | tens of milliseconds |
| Server memory | about 1.7 GB | dominated by per-photo metadata dictionaries; the open performance task |
| Startup | 9-17 s from a USB disk | proportional; the `.npy` cache task would help |
| Faces | about 1 KB each in RAM | a few GB for several million faces; scoring is vectorised in chunks of 20,000 |
| Embedding job | about 6 h | about 60 h on the same CPU (so a GPU or bigger machine, once) |

For a household the design has a lot of headroom; the first constraint would be the per-photo Python
dictionaries, not the vector search.

## What's next

The README's roadmap, in its order, with where each would land in the code:

| # | Item | Where it fits |
|---|---|---|
| 1 | **Dates in a search** ("ben 2019", "christmas 2019", "summer 2022" as date filters) | A third reader in `select()` next to names and places: parse years, seasons and holidays into the existing `date`/`year` masks. Today the year goes to the image model, which cannot read dates |
| 2 | **Score search on real queries** | A page that replays the top results of queries from `query-log.jsonl` and lets you mark the wrong ones, giving a score to compare every later change against |
| 3 | **A bigger image model** (SigLIP 2 so400m) | `embed.py run --model ...` already stores per model; the work is re-embedding and re-measuring `MATCH_FLOOR` and `MATCH_WINDOW` |
| 4 | **Text in photos** (OCR) | A new batch job writing text into `tags`; `reindex` already includes tag text |
| 5 | **Videos** | Keyframes through the same embedding path; faces in keyframes |
| 6 | **Event captions** | `estate.py tag` on one photo per event, applied to all its photos; shows up in the full-text index |
| 7 | **Backups built in** | A command that configures and verifies `rsync` or `restic`; the append-only store makes it simple |
| 8 | **Less memory** | The open tasks in PERFORMANCE.md: columns instead of dictionaries; the `.npy` embedding cache |

Beyond the list, the natural continuations that the architecture invites:

- **Apply the near-duplicate decisions**, with a hard-link or a "hidden" flag rather than deleting.
- **Replay the face journal** against a rescan with a newer face model.
- **Write metadata back** as XMP sidecars with `exiftool` (PLAN.md's "phase 2"), so any photo app can
  read what Photo Estate learned.
- **Search-time reading of seasons and holidays**, which `places` already has the dates for.
- **Live reload**: let the server notice new embeddings and faces without a restart.

## The principle to keep

Every decision above serves one sentence from the README, which also serves as the project's contract
with its owner:

> The archive is deliberately boring: **plain files plus one SQLite file.** No app, this one included,
> is needed to read it.

Everything else (the models, the grid, the gestures, the People page) is a *view* of that archive, and
is allowed to be replaced.
