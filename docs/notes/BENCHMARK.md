# Benchmark set and library stats

Collected 2026-11-08 from Google Photos (web) in the account that holds the main library.

## Library stats: exact, from the existing Takeout in Drive (2026-11-08)

Drive already holds a full Google Photos Takeout from **2026-05-23**: `Drive/Takeout/takeout-20260523T191842Z-3-00{1..9}.zip`,
9 zips of ~50 GB each, **428 GiB** in total. These figures come from each zip's table of
contents, read by fetching only the last 48 MB of each zip, so nothing was fully downloaded.

| Metric | Value |
|---|---|
| Media files | **115,409** (457.9 GB) |
| Live Photo / motion pairs (same name, image + video) | 16,572, so ~16.5k of the videos are motion clips, not real videos |
| Album copies (outside `Photos from YYYY`) | ~1,115, mostly exact duplicates |
| **Unique items, as Google counts them** | **≈ 97,700** |
| Images | jpg 50,211 · heic 42,792 · png 2,474 · nef 707 · cr3 83 · jpeg 245 · dng 14 · other ~50 → **≈ 96.5k** to tag |
| Videos | mp4 16,515 (107.8 GB) · **mov 2,378 (135.9 GB)** · 3gp/avi/m4v 22 |
| JSON sidecars | 99,249 |
| Albums | 67 (the largest has 251 photos) |

Items per year: 2010: 890 · 2012: 1.1k · 2013: 1.4k · 2014: 1.9k · 2015: 2.7k · 2016: 3.9k · 2017: 9.0k ·
2018: 8.9k · 2019: 10.3k · 2020: 6.8k · 2021: 7.1k · 2022: 10.3k · 2023: 9.2k · **2024: 21.4k (107 GB)** ·
2025: 16.2k (92 GB) · 2026 to May 23: 3.0k

**What this changes:**
- Storage: plan for a **1 TB+** store. De-duplication only saves about 1%.
- No new full Takeout is needed. Ingest these 9 zips straight from Drive with rclone, one
  50 GB zip at a time (161 GB free locally). Then export **only "Photos from 2026"** to pick up
  everything taken after May 23. Takeout lets you select year folders like albums.
- Tagging: ~96.5k images. At the rented-GPU estimate (~8 images/s on an H100) that's ~3.5 h, about $10–15.

## Earlier estimate (superseded)

| Metric | Value | Source |
|---|---|---|
| Google Photos storage | **56.32 GB** | Google One storage page |
| Large photos & videos | 35.1 GB | Photos → Manage storage |
| Screenshots | 574 MB | Photos → Manage storage |
| Blurry photos | 340 MB | Photos → Manage storage |
| Other apps | 6 MB | Photos → Manage storage |
| Total Google storage used | 485.76 GB (Drive 428.38 GB, Gmail 1.06 GB) | Google One |
| **Estimated item count** | **~75,000 (±15%)** | see below |
| Date range | at least 2010 → May 2026, plus scans of prints from ~1910s | timeline / search |

**How the count was estimated.** Google Photos never shows a total. Its timeline sizes
itself from per-month item counts before loading them: about 7.0M px high, at a measured
~0.011 items/px over 300 fully loaded items and a further 9 spot samples. That gives about
75k items. The exact figure will come from `estate.py stats` after the Takeout ingest.

**Storage caveat.** Photos uploaded at "High quality" before June 2021 don't count toward
storage, so the Takeout could be **bigger than 56 GB**. Plan disk space for 2–3× that.

**Takeout sizing:** with 4 GB zips, expect roughly 15–40 zips.

## Benchmark album: "Benchmark – tagging"

A private album, not shared, in the main account. It should hold **121 photos** (I sent 121 adds. Google showed an "added" message for
13 of the 21 categories; the rest didn't show one, so the album's own count is still
unverified). The photos were picked
by Google Photos search. For each category it takes the 4 "most relevant" results, plus
picks spread evenly across the rest of the results, so several years are covered. When
Takeout runs, the album comes out as its own folder.

| Category (search) | Picked | Date spread |
|---|---|---|
| dog | 4 | 2019–2025 |
| cat | 3 | 2010–2024 |
| selfie | 6 | 2013–2026 |
| group photo | 8 | 2018–2026 |
| kids | 8 | 2017–2026 |
| food | 8 | 2024–2026 |
| landmark | 6 | 2016–2024 |
| mountains | 5 | 2024–2025 |
| beach | 5 | 2018–2022 |
| snow | 5 | 2020–2022 |
| sunset | 5 | 2020–2022 |
| birthday | 6 | 2017–2025 |
| wedding | 5 | 2018–2024 |
| christmas | 5 | 2019–2025 |
| black and white | 8 | 2012–2026 |
| blurry (incl. 2 videos) | 6 | 2019–2026 |
| night | 5 | 2016–2021 |
| flowers | 5 | 2020–2025 |
| bird | 4 | 2010–2025 |
| concert (incl. 1 video) | 4 | 2017–2024 |
| car | 4 | 2016–2024 |

Unintentional extras that are useful: several **near-duplicate bursts** got picked (for
example `kids` Sep 13 2019 11:29:42 and :47; `sunset` Aug 7 2022 ×3; `mountains` Jul 15 2025 ×3).
Those test whether the same scene gets the same tags.

### Still to add (blocked by Google rate-limiting, HTTP 429)

| Category | Where to get it | Target |
|---|---|---|
| Receipts | Documents → Receipts | 5 |
| Whiteboards / handwriting | search "whiteboard" (many results), Documents → Notes | 6 |
| Books, menus | Documents → Books & magazines, Recipes & menus | 5 |
| Screenshots | Screenshots & recordings (**review each one**: may show banking or messages) | 6 |
| Scanned old prints (~1910s family photos) | timeline around 2022 (Jul 1 2022 scans) | 10 |
| City / forest / panorama / sports / museum / airplane / garden / crowd / baby | search | ~25 |

**Kept out on purpose:** Documents → *Identity* (passports/IDs) and *Payment methods*
(cards). They'd be sent to the rented GPU, and they aren't needed to compare models.
**Whiteboards:** many look like work whiteboards. Leave them out if their content shouldn't
leave the house.
