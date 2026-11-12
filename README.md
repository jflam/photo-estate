# Photo Estate

Every photo and video from Google Photos, iCloud Photos and Flickr, gathered onto one disk at
home, de-duplicated, with each service's metadata kept and searchable by describing a photo in
plain words. Everything runs locally; no photo leaves the house. It's the first step in getting a
lifetime of personal data out of cloud services and into a form I own.

The archive is deliberately boring: **plain files plus one SQLite file.** No app, including this
one, is needed to read it. If every script here disappeared, the photos would still be ordinary
JPEG/HEIC/MP4 files in folders, and the catalog would still be readable with `sqlite3`. Nothing
depends on a particular disk, filesystem, operating system or server: see
[Where it runs](#where-it-runs) for what's required and what's just my setup.

## Try it: the demo estate

`photo-estate/demo/` is a complete little estate you can browse without any photos of your own:
about 100 CC0 photos (Unsplash photos copied to Wikimedia Commons while Unsplash was CC0), run
through the same pipeline as the real thing (ingest, link, exif, places, embeddings), checked in
with the catalog beside them.

```bash
cd photo-estate
uv run embed.py serve --demo        # http://127.0.0.1:8767/
```

The demo uses port 8767, so it can run beside the real app on 8766; it reads nothing outside
`demo/`. On my Mac mini it runs as a second launchd agent (see
[deploy/MAC-MINI.md](photo-estate/deploy/MAC-MINI.md#the-demo)).

The photos were picked to show off search by meaning. They come in groups of eight that look
alike (lighthouses, sunsets over the sea, coffee, kittens, snowy peaks, tulips, hot air balloons,
dunes...), and nothing in the catalog says which group a photo is in. Type "latte art", "kitten"
or "dog in the snow", or open a photo and press **More like this**. Each photo also keeps its
real camera and date, and most their place, so the place and camera filters and the timeline
have something to show. The demo is served from a scratch copy: nothing in the repo is written,
and searches aren't logged. Typed searches need the SigLIP model, which is downloaded from Hugging
Face the first time, as with a real estate.

`demo/photos.json` lists the photos; `demo/CREDITS.md` names each one's author and source.
To rebuild it (after changing the list, say): `uv run tools/make_demo.py`.

## What's in the estate today

| | |
|---|---|
| Sources | Google Takeout (9 × 50 GB zips), two iCloud Photos libraries, Flickr data download |
| Unique assets | ~102k images + ~19k videos, ~485 GB on disk |
| Duplicates collapsed | ~10k byte-identical copies (album copies, the same photo in two services) |
| Dates | 2000–2026 on almost every asset; GPS on ~106k |
| Metadata kept | Google sidecars (date, GPS, description, people, favourites, albums), iCloud XMP (date with timezone, GPS, favourites), Flickr (title, description, albums, tags, geo) |
| Camera data | Full EXIF/XMP/QuickTime dump per asset via exiftool; camera, lens and exposure as columns |
| Places and events | Town, county, state and country per photo; local time with its zone; ~7k events and ~130 trips |
| Semantic search | SigLIP 2 embeddings for every decodable image |

## How it works

```
 Google Takeout zips ─┐
 iCloud (icloudpd)  ──┼─▶ estate.py ingest ─▶ store/ab/cd/<sha256>.<ext>   every unique file, once
 Flickr data zips   ──┘        │                       (content-addressed, never modified)
                               ▼
                         estate.db (SQLite)
                               │
        estate.py link ────────┤  match each service's sidecars to its files; merge onto the asset
        estate.py exif ────────┤  every file's EXIF/XMP/QuickTime tags (exiftool); camera, lens, exposure
        places.py run ─────────┤  place names, local time, events, trips (offline, seconds)
        estate.py dupes/review ┤  exact + near-duplicate report, local review page
        embed.py run ──────────┤  SigLIP 2 image embeddings (Tier 1)
        embed.py serve ────────┘  local search page at http://127.0.0.1:8766/
```

**Ingest** streams each file out of a zip or folder while hashing it. A file whose SHA-256 is
already in the store is recorded as another *occurrence* (source, path, album) of the same
*asset*, and isn't written again. Ingest is resumable, and a zip that's already been ingested is
skipped.

**Link** is where each service's quirks are handled:
- *Google Takeout* puts metadata in JSON sidecars whose names are truncated, numbered and
  sometimes in a different zip from their photo. Live Photo clips share the still's sidecar.
- *iCloud* (via `icloudpd --xmp-sidecar`) gives XMP files whose timestamps carry the UTC offset, so
  an iCloud copy and a Google copy of the same photo land on the same instant.
- *Flickr* ships originals (`<title>_<id>_o.jpg`) and metadata (`photo_<id>.json`) in separate
  zips; they're joined by photo id.

Where sources disagree, Google's sidecar wins, then iCloud's XMP, then EXIF, then Flickr. Link can
be re-run at any time and gives the same result.

**EXIF.** `estate.py exif` runs exiftool over every asset (images, RAW and video) and keeps the
whole tag dump as JSON, minus maker notes and binary blobs. Camera, lens, focal length, aperture,
shutter, ISO, software, and video duration and frame rate are also promoted to columns. Camera
names are normalised ("NIKON CORPORATION / NIKON D300" becomes "Nikon D300") and are searchable.
Needs exiftool (`brew install exiftool` on macOS, `sudo pacman -S perl-image-exiftool` on Arch).

**Places and events.** `places.py run` works out where and when from what's already in the
catalog, with no network: GPS becomes a place name (offline GeoNames, with big cities winning over
their suburbs: the Eiffel Tower is in Paris, not Vanves) and a timezone; UTC times from Google and
iCloud become wall-clock time where the photo was taken; photos are grouped into events (a gap of
3+ hours ends one) and runs of events far from that year's home into trips ("Trip to Denmark and
Belgium"). Photos without GPS borrow their event's place and zone. Everything is recomputed on
each run, in a few seconds.

**Duplicates.** Exact duplicates collapse automatically. Near-duplicates (resized copies,
re-encodes, edits, RAW+JPEG pairs, bursts) are found with a 64-bit perceptual hash and are
*reported only*: `estate.py review` serves a page for deciding what to keep, and records
decisions to a file. Nothing is ever deleted.

**Search** has two layers: SQLite FTS5 over filenames, albums, people, descriptions and tags
(`estate.py search`), and SigLIP 2 embeddings for "describe the photo" search
(`embed.py search`, and the web page).

**The app** (`embed.py serve`, one HTML file) is one search field, a row of filters and a timeline.
Type a description to search by meaning, or start typing a place, person, album, trip or camera
and pick it from the suggestions. Where, People, Albums, Trips, Camera and Favourites narrow
whatever is showing, with live counts; drag across the timeline to pick a date range, or click a
year. With no description the photos are in date order, grouped by month (by day inside a trip or
event). A photo opens full-screen with its local time, place, camera, lens and exposure; every one
of those is a link to the photos that share it, and "More like this" finds look-alikes.
Near-identical copies are hidden unless switched on. The typed filters still work in the search
field: `sunset place:denmark`, `kids album:"Summer 2019"`, `camera:d300`, `date:2010-07`, `is:fav`.

**Query log.** The app appends every search, every further page of results, every "More like this"
and every photo opened or shared from a result list to `query-log.jsonl` next to the catalog
(`--query-log` to move it), one JSON object per line, with the top 20 results each search showed
and the position of each photo opened. It's there to pick evaluation queries from real use and to
see which searches found something; nothing in the app reads it back.

On a phone, use Share → **Add to Home Screen** to get it as an app with its own icon and no
browser bars. In the photo viewer, swipe left and right between photos, swipe down to close, swipe
up for details, and pinch or double-tap to zoom. The share button sends the original file to the
share sheet, so **Save Image** keeps its date and place. If the server can't be reached (on my
phone, usually because Tailscale is off), the app says so instead of showing a browser error.

## Where it runs

The software makes very few assumptions about the machine and disks it runs on. Everything below
the line is a choice you can make differently; my own setup is given as an example.

**What the software needs:**

| Piece | Requirement |
|---|---|
| **Store** (`--store`) | Any directory you can read and write: a USB disk, an internal drive, an SMB/NFS share on a NAS, an `rclone mount` of a bucket. Files are named by their SHA-256 in lowercase hex, so any filesystem works, case-insensitive ones included. Files are only ever added, never changed. |
| **Catalog** (`--db`) | One SQLite file. **Keep it on a local disk, not a network share**: SQLite's locking isn't reliable over SMB or NFS. It's small (about 1 GB for 120k photos, mostly embeddings). |
| **Thumbnail cache** | `review/` next to the catalog by default (`--cache` to move it). About 2.5 GB of small JPEGs for 100k photos, read 150 at a time as you scroll. If the catalog's disk is a spinning one, add `--fast-cache` on an SSD: it fills from `--cache` as photos are viewed. |
| **A machine to run it** | Anything that runs Python through [`uv`](https://docs.astral.sh/uv/): macOS or Linux, Intel or ARM. 16 GB of RAM is enough to serve 100k photos. No GPU needed; SigLIP runs on the CPU. |
| **Remote access** (optional) | The app listens on `127.0.0.1` only. Put whatever you like in front of it: [Tailscale](https://tailscale.com/kb/1312/serve) `serve`, Caddy, nginx, or nothing if you only use it on that machine. |

`--db` and `--store` are separate arguments everywhere, so the catalog and the photos can live on
different devices. The pull scripts default to `ROOT/estate.db` and `ROOT/store` but take `DB=` and
`STORE=` overrides.

**My setups, as examples:**

| | First (Nov 2026) | Now | Planned |
|---|---|---|---|
| Machine | Linux laptop (Arch, Apple M2 Max) | Mac mini (M1, 16 GB), always on | Same Mac mini |
| Store | 8 TB USB disk, btrfs | Same disk, reformatted APFS | UniFi UNAS (network storage), over SMB |
| Catalog + thumbnails | On the USB disk | On the USB disk | On the Mac mini's internal SSD |
| Service | Run by hand | launchd agent | launchd agent |
| Access | `127.0.0.1` on the laptop | `tailscale serve` on my tailnet only | Same |

None of these moves changed the software, only paths and the service wrapper. Moving the store to
the NAS means passing `--store /Volumes/photos/store --db ~/estate/estate.db` instead of putting both
on one disk. Things that do change from one setup to another:

- **Filesystem.** btrfs checksums every block, which is nice for an archive, but macOS can't read
  it; that's why the disk became APFS when it moved to the Mac. Because every file is named by its
  own SHA-256, you can check for silent corruption on any filesystem by re-hashing (see
  [DISK-SETUP.md](photo-estate/DISK-SETUP.md#checking-the-archive)).
- **macOS privacy prompts.** A background service touching an external or network volume triggers
  a one-time "allow access" dialog that someone has to click at the Mac. See
  [deploy/MAC-MINI.md](photo-estate/deploy/MAC-MINI.md).
- **Speed.** Ingest and `embed.py run` read every original, so they're bound by the store's
  throughput (a USB hard disk does about 150–200 MB/s; gigabit SMB about 110 MB/s). Browsing and
  search only touch the catalog and thumbnail cache, which is why those stay local.
- **The pull scripts** were written on Linux. On macOS they need a newer bash than the system's
  3.2 (`brew install bash`), plus `rclone`, `icloudpd` and `sqlite3`.

## Layout

```
photo-estate/
  estate.py            ingest, link, dupes, review, tag (any OpenAI-compatible VLM), search, stats
  embed.py             SigLIP 2 embeddings: run, search, serve
  places.py            Tier 0: place names, local time, events, trips
  search.html          the photo app served by embed.py
  app/                 home-screen app files: manifest, icons (tools/make_icons.py), service worker
  review.html          the near-duplicate review page served by estate.py
  pull-drive.sh        pull Takeout zips from Google Drive one at a time and ingest them
  pull-icloud.sh       mirror iCloud libraries with icloudpd and ingest them
  demo/                ~100 CC0 photos as a ready-made estate (`embed.py serve --demo`)
  tools/make_demo.py   builds demo/ from the list in demo/photos.json
  tests/test_poc.py    end to end on a synthetic Takeout + iCloud + Flickr export
  tools/sigbench.py    SigLIP speed benchmark
  tools/bench_server.py, tools/thumb_burst.py   photo app latency benchmarks
  PLAN.md              the overall plan and phases
  DISK-SETUP.md        choosing and preparing storage, the overnight pull, checking the archive
  deploy/MAC-MINI.md   running the photo app as an always-on service (my Mac mini as the example)
  deploy/*.plist       the launchd agents for that service and the demo
  TAGGING-RESEARCH.md  the tiered tagging pipeline (what comes after embeddings)
  BENCHMARK.md         library stats and the benchmark album
  PERFORMANCE.md       where the photo app's time goes, what was changed, open tasks

ESTATE ROOT/          (wherever you keep it; never committed. Mine: /Volumes/estate)
  store/               every unique file, exactly once
  estate.db            the catalog (can live elsewhere; see Where it runs)
  review/              thumbnail and preview cache for the app
  flickr/, icloud/     raw exports kept as received
```

## Using it

Everything runs with [`uv`](https://docs.astral.sh/uv/); each script declares its own
dependencies, so there is no environment to set up. Run from `photo-estate/`, with `DB` and `STORE`
set to your paths:

```bash
DB=/Volumes/estate/estate.db STORE=/Volumes/estate/store    # mine; use your own
E="uv run estate.py --db $DB"

$E ingest --store $STORE takeout-*.zip          # or a folder, or Flickr zips
$E link                                         # after every batch
$E exif --store $STORE                          # camera/lens/exposure for new assets
uv run places.py --db $DB run                   # places, local time, events, trips
uv run places.py --db $DB events --trips
$E stats
$E dupes                                        # duplicate report
$E review --store $STORE                        # http://127.0.0.1:8765/
$E search "rialto OR olympic"                   # full-text

uv run embed.py --db $DB run --store $STORE     # embed new images
uv run embed.py --db $DB serve --store $STORE   # the photo app: http://127.0.0.1:8766/
```

To keep the app running all the time and reach it from your other devices, see
[deploy/MAC-MINI.md](photo-estate/deploy/MAC-MINI.md). Mine runs on a Mac mini as a launchd agent,
published to my devices only with `tailscale serve`. On Linux the same thing is a systemd user
service plus whatever reverse proxy you like.

Adding a new source follows the same steps every time: **ingest → link → exif → places → embed run**. Each one
only does new work. For Google and iCloud, `pull-drive.sh` and `pull-icloud.sh` wrap the whole
sequence and snapshot the catalog at the end.

Tests (no accounts or models needed):

```bash
uv run tests/test_poc.py
```

## Status and what's next

Done: ingest from all three services, metadata merge, EXIF extraction, places/events/trips, exact and near-duplicate detection,
near-dupe review page, Tier 1 embeddings with `siglip2-base-patch16-256`, the photo app, and
running it as an always-on service on the Mac mini.

Not yet: RAW files (~800) and videos aren't embedded, there are no faces,
and nothing is backed up to a second location (the cloud services are still the backup).
Next for storage is moving the store to a UniFi UNAS on the home network. [TAGGING-RESEARCH.md](photo-estate/TAGGING-RESEARCH.md)
describes the next tiers; [PLAN.md](photo-estate/PLAN.md) has the phases.

**Photo app performance** is written up in [PERFORMANCE.md](photo-estate/PERFORMANCE.md), with open
tasks ready to pick up (each one can be handed to an agent as-is).
