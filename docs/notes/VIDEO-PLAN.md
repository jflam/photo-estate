# Videos and Live Photos: the plan, what was measured, and what was built

Written 2026-11-21, with the work in the `videos` branch. The book describes the result
([chapter 4](../book/04-store-and-ingest.md), [6](../book/06-linking.md),
[10](../book/10-embeddings.md), [11](../book/11-server.md), [13](../book/13-photo-app.md)); this note
keeps the reasoning and the numbers. It closes most of
[issue 20](https://github.com/jflam/photo-estate/issues/20).

## Where it started

Videos were stored, de-duplicated and read by exiftool like photos, and then nothing showed them:

- The app loads only assets that are `kind='image'` and have an embedding, and `embed.py run` embedded
  only images. So no video was in the grid, in search, or in the phones' feed.
- `link` found a Live Photo's clip by its name, to lend it the still's sidecar, and kept no record that
  the two belong together.
- The server could not play a video: no `Range` support, and `/original` read a whole file into memory.

## What the library holds (measured on the Mac mini, read-only)

| | |
|---|---|
| Videos | 19,612: `.mp4` 16,634 (180 GB), `.mov` 2,954 (180 GB), `.3gp` 15, `.avi` 5, `.wmv` 2, `.m4v` 2 |
| Footage | 67 hours in all; the 16,712 videos of four seconds or less are 11 hours of it |
| Codecs | H.264 9,960 · HEVC 9,489 · VP9 151 · Motion JPEG and MPEG-4 a dozen |
| Have Apple's content identifier | 16,174 |
| ... and a still of the same name in the same folder | 16,172, every one four seconds or shorter |
| Share a name with a still but have no identifier | 889: 588 run over ten seconds (old `IMG_1370.MOV` next to an unrelated `IMG_1370.JPG`), 146 are Google `MVIMG_` motion photos |
| Stand on their own, once the clips are paired | 3,427 videos, about 56 hours and 233 GB |

Two things follow. A Live Photo's clip cannot be told by its name alone: 588 real videos would have been
swallowed by a photo that happens to share their number. And the content identifier is an exact mark: no
real video has one. exiftool already stores it for the clip (`Keys:ContentIdentifier`). The still's copy
is in Apple's MakerNotes, which `estate.py exif` leaves out, so the two files are not matched by the
identifier's value: the identifier says "this is a clip", the name says whose. Only 2 marked clips had no
still, so reading MakerNotes to match by value would gain nothing.

How the clips are named, by source: Takeout `IMG_1.HEIC` + `IMG_1.MP4`; the iCloud mirrors
`IMG_1.HEIC` + `IMG_1.MOV` or `IMG_1_HEVC.MOV` (556 of them); the phone's own uploads
`IMG_1-3fa9c1b.HEIC` + `IMG_1-3fa9c1b.MOV` (`UploadRequest.path` gives both files the same stem).

## Live Photos

An Apple HEIC does not contain the motion: a Live Photo is two files. The decision is that **the still is
the photo and the clip is part of it**, never an item of its own.

- `live_pairs(clip, still, how)` records the pair. `estate.py live` fills it, and so does `estate.py exif`
  when it finishes (the marks are exiftool's, and it runs before `embed.py run` in every pipeline).
- A clip is marked by the content identifier (`how='id'`), or by an `MVIMG` name on a video of four seconds
  or less (`how='mvimg'`). Its still is the image of the same name in the same folder, also trying the name
  without `_HEVC` and without Takeout's `(1)`. The folder is looked for in the clip's own source first, then
  in every other: a Takeout is many zips, each a source, and a folder's files are spread over them. (The
  first version asked for the same source and paired 15,084; without it, 16,185.)
- Run on a copy of the real catalog: 16,185 clips paired with 16,095 stills in 5 seconds (16,172 by the
  identifier, 13 `MVIMG`); 90 stills have two clips.
- A paired clip is not embedded, has no thumbnail and is in no list or count. Its still's row carries
  `live: <clip sha>`, and `GET /video/<clip sha>` plays it.
- A marked clip with no still stays an ordinary short video until a later run finds its still.
- When a still has two clips (Takeout's H.264 `.mp4` and iCloud's HEVC `.mov` of the same moment), the
  `.mp4` is the one played: every browser can.

On the page and the phone a Live Photo has a LIVE mark; holding it plays the clip over the still.

## Searching videos: frames through the same model

A video is searched by stills taken from it, embedded by the same SigLIP 2 model as the photos.

**Reading frames.** `video.py` uses PyAV, which carries its own FFmpeg: the Mac mini has no ffmpeg, and
the scripts' inline dependencies stay the only install step. Frames are read by seeking to the key frame
at or before each time, so a 14-minute 8 GB 4K video costs 30 decoded frames (6 s on the mini's USB disk),
not 25,000. One frame per three seconds, at most 30 a video, each at the middle of its stretch.

Tried on 66 of the library's videos chosen to cover every container, codec, rotation and size: all
decoded but two `.3gp` files the disk would not let be read. Phone videos are stored as the sensor saw
them, with a rotation beside them; PyAV gives it per frame (`frame.rotation`, counter-clockwise: exiftool's
`Rotation: 90` is `-90`) and the frames come out upright. An old `.wmv` carried its thumbnail as a second
"video" stream ahead of the real one, so the stream is chosen with FFmpeg's "best", not taken first.

**One vector or many.** Measured on 220 videos longer than 4.5 seconds (2,352 frames), against twenty
plain searches ("a dog", "a birthday cake with candles", "snow", ...), counting a (video, search) pair as
found when it scores at or above the app's floor of 0.085:

| What stands for the video | Pairs found |
|---|---|
| Its best frame | 196 |
| The mean of its frames | 98 |

The mean finds half. A video of a party is cake for ten seconds and faces for the rest, and the average
is neither. So every frame's vector is kept (`video_frames`), and a search scores a video by its best
frame: one extra matrix product over the frames, then `np.maximum.at` onto the videos' scores
(`Catalog.sims`). The result row says where the best frame is (`at`, seconds), and the viewer starts
there. The mean still goes in `embeddings`, as the video's own vector: it puts the video in the same
matrix as the photos, so the grid, albums, "more like this", "Best first" and the phones' feed needed no
second path.

**Which frames to keep.** Half of consecutive frames are more than 0.955 alike. A frame is kept only if
its cosine with the last one kept is under a threshold:

| Threshold | Frames kept | Pairs still found (of 196) |
|---|---|---|
| 0.80 | 17% | 126 |
| 0.88 | 26% | 148 |
| 0.92 | 37% | 165 |
| 0.94 | 47% | 174 |
| **0.96** | **63%** | **190** |

0.96 it is: 3% of matches lost for a third of the memory. For the whole library that is about 23,000
frames (6.7 a video), 70 MB in the app as float32 beside the photos' 330 MB.

**Cost.** 1.4 seconds a video on the mini (0.9 reading frames, 0.5 in the model on its GPU): about 80
minutes for the 3,427 videos, once. Each video is committed whole, so a stopped run loses one.

**What videos do not get.** No dHash (duplicates of a video are exact copies only), no faces, and they
never stack with the photos taken beside them.

## Playing

- `GET /video/<sha>` streams the stored file from the disk in 256 KB pieces and answers `Range` with a
  206, which Safari and AVPlayer require: a player reads the start, then the index at the end, then
  wherever it is dragged to.
- `.mov` and `.m4v` are sent as `video/mp4`. A phone's `.mov` holds H.264 or HEVC in the same boxes an
  `.mp4` does; Chrome refuses the type `video/quicktime` and plays the same bytes as `video/mp4`.
- A video's thumbnail and preview are its poster frame, one second in (the first frame is often black),
  made by the same background job and cached in the same place as a photo's.
- The page plays a video in a `<video>` element, the phone with AVPlayer, both straight from `/video`.
- A video the browser cannot play (the `.avi`, `.wmv` and `.3gp` files; HEVC in a browser without it)
  shows its poster, says so, and offers the file.

## What is left

| | |
|---|---|
| **A 720p proxy** | Originals are streamed as they are: a 4K HEVC file from the phone is about 50 Mbit/s, which a phone away from home may not keep up with. A background job that makes an H.264 720p copy into the cache, like previews, would fix that and the two dozen files no browser plays. It needs a transcoder; PyAV can do it, slowly. Decided to wait until the originals have been tried from the phone. |
| **Faces in videos** | Run the face detector over the same frames. `faces` needs a time column, and one person in thirty frames must count once. |
| **A true Live Photo on the phone** | The phone plays the clip over the still. Saving a real Live Photo back to Photos needs both files there with matching identifiers (`PHAssetCreationRequest` with `.photo` and `.pairedVideo`); Takeout's re-encoded MP4s have lost theirs. |
| **Videos kept on the phone** | The phone streams. Keeping a video for offline use is a storage decision, not a small one. |
| **Motion photos inside a JPEG** | Newer Pixels put the clip inside the JPEG itself. Takeout has split most; none were looked for. |
| **Pairs when a video was embedded first** | A clip paired after it was embedded as a video (its still arrived later) leaves the lists at the next restart, not at the next reload. |

## Putting it on the Mac mini

Nothing changes for anyone until step 3 has run; each step can be run again.

```bash
cd ~/src/photo-estate/photo-estate
DB=~/estate/estate.db STORE=/Volumes/estate/store
sqlite3 "$DB" ".backup '$HOME/photo-estate-lab/estate.db.before-videos'"   # the catalog gains two tables
uv run estate.py --db "$DB" live                 # 1. pair the 16,185 clips with their stills (5 seconds)
uv run embed.py --db "$DB" run --store "$STORE"  # 2. the 3,427 videos' frames (about 80 minutes)
launchctl kickstart -k gui/$(id -u)/com.jflam.photo-search   # 3. restart the app
```

After the restart the background job makes the videos' thumbnails and previews (about a second each),
and the phones are sent the new rows: the videos, and the 16,000 stills that now carry `live`. New
uploads need nothing: `ingest-iphone.sh` already runs `exif` (which pairs) and `embed.py run` (which now
does videos).
