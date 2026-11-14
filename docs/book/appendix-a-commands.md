# Appendix A: command reference

Every command, with every option and its default, taken from each script's `argparse` definitions.
Run from the `photo-estate/` directory. `--db` is a *global* option placed **before** the sub-command.

## estate.py: the catalog tool

```bash
uv run estate.py [--db estate.db] <command> [options]
```

| Command | Arguments and options | Purpose | Chapter |
|---|---|---|---|
| `ingest` | `sources...` (zips or folders); `--store DIR` (**required**); `--force` re-scan sources already ingested; `--delete-after` delete each zip once ingested | Hash and store media; record occurrences, sidecars, XMP, Flickr JSON | [4](04-store-and-ingest.md) |
| `link` | none | Match sidecars to media; merge dates, GPS, people, favourites, albums; rebuild the text index | [6](06-linking.md) |
| `exif` | `--store DIR` (**required**); `--workers N` (default: half the CPUs); `--batch 200`; `--limit 0` (all); `--force` | Read all metadata with `exiftool`; promote camera, lens, exposure | [7](07-places-events-trips.md) |
| `dupes` | `--threshold 5` max dHash Hamming distance; `--limit 20`; `--json FILE` | Exact and near-duplicate report | [8](08-duplicates.md) |
| `review` | `--store DIR` (**required**); `--threshold 5`; `--port 8765`; `--out DIR` (default `<db dir>/review`) | Local page to choose which near-duplicates to keep (records choices only) | [8](08-duplicates.md) |
| `tag` | `--store DIR` (**required**); `--model NAME` (**required**); `--endpoint http://127.0.0.1:8080/v1`; `--limit 0`; `--max-side 768`; `--timeout 300`; `--no-json-mode` | Caption and tag images with a local OpenAI-compatible vision model | [9](09-text-search-and-tags.md) |
| `search` | `query` (FTS5 syntax); `--limit 20`; `--store DIR` (also print blob paths) | Full-text search | [9](09-text-search-and-tags.md) |
| `stats` | none | Summary of the estate | |

Examples:

```bash
E="uv run estate.py --db ~/estate/estate.db"
$E ingest --store /Volumes/photos/store takeout-*.zip
$E ingest --store /Volumes/photos/store --force ~/estate/icloud/PrimarySync
$E link
$E exif --store /Volumes/photos/store
$E dupes --threshold 8 --json near.json
$E review --store /Volumes/photos/store            # http://127.0.0.1:8765/
$E search 'paris OR beach' --limit 50
$E search '"sea stacks"'
```

## embed.py: embeddings and the photo app

```bash
uv run embed.py [--db estate.db] [--model google/siglip2-base-patch16-256] [--threads 8] <command>
```

| Command | Options | Purpose | Chapter |
|---|---|---|---|
| `run` | `--store DIR` (**required**); `--workers 3` decoder processes; `--batch 16`; `--limit 0` | Embed every image with no embedding for this model (resumable) | [10](10-embeddings.md) |
| `search` | `query`; `--limit 15` | Command-line semantic search | [10](10-embeddings.md) |
| `stats` | none | Images embedded per model | |
| `serve` | `--store DIR` (required unless `--demo`); `--demo`; `--port` (default 8766, or 8767 with `--demo`); `--cache DIR` (default `<db dir>/review`); `--fast-cache DIR`; `--no-pregenerate`; `--match-floor 0.085` (`-1` = no floor); `--match-window 0.04` (`2` = show every photo, ranked); `--query-log FILE` (default `<db dir>/query-log.jsonl`) | The photo app | [11](11-server.md) |

```bash
uv run embed.py --db estate.db run --store STORE
uv run embed.py --db estate.db search "kids at the lake"
uv run embed.py --db estate.db serve --store STORE --fast-cache ~/Library/Caches/photo-estate
uv run embed.py serve --demo
```

`--demo` ignores `--db`, `--store`, `--cache` and `--fast-cache`, and sends the query log to
`/dev/null`.

## faces.py: faces and people

```bash
uv run faces.py [--db estate.db] <command>
```

