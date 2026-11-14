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
different devices. Ingest and embedding read every original, so they run at the store's speed (a USB
hard disk does about 150-200 MB/s); browsing and search touch only the catalog and cache, which is why
those should be local and fast.

### The author's reference setup

| | |
|---|---|
| Library | About 102,000 photos and 19,000 videos (485 GB) from all three services; about 10,000 duplicates collapsed; about 130 trips found |
| Machine | An M1 Mac mini, 16 GB RAM, running the app as a launchd agent |
| Disk | An 8 TB USB hard disk formatted APFS, mounted `/Volumes/estate` |
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
  icloud/<library>/YYYY/MM/DD/...      the icloudpd mirror (keep it)
  pull.log
~/Library/Caches/photo-estate/         the --fast-cache on the internal SSD (disposable)
~/.cache/photo-estate/buffalo_l/       the face models (downloaded once)
~/.cache/huggingface/                  the SigLIP 2 weights (downloaded once)
```

## The first run, end to end

Run from `photo-estate/`, with `DB` and `STORE` set:

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
| `link` | CPU | Minutes |
| `exif` | `exiftool` | Per-file; progress and ETA are printed |
| `places run` | CPU | **Seconds** |
| `embed run` | CPU (the model) | About 6.8 img/s measured on a laptop: roughly 4-6 hours (PLAN.md says ~4 h for ~99k images; TAGGING-RESEARCH.md measured ~6 h) |
| `faces run` | CPU | "About as fast as the SigLIP pass" |
| `serve` startup | Disk reads of the catalog | 9-17 s on the Mac mini with a USB HDD; 2.4-2.8 s on a laptop SSD |

### Adding photos later

New photos are a cheap increment: run **ingest → link → exif → places → embed** again (and `faces`).
Each does only new work. For Google, request a Takeout of just the newest "Photos from YYYY" folder
and use `PATTERN` to pick up the new zips; for iCloud, re-run `pull-icloud.sh` (the mirror makes it
incremental). Restart the app to load the new photos (the app reads the catalog at startup).

## Running it as a service (macOS)

The photo app is a long-running process; [deploy/MAC-MINI.md](../../photo-estate/deploy/MAC-MINI.md)
is the author's complete guide. The moving parts:

### launchd agent

[deploy/com.jflam.photo-search.plist](../../photo-estate/deploy/com.jflam.photo-search.plist):

| Key | Value | Why |
|---|---|---|
| `ProgramArguments` | `uv run --quiet .../embed.py --db /Volumes/estate/estate.db serve --store /Volumes/estate/store --fast-cache ~/Library/Caches/photo-estate` | The same command you would type |
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

[PERFORMANCE.md](../../photo-estate/PERFORMANCE.md) records the investigation and results, measured on
the Mac mini (M1, 16 GB, catalog on a USB hard disk) from a laptop over Tailscale.

### Where the time went

| Finding | Number |
|---|---|
| Server work per request | API calls 2-12 ms; cached thumbnail 1.5 ms; a text query about 85 ms |
| The real bottleneck | The disk: an uncached thumbnail costs a ~9 ms seek and seeks do not overlap, so 150 cold thumbnails took 0.65-0.92 s at 1, 16 or 64 threads |
| Memory pressure | 16 GB with swap nearly full: the OS page cache cannot keep thumbnails or the catalog warm |
| Startup | Seek-bound SQLite reads, not Python: the `exif` scan 8.7 s, a correlated subquery 5.8 s, embeddings via the primary key 2.7 s |
| The network path | `tailscale serve` forwards each request as HTTP/1.1; the Python server was HTTP/1.0, so every request opened a connection and a thread |

### What changed, and the result

| Change | Before → After |
|---|---|
| `--fast-cache`: thumbnails on the internal SSD, filled from the disk's cache | 150 cold thumbnails: ~1.0 s → 174-186 ms |
| HTTP/1.1 keep-alive, no Nagle delay, gzip, ETag | `/api/search` 150 results: 120 KB → 14 KB; `/api/info` 67 KB → 22 KB |
| Startup reads in table order; covering index `exif_app` | Restart to first answer ~21-26 s → 9-17 s |
| Text encoder loads in a background thread, with a warm-up query | First text query after restart 1.36 s → 0.21 s |

On a laptop (M2 Max, NVMe) the same changes took restart-to-first-answer from 8.1-9.8 s to 2.4-2.8 s,
and `/api/facets` from 8.6 ms to 4.3 ms. API answers are **byte-identical** before and after on a set of
requests: the changes did not alter behaviour.

A Rust rewrite was considered and *not done*: it would not make disk seeks faster, server work is a
few milliseconds per request, and what is left of startup is sequential disk reads.

### Open performance tasks

Each is written up independently in PERFORMANCE.md:

1. A faster static server for `/thumb/` (only worth it if it saves well over 50 ms of a ~150 ms burst).
2. Cache the embedding matrix as `.npy` on the SSD (saves ~2.7 s of startup).
3. Pre-generate HEIC previews onto the SSD (the first open of a HEIC costs 0.1-0.5 s).
4. Reduce the server's ~1.7 GB RSS: the per-photo `meta` dictionaries are the target.
5. HTTP/2 to the backend (only if task 1 shows the proxy's connections matter).

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
> there yet.** ([DISK-SETUP.md](../../photo-estate/DISK-SETUP.md))

Because the store is append-only and content-addressed, backing it up is unusually simple:

| What | How |
|---|---|
| The store to a second disk or NAS | `rsync -a ROOT/ BACKUP/`: only new files are copied each time |
| Off-site, encrypted | `restic` to object storage (Backblaze B2 is about $6/TB/month) |
| The catalog | `sqlite3 estate.db ".backup 'estate.db.bak'"` (the pull scripts do this); also `rsync` it. It is small and holds all the metadata, places and embeddings |
| Detecting corruption | Re-hash every file in `store/` and compare with its name (a snippet is in DISK-SETUP.md). About an hour per 500 GB on a USB hard disk. Works on any filesystem, with or without checksums |
| A filesystem with checksums | btrfs `scrub` monthly, or ZFS |
| Moving an estate | It is just files: `rsync`, then compare file counts, total bytes and the SHA-256 of `estate.db` before erasing the old disk |

What to back up, in priority order: the **store**; the **catalog**; `face-labels.jsonl` and
`review/decisions.json` (your work); everything else can be regenerated.

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
| Disks, filesystems, the overnight pull, integrity check, backups | [DISK-SETUP.md](../../photo-estate/DISK-SETUP.md) |
| Measurements and open tasks | [PERFORMANCE.md](../../photo-estate/PERFORMANCE.md) |
| Library statistics and the benchmark album | [BENCHMARK.md](../../photo-estate/BENCHMARK.md) |
