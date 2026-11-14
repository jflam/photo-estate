# Photo Estate: how it works

A technical book about a small program that does a large job: take every photo and video you have
scattered across Google Photos, iCloud and Flickr, put them on a disk you own, and make them
searchable by what is in them, who is in them and where they were taken.

The whole application is about **5,500 lines of code** (6,200 with its tests and tools), written
as a handful of single-file Python scripts and three single-file web pages. This book walks through
all of it: why it is shaped the way it is, how each piece works, and how the pieces fit together.

## How to read it

You can read it front to back, or jump to the part you care about. Each chapter starts with what
problem it solves and ends with where to look in the code.

| If you want to... | Read |
|---|---|
| Understand the whole thing in ten minutes | [1. The big picture](01-big-picture.md) |
| Know how big it is and where each file fits | [2. A tour of the code](02-code-tour.md) |
| Get your photos out of Google, iCloud and Flickr | [3. Repatriating your photos](03-repatriation.md) |
| Know how files are stored and de-duplicated | [4. The store and ingest](04-store-and-ingest.md) |
| Read the database | [5. The catalog](05-catalog.md) |
| See how each service's metadata is reconciled | [6. Linking metadata](06-linking.md) |
| See how places, local time, events and trips are worked out | [7. EXIF, places, events and trips](07-places-events-trips.md) |
| See how duplicates are found | [8. Duplicates](08-duplicates.md) |
| Understand the text index and the local-model tagger | [9. Full-text search and tags](09-text-search-and-tags.md) |
| Understand image embeddings and "describe a photo" search | [10. Image embeddings](10-embeddings.md) |
| See how the server holds a library in memory and answers in milliseconds | [11. The photo server](11-server.md) |
| Know how a typed query is read ("ben and matthew on a beach") | [12. The search language](12-search-language.md) |
| Understand the web page: grid, timeline, viewer, phone gestures | [13. The photo app](13-photo-app.md) |
| Understand face detection and recognition | [14. Finding faces](14-faces.md) |
| Understand how your corrections teach the system | [15. Naming people: human corrections](15-human-corrections.md) |
| Run it for real: disks, services, speed, backups | [16. Operating it](16-operations.md) |
| See how it is tested, and how the demo is built | [17. Tests and the demo library](17-testing-and-demo.md) |
| Know the trade-offs, limits and what is next | [18. Design decisions and what's next](18-design-and-future.md) |
| Look something up | [Appendix A: command reference](appendix-a-commands.md), [B: HTTP API](appendix-b-http-api.md), [C: glossary](appendix-c-glossary.md) |

## The system on one page

```mermaid
flowchart LR
    subgraph Sources
      G["Google Photos<br/>(Takeout zips)"]
      I["iCloud Photos<br/>(icloudpd mirror)"]
      F["Flickr<br/>(data download zips)"]
      O["Any folder"]
    end
    subgraph Disk["Your disk"]
      ST[("store/ab/cd/sha256.ext<br/>every unique file, once")]
      DB[("estate.db<br/>SQLite catalog")]
      CA["review/<br/>thumbnails, face crops"]
      LG["query-log.jsonl<br/>face-labels.jsonl"]
    end
    G & I & F & O --> ING["estate.py ingest"]
    ING --> ST
    ING --> DB
    DB --> LNK["estate.py link"] --> DB
    ST --> EX["estate.py exif"] --> DB
    DB --> PL["places.py run"] --> DB
    ST --> EM["embed.py run"] --> DB
    ST --> FA["faces.py run"] --> DB
    FA --> CA
    DB --> SRV["embed.py serve<br/>the photo app"]
    ST --> SRV
    CA --> SRV
    SRV --> LG
    SRV --> BR["Your browser / phone"]
    BR -->|"your corrections"| SRV
```

Everything left of the disk is a one-time or occasional batch job. Everything right of it is one
long-running local web server. The disk holds three kinds of thing, and the difference between
them is the central idea of the whole design:

- **The store**: your original files, byte for byte, never modified. Irreplaceable.
- **The catalog**: what the services told us (dates, places, albums, people), plus what the
  scripts derived (embeddings, faces, events). Part of it is irreplaceable (what Google's sidecars
  said, what you told the app about faces); part is cheap to recompute (places, events, trips).
- **The cache**: thumbnails and face crops. Disposable. Delete it and it refills.

## About the numbers in this book

Line counts come from `tokei` on the repository as of 2026-11-13 (21 commits, 2026-11-11 to
2026-11-13). Performance and scale figures come from the project's own measurements in
[PERFORMANCE.md](../../photo-estate/PERFORMANCE.md), [BENCHMARK.md](../../photo-estate/BENCHMARK.md)
and the README; where this book quotes one, it says so. Code references link to files and name the
function, because line numbers drift.

## Conventions

- `DB` is the catalog file (`estate.db`). `STORE` is the directory of photos.
- A **sha** is the 64-character SHA-256 of a file's bytes, in lowercase hex. It is a photo's
  identity everywhere in the system.
- An **asset** is one unique file. An **occurrence** is one place that file was found (a zip, a
  path, an album). One asset can have many occurrences.