| Command | Options | Purpose | Chapter |
|---|---|---|---|
| `run` | `--store DIR` (**required**); `--cache DIR` (default `<db dir>/review`); `--workers 3`; `--threads 6` onnxruntime threads; `--limit 0` | Detect and embed faces in images not scanned yet | [14](14-faces.md) |
| `stats` | none | Faces found, named by source, waiting | [15](15-human-corrections.md) |
| `serve` | `--store DIR` (**required**); `--cache DIR`; `--port 8768` | The naming page on its own (`http://127.0.0.1:8768/people`). `embed.py serve` also shows it at `/people` once faces exist | [15](15-human-corrections.md) |

## places.py: where and when

```bash
uv run places.py [--db estate.db] <command>
```

| Command | Options | Purpose | Chapter |
|---|---|---|---|
| `run` | `--gap 3.0` hours of quiet that end an event; `--away 100.0` km from home that makes an event part of a trip | Recompute place names, timezones, local time, events and trips; rebuild the text index | [7](07-places-events-trips.md) |
| `events` | `--trips` list trips instead; `--min 1` smallest event; `--limit 40` | List events, newest first | [7](07-places-events-trips.md) |

## Shell scripts

### pull-drive.sh

```bash
./pull-drive.sh ESTATE_ROOT
```

| Variable | Default | Meaning |
|---|---|---|
| `REMOTE` | `gdrive:Takeout` | rclone remote and folder |
| `PATTERN` | the author's export name | Glob of zips to take (**set this**) |
| `STAGING` | `~/estate-staging` | Local scratch for the current zip |
| `DB` | `ESTATE_ROOT/estate.db` | Catalog location |
| `STORE` | `ESTATE_ROOT/store` | Store location |

Needs `rclone`, `sqlite3`, `uv`, bash 4 and GNU `df`. See [chapter 3](03-repatriation.md).

### pull-icloud.sh

```bash
ICLOUD_USER=you@example.com ./pull-icloud.sh ESTATE_ROOT
```

| Variable | Default | Meaning |
|---|---|---|
| `ICLOUD_USER` | (required) | Your Apple ID |
| `LIBRARIES` | `PrimarySync` | Space-separated library names (`icloudpd --list-libraries`) |
| `DB`, `STORE` | as above | |

Needs `icloudpd`, `sqlite3`, `uv`. First run `icloudpd --username "$ICLOUD_USER" --auth-only`.

## tools/

| Command | Purpose |
|---|---|
| `uv run tools/make_demo.py [--cache DIR] [--out DIR]` | Rebuild the demo library from `demo/photos.json` (needs `exiftool` and network) |
| `uv run tools/screenshots.py [--url URL] [--out DIR]` | Retake the README screenshots from the running demo |
| `uv run tools/make_icons.py` | Redraw the home-screen icons into `app/` |
| `T=8 uv run tools/sigbench.py STORE [MODEL]` | Time SigLIP 2 image embedding on CPU |
| `uv run tools/bench_server.py URL [--h2] [--quick] [--store DIR]` | Endpoint latency |
| `uv run tools/thumb_burst.py URL DB` | Cold thumbnail bursts |
| `uv run tests/test_poc.py` | End-to-end test |

## Environment variables that matter

| Variable | Effect |
|---|---|
| `XDG_CACHE_HOME` | Where the face models are cached (default `~/.cache`) |
| `HF_HUB_OFFLINE=1` | Stop `transformers` contacting Hugging Face once the model is cached (the launchd service sets it) |
| `T`, `DT` | `sigbench.py` thread count and torch dtype |

## Typical sequences

```bash
# First time
ingest -> link -> exif -> places run -> embed run -> embed serve [-> faces run -> /people]

# New photos arrived
ingest (new zips) -> link -> exif -> places run -> embed run [-> faces run]; restart serve

# After changing event or trip rules
places run --gap 2 --away 60            # seconds; nothing else needed

# After switching image model
embed --model NEW run; embed --model NEW serve; re-measure --match-floor and --match-window
```
