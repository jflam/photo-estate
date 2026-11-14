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

**Catalog.** The SQLite file `estate.db`.

**Centroid.** The mean (normalised sum) of a person's trusted face vectors for one year.

**Content-addressed.** Stored under a name derived from the content (its SHA-256), so identical bytes
share one file.

**Cosine similarity.** For two vectors of length 1, their dot product: 1 means identical direction, 0
unrelated. Used for image-text matching, "more like this", and face matching.

**dHash (difference hash).** A 64-bit perceptual fingerprint: whether each pixel of a 9×8 grey
thumbnail is brighter than its right-hand neighbour. Similar images have hashes a few bits apart.

**Embedding.** A fixed-length list of numbers that represents the meaning of an image, a sentence or a
face, such that similar things have similar vectors.

**Event.** A burst of photos with no gap of more than 3 hours (configurable) between consecutive
photos. Its id is its start time in epoch seconds.

**EXIF.** Metadata a camera writes inside a photo file: camera, lens, exposure, often date and offset.
Read with `exiftool`.

**Facet.** A field you can filter by and count: country, region, city, person, album, camera, lens,
trip.

**FTS5.** SQLite's built-in full-text search engine.

**Google (source).** A face name from a Google people tag, matched to a face by `seed()`. Trusted.

**Hamming distance.** The number of bits in which two bit strings differ.

**Home.** The place (per year) with photos on the most distinct days; used to decide what is "away".

**icloudpd.** The open-source iCloud Photos downloader the project uses.

**Journal (face).** `face-labels.jsonl`: an append-only record of your face decisions, keyed by photo
hash and box.

**Locality.** The nearer suburb or neighbourhood when a big city was chosen as a photo's `city`.

**Match floor / match window.** The two constants (0.085 and 0.04) that decide which photos count as
matching a typed search: at least the floor, and within the window of the best match.

**NMS (non-maximum suppression).** Discarding overlapping detector boxes except the best.

**Occurrence.** One place a file was found: a source (zip or folder), a path, and the album that implies.
One asset can have many.

**Pending (face).** A face whose best-matching person scores at least 0.30 but who is not yet named: it
is waiting for your yes or no.

**Perceptual hash.** A fingerprint that stays similar when an image is resized, recompressed or lightly
edited.

**PWA.** Progressive Web App: a web page that can be installed on a phone's home screen.

**Sidecar.** A small metadata file next to a photo. Google's is JSON; iCloud's (via `icloudpd`) is XMP.

**SigLIP 2.** Google's image-text model used for semantic search (`siglip2-base-patch16-256`).

**Source (of a name).** Who said a face is a person: `you`, `google` or `auto`.

**Source (of a file).** The zip or folder a file was ingested from; recorded in `sources` and
`occurrences`.

**Store.** The directory `store/ab/cd/<sha256>.<ext>` holding every unique original file.

**Stranger.** A face you chose not to name (`person_id = 0`); never asked about again.

**Stride.** The spacing, in pixels, of a detector's grid cells: 8, 16 and 32 here.

**Takeout.** Google's data-export service; the only complete export of a Google Photos library.

**Tailscale.** A private network (a "tailnet") used here to reach the app from a phone, with
`tailscale serve` providing HTTPS.

**Trip.** A run of events more than 100 km (configurable) from home, spanning at least two dates.

**Trusted (face).** One whose name came from Google or from you. Only trusted faces build the reference
faces everything else is compared with.

**uv.** The Python package and script runner used to run each script with its inline dependencies.

**VLM.** A vision-language model that answers questions about images; here, any OpenAI-compatible
server used by `estate.py tag`.

**WAL (write-ahead log).** SQLite's journal mode that lets readers and a writer work at once.

**Window (years).** For faces, the 2 years either side of a photo whose named faces a person's mean
face may come from.

**XMP.** An XML metadata format; iCloud sidecars from `icloudpd --xmp-sidecar` are XMP.
