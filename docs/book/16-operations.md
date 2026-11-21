# 16. Operating it

Everything so far described what the code does. This chapter is about running it: where the pieces
live, what hardware it needs, how long each stage takes, how to keep the app running all the time and
reachable from a phone, how it was made fast, and how to keep the archive safe.

## What it needs

The software "makes very few assumptions about the machine and disks it runs on" (README). The
requirements, by component:

| Piece | Requirement |
|---|---|
| **Store** (`--store`) | Any directory you can read and write: USB disk, internal drive, SMB/NFS share on a NAS, an `rclone mount` of a bucket. Files are only ever added |
| **Catalog** (`--db`) | One SQLite file on a **local** disk (never a network share: SQLite's locking is unreliable there). About 1 GB for 120k photos |
| **Thumbnail cache** | `review/` next to the catalog by default (`--cache`); about 2.5 GB for 100k photos. Add `--fast-cache` on an SSD when the catalog's disk is a hard disk |
| **A machine** | Anything that runs Python through `uv`: macOS or Linux, Intel or ARM. 16 GB of RAM serves 100k photos. **No GPU** |
| **Remote access** (optional) | The app listens on `127.0.0.1` only; put Tailscale `serve`, Caddy, nginx, or nothing in front |

`--db` and `--store` are **separate arguments everywhere**, so the catalog and the photos can live on
different devices: the catalog can sit on an internal SSD while the store stays on a USB hard disk
(`ingest-iphone.sh` takes the store and the catalog from different places for exactly this). Ingest and embedding read every original, so they run at the store's speed (a USB
hard disk does about 150-200 MB/s); browsing and search touch only the catalog and cache, which is why
those should be local and fast.

### The author's reference setup

| | |
|---|---|
| Library | About 102,000 photos and 19,000 videos (485 GB) from all three services; about 10,000 duplicates collapsed; about 130 trips found |
| Machine | An M1 Mac mini, 16 GB RAM, running the app as a launchd agent |
| Disk | An 8 TB USB hard disk formatted APFS, mounted `/Volumes/estate`, for the photos; the catalog is on the mini's internal SSD (`~/estate`) since 2026-11-20 |
| Network | `tailscale serve` publishing `https://<host>.<tailnet>.ts.net:8766` to the author's own devices |
| Earlier | A Linux laptop with a btrfs disk; a NAS is the next move |

## The layout on disk

```
ROOT/                                  e.g. /Volumes/estate
  store/ab/cd/<sha256>.<ext>           every unique file, once, bytes untouched   (irreplaceable)
  estate.db                            the catalog (+ estate.db-wal, -shm while open)
  estate.db.bak-YYYYMMDD               snapshots made by the pull scripts
  review/                              the cache (disposable)
    thumbs/<sha>.jpg   previews/<sha>.jpg   large/<sha>.jpg
    faces/<ab>/<sha>-<idx>.jpg
    thumb-dims.json
    decisions.json                     your near-duplicate choices
  query-log.jsonl                      searches and photos opened
  face-labels.jsonl                    your face decisions (a journal)
  sync-feed.db                         the change feed's versions (chapter 20); safe to delete
  iphone/YYYY/MM/DD/...                the inbox: what the phone uploaded (chapter 24); keep it
  catalog-backups/                     daily copies of estate.db and face-labels.jsonl (--backup-dir)
  icloud/<library>/YYYY/MM/DD/...      the icloudpd mirror (keep it)
  pull.log
~/Library/Caches/photo-estate/         the --fast-cache on the internal SSD (disposable)
~/.cache/photo-estate/buffalo_l/       the face models (downloaded once)
~/.cache/huggingface/                  the SigLIP 2 weights (downloaded once)
```

## The first run, end to end

