# Sovereign Photo Estate — Plan

Goal: get every photo and video out of Google Photos, de-duplicate them, keep all of Google's
metadata, and add searchable captions/tags using a cheap open model that runs locally. The archive
should be **plain files + one SQLite file**, so no app (Google's or anyone else's) holds it hostage.

## 1. Getting the data out (the hard constraint)

* **The Google Photos Library API no longer works for this.** Since 31 March 2025 it only returns
  media *your app uploaded*; the Picker API needs you to hand-select items. Neither can export
  a whole library.
* **Google Takeout is the only complete export.** At takeout.google.com, choose only *Google
  Photos*, file type **.zip**, and size **2 GB or 4 GB per file**. Small chunks are what make the
  rolling ingest below work on a laptop.
  * Delivery: "send download link by email" is simplest. Links last about 7 days and each file can
    be downloaded a limited number of times. "Add to Drive" counts against your Google storage
    quota, so it only helps if you have room left.
  * Takeout hands back what Google stored. Anything uploaded at "Storage saver" quality comes back
    compressed; there is no way to get the originals back.
  * Expect the classic Takeout mess, which `estate.py` handles: the same photo copied into every
    album folder; JSON sidecars named `X.jpg.json`, `X.jpg.supplemental-metadata.json`, or cut
    short around 51 characters; `X(1).jpg` paired with `X.jpg(1).json`; `X-edited.jpg` sharing
    the original's sidecar; and **sidecars in a different zip from their photo**.

## 2. Storage: rolling ingest, so the laptop never holds the whole library

The laptop has about 160 GB free. The estate itself lives somewhere else:

```
Takeout zip (2–4 GB) ──download──▶ laptop scratch ──ingest──▶ STORE (external SSD / NAS / rclone mount)
                                         └── delete the zip once it's ingested (--delete-after)
```

* Peak local disk use = one zip + the SQLite catalog (tens of MB per 100k photos).
* `STORE` is content-addressed: `ab/cd/<sha256>.<ext>`. Each byte-identical file is written once,
  however many albums or zips it shows up in. Any mounted path works: a USB disk, an NFS/SMB NAS,
  or an S3-compatible bucket (Backblaze B2, MinIO, Garage) mounted with `rclone mount`.
* Ingest can be resumed: zips already ingested are skipped, and a run that crashes picks up where
  it stopped.
* Backups: 3-2-1 applies. The store never changes once a file is written (files are only added),
  so `restic` or `rclone sync` to a second location is cheap.

## 3. De-duplication

| Layer | How | Action |
|---|---|---|
| Exact | SHA-256 of the bytes while they stream out of the zip | Collapsed automatically. One blob, many *occurrences* (zip + path + album) |
| Near | 64-bit dHash, Hamming distance ≤ 5, pigeonhole banding (no all-pairs scan) | **Report only.** Edits, re-compressed WhatsApp copies, bursts. You decide; nothing is ever auto-deleted |

Albums don't get lost when copies collapse: each occurrence keeps its album, so an asset can
belong to many albums.

## 4. Metadata

`link` matches sidecars to media across *all* ingested zips, then merges onto each asset:
the taken-at time (sidecar UTC timestamp first, EXIF `DateTimeOriginal` as fallback), GPS
(`geoData`, then `geoDataExif`), description, favourite flag, tagged people, and albums.
Google's date and location edits exist **only** in the sidecars, so this step is what keeps them.

Later (phase 2): write the merged metadata to XMP sidecars with `exiftool`, so any photo app can
read it without the SQLite file.

## 5. Analysis and tagging: specialists first, all local

Full reasoning, models and bake-off in [TAGGING-RESEARCH.md](TAGGING-RESEARCH.md). In short,
tagging is mostly recognition, which small specialist models do better and far more cheaply than
general vision-language models (VLMs), and they run on this laptop, so nothing leaves the house.

| Tier | What | Runs on |
|---|---|---|
| 0 | Deterministic: local time, season, sun position, holidays, events, trips, places, device facts | every photo, minutes |
| 1 | **SigLIP 2** embeddings: semantic search (`sqlite-vec`) and zero-shot tags from a fixed vocabulary | every photo |
| 2 | **RAM++** open-set tags with confidences, as a second opinion | every photo |
| 3 | **PP-OCRv5** text, only on photos flagged as documents/screenshots/whiteboards | a few thousand |
| 4 | **InsightFace** faces, clusters named from Google's people tags | every photo |
| 5 | **VLM** (Qwen3.5-9B) with Tier 0–4 output in the prompt, only for documents and one photo per event | ~10–20k |

`estate.py tag` already speaks to any OpenAI-compatible endpoint, so it becomes Tier 5 as is.
Every result is stored per model, so any tier can be re-run with a better model later.

## 6. Viewing it (don't build a UI)

**Immich** (self-hosted) has a Google Photos-like UI, maps, CLIP search, and faces. Point it at
`STORE` as a read-only *external library*. The catalog in this repo stays the source of truth;
Immich is one replaceable view of it. (If you only wanted Immich, `immich-go` can import
Takeout zips straight into it. The POC's advantage is an archive that belongs to no app, with
its own de-dupe and tagging you control.)

## Phases

| Phase | Deliverable | Status |
|---|---|---|
| 0 | POC: ingest + exact/near de-dupe + sidecar linking + local VLM tagging + search, tested on a synthetic Takeout | **this folder** |
| 1 | 8 TB external disk; pull the existing Drive Takeout (9 × 50 GB zips) with `pull-drive.sh`, then a "Photos from 2026" delta (see DISK-SETUP.md) | next |
| 2 | Bake-off on the benchmark album; Tiers 0–3 over the whole library; XMP write-back; video keyframes | |
| 3 | Faces; Tier 5 VLM on documents and events; Immich as viewer; restic backups; a periodic Takeout for anything new until you stop using Google Photos | |

## Usage

```bash
# 1. ingest one or more zips (or extracted folders)
uv run estate.py --db estate.db ingest --store /mnt/photos/store takeout-*.zip [--delete-after]
# Flickr: ingest the data-download-N.zip originals AND the account-data <nsid>_<hash>_partN.zip
# (photo_<id>.json: title, description, albums, tags, geo); `link` matches them by photo id
uv run estate.py --db estate.db ingest --store /mnt/photos/store flickr/*.zip
# 2. match sidecars and merge metadata (run again after every batch of zips)
uv run estate.py --db estate.db link
# 3. duplicate report, then review near-duplicates in a local page (records choices only)
uv run estate.py --db estate.db dupes
uv run estate.py --db estate.db review --store /mnt/photos/store   # http://127.0.0.1:8765/
# 4. tag with a local model (llama-server on :8080, or Ollama on :11434/v1)
uv run estate.py --db estate.db tag --store /mnt/photos/store --model qwen3.5-9b --limit 50
# 5. search
uv run estate.py --db estate.db search "paris OR beach"
# Tier 1: SigLIP 2 embeddings (resumable; ~4 h for ~99k images on CPU with the base model)
uv run embed.py --db estate.db run --store /mnt/photos/store
uv run embed.py --db estate.db search "kids at the lake"
uv run embed.py --db estate.db serve --store /mnt/photos/store   # search page: http://127.0.0.1:8766/
uv run estate.py --db estate.db stats
```

Test end to end on a synthetic Takeout (no Google account or model needed):

```bash
uv run tests/test_poc.py
```
