# Appendix B: HTTP API reference

Three servers share the same style (plain `BaseHTTPRequestHandler`, JSON, loopback only, no sign-in of
their own; the photo app decides who each request is for from `tailscale serve`'s `Tailscale-User-Login`, below):

| Server | Started by | Default port | Purpose |
|---|---|---|---|
| **Photo app** | `embed.py serve` | 8766 (8767 with `--demo`, 8769 with `--sandbox`) | The product; also hosts the People page |
| **People page alone** | `faces.py serve` | 8764 | Naming only |
| **Duplicate review** | `estate.py review` | 8765 | Near-duplicate choices |

The defaults are in [core/config.py](../../photo-estate/core/config.py). Port 8768 belongs to the cloud preview
on the Mac mini (`deploy/preview-branch.sh`, [chapter 27](27-built-with-claude.md)); before issue #73,
`--sandbox` and `faces.py serve` defaulted to it too.

## The photo app

`embed.py serve` builds a `PhotoApp` ([photo_app.py](../../photo-estate/photo_app.py)), whose `handle` sends
each request to the module that answers it ([chapter 11](11-server.md)):

| Routes | Answered by |
|---|---|
| `/`, the app files, `/api/info`, `/api/sync`, `/api/favorite`, `/api/albums/<id>/download`, `/request-access`, `/api/request-access`, `/api/me`, `/members`, `/api/members*` | `PhotoApp` in [photo_app.py](../../photo-estate/photo_app.py), with [members.py](../../photo-estate/members.py) |
| `/api/lenses*`, `/api/albums/<id>/publish`, `/api/photos/<sha>/show`, `/api/photos/<sha>/why` | `PhotoApp`, with [lenses.py](../../photo-estate/lenses.py) |
| `/api/private`, `/held`, `/api/held`, `/api/held/review`, `/api/tombstones` | `PhotoApp`, with [private.py](../../photo-estate/private.py) |
| `/inbox`, `/api/inbox`, `/api/inbox/<id>/dismiss` | `PhotoApp`, with [member_inbox.py](../../photo-estate/member_inbox.py) |
| `/api/questions*`, `/api/stories*`, `/api/photos/<sha>/stories`, `/api/albums/<id>/stories` | `PhotoApp` (`stories_route`), with [stories.py](../../photo-estate/stories.py) |
| `/api/search`, `/api/similar`, `/api/facets`, `/api/periods`, `/api/layout`, `/api/log` | `Browse` in [browse.py](../../photo-estate/browse.py) |
| `/thumb/`, `/preview/`, `/large/`, `/original/` | `Images` in [images.py](../../photo-estate/images.py) |
| `/video/` | The HTTP handler in photo_app.py (`Handler.video`), which streams the file `Images.video_file` names |
| `/api/upload/...`, `/api/ingest`, `/api/restart`, `/stats`, `/api/stats` | `Admin` in [admin.py](../../photo-estate/admin.py) |
| `/people`, `/face/`, `/api/people*` | `People` in [faces.py](../../photo-estate/faces.py) |
| `/changes`, `/api/changes*`, `/api/albums*`, `/api/tasks*` | `Changes` in [changes.py](../../photo-estate/changes.py), `Tasks` in [tasks.py](../../photo-estate/tasks.py) |
| `/mcp` | `Server` in [estate_mcp.py](../../photo-estate/estate_mcp.py) |
| `/setup`, `/api/intake*` (not with `--demo`) | `IntakeAPI` in [intake/api.py](../../photo-estate/intake/api.py) |
| `/compare`, `/api/compare*` (only with `--compare-vectors`) | `Compare` in [compare.py](../../photo-estate/compare.py) |

All of them answer from the same in-memory catalog (`catalog.Live`, in [catalog.py](../../photo-estate/catalog.py)),
as the member the request is for sees it (`Live.visible_for`: [What each member sees](#what-each-member-sees-lenses)
below). Every count, list, cover, stack, trip card, favourite and sync row is that member's.

### Who may do what

Every request is for a member of the estate or for nobody ([members.py](../../photo-estate/members.py),
[chapter 11](11-server.md)). Through `tailscale serve`, the member is the one whose login is in the
`Tailscale-User-Login` header; a request from this computer itself (`127.0.0.1` or `::1`, with no
`Tailscale-User-Login`, `X-Forwarded-For` or `Forwarded` header: the browser there, the task agent, a
local MCP client) is the sysadmin. With `--demo`, and only then, the header is not read: `X-Demo-Member:
<name>` or a `demo_member` cookie names a demo member (`ada`, the sysadmin; `ben`, `cleo`), a name that is
none of theirs is a login nobody has let in, and with neither a request is Ada.

The rules are one table, `ROUTE_AUTH` in photo_app.py (`http_util.Auth`). A refused browser opening a page
(`Accept: text/html`) gets the request-access page with status 403; anything else refused is `403
{"error": "not allowed"}` (uploads: `403 not allowed`, text).

| Rule | Who | Routes |
|---|---|---|
| `PUBLIC` | anyone who can reach the app | `GET /request-access`, `POST /api/request-access`, `GET /api/info` (a non-member gets only `{"member": false}`), `GET /manifest.webmanifest`, `/sw.js`, `/icon-*.png` |
| `SYSADMIN` | an active sysadmin, or this computer | `GET /members`, `GET /api/members`, `POST /api/members/<id>`, `GET /setup`, `GET` and `POST /api/intake/...`, `POST /api/restart`; and since phase 5, `GET /stats`, `GET /api/stats` and `GET /api/ingest` (they count every member's photos, private ones too, and the ingest's log names files) |
| `MEMBER` | an active member of any role, or this computer | everything else: every other page, `GET` and write, `/app/*.js`, images and `/video/`, album downloads, `/api/sync`, `/mcp`, uploads, `OPTIONS /api/upload/...` |
| `LOCAL` | this computer only | nothing yet |

Until 2026-11-22 reading was open to anyone who could reach the app, and writes took a `--upload-user`
login (everyone, with none named) or this computer. Before 2026-11-19 the three People writes took no
permission, and intake, the ingest, restart and uploads took a `--upload-user` login only.

### Members

| Endpoint | Behavior |
|---|---|
| `GET /request-access` | A page of a few words, no script: for a login nobody has let in, "Ask <sysadmin> to let you in" with a name field (filled from `Tailscale-User-Name`) and a button; after asking, that the sysadmin has been told (in their inbox) and will let you in; for a revoked member, that they are not let in; with no login at all, that nothing said who is asking; for a member, that they are in. The same page is what a refused browser is shown, and a member refused a sysadmin's page is told "Only <sysadmin> can open this". With `--demo` it ends with links to be each active demo member |
| `POST /api/request-access {name?}` | A login with no member becomes a `pending` member (`member.requested` in the journal), and a `member_request` item in every active sysadmin's inbox; a member's login, in any status, changes nothing. A form's post (`application/x-www-form-urlencoded`) is answered `303` to `/request-access`; JSON with `{"status"}`. `400` with no login |
| `GET /api/me` | The member the request is for: `{id, name, role, status, upload_policy, person_id, kind, joined_at, login, local}`. From this computer `local` is true and `login` empty (`id` is null if the catalog has no sysadmin yet) |
| `GET /members` | `members.html`, the sysadmin's page (with an `ETag`, `no-cache`) |
| `GET /api/members` | `{members: [{id, name, kind, role, status, upload_policy, person_id, person, joined_at, logins: [{login, added_at}]}], persons: [{id, name}], me, demo, roles, policies}`: waiting members first, then active, then revoked; `persons` are the people named in People that a member can be linked to |
| `POST /api/members/<id> {name?, role?, status?, upload_policy?, person_id?}` | Change a member: approve (`status: "active"`, which sets `joined_at` the first time), revoke (`"revoked"`), a role (`sysadmin`, `member`), an upload policy (`everything`, `review`, `review_flagged`; nothing reads it yet), a person (or `null`), a name. `{member}` as listed above; one `member.changed` journal entry naming what changed. Letting a pending member in, or turning them away, closes their request's inbox items; a member who is no active sysadmin after it (made a member, or revoked) has their `member_request` and `lens_proposed` items done (one made a sysadmin is given none for what already waits). `400` for any other field or value, a person that does not exist, or a change that would leave no active sysadmin, or any change to the estate's agent (kind `agent`, listed with the others); `404` for no such member |
| `GET /members?be=<login>` | `--demo` only: sets (or, with `be=` alone, clears) the `demo_member` cookie and answers `303` to `/members` for a sysadmin, `/` for anyone else |

### Malformed requests

| Request | Answer |
|---|---|
| A number parameter that isn't one: `limit`, `cursor` on `/api/search` and `/api/similar`; `limit` on `/api/sync`; `job`, `after` on `/setup/practice-consent` and `/api/intake/jobs/<id>` | `400 {"error": "limit must be a whole number"}` (named for the parameter). Numbers out of range are clamped as before (`limit` 1-500 on search, 1-10000 on sync) |
| A POST body that isn't JSON | `400 {"error": "the body is not JSON"}`; on `/mcp`, JSON-RPC's `-32700` error, also a 400. An empty body is `{}` |
| A `Content-Length` that isn't a whole number | `400 {"error": "Content-Length must be a whole number"}` |
| A JSON body over 8 MB (`http_util.MAX_BODY`) | `413 {"error": "the body is over 8388608 bytes"}`, before reading it; the connection is closed |
| `from=` or `to=` with a month outside 1-12 (`2020-00`, `2020-99`) | Ignored, like any date that can't be read |

The setup server (`intake/server.py`) and `faces.py serve` read bodies the same way.

### Pages and files

| Path | Returns |
|---|---|
| `GET /` , `/index.html` | `search.html`, with an `ETag` and `Cache-Control: no-cache` |
| `GET /manifest.webmanifest`, `/icon-180.png`, `/icon-192.png`, `/icon-512.png`, `/icon-maskable-512.png`, `/sw.js` | The files in `app/`, served from the site root |
| `GET /app/<name>.js` (`/app/members-flow.js`, `/app/inbox-flow.js`, `/app/agents-flow.js`, `/app/dom.js`, `/app/api.js`, `/app/fsm.js`, `/app/toast.js`, `/app/format.js`, `/app/query.js`, `/app/layout.js`, `/app/grid-flow.js`, `/app/library-flow.js`, `/app/albums-flow.js`, `/app/viewer-flow.js`, `/app/favourite.js`, `/app/loop.js`, `/app/work.js`, `/app/save-queue.js`, `/app/changes-flow.js`, `/app/people-flow.js`, `/app/stats-watch.js`, `/app/setup-flow.js`) | A script module the pages import (`pages.module`), as `text/javascript; charset=utf-8`, with an `ETag` and `Cache-Control: no-cache` like the page. Only the modules in `app/`: anything else under `/app/` (their tests, `package.json`, `/app/sw.js`) is a 404. The setup server, `faces.py serve` and `estate.py review` serve the same paths for their pages, without the `ETag` |
| `GET /held`, `/held.html` | `changes.html` in its held mode: the review of the member's held photos ([below](#private-photos)), with an `ETag` and `Cache-Control: no-cache` |
| `GET /people`, `/people/` | `people.html`, once faces exist and `People` has loaded (else 503; 500 with the error if loading them failed) |

### Uploads from the phone

| Request | Returns |
|---|---|
| `PUT /api/upload/<YYYY/MM/DD/name.ext>` | Saves the body to the inbox (`--inbox`, default `<db dir>/iphone`; none with `--demo`), in the folder of the member the request is for: `<inbox>/m<member id>/YYYY/MM/DD/name.ext` (the path the phone sends is unchanged; ingest reads who shared it from the folder). For the first sysadmin, a file already at the inbox's top (sent before member folders) is found there. `Content-Length` is required; an optional `X-Content-SHA256` is checked against the bytes received. Optional metadata headers, written into the `.xmp` sidecar: `X-Taken`, `X-Latitude`/`X-Longitude`, `X-Favorite`, and `X-Screenshot: 1` (PhotoKit's screenshot mark: the keyword `screenshot`, which the hold at ingestion reads); `X-Batch: <id>/<count>` for the ingest. `201 {"status":"stored"}`; `200 "exists"` if the same path and size is already there (so retries are safe); `200 {"status":"refused: opted_out" | "refused: purged"}` with `X-Upload-Refused: opted_out | purged` when the bytes are tombstoned for this member or purged: answered **before the body is read** (the connection then closes) when `X-Content-SHA256` names them, else after they are hashed, the staging file removed; nothing reaches the inbox (a 200, not a 4xx, because the system uploader and an old app retry or report anything else: [chapter 24](24-uploading.md)); `500` if the catalog cannot be read to check; `409` if the path exists with a different size; `400` for a bad path, short body or wrong checksum; `403` if the caller is not an active member and the request is not from this computer; `411` without a length |

The extension must be a photo or video type, or `X.<media ext>.xmp` for a sidecar. The server never
writes the catalog: `ingest-iphone.sh` ingests the inbox ([upload.py](../../photo-estate/upload.py)).

`GET /api/ingest` reports the inbox and the last run; `POST /api/ingest` runs `ingest-iphone.sh`
(one at a time: `202`, or `409` if a run is going) and `POST /api/restart` relaunches the app, but
only when launchd started it (else `409`). The Library info page (`/stats`) has the buttons for
these. Both POSTs take the write rule, and their body, if any, is not looked at: the script and its
arguments are fixed.

`GET /api/ingest` returns `{enabled, running, started_at, finished_at, exit_code, log: [last 300
lines], restart_needed, waiting: {files, bytes}, can_restart}`; `{"enabled": false}` with `--demo`. It
takes the `SYSADMIN` rule (the log names files of every member's uploads); `POST /api/ingest` the `MEMBER` rule.

### Search and browse

`GET /api/search` and `GET /api/similar` take the same parameters; `/api/similar` additionally
requires `sha` and ranks by that photo's vector (404 `{"error":"unknown photo"}` if unknown, or private to
someone else: the same answer). In `q`, `is:private`, `is:held` and `is:withdrawn` keep the member's own
private photos (all of them, those held at ingestion, those they withdrew); a row of theirs carries `private`
(`held`, `withdrawn`, or `new`: not checked yet). A photo private to someone else is in no result, count,
facet, trip, cover or "similar" list; a `stack` named by it, an `event` or `trip` only such photos are in, and a
name or place only they carry are answered as ones the library does not have.

| Parameter | Repeatable | Meaning |
|---|---|---|
| `q` | | Free text, possibly with typed filters, names and places ([chapter 12](12-search-language.md)) |
| `names` | | `0` = do not read names and places in `q` |
| `sha` | | The photo for `/api/similar` |
| `country`, `region`, `city`, `camera`, `lens` | yes | Facet values; **any of** the repeated values |
| `album` | yes | **Any of** |
| `person` | yes | **All of** |
| `trip` | | A trip id (scope if single) |
| `event` | | An event id (filter and scope) |
| `from`, `to` | | `YYYY` or `YYYY-MM` month bounds, inclusive |
| `fav` | | `1` = favourites only |
| `kind` | | `video` = videos only, `photo` = no videos (the same as `is:video` and `is:photo` in `q`). A Live Photo is a photo |
| `sort` | | `old` or `new` (default `old` when scoped to an event or trip, else `new`); only for unranked results |
| `cursor` | | Position to continue from (the previous response's `next`) |
| `limit` | | 1-500, default 120 |
| `dedupe` | | `0` to show near-identical shots (default `1`: hide) |
| `shots` | | `0` to hide screenshots, told from size, camera and name as the iPhone does (default `1`: show). Browsing and filtering only: a described search or More like this still finds them |

Response:

```json
{
  "results": [ { "sha": "...", "ext": ".jpg", "taken": "...", "local": "2019-07-14T12:22:33",
                 "tz": "Europe/Paris", "lat": 48.85, "lon": 2.29, "desc": null,
                 "people": ["Ben"], "albums": ["Paris Trip"], "fav": true, "w": 4032, "h": 3024, "ar": 1.3333,
                 "path": "Takeout/.../IMG_0001.JPG", "camera": "Apple iPhone 15 Pro", "lens": "...",
                 "focal": 6.8, "focal35": 24.0, "fnumber": 1.78, "exposure": 0.004, "iso": 50,
                 "place": "Paris, Île-de-France, France", "city": "...", "region": "...", "country": "France",
                 "county": "...", "place_inferred": false, "event": 1563099753, "event_title": "Paris Trip",
                 "trip": 1563099753, "trip_title": "Trip to Paris, France" } ],
  "next": 150,                // cursor for the next page, or null
  "hidden": 3,                // near-identical shots skipped on this page
  "total": 104,               // photos in the library
  "matching": 8,              // photos matching (the length of the ranking)
  "text": "latte art",        // what was left for the image model
  "filters": ["place:denmark"],   // typed filters applied
  "people": ["Ben Lam"], "places": ["San Diego"],   // names and places read from q
  "scope": { "kind": "trip", "id": 1, "title": "...", "start": "...", "end": "...", "n": 120, "trip": null },
  "sort": "new",
  "ms": 91
}
```

(`results[*]` fields are present when known.) A video's row also has `"kind": "video"` and `"dur"`
(seconds, from exiftool; `null` when it did not say), and, when a search with words found it, `"at"`:
the seconds into it where its best frame for those words is, where a player should start. A Live
Photo's still has `"live"`: the sha of its clip, to play from `/video/<that sha>`. A photo's row has
none of these. A video's score in a ranking is its best frame's or its mean's, whichever is higher
([chapter 10](10-embeddings.md#videos)); a Live Photo's clip is never a result.

### Facets and info

| Request | Returns |
|---|---|
| `GET /api/facets?...` (same filter parameters) | `{"facets": {country, region, city, camera, lens, trip, album, person: [[label, count], ...]}, "months": [counts], "fav": N, "matching": N}` (each facet's counts ignore its own selection; `person` counts include it) |
| `GET /api/periods?...` (same filter parameters, plus `dedupe`, `shots`, `sort`) | The Library's Years and Months: `{"years": [...], "months": [...], "matching": N, "total": N}`, each card `{"key": "2019" or "2019-07", "n", "at", "cover", "ar", "places": [...]}`; `at` is where the card's photos start in `/api/search` with the same parameters (a `cursor`) |
| `GET /api/layout?...` (same filter parameters, plus `sort`) | One byte per photo, in `/api/search`'s order with the same parameters: its aspect ratio between 0.5 and 2.6 in 255 steps, so the page can lay a month out before fetching it. `application/octet-stream` with an `ETag` |
| `GET /api/info` | For a member: `{"total", "videos", "model", "m0", "trips": [{id, title, start, end, n, cover}], "demo", "faces", "facets", "months", "fav", "matching"}`: the unfiltered facet payload merged in, all of it what this member sees (`fav`: their own favourites). `videos` is how many of `total` are videos. `months` is the list of per-month counts, so the number of months is its length and `m0` is the first month as `year*12 + month - 1` |
| `GET /stats` | The library info page ([stats.html](../../photo-estate/stats.html)) |
| `GET /api/stats[?refresh=1]` | The `SYSADMIN` rule. `{state: working\|ready\|error, library, disk, app}`: library figures from memory (every photo in the catalog the first sysadmin may be shown, none private to another member; `favourites` is the asking member's own), disk figures (catalog tables, cache folders, free space) measured once in a background thread and saved to `stats-cache.json` next to the catalog; `refresh=1` recomputes (the page's Recompute button); `catalog_assets_now` lets the page flag a stale measurement. Code in [stats.py](../../photo-estate/stats.py), which also runs standalone |
| `GET /api/sync?epoch=E&since=V[&after=SHA][&limit=N]` | The catalog as a change feed, for a device that keeps its own copy (the iPhone app; [sync.py](../../photo-estate/sync.py)): `{epoch, photos: [rows as in /api/search, plus dup], deleted: [shas], since, after, more, total, trips}`. Rows come in (version, sha) order; send `since` and `after` back to continue, until `more` is false. `deleted` holds the deletions between the cursor sent and the one returned (each is sent once; none to a device that is up to date; apply them from every page). `limit` counts rows. An unknown `epoch` starts from the beginning. `503` with `Retry-After` while the feed is being built after a start. Each member has their own feed, of what they see: the first sysadmin's is `sync-feed.db` beside the catalog, as before members; anyone else's is `sync-feed-<member id>.db`, made the first time one of their phones asks (that first answer takes as long as reading every row once, about a second for 100,000 photos) and kept up to date from then on |
| `GET /api/log?ev=open\|share&sha=...&pos=N` plus the search's parameters | Appends an event to the query log; `204` |
| `GET /api/layout?...` (same filter parameters, plus `sort`) | Every matching photo's shape, one byte each, in `/api/search`'s order: the aspect ratio between 0.5 and 2.6 in 255 steps (`application/octet-stream`, with an `ETag`). The page lays a month out from it before it knows which photos are in it |
| `POST /api/favorite {sha, fav}` | Makes a photo the member's favourite, or not (`member_favorites`; nobody else's changes): `{sha, fav, favourites}`, `favourites` their count. Takes the write rule; `403` otherwise. It waits for the requests in flight, since it changes the view they read (the gate's write side, [chapter 11](11-server.md)) |

### Albums and change sets

Answered by [changes.py](../../photo-estate/changes.py) (chapter 26). Every `/api/changes` address
also answers under `/api/albums`; `/api/changes` lists only the albums that are up for hiding.

An album or set is its owner's. A member sees their own and the albums others have published as a
series; anyone else's answers `404`, as if there were none. Only its owner changes one: a write to a
series someone else published is `403 {"error": "this album is someone else's: ..."}` (its favourite is
the exception: that is the member's own). Each listed album and set carries `owner` (members.id; one
from before members is the first sysadmin's), `mine`, `lens` (the filter lens a hiding set hides for, or
null), `series` (the album's lens once published, or null) and `published`.

| Request | Does |
|---|---|
| `GET /changes`, `/changes/<id>` | The review page ([changes.html](../../photo-estate/changes.html)) |
| `GET /api/albums` | `{sets, hidden, busy}`: every album the member sees, newest first (`n` and the cover are of the photos they see); `hidden`, how many photos their filters hide |
| `GET /api/albums/<id>/photos?cursor=&limit=` | Its photos as `/api/search` gives them (`fav` the member's), each with its `section` and `reason`. An album or series lists only the photos the member sees, whoever made it; a set up for hiding (its owner's review) lists all of its photos, `hidden` on those it hides |
| `GET /api/albums/<id>/download` | The same photos' original files as one zip, written as it is read |
| `POST /api/albums {title, prompt?, query?, items?}` | A new album; with `query`, a live one (whatever that search finds) |
| `POST /api/albums/<id>/items {items?: [{sha, section?, reason?, score?}], remove?: [sha]}` | Add photos, or take them out |
| `POST /api/albums/<id>/rename {title?, prompt?}` | Change its title or note |
| `POST /api/albums/<id>/query {query}` | What a live album searches for |
| `POST /api/albums/<id>/favorite {fav}` | Make every photo in it the member sees their favourite, or not: `{id, photos, fav, changed}` |
| `POST /api/albums/<id>/publish {on?}` | Publish an album of the member's as a series (its lens is made the first time), or take it back (`on: false`, which closes its subscribers' `lens_changed` items): `{album, lens, published, changed}`. `403` someone else's, `409` a set up for hiding |
| `POST /api/albums/<id>/hide`, `/view` | Put it up for hiding (it becomes a change set), or take it down again. `409` for hiding an album published as a series (stop publishing it first) or one with new photos waiting for it, an open `series_update` item (add them, or none, first) |
| `POST /api/albums/<id>/discard` | Put it away (it must hide nothing): marked `discarded`, its rows kept; it is in no listing and every address of it answers 404. `409` while it hides photos, is published as a series, or has new photos waiting for it |
| `GET /api/changes/<id>`, `/api/changes/<id>/check` | One set and its photos; whether every photo is where the set says it should be |
| `POST /api/changes {title, prompt?, op?, items}` | Propose a set |
| `POST /api/changes/<id>/decide {shas, decision}` | Mark photos `accept`, `reject` (keep) or `pending` |
| `POST /api/changes/<id>/apply {through?, lens?}` | Hide every photo not marked reject, and put back those that are; with `through`, only as far as that photo in the page's order. They are hidden for the members who see through the set's lens, chosen at its first apply: `lens` `"mine"` (the default: the owner's own filter, "Hidden photos", made then), `"estate"` (the Estate defaults: a sysadmin only, else `403`), or a filter lens's id (the owner's, or an estate default for a sysadmin). After that the set keeps its lens until it hides nothing (`409` for another). Answers the set with `hid`, `showed` and `lens`. An apply that leaves nothing to apply closes the set's `review` inbox item; one that changes what a published filter hides tells its live subscribers (`lens_changed`) |
| `POST /api/changes/<id>/revert`, `/commit`, `/reopen`, `/discard` | Undo everything the set did; mark it done; make a done set changeable again; put away a set that hides nothing (marked `discarded`, never deleted; `409` as for an album's `discard`) |

**Series** (multiplayer phase 7): a list album that grows from a search, kept up to date as photos arrive
([listeners.py](../../photo-estate/listeners.py)).

| Request | Does |
|---|---|
| `POST /api/albums {title, grows_from: search}` | A series: a list album of what the search (as `query` takes it) finds now, as its owner sees the library, each photo with the reason "Matches …"; `grows_from` on the album from then on. New photos that match are added by the estate's agent (a family series) or wait for its owner (a `series_update` inbox item) |
| `POST /api/albums/<id>/grow {grows_from: search \| null}` | Give a list album a search to grow from, or take it away (`album.grows`; taking it away closes its `series_update` item). `409` for a live album or a change set, `400` for a search that cannot be run |
| `GET /api/albums/<id>/candidates` | Its owner's: `{album, grows_from, results}`, the new photos waiting for the series (named by `series.update_requested` since its `series_update` item opened, that they may be shown and the album does not hold), as `/api/search` rows; empty with no open item. `404` for anyone else |
| `POST /api/albums/<id>/candidates {add: [sha]}` | Add those of them that are waiting (`added`; `not_candidates` counts the rest, which are not added) and the `series_update` item is done, whatever was added: an empty `add` says "none of these" |

An album the estate's agent makes acting for no one is published at once as a series maintained by it
(`lenses.maintained_by`): every member sees it in their Albums (`maintained_by` on the album, and `family_series`
in the MCP's answer).

### What each member sees (lenses)

Answered by `PhotoApp` with [lenses.py](../../photo-estate/lenses.py) ([chapter 11](11-server.md#what-each-member-sees-lenses)).
All take the `MEMBER` rule; a body is a JSON object (`400` otherwise; `on` and `accept` are booleans,
`true` when left out). A write that changes what someone sees is in every view before it answers; the
sync feeds follow a moment later. Errors are `{"error"}` with the status given.

| Request | Does |
|---|---|
| `GET /api/lenses` | `{lenses, mine, sysadmin, proposed}`: the member's own lenses, the estate defaults and every lens others have published, oldest first. A lens is `{id, title, kind: filter\|series, set_id, published, estate_default, created_at, owner: {id, name}, mine, subscription: live\|snapshot\|opted_out\|null, proposed}`, and a filter also `{on, photos}` (whether it hides for this member now; how many photos it hides) and, for its owner, `sets` (its hiding sets' ids). `mine` is the member's own filter (where their hides go by default) or null; `proposed` the ids of the filters proposed as estate defaults |
| `POST /api/lenses {title}` | A new filter lens of the member's: `{lens, changed}`. `400` with no title |
| `POST /api/lenses/<id>/subscribe {mode?: live\|snapshot, title?}` | Subscribe to a published lens of someone else's: `live` (the default: it follows the lens) or `snapshot` (a copy now, into a lens of the member's own, titled `title` or "<title> (a copy)": for a filter a new filter lens and one applied hiding set; for a series a new album of theirs with the photos they see of it). For an estate default, `live` takes back an opt-out. `{lens, mode, changed}`, and for a snapshot `{copy (a filter's new lens), album (the new set or album), photos}`. `400` another mode, `404` no such lens or not published, `409` their own lens (always on) |
| `POST /api/lenses/<id>/unsubscribe` | Leave a lens (a snapshot's copy stays theirs); for an estate default, opt out of it: `{lens, mode: opted_out\|null, changed}`. `404` no such lens they can see |
| `POST /api/lenses/<id>/publish {on?}` | Publish a lens of the member's, or take it back (which closes its subscribers' `lens_changed` items): `{lens, published, changed}`. `403` someone else's, `409` taking back an estate default (one a sysadmin accepted while the request was on its way too), or publishing a series whose album is up for hiding or discarded |
| `POST /api/lenses/<id>/propose` | Propose a filter of the member's as an estate default (it is published too): `{lens, proposed_at, changed}`. `409` a series, or one that is an estate default already. Nothing changes for anyone until a sysadmin decides: every other active sysadmin gets a `lens_proposed` inbox item, which deciding (or taking the filter back with `publish {on: false}`) closes |
| `POST /api/lenses/<id>/decide {accept?}` | A sysadmin accepts a proposed filter as an estate default (every member then sees through it unless they opt out), or declines it (`accept: false`: it stays its owner's, published): `{lens, estate_default, changed}`. `403` not a sysadmin, `409` not proposed (also when its owner took it back, or another sysadmin decided it, while the request was on its way) |
| `POST /api/photos/<sha>/show {on?}` | "Show it anyway": a photo the member's filters hide is shown to them all the same, or no longer (`on: false`): `{sha, shown, changed}`. `404` for a photo they may not be shown |
| `GET /api/photos/<sha>/why` | Why the member does not see a photo, from the state tables: `{sha, visible, hidden, shown_anyway, text, reasons: [{set: {id, title}, hidden_at, lens: {id, title, estate_default} or null (a set from before lenses, hiding for everyone), owner: {id, name}, mine}], private: {sharer, mine: {why, since, until} or null, others_sharing}}` (`private`, phase 5: whether they shared the photo, whether it is private to them, and how many other sharers do not hold it private; `others_sharing` is null when they are no sharer), newest first, only the filters that are on for this member (another member's hides are not theirs to see). `text` says the first in words: "Hidden by Ada's lens “Estate defaults” on Nov 3", "Hidden by your lens “Hidden photos” on Nov 23" (the year too when it is not this one), "Shown anyway; hidden by ...". The day in `text` is the UTC day of `hidden_at`; the viewer words the line itself from `reasons`, with the day in the viewer's own time zone. `404` for a photo they may not be shown |

### Private photos

Answered by `PhotoApp` with [private.py](../../photo-estate/private.py) ([chapter 11](11-server.md#private-photos)).
The `MEMBER` rule; outside the gate, like a lens: the change is in every view before it answers, and the sync
feeds and People follow.

| Request | Does |
|---|---|
| `POST /api/private {shas: [sha], private: true, until?}` | Withdraw photos the member shared (private to them, `withdrawn`), and with `until` (`YYYY-MM-DD`) set the day each is released on by itself; for one already held or withdrawn, `until` sets or (`null`) clears its day; one still `new` (not checked yet) is withdrawn like any other. `{withdrawn, release_set, released, changed, photos}`, and for one photo `private` (as the why answer's). One `photo.withdrawn` (and `photo.release_set`) journal entry |
| `POST /api/private {shas: [sha], private: false}` | Release them now: `{released, ...}`, one `photo.released` entry; the member's `held` inbox item says how many still wait to be looked over, and is done when none does |
| `GET /api/held` | The review of held photos (`/held`): every held photo of the member's that waits to be looked over (an open `held` row with no `reviewed_at`), whatever their view options, stacks or hides: `{items: [{sha, section, why, scores, when, ar, place, people, hidden?, missing?}], sections: [{key, n}], held, waiting, hold_at, prompts, member}`. In order, likeliest mistakes first: `section` `named` (a person named in it), `close` (best score under `HOLD_AT + 0.01`), `documents`, `sure` (a screenshot, or a score of 0.13 or more), `hidden` (the member hides it); within one, best score (lowest first), then sha. `why`: the prompts at or over the line, `is_screenshot`, `phone_screenshot`; `scores`: one per prompt, in the order of `prompts` (null: no vector); `missing`: not in the catalog served. Another member's held photos are no part of it |
| `GET /api/held?count=1` | `{held, waiting}`: how many photos are held for the member, and how many of them wait to be looked over (the search's held view says these) |
| `POST /api/held/review {release: [sha], keep: [sha]}` | "Reviewed up to here" on `/held`: release those in `release` (`released_at`) and keep those in `keep` private, looked over (`reviewed_at`): both leave the review. Only photos that wait to be looked over are touched; any other sha (looked over or released already, held after the page read the list, someone else's, no photo) is skipped, each the same. `{released, kept, skipped, changed, held, waiting}`. One `photo.released` and one `photo.reviewed {photos}` journal entry, by the member; the `held` inbox item says how many still wait, or is done. A kept photo is private as before (`is:held` lists it; the viewer's line releases it) |

`400` for a body without `private` (a boolean), without `shas` (at most 20,000 sha256s; there is no `which`: no
call releases every photo at once), or with an `until` that is not a day; `404 {"error": "no such photo"}` when any
photo is one the member may not be shown (as for an unknown sha); `403` for a photo they did not share, or a request
for no member. `/api/held/review`: `400` when `release` or `keep` is not a list of lower-case hex sha256s (at most
50,000 each) or a sha is in both; `403` (as `/api/held`) for a request with no member. A timer in the app releases
photos whose day has come (`photo.released {auto: true}`, by no actor, acting for their sharer).

### Tombstones (the phone gate)

The member's own opt-outs: photos they kept back on their phone, by the SHA-256 of each file
([chapter 11](11-server.md#private-photos), [chapter 24](24-uploading.md#the-phone-gate-looking-before-sharing)).
The `MEMBER` rule. An opt-out refuses that member's upload of those bytes (above) and nobody else's.

| Request | Does |
|---|---|
| `GET /api/tombstones` | `{tombstones: [sha256]}`: the member's opt-outs, not lifted (a purge, member 0's, is no member's and is not listed). Empty for a request with no member |
| `POST /api/tombstones {add?: [sha256], lift?: [sha256]}` | Opt out of `add` (a new row, or a lifted one standing again) and take back `lift` (`lifted_at`; a purge is never lifted, and never the member's to lift). `{opted_out, lifted, changed}`: how many changed. One `tombstones.changed {opted_out, lifted}` journal entry a call, by the member, when anything changed |

`400` when `add` or `lift` is not a list of lower-case hex SHA-256s (at most 20,000 each), or names a hash in
both; `403` for a request with no member.

### The inbox

Answered by `PhotoApp` with [member_inbox.py](../../photo-estate/member_inbox.py) ([chapter 11](11-server.md#the-inbox)).
All take the `MEMBER` rule, and a member only ever sees and changes their own items. The kinds are
`member_request`, `review`, `lens_proposed`, `lens_changed`, `held` (photos kept private at ingestion that wait to
be looked over: one item per member, `held:<the batch that opened it>`, saying how many, opening `/held`), `question` (a group of questions dealt to the member, `ask:<group>`, opening
`/inbox?ask=<group>`: [questions and stories](#questions-and-stories)) and `series_update` (new photos that may
belong in a series of the member's, `album:<id>`, opening `/?album=<id>&candidates=1`) and `binding` (their own agent
connected from a new device, `binding:<id>`, opening `/agents`: [agents](#agents-mcp-and-tasks)); each is closed by the
decision it waits for, wherever that is made, not here. Reading the inbox first deals the member the questions
their weekly allowance has room for (`stories.deal`), and leaves out a `question` item none of whose questions
may be shown to them now.

| Request | Does |
|---|---|
| `GET /inbox` | `inbox.html`, the member's inbox (with an `ETag`, `no-cache`) |
| `GET /api/inbox` | `{items: [{id, kind, ref, title, created_at, updated_at, opens}], open}`: the member's open items, newest first (by `updated_at`: a `lens_changed` item brought up to date moves to the top, as does a `held` item a new hold brings up to date; a review only retitles it). `ref` is what it is about (`member:<id>`, `album:<id>`, `lens:<id>`, `held:<batch>`); `opens` the address that decides it (`/members`, `/changes/<id>`, `/?albums`, `/?album=<id>`, `/held`); `open` how many. It first deals the questions the member's allowance has room for (`stories.deal`, with the write lock held: two reads at once deal a question once, into one item) |
| `POST /api/inbox/<id>/dismiss` | Put one of the member's own open items away: `{id, state: "dismissed", open}` (how many are left), and one `inbox.dismissed` journal entry. It decides nothing (a request to be let in stays pending). `404` for an item that is not theirs, `409` for one that is not open, including one decided (or put away from another tab) while the request was on its way |

### Questions and stories

Answered by `PhotoApp.stories_route` with [stories.py](../../photo-estate/stories.py) ([chapter 11](11-server.md#the-estates-agent-listeners-questions-and-stories),
[chapter 26](26-albums-and-agents.md#questions-and-stories)). All take the `MEMBER` rule. A photo the member may
not be shown (`shared_for`: private to someone else, or not in the catalog) is answered as one that does not
exist (`404`), wherever it is named.

| Request | Does |
|---|---|
| `GET /api/questions` | Deals the member what their allowance has room for (as `GET /api/inbox` does), then `{items: [{item, group_key, questions: [{id, text, why, photos, asked_at, asked_by: {id, name, kind}, for, to}]}], questions}`: their open questions as their inbox items (newest item first; `item` is the inbox item's id), each photo a row as `/api/search` gives it, `asked_by.kind` `agent` or `person`, `for` the member an agent's run acted for (or `null`), `to` `"you"` or `"the family"`. A question naming a photo they may not be shown now is left out |
| `POST /api/questions/<id>/answer` | Answer a question dealt to the member. Typed: JSON `{text}`. Spoken: the recording as the whole body, with its `Content-Type` (below), and `?seconds=` its length as the page measured it (kept as the recording's `seconds`: a browser's MP4 says nothing of its length until played; `400` if not a number; left out by the phone). `201` with the story (as `GET /api/photos/<sha>/stories` lists one). Another answer, theirs or anyone's, is another story. `404` for a question not dealt to them, or naming a photo they may not be shown now |
| `POST /api/questions/<id>/dismiss` | "I don't know": `{question, dismissed}`; put away for this member for good (a question to them alone is closed); `question.dismissed` in the journal. It counted against the week it was dealt in. Dismissing the whole inbox item (`POST /api/inbox/<id>/dismiss`) puts all of its questions away |
| `POST /api/stories` | Tell a story with no question (the viewer's "tell a story about this photo"). Typed: JSON `{photos: [sha], text}` (1 to 20 photos). Spoken: the recording as the body with its `Content-Type`, and the photos in the query, `?photos=<sha>,<sha>` (and `&seconds=`, as above). `201` with the story; `story.told` in the journal. `404` for a photo the member may not be shown |
| `GET /api/photos/<sha>/stories` | `{sha, stories: [{id, told_by: {id, name}, told_at, text, question: {id, text} \| null, photos, recording: {url, type, seconds} \| null, transcript, transcribed_at, transcript_state, transcript_error}]}`, oldest first: the stories about the photo the member may read (its teller; anyone else who may be shown every photo of it). `photos` lists those of its photos the member may be shown; `transcript_state` is `null` (no recording), `waiting`, `done` or `failed` (with `transcript_error`, tried again on the next run). `404` for a photo they may not be shown |
| `GET /api/albums/<id>/stories` | A series' stories: `{album, stories}`, those told about any photo of the album that the member sees (as `GET /api/albums/<id>/photos` lists them), newest first, each once. `404` for an album they may not see |
| `GET /api/stories/<id>/recording` | The recording, to a member who may read the story (else `404`), as its type (`audio/mp4` for `.m4a`, `audio/webm`, `audio/wav`, `audio/mpeg`, `audio/ogg`, `audio/aac`), with `Accept-Ranges: bytes`; a `Range` request is answered `206` with `Content-Range` (`416` when it cannot be met), `Cache-Control: private, max-age=86400` |

**A recording upload**, exactly: `POST` with the audio file as the whole body (no form, no JSON, no base64),
`Content-Type` one of `audio/mp4`, `audio/x-m4a`, `audio/m4a` (an iPhone's AVAudioRecorder AAC `.m4a`; Safari's
MediaRecorder `audio/mp4`), `audio/webm` (Chrome and Firefox: Opus), `audio/ogg`, `audio/wav`, `audio/x-wav`,
`audio/wave`, `audio/mpeg`, `audio/aac`; parameters such as `;codecs=opus` are ignored. At most 8 MiB
(`stories.MAX_RECORDING`, the app's `MAX_BODY`: 15 minutes or more of AAC at 64 kbit/s): over it `413` (nothing
read). `415` for another type, `400` for an empty body or one PyAV cannot read as sound. The bytes are stored
in the store content-addressed (`<store>/<sha[:2]>/<sha[2:4]>/<sha><ext>`, immutable where the store is marked,
in the demo's scratch folder with `--demo`), with **no `assets` row**, and transcribed later on the mini by a
queued job ([appendix A](appendix-a-commands.md#transcribepy-the-recordings-of-stories)).

### Agents: MCP and tasks

| Request | Does |
|---|---|
| `POST /mcp` | The [MCP](https://modelcontextprotocol.io/) endpoint ([estate_mcp.py](../../photo-estate/estate_mcp.py)): streamable HTTP in its simplest form, one JSON-RPC message (or a batch) per POST, answered with JSON, no stream and no session. Tools: `library_overview` (with `members`: the family, `[{id, name, person?}]`), `search_photos`, `similar_photos`, `get_photos` (each photo with the `stories` the caller may read), `list_albums`, `get_album`, `create_album` (`search` and `grow: true`: a series), `amend_album`, `propose_hiding`, `list_change_sets`, `get_change_set`, `create_change_set`, `amend_change_set`, `ask_member`, `submit_task` (`family: true` is not offered here: `POST /api/tasks`), `get_task`, `cancel_task`, and for a member's own agent only `journal_since`. `search_photos` takes `kind` (`photo` or `video`), and its rows carry `kind`, `seconds` and `live_photo`. Every tool answers as a member sees the library, and what it makes is theirs: with a member's login, that member's **own agent** (below); from this computer with no login, the sysadmin; and with a run's token, `X-Estate-Agent: <token>` (honoured only from this computer, while that run lasts), the estate's agent acting for the run's member, or for no one (the family archivist: only photos private to nobody; its albums are published family series), journaled with the agent as the actor. A token from another computer, one that is no running task's, or a request carrying `X-Estate-Task` (the task number runs carried before) is `403` with JSON-RPC error `-32001`. A run is not offered the task tools |
| A member's own agent | A call with a member's login (through `tailscale serve`; with `--demo`, `X-Demo-Member` or the cookie) is their agent on the device it came from (`X-Forwarded-For`, named by `tailscale whois`; with `--demo`, `X-Demo-Device`, else "this computer"): an `agent_bindings` row, made `pending` by its first call, with a `binding.requested` entry and a `binding` inbox item. **Pending**: every tool that only looks; a tool that writes answers `isError` with "Not yet: <member> has not allowed this agent (on <device>)…". **Approved**: it writes as the member; each journal entry it makes carries `{"binding": <id>}` in its data. **Revoked**: every call (`initialize` and `tools/list` too) is `403`, JSON-RPC error `-32001`. Never offered `submit_task`, `get_task`, `cancel_task` (called anyway: `isError`, "not for a member's own agent"); `ask_member`'s `follow_up` is refused. Its `initialize` gives other instructions (`OWN_INSTRUCTIONS`): allowed first, no tasks, `journal_since` and series |
| `journal_since` (MCP, own agent) | `{cursor? (0), limit? (100, at most 500)}` → `{entries: [{id, at, kind, subject, by, for, data}], next_cursor, more, waiting?}`: the journal entries after `cursor` that its member may know of, oldest first (`by`, `for`: members' names). At most 5,000 entries are looked at a call (`more`). Which kinds, and for whom, is the table in [journal.py](../../photo-estate/journal.py)'s docstring ([chapter 11](11-server.md#a-members-own-agent)); an `ingest.finished` not yet in the served catalog stops the page (`waiting`) |
| `amend_album`'s `request_done` (MCP) | `true`: the new photos waiting for this series (`series.update_requested`) have been looked at; closes the owner's `series_update` item (after any photos the same call adds), `request_done: true` in the answer. The rest stay out. Another member's album: an error |
| `GET /agents` | `agents.html`, Connect your agent (with an `ETag`, `no-cache`); in the Library's menu |
| `GET /api/agents` | `{bindings: [{id, member, node, device, status, requested_at, decided_at, seen_at}], url, here: {node, device}, me, demo, login}`: the member's own agents, one per device, last seen first; `url`, `/mcp` as this request reached the app (`X-Forwarded-Proto` and `X-Forwarded-Host` through serve, else `Host`); `here`, the device this request came from; `login` (the demo only) for the demo's header. This computer (no member) has none |
| `POST /api/agents/<id> {allow}` | `allow: true` allows the member's agent (approved, also from revoked), `false` turns it off (revoked): `{binding}`, a `binding.approved` or `binding.revoked` entry by the member, and its inbox item done. Allowing from the agent's own device is `403` ("allow it from another device…"): the agent could send any request a browser there could; turning off is allowed from anywhere. `404` for another member's (and for this computer), `400` without a boolean `allow` |
| `ask_member` (MCP) | `{photos: [id] (1-20), text, why?, to?, group_key?, priority? (0-1), follow_up?}`: file a question for one member (`to`: a name or id from `library_overview`'s members) or, without `to`, the family; returns at once `{question, status: "queued", to, photos, group_key, follow_up}`. Refused (an error the agent reads, nothing filed) for a photo the caller does not see, one the recipient may not be shown, or, for the family, one private to anyone. `follow_up: true` on a question the estate's agent asks: an answer starts a new run acting for the same member, with the question and the answer |
| `GET /api/tasks` | Whether an agent can run on this computer, and the member's own tasks (those acting for them; one from before members is the first sysadmin's), newest first ([tasks.py](../../photo-estate/tasks.py)) |
| `GET /api/tasks/<id>?after=N` | One task, its events after event N, and the change set it made; `404` for a task acting for someone else (what its agent saw is what that member sees, their private photos too) |
| `POST /api/tasks {prompt, family?}` | Start one: Claude Code, run headless with only the library's MCP tools. Any member may. The task records who asked (`started_by: "member:<id>"`) and whom it acts for (`acting_for`: the member, the sysadmin's from this computer; with `family: true`, `null`: the family archivist, which sees only photos private to nobody and makes family series). A run a listener starts is recorded the same way, `started_by: "journal:<entry>/album:<id>"` or `"…/story:<id>"`, every task in these answers carries both. A task acting for no one is read by whoever started it and the sysadmins |
| When a task ends | Each hiding set its agent made that has photos and hides none yet becomes a `review` item in the inbox of whoever asked ([the inbox](#the-inbox)) |
| `POST /api/tasks/<id>/cancel` | Stop a task that is queued or running: a queued one never starts, a running one's agent gets SIGTERM, then SIGKILL after ten seconds, and the task stays `cancelled` however the agent ends. Answers the task (`404` if there is none; for a task acting for someone else, only `{id, status, created_at, finished_at}`); one already finished is returned unchanged |

### Bringing photos in (the setup page)

Answered by [intake/api.py](../../photo-estate/intake/api.py), both by the full app and by the small
setup server that runs before there is a library. In the full app every POST takes the write rule (a
job reads any folder on this computer); the setup server, which runs before there is a library to
protect, has no rule of its own.

| Request | Does |
|---|---|
| `GET /setup` | The setup page ([setup.html](../../photo-estate/setup.html)) |
| `GET /api/intake` | The library, every source (with its guide and checks), every job |
| `GET /api/intake/jobs/<id>?after=N` | One job, with the lines it printed after event N |
| `POST /api/intake/library {root}` | Where the library lives (setup only, before anything is in it) |
| `POST /api/intake/preview {source, config}` | What that would bring in; changes nothing |
| `POST /api/intake/jobs {source, config}` | Start bringing it in |
| `POST /api/intake/jobs/<id>/answer {value}` | What a waiting job asked for (a password, a code, "ok") |
| `POST /api/intake/jobs/<id>/cancel`, `/retry` | Stop a job, or run it again. A job cancelled while queued is never started; a running one's step gets SIGTERM, then SIGKILL after ten seconds. Retry is for a failed or cancelled job |
| `POST /api/intake/choose {what: folder or file}` | The Mac's own Open dialog (only from this Mac) |

### Images

| Path | Returns |
|---|---|
| `GET /thumb/<sha>` | 360 px WebP (`image/webp`); a JPEG if it was made before 2026-11-20 and not yet redone |
| `GET /preview/<sha>` | 1600 px JPEG |
| `GET /large/<sha>` | 4096 px JPEG |
| `GET /original/<sha>` | The stored file, for the stills a phone can save (`.jpg`, `.jpeg`, `.png`, `.heic`, `.heif`, `.gif`, `.webp`, `.tif`, `.tiff`, `.avif`; `ORIGINAL_TYPES` in [core/media.py](../../photo-estate/core/media.py)), with that type's `Content-Type` and `Content-Disposition: inline; filename*=UTF-8''<original name>`; 404 for RAW, BMP and video |
| `GET /video/<sha>` | A video in the catalog, or a Live Photo's clip by the clip's sha, as stored, read from the disk as it is sent. `Range: bytes=a-b` (or `a-`, or `-n`) answers `206` with those bytes and `Content-Range`; no `Range`, or one it does not handle (several ranges, not `bytes=`), answers `200` with the whole file; a range that starts at or after the end, or `bytes=-0`, answers `416` with `Content-Range: bytes */<size>`. `Content-Type` from `VIDEO_TYPES` in core/media.py (`.mov` and `.m4v` as `video/mp4`), `Accept-Ranges: bytes`, `Content-Disposition: inline; filename*=UTF-8''<original name>`, `Cache-Control: private, max-age=2592000, immutable`. 404 for anything that is not a video or a clip, or a file not on the disk. Anyone who can reach the app may ask, as for the images. `HEAD` is a 501, as for every route of this server |

For a video, `/thumb/`, `/preview/` and `/large/` are made from its poster frame (about a second in).
A photo private to someone else (phase 5) is answered exactly as a sha the server does not have, on every one
of these: `404`, an empty body (`/video/`: `not found`), `no-store`; so is a Live Photo's clip none of whose
stills the member may be shown, and `GET /face/<id>` for a face in such a photo.
The first four validate `<sha>` as 64 lowercase hex characters of a known photo (else 404) and are
served `public, max-age=2592000, immutable`; `/video/` validates it the same way.

### Comparing two models (an experiment, `serve --compare-vectors DIR`)

`DIR` holds a second model's vectors as `tools/embed_squares.py` writes them: `shard-NNNN.npy` (float16,
unit rows), `shard-NNNN.shas.txt` and `meta.json` (`model`). Without the option these routes are 404.
The second model is loaded by starting a run (`POST /api/compare/run`, or a search) and let go when the run
is finished, or after `--compare-idle` minutes unused; while it loads, `info` says `b.ready: false` and a
search is a 503. `GET /api/info` says `compare: true` when these routes exist.

| Path | Returns |
|---|---|
| `GET /compare` | `compare.html`: start or carry on a run of the fixed searches, vote on each, read the report |
| `GET /api/compare/info` | `{a: {model, photos, dim}, b: {model, photos, dim, ready, failed, meta}, both, votes: {a, b, same}, terms, k, run, reports}`. `terms` are the searches a run asks and `k` the photos a column; `run` is the run under way (`{id, started, terms, k, answers: {search: winner}}`) or null; `reports` the finished runs, newest first (`{id, finished, a, b, count, terms, verdict}`). Looking loads no model |
| `POST /api/compare/run {fresh?}` | The run under way, or a new one (`fresh: true` gives up the one under way), as `info.run`; starts loading the second model. The write rule |
| `POST /api/compare/run/finish {run, early?}` | Ends the run: its report `{id, started, finished, a, b, k, photos, terms, count: {a, b, same, skip}, unanswered, p, verdict, overlap, a_ms, b_ms, rows: [{term, winner, left, overlap, a_ms, b_ms}]}`, also written to `runs/<id>.md`; the second model is let go. 409 with `left` if searches are unanswered and `early` is not true; 404 if that run is not under way |
| `GET /api/compare/report?run=ID` | A finished run's report (404 if there is none, 409 if it is not finished) |
| `GET /api/compare?q=&k=48` | `{q, text, applied, candidates, a, b, overlap}`. `q` is read as `/api/search` reads it: names, places and `key:value` filters choose the photos, the words left over (`text`) are ranked. Both sides rank the same photos (those with a vector from both models), and each of `a` and `b` is `{model, text_ms, rank_ms, results: [{id, score}]}`, the top `k` (at most 200) with no match floor. 400 when there are no words to rank by; 503 while the second model loads |
| `POST /api/compare/vote {q, winner: a\|b\|same, left, k, run?, overlap?, a_ms?, b_ms?}` | With `run`, that run's answer to one of its searches (`winner` may also be `skip`; answered again, it replaces the answer; 404 for a run not under way, 400 for a search not in it). Appends a line to the votes file (`--compare-votes`, default `DIR/votes.jsonl`) and returns `{ok, votes}`. Takes a member (or this computer, else 403), though it writes only that file |

### The People API (also on `faces.py serve`)

People is built once, for every member, without the photos the estate defaults hide. In the photo app a
member's own filters apply to what a GET lists: faces in photos they do not see are left out of each list
(and a person's cover is one they see; a face of a photo they do not see is `404`), while the counts
(`named`, `pending`, `total`, ...) stay People's own.

| Request | Purpose |
|---|---|
| `GET /face/<id>` | The 160 px face crop (regenerated from the original if the cache lacks it); cached one day |
| `GET /api/people` | The overview: faces, photos, named, ignored, pending, people with year counts and cover, and the state of the unknown-groups job (`unknown`: `waiting`, `working`, `ready` or `failed`) |
| `GET /api/people/review?person=ID[&year=Y][&limit=60][&skipped=1]` | Faces waiting for that person, most likely first |
| `GET /api/people/person?person=ID[&year=Y][&limit=12]` | A person's named faces: a few per year, or all of one year |
| `GET /api/people/odd?person=ID[&year=Y][&offset=0][&limit=60]` | A person's named faces, least like the rest first |
| `GET /api/people/unknown[?limit=12]` | Groups of unnamed faces: `{state, groups}`, `state` as in the overview; with `failed`, also `error` (what the grouping raised) and no groups |
| `GET /api/people/photo?face=ID` | One face's photo and every face in it |
| `POST /api/people/label` | Name, forget, rule out, put aside, undo, name a cluster ([chapter 15](15-human-corrections.md) for the body) |
| `POST /api/people/label-many` | `{"items": [label bodies]}`: apply in order, in one transaction (all or none), rescore once |
| `POST /api/people/person` | `{"id", "name"?, "born"?}`: rename (or merge: a name another person has, in any case, moves this person's faces, `face_not` and `face_later` rows to them) and set birth month. Returns `{"ok": true, "id": ID}` (the kept person's id after a merge), or `{"error": "unknown person"}` |

`review`, `person`, `odd` (an unknown or just-merged `person`) and `photo` (an unknown `face`) are 404.
Each POST is one SQLite transaction; if SQLite refuses it (locked, disk full) nothing changes, and the
request fails as any request that raises does: the server prints the error and closes the connection
without an answer ([chapter 15](15-human-corrections.md)).

The three POSTs take the write rule in the photo app (`403` for a viewer); `faces.py serve`, run by
hand on this computer, has no rule.

`POST /api/people/label` body (any combination):

```json
{ "faces": [12, 13], "person": 5,          // person: id | "a name" | 0 (stranger) | null (forget)
  "pending": {"person": 5, "year": 2016},  // instead of "faces": everything waiting for that person
  "cluster": 3,                            // instead of "faces": the whole unknown group
  "not": [14], "not_person": 5,            // rule out
  "later": [15], "later_person": 5,        // put aside
  "undo": [12], "undo_person": 5 }         // take back
```

Returns `{"ok": true, "pending": N}`.

## The `faces.py serve` extras

| Request | Returns |
|---|---|
| `GET /` | 302 to `/people` |
| `GET /preview/<sha>` | A 1600 px JPEG preview, from `review/previews/` (made in the background, newest first, or on demand) |

## The duplicate review server (`estate.py review`)

| Request | Returns |
|---|---|
| `GET /` | `review.html` |
| `GET /groups.json` | `{"groups": [...], "categories": {key: {label, policy, short}}}` |
| `GET /decisions.json` | Choices saved so far |
| `GET /thumb/<sha>`, `/preview/<sha>` | A 360 px WebP and a 1600 px JPEG |
| `POST /decisions` | `{groupId: {keep: [shas], reviewed: bool, category}}`; merged into `decisions.json`; groups with an empty `keep` are ignored |

## Status codes

| Code | When |
|---|---|
| 200 | OK |
| 206 | `/video/<sha>` with a `Range` it can answer |
| 204 | `/api/log` |
| 304 | A conditional request matched the `ETag` |
| 201 | `PUT /api/upload/...` stored a new file (a `200` with `X-Upload-Refused` is a refused one: above) |
| 400 | A malformed request (a number parameter, a body that isn't JSON, a bad `Content-Length`: above), a malformed review decision, or a refused upload |
| 403 | A write from a caller who may not write (above) |
| 404 | Unknown sha, unknown path, `/api/similar` with an unknown photo, or `/api/people/{review,person,odd}` with an unknown person (one just merged away included) and `/api/people/photo` with an unknown face |
| 413 | A JSON body over 8 MB |
| 416 | `/video/<sha>` with a `Range` that starts at or after the end of the file |
| 500 | A part loaded in the background failed (issue #82): a typed search after the search model failed to load (`{"error": "the search model (...) failed to load: ..."}`), or `/people`, `/face/*` and `/api/people/*` after the faces failed to load (the error as text; `/people` as a page that says so) |
| 503 | `/people` before the face data has loaded; a typed search (`/api/search?q=`, `/api/periods` and the rest that read words) while the search model is still loading after waiting 90 s for it (`{"error": "... is still loading ..."}`, `Retry-After: 5`) |