The app can do all of this itself. `uv run embed.py serve` with no library shows a setup page that
asks where the library should live and where the photos are now, then runs the stages below as one
*intake job* per source and opens the library when the first photos are searchable. A job shows
which of its seven steps it is on ("Step 2 of 7 · Copying them into your library · 9,100 of 15,856"),
names them all, and marks the one after which photos are searchable; a step that can't count says how
long it has been running. Its `link` covers only what the job brought in (`--source`, [chapter 6](06-linking.md)).
Jobs are kept in `intake.db` beside the catalog and resume from their stage after a restart. A job's
state (queued, running, waiting, done, failed, cancelled) changes only by the moves in
`core/states.py`'s `JOB_MOVES`, each one checked and written by `Store.move` in one step, and the
engine claims a queued job the same way, so a job cancelled while it waits its turn is never started;
cancelling a running one stops its step with SIGTERM, then SIGKILL ten seconds later (issue #86). The
setup page stays at `/setup` (the **+** in the library's header) for adding sources later. The code
is in `intake/`, and [INTAKE-PLAN.md](../notes/INTAKE-PLAN.md) explains how sources plug in.

The page itself ([setup.html](../../photo-estate/setup.html)) is two strict state machines, with their
tables in [app/setup-flow.js](../../photo-estate/app/setup-flow.js) (issue #80). One is where the server
is: no library yet (`place`), nothing coming in (`idle`), a job running (`watching`) or waiting for you
(`answering`), then `opening` when the first photos are in and the server is becoming the photo app. The
page asks `/api/intake` only in the first of those (every 1.2 s while a job is live, every 8 s
otherwise, not while the tab is hidden); in `opening` it asks `/api/info` every 1.5 s and goes to the
library when that answers, and after ten minutes says "Still not open" with **Try again**. The other is
the source being added: `open` (what you have typed), `checking`, `checked` (its preview said yes),
`starting`. What is typed and what the preview said belong to those states, so editing a field after a
check goes back to `open` and the job can not be started on a preview of something else.

### How the steps report back: the event lines

The setup page's progress bar and the photo app's live reload are driven by one small protocol, not by
the steps' human-readable output ([core/events.py](../../photo-estate/core/events.py), issue #84; before it,
a reworded log line could stall the bar or the reload, as in issues #12-#14). A parent that wants the
events sets `PHOTO_ESTATE_EVENTS=1` in the child's environment; `intake/engine.py` does for every stage
and `upload.py`'s `IngestJob` does for `ingest-iphone.sh`, which passes it on to the scripts it runs. Without
it nothing changes: a person running a step by hand sees the same lines as before.

Each event is one line on the child's stdout, `@@pe ` followed by a JSON object:

```
@@pe {"v":"1.0","type":"progress","stage":"link","done":2000,"total":14327,"what":"matching each photo with its details"}
@@pe {"v":"1.0","type":"event","name":"new_assets","count":3}
@@pe {"v":"1.0","type":"event","name":"catalog_ready"}
```

| Field | Meaning |
|---|---|
| `v` | `"MAJOR.MINOR"`, now `"1.0"`. A reader takes any `1.x` line and ignores fields it does not know; a line of another major version is ignored |
| `type` | `progress` or `event`; a reader ignores any other type |
| `stage`, `done`, `total`, `what` | `progress`: which step reports (below), how many items it has done, out of how many (left out when it is not known yet), and what it is doing now (optional, shown as the step's detail) |
| `name` and its fields | `event`: `new_assets` (`count`: assets plus XMP sidecars the ingest added; above 0 means the run brought photos in) and `catalog_ready` (the new photos are searchable; faces still to come). A reader ignores names it does not know |

| Who | Prints | Where it used to be only a human line |
|---|---|---|
| `estate.py ingest --progress` | `progress` `ingest`, every 25 photos read (no total: the engine has it from the preview) | `progress N` (now printed only without events) |
| `estate.py link` | `progress` `link` (matching, saving), every 2,000 and at the end | `progress N of M: what` (likewise) |
| `estate.py exif` | `progress` `exif`, after each exiftool batch | `HH:MM:SS N/M F files/s` (still printed) |
| full-text rebuild (`link`, `exif`, `places`) | `progress` `search`, every 1,000 and at the end | `progress N of M: updating search` (only without events) |
| `places.py run` | `progress` `places`, its five steps | `progress K of 5: what` (only without events) |
| `embed.py run`, `faces.py run` | `progress` `embed`/`faces` with `done` 0 when it knows the total, then each minute | `N to do`, `N new images to scan`, `HH:MM:SS N/M R img/s` (still printed) |
| `ingest-iphone.sh` | `new_assets` after the ingest (also `count` 0, before "nothing new"), `catalog_ready` right after the line "catalog ready" | "N new assets and sidecars", "catalog ready" (still printed) |

The parents read the child's stdout with stderr merged into it, so an event comes in order with the lines
around it (`catalog_ready` before the faces step's output). `core.events.parse(line)` is the one reader:
it returns a `Progress` or an `Event`, or `None` for a human line, a malformed one, another major version or
an unknown type, and never raises. Lines starting with `@@pe ` are never shown: the engine turns `progress`
into the job's `done`, `total` and `detail` (through `Store.set`; a total holds for later counts without
one) and writes every other line to the job's log; `IngestJob` keeps every other line in the log the
Library info page shows, and reloads the catalog at `catalog_ready` once a `new_assets` with a count
above 0 came first (at the end of a successful run if `catalog_ready` never comes). `face-labels.jsonl`
is a separate thing, the People page's decision journal, and none of this goes in it.

By hand, run from `photo-estate/`, with `DB` and `STORE` set:

```bash
DB=~/estate/estate.db STORE=/Volumes/photos/store
E="uv run estate.py --db $DB"

$E ingest --store $STORE takeout-*.zip          # or pull-drive.sh / pull-icloud.sh
$E link                                         # attach each service's metadata
$E exif --store $STORE                          # camera, lens, exposure (needs exiftool)
uv run places.py --db $DB run                   # places, local time, events, trips (offline, seconds)
uv run embed.py --db $DB run --store $STORE     # SigLIP 2 embeddings for search
uv run embed.py --db $DB serve --store $STORE   # the app: http://127.0.0.1:8766/
uv run faces.py --db $DB run --store $STORE     # optional: then name people at /people
```

Order matters in only two places: `link` after `ingest`; and `exif` before `places` (which reads the
timezone offsets exif extracts). Every stage can be run again at any time and does only new work.

### How long each stage takes (for a ~100k-photo library)

| Stage | Bound by | Time |
|---|---|---|
| Acquire | Your network | 428 GiB: about 2 h at 500 Mbit/s, 10 h at 100 Mbit/s; ingest overlaps |
| `ingest` | The store's disk (hashing every byte) | Disk speed, 150-200 MB/s on USB HDD |
| `link` | The catalog's disk | Minutes on an SSD. On the Mac mini's USB hard disk a whole-library `link` took 42 min at 135k photos; with `--source` it follows the size of the import |
| `exif` | `exiftool` | Per-file; progress and ETA are printed |
| `places run` | CPU, then the catalog's disk | Seconds on a laptop for a small library; about 10 min on the Mac mini at 135k photos |
| `embed run` | CPU (the model) | About 6.8 img/s measured on a laptop: roughly 4-6 hours (PLAN.md says ~4 h for ~99k images; TAGGING-RESEARCH.md measured ~6 h) |
| `faces run` | CPU | "About as fast as the SigLIP pass" |
| `serve` startup | Disk reads of the catalog | 9-19 s on the Mac mini with the catalog on a USB HDD, 4.4 s with it on the mini's SSD; 2.4-2.8 s on a laptop SSD |
| People (`faces.py`) loading | Disk reads of `faces` and `assets` | 110,020 faces: 50-68 s as one join on the USB HDD (120,000 lookups of a photo by its hash); 4.1 s as that join on the SSD; 4.7 s first time and 1.7 s after as three front-to-back reads, even on the USB HDD |

### Adding photos later

New photos are a cheap increment: run **ingest → link → exif → places → embed** again (and `faces`).
Each does only new work. For Google, request a Takeout of just the newest "Photos from YYYY" folder
and use `PATTERN` to pick up the new zips; for iCloud, re-run `pull-icloud.sh` (the mirror makes it
incremental). By hand, `link --source <folder or zip name>` keeps `link` to the new photos, and the
app needs a restart to load them. An import from the setup page (**+**) does all of this and reloads
the app's catalog in place.

**Phone uploads need none of this** ([chapter 24](24-uploading.md)). The phone tags each file with the batch it belongs to and how
many files that batch has (`X-Batch: <id>/<count>`), so when the last one is in, the app starts
cataloguing at once. Otherwise (an upload failed, or an older app) an upload starts a quiet period
(`--ingest-quiet`, 60 s; 0 turns automatic runs off). Further uploads restart it, but a run starts at the latest
`--ingest-max-wait` (600 s) after the first of a burst. It then runs `ingest-iphone.sh`, and when that
added photos the app re-reads what changed in the catalog in the background and swaps it in, with no restart: searches
keep being answered from the old catalog until the new one is ready. The reload starts as soon as the
script says the photos are searchable (its `catalog_ready` event, after embeddings: see "How the steps
report back" above); faces run on, and the People
page reloads when they finish. A failed run is retried after
10 minutes, up to three times; uploads left in the inbox from before a start are picked up at startup.
`IngestJob` (upload.py) is one state at a time (idle, quiet, starting, running, backoff) and a step its
`MOVES` table does not list raises, rather than being logged and taken anyway; a run started by hand
cancels the quiet-period timer, since that run catalogues whatever the timer was waiting for (issue #86).

## Running it as a service (macOS)

The photo app is a long-running process; [deploy/MAC-MINI.md](../../photo-estate/deploy/MAC-MINI.md)
is the author's complete guide. The moving parts:

### launchd agent

[deploy/com.jflam.photo-search.plist](../../photo-estate/deploy/com.jflam.photo-search.plist):

| Key | Value | Why |
|---|---|---|
| `ProgramArguments` | `uv run --quiet .../embed.py --db ~/estate/estate.db serve --store /Volumes/estate/store --inbox /Volumes/estate/iphone --cache /Volumes/estate/review --fast-cache ~/Library/Caches/photo-estate` | The same command you would type |
| `RunAtLoad`, `KeepAlive` | true | Starts at login, restarts on exit |
| `ThrottleInterval` | 30 s | Stops a crash loop from spinning |
| `EnvironmentVariables.HF_HUB_OFFLINE` | `1` | Never contact Hugging Face once the model is cached; the app should not make network calls at runtime |
| `PATH` | `/opt/homebrew/bin:/usr/bin:/bin` | launchd's PATH is minimal |
| `StandardOutPath` / `StandardErrorPath` | `~/Library/Logs/photo-search.log` | |

```bash
cp photo-estate/deploy/com.jflam.photo-search.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.jflam.photo-search.plist
launchctl kickstart -k gui/$(id -u)/com.jflam.photo-search     # restart after pulling new code
launchctl bootout   gui/$(id -u)/com.jflam.photo-search        # stop it while a long job runs
```

It is a *per-user* agent, so the Mac must be logged in after a reboot (automatic login covers this). On
Linux the equivalent is a systemd **user** service with `loginctl enable-linger`.

### The macOS permission gotcha

The first time launchd starts the agent, macOS asks to let **`uv`** access files on a removable
volume. Until someone clicks **Allow** on the Mac's screen, the process hangs in `open()` on
`estate.db` and uses no CPU. Full Disk Access is not needed. The permission is tied to the exact `uv`
binary, so `uv` is pinned in Homebrew (`brew pin uv`); to upgrade, unpin, upgrade, restart, and click
Allow again. A command to check is in the guide.

### Reaching it from a phone: Tailscale

`tailscale serve --bg --https=8766 http://127.0.0.1:8766` publishes the loopback server to the
author's tailnet over HTTPS, handling TLS and HTTP/2. Because the app has **no authentication of its
own**, the network boundary *is* the access control: it is reachable by the devices on the tailnet and
by nobody else. With a default-deny Tailscale policy, port 8766 needs an explicit grant (and the
guide records one for SSH too). When Tailscale is off on the phone, the service worker (chapter 13)
shows the friendly "Can't reach your photos" page.

### A demo beside the real app

The demo library runs as a second agent on port 8767 from a **git worktree** (`~/src/photo-estate-demo`),
so it can run a different branch from the real app and be shown to anyone without opening the real
library. It copies its catalog to a temp directory, needs no disk permission, and starts in seconds.

## How it was made fast

[PERFORMANCE.md](../notes/PERFORMANCE.md) records the investigation and results, measured on
the Mac mini (M1, 16 GB, catalog on a USB hard disk) from a laptop over Tailscale.

### Where the time went

| Finding | Number |
|---|---|
| Server work per request | API calls 2-12 ms; cached thumbnail 1.5 ms; a text query about 85 ms |
| The real bottleneck | The disk: an uncached thumbnail costs a ~9 ms seek and seeks do not overlap, so 150 cold thumbnails took 0.65-0.92 s at 1, 16 or 64 threads |
| Memory pressure | 16 GB with swap nearly full: the OS page cache cannot keep thumbnails or the catalog warm |
| Startup | Seek-bound SQLite reads, not Python: the `exif` scan 8.7 s, a correlated subquery 5.8 s, embeddings via the primary key 2.7 s |
| The network path | `tailscale serve` forwards each request as HTTP/1.1; the Python server was HTTP/1.0, so every request opened a connection and a thread |
| GPU memory (2026-11-20) | Not in RSS on a Mac (`ps` showed 0.5 GB). `footprint <pid>` showed 7.8 GB, 5.6 GB of it the two SigLIP models held on the GPU |

### What changed, and the result

| Change | Before → After |
|---|---|
| `--fast-cache`: thumbnails on the internal SSD, filled from the disk's cache | 150 cold thumbnails: ~1.0 s → 174-186 ms |
| HTTP/1.1 keep-alive, no Nagle delay, gzip, ETag | `/api/search` 150 results: 120 KB → 14 KB; `/api/info` 67 KB → 22 KB |
| Startup reads in table order; covering index `exif_app` | Restart to first answer ~21-26 s → 9-17 s |
| Text encoder loads in a background thread, with a warm-up query | First text query after restart 1.36 s → 0.21 s |
| The catalog on the mini's SSD; `face_rows()` reads three tables front to back in place of a join (2026-11-20) | Page answers after a restart 22 s → 7 s; People loads 50-68 s → under 4 s |
| `/compare`'s model loaded only for a run; a text-only model drops its image half (2026-11-20) | App footprint 7.8 GB → 3.3 GB; GPU memory 5.6 GB → 1.5 GB |

On a laptop (M2 Max, NVMe) the same changes took restart-to-first-answer from 8.1-9.8 s to 2.4-2.8 s,
and `/api/facets` from 8.6 ms to 4.3 ms. API answers are **byte-identical** before and after on a set of
requests: the changes did not alter behaviour.

A Rust rewrite was considered and *not done*: it would not make disk seeks faster, server work is a
few milliseconds per request, and what is left of startup is sequential disk reads.

### Open performance tasks

Reviewed on 2026-11-20, and written up in PERFORMANCE.md:

1. Pregenerate the viewer's 1600 px previews onto the SSD: only 2,845 of 113,634 are cached, so nearly
   every first open decodes the original from the hard disk (#26).
2. Smaller grid thumbnails (WebP or AVIF): opening a month is now mostly the time to move 24 KB a
   thumbnail (#104).

Overtaken by the move of the catalog to the SSD and by the GPU memory fix: caching the embedding matrix
(#24), reducing the server's memory (#23), a faster static server for `/thumb/` (#25) and HTTP/2 to the
backend (#27).

### Measuring it yourself

```bash
uv run tools/bench_server.py http://127.0.0.1:8766                            # every endpoint, direct
uv run tools/bench_server.py https://HOST.TAILNET.ts.net:8766 --h2 --quick    # through tailscale serve
uv run tools/thumb_burst.py https://HOST.TAILNET.ts.net:8766 ~/estate-local/estate.db   # cold bursts
```

PERFORMANCE.md's advice: work on a *local copy* of the catalog and cache, not the live disk (on macOS a
new process reading `/Volumes/...` can trigger a permission dialog that someone has to click at the
machine); local numbers are good for before/after, but re-measure on the real machine because the hard
disk and memory pressure only show up there.

## Keeping the archive safe

> Until there is a second copy, **the cloud services are still your backup: do not delete anything
> there yet.** ([DISK-SETUP.md](../guides/DISK-SETUP.md))

Because the store is append-only and content-addressed, backing it up is unusually simple:

| What | How |
|---|---|
| The store to a second disk or NAS | `rsync -a ROOT/ BACKUP/`: only new files are copied each time |
| Off-site, encrypted | `restic` to object storage (Backblaze B2 is about $6/TB/month) |
| The catalog | **Built in:** `serve --backup-dir DIR` copies `estate.db` and `face-labels.jsonl` into `DIR` every `--backup-every` hours (default 24), keeping `--backup-keep` (default 7), using SQLite's backup API so the copy is consistent while the app and an ingest keep writing; the files open read-only with no `-shm` beside them. The pull scripts also run `sqlite3 estate.db ".backup 'estate.db.bak'"`. It is small and holds all the metadata, places and embeddings |
| Detecting corruption | Re-hash every file in `store/` and compare with its name (a snippet is in DISK-SETUP.md). About an hour per 500 GB on a USB hard disk. Works on any filesystem, with or without checksums |
| A filesystem with checksums | btrfs `scrub` monthly, or ZFS |
| Moving an estate | It is just files: `rsync`, then compare file counts, total bytes and the SHA-256 of `estate.db` before erasing the old disk |

What to back up, in priority order: the **store** (which includes what phones uploaded, once ingested);
the **inbox** `iphone/` until it has been ingested and the store copied; the **catalog**;
`face-labels.jsonl` and `review/decisions.json` (your work); everything else can be regenerated, including
`sync-feed.db` (a lost one costs each phone one full sync) and everything on the phone itself.

### The README's promise, as an operating rule

> Please keep the archive's promise in any change: files are only added, never modified or deleted, and
> the catalog stays readable without this code.

## Privacy and security notes

- **Nothing leaves the machine at runtime.** Models run locally; GeoNames and timezone data ship with
  Python packages; the app makes no outbound calls. Network access is limited to *acquisition*
  (rclone, icloudpd, Takeout) and one-time model downloads (Hugging Face for SigLIP, a GitHub release
  for the face models). `HF_HUB_OFFLINE=1` in the service enforces the former.
- **The app has no login.** It binds to `127.0.0.1`. Do not change that without adding something in
  front of it.
- **The server only serves hash-named files**: every image route validates `^[0-9a-f]{64}$` against
  known photos, so there is no path to read an arbitrary file.
- **Logs are local and optional**: `query-log.jsonl` (disable with `--query-log /dev/null`) and
  `face-labels.jsonl`. The query log can contain a Tailscale user login.
- **Face models are non-commercial.** The InsightFace weights are for non-commercial use only; the
  README states it.

## Where to look

| Topic | File |
|---|---|
| Service guide | [deploy/MAC-MINI.md](../../photo-estate/deploy/MAC-MINI.md) |
| launchd plists | [deploy/](../../photo-estate/deploy/) |
| Disks, filesystems, the overnight pull, integrity check, backups | [DISK-SETUP.md](../guides/DISK-SETUP.md) |
| Measurements and open tasks | [PERFORMANCE.md](../notes/PERFORMANCE.md) |
| Library statistics and the benchmark album | [BENCHMARK.md](../notes/BENCHMARK.md) |
