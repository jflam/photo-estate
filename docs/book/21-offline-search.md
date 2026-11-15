# 21. Search with no server

On the web page, the server reads the question and answers it (chapters 11 and 12). On the phone, the
question has to be read and answered **locally**, from the replica of chapter 20, and give the same
answers the server would. This chapter is about that port: the same query model, the same reading of
names and places, and the same live counts, in about 400 lines of Swift
([Query.swift](../../ios/PhotoEstate/Catalog/Query.swift)), plus the one kind of search that cannot be
ported and how the app behaves when it has to give it up.

## One question, as an object

The web page holds the whole question in one object, `S` (chapter 13). The phone's equivalent is
`Query`, and its fields are the same ones with the same meaning:

| `Query` field | The web page's | Meaning |
|---|---|---|
| `text` | `q` | What was typed, possibly with typed filters, names and places |
| `plain` | `plain` | Do not read names and places in `text` ("search my exact words") |
| `similar` | `sha` | "More like this" for this photo |
| `picks[facet]` | `f.country` ... `f.lens` | Chosen values; `Facet` is `country`, `region`, `city`, `person`, `album`, `camera`, `lens`, `trip` |
| `event` | `event` | Scope to an event |
| `from`, `to` | `from`, `to` | Months, as `year * 12 + month - 1`, inclusive |
| `fav`, `oldFirst` | `fav`, `sort` | Favourites only; the order |

Because the fields match, a `Query` can be turned back into the server's query string
(`Query.items(...)`) for the searches only the server can answer. The same object drives both paths.

## Vocabulary: what the library knows

To read "ben and matthew in san diego" the app has to know that Ben and Matthew are people and San Diego
is a place. The server builds that from its in-memory arrays at startup; the phone builds a `Vocab` from
its replica with seven `GROUP BY` queries (people and albums from `tags`; city, region, country, camera
and lens from `photos`). From those it derives:

- **`who`**: a map from lower-case names to people. A full name always maps; a **first name maps only
  if one person has it** ("ben" means Ben Lam only while he is the only Ben).
- **`places`**: lower-case place names to place names, from the first part of each "City, Region,
  Country" string, so "denmark", "california" and "paris" all read. A word that is a person's first
  name is **a person first**, not a place.
- **A regular expression** that finds any of those names in one pass, longest names first so "san
  diego" wins over "san", optionally followed by `'s`, and refusing to match inside a longer word.

`Vocab` is rebuilt when the catalog's `revision` goes up, not on every keystroke.

## Reading the words

`Reader.readWords` is a port of the server's `read_words` (chapter 12), including its quirks, which are
the part that took the longest to get right:

1. **Find** every name and place the regular expression matches. Each becomes a marker in the text.
2. **Everyday words**: a first name that is also an English word (`sunny`, `rose`, `max`, `may`...)
   counts as a person only when **capitalized** or possessive ("Sunny's"). Countries that are also
   words (`turkey`, `china`, `georgia`, `jordan`...) likewise need a capital. A *city* whose name is an
   ordinary word ("reading", "bath") is detected with `UITextChecker`: if the word is a valid English
   word and lower-case, it is not a place. (The server does the same with the system word list,
   `/usr/share/dict/words`; the phone gets a dictionary from the OS.)
3. **Drop the glue.** "ben **and** matthew **in** san diego **photos**": the joiners next to a marker
   (`and`, `with`, `&`, commas between names; `in`, `at`, `near`, `from`... between places) and filler
   words (`photo`, `pictures`, `show me`, `trip`) disappear, so only "on a beach" is left to describe.
4. **If nothing is left but filler**, the rest is empty: "ben san diego" is entirely a filter.

The result is `(names, places, rest)`. The table below is the web app's (chapter 12), and the phone gives
the same reading:

| You type | Names | Places | Left to describe |
|---|---|---|---|
| ben and matthew on a beach | Ben, Matthew | | on a beach |
| ben san diego | Ben | San Diego | |
| seattle skyline | | Seattle | skyline |
| photos of sunny | *(not read: lower-case)* | | sunny |
| Sunny's birthday | Sunny | | birthday |

## From the reading to SQL: `Plan`

`Reader.plan` turns a `Query` into a `Plan`: a list of **clauses**, each tagged with a key, and the words
left over. A clause is one SQL condition and its arguments:

| Key | Clause |
|---|---|
| `country`, `region`, `city`, `camera`, `lens` | `p.city IN (?, ?)`: **any of** the chosen values |
| `album` | `p.id IN (SELECT id FROM tags WHERE kind='album' AND name IN (...))`: **any of** |
| `person` | One `p.id IN (SELECT id FROM tags WHERE kind='person' AND name=?)` per person, joined with `AND`: **all of** them in one photo |
| `trip`, `event` | `p.trip IN (...)`, `p.event = ?` |
| `date` | `p.ym BETWEEN ? AND ?` |
| `fav` | `p.fav = 1` |
| `typed` | Typed filters: `place:` and `camera:` and so on, with `instr()` over the lower-cased column; `year:2010-2012` as an integer range; `date:2010-07` as a prefix |
| `names`, `places` | What the reader found. A place reads as `p.place_words LIKE '% san diego %'`, so "Paris" matches the city and not "Parish" |

