# Appendix C: glossary

**Album.** A named group of photos. From Google: a Takeout folder with a `metadata.json`. From Flickr:
the albums in a photo's JSON. iCloud's date folders are *not* albums.

**Anchor.** A candidate position on a detector's grid. SCRFD has two per cell at each of three strides.

**ArcFace.** The face-recognition network family (here `w600k_r50`) trained so that cosine similarity
between embeddings measures "same person".

**Asset.** One unique file in the store: one row in `assets`, identified by its sha. Compare
*occurrence*.

**Auto (source).** A face name inferred by `rescore()` from trusted names, not confirmed by anyone. It
is never used as evidence.

**Banding.** Cutting a 64-bit hash into `t+1` pieces so that two hashes within distance `t` must agree
exactly on at least one piece; used to find near-duplicates without comparing all pairs.

**Batch (upload).** The files a phone sends together, named in an `X-Batch: <id>/<count>` header; when the last arrives, the server starts cataloguing at once ([24](24-uploading.md)).

**Best frame.** Of a video's kept frames, the one closest to a search. A video's score in a search is its best frame's, or its mean's when that is higher (`Catalog.sims`), and `at` on its row says where that frame is, in seconds ([10](10-embeddings.md#videos)).

**Bradley–Terry model.** A model of pairwise choices: each item has a score, and the chance A is picked over B is `sigmoid(score A − score B)`. The taste scorer is one ([25](25-best-photos.md)).

**Catalog.** The SQLite file `estate.db`.

**Centroid.** The mean (normalised sum) of a person's trusted face vectors for one year.

**Clip (Live Photo).** The short video, a few seconds long, taken with a Live Photo's still and stored as a file of its own. Told by Apple's content identifier, or a Google `MVIMG` name, plus a still of its name in the same folder (of its own source first, then of any other); recorded in `live_pairs`. A paired clip is never an item of its own: its still carries it (`live`) ([6](06-linking.md#step-2-live-photos-and-motion-photos)).

**Content-addressed.** Stored under a name derived from the content (its SHA-256), so identical bytes
share one file.

**Cosine similarity.** For two vectors of length 1, their dot product: 1 means identical direction, 0
unrelated. Used for image-text matching, "more like this", and face matching.

**dHash (difference hash).** A 64-bit perceptual fingerprint: whether each pixel of a 9×8 grey
thumbnail is brighter than its right-hand neighbour. Similar images have hashes a few bits apart.

**Cursor (sync).** The pair `(since, after)` a device sends to say "I have every row up to this version and hash" ([20](20-sync.md)).

**Embedding.** A fixed-length list of numbers that represents the meaning of an image, a sentence or a
face, such that similar things have similar vectors.

**Epoch (sync).** A random name for one `sync-feed.db`. A device that sees an epoch it doesn't know fetches everything again and drops what the new feed lacks ([20](20-sync.md)).

**Event.** A burst of photos with no gap of more than 3 hours (configurable) between consecutive
photos. Its id is its start time in epoch seconds.

**EXIF.** Metadata a camera writes inside a photo file: camera, lens, exposure, often date and offset.
Read with `exiftool`.

**Facet.** A field you can filter by and count: country, region, city, person, album, camera, lens,
trip.

**FTS5.** SQLite's built-in full-text search engine.

**Frame vectors.** The SigLIP 2 vectors of the frames a video is searched by: one frame every three seconds, at most 30, and of those only each that is under 0.96 cosine of the last one kept. Kept in `video_frames`; their mean is the video's own vector in `embeddings` ([10](10-embeddings.md#videos)).

**Google (source).** A face name from a Google people tag, matched to a face by `seed()`. Trusted.

**Hamming distance.** The number of bits in which two bit strings differ.

**Home.** The place (per year) with photos on the most distinct days; used to decide what is "away".

**icloudpd.** The open-source iCloud Photos downloader the project uses.

**Inbox.** The folder (`iphone/` next to the catalog) where phone uploads land, in the same shape as the iCloud mirror; `ingest-iphone.sh` catalogues it ([24](24-uploading.md)).

**Journal.** The estate's append-only log of what happened, one row of `journal` per change with who did it (`actor`), its `kind` (`member.added`, `member.requested`, `member.changed` so far) and what it is about (`subject`, such as `member:3`). Never changed or deleted; nothing reads it yet. Not the face journal below, nor the `events` tables ([11](11-server.md#members-and-who-may-do-what)).

**Journal (face).** `face-labels.jsonl`: an append-only record of your face decisions, keyed by photo
hash and box.

**Replica.** The phone's read-only copy of the catalog, in `catalog.db`; the server is the only writer ([20](20-sync.md)).

**Live Photo.** A still and a clip taken together by an iPhone (or a Pixel's motion photo). Here the still is the photo and the clip is part of it: one item, searched, scored and counted by its still ([6](06-linking.md#step-2-live-photos-and-motion-photos)).

**Local (request).** One from the computer the app runs on, not through `tailscale serve` (`is_local`). It is the sysadmin ([11](11-server.md#members-and-who-may-do-what)).

**Locality.** The nearer suburb or neighbourhood when a big city was chosen as a photo's `city`.

**Login.** A Tailscale login (`alice@icloud.com`), which `tailscale serve` sends as `Tailscale-User-Login`. Each is one member's; a member may have several ([11](11-server.md#members-and-who-may-do-what)).

**Match floor / match window.** The two constants (0.085 and 0.04) that decide which photos count as
matching a typed search: at least the floor, and within the window of the best match.

**Member.** Someone the estate knows and lets in, a row of `members`: a name, a role, a status (`pending`, `active`, `revoked`; never deleted), an upload policy, and the person in People they are, if linked. Not a person, which is someone *in* the photos ([11](11-server.md#members-and-who-may-do-what)).

**NMS (non-maximum suppression).** Discarding overlapping detector boxes except the best.

**Occurrence.** One place a file was found: a source (zip or folder), a path, and the album that implies.
One asset can have many.

**Pending (face).** A face whose best-matching person scores at least 0.30 but who is not yet named: it
is waiting for your yes or no.

**Perceptual hash.** A fingerprint that stays similar when an image is resized, recompressed or lightly
edited.

**Poster.** The frame that stands for a video: about one second in (the first is often black), or the first frame of a video of two seconds or less (`video.poster`). A video's thumbnail, preview and large image are made from it ([11](11-server.md)).

**Preview.** The 1600 px JPEG of a photo the viewer shows (`/preview/<sha>`), made in the background
ahead of time so opening a photo never waits on its original. Chapter 11.

**PWA.** Progressive Web App: a web page that can be installed on a phone's home screen.

**Quality (score).** A photo's score from the general aesthetic scorer, about 1 to 7; at least 5.0 counts as a good photo ([25](25-best-photos.md)).

**Request access.** What someone whose login is no member's does on the page they are shown (`/request-access`): it makes a `pending` member, whom a sysadmin lets in on `/members` ([11](11-server.md#members-and-who-may-do-what)).

**Role.** What a member may do: `sysadmin` (let people in, bring photos in from a folder, restart) or `member` (everything else). Several members may be sysadmins, and there is always at least one ([11](11-server.md#members-and-who-may-do-what)).

**Scrubber.** The handle at the iPhone grid's right edge that shows the month you are in and lets you drag to any date ([23](23-native-interface.md)).

**Sidecar.** A small metadata file next to a photo. Google's is JSON; iCloud's (via `icloudpd`) is XMP.

**SigLIP 2.** Google's image-text model used for semantic search (`siglip2-base-patch16-256`).

**Source (of a name).** Who said a face is a person: `you`, `google` or `auto`.

**Source (of a file).** The zip or folder a file was ingested from; recorded in `sources` and
`occurrences`.

**Stack.** Near-identical shots taken within 10 seconds of each other, shown as their best-ranked photo with a count ([25](25-best-photos.md)).

**Store.** The directory `store/ab/cd/<sha256>.<ext>` holding every unique original file.

**Stranger.** A face you chose not to name (`person_id = 0`); never asked about again.

**Stride.** The spacing, in pixels, of a detector's grid cells: 8, 16 and 32 here.

**Sysadmin.** A member with the sysadmin role; the first is the first `--upload-user` (or `--sysadmin`) on the first start. A request from this computer is the sysadmin too.

**Takeout.** Google's data-export service; the only complete export of a Google Photos library.

**Tailscale.** A private network (a "tailnet") used here to reach the app from a phone, with
`tailscale serve` providing HTTPS.

**Taste (score).** A photo's score from the scorer trained on the owner's own "which is better?" picks; it ranks good photos only ([25](25-best-photos.md)).

**Teacher (scorer).** Aesthetic Predictor V2.5, used only as a local tool to label photos for the quality scorer; never shipped ([25](25-best-photos.md)).

**Thumbnail.** The 360 px image of a photo in a grid (`/thumb/<sha>`), a WebP at quality 78 since
2026-11-20 (a JPEG before). Chapter 11.

**Tile.** What the iPhone grid needs to draw a photo and nothing else: its hash, aspect ratio, local time, city and favourite flag ([21](21-offline-search.md)).

**Trip.** A run of events more than 100 km (configurable) from home, spanning at least two dates.

**Trusted (face).** One whose name came from Google or from you. Only trusted faces build the reference
faces everything else is compared with.

**Upload policy.** What the iPhone app will ask a member before it shares their photos: `everything`, `review` (each batch) or `review_flagged` (only what may be private). Set on `/members`; nothing reads it yet ([11](11-server.md#members-and-who-may-do-what)).

**uv.** The Python package and script runner used to run each script with its inline dependencies.

**VLM.** A vision-language model that answers questions about images; here, any OpenAI-compatible
server used by `estate.py tag`.

**WAL (write-ahead log).** SQLite's journal mode that lets readers and a writer work at once.

**WebP.** An image format from Google, read by every current browser and by iOS. Grid thumbnails use it:
70% of a JPEG's bytes at the same closeness to the original.

**Window (years).** For faces, the 2 years either side of a photo whose named faces a person's mean
face may come from.

**XMP.** An XML metadata format; iCloud sidecars from `icloudpd --xmp-sidecar` are XMP.