"Any of" for places and albums but "all of" for people is the server's rule too (appendix B), and the
reason is the same: choosing two people means photos *of them together*, while choosing two cities
means either.

Typed filters are parsed with a regular expression before anything else and stripped from the text,
exactly like the server: `sunset place:denmark` becomes the clause `place` and the words "sunset".

## Answering: tiles, counts, months

Everything the Library draws comes from three queries over the plan
([Query.swift](../../ios/PhotoEstate/Catalog/Query.swift), the `Catalog` extension):

- **`tiles(plan)`**: every selected photo in time order, as a `Tile` (`sha`, aspect ratio, local time,
  city, favourite flag). A `Tile` is deliberately tiny: a library of 100,000 is held in memory as
  100,000 of them, and the grid needs nothing else to lay itself out (chapter 23). Undated photos sort
  last in either direction (`ORDER BY whenk = '', whenk`).
- **`counts(facet, plan)`**: the live counts behind each filter sheet. It follows the server's rule from
  chapter 11, **"each facet ignores its own selection"**: the count beside each city is the count *if you
  picked that city instead*, by dropping the `city` clause from the plan. People are the exception:
  picking a second person narrows to photos of both, so their counts keep the people clause. A value
  that would give nothing shows `0` instead of vanishing.
- **`months(plan)`**: photos per month, ignoring the date clause, for the timeline in the date sheet.

There are no precomputed facets as there are on the server: SQLite counts over the plan's `WHERE` on demand. That is the main difference in architecture, and it works
because the replica is small, flat and indexed (`whenk`, `event`, `trip`; `tags` by id).

## The one search that can't move: ranking by look

"dog in the snow" and **More like this** rank every photo by cosine similarity between a text (or a
photo) and 768 numbers per image, using SigLIP (chapter 10). That needs the model, and every photo's
vector. For now, those searches go to the server, with the *same* `Query` serialized by `items()`:

```
GET /api/search?q=dog+in+the+snow&names=0&limit=300&cursor=0&dedupe=1
GET /api/similar?sha=...
```

(`Remote.search` in [Query.swift](../../ios/PhotoEstate/Catalog/Query.swift).) The response has the same
rows as the replica holds, so a ranked result is shown with the same grid and viewer, flat rather than
grouped by month (`grain = 0`), with a "best match first" subtitle. The whole question goes, filters
included, so "ben on a beach" is read **by the server** (names first, then "on a beach" ranked) and the
phone only draws the answer.

The title says how a search was read, as on the web page: the server's `people`, `places` and `filters`
fields come back and replace the phone's own, so what the title says is what the server did.

### Offline: a fallback that says so

When the request fails (a `URLError`; `markOnline(false)` also turns the toolbar icon to a crossed
Wi-Fi), the phone falls back to what it *can* do with a description: match its words against what the
replica has as text. That is the FTS5 index built when a row arrives (chapter 20): a photo's description,
file name, camera, lens, place, county, event and trip titles, people and albums, one row per photo.
`Reader.match` turns the description into a query where **every word must appear** and the last is a
prefix (`"dog" "in" "snow"*`), minus filler words.

So offline, "dog in the snow" finds photos whose *file name or caption* has those words, which is only
useful when they do, and the grid says so, above the photos:

> Offline: showing photos whose people, places, albums, captions or file names contain "dog in the
> snow". Finding photos by what's in them needs your server.

And **More like this** says "compares what photos look like, which needs your server. It isn't
reachable right now", and shows nothing rather than a wrong answer. The principle is the same as the
web app's rule for "kangaroo" (chapter 12): **an honest empty result beats a plausible wrong one.**

## Differences from the server, on purpose

| | Server | Phone |
|---|---|---|
| Counts | Precomputed integer codes and NumPy masks | `GROUP BY` over the replica, on demand |
| Typed filter `trip:` / `event:` | Ids | Ids |
| Near-identical shots | Computed from vectors | The feed's `dup` flag (chapter 20) |
| Ranking | SigLIP | Not available; the server's, or words |
| Everyday-word city check | A spelling dictionary | `UITextChecker` |
| Facet suggestions while typing | The filter sheets | `Vocab.suggest`: values holding every typed word, scored by position (start of value beats start of a word beats the middle) and by how many photos have it |

The last row is a feature of the Search tab: as you type "den", the page under the field is replaced by
the filters that match (a place, a person, an album: whatever holds the letters), each with a photo count, so a
half-typed place is one tap rather than a full word and a return.

## Where to look in the code

| Topic | Where |
|---|---|
| `Query`, `Facet`, `Plan`, `Clause` | [Query.swift](../../ios/PhotoEstate/Catalog/Query.swift) |
| `Vocab.load`, `Vocab.suggest` | [Query.swift](../../ios/PhotoEstate/Catalog/Query.swift) |
| `Reader.readWords`, `Reader.plan`, `Reader.match` | [Query.swift](../../ios/PhotoEstate/Catalog/Query.swift) |
| `tiles`, `counts`, `months`, `scope` | the `Catalog` extension in the same file |
| Choosing the phone or the server | `LibraryModel.reload` in [LibraryModel.swift](../../ios/PhotoEstate/Library/LibraryModel.swift) |
| The server's reading, which this mirrors | `read_words`, `parse`, `select` in [embed.py](../../photo-estate/embed.py); [chapter 12](12-search-language.md) |
